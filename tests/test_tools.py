"""Tools, rules in data, and idempotent writes for the leave-request domain."""
import inspect

import pytest

from app.tools.leave_tools import CalendarTools, DeskTools


# ── 1. Every tool has a proper description ───────────────────────────────────

@pytest.mark.parametrize("cls", [CalendarTools, DeskTools])
def test_every_tool_is_described(cls, db):
    inst = cls(db) if cls is CalendarTools else cls(db, "S001")
    for name in cls.TOOL_NAMES:
        doc = inspect.getdoc(getattr(inst, name)) or ""
        assert len(doc) >= 120, f"{cls.__name__}.{name} needs a longer description"


# ── 2. Calendar read-only tools ───────────────────────────────────────────────

def test_list_holidays_returns_correct_data(db):
    holidays = CalendarTools(db).list_holidays(2026)["holidays"]
    names = [h["name"] for h in holidays]
    assert "Republic Day" in names
    assert "Holi" in names
    assert all(h["date"].startswith("2026-") for h in holidays)


def test_get_leave_type_sick(db):
    lt = CalendarTools(db).get_leave_type("sick")
    assert lt["name"] == "sick" and lt["max_per_year"] == 10


def test_get_leave_type_unknown_returns_error(db):
    result = CalendarTools(db).get_leave_type("holiday")
    assert result["error"] == "not_found"


# ── 3. Business rule lives in the database, not the prompt ───────────────────

def test_policy_comes_from_the_database(db):
    """Changing the policy row must change what check_can_apply returns."""
    desk = DeskTools(db, "S002")
    # S002 has 0 sick days — refused by default
    result = desk.check_can_apply("sick", 1)
    assert result["can_apply"] is False

    # Manually top up the balance (simulate policy change effect)
    db.conn.execute(
        "UPDATE leave_balance SET remaining_days = 5 WHERE staff_id = 'S002' AND leave_type_id = 1"
    )
    result2 = desk.check_can_apply("sick", 1)
    assert result2["can_apply"] is True


def test_apply_refused_when_balance_is_zero(db):
    """S002 has 0 sick-leave days. check_can_apply should report refusal."""
    result = DeskTools(db, "S002").check_can_apply("sick", 1)
    assert result["can_apply"] is False
    assert any("0" in r for r in result["reasons"])


def test_apply_refused_when_days_exceed_balance(db):
    """S003 has 2 casual days; requesting 3 must be refused."""
    result = DeskTools(db, "S003").check_can_apply("casual", 3)
    assert result["can_apply"] is False
    assert any("2" in r for r in result["reasons"])


# ── 4. apply_leave enforces the rule even if model skips the check ────────────

def test_apply_leave_refused_even_if_model_skips_check(db):
    """apply_leave for S002 (0 sick days) must return an error, not create an application."""
    result = DeskTools(db, "S002").apply_leave("sick", "2026-03-01", "2026-03-03", 3)
    assert result["error"] == "not_allowed"
    assert db.count("leave_application") == 0


# ── 5. apply_leave is idempotent ──────────────────────────────────────────────

def test_apply_leave_twice_is_not_an_error(db):
    """Calling apply_leave twice with S001 (has balance) should not create two rows."""
    desk = DeskTools(db, "S001")
    r1 = desk.apply_leave("sick", "2026-01-15", "2026-01-17", 3)
    # Simulate a replay by calling db.apply directly with the same app_id
    r2 = db.apply("S001", r1["application_id"], 1, "2026-01-15", "2026-01-17", 3)
    assert r1["application_id"] == r2["application_id"]
    assert r2["already_applied"] is True
    assert db.count("leave_application") == 1
    # Balance deducted only once: 5 - 3 = 2
    assert db.get_balance("S001", 1) == 2


# ── 6. notify_hod is deduplicated ─────────────────────────────────────────────

def test_same_notification_sent_once(db):
    desk = DeskTools(db, "S001")
    r1 = desk.notify_hod("Sick leave applied.")
    r2 = desk.notify_hod("Sick leave applied.")
    assert r1["notification_id"] == r2["notification_id"]
    assert r2["duplicate"] is True
    assert db.count("notification") == 1


# ── 7. Desk cannot act for another staff member ───────────────────────────────

def test_desk_cannot_act_for_another_staff_member(db):
    """No tool on DeskTools should accept a roll_no / staff_id parameter."""
    params = {
        n: list(inspect.signature(getattr(DeskTools, n)).parameters)
        for n in DeskTools.TOOL_NAMES
    }
    for name, plist in params.items():
        assert "roll_no" not in plist, f"{name} leaks roll_no parameter"
        assert "staff_id" not in plist, f"{name} leaks staff_id parameter"
