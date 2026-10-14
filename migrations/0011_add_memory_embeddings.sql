-- Derived semantic-search vectors for L3 long-term memories (MEM-NFR-02).
-- The table is a pure derived index: the authoritative record stays in
-- long_term_memories, backups never carry this table, and every lifecycle
-- delete (forget / clear / reconcile / expiry / restore) must drop the row.
-- `revision` mirrors the embedded revision so a revise marks the row stale;
-- the background embedding job re-embeds rows where the revision no longer
-- matches current_revision or the model/dims changed after a config switch.
CREATE TABLE IF NOT EXISTS memory_embeddings (
    memory_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    memory_space_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    revision INTEGER NOT NULL,
    dims INTEGER NOT NULL CHECK(dims BETWEEN 1 AND 8192),
    format INTEGER NOT NULL CHECK(format IN (1, 2)),
    vector BLOB NOT NULL,
    norm REAL NOT NULL,
    model TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

-- Covering index: the SemanticIndexCache watermark probe runs
-- SELECT COUNT(*), MAX(updated_at) per namespace on every query; with
-- updated_at in the index both aggregates are answered from index pages
-- alone, so the freshness check never row-lookups (and SQLCipher-decrypts)
-- the namespace's ~1KB vector payloads. The K3 100k acceptance run showed
-- that row-lookup probe alone costs ~400ms of decrypt per query.
CREATE INDEX IF NOT EXISTS idx_memory_embeddings_namespace
ON memory_embeddings(account_id, agent_id, memory_space_id, updated_at);

-- background_worker_state gains the "embedding" backfill job, but 0008 sealed
-- job_kind with a CHECK whitelist and SQLite cannot ALTER a CHECK constraint.
-- Rebuild the table with the widened whitelist; the copy keeps existing
-- schedule/backoff rows so a daemon restart resumes where it left off.
-- (Indexes drop with the old table and are recreated after the rename.)
CREATE TABLE background_worker_state_v11 (
    account_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    memory_space_id TEXT NOT NULL,
    job_kind TEXT NOT NULL CHECK(job_kind IN ('maintenance','backup','embedding')),
    last_attempt_at INTEGER,
    last_success_at INTEGER,
    next_retry_at INTEGER,
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT,
    last_error_message TEXT,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(account_id, agent_id, memory_space_id, job_kind)
);
INSERT INTO background_worker_state_v11
SELECT account_id, agent_id, memory_space_id, job_kind, last_attempt_at,
       last_success_at, next_retry_at, retry_count, last_error_code,
       last_error_message, updated_at
FROM background_worker_state;
DROP TABLE background_worker_state;
ALTER TABLE background_worker_state_v11 RENAME TO background_worker_state;
CREATE INDEX IF NOT EXISTS idx_background_worker_due
ON background_worker_state(job_kind, next_retry_at, updated_at);
