ALTER TABLE session_events RENAME TO session_events_v1;

CREATE TABLE session_events (
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

INSERT INTO session_events(
    event_id,account_id,agent_id,memory_space_id,session_id,event_sequence,
    event_type,payload,payload_hash,sync_policy,expires_at,created_at,hlc
)
SELECT
    event_id,account_id,agent_id,memory_space_id,session_id,event_sequence,
    event_type,payload,payload_hash,sync_policy,expires_at,created_at,hlc
FROM session_events_v1;

DROP TABLE session_events_v1;

CREATE INDEX idx_session_events_context
ON session_events(account_id, agent_id, session_id, event_sequence);
