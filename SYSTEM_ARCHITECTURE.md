# System Architecture — Leave Request Assistant

## Overview

The Leave Request Assistant is a **multi-agent, durable-execution service** built on two SQLite
databases, a job queue, and a three-agent hierarchy. A staff member submits a plain-English
question; the system processes it reliably even if a worker crashes mid-run, without duplicating
any side effect (leave deduction or HOD notification).

---

## High-Level Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            CLIENT LAYER                                     │
│                                                                             │
│   Staff Member                                                              │
│       │  plain-English question                                             │
│       ▼                                                                     │
│   scripts/ask.py  ──enqueue──▶  agent.db (job queue)                       │
│   scripts/demo.py (scripted)                                                │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           WORKER LAYER                                      │
│                                                                             │
│   scripts/worker.py  ──starts──▶  Worker (app/worker.py)                   │
│                                       │                                    │
│                                  claim_next()  ◀── lease + heartbeat        │
│                                       │                                    │
│                                  execute_run() (app/runner.py)              │
│                                       │                                    │
│                              reap_expired() on startup                      │
│                       (dead worker's run is requeued automatically)         │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         SUPERVISOR AGENT                                    │
│                                                                             │
│   System prompt: "You are the Campus Leave Request Assistant for {roll_no}" │
│   Tools: ask_calendar, ask_desk  (both are delegations, NOT direct tools)  │
│                                                                             │
│   SupervisorTools (app/agents.py)                                          │
│       │                                                                     │
│       ├── ask_calendar ──▶  run_specialist("calendar", ...)                │
│       │                                                                     │
│       └── ask_desk ──────▶  run_specialist("desk", ...)                   │
└──────────────────┬───────────────────────┬──────────────────────────────────┘
                   │                       │
                   ▼                       ▼
┌──────────────────────────┐   ┌──────────────────────────────────────────────┐
│   CALENDAR SPECIALIST    │   │            DESK SPECIALIST                   │
│                          │   │                                              │
│  Prompt: read-only only  │   │  Prompt: acts for {roll_no} only            │
│                          │   │                                              │
│  Tools (read-only):      │   │  Tools (with side effects):                 │
│  • list_holidays(year)   │   │  • get_staff()                              │
│  • get_leave_type(name)  │   │  • check_can_apply(leave_type, days)        │
│                          │   │  • apply_leave(...)        ★ side effect    │
│  No writes. Ever.        │   │  • notify_hod(message)     ★ side effect    │
└──────────────────────────┘   └──────────────────────────────────────────────┘
                                             │
                                             ▼
                               ┌─────────────────────────┐
                               │       leave.db           │
                               │  (business data)         │
                               └─────────────────────────┘
```

---

## Component Breakdown

### 1. Client Layer

| Component | File | Role |
|---|---|---|
| Demo runner | `scripts/demo.py` | Runs two scripted questions end-to-end; also runs the crash demo |
| Live client | `scripts/ask.py` | Enqueues a single question and polls until the run finishes |
| Live worker | `scripts/worker.py` | Starts a `Worker` loop that drains the queue indefinitely |

### 2. Worker Layer

| Component | File | Role |
|---|---|---|
| `Worker` | `app/worker.py` | Claims one run at a time; handles `LeaseLost` and `AgentError` outcomes |
| `execute_run` | `app/runner.py` | Step-by-step execution with heartbeat; rebuilds state from DB on resume |
| `RunStore` | `app/memory.py` | Queue, lease, heartbeat, reaper, conversation history |

**Durable Execution Flow:**
```
claim_next()
    │
    ▼
rebuild()          ◀── reads run_step table to skip already-done steps
    │
    ▼
[loop]
  heartbeat()      ◀── extends lease; returns False if another worker took over
  run_tool()       ◀── calls specialist or reads idempotency cache
  record_tool_call()
    │
    ▼
complete()         ◀── saves reply, marks run succeeded, atomically
```

### 3. Supervisor Agent

The supervisor is the **only agent the worker talks to**. It:
- Has **no domain tools** — only `ask_calendar` and `ask_desk` (delegations)
- Receives the staff member's question and formats sub-tasks for each specialist
- Synthesises the specialists' answers into a single reply

```python
# app/agents.py — SupervisorTools
DELEGATES    = ("ask_calendar", "ask_desk")   # everything is a delegation
SIDE_EFFECTS = ()                             # supervisor itself has no side effects
```

### 4. Calendar Specialist (Read-Only)

- **No write tools at all** — enforced at the class level (`SIDE_EFFECTS = ()`)
- Runs its own mini agent loop inside `run_specialist()`
- Answers questions about holidays and leave-type entitlements

### 5. Desk Specialist (Bound to One Staff Member)

- Bound to `roll_no` at construction time — **cannot act for another person**
- No tool accepts `roll_no` as a parameter (enforced by test)
- Side effects (`apply_leave`, `notify_hod`) run through `db.once()` — exactly-once per idempotency key

---

## Agent Communication Pattern ("Agent as Tool")

```
Supervisor generates:
    tool_call { name: "ask_desk", args: { request: "Apply 3 days sick leave..." } }
          │
          ▼
    run_specialist("desk", DESK_SYSTEM, DeskTools(db, roll_no), ...)
          │
          ▼
    Desk's own mini-loop:
        1. check_can_apply(leave_type="sick", days=3)
        2. apply_leave(leave_type="sick", from_date=..., to_date=..., days=3)
        3. notify_hod(message="...")
        4. Final text answer
          │
          ▼
    Returns: { "agent": "desk", "answer": "...", "tools_used": [...] }
          │
          ▼
    Supervisor sees this as a tool result and composes the final reply
```

---

## Idempotency & Exactly-Once Execution

Every side effect is protected by two layers:

### Layer 1 — `db.once()` (per idempotency key)
```
key = sha256(run_id + step_seq + tool_name + args)

on first call:  execute effect → store (key, result) → return result
on replay:      read stored result → return it (effect NOT run again)
```

### Layer 2 — Application-level deduplication

| Side effect | How deduplicated |
|---|---|
| `apply_leave` | `leave_application.id` is a UUID supplied by the caller; second insert is a no-op |
| `notify_hod` | `notification.dedupe_key = sha256(roll_no + message + date)` — UNIQUE constraint |

---

## Crash Recovery

```
worker-A runs:
  1. apply_leave  ✓  (committed to leave.db + idempotency table)
  2. <<< CRASH >>>   (run_step not recorded in agent.db yet)

lease expires → worker-B picks up the run:
  3. rebuild() — sees no recorded result for step 2
  4. calls apply_leave again with the SAME key
  5. db.once() finds the key → returns stored result, skips the effect
  6. notify_hod ✓  (new, runs once)
  7. complete() → run marked "succeeded"

Result: 1 application, 1 notification — no duplicates
```

---

## Provider Strategy

```
┌──────────────────────────────────────────────┐
│            Model Provider                    │
├──────────────────┬───────────────────────────┤
│ Development /    │ ScriptedProvider           │
│ Tests / Demo     │ RoutedMock / PositionalMock│
│                  │ No network, no quota       │
├──────────────────┼───────────────────────────┤
│ Production       │ OpenAIProvider             │
│                  │ Reads OPENAI_API_KEY       │
│                  │ Default model: gpt-4o-mini │
└──────────────────┴───────────────────────────┘
```

All three agents share one `OpenAIProvider` instance in production; each keeps its own system
prompt and tool list.

---

## File Map

```
library-assistant/
├── schema/
│   ├── agent.sql          # agent.db schema (queue, runs, steps, tool_calls)
│   └── leave.sql          # leave.db schema (domain data)
│
├── app/
│   ├── db.py              # SQLite connect() + transaction() — shared helper
│   ├── memory.py          # RunStore: queue, lease, heartbeat, reaper
│   ├── idempotency.py     # Key derivation (sha256)
│   ├── leave_db.py        # All SQL for leave.db
│   ├── agents.py          # Supervisor + specialist agent loops
│   ├── providers.py       # OpenAIProvider + mock providers
│   ├── config.py          # open_stores(), make_providers()
│   ├── runner.py          # execute_run() — step-by-step, crash-safe
│   ├── worker.py          # Worker class
│   └── tools/
│       ├── dispatch.py    # Generic tool call dispatcher
│       └── leave_tools.py # CalendarTools + DeskTools
│
├── scripts/
│   ├── demo.py            # Two-question demo + crash replay
│   ├── ask.py             # Queue one question, wait for answer
│   ├── worker.py          # Start a live worker loop
│   └── _term.py           # ANSI colour helpers
│
└── tests/
    ├── conftest.py        # Fixtures: db, store, clock
    ├── test_tools.py      # 13 tool-level tests
    ├── test_agents.py     # 7 agent-level tests
    └── test_end_to_end.py # 4 full-stack tests
```

---

## Key Design Decisions

| Decision | Rationale |
|---|---|
| Supervisor has no domain tools | Enforces separation: policy questions go to calendar, writes go to desk |
| Desk is bound to one `roll_no` | Prevents any tool from accidentally acting for the wrong person |
| Business rule in `policy` table | Changing the rule doesn't require a code deploy |
| `apply_leave` enforces balance even if `check_can_apply` was skipped | Safety net against model mistakes |
| Two separate databases | Agent memory is framework-level; domain data is application-level |
| `db.once()` on every side effect | Crashed and replayed worker never creates duplicate applications |
