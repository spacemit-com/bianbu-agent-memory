CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS service_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_sessions (
    account_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    credential_ref TEXT,
    token_expires_at INTEGER,
    last_auth_at INTEGER,
    policy_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    device_name TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_spaces (
    memory_space_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    space_type TEXT NOT NULL,
    external_ref TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE(account_id, agent_id, space_type, external_ref)
);

CREATE TABLE IF NOT EXISTS instructions (
    instruction_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    memory_space_id TEXT NOT NULL,
    scope TEXT NOT NULL CHECK(scope IN ('managed','account','agent','workspace','device')),
    text TEXT NOT NULL,
    rationale TEXT,
    applicability TEXT,
    priority INTEGER NOT NULL,
    source TEXT NOT NULL,
    current_revision INTEGER NOT NULL,
    sync_policy TEXT NOT NULL CHECK(sync_policy IN ('local_only','summary_only','account_agent','full')),
    sensitivity TEXT NOT NULL,
    expires_at INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    hlc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instruction_revisions (
    instruction_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    parent_revision INTEGER,
    payload BLOB NOT NULL,
    payload_hash TEXT NOT NULL,
    author_device_id TEXT NOT NULL,
    author_agent_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    hlc TEXT NOT NULL,
    PRIMARY KEY(instruction_id, revision)
);

CREATE TABLE IF NOT EXISTS session_events (
    event_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    memory_space_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    event_sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN (
      'user_message','assistant_message','tool_call','tool_result',
      'task_started','task_completed','task_failed','checkpoint_marker','summary_updated',
      'memory_delete_proposed'
    )),
    payload BLOB NOT NULL,
    payload_hash TEXT NOT NULL,
    sync_policy TEXT NOT NULL CHECK(sync_policy IN ('local_only','summary_only','account_agent','full')),
    expires_at INTEGER,
    created_at INTEGER NOT NULL,
    hlc TEXT NOT NULL,
    UNIQUE(account_id, agent_id, session_id, event_sequence)
);

CREATE INDEX IF NOT EXISTS idx_session_events_context
ON session_events(account_id, agent_id, session_id, event_sequence);

CREATE TABLE IF NOT EXISTS session_summaries (
    session_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    memory_space_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    parent_revision INTEGER,
    summary_payload BLOB NOT NULL,
    source_event_sequence INTEGER NOT NULL,
    sync_policy TEXT NOT NULL CHECK(sync_policy IN ('local_only','summary_only','account_agent','full')),
    producer TEXT NOT NULL DEFAULT 'agent',
    model_ref TEXT,
    policy_version INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    hlc TEXT NOT NULL,
    is_current INTEGER NOT NULL CHECK(is_current IN (0,1)),
    PRIMARY KEY(session_id, revision)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_session_summary_current
ON session_summaries(account_id, agent_id, session_id) WHERE is_current = 1;

CREATE TABLE IF NOT EXISTS long_term_memories (
    memory_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    memory_space_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('profile','preference','fact','project','episodic','decision','reference')),
    content TEXT NOT NULL,
    summary TEXT,
    tags_json TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL,
    sensitivity TEXT NOT NULL,
    current_revision INTEGER NOT NULL,
    sync_policy TEXT NOT NULL CHECK(sync_policy IN ('local_only','summary_only','account_agent','full')),
    expires_at INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    hlc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_long_term_namespace
ON long_term_memories(account_id, agent_id, memory_space_id, kind, updated_at DESC);

CREATE TABLE IF NOT EXISTS memory_revisions (
    memory_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    parent_revision INTEGER,
    payload BLOB NOT NULL,
    payload_hash TEXT NOT NULL,
    author_device_id TEXT NOT NULL,
    author_agent_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    hlc TEXT NOT NULL,
    PRIMARY KEY(memory_id, revision)
);

CREATE TABLE IF NOT EXISTS evidence_refs (
    memory_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    evidence_type TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    evidence_hash TEXT,
    PRIMARY KEY(memory_id, revision, evidence_type, evidence_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS long_term_fts USING fts5(
    memory_id UNINDEXED,
    content,
    summary,
    tags,
    tokenize = 'trigram'
);

CREATE TABLE IF NOT EXISTS tombstones (
    tombstone_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    record_type TEXT NOT NULL,
    record_id TEXT NOT NULL,
    deleted_revision INTEGER NOT NULL,
    delete_epoch INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    hlc TEXT NOT NULL,
    cloud_ack_at INTEGER,
    UNIQUE(account_id, agent_id, record_type, record_id, delete_epoch)
);

CREATE TABLE IF NOT EXISTS forget_jobs (
    job_id TEXT PRIMARY KEY,
    tombstone_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    state TEXT NOT NULL,
    sync_policy TEXT NOT NULL,
    audit_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    op_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    record_type TEXT NOT NULL,
    record_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    revision INTEGER NOT NULL,
    hybrid_clock TEXT NOT NULL,
    payload_ciphertext BLOB NOT NULL,
    payload_hash TEXT NOT NULL,
    sync_policy TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at INTEGER,
    status TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outbox_pending
ON outbox(account_id, agent_id, status, created_at);

CREATE TABLE IF NOT EXISTS inbox (
    remote_op_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    server_cursor TEXT NOT NULL,
    received_at INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    apply_status TEXT NOT NULL,
    error_code TEXT
);

CREATE TABLE IF NOT EXISTS sync_cursors (
    account_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    local_cursor TEXT,
    server_cursor TEXT,
    last_success_at INTEGER,
    last_error TEXT,
    PRIMARY KEY(account_id, agent_id)
);

CREATE TABLE IF NOT EXISTS sync_conflicts (
    conflict_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    record_type TEXT NOT NULL,
    record_id TEXT NOT NULL,
    local_revision INTEGER NOT NULL,
    remote_revision INTEGER NOT NULL,
    visible_revision INTEGER,
    state TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    resolved_at INTEGER,
    resolution_revision INTEGER
);

CREATE TABLE IF NOT EXISTS sync_jobs (
    job_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS backup_jobs (
    job_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    agent_id TEXT,
    job_type TEXT NOT NULL,
    state TEXT NOT NULL,
    backup_id TEXT,
    progress INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS backup_catalog (
    backup_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    agent_id TEXT,
    capsule_version INTEGER NOT NULL,
    base_backup_id TEXT,
    sync_cursor TEXT,
    manifest_hash TEXT NOT NULL,
    object_ref TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    retention_until INTEGER,
    verification_state TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_spool (
    audit_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    account_id_hash TEXT,
    device_id TEXT,
    agent_id TEXT,
    agent_instance_id TEXT,
    record_type TEXT,
    record_id_hash TEXT,
    action TEXT NOT NULL,
    result TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    export_state TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_export
ON audit_spool(export_state, created_at);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    account_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_blob BLOB NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY(account_id, agent_id, operation, idempotency_key)
);

CREATE TABLE IF NOT EXISTS checkpoint_refs (
    checkpoint_ref TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    agent_instance_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    last_event_sequence INTEGER NOT NULL,
    summary_revision INTEGER NOT NULL,
    outbox_high_watermark INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
