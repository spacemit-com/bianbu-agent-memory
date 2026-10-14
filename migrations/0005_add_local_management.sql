CREATE TABLE IF NOT EXISTS memory_settings (
    account_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    memory_space_id TEXT NOT NULL,
    memory_enabled INTEGER NOT NULL DEFAULT 1 CHECK(memory_enabled IN (0,1)),
    retention_days INTEGER CHECK(retention_days IN (7,30,90)),
    audit_enabled INTEGER NOT NULL DEFAULT 1 CHECK(audit_enabled IN (0,1)),
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(account_id, agent_id, memory_space_id)
);

ALTER TABLE session_summaries ADD COLUMN expires_at INTEGER;

CREATE INDEX IF NOT EXISTS idx_session_events_expiry
ON session_events(account_id, agent_id, memory_space_id, expires_at);

CREATE INDEX IF NOT EXISTS idx_session_summaries_lifecycle
ON session_summaries(account_id, agent_id, memory_space_id, is_current, created_at);

CREATE INDEX IF NOT EXISTS idx_checkpoints_recent
ON checkpoint_refs(account_id, agent_id, agent_instance_id, created_at DESC);
