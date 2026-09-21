# Campus Leave Request Assistant

A small, complete agent service for the **Leave Requests** domain.
A staff member asks a question in plain English. A **supervisor** agent delegates to two
**specialist** agents: a calendar specialist that can only look things up, and a desk specialist
that can apply leave and notify the HOD. The run is a job on a queue — a worker that dies halfway
through does **not** deduct the balance a second time.

```
staff ─▶ queue (agent.db) ─▶ worker ─▶ supervisor ──ask_calendar──▶ calendar agent ─▶ list_holidays
                                                  │                                   get_leave_type
                                                  └─ask_desk───────▶ desk agent ─────▶ get_staff
                                                                                        check_can_apply
                                                                                        apply_leave*
                                                                                        notify_hod*
                                                                          * side effects: run once per key
```

Two SQLite databases:
- **`leave.db`** — staff, leave_type, leave_balance, holiday, leave_application, policy, notification, idempotency
- **`agent.db`** — thread, message, run, run_step, tool_call (agent memory + queue)

---

## Run it (no API key needed)

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m scripts.demo            # two questions, scripted models, every step printed
python -m scripts.demo --crash    # worker dies after applying leave; second worker finishes → PASS
pytest                            # ≥ 18 tests, under a second
```

With a real OpenAI key (`export OPENAI_API_KEY=...`):

```bash
python -m scripts.demo --real                                    # same questions, real model
python -m scripts.worker                                         # terminal 1 (live worker)
python -m scripts.ask --student S001 "I need 2 days sick leave." # terminal 2
```

Set `OPENAI_MODEL` to override the default `gpt-4o-mini`.

---

## Architecture

| Layer | What it does |
|---|---|
| `schema/leave.sql` | leave.db schema with policy table (rule lives in data, not the prompt) |
| `schema/agent.sql` | agent.db schema (queue, runs, steps, tool calls) |
| `app/leave_db.py` | All SQL: migrate/seed, reads, idempotent `apply()`, `once()` wrapper |
| `app/tools/leave_tools.py` | Six tools in two toolsets (CalendarTools, DeskTools) |
| `app/agents.py` | Supervisor + Calendar + Desk prompts; agent loops |
| `app/providers.py` | OpenAIProvider + scripted mocks for tests/demo |
| `app/runner.py` | Step-by-step run executor (crash-safe, lease-aware) |
| `app/worker.py` | Claim → execute → record outcome loop |
| `app/memory.py` | RunStore: queue, heartbeat, reaper, lease |
| `app/idempotency.py` | Stable key derivation |
| `scripts/demo.py` | Two demo questions + crash replay |

---

## Seed data

| Staff | Dept | Sick balance | Casual balance | Scenario |
|---|---|---|---|---|
| S001 Priya Raman | CSE | 5 days | 10 days | Can apply — Q1 |
| S002 Arjun Kumar | IT | 0 days | 10 days | Refused: no sick balance; casual capped — Q2 |
| S003 Divya Sekar | ECE | 5 days | 2 days | Refused: only 2 casual days, 3 requested |

Policy rows: `max_sick_days=10`, `max_casual_days=15`, `advance_notice_days=1`.

Holidays seeded: Republic Day (2026-01-26), Holi (2026-03-14), Independence Day (2026-08-15), Gandhi Jayanti (2026-10-02).

---

## Where each requirement shows up

| Requirement | Where to look |
|---|---|
| Two SQLite databases | `leave.db` vs `agent.db` |
| ≥ 5 tools, every one described | `app/tools/leave_tools.py` |
| Business rule in data | `policy` table in `leave.db`; enforced in `leave_db.check_can_apply` and `apply_leave` |
| Queue + worker + lease | `app/memory.py`, `app/worker.py`, `app/runner.py` |
| Idempotency | `LeaveDb.once()`, `apply()` app_id uniqueness, `record_notification` dedupe key |
| Two agents + least privilege | calendar has no write tools; DeskTools is bound to one roll_no |
| No API key needed | `python -m scripts.demo` and `pytest` work with scripted models |

---

## Known limits (on purpose)

- A specialist's inner steps are not checkpointed; on crash the specialist reruns and idempotency keys prevent duplicates.
- No approval workflow before applying leave (could be added as a Day 5 feature).
- No web UI; use `scripts/ask.py` + `scripts/worker.py` for a live demo.
