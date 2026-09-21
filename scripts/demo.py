"""The whole leave-request project in one command, for the classroom.

    python -m scripts.demo            # scripted models: no key, same output every time
    python -m scripts.demo --real     # same two questions on real OpenAI (needs OPENAI_API_KEY)
    python -m scripts.demo --crash    # kill the run right after apply_leave; a second worker
                                      # finishes without double-deducting the balance → PASS

Uses throwaway databases in a temp folder.
"""
import argparse
import os
import tempfile

from scripts._term import CYAN, DIM, GREEN, RED, RESET, print_step

QUESTIONS = [
    # Q1: Priya applies for sick leave — should succeed
    ("S001", "I need 3 days of sick leave from 2026-01-15 to 2026-01-17. Please apply it and notify my HOD."),
    # Q2: Arjun tries 15-day casual — refused (only 10 remaining)
    ("S002", "Can I take 15 days of casual leave starting next month?"),
]


class Crash(BaseException):
    """Like kill -9: nothing catches it."""


def counts(db) -> str:
    return (f"applications {db.count('leave_application')}   "
            f"notifications {db.count('notification')}   "
            f"idempotency keys {db.count('idempotency')}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--real",  action="store_true")
    p.add_argument("--crash", action="store_true")
    a = p.parse_args()

    tmp = tempfile.mkdtemp(prefix="leave-demo-")
    os.environ["AGENT_DB"] = os.path.join(tmp, "agent.db")
    os.environ["LEAVE_DB"] = os.path.join(tmp, "leave.db")

    from app.config import make_providers, open_stores
    from app.worker import Worker

    store, db = open_stores()
    providers = make_providers(mock=not a.real)
    print(f"{DIM}databases in {tmp}   model: {providers['supervisor'].model}{RESET}")
    print(f"{DIM}before: {counts(db)}{RESET}\n")

    questions = QUESTIONS[:1] if a.crash else QUESTIONS
    for roll_no, text in questions:
        thread = store.create_thread(roll_no)
        run_id = store.enqueue(thread, text, providers["supervisor"].model)
        print(f"{CYAN}{roll_no}>{RESET} {text}")

        if a.crash:
            real_once = db.once

            def once_then_die(key, tool_name, effect):
                result = real_once(key, tool_name, effect)
                if tool_name == "apply_leave":
                    raise Crash()       # application + key committed; nothing after is
                return result

            db.once = once_then_die
            try:
                Worker(store, db, providers, worker_id="worker-A",
                       lease_seconds=60, on_step=print_step).run_once()
            except Crash:
                db.once = real_once
                print(f"\n  {RED}worker-A died right after writing the leave application{RESET}")
                print(f"  {DIM}{counts(db)}; run is '{store.get_run(run_id)['status']}'{RESET}")
                store.clock = lambda: __import__("time").time() + 61   # pretend the lease expired
                print(f"  {DIM}...lease expires, worker-B claims the run{RESET}\n")
            Worker(store, db, providers, worker_id="worker-B",
                   lease_seconds=60, on_step=print_step).run_until_idle()
        else:
            Worker(store, db, providers, worker_id="demo-worker", on_step=print_step).run_until_idle()

        run = store.get_run(run_id)
        colour = GREEN if run["status"] == "succeeded" else RED
        reply = (store.load_history(thread)[-1]["text"]
                 if run["status"] == "succeeded" else run["error_code"])
        print(f"{colour}assistant>{RESET} {reply}")
        print(f"{DIM}run {run_id[:8]} {run['status']} after {run['attempts']} attempt(s), "
              f"{run['tokens_in']}+{run['tokens_out']} supervisor tokens{RESET}\n")

    print(f"after:  {counts(db)}")
    if a.crash:
        # Seed has 0 applications; crash demo adds exactly 1 application + 1 notification
        ok = db.count("leave_application") == 1 and db.count("notification") == 1
        print(f"{GREEN}PASS: one new application, one HOD notification{RESET}"
              if ok else f"{RED}FAIL: duplicates detected{RESET}")


if __name__ == "__main__":
    main()
