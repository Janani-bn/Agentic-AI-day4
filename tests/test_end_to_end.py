"""End-to-end tests: queue → worker → supervisor → specialists → crash → replay."""
import pytest

from app.providers import demo_providers
from app.worker import Worker
from tests.conftest import SimulatedCrash

PRIYA_Q = "I need 3 days of sick leave from 2026-01-15 to 2026-01-17. Please apply it and notify my HOD."
ARJUN_Q = "Can I take 15 days of casual leave starting next month?"


def ask(store, roll_no, text):
    thread = store.create_thread(roll_no)
    return thread, store.enqueue(thread, text, "mock")


# ── 1. Happy path: question goes all the way through ─────────────────────────

def test_question_goes_all_the_way_through(store, db):
    thread, run_id = ask(store, "S001", PRIYA_Q)
    assert Worker(store, db, demo_providers(), worker_id="w").run_until_idle() == [(run_id, "succeeded")]
    run = store.get_run(run_id)
    tools_used = [s["tool_name"] for s in run["steps"] if s["kind"] == "tool"]
    assert "ask_calendar" in tools_used
    assert "ask_desk" in tools_used
    assert db.count("leave_application") == 1
    assert db.count("notification") == 1


# ── 2. Policy refusal is a normal answer (not an error) ──────────────────────

def test_policy_refusal_is_a_normal_answer(store, db):
    thread, run_id = ask(store, "S002", ARJUN_Q)
    Worker(store, db, demo_providers(), worker_id="w").run_until_idle()
    assert store.get_run(run_id)["status"] == "succeeded"
    reply = store.load_history(thread)[-1]["text"]
    assert "cannot" in reply.lower() or "10" in reply
    assert db.count("leave_application") == 0


# ── 3. Crash inside specialist after apply_leave does not duplicate it ────────

def test_crash_after_apply_leave_does_not_duplicate(store, db, clock):
    _, run_id = ask(store, "S001", PRIYA_Q)
    real_once = db.once

    def once_then_die(key, tool_name, effect):
        result = real_once(key, tool_name, effect)
        if tool_name == "apply_leave":
            raise SimulatedCrash()
        return result

    db.once = once_then_die
    with pytest.raises(SimulatedCrash):
        Worker(store, db, demo_providers(), worker_id="A", lease_seconds=30).run_once()
    db.once = real_once

    # Application committed; run still 'running' (lease held)
    assert db.count("leave_application") == 1
    assert store.get_run(run_id)["status"] == "running"

    # Advance past lease expiry; worker-B picks it up and finishes
    clock.advance(31)
    results = Worker(store, db, demo_providers(), worker_id="B", lease_seconds=30).run_until_idle()
    assert results == [(run_id, "succeeded")]
    # Still just one application, and HOD notified once
    assert db.count("leave_application") == 1
    assert db.count("notification") == 1
    assert store.get_run(run_id)["attempts"] == 2


# ── 4. Sending the same question twice creates only one application ────────────

def test_asking_twice_still_one_application(store, db):
    for _ in range(2):
        ask(store, "S001", PRIYA_Q)
    Worker(store, db, demo_providers(), worker_id="w").run_until_idle()
    assert db.count("leave_application") == 1
    assert db.count("notification") == 1
