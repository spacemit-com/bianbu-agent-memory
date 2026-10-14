-- MEM-NFR-01-01: bigram lexical index.
--
-- The trigram tokenizer cannot index two-character CJK terms, so queries
-- like "终端 配色" fell through the FTS stages into full-namespace LIKE
-- scans (measured ~2.8s per query at 100k rows on K3, P95 40s under 32
-- concurrent clients). long_term_fts2 stores segmented tokens — per-run CJK
-- bigrams plus ASCII words — under a unicode61 tokenizer, so every query
-- run with at least two characters has an indexed lookup. The rowid mirrors
-- long_term_memories.rowid so deletes resolve by rowid instead of scanning
-- the index (the legacy memory_id-column delete measured ~110ms at 100k
-- rows).
CREATE VIRTUAL TABLE IF NOT EXISTS long_term_fts2 USING fts5(
    memory_id UNINDEXED,
    terms,
    tokenize = 'unicode61'
);

-- Per-namespace readiness. index_memory maintains fts2 inline for every row
-- written from this schema version on, so namespaces without a state row are
-- treated as ready. A ready=0 row is inserted below only for namespaces that
-- already held rows when this migration ran: their legacy rows are backfilled
-- by the background "lexical" job (cursor-batched, restart-safe), and
-- searches keep using the historical SQL cascade until the backfill marks
-- the namespace ready.
CREATE TABLE IF NOT EXISTS lexical_index_state (
    account_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    memory_space_id TEXT NOT NULL,
    ready INTEGER NOT NULL DEFAULT 0,
    backfill_cursor INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(account_id, agent_id, memory_space_id)
);

INSERT OR IGNORE INTO lexical_index_state(
    account_id, agent_id, memory_space_id, ready, backfill_cursor
)
SELECT DISTINCT account_id, agent_id, memory_space_id, 0, 0
FROM long_term_memories;

-- background_worker_state gains the "lexical" backfill job; 0011 sealed
-- job_kind with a CHECK whitelist and SQLite cannot ALTER a CHECK constraint.
-- Rebuild the table with the widened whitelist, preserving schedule/backoff
-- rows (the copy keeps existing state so a restart resumes where it left
-- off; indexes drop with the old table and are recreated after the rename).
CREATE TABLE background_worker_state_v12 (
    account_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    memory_space_id TEXT NOT NULL,
    job_kind TEXT NOT NULL CHECK(job_kind IN ('maintenance','backup','embedding','lexical')),
    last_attempt_at INTEGER,
    last_success_at INTEGER,
    next_retry_at INTEGER,
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT,
    last_error_message TEXT,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(account_id, agent_id, memory_space_id, job_kind)
);
INSERT INTO background_worker_state_v12
SELECT account_id, agent_id, memory_space_id, job_kind, last_attempt_at,
       last_success_at, next_retry_at, retry_count, last_error_code,
       last_error_message, updated_at
FROM background_worker_state;
DROP TABLE background_worker_state;
ALTER TABLE background_worker_state_v12 RENAME TO background_worker_state;
CREATE INDEX IF NOT EXISTS idx_background_worker_due
ON background_worker_state(job_kind, next_retry_at, updated_at);
