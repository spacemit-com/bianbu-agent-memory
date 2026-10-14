CREATE TABLE IF NOT EXISTS semantic_projection_jobs (
    job_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    memory_space_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    run_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    user_event_id TEXT NOT NULL,
    assistant_event_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','leased','retry','completed','dead')),
    lease_owner TEXT,
    lease_expires_at INTEGER,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at INTEGER,
    provider_result_json TEXT,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(account_id, agent_id, memory_space_id, provider, run_id)
);

CREATE INDEX IF NOT EXISTS idx_semantic_projection_claim
ON semantic_projection_jobs(
    account_id, agent_id, memory_space_id, provider, state, next_retry_at, created_at
);

CREATE TABLE IF NOT EXISTS semantic_projection_mappings (
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    memory_space_id TEXT NOT NULL,
    provider_memory_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    canonical_revision INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('active','delete_proposed','superseded')),
    last_score REAL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(provider, account_id, agent_id, memory_space_id, provider_memory_id)
);

CREATE INDEX IF NOT EXISTS idx_semantic_projection_canonical
ON semantic_projection_mappings(
    account_id, agent_id, memory_space_id, provider, memory_id, state
);
