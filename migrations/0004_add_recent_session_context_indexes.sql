CREATE INDEX IF NOT EXISTS idx_session_events_recent
ON session_events(account_id, agent_id, memory_space_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_session_summaries_recent
ON session_summaries(account_id, agent_id, memory_space_id, created_at DESC)
WHERE is_current = 1;
