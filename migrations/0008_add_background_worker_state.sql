CREATE TABLE IF NOT EXISTS background_worker_state (
    account_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    memory_space_id TEXT NOT NULL,
    job_kind TEXT NOT NULL CHECK(job_kind IN ('maintenance','backup')),
    last_attempt_at INTEGER,
    last_success_at INTEGER,
    next_retry_at INTEGER,
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT,
    last_error_message TEXT,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(account_id, agent_id, memory_space_id, job_kind)
);

CREATE INDEX IF NOT EXISTS idx_background_worker_due
ON background_worker_state(job_kind, next_retry_at, updated_at);
