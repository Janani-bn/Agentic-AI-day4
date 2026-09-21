"""Multi-agent tests: supervisor delegation, specialist loops, key replay, least privilege."""
from app.agents import SupervisorTools, run_specialist, run_tool
from app.providers import ModelTurn, ScriptedProvider, ToolCall, demo_providers
from app.tools.leave_tools import CalendarTools


# ── 1. Supervisor only has delegation tools ───────────────────────────────────

def test_supervisor_only_has_delegation_tools(db):
    tools = SupervisorTools(db, demo_providers(), "S001")
    assert set(tools.functions()) == {"ask_calendar", "ask_desk"}
    assert set(tools.DELEGATES) == set(tools.TOOL_NAMES)


# ── 2. Calendar specialist answers a question ─────────────────────────────────

def test_calendar_specialist_answers_a_question(db):
    result, replayed = run_tool(
        SupervisorTools(db, demo_providers(), "S001"), db, "k1",
        "ask_calendar", {"question": "List public holidays in 2026 and confirm sick leave type details."}
    )
    assert result["agent"] == "calendar"
    assert "get_leave_type" in result["tools_used"]
    assert not replayed


# ── 3. Desk specialist applies leave and notifies HOD ────────────────────────

def test_desk_specialist_applies_and_notifies(db):
    result, _ = run_tool(
        SupervisorTools(db, demo_providers(), "S001"), db, "k1",
        "ask_desk",
        {"request": "Apply 3 days of sick leave from 2026-01-15 to 2026-01-17 for S001 and notify the HOD."}
    )
    assert "apply_leave" in result["tools_used"]
    assert "notify_hod" in result["tools_used"]
    assert db.count("leave_application") == 1
    assert db.count("notification") == 1


# ── 4. Repeated delegation with the same key does nothing twice ───────────────

def test_repeated_delegation_with_same_key_does_nothing_twice(db):
    tools = SupervisorTools(db, demo_providers(), "S001")
    args = {"request": "Apply 3 days of sick leave from 2026-01-15 to 2026-01-17 for S001 and notify the HOD."}
    run_tool(tools, db, "same-key", "ask_desk", args)
    run_tool(tools, db, "same-key", "ask_desk", args)
    # Side effects run at most once each — idempotency key prevents duplicates
    assert db.count("leave_application") == 1
    assert db.count("notification") == 1
    assert db.count("idempotency") == 2   # one key per side-effect tool


# ── 5. Bad delegation arguments are fed back as errors ───────────────────────

def test_bad_delegation_arguments_are_fed_back(db):
    result, _ = run_tool(
        SupervisorTools(db, demo_providers(), "S001"), db, "k",
        "ask_desk", {"request": ""}
    )
    assert result["error"] == "invalid_arguments"


# ── 6. A looping specialist hits the step limit and stops ────────────────────

def test_looping_specialist_stops(db):
    looping = ScriptedProvider(
        [ModelTurn(text=None, tool_calls=[ToolCall("list_holidays", {"year": 2026})])],
        loop=True,
    )
    result = run_specialist("calendar", "sys", CalendarTools(db), db=db,
                             provider=looping, task="x", parent_key="k")
    assert result["error"] == "specialist_step_limit"


# ── 7. Specialists see only their own task (not the whole history) ────────────

def test_specialists_see_only_their_task(db):
    providers = demo_providers()
    run_tool(
        SupervisorTools(db, providers, "S001"), db, "k",
        "ask_calendar",
        {"question": "List public holidays in 2026 and confirm sick leave type details."}
    )
    # Calendar specialist got only its own task, not the supervisor's full conversation
    seen = providers["calendar"].calls[0]
    assert seen == [{"role": "user", "text": "List public holidays in 2026 and confirm sick leave type details."}]
    # Desk was never called
    assert providers["desk"].calls == []
