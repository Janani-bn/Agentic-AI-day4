# Database Architecture — Leave Request Assistant

The system uses **two completely separate SQLite databases**. This separation keeps agent
infrastructure (queue, runs, memory) independent from domain business data, exactly as the
requirements specify.

---

## Database Overview

| Database | File | Purpose |
|---|---|---|
| `leave.db` | `schema/leave.sql` | All domain data: staff, balances, applications, holidays, policy, notifications |
| `agent.db` | `schema/agent.sql` | Agent memory: conversations, runs, job queue, step audit trail |

---

## `leave.db` — Domain Database

### Entity-Relationship Diagram

```
┌─────────────┐       ┌──────────────┐       ┌─────────────────────┐
│    staff    │       │  leave_type  │       │    leave_balance     │
│─────────────│       │──────────────│       │─────────────────────│
│ roll_no  PK │◀──┐   │ id        PK │◀──┐   │ staff_id   FK ──┐   │
│ name        │   │   │ name  UNIQUE │   │   │ leave_type_id FK│──┘ │
│ dept        │   │   │ max_per_year │   │   │ remaining_days  │    │
│ role        │   │   └──────────────┘   │   │   CHECK (≥ 0)   │    │
└─────────────┘   │                      │   └─────────────────────┘
      │           │                      │          PK (staff_id, leave_type_id)
      │           │   ┌──────────────────────────────────────────────┐
      │           │   │          leave_application                   │
      │           │   │──────────────────────────────────────────────│
      └───────────┼──▶│ id            PK  (caller-supplied UUID)     │
                  └───│ staff_id      FK                             │
                      │ leave_type_id FK                             │
                      │ from_date                                    │
                      │ to_date                                      │
                      │ days       CHECK (> 0)                       │
                      │ status     CHECK (pending|approved|rejected  │
                      │                  |withdrawn)                 │
                      │ applied_at DEFAULT now                       │
                      └──────────────────────────────────────────────┘

┌─────────────┐       ┌──────────────┐       ┌──────────────────────┐
│   holiday   │       │   policy     │       │    notification       │
│─────────────│       │──────────────│       │──────────────────────│
│ date     PK │       │ name      PK │       │ id          PK       │
│ name        │       │ value        │       │ staff_id    FK       │
└─────────────┘       └──────────────┘       │ message              │
                                             │ dedupe_key  UNIQUE   │
                      ┌──────────────┐       │ sent_at              │
                      │ idempotency  │       └──────────────────────┘
                      │──────────────│
                      │ key       PK │
                      │ tool_name    │
                      │ result_json  │
                      │ created_at   │
                      └──────────────┘
```

---

### Table Details

#### `staff`
Holds all staff members who can raise leave requests.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `roll_no` | TEXT | **PRIMARY KEY** | Unique staff identifier (e.g. S001) |
| `name` | TEXT | NOT NULL | Full name |
| `dept` | TEXT | NOT NULL | Department (CSE, IT, ECE …) |
| `role` | TEXT | NOT NULL DEFAULT 'student' | `student` or `faculty` |

```sql
-- Example rows
S001 | Priya Raman  | CSE | faculty
S002 | Arjun Kumar  | IT  | faculty
S003 | Divya Sekar  | ECE | student
```

---

#### `leave_type`
Reference table for the types of leave the institution supports.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | INTEGER | **PRIMARY KEY** | Surrogate key |
| `name` | TEXT | NOT NULL, UNIQUE | `sick`, `casual`, or `earned` |
| `max_per_year` | INTEGER | NOT NULL | Maximum days allowed in one year |

```sql
-- Seed data
1 | sick    | 10
2 | casual  | 15
3 | earned  | 20
```

---

#### `leave_balance`
Tracks each staff member's **remaining** days for each leave type.
The `CHECK (remaining_days >= 0)` constraint is the database-level guard against overdraft.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `staff_id` | TEXT | FK → staff.roll_no | Owner of this balance |
| `leave_type_id` | INTEGER | FK → leave_type.id | Which type |
| `remaining_days` | INTEGER | CHECK (≥ 0) | Days still available |
| — | — | **PRIMARY KEY (staff_id, leave_type_id)** | One row per person per type |

```sql
-- Example: Priya's balances
S001 | 1 (sick)    | 5
S001 | 2 (casual)  | 10
S001 | 3 (earned)  | 15

-- Arjun has 0 sick days left → any sick leave request is refused
S002 | 1 (sick)    | 0
```

**How `apply()` uses this table (optimistic deduction):**
```sql
-- Only runs if eligibility check passed
UPDATE leave_balance
   SET remaining_days = remaining_days - :days
 WHERE staff_id = :roll_no AND leave_type_id = :leave_type_id;
-- The CHECK constraint fires if remaining_days would go below 0
```

---

#### `holiday`
Public / gazetted holidays. Used by the calendar specialist to warn if a requested date is a holiday.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `date` | TEXT | **PRIMARY KEY** | ISO-8601 format `YYYY-MM-DD` |
| `name` | TEXT | NOT NULL | Holiday name |

```sql
-- Seeded rows
2026-01-26 | Republic Day
2026-03-14 | Holi
2026-08-15 | Independence Day
2026-10-02 | Gandhi Jayanti
```

---

#### `leave_application`
One row per approved leave request. The `id` is supplied by the caller (a UUID) to make
`apply()` idempotent — inserting the same UUID twice is a no-op.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | TEXT | **PRIMARY KEY** | Caller-supplied UUID (idempotency anchor) |
| `staff_id` | TEXT | FK → staff.roll_no | Who applied |
| `leave_type_id` | INTEGER | FK → leave_type.id | Which type |
| `from_date` | TEXT | NOT NULL | Start date (YYYY-MM-DD) |
| `to_date` | TEXT | NOT NULL | End date (YYYY-MM-DD) |
| `days` | INTEGER | CHECK (> 0) | Working days requested |
| `status` | TEXT | CHECK (pending\|approved\|rejected\|withdrawn) | Current state |
| `applied_at` | TEXT | DEFAULT now | Timestamp |

---

#### `policy` ⭐ Business Rule in Data
This table is the **policy engine**. Rules live here, not in any prompt.
Changing a value takes effect immediately without redeploying code.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `name` | TEXT | **PRIMARY KEY** | Rule name |
| `value` | INTEGER | NOT NULL | Rule value |

```sql
-- Seeded policy rows
max_sick_days        | 10   -- max sick leave per year
max_casual_days      | 15   -- max casual leave per year
advance_notice_days  | 1    -- must apply at least N days ahead
```

**How it enforces rules without a prompt:**
```python
# In leave_db.check_can_apply() — reads the table, never the prompt
balance = get_balance(roll_no, leave_type_id)
if days > balance:
    return {"can_apply": False, "reasons": [f"only {balance} day(s) remaining"]}
```

---

#### `notification`
Stores every HOD notification. The `dedupe_key` (a sha256 hash) ensures the **same message
on the same day is never inserted twice**, even after a crash and replay.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | INTEGER | **PRIMARY KEY** | Auto-increment |
| `staff_id` | TEXT | FK → staff.roll_no | Who triggered the notification |
| `message` | TEXT | NOT NULL | Message body |
| `dedupe_key` | TEXT | NOT NULL, UNIQUE | sha256(roll_no + message + date) |
| `sent_at` | TEXT | DEFAULT now | Timestamp |

```python
# Deduplication key computation (app/idempotency.py)
key = sha256(canonical_json([roll_no, normalised_message, date.isoformat()]))
```

---

#### `idempotency`
Stores the result of every side-effect tool call so that a replayed run returns the stored
result without re-executing the effect.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `key` | TEXT | **PRIMARY KEY** | sha256(run_id + step_seq + tool_name + args) |
| `tool_name` | TEXT | NOT NULL | Which tool produced this result |
| `result_json` | TEXT | NOT NULL | JSON-serialised return value |
| `created_at` | TEXT | DEFAULT now | When the effect first ran |

---

## `agent.db` — Agent Memory Database

### Table Overview

| Table | Purpose |
|---|---|
| `thread` | One conversation per staff member session |
| `message` | Append-only conversation turns (user + model) |
| `run` | One job per queued question; tracks status, lease, attempts |
| `run_step` | Every model turn and tool call recorded in order |
| `tool_call` | Tool call details linked to a `run_step` |

### Schema Diagram

```
┌────────────┐           ┌─────────────────────────────────────────┐
│   thread   │           │                  run                    │
│────────────│           │─────────────────────────────────────────│
│ id      PK │◀──────────│ id             PK                       │
│ student_id │           │ thread_id      FK                       │
│ created_at │           │ status         queued|running|succeeded  │
└────────────┘           │                failed|cancelled|dead    │
      │                  │ model                                   │
      │                  │ tokens_in / tokens_out                  │
      ▼                  │ attempts / max_attempts                 │
┌────────────┐           │ available_at   (unix time, for delay)   │
│  message   │           │ lease_owner    (worker id)              │
│────────────│           │ lease_until    (unix time)              │
│ id      PK │           │ cancel_requested                        │
│ thread_id  │           │ error_code                              │
│ seq        │           └───────────────┬─────────────────────────┘
│ role       │                           │
│ text       │                           ▼
│ created_at │           ┌─────────────────────────────────────────┐
│ UNIQUE     │           │              run_step                   │
│ (thread,   │           │─────────────────────────────────────────│
│  seq)      │           │ id         PK                           │
└────────────┘           │ run_id     FK                           │
                         │ seq        UNIQUE (run_id, seq)         │
                         │ kind       model | tool                 │
                         │ tokens_in / tokens_out  (model steps)   │
                         │ text        (model's words)             │
                         │ tool_calls  (JSON, model steps)         │
                         └───────────────┬─────────────────────────┘
                                         │
                                         ▼
                         ┌─────────────────────────────────────────┐
                         │              tool_call                  │
                         │─────────────────────────────────────────│
                         │ id              PK                      │
                         │ run_step_id     FK (UNIQUE)             │
                         │ tool_name                               │
                         │ args            JSON                    │
                         │ result          JSON                    │
                         │ ok              0 | 1                   │
                         │ latency_ms                              │
                         │ idempotency_key                         │
                         └─────────────────────────────────────────┘
```

### Key Constraints in `agent.db`

```sql
-- Conversations are append-only
CREATE TRIGGER message_no_update BEFORE UPDATE ON message ...
CREATE TRIGGER message_no_delete BEFORE DELETE ON message ...

-- Fast queue query (claim_next uses this index)
CREATE INDEX run_claimable ON run (status, available_at);

-- Each (run, seq) pair is unique — prevents duplicate steps on replay
UNIQUE (run_id, seq)  -- on run_step
```

---

## Data Flow: "Apply Sick Leave" End-to-End

```
1. Staff types question
       │
       ▼
2. agent.db: INSERT thread, INSERT message (role=user), INSERT run (status=queued)

3. Worker claims run
       │  agent.db: UPDATE run SET status=running, lease_owner=..., lease_until=...
       ▼
4. Supervisor calls ask_calendar
       │  agent.db: INSERT run_step (kind=model, tool_calls=[ask_calendar])
       │  agent.db: INSERT run_step (kind=tool), INSERT tool_call
       ▼
5. Calendar specialist calls get_leave_type("sick")
       │  leave.db: SELECT * FROM leave_type WHERE name='sick'
       ▼
6. Supervisor calls ask_desk
       │  agent.db: INSERT run_step (kind=model, tool_calls=[ask_desk])
       ▼
7. Desk specialist calls check_can_apply("sick", 3)
       │  leave.db: SELECT remaining_days FROM leave_balance WHERE staff_id='S001'
       ▼
8. Desk specialist calls apply_leave(...)
       │  leave.db: INSERT leave_application (id=UUID)
       │  leave.db: UPDATE leave_balance SET remaining_days = remaining_days - 3
       │  leave.db: INSERT idempotency (key=sha256(...), result_json=...)
       │  agent.db: INSERT run_step (kind=tool), INSERT tool_call (idempotency_key=...)
       ▼
9. Desk specialist calls notify_hod(...)
       │  leave.db: INSERT notification (dedupe_key=sha256(...))
       ▼
10. Supervisor composes final answer
        │  agent.db: INSERT run_step (kind=model, text="Your leave has been applied...")
        │  agent.db: INSERT message (role=model)
        │  agent.db: UPDATE run SET status=succeeded
```

---

## Seed Data Summary

### `leave_balance` (leave.db)

| Staff | Leave Type | Remaining | Demo Scenario |
|---|---|---|---|
| S001 Priya | sick | 5 | ✅ Can apply 3 days |
| S001 Priya | casual | 10 | ✅ Can apply |
| S001 Priya | earned | 15 | ✅ Can apply |
| S002 Arjun | sick | **0** | ❌ Refused: no balance |
| S002 Arjun | casual | 10 | ❌ Refused: 15 > 10 |
| S002 Arjun | earned | 15 | ✅ Can apply |
| S003 Divya | sick | 5 | ✅ Can apply |
| S003 Divya | casual | **2** | ❌ Refused: 3 > 2 |
| S003 Divya | earned | 5 | ✅ Can apply |

### `policy` (leave.db)

| Rule | Value | Effect |
|---|---|---|
| `max_sick_days` | 10 | Max sick leave per year |
| `max_casual_days` | 15 | Max casual leave per year |
| `advance_notice_days` | 1 | Must apply ≥ 1 day in advance |

### `holiday` (leave.db)

| Date | Name |
|---|---|
| 2026-01-26 | Republic Day |
| 2026-03-14 | Holi |
| 2026-08-15 | Independence Day |
| 2026-10-02 | Gandhi Jayanti |
