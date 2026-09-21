"""leave.db: all SQL for the leave-request domain.

Responsibilities
- migrate()           : apply schema + seed once (idempotent)
- reads               : get_staff, get_leave_type, list_holidays, check_can_apply
- apply()             : optimistic balance deduction — safe to repeat (idempotent row)
- record_notification : deduplicated HOD message
- once()              : exactly-once wrapper used by every side-effect tool
- count()             : test / demo helper
"""
import json
import uuid
from pathlib import Path

from app.db import connect, transaction
from app.idempotency import notification_dedupe_key

SCHEMA = Path(__file__).resolve().parent.parent / "schema" / "leave.sql"


class LeaveDb:
    def __init__(self, path: str = ":memory:"):
        self.conn = connect(path)

    # ── schema + seed ────────────────────────────────────────────────────────

    def migrate(self) -> None:
        self.conn.executescript(SCHEMA.read_text())
        self._seed()

    def _seed(self) -> None:
        """Insert reference data if missing. Safe to call repeatedly."""
        with transaction(self.conn) as c:
            # leave types
            c.executemany(
                "INSERT OR IGNORE INTO leave_type (id, name, max_per_year) VALUES (?, ?, ?)",
                [(1, "sick", 10), (2, "casual", 15), (3, "earned", 20)],
            )
            # staff
            c.executemany(
                "INSERT OR IGNORE INTO staff (roll_no, name, dept, role) VALUES (?, ?, ?, ?)",
                [
                    ("S001", "Priya Raman",  "CSE", "faculty"),
                    ("S002", "Arjun Kumar",  "IT",  "faculty"),
                    ("S003", "Divya Sekar",  "ECE", "student"),
                ],
            )
            # leave balances  (staff × leave_type)
            c.executemany(
                "INSERT OR IGNORE INTO leave_balance (staff_id, leave_type_id, remaining_days)"
                " VALUES (?, ?, ?)",
                [
                    # Priya — can apply for sick and casual
                    ("S001", 1, 5), ("S001", 2, 10), ("S001", 3, 15),
                    # Arjun — no sick balance → refused
                    ("S002", 1, 0), ("S002", 2, 10), ("S002", 3, 15),
                    # Divya — only 2 casual days left → 3-day casual refused
                    ("S003", 1, 5), ("S003", 2, 2),  ("S003", 3, 5),
                ],
            )
            # public holidays
            c.executemany(
                "INSERT OR IGNORE INTO holiday (date, name) VALUES (?, ?)",
                [
                    ("2026-01-26", "Republic Day"),
                    ("2026-03-14", "Holi"),
                    ("2026-08-15", "Independence Day"),
                    ("2026-10-02", "Gandhi Jayanti"),
                ],
            )
            # policy  (business rules live here, NOT in the prompt)
            c.executemany(
                "INSERT OR IGNORE INTO policy (name, value) VALUES (?, ?)",
                [
                    ("max_sick_days",        10),   # max sick leave per year
                    ("max_casual_days",      15),   # max casual leave per year
                    ("advance_notice_days",   1),   # must apply at least N days ahead
                ],
            )

    # ── reads ────────────────────────────────────────────────────────────────

    def get_staff(self, roll_no: str) -> dict | None:
        row = self.conn.execute(
            "SELECT roll_no, name, dept, role FROM staff WHERE roll_no = ?", (roll_no,)
        ).fetchone()
        return dict(row) if row else None

    def get_leave_type(self, name: str) -> dict | None:
        row = self.conn.execute(
            "SELECT id, name, max_per_year FROM leave_type WHERE name = ?", (name.lower(),)
        ).fetchone()
        return dict(row) if row else None

    def list_holidays(self, year: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT date, name FROM holiday WHERE date LIKE ? ORDER BY date",
            (f"{year}-%",),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_balance(self, roll_no: str, leave_type_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT remaining_days FROM leave_balance WHERE staff_id = ? AND leave_type_id = ?",
            (roll_no, leave_type_id),
        ).fetchone()
        return row["remaining_days"] if row else None

    def get_policy(self, name: str) -> int | None:
        row = self.conn.execute(
            "SELECT value FROM policy WHERE name = ?", (name,)
        ).fetchone()
        return row["value"] if row else None

    def check_can_apply(self, roll_no: str, leave_type_id: int, days: int) -> dict:
        """Read-only eligibility check. Returns {can_apply, reasons}."""
        reasons = []
        balance = self.get_balance(roll_no, leave_type_id)
        if balance is None:
            reasons.append("leave type not configured for this staff member")
        elif days > balance:
            reasons.append(
                f"only {balance} day(s) remaining, {days} requested"
            )
        if not reasons:
            return {"can_apply": True, "reasons": []}
        return {"can_apply": False, "reasons": reasons}

    def active_applications(self, roll_no: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM leave_application WHERE staff_id = ? AND status = 'pending'"
            " ORDER BY applied_at",
            (roll_no,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── side effects ─────────────────────────────────────────────────────────

    def apply(
        self,
        roll_no: str,
        app_id: str,
        leave_type_id: int,
        from_date: str,
        to_date: str,
        days: int,
    ) -> dict:
        """Insert a leave application and deduct balance. Safe to repeat: same app_id → same row.

        Enforces the balance rule even if the caller skipped check_can_apply.
        Raises ValueError if balance is insufficient (caught by once() → error dict).
        """
        with transaction(self.conn) as c:
            # Already committed on a previous attempt?
            existing = c.execute(
                "SELECT id, status FROM leave_application WHERE id = ?", (app_id,)
            ).fetchone()
            if existing:
                return {"application_id": existing["id"], "status": existing["status"],
                        "already_applied": True}

            # Enforce balance rule — in data, not the prompt
            balance = self.get_balance(roll_no, leave_type_id)
            if balance is None or days > balance:
                raise ValueError(
                    f"insufficient balance: {balance} day(s) available, {days} requested"
                )

            c.execute(
                "INSERT INTO leave_application (id, staff_id, leave_type_id, from_date, to_date, days)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (app_id, roll_no, leave_type_id, from_date, to_date, days),
            )
            c.execute(
                "UPDATE leave_balance SET remaining_days = remaining_days - ?"
                " WHERE staff_id = ? AND leave_type_id = ?",
                (days, roll_no, leave_type_id),
            )
        return {"application_id": app_id, "status": "pending", "already_applied": False}

    def record_notification(self, roll_no: str, message: str) -> dict:
        """Insert a notification; same message on the same day is a no-op (returns same id)."""
        from datetime import date

        key = notification_dedupe_key(roll_no, message, date.today())
        with transaction(self.conn) as c:
            existing = c.execute(
                "SELECT id FROM notification WHERE dedupe_key = ?", (key,)
            ).fetchone()
            if existing:
                return {"notification_id": existing["id"], "duplicate": True}
            cur = c.execute(
                "INSERT INTO notification (staff_id, message, dedupe_key) VALUES (?, ?, ?)",
                (roll_no, message, key),
            )
            return {"notification_id": cur.lastrowid, "duplicate": False}

    # ── exactly-once wrapper ──────────────────────────────────────────────────

    def once(self, key: str, tool_name: str, effect) -> tuple[dict, bool]:
        """Run effect() at most once per key. Returns (result, fresh).

        fresh=False means the stored result was returned and nothing was done again.
        """
        with transaction(self.conn) as c:
            row = c.execute(
                "SELECT result_json FROM idempotency WHERE key = ?", (key,)
            ).fetchone()
            if row:
                return json.loads(row["result_json"]), False
            result = effect()
            c.execute(
                "INSERT INTO idempotency (key, tool_name, result_json) VALUES (?, ?, ?)",
                (key, tool_name, json.dumps(result, default=str)),
            )
            return result, True

    # ── helper ───────────────────────────────────────────────────────────────

    def count(self, table: str) -> int:
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def new_app_id(self) -> str:
        """Stable UUID for a new application (caller passes this in)."""
        return str(uuid.uuid4())
