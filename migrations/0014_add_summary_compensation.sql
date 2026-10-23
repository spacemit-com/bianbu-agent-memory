-- Extend the persisted scheduler without losing existing retry schedules.
ALTER TABLE background_worker_state RENAME TO background_worker_state_v12;
DROP INDEX IF EXISTS idx_background_worker_due;
CREATE TABLE background_worker_state (
    account_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    memory_space_id TEXT NOT NULL,
    job_kind TEXT NOT NULL CHECK(job_kind IN ('maintenance','backup','embedding','lexical','summary')),
    last_attempt_at INTEGER,
    last_success_at INTEGER,
    next_retry_at INTEGER,
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT,
    last_error_message TEXT,
    updated_at INTEGER NOT NULL,
    scan_offset INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(account_id, agent_id, memory_space_id, job_kind)
);
INSERT INTO background_worker_state (
    account_id,agent_id,memory_space_id,job_kind,last_attempt_at,last_success_at,
    next_retry_at,retry_count,last_error_code,last_error_message,updated_at
) SELECT * FROM background_worker_state_v12;
DROP TABLE background_worker_state_v12;
CREATE INDEX idx_background_worker_due
ON background_worker_state(job_kind, next_retry_at, updated_at);
