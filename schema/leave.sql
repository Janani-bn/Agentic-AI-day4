-- leave.db: all business data for the leave-request assistant.
-- Agent memory (queue, runs, steps) lives in agent.db, not here.

-- ── staff ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS staff (
    roll_no    TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    dept       TEXT NOT NULL,
    role       TEXT NOT NULL DEFAULT 'student'   -- 'student' | 'faculty'
);

-- ── leave types ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS leave_type (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,          -- 'sick', 'casual', 'earned'
    max_per_year   INTEGER NOT NULL
);

-- ── remaining balance per staff per leave type ─────────────────────────────
CREATE TABLE IF NOT EXISTS leave_balance (
    staff_id        TEXT NOT NULL REFERENCES staff (roll_no),
    leave_type_id   INTEGER NOT NULL REFERENCES leave_type (id),
    remaining_days  INTEGER NOT NULL CHECK (remaining_days >= 0),
    PRIMARY KEY (staff_id, leave_type_id)
);

-- ── public holidays ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS holiday (
    date  TEXT PRIMARY KEY,                        -- ISO-8601 'YYYY-MM-DD'
    name  TEXT NOT NULL
);

-- ── leave applications ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS leave_application (
    id              TEXT PRIMARY KEY,              -- caller-supplied UUID (idempotency)
    staff_id        TEXT NOT NULL REFERENCES staff (roll_no),
    leave_type_id   INTEGER NOT NULL REFERENCES leave_type (id),
    from_date       TEXT NOT NULL,
    to_date         TEXT NOT NULL,
    days            INTEGER NOT NULL CHECK (days > 0),
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'approved', 'rejected', 'withdrawn')),
    applied_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ── business-rule policy (rule lives in data, not in the prompt) ───────────
CREATE TABLE IF NOT EXISTS policy (
    name   TEXT PRIMARY KEY,
    value  INTEGER NOT NULL
);

-- ── HOD / staff notifications ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS notification (
    id          INTEGER PRIMARY KEY,
    staff_id    TEXT NOT NULL REFERENCES staff (roll_no),
    message     TEXT NOT NULL,
    dedupe_key  TEXT NOT NULL UNIQUE,              -- same msg same day → same row
    sent_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ── exactly-once side effects ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS idempotency (
    key          TEXT PRIMARY KEY,
    tool_name    TEXT NOT NULL,
    result_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
