ALTER TABLE memory_settings ADD COLUMN backup_enabled INTEGER NOT NULL DEFAULT 0
    CHECK(backup_enabled IN (0,1));
ALTER TABLE memory_settings ADD COLUMN backup_frequency TEXT NOT NULL DEFAULT 'weekly'
    CHECK(backup_frequency IN ('manual','daily','weekly','monthly'));
-- Tightened with the 0.1.16 scope convergence: `summaries` (L2 conversation
-- summaries) must not be uploaded (MEM-L4-02). Databases created before that
-- release keep their stored value and converge it on read/write instead.
ALTER TABLE memory_settings ADD COLUMN backup_scopes_json TEXT NOT NULL DEFAULT
    '["preferences","development_habits","schedule_habits","important"]';

ALTER TABLE backup_catalog ADD COLUMN device_id TEXT;
ALTER TABLE backup_catalog ADD COLUMN memory_space_id TEXT;
ALTER TABLE backup_catalog ADD COLUMN scopes_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE backup_catalog ADD COLUMN object_size INTEGER NOT NULL DEFAULT 0;
ALTER TABLE backup_catalog ADD COLUMN crypto_suite TEXT NOT NULL DEFAULT
    'AES-256-GCM+SHA256';

CREATE INDEX IF NOT EXISTS idx_backup_catalog_scope
ON backup_catalog(account_id, agent_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_backup_jobs_scope
ON backup_jobs(account_id, agent_id, created_at DESC);
