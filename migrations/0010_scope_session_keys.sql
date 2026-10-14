-- Scope Session identity and sequence keys by the complete Memory namespace.
-- Older schemas keyed these rows only by account/agent/session (and the event
-- sequence UNIQUE constraint omitted memory_space_id), so two Runtime-selected
-- Agent contexts could collide when they used the same session id.

CREATE TABLE session_events_v10 (
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
    payload_encoding TEXT NOT NULL DEFAULT 'json',
    payload_hash TEXT NOT NULL,
    sync_policy TEXT NOT NULL CHECK(sync_policy IN ('local_only','summary_only','account_agent','full')),
    expires_at INTEGER,
    created_at INTEGER NOT NULL,
    hlc TEXT NOT NULL,
    UNIQUE(account_id, agent_id, memory_space_id, session_id, event_sequence)
);

INSERT INTO session_events_v10(
    event_id,account_id,agent_id,memory_space_id,session_id,event_sequence,event_type,
    payload,payload_encoding,payload_hash,sync_policy,expires_at,created_at,hlc
)
SELECT event_id,account_id,agent_id,memory_space_id,session_id,event_sequence,event_type,
       payload,COALESCE(payload_encoding,'json'),payload_hash,sync_policy,expires_at,created_at,hlc
FROM session_events;

DROP TABLE session_events;
ALTER TABLE session_events_v10 RENAME TO session_events;

CREATE INDEX idx_session_events_context
ON session_events(account_id, agent_id, memory_space_id, session_id, event_sequence);
CREATE INDEX idx_session_events_recent
ON session_events(account_id, agent_id, memory_space_id, created_at DESC);
CREATE INDEX idx_session_events_expiry
ON session_events(account_id, agent_id, memory_space_id, expires_at);

CREATE TABLE session_summaries_v10 (
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
    expires_at INTEGER,
    PRIMARY KEY(session_id, account_id, agent_id, memory_space_id, revision)
);

INSERT INTO session_summaries_v10(
    session_id,account_id,agent_id,memory_space_id,revision,parent_revision,
    summary_payload,source_event_sequence,sync_policy,producer,model_ref,
    policy_version,created_at,hlc,is_current,expires_at
)
SELECT session_id,account_id,agent_id,memory_space_id,revision,parent_revision,
       summary_payload,source_event_sequence,sync_policy,producer,model_ref,
       policy_version,created_at,hlc,is_current,expires_at
FROM session_summaries;

DROP TABLE session_summaries;
ALTER TABLE session_summaries_v10 RENAME TO session_summaries;

CREATE UNIQUE INDEX idx_session_summary_current
ON session_summaries(account_id, agent_id, memory_space_id, session_id)
WHERE is_current = 1;
CREATE INDEX idx_session_summaries_recent
ON session_summaries(account_id, agent_id, memory_space_id, created_at DESC);
CREATE INDEX idx_session_summaries_lifecycle
ON session_summaries(account_id, agent_id, memory_space_id, is_current, created_at);
