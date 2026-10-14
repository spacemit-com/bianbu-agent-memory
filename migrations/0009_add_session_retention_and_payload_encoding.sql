ALTER TABLE session_events ADD COLUMN payload_encoding TEXT NOT NULL DEFAULT 'json';

CREATE TABLE IF NOT EXISTS session_retention_manifests (
    session_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    memory_space_id TEXT NOT NULL,
    archive_status TEXT NOT NULL CHECK(archive_status IN ('pending','summarized','projected','verified','deleted','blocked')),
    source_event_high_watermark INTEGER NOT NULL,
    l2_summary_revision INTEGER,
    l3_memory_count INTEGER NOT NULL DEFAULT 0,
    pending_projection_count INTEGER NOT NULL DEFAULT 0,
    verified_at INTEGER,
    deleted_at INTEGER,
    last_error TEXT,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(account_id, agent_id, memory_space_id, session_id)
);

CREATE INDEX IF NOT EXISTS idx_session_retention_recent
ON session_retention_manifests(account_id, agent_id, memory_space_id, archive_status, updated_at DESC);
