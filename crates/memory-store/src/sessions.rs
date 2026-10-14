use crate::{
    db_error, decode_json_payload, encode_json_payload, hash_bytes, json_bytes, parse_params,
    ServiceResult, Store,
};
use memory_core::{allowed_event_type, allowed_sync_policy, MemoryIdentity, MAX_TEXT_BYTES};
use memory_protocol::{RequestEnvelope, ServiceError};
use rusqlite::{params, OptionalExtension};
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::time::Duration;
use uuid::Uuid;

const MAX_MODEL_RESPONSE_BYTES: usize = 1 * 1024 * 1024;

#[derive(Deserialize)]
struct AppendEvent {
    session_id: String,
    event_type: String,
    payload: Value,
    #[serde(default = "default_event_sync")]
    sync_policy: String,
    #[serde(default)]
    expires_at: Option<i64>,
}

fn default_event_sync() -> String {
    "local_only".to_owned()
}

#[derive(Deserialize)]
struct UpdateSummary {
    session_id: String,
    patch: Value,
    source_event_sequence: i64,
    expected_revision: i64,
    #[serde(default = "default_summary_sync")]
    sync_policy: String,
    #[serde(default = "default_producer")]
    producer: String,
    #[serde(default)]
    model_ref: Option<String>,
}

fn default_summary_sync() -> String {
    "summary_only".to_owned()
}
fn default_producer() -> String {
    "agent".to_owned()
}

#[derive(Deserialize)]
struct GetContext {
    session_id: String,
    #[serde(default)]
    include_events: bool,
    #[serde(default = "default_true")]
    include_summary: bool,
    #[serde(default = "default_event_limit")]
    event_limit: i64,
}

#[derive(Deserialize)]
struct GetRecentContext {
    #[serde(default)]
    exclude_session_id: Option<String>,
    #[serde(default = "default_turn_limit")]
    turn_limit: i64,
    #[serde(default = "default_summary_limit")]
    summary_limit: i64,
    #[serde(default)]
    selection_mode: String,
    #[serde(default = "default_session_limit")]
    session_limit: i64,
    #[serde(default = "default_recent_event_limit")]
    event_limit_per_session: i64,
}

fn default_true() -> bool {
    true
}
fn default_event_limit() -> i64 {
    100
}

fn default_turn_limit() -> i64 {
    5
}

fn default_summary_limit() -> i64 {
    3
}

fn default_session_limit() -> i64 {
    5
}

fn default_recent_event_limit() -> i64 {
    5000
}

#[derive(Deserialize)]
struct GetHistory {
    session_id: String,
    #[serde(default)]
    before_sequence: Option<i64>,
    #[serde(default = "default_history_limit")]
    limit: i64,
}

fn default_history_limit() -> i64 {
    200
}

#[derive(Deserialize)]
struct ArchiveOld {
    #[serde(default = "default_session_limit")]
    keep_sessions: i64,
    #[serde(default)]
    dry_run: bool,
}

#[derive(Deserialize)]
struct CompactSession {
    session_id: String,
    #[serde(default = "default_auto")]
    level: String,
    #[serde(default = "default_target_tokens")]
    target_tokens: i64,
}

#[derive(Deserialize)]
struct SummarizeSession {
    session_id: String,
    #[serde(default = "default_summary_interval")]
    interval_turns: i64,
    #[serde(default = "default_turn_limit")]
    keep_raw_turns: i64,
    #[serde(default)]
    force: bool,
}

fn default_summary_interval() -> i64 {
    5
}

fn default_auto() -> String {
    "auto".to_owned()
}
fn default_target_tokens() -> i64 {
    2000
}

#[derive(Deserialize)]
struct CheckpointRequest {
    session_id: String,
}

#[derive(Deserialize)]
struct RestoreCheckpointRequest {
    checkpoint_ref: String,
}

/// Return the stable identifier used to pair a user message with its final
/// assistant response. Runtime writes `run_id` (and newer clients may write
/// `turn_id`) into both event payloads. Events without either identifier are
/// retained for audit, but are never considered a complete L1 turn.
fn event_turn_key(payload: &[u8]) -> Option<String> {
    let value = serde_json::from_slice::<Value>(payload).ok()?;
    let object = value.as_object()?;
    for field in ["turn_id", "run_id"] {
        if let Some(value) = object.get(field).and_then(Value::as_str) {
            let value = value.trim();
            if !value.is_empty() {
                return Some(value.to_owned());
            }
        }
    }
    None
}

fn event_is_eligible(payload: &[u8]) -> bool {
    let Ok(value) = serde_json::from_slice::<Value>(payload) else {
        return false;
    };
    let Some(object) = value.as_object() else {
        return false;
    };
    if object
        .get("text")
        .and_then(Value::as_str)
        .map(str::trim)
        .map(str::is_empty)
        .unwrap_or(true)
    {
        return false;
    }
    match object.get("status").and_then(Value::as_str) {
        Some(status)
            if matches!(
                status.to_ascii_lowercase().as_str(),
                "cancelled"
                    | "canceled"
                    | "timeout"
                    | "timed_out"
                    | "failed"
                    | "error"
                    | "aborted"
                    | "incomplete"
                    | "pending"
                    | "streaming"
            ) =>
        {
            false
        }
        _ => true,
    }
}

/// Filter a session's raw events down to user/assistant pairs with the same
/// turn identifier. Unmatched, duplicated, or out-of-order messages are
/// intentionally omitted from L1/L2 processing.
fn completed_turns(messages: &[(i64, String, Vec<u8>)]) -> Vec<((i64, Vec<u8>), (i64, Vec<u8>))> {
    let mut pending_users = BTreeMap::<String, (i64, Vec<u8>)>::new();
    let mut completed = Vec::new();
    for (sequence, event_type, payload) in messages {
        if !event_is_eligible(payload) {
            continue;
        }
        let Some(turn_key) = event_turn_key(payload) else {
            // Events without a stable turn/run identifier are retained only
            // as audit data and cannot become an L1/L2 memory turn.
            continue;
        };
        match event_type.as_str() {
            "user_message" => {
                pending_users.insert(turn_key, (*sequence, payload.clone()));
            }
            "assistant_message" => {
                if let Some(user) = pending_users.remove(&turn_key) {
                    completed.push((user, (*sequence, payload.clone())));
                }
            }
            _ => {}
        }
    }
    completed.sort_by_key(|(_, assistant)| assistant.0);
    completed
}

fn recent_event_turn_key(message: &Value) -> Option<String> {
    let payload = message.get("payload")?;
    event_turn_key(&serde_json::to_vec(payload).ok()?)
}

fn recent_completed_turns(messages: &[Value]) -> Vec<(Value, Value)> {
    let mut pending_users = BTreeMap::<(String, String), Value>::new();
    let mut completed = Vec::new();
    for message in messages {
        let Some(session_id) = message.get("session_id").and_then(Value::as_str) else {
            continue;
        };
        let Some(event_type) = message.get("event_type").and_then(Value::as_str) else {
            continue;
        };
        let Some(payload) = message.get("payload") else {
            continue;
        };
        let Ok(payload_bytes) = serde_json::to_vec(payload) else {
            continue;
        };
        if !event_is_eligible(&payload_bytes) {
            continue;
        }
        if let Some(turn_key) = recent_event_turn_key(message) {
            let key = (session_id.to_owned(), turn_key);
            match event_type {
                "user_message" => {
                    pending_users.insert(key, message.clone());
                }
                "assistant_message" => {
                    if let Some(user) = pending_users.remove(&key) {
                        completed.push((user, message.clone()));
                    }
                }
                _ => {}
            }
            continue;
        }
        // Missing turn/run identifiers are audit-only and never form a turn.
    }
    completed.sort_by_key(|(_, assistant)| {
        assistant
            .get("created_at")
            .and_then(Value::as_i64)
            .unwrap_or_default()
    });
    completed
}

impl Store {
    pub(crate) fn append_event(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: AppendEvent = parse_params(request)?;
        validate_session_id(&input.session_id)?;
        if !allowed_event_type(&input.event_type) {
            return Err(ServiceError::invalid("invalid event_type"));
        }
        if !allowed_sync_policy(&input.sync_policy) || input.sync_policy == "account_agent" {
            return Err(ServiceError::invalid(
                "session events must use local_only, summary_only, or full",
            ));
        }
        if input.sync_policy == "full" {
            return Err(ServiceError::permission_denied());
        }
        let logical_payload = json_bytes(&input.payload)?;
        if logical_payload.len() > MAX_TEXT_BYTES {
            return Err(ServiceError::invalid("event payload is too large"));
        }
        let payload_hash = hash_bytes(&logical_payload);
        let (payload_blob, payload_encoding) = encode_json_payload(&input.payload)?;
        // MEM-L1-01: only a completed assistant turn may trigger the automatic
        // L2 summary. Tool/task events are still recorded into summary content
        // when a summary is produced, but they are neither a trigger source nor
        // a gate bypass.
        let should_auto_summarize = input.event_type == "assistant_message";
        let summary_session_id = input.session_id.clone();
        let product_config = self.product_config()?;
        let summary_interval_turns = product_config.summary_interval_turns;
        let recent_turn_limit = product_config.recent_turn_limit;
        let result = self.with_write(identity, request, "sessions.append_event", "session_event", |tx, hlc, now, _, _| {
            let expires_at = if input.expires_at.is_none()
                && matches!(input.event_type.as_str(), "tool_call" | "tool_result" | "task_started" | "task_completed" | "task_failed")
            {
                Some(now.saturating_add(7 * 24 * 60 * 60 * 1000))
            } else {
                Store::effective_expiry(tx, identity, input.expires_at, now)?
            };
            let event_sequence: i64 = tx
                .query_row(
                    "SELECT COALESCE(MAX(event_sequence),0)+1 FROM session_events
                     WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND session_id=?4",
                    params![identity.account_id, identity.agent_id, identity.memory_space_id, input.session_id],
                    |row| row.get(0),
                )
                .map_err(db_error)?;
            let event_id = Uuid::new_v4().to_string();
            tx.execute(
                "INSERT INTO session_events(
                    event_id,account_id,agent_id,memory_space_id,session_id,event_sequence,event_type,
                    payload,payload_encoding,payload_hash,sync_policy,expires_at,created_at,hlc
                 ) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14)",
                params![
                    event_id, identity.account_id, identity.agent_id, identity.memory_space_id,
                    input.session_id, event_sequence, input.event_type, payload_blob, payload_encoding,
                    payload_hash, input.sync_policy, expires_at, now, hlc
                ],
            ).map_err(db_error)?;
            Store::enqueue_outbox(
                tx, identity, "session_event", &event_id, "append", event_sequence,
                hlc, &payload_hash, &input.sync_policy, now,
            )?;
            Ok((json!({
                "event_id":event_id,
                "session_id":input.session_id,
                "event_sequence":event_sequence,
                "event_type":input.event_type,
                "created_at":now,
                "hlc":hlc
            }), Some(event_id)))
        })?;
        if should_auto_summarize {
            let mut summary_request = request.clone();
            summary_request.method = "sessions.summarize".to_owned();
            summary_request.request_id = Uuid::new_v4().to_string();
            summary_request.params = json!({
                "session_id":summary_session_id,
                "interval_turns":summary_interval_turns,
                "keep_raw_turns":recent_turn_limit
            });
            summary_request.idempotency_key = Some(format!(
                "memoryd:auto-summary:{}:{}",
                summary_session_id,
                result["event_sequence"].as_i64().unwrap_or_default()
            ));
            // The event transaction has already committed. Summary generation is
            // deliberately best-effort so a bad or unavailable producer can never
            // roll back or delete the authoritative L1 source messages.
            let _ = self.summarize_session(identity, &summary_request);
        }
        Ok(result)
    }

    pub(crate) fn update_summary(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: UpdateSummary = parse_params(request)?;
        validate_session_id(&input.session_id)?;
        validate_summary_patch(&input.patch)?;
        if input.expected_revision < 0 || input.source_event_sequence < 0 {
            return Err(ServiceError::invalid(
                "revision and source sequence cannot be negative",
            ));
        }
        if !allowed_sync_policy(&input.sync_policy) || input.sync_policy == "full" {
            return Err(ServiceError::invalid("invalid summary sync_policy"));
        }
        let summary_retention_days = self.product_config()?.summary_retention_days;
        self.with_write(identity, request, "sessions.update_summary", "session_summary", |tx, hlc, now, _, _| {
            let current: Option<(i64, Vec<u8>, i64)> = tx
                .query_row(
                    "SELECT revision,summary_payload,source_event_sequence FROM session_summaries
                     WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND session_id=?4 AND is_current=1",
                    params![identity.account_id, identity.agent_id, identity.memory_space_id, input.session_id],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                )
                .optional()
                .map_err(db_error)?;
            let current_revision = current.as_ref().map(|row| row.0).unwrap_or(0);
            if current_revision != input.expected_revision {
                return Err(ServiceError::conflict(format!(
                    "expected revision {}, current revision is {}",
                    input.expected_revision, current_revision
                )));
            }
            let mut summary = current
                .as_ref()
                .and_then(|row| serde_json::from_slice::<Value>(&row.1).ok())
                .unwrap_or_else(empty_summary);
            merge_summary(&mut summary, &input.patch)?;
            let summary_blob = json_bytes(&summary)?;
            if summary_blob.len() > MAX_TEXT_BYTES {
                return Err(ServiceError::invalid("summary is too large"));
            }
            let max_event_sequence: i64 = tx
                .query_row(
                    "SELECT COALESCE(MAX(event_sequence),0) FROM session_events
                     WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND session_id=?4",
                    params![identity.account_id, identity.agent_id, identity.memory_space_id, input.session_id],
                    |row| row.get(0),
                )
                .map_err(db_error)?;
            if input.source_event_sequence > max_event_sequence {
                return Err(ServiceError::invalid(
                    "source_event_sequence is beyond the latest session event",
                ));
            }
            let revision = current_revision + 1;
            tx.execute(
                "UPDATE session_summaries SET is_current=0
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND session_id=?4 AND is_current=1",
                params![identity.account_id, identity.agent_id, identity.memory_space_id, input.session_id],
            ).map_err(db_error)?;
            tx.execute(
                "INSERT INTO session_summaries(
                    session_id,account_id,agent_id,memory_space_id,revision,parent_revision,
                    summary_payload,source_event_sequence,sync_policy,producer,model_ref,
                    policy_version,created_at,hlc,is_current,expires_at
                 ) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,1,?12,?13,1,?14)",
                params![
                    input.session_id,identity.account_id,identity.agent_id,identity.memory_space_id,
                    revision, if current_revision == 0 {None} else {Some(current_revision)}, summary_blob,
                    input.source_event_sequence,input.sync_policy,input.producer,input.model_ref,now,hlc,
                    now.saturating_add(summary_retention_days * 24 * 60 * 60 * 1000)
                ],
            ).map_err(db_error)?;
            let payload_hash = hash_bytes(&summary_blob);
            Store::enqueue_outbox(
                tx, identity, "session_summary", &input.session_id, "revise", revision,
                hlc, &payload_hash, &input.sync_policy, now,
            )?;
            Ok((json!({
                "session_id":input.session_id,
                "revision":revision,
                "parent_revision":if current_revision == 0 {Value::Null} else {json!(current_revision)},
                "source_event_sequence":input.source_event_sequence,
                "summary":summary,
                "sync_policy":input.sync_policy,
                "producer":input.producer,
                "model_ref":input.model_ref,
                "hlc":hlc
            }), Some(input.session_id.clone())))
        })
    }

    pub(crate) fn get_context(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: GetContext = parse_params(request)?;
        validate_session_id(&input.session_id)?;
        if !(1..=200).contains(&input.event_limit) {
            return Err(ServiceError::invalid(
                "event_limit must be between 1 and 200",
            ));
        }
        let connection = self.connection()?;
        let summary = if input.include_summary {
            let row: Option<(i64, Vec<u8>, i64, String, String, Option<String>, String)> = connection
                .query_row(
                    "SELECT revision,summary_payload,source_event_sequence,sync_policy,producer,model_ref,hlc
                     FROM session_summaries
                     WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND session_id=?4 AND is_current=1",
                    params![identity.account_id, identity.agent_id, identity.memory_space_id, input.session_id],
                    |row| Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?,row.get(4)?,row.get(5)?,row.get(6)?)),
                )
                .optional()
                .map_err(db_error)?;
            row.map(
                |(revision, blob, source, policy, producer, model_ref, hlc)| {
                    json!({
                        "revision":revision,
                        "summary":serde_json::from_slice::<Value>(&blob).unwrap_or(Value::Null),
                        "source_event_sequence":source,
                        "sync_policy":policy,
                        "producer":producer,
                        "model_ref":model_ref,
                        "hlc":hlc
                    })
                },
            )
        } else {
            None
        };
        let events = if input.include_events {
            let mut statement = connection
                .prepare(
                    "SELECT event_id,event_sequence,event_type,payload,payload_encoding,created_at,hlc,sync_policy
                     FROM session_events
                     WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND session_id=?4
                     ORDER BY event_sequence DESC LIMIT ?5",
                )
                .map_err(db_error)?;
            let rows = statement
                .query_map(
                    params![
                        identity.account_id,
                        identity.agent_id,
                        identity.memory_space_id,
                        input.session_id,
                        input.event_limit
                    ],
                    |row| {
                        let payload: Vec<u8> = row.get(3)?;
                        let encoding: String = row.get(4)?;
                        let decoded =
                            decode_json_payload(&payload, &encoding).map_err(|error| {
                                rusqlite::Error::ToSqlConversionFailure(Box::new(error))
                            })?;
                        Ok(json!({
                            "event_id":row.get::<_,String>(0)?,
                            "event_sequence":row.get::<_,i64>(1)?,
                            "event_type":row.get::<_,String>(2)?,
                            "payload":decoded,
                            "created_at":row.get::<_,i64>(5)?,
                            "hlc":row.get::<_,String>(6)?,
                            "sync_policy":row.get::<_,String>(7)?
                        }))
                    },
                )
                .map_err(db_error)?;
            let mut values = rows.collect::<Result<Vec<_>, _>>().map_err(db_error)?;
            values.reverse();
            values
        } else {
            Vec::new()
        };
        let latest: i64 = connection
            .query_row(
                "SELECT COALESCE(MAX(event_sequence),0) FROM session_events
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND session_id=?4",
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id,
                    input.session_id
                ],
                |row| row.get(0),
            )
            .map_err(db_error)?;
        Ok(json!({
            "session_id":input.session_id,
            "last_event_sequence":latest,
            "summary":summary,
            "events":events
        }))
    }

    pub(crate) fn get_recent_context(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: GetRecentContext = parse_params(request)?;
        if let Some(session_id) = input.exclude_session_id.as_deref() {
            validate_session_id(session_id)?;
        }
        if !(1..=50).contains(&input.turn_limit) {
            return Err(ServiceError::invalid("turn_limit must be between 1 and 50"));
        }
        if !(0..=20).contains(&input.summary_limit) {
            return Err(ServiceError::invalid(
                "summary_limit must be between 0 and 20",
            ));
        }

        if input.selection_mode == "recent_sessions" {
            if !(1..=20).contains(&input.session_limit) {
                return Err(ServiceError::invalid(
                    "session_limit must be between 1 and 20",
                ));
            }
            if !(1..=5000).contains(&input.event_limit_per_session) {
                return Err(ServiceError::invalid(
                    "event_limit_per_session must be between 1 and 5000",
                ));
            }
            return self.get_recent_sessions_context(identity, &input);
        }

        if input.selection_mode == "session_status" {
            if !(1..=20).contains(&input.session_limit) {
                return Err(ServiceError::invalid(
                    "session_limit must be between 1 and 20",
                ));
            }
            return self.get_session_status_context(identity, &input);
        }

        let connection = self.connection()?;
        let scan_limit = (input.turn_limit * 8).min(400);
        let mut statement = connection
            .prepare(
                "SELECT event_id,session_id,event_sequence,event_type,payload,payload_encoding,created_at,hlc,sync_policy
                 FROM session_events
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3
                   AND event_type IN ('user_message','assistant_message')
                   AND (?4 IS NULL OR session_id != ?4)
                   AND (expires_at IS NULL OR expires_at > (unixepoch('subsec') * 1000))
                 ORDER BY created_at DESC,rowid DESC LIMIT ?5",
            )
            .map_err(db_error)?;
        let rows = statement
            .query_map(
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id,
                    input.exclude_session_id,
                    scan_limit
                ],
                |row| {
                    let payload: Vec<u8> = row.get(4)?;
                    let encoding: String = row.get(5)?;
                    let decoded = decode_json_payload(&payload, &encoding).map_err(|error| {
                        rusqlite::Error::ToSqlConversionFailure(Box::new(error))
                    })?;
                    Ok(json!({
                        "event_id":row.get::<_,String>(0)?,
                        "session_id":row.get::<_,String>(1)?,
                        "event_sequence":row.get::<_,i64>(2)?,
                        "event_type":row.get::<_,String>(3)?,
                        "payload":decoded,
                        "created_at":row.get::<_,i64>(6)?,
                        "hlc":row.get::<_,String>(7)?,
                        "sync_policy":row.get::<_,String>(8)?
                    }))
                },
            )
            .map_err(db_error)?;
        let mut scanned = rows.collect::<Result<Vec<_>, _>>().map_err(db_error)?;
        scanned.reverse();
        let completed_turns = recent_completed_turns(&scanned);
        let first = completed_turns
            .len()
            .saturating_sub(input.turn_limit as usize);
        let messages = completed_turns
            .into_iter()
            .skip(first)
            .flat_map(|(user, assistant)| [user, assistant])
            .collect::<Vec<_>>();

        let summaries = if input.summary_limit == 0 {
            Vec::new()
        } else {
            let mut statement = connection
                .prepare(
                    "SELECT session_id,revision,summary_payload,source_event_sequence,
                            sync_policy,producer,model_ref,created_at,hlc
                     FROM session_summaries
                     WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3
                       AND is_current=1 AND (?4 IS NULL OR session_id != ?4)
                     ORDER BY created_at DESC,rowid DESC LIMIT ?5",
                )
                .map_err(db_error)?;
            let rows = statement
                .query_map(
                    params![
                        identity.account_id,
                        identity.agent_id,
                        identity.memory_space_id,
                        input.exclude_session_id,
                        input.summary_limit
                    ],
                    |row| {
                        let payload: Vec<u8> = row.get(2)?;
                        Ok(json!({
                            "session_id":row.get::<_,String>(0)?,
                            "revision":row.get::<_,i64>(1)?,
                            "summary":serde_json::from_slice::<Value>(&payload).unwrap_or(Value::Null),
                            "source_event_sequence":row.get::<_,i64>(3)?,
                            "sync_policy":row.get::<_,String>(4)?,
                            "producer":row.get::<_,String>(5)?,
                            "model_ref":row.get::<_,Option<String>>(6)?,
                            "created_at":row.get::<_,i64>(7)?,
                            "hlc":row.get::<_,String>(8)?
                        }))
                    },
                )
                .map_err(db_error)?;
            rows.collect::<Result<Vec<_>, _>>().map_err(db_error)?
        };

        Ok(json!({
            "excluded_session_id":input.exclude_session_id,
            "turn_limit":input.turn_limit,
            "completed_turns":messages.len() / 2,
            "summaries":summaries,
            "messages":messages
        }))
    }

    fn get_recent_sessions_context(
        &self,
        identity: &MemoryIdentity,
        input: &GetRecentContext,
    ) -> ServiceResult<Value> {
        let connection = self.connection()?;
        let mut statement = connection
            .prepare(
                "SELECT event_id,session_id,event_sequence,event_type,payload,payload_encoding,
                        created_at,hlc,sync_policy
                 FROM session_events
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3
                   AND (?4 IS NULL OR session_id != ?4)
                   AND (expires_at IS NULL OR expires_at > (unixepoch('subsec') * 1000))
                 ORDER BY created_at,rowid",
            )
            .map_err(db_error)?;
        let rows = statement
            .query_map(
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id,
                    input.exclude_session_id
                ],
                |row| {
                    let blob: Vec<u8> = row.get(4)?;
                    let encoding: String = row.get(5)?;
                    let payload = decode_json_payload(&blob, &encoding).map_err(|error| {
                        rusqlite::Error::ToSqlConversionFailure(Box::new(error))
                    })?;
                    Ok(json!({
                        "event_id":row.get::<_,String>(0)?,
                        "session_id":row.get::<_,String>(1)?,
                        "event_sequence":row.get::<_,i64>(2)?,
                        "event_type":row.get::<_,String>(3)?,
                        "payload":payload,
                        "created_at":row.get::<_,i64>(6)?,
                        "hlc":row.get::<_,String>(7)?,
                        "sync_policy":row.get::<_,String>(8)?
                    }))
                },
            )
            .map_err(db_error)?;
        let events = rows.collect::<Result<Vec<_>, _>>().map_err(db_error)?;
        let mut by_session = HashMap::<String, Vec<Value>>::new();
        for event in events {
            if let Some(session_id) = event.get("session_id").and_then(Value::as_str) {
                by_session
                    .entry(session_id.to_owned())
                    .or_default()
                    .push(event);
            }
        }
        let mut candidates = Vec::<(i64, String, Vec<Value>, Vec<(Value, Value)>)>::new();
        for (session_id, session_events) in by_session {
            let completed = recent_completed_turns(&session_events);
            let Some(last) = completed.last() else {
                continue;
            };
            let last_completed_at = last
                .1
                .get("created_at")
                .and_then(Value::as_i64)
                .unwrap_or_default();
            candidates.push((last_completed_at, session_id, session_events, completed));
        }
        candidates.sort_by(|left, right| right.0.cmp(&left.0));
        candidates.truncate(input.session_limit as usize);
        let mut sessions = Vec::with_capacity(candidates.len());
        for (last_completed_at, session_id, mut session_events, completed) in candidates {
            let completed_turn_count = completed.len();
            let messages = completed
                .iter()
                .flat_map(|(user, assistant)| [user.clone(), assistant.clone()])
                .collect::<Vec<_>>();
            session_events.sort_by_key(|item| {
                item.get("event_sequence")
                    .and_then(Value::as_i64)
                    .unwrap_or_default()
            });
            let total_events = session_events.len();
            let has_older_events = total_events > input.event_limit_per_session as usize;
            if has_older_events {
                let start = total_events - input.event_limit_per_session as usize;
                session_events = session_events.split_off(start);
            }
            let summary = connection
                .query_row(
                    "SELECT revision,summary_payload,source_event_sequence,sync_policy,producer,model_ref,created_at,hlc
                     FROM session_summaries WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3
                       AND session_id=?4 AND is_current=1",
                    params![identity.account_id,identity.agent_id,identity.memory_space_id,session_id],
                    |row| {
                        let blob: Vec<u8> = row.get(1)?;
                        Ok(json!({
                            "revision":row.get::<_,i64>(0)?,
                            "summary":serde_json::from_slice::<Value>(&blob).unwrap_or(Value::Null),
                            "source_event_sequence":row.get::<_,i64>(2)?,
                            "sync_policy":row.get::<_,String>(3)?,
                            "producer":row.get::<_,String>(4)?,
                            "model_ref":row.get::<_,Option<String>>(5)?,
                            "created_at":row.get::<_,i64>(6)?,
                            "hlc":row.get::<_,String>(7)?
                        }))
                    },
                )
                .optional()
                .map_err(db_error)?;
            sessions.push(json!({
                "session_id":session_id,
                "last_completed_at":last_completed_at,
                "completed_turn_count":completed_turn_count,
                "returned_event_count":session_events.len(),
                "event_count":total_events,
                "has_older_events":has_older_events,
                "summary":summary,
                "messages":messages,
                "events":session_events
            }));
        }
        Ok(json!({
            "selection_mode":"recent_sessions",
            "session_limit":input.session_limit,
            "sessions":sessions
        }))
    }

    /// One-line-per-session status list for default new-session injection.
    /// Unlike `recent_sessions` mode this carries no message bodies: each
    /// session contributes only metadata (title, last activity, turn count,
    /// in-progress/interrupted flags), and sessions without any completed
    /// turn are still listed so an in-execution first run stays visible.
    fn get_session_status_context(
        &self,
        identity: &MemoryIdentity,
        input: &GetRecentContext,
    ) -> ServiceResult<Value> {
        let connection = self.connection()?;
        let mut statement = connection
            .prepare(
                "SELECT session_id,event_sequence,event_type,payload,payload_encoding,created_at
                 FROM session_events
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3
                   AND event_type IN ('user_message','assistant_message','task_started','task_failed')
                   AND (?4 IS NULL OR session_id != ?4)
                   AND (expires_at IS NULL OR expires_at > (unixepoch('subsec') * 1000))
                 ORDER BY created_at,rowid",
            )
            .map_err(db_error)?;
        let rows = statement
            .query_map(
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id,
                    input.exclude_session_id
                ],
                |row| {
                    let blob: Vec<u8> = row.get(3)?;
                    let encoding: String = row.get(4)?;
                    let payload = decode_json_payload(&blob, &encoding).map_err(|error| {
                        rusqlite::Error::ToSqlConversionFailure(Box::new(error))
                    })?;
                    Ok(json!({
                        "session_id":row.get::<_,String>(0)?,
                        "event_sequence":row.get::<_,i64>(1)?,
                        "event_type":row.get::<_,String>(2)?,
                        "payload":payload,
                        "created_at":row.get::<_,i64>(5)?
                    }))
                },
            )
            .map_err(db_error)?;
        let events = rows.collect::<Result<Vec<_>, _>>().map_err(db_error)?;
        let mut by_session = HashMap::<String, Vec<Value>>::new();
        for event in events {
            if let Some(session_id) = event.get("session_id").and_then(Value::as_str) {
                by_session
                    .entry(session_id.to_owned())
                    .or_default()
                    .push(event);
            }
        }
        // The scan above is restricted to conversation-relevant event types;
        // fold tool/task timestamps in through this cheap aggregate so
        // last_active_at reflects any activity, not just messages.
        let mut statement = connection
            .prepare(
                "SELECT session_id,MAX(created_at) FROM session_events
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3
                   AND (?4 IS NULL OR session_id != ?4)
                   AND (expires_at IS NULL OR expires_at > (unixepoch('subsec') * 1000))
                 GROUP BY session_id",
            )
            .map_err(db_error)?;
        let rows = statement
            .query_map(
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id,
                    input.exclude_session_id
                ],
                |row| Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?)),
            )
            .map_err(db_error)?;
        let last_active_map = rows
            .collect::<Result<HashMap<String, i64>, _>>()
            .map_err(db_error)?;

        let mut statuses = Vec::with_capacity(by_session.len());
        for (session_id, session_events) in by_session {
            let mut completed_turn_count = 0usize;
            let mut last_completed_seq: i64 = 0;
            let mut pending_users = BTreeMap::<String, i64>::new();
            let mut first_user_text: Option<String> = None;
            let mut first_audit_text: Option<String> = None;
            for event in &session_events {
                let Some(event_type) = event.get("event_type").and_then(Value::as_str) else {
                    continue;
                };
                let sequence = event
                    .get("event_sequence")
                    .and_then(Value::as_i64)
                    .unwrap_or_default();
                let payload = event.get("payload").cloned().unwrap_or(Value::Null);
                let Ok(payload_bytes) = serde_json::to_vec(&payload) else {
                    continue;
                };
                let payload_text = || {
                    payload
                        .get("text")
                        .and_then(Value::as_str)
                        .map(str::trim)
                        .filter(|value| !value.is_empty())
                        .map(str::to_owned)
                };
                match event_type {
                    "user_message" => {
                        if !event_is_eligible(&payload_bytes) {
                            continue;
                        }
                        if first_user_text.is_none() {
                            first_user_text = payload_text();
                        }
                        if let Some(turn_key) = event_turn_key(&payload_bytes) {
                            pending_users.insert(turn_key, sequence);
                        }
                    }
                    "assistant_message" => {
                        if !event_is_eligible(&payload_bytes) {
                            continue;
                        }
                        if let Some(turn_key) = event_turn_key(&payload_bytes) {
                            if pending_users.remove(&turn_key).is_some() {
                                completed_turn_count += 1;
                                last_completed_seq = last_completed_seq.max(sequence);
                            }
                        }
                    }
                    "task_started" | "task_failed" => {
                        if first_audit_text.is_none() {
                            first_audit_text = payload_text();
                        }
                    }
                    _ => {}
                }
            }
            // A session is in progress when its latest activity trails the
            // last completed turn without finishing: either an unmatched
            // user message or a run-start marker with no outcome yet.
            let pending_after_completion = pending_users
                .values()
                .any(|sequence| *sequence > last_completed_seq);
            let last_event = session_events.last();
            let suffix_type = last_event.and_then(|event| {
                let sequence = event
                    .get("event_sequence")
                    .and_then(Value::as_i64)
                    .unwrap_or_default();
                if sequence > last_completed_seq {
                    event.get("event_type").and_then(Value::as_str)
                } else {
                    None
                }
            });
            let in_progress = pending_after_completion || suffix_type == Some("task_started");
            let interrupted = !in_progress && suffix_type == Some("task_failed");
            let last_active_at = last_active_map
                .get(&session_id)
                .copied()
                .or_else(|| {
                    last_event.and_then(|event| event.get("created_at").and_then(Value::as_i64))
                })
                .unwrap_or_default();
            let title = first_user_text
                .or(first_audit_text)
                .map(|text| clip_utf8(&text, 48));
            statuses.push((
                last_active_at,
                session_id.clone(),
                json!({
                    "session_id":session_id,
                    "title":title,
                    "last_active_at":last_active_at,
                    "completed_turn_count":completed_turn_count,
                    "in_progress":in_progress,
                    "interrupted":interrupted
                }),
            ));
        }
        statuses.sort_by(|left, right| {
            right
                .0
                .cmp(&left.0)
                .then_with(|| left.1.cmp(&right.1))
        });
        statuses.truncate(input.session_limit as usize);
        let sessions = statuses
            .into_iter()
            .map(|(_, _, value)| value)
            .collect::<Vec<_>>();
        Ok(json!({
            "selection_mode":"session_status",
            "session_limit":input.session_limit,
            "sessions":sessions
        }))
    }

    pub(crate) fn get_history(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: GetHistory = parse_params(request)?;
        validate_session_id(&input.session_id)?;
        if !(1..=5000).contains(&input.limit) {
            return Err(ServiceError::invalid(
                "history limit must be between 1 and 5000",
            ));
        }
        let connection = self.connection()?;
        let mut statement = connection
            .prepare(
                "SELECT event_id,event_sequence,event_type,payload,payload_encoding,created_at,hlc,sync_policy
                 FROM session_events WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3
                   AND session_id=?4 AND (?5 IS NULL OR event_sequence<?5)
                 ORDER BY event_sequence DESC LIMIT ?6",
            )
            .map_err(db_error)?;
        let rows = statement
            .query_map(
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id,
                    input.session_id,
                    input.before_sequence,
                    input.limit
                ],
                |row| {
                    let blob: Vec<u8> = row.get(3)?;
                    let encoding: String = row.get(4)?;
                    let payload = decode_json_payload(&blob, &encoding).map_err(|error| {
                        rusqlite::Error::ToSqlConversionFailure(Box::new(error))
                    })?;
                    Ok(json!({
                        "event_id":row.get::<_,String>(0)?,"event_sequence":row.get::<_,i64>(1)?,
                        "event_type":row.get::<_,String>(2)?,"payload":payload,
                        "created_at":row.get::<_,i64>(5)?,"hlc":row.get::<_,String>(6)?,
                        "sync_policy":row.get::<_,String>(7)?
                    }))
                },
            )
            .map_err(db_error)?;
        let mut events = rows.collect::<Result<Vec<_>, _>>().map_err(db_error)?;
        events.reverse();
        let next_before_sequence = events
            .first()
            .and_then(|event| event.get("event_sequence"))
            .and_then(Value::as_i64);
        Ok(
            json!({"session_id":input.session_id,"events":events,"next_before_sequence":next_before_sequence}),
        )
    }

    pub(crate) fn archive_old_sessions(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: ArchiveOld = parse_params(request)?;
        if !(1..=100).contains(&input.keep_sessions) {
            return Err(ServiceError::invalid(
                "keep_sessions must be between 1 and 100",
            ));
        }
        let connection = self.connection()?;
        let mut statement = connection
            .prepare(
                "SELECT session_id,MAX(created_at) FROM session_events
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3
                 GROUP BY session_id ORDER BY MAX(created_at) DESC",
            )
            .map_err(db_error)?;
        let rows = statement
            .query_map(
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id
                ],
                |row| Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?)),
            )
            .map_err(db_error)?;
        let all = rows.collect::<Result<Vec<_>, _>>().map_err(db_error)?;
        let candidates = all
            .into_iter()
            .skip(input.keep_sessions as usize)
            .collect::<Vec<_>>();
        if input.dry_run {
            return Ok(json!({"keep_sessions":input.keep_sessions,"dry_run":true,
                "candidates":candidates.iter().map(|(id, created)| json!({"session_id":id,"last_event_at":created})).collect::<Vec<_>>() }));
        }
        let mut results = Vec::new();
        for (session_id, _) in candidates {
            let result = self.archive_one_session(identity, request, &session_id)?;
            results.push(result);
        }
        Ok(json!({"keep_sessions":input.keep_sessions,"dry_run":false,"sessions":results}))
    }

    fn archive_one_session(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
        session_id: &str,
    ) -> ServiceResult<Value> {
        let mut summary_request = request.clone();
        summary_request.method = "sessions.summarize".to_owned();
        summary_request.request_id = Uuid::new_v4().to_string();
        summary_request.idempotency_key = Some(format!("archive-summary:{session_id}"));
        summary_request.params =
            json!({"session_id":session_id,"interval_turns":1,"keep_raw_turns":1,"force":true});
        let summary = self.summarize_session(identity, &summary_request)?;
        let summary_completed_turns = summary
            .get("completed_turns")
            .and_then(Value::as_i64)
            .unwrap_or_default();
        if summary_completed_turns == 0 {
            return Ok(json!({
                "session_id": session_id,
                "state": "blocked",
                "reason": "no_completed_turns_for_l2"
            }));
        }
        let source_sequence = summary
            .get("source_event_sequence")
            .and_then(Value::as_i64)
            .unwrap_or(0);
        let revision = summary.get("revision").and_then(Value::as_i64).unwrap_or(0);
        let connection = self.connection()?;
        let mut event_statement = connection
            .prepare(
                "SELECT event_sequence,event_type,payload,payload_encoding FROM session_events
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND session_id=?4
                   AND event_type IN ('user_message','assistant_message') ORDER BY event_sequence",
            )
            .map_err(db_error)?;
        let event_rows = event_statement
            .query_map(
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id,
                    session_id
                ],
                |row| {
                    let blob: Vec<u8> = row.get(2)?;
                    let encoding: String = row.get(3)?;
                    let payload = decode_json_payload(&blob, &encoding).map_err(|error| {
                        rusqlite::Error::ToSqlConversionFailure(Box::new(error))
                    })?;
                    Ok((
                        row.get::<_, i64>(0)?,
                        row.get::<_, String>(1)?,
                        json_bytes(&payload).map_err(|error| {
                            rusqlite::Error::ToSqlConversionFailure(Box::new(error))
                        })?,
                    ))
                },
            )
            .map_err(db_error)?;
        let event_rows = event_rows
            .collect::<Result<Vec<_>, _>>()
            .map_err(db_error)?;
        let completed = completed_turns(&event_rows);
        let last_sequence = completed
            .last()
            .map(|(_, assistant)| assistant.0)
            .unwrap_or_default();
        if source_sequence < last_sequence {
            return Ok(
                json!({"session_id":session_id,"state":"blocked","reason":"l2_summary_does_not_cover_session"}),
            );
        }
        let pending_projection: i64 = connection.query_row(
            "SELECT COUNT(*) FROM semantic_projection_jobs WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND session_id=?4 AND state NOT IN ('completed')",
            params![identity.account_id,identity.agent_id,identity.memory_space_id,session_id], |row| row.get(0)).map_err(db_error)?;
        if pending_projection != 0 {
            return Ok(
                json!({"session_id":session_id,"state":"blocked","reason":"projection_not_complete","pending_projection_count":pending_projection}),
            );
        }
        let now = memory_core::now_ms();
        let mut connection = self.connection()?;
        let transaction = connection.transaction().map_err(db_error)?;
        transaction.execute(
            "INSERT INTO session_retention_manifests(session_id,account_id,agent_id,memory_space_id,archive_status,source_event_high_watermark,l2_summary_revision,l3_memory_count,pending_projection_count,verified_at,updated_at)
             VALUES(?1,?2,?3,?4,'verified',?5,?6,0,0,?7,?7)
             ON CONFLICT(account_id,agent_id,memory_space_id,session_id) DO UPDATE SET archive_status='verified',source_event_high_watermark=excluded.source_event_high_watermark,l2_summary_revision=excluded.l2_summary_revision,verified_at=excluded.verified_at,updated_at=excluded.updated_at,last_error=NULL",
            params![session_id,identity.account_id,identity.agent_id,identity.memory_space_id,last_sequence,revision,now]).map_err(db_error)?;
        let deleted = transaction.execute(
            "DELETE FROM session_events WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND session_id=?4",
            params![identity.account_id,identity.agent_id,identity.memory_space_id,session_id]).map_err(db_error)?;
        transaction.execute(
            "UPDATE session_retention_manifests SET archive_status='deleted',deleted_at=?1,updated_at=?1 WHERE account_id=?2 AND agent_id=?3 AND memory_space_id=?4 AND session_id=?5",
            params![now,identity.account_id,identity.agent_id,identity.memory_space_id,session_id]).map_err(db_error)?;
        transaction.commit().map_err(db_error)?;
        Ok(
            json!({"session_id":session_id,"state":"deleted","deleted_event_count":deleted,"l2_summary_revision":revision,"source_event_high_watermark":last_sequence}),
        )
    }

    pub(crate) fn compact_session(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: CompactSession = parse_params(request)?;
        validate_session_id(&input.session_id)?;
        if !matches!(input.level.as_str(), "rules" | "summary" | "full" | "auto") {
            return Err(ServiceError::invalid("invalid compression level"));
        }
        if !(128..=128_000).contains(&input.target_tokens) {
            return Err(ServiceError::invalid("target_tokens is out of range"));
        }
        self.with_write(identity, request, "sessions.compact", "session", |tx, _, _, _, _| {
            let event_count: i64 = tx.query_row(
                "SELECT COUNT(*) FROM session_events WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND session_id=?4",
                params![identity.account_id,identity.agent_id,identity.memory_space_id,input.session_id],
                |row| row.get(0),
            ).map_err(db_error)?;
            let summary_revision: Option<i64> = tx.query_row(
                "SELECT revision FROM session_summaries
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND session_id=?4 AND is_current=1",
                params![identity.account_id,identity.agent_id,identity.memory_space_id,input.session_id],
                |row| row.get(0),
            ).optional().map_err(db_error)?;
            let completed_level = match input.level.as_str() {
                "rules" => "rules",
                "summary" if summary_revision.is_some() => "summary",
                "summary" => "rules",
                "full" | "auto" if summary_revision.is_some() => "summary",
                _ => "rules",
            };
            let degraded_reason = match input.level.as_str() {
                "full" => Some("model_unavailable"),
                "auto" => Some("model_adapter_not_configured"),
                "summary" if summary_revision.is_none() => Some("summary_unavailable"),
                _ => None,
            };
            Ok((json!({
                "session_id":input.session_id,
                "requested_level":input.level,
                "completed_level":completed_level,
                "target_tokens":input.target_tokens,
                "event_count":event_count,
                "summary_revision":summary_revision,
                "degraded_reason":degraded_reason,
                "source_events_preserved":true
            }), Some(input.session_id.clone())))
        })
    }

    pub(crate) fn summarize_session(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: SummarizeSession = parse_params(request)?;
        validate_session_id(&input.session_id)?;
        if !(1..=50).contains(&input.interval_turns) || !(1..=50).contains(&input.keep_raw_turns) {
            return Err(ServiceError::invalid(
                "summary interval and raw turn window must be between 1 and 50",
            ));
        }
        let product_config = self.product_config()?;
        let summary_retention_days = product_config.summary_retention_days;
        let summary_session_id = input.session_id.clone();
        let result = self.with_write(identity, request, "sessions.summarize", "session_summary", |tx, hlc, now, _, _| {
            let mut statement = tx.prepare(
                "SELECT event_sequence,event_type,payload,payload_encoding FROM session_events
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND session_id=?4
                 ORDER BY event_sequence",
            ).map_err(db_error)?;
            let rows = statement.query_map(
                params![identity.account_id,identity.agent_id,identity.memory_space_id,input.session_id],
                |row| {
                    let blob: Vec<u8> = row.get(2)?;
                    let encoding: String = row.get(3)?;
                    let payload = decode_json_payload(&blob, &encoding).map_err(|error| {
                        rusqlite::Error::ToSqlConversionFailure(Box::new(error))
                    })?;
                    let payload = json_bytes(&payload).map_err(|error| {
                        rusqlite::Error::ToSqlConversionFailure(Box::new(error))
                    })?;
                    Ok((row.get::<_,i64>(0)?,row.get::<_,String>(1)?,payload))
                },
            ).map_err(db_error)?;
            let events = rows.collect::<Result<Vec<_>,_>>().map_err(db_error)?;
            let messages = events.iter()
                .filter(|(_, event_type, _)| matches!(event_type.as_str(), "user_message" | "assistant_message"))
                .cloned()
                .collect::<Vec<_>>();
            let completed = completed_turns(&messages);
            let completed_turn_count = completed.len() as i64;
            let summary_row: Option<(i64,Vec<u8>,i64)> = tx.query_row(
                "SELECT revision,summary_payload,source_event_sequence FROM session_summaries
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND session_id=?4 AND is_current=1",
                params![identity.account_id,identity.agent_id,identity.memory_space_id,input.session_id],
                |row| Ok((row.get(0)?,row.get(1)?,row.get(2)?)),
            ).optional().map_err(db_error)?;
            let previous_revision = summary_row.as_ref().map(|row| row.0).unwrap_or(0);
            let previous_sequence = summary_row.as_ref().map(|row| row.2).unwrap_or(0);
            // MEM-L1-01: tool/task events are summary *content*, never a trigger
            // source and never a gate bypass. Only completed turns count.
            if (completed_turn_count == 0 && previous_revision == 0)
                || (!input.force && completed_turn_count % input.interval_turns != 0)
            {
                return Ok((json!({
                    "session_id":input.session_id,"state":"not_due","completed_turns":completed_turn_count,
                    "interval_turns":input.interval_turns,"revision":previous_revision,
                    "source_events_preserved":true
                }),Some(input.session_id.clone())));
            }
            let mut summarizable = Vec::<(i64, String, Vec<u8>)>::new();
            for (user, assistant) in &completed {
                summarizable.push((user.0, "user_message".to_owned(), user.1.clone()));
                summarizable.push((assistant.0, "assistant_message".to_owned(), assistant.1.clone()));
            }
            for (sequence, event_type, payload) in events {
                if sequence > previous_sequence && matches!(event_type.as_str(), "tool_call" | "tool_result" | "task_started" | "task_completed" | "task_failed") {
                    summarizable.push((sequence, event_type, payload));
                }
            }
            summarizable.sort_by_key(|row| row.0);
            let source_sequence = summarizable.last().map(|row| row.0).unwrap_or(0);
            if source_sequence <= previous_sequence {
                return Ok((json!({
                    "session_id":input.session_id,"state":"up_to_date","completed_turns":completed_turn_count,
                    "revision":previous_revision,"source_event_sequence":previous_sequence,
                    "source_events_preserved":true
                }),Some(input.session_id.clone())));
            }
            let previous_status = summary_row.as_ref()
                .and_then(|row| serde_json::from_slice::<Value>(&row.1).ok())
                .and_then(|value| value.get("current_status").and_then(Value::as_str).map(str::to_owned));
            let previous_summary = summary_row.as_ref()
                .and_then(|row| serde_json::from_slice::<Value>(&row.1).ok())
                .unwrap_or(Value::Null);
            let mut lines = previous_status.into_iter().collect::<Vec<_>>();
            let mut fields = SummaryFields::from_previous(&previous_summary);
            for (sequence,event_type,blob) in summarizable {
                if sequence <= previous_sequence {
                    continue;
                }
                let payload = serde_json::from_slice::<Value>(&blob).map_err(|_| {
                    ServiceError::unavailable("stored session event JSON is invalid")
                })?;
                if !matches!(event_type.as_str(), "user_message" | "assistant_message") {
                    let tool = payload.get("tool").and_then(Value::as_str).unwrap_or("operation");
                    let status = payload.get("status").and_then(Value::as_str).unwrap_or("");
                    let operation = clip_utf8(&format!("{tool} {status}"), SUMMARY_TOOL_ITEM_BYTES);
                    lines.push(format!("operation: {operation}"));
                    if let Some(command) = payload.get("command").and_then(Value::as_str).filter(|v| !v.is_empty()) {
                        fields.commands_executed.push(&clip_utf8(command, SUMMARY_TOOL_ITEM_BYTES), sequence);
                    }
                    if let Some(files) = payload.get("files").and_then(Value::as_array) {
                        for file in files.iter().filter_map(Value::as_str) {
                            fields.files_resources_touched.push(&clip_utf8(file, SUMMARY_TOOL_ITEM_BYTES), sequence);
                        }
                    }
                    if matches!(event_type.as_str(), "tool_result" | "task_completed") && !status.eq_ignore_ascii_case("failed") {
                        fields.operations_completed.push(&operation, sequence);
                    }
                    if event_type == "task_failed" || matches!(status.to_ascii_lowercase().as_str(), "failed" | "error") {
                        fields.errors_encountered.push(&operation, sequence);
                    }
                    continue;
                }
                let text = payload.get("text").and_then(Value::as_str).unwrap_or("").trim();
                if text.is_empty() {
                    continue;
                }
                let clipped = clip_utf8(text, 1200);
                let role = if event_type == "user_message" {"用户"} else {"助手"};
                lines.push(format!("{role}: {clipped}"));
                let user_side = event_type == "user_message";
                for sentence in split_summary_sentences(text) {
                    let sentence = clip_utf8(sentence, SUMMARY_SENTENCE_BYTES);
                    if user_side {
                        classify_user_sentence(&mut fields, &sentence, sequence);
                    } else {
                        classify_assistant_sentence(&mut fields, &sentence, sequence);
                    }
                }
            }
            let status = clip_utf8_tail(&lines.join("\n"), 8000);
            let mut summary = fields.into_payload(status, &input.session_id, source_sequence);
            let summary_trimmed = enforce_summary_budget(&mut summary);
            let summary_blob = json_bytes(&summary)?;
            let revision = previous_revision + 1;
            tx.execute(
                "UPDATE session_summaries SET is_current=0
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND session_id=?4 AND is_current=1",
                params![identity.account_id,identity.agent_id,identity.memory_space_id,input.session_id],
            ).map_err(db_error)?;
            tx.execute(
                "INSERT INTO session_summaries(
                    session_id,account_id,agent_id,memory_space_id,revision,parent_revision,
                    summary_payload,source_event_sequence,sync_policy,producer,model_ref,
                    policy_version,created_at,hlc,is_current,expires_at
                 ) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,'summary_only','memoryd.rules',NULL,1,?9,?10,1,?11)",
                params![input.session_id,identity.account_id,identity.agent_id,identity.memory_space_id,
                        revision,if previous_revision==0 {None}else{Some(previous_revision)},summary_blob,
                        source_sequence,now,hlc,now.saturating_add(summary_retention_days*24*60*60*1000)],
            ).map_err(db_error)?;
            let mut outcome = json!({
                "session_id":input.session_id,"state":"summarized","completed_turns":completed_turn_count,
                "revision":revision,"source_event_sequence":source_sequence,"summary":summary,
                "producer":"memoryd.rules","source_events_preserved":true,
                "raw_turns_retained":input.keep_raw_turns
            });
            if summary_trimmed {
                outcome["degraded_reason"] = json!("summary_trimmed");
            }
            Ok((outcome,Some(input.session_id.clone())))
        })?;
        // The rules summary is always committed first.  An enabled external
        // model may then improve it in a separate transaction; any timeout,
        // transport error or invalid response leaves the rules result intact.
        if product_config.summary_model.enabled
            && result.get("state").and_then(Value::as_str) == Some("summarized")
        {
            if let Ok(model_summary) = request_model_summary(
                &product_config.summary_model,
                result.get("summary").unwrap_or(&Value::Null),
            ) {
                let mut update = request.clone();
                update.method = "sessions.update_summary".to_owned();
                update.request_id = Uuid::new_v4().to_string();
                update.idempotency_key = Some(format!(
                    "memoryd:model-summary:{}:{}",
                    summary_session_id,
                    result
                        .get("revision")
                        .and_then(Value::as_i64)
                        .unwrap_or_default()
                ));
                update.params = json!({
                    "session_id": summary_session_id,
                    "patch": model_summary,
                    "source_event_sequence": result["source_event_sequence"],
                    "expected_revision": result["revision"],
                    "sync_policy": "summary_only",
                    "producer": "memoryd.model",
                    "model_ref": product_config.summary_model.model
                });
                if let Ok(mut modeled) = self.update_summary(identity, &update) {
                    modeled["degraded_reason"] = Value::Null;
                    return Ok(modeled);
                }
            }
            let mut degraded = result;
            degraded["degraded_reason"] = json!("model_unavailable");
            return Ok(degraded);
        }
        Ok(result)
    }

    pub(crate) fn prepare_checkpoint(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: CheckpointRequest = parse_params(request)?;
        validate_session_id(&input.session_id)?;
        self.with_write(identity, request, "runtime.prepare_checkpoint", "checkpoint", |tx, hlc, now, _, _| {
            let sequence: i64 = tx.query_row(
                "SELECT COALESCE(MAX(event_sequence),0)+1 FROM session_events
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND session_id=?4",
                params![identity.account_id,identity.agent_id,identity.memory_space_id,input.session_id],
                |row| row.get(0),
            ).map_err(db_error)?;
            let event_id = Uuid::new_v4().to_string();
            let payload = json!({"agent_instance_id":identity.agent_instance_id});
            let blob = json_bytes(&payload)?;
            let payload_hash = hash_bytes(&blob);
            tx.execute(
                "INSERT INTO session_events(
                    event_id,account_id,agent_id,memory_space_id,session_id,event_sequence,event_type,
                    payload,payload_hash,sync_policy,created_at,hlc
                 ) VALUES(?1,?2,?3,?4,?5,?6,'checkpoint_marker',?7,?8,'local_only',?9,?10)",
                params![event_id,identity.account_id,identity.agent_id,identity.memory_space_id,input.session_id,sequence,blob,payload_hash,now,hlc],
            ).map_err(db_error)?;
            let summary_revision: i64 = tx.query_row(
                "SELECT COALESCE(MAX(revision),0) FROM session_summaries
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND session_id=?4 AND is_current=1",
                params![identity.account_id,identity.agent_id,identity.memory_space_id,input.session_id],
                |row| row.get(0),
            ).map_err(db_error)?;
            let outbox_high_watermark: i64 = tx.query_row(
                "SELECT COALESCE(MAX(rowid),0) FROM outbox WHERE account_id=?1 AND agent_id=?2",
                params![identity.account_id,identity.agent_id],
                |row| row.get(0),
            ).map_err(db_error)?;
            let pending_jobs: i64 = tx.query_row(
                "SELECT COUNT(*) FROM sync_jobs WHERE account_id=?1 AND agent_id=?2 AND state!='completed'",
                params![identity.account_id,identity.agent_id],
                |row| row.get(0),
            ).map_err(db_error)?;
            let checkpoint_ref = Uuid::new_v4().to_string();
            tx.execute(
                "INSERT INTO checkpoint_refs(
                    checkpoint_ref,account_id,agent_id,agent_instance_id,session_id,last_event_sequence,
                    summary_revision,outbox_high_watermark,created_at
                 ) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9)",
                params![checkpoint_ref,identity.account_id,identity.agent_id,identity.agent_instance_id,input.session_id,sequence,summary_revision,outbox_high_watermark,now],
            ).map_err(db_error)?;
            tx.execute(
                "DELETE FROM checkpoint_refs WHERE checkpoint_ref IN (
                    SELECT checkpoint_ref FROM checkpoint_refs
                    WHERE account_id=?1 AND agent_id=?2 AND agent_instance_id=?3
                    ORDER BY created_at DESC,checkpoint_ref DESC LIMIT -1 OFFSET 10
                 )",
                params![identity.account_id,identity.agent_id,identity.agent_instance_id],
            ).map_err(db_error)?;
            Ok((json!({
                "checkpoint_ref":checkpoint_ref,
                "last_event_sequence":sequence,
                "summary_revision":summary_revision,
                "pending_memory_jobs":pending_jobs,
                "outbox_high_watermark":outbox_high_watermark
            }), Some(checkpoint_ref)))
        })
    }

    pub(crate) fn restore_checkpoint(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: RestoreCheckpointRequest = parse_params(request)?;
        let connection = self.connection()?;
        let checkpoint: Option<(String, i64, i64, i64)> = connection
            .query_row(
                "SELECT session_id,last_event_sequence,summary_revision,outbox_high_watermark
             FROM checkpoint_refs
             WHERE checkpoint_ref=?1 AND account_id=?2 AND agent_id=?3 AND agent_instance_id=?4",
                params![
                    input.checkpoint_ref,
                    identity.account_id,
                    identity.agent_id,
                    identity.agent_instance_id
                ],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .optional()
            .map_err(db_error)?;
        let (session_id, last_event_sequence, summary_revision, outbox_high_watermark) =
            checkpoint.ok_or_else(ServiceError::not_found)?;
        let revisions_after: i64 = connection
            .query_row(
                "SELECT COUNT(*) FROM session_events
             WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND session_id=?4 AND event_sequence>?5",
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id,
                    session_id,
                    last_event_sequence
                ],
                |row| row.get(0),
            )
            .map_err(db_error)?;
        let outbox_after: i64 = connection
            .query_row(
                "SELECT COUNT(*) FROM outbox WHERE account_id=?1 AND agent_id=?2 AND rowid>?3",
                params![
                    identity.account_id,
                    identity.agent_id,
                    outbox_high_watermark
                ],
                |row| row.get(0),
            )
            .map_err(db_error)?;
        Ok(json!({
            "checkpoint_ref":input.checkpoint_ref,
            "session_id":session_id,
            "last_event_sequence":last_event_sequence,
            "summary_revision":summary_revision,
            "events_after_checkpoint":revisions_after,
            "outbox_after_checkpoint":outbox_after
        }))
    }

    pub(crate) fn list_checkpoints(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        #[derive(Deserialize)]
        struct ListCheckpoints {
            #[serde(default = "default_checkpoint_limit")]
            limit: i64,
        }
        fn default_checkpoint_limit() -> i64 {
            10
        }
        let input: ListCheckpoints = parse_params(request)?;
        if !(1..=10).contains(&input.limit) {
            return Err(ServiceError::invalid(
                "checkpoint limit must be between 1 and 10",
            ));
        }
        let connection = self.connection()?;
        let mut statement = connection
            .prepare(
                "SELECT checkpoint_ref,session_id,last_event_sequence,summary_revision,
                    outbox_high_watermark,created_at
             FROM checkpoint_refs WHERE account_id=?1 AND agent_id=?2 AND agent_instance_id=?3
             ORDER BY created_at DESC,checkpoint_ref DESC LIMIT ?4",
            )
            .map_err(db_error)?;
        let rows = statement.query_map(
            params![identity.account_id,identity.agent_id,identity.agent_instance_id,input.limit],
            |row| Ok(json!({
                "checkpoint_ref":row.get::<_,String>(0)?,"session_id":row.get::<_,String>(1)?,
                "last_event_sequence":row.get::<_,i64>(2)?,"summary_revision":row.get::<_,i64>(3)?,
                "outbox_high_watermark":row.get::<_,i64>(4)?,"created_at":row.get::<_,i64>(5)?
            })),
        ).map_err(db_error)?;
        Ok(Value::Array(
            rows.collect::<Result<Vec<_>, _>>().map_err(db_error)?,
        ))
    }
}

fn clip_utf8(value: &str, limit: usize) -> String {
    if value.len() <= limit {
        return value.to_owned();
    }
    let mut end = limit;
    while !value.is_char_boundary(end) {
        end -= 1;
    }
    value[..end].to_owned()
}

fn clip_utf8_tail(value: &str, limit: usize) -> String {
    if value.len() <= limit {
        return value.to_owned();
    }
    let mut start = value.len() - limit;
    while !value.is_char_boundary(start) {
        start += 1;
    }
    value[start..].to_owned()
}

fn summary_strings(summary: &Value, key: &str) -> Vec<String> {
    summary
        .get(key)
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
        .collect()
}

/// Text classified out of a user or assistant message is clipped per sentence:
/// one long paragraph must not crowd out every other fact.
const SUMMARY_SENTENCE_BYTES: usize = 300;
/// Tool and task events keep the historical 500-byte clip.
const SUMMARY_TOOL_ITEM_BYTES: usize = 500;
/// A concrete resource (path or command) lifted out of assistant prose.
const SUMMARY_RESOURCE_BYTES: usize = 300;
/// Historical per-field ceilings: 50 items for prose, 100 for tool events.
const SUMMARY_TEXT_ITEM_CAP: usize = 50;
const SUMMARY_TOOL_ITEM_CAP: usize = 100;
/// Keep the assembled payload strictly below the protocol text ceiling that
/// `sessions.update_summary` enforces (`memory_core::MAX_TEXT_BYTES`).
const SUMMARY_BUDGET_MARGIN_BYTES: usize = 4 * 1024;
/// Two items describe the same fact once their token sets overlap this much.
/// The threshold is deliberately conservative so distinct facts stay apart.
const SUMMARY_DEDUP_JACCARD: f64 = 0.8;
/// Token overlap is only meaningful beyond a handful of tokens.
const SUMMARY_DEDUP_MIN_TOKENS: usize = 4;
/// Fields whose items are index-aligned with `fact_source_sequences`.
const SUMMARY_SEQUENCED_KEYS: &[&str] = &[
    "decisions",
    "unresolved_tasks",
    "salient_facts",
    "files_resources_touched",
    "commands_executed",
    "operations_completed",
    "errors_encountered",
];

/// User-side keyword tables. They are explicit constants so the extraction
/// contract can be reviewed and pinned by unit tests.
const SUMMARY_DECISION_MARKERS: &[&str] = &[
    "决定",
    "采用",
    "选择",
    "默认使用",
    "默认用",
    "以后使用",
    "以后都",
    "不再使用",
    "decide",
    "choose",
    "chose",
    "use ",
];
const SUMMARY_TASK_MARKERS: &[&str] = &[
    "待办",
    "还要",
    "需要完成",
    "稍后",
    "提醒我",
    "todo",
    "to do",
    "need to",
    "follow up",
];
const SUMMARY_FACT_MARKERS: &[&str] = &[
    "请记住",
    "记住",
    "记得",
    "我叫",
    "我喜欢",
    "我不喜欢",
    "我讨厌",
    "我偏好",
    "我习惯",
    "我常用",
    "别用",
    "remember",
    "i prefer",
    "i usually",
    "i like",
    "i dislike",
];
/// Assistant prose is only mined for these fixed patterns. The assistant side
/// never feeds `decisions` or `salient_facts`, which stay user-sourced.
const SUMMARY_COMPLETION_MARKERS: &[&str] =
    &["已完成", "已修改", "已创建", "已删除", "已修复", "已提交"];
const SUMMARY_ERROR_MARKERS: &[&str] = &["失败", "报错", "异常", "error", "failed"];
const SUMMARY_PATH_EXTENSIONS: &[&str] = &[
    ".rs", ".py", ".md", ".json", ".toml", ".yaml", ".yml", ".sh", ".js", ".ts", ".tsx",
    ".go", ".c", ".h", ".cpp", ".txt", ".log", ".csv", ".sql", ".conf", ".service", ".deb",
    ".diff", ".patch", ".ini", ".cfg",
];
const SUMMARY_COMMAND_NAMES: &[&str] = &[
    "cargo", "git", "dpkg", "apt", "apt-get", "systemctl", "python", "python3", "pip", "bash",
    "sh", "ssh", "scp", "rsync", "docker", "make", "cmake", "npm", "pnpm", "yarn", "curl",
    "wget", "journalctl", "mount", "umount", "chmod", "chown", "tar", "grep", "rg", "sed",
    "awk", "sqlite3", "pytest",
];
const SUMMARY_TRAILING_PUNCTUATION: &[char] = &[
    ',', '.', ';', ':', '!', '?', ')', ']', '}', '>', '\'', '"', '`', '，', '。', '；', '：',
    '！', '？', '）', '】', '、', '“', '”',
];

/// One extracted summary field together with the session event each item came
/// from. `values` and `sequences` stay index-aligned; an item carried over from
/// an earlier revision keeps `None` because its origin was never recorded.
struct SummaryField {
    values: Vec<String>,
    sequences: Vec<Option<i64>>,
    cap: usize,
}

impl SummaryField {
    fn carried_over(summary: &Value, key: &str, cap: usize) -> Self {
        let values = summary_strings(summary, key);
        let recorded = summary
            .get("fact_source_sequences")
            .and_then(Value::as_object)
            .and_then(|sources| sources.get(key))
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let sequences = (0..values.len())
            .map(|index| recorded.get(index).and_then(Value::as_i64))
            .collect();
        Self {
            values,
            sequences,
            cap,
        }
    }

    fn push(&mut self, value: &str, sequence: i64) {
        let value = normalize_summary_text(value);
        if value.is_empty()
            || self
                .values
                .iter()
                .any(|existing| similar_summary_text(existing, &value))
        {
            return;
        }
        self.values.push(value);
        self.sequences.push(Some(sequence));
        if self.cap > 0 && self.values.len() > self.cap {
            self.values.remove(0);
            self.sequences.remove(0);
        }
    }
}

/// The rules summary being assembled for one session.
struct SummaryFields {
    decisions: SummaryField,
    unresolved_tasks: SummaryField,
    salient_facts: SummaryField,
    files_resources_touched: SummaryField,
    commands_executed: SummaryField,
    operations_completed: SummaryField,
    errors_encountered: SummaryField,
}

impl SummaryFields {
    fn from_previous(summary: &Value) -> Self {
        Self {
            decisions: SummaryField::carried_over(summary, "decisions", SUMMARY_TEXT_ITEM_CAP),
            unresolved_tasks: SummaryField::carried_over(
                summary,
                "unresolved_tasks",
                SUMMARY_TEXT_ITEM_CAP,
            ),
            salient_facts: SummaryField::carried_over(
                summary,
                "salient_facts",
                SUMMARY_TEXT_ITEM_CAP,
            ),
            files_resources_touched: SummaryField::carried_over(
                summary,
                "files_resources_touched",
                SUMMARY_TOOL_ITEM_CAP,
            ),
            commands_executed: SummaryField::carried_over(
                summary,
                "commands_executed",
                SUMMARY_TOOL_ITEM_CAP,
            ),
            operations_completed: SummaryField::carried_over(
                summary,
                "operations_completed",
                SUMMARY_TOOL_ITEM_CAP,
            ),
            errors_encountered: SummaryField::carried_over(
                summary,
                "errors_encountered",
                SUMMARY_TOOL_ITEM_CAP,
            ),
        }
    }

    /// Build the summary payload. `fact_source_sequences` mirrors the item
    /// arrays by index so that archiving a summary into long-term memory can
    /// point every fact back at the session events it came from.
    fn into_payload(self, status: String, session_id: &str, source_sequence: i64) -> Value {
        let mut payload = serde_json::Map::new();
        payload.insert("current_status".to_owned(), json!(status));
        payload.insert(
            "context_pointers".to_owned(),
            json!([format!("session:{session_id}:events:1-{source_sequence}")]),
        );
        let mut sources = serde_json::Map::new();
        for (key, field) in self.entries() {
            sources.insert(key.to_owned(), json!(field.sequences));
            payload.insert(key.to_owned(), json!(field.values));
        }
        payload.insert("fact_source_sequences".to_owned(), Value::Object(sources));
        Value::Object(payload)
    }

    fn entries(self) -> [(&'static str, SummaryField); 7] {
        [
            ("decisions", self.decisions),
            ("unresolved_tasks", self.unresolved_tasks),
            ("salient_facts", self.salient_facts),
            ("files_resources_touched", self.files_resources_touched),
            ("commands_executed", self.commands_executed),
            ("operations_completed", self.operations_completed),
            ("errors_encountered", self.errors_encountered),
        ]
    }
}

fn classify_user_sentence(fields: &mut SummaryFields, sentence: &str, sequence: i64) {
    let lowered = sentence.to_lowercase();
    if SUMMARY_DECISION_MARKERS
        .iter()
        .any(|marker| lowered.contains(marker))
    {
        fields.decisions.push(sentence, sequence);
    } else if SUMMARY_TASK_MARKERS
        .iter()
        .any(|marker| lowered.contains(marker))
    {
        fields.unresolved_tasks.push(sentence, sequence);
    } else if SUMMARY_FACT_MARKERS
        .iter()
        .any(|marker| lowered.contains(marker))
    {
        fields.salient_facts.push(sentence, sequence);
    }
}

fn classify_assistant_sentence(fields: &mut SummaryFields, sentence: &str, sequence: i64) {
    if SUMMARY_COMPLETION_MARKERS
        .iter()
        .any(|marker| sentence.contains(marker))
    {
        let (paths, commands) = extract_summary_resources(sentence);
        for path in paths {
            fields.files_resources_touched.push(&path, sequence);
        }
        for command in commands {
            fields.commands_executed.push(&command, sequence);
        }
        fields.operations_completed.push(sentence, sequence);
        return;
    }
    let lowered = sentence.to_lowercase();
    if SUMMARY_ERROR_MARKERS
        .iter()
        .any(|marker| lowered.contains(marker))
    {
        fields.errors_encountered.push(sentence, sequence);
    }
}

/// Split prose into sentences. CJK punctuation always ends a sentence; ASCII
/// `.?!;` only ends one when whitespace or the end of the text follows, so
/// paths such as `embedding.rs` and versions such as `v1.2` stay in one piece.
fn split_summary_sentences(text: &str) -> Vec<&str> {
    let mut sentences = Vec::new();
    let mut start = 0usize;
    for (index, character) in text.char_indices() {
        let end = index + character.len_utf8();
        let boundary = match character {
            '。' | '！' | '？' | '；' | '\n' | '\r' => true,
            '.' | '!' | '?' | ';' => text[end..]
                .chars()
                .next()
                .map_or(true, char::is_whitespace),
            _ => false,
        };
        if boundary {
            let sentence = text[start..index].trim();
            if !sentence.is_empty() {
                sentences.push(sentence);
            }
            start = end;
        }
    }
    let tail = text[start..].trim();
    if !tail.is_empty() {
        sentences.push(tail);
    }
    sentences
}

/// Normalize separators and full-width alphanumerics so the same fact typed
/// twice is recognized once. Case is preserved for readability; comparisons
/// lowercase on their own.
fn normalize_summary_text(value: &str) -> String {
    let mut normalized = String::with_capacity(value.len());
    let mut separator = false;
    for character in value.chars() {
        let character = match character {
            '\u{3000}' => ' ',
            '\u{ff10}'..='\u{ff19}' | '\u{ff21}'..='\u{ff3a}' | '\u{ff41}'..='\u{ff5a}' => {
                char::from_u32(character as u32 - 0xfee0).unwrap_or(character)
            }
            _ => character,
        };
        if character.is_whitespace() {
            separator = !normalized.is_empty();
            continue;
        }
        if separator {
            normalized.push(' ');
            separator = false;
        }
        normalized.push(character);
    }
    normalized
}

fn is_cjk(character: char) -> bool {
    matches!(
        character as u32,
        0x3040..=0x30ff | 0x3400..=0x4dbf | 0x4e00..=0x9fff | 0xf900..=0xfaff
    )
}

/// Tokenize for duplicate detection: ASCII words stay whole, CJK runs become
/// single characters plus adjacent pairs (a cheap bigram index).
fn summary_tokens(value: &str) -> BTreeSet<String> {
    let mut tokens = BTreeSet::new();
    let mut word = String::new();
    let mut run: Vec<char> = Vec::new();
    for character in value.chars().chain(std::iter::once(' ')) {
        if character.is_ascii_alphanumeric() {
            flush_cjk_tokens(&mut tokens, &mut run);
            word.push(character.to_ascii_lowercase());
            continue;
        }
        if !word.is_empty() {
            tokens.insert(std::mem::take(&mut word));
        }
        if is_cjk(character) {
            run.push(character);
        } else {
            flush_cjk_tokens(&mut tokens, &mut run);
        }
    }
    tokens
}

fn flush_cjk_tokens(tokens: &mut BTreeSet<String>, run: &mut Vec<char>) {
    match run.len() {
        0 => return,
        1 => {
            tokens.insert(run[0].to_string());
        }
        _ => {
            for (index, character) in run.iter().enumerate() {
                tokens.insert((*character).to_string());
                if let Some(next) = run.get(index + 1) {
                    tokens.insert(format!("{character}{next}"));
                }
            }
        }
    }
    run.clear();
}

fn similar_summary_text(left: &str, right: &str) -> bool {
    let left = normalize_summary_text(left);
    let right = normalize_summary_text(right);
    if left == right {
        return true;
    }
    let left = summary_tokens(&left);
    let right = summary_tokens(&right);
    if left.len() < SUMMARY_DEDUP_MIN_TOKENS || right.len() < SUMMARY_DEDUP_MIN_TOKENS {
        return false;
    }
    if has_distinct_summary_identifier(&left, &right) {
        return false;
    }
    let shared = left.intersection(&right).count() as f64;
    let total = left.union(&right).count() as f64;
    total > 0.0 && shared / total >= SUMMARY_DEDUP_JACCARD
}

/// ASCII words are concrete identifiers: `用 vim` and `用 emacs` are different
/// facts even though every surrounding character matches.
fn has_distinct_summary_identifier(left: &BTreeSet<String>, right: &BTreeSet<String>) -> bool {
    left.symmetric_difference(right)
        .any(|token| token.chars().all(|character| character.is_ascii_alphanumeric()))
}

/// Pull concrete resources (paths, commands) out of an assistant sentence.
fn extract_summary_resources(sentence: &str) -> (Vec<String>, Vec<String>) {
    let tokens: Vec<String> = sentence
        .split_whitespace()
        .map(|token| {
            token.trim_matches(|character: char| SUMMARY_TRAILING_PUNCTUATION.contains(&character))
        })
        .filter(|token| !token.is_empty())
        .map(str::to_owned)
        .collect();
    let mut paths = Vec::new();
    let mut commands = Vec::new();
    for (index, token) in tokens.iter().enumerate() {
        if looks_like_url(token) {
            continue;
        }
        if looks_like_path(token) {
            if !paths.contains(token) {
                paths.push(clip_utf8(token, SUMMARY_RESOURCE_BYTES));
            }
            continue;
        }
        if commands.is_empty()
            && SUMMARY_COMMAND_NAMES.contains(&token.to_ascii_lowercase().as_str())
        {
            let mut command = token.clone();
            let mut parts = 1;
            for argument in tokens.iter().skip(index + 1) {
                if parts >= 3
                    || looks_like_path(argument)
                    || !argument.chars().all(|character| character.is_ascii())
                {
                    break;
                }
                command.push(' ');
                command.push_str(argument);
                parts += 1;
            }
            commands.push(clip_utf8(&command, SUMMARY_RESOURCE_BYTES));
        }
    }
    (paths, commands)
}

fn looks_like_path(token: &str) -> bool {
    if token.contains('/') || token.contains('\\') {
        return true;
    }
    let lowered = token.to_ascii_lowercase();
    SUMMARY_PATH_EXTENSIONS
        .iter()
        .any(|extension| lowered.ends_with(extension))
}

fn looks_like_url(token: &str) -> bool {
    token.contains("://") || token.to_ascii_lowercase().starts_with("http")
}

/// Trim the payload to the protocol ceiling. Items are dropped oldest-first
/// from the largest field; the tail transcript is only shortened once every
/// extracted item is gone. Returns whether anything was dropped.
fn enforce_summary_budget(summary: &mut Value) -> bool {
    let budget = MAX_TEXT_BYTES.saturating_sub(SUMMARY_BUDGET_MARGIN_BYTES);
    if summary_bytes(summary) <= budget {
        return false;
    }
    let mut trimmed = false;
    while summary_bytes(summary) > budget {
        if !drop_oldest_summary_item(summary) {
            break;
        }
        trimmed = true;
    }
    while summary_bytes(summary) > budget {
        let remaining = summary
            .get("current_status")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        if remaining.is_empty() {
            break;
        }
        let shortened = clip_utf8_tail(&remaining, remaining.len() / 2);
        if let Some(object) = summary.as_object_mut() {
            object.insert("current_status".to_owned(), json!(shortened));
        }
        trimmed = true;
    }
    trimmed
}

fn summary_bytes(summary: &Value) -> usize {
    serde_json::to_vec(summary).map_or(usize::MAX, |blob| blob.len())
}

fn drop_oldest_summary_item(summary: &mut Value) -> bool {
    let Some(object) = summary.as_object_mut() else {
        return false;
    };
    let mut target: Option<&'static str> = None;
    let mut longest = 0usize;
    for key in SUMMARY_SEQUENCED_KEYS {
        let length = object
            .get(*key)
            .and_then(Value::as_array)
            .map_or(0, Vec::len);
        if length > longest {
            longest = length;
            target = Some(key);
        }
    }
    let Some(key) = target else {
        return false;
    };
    if let Some(items) = object.get_mut(key).and_then(Value::as_array_mut) {
        items.remove(0);
    }
    if let Some(recorded) = object
        .get_mut("fact_source_sequences")
        .and_then(Value::as_object_mut)
        .and_then(|sources| sources.get_mut(key))
        .and_then(Value::as_array_mut)
    {
        if !recorded.is_empty() {
            recorded.remove(0);
        }
    }
    true
}

/// Call a small OpenAI-compatible chat endpoint without coupling the daemon
/// to a heavyweight HTTP runtime. The call is deliberately outside SQLite
/// transactions and returns only a validated summary patch.
fn request_model_summary(
    config: &crate::product::SummaryModelConfig,
    rules: &Value,
) -> Result<Value, String> {
    let base_url = config
        .base_url
        .as_deref()
        .ok_or("summary model endpoint missing")?;
    if !base_url.starts_with("http://") {
        return Err("summary endpoints must use local/trusted http://".to_owned());
    }
    let url = format!("{}/chat/completions", base_url.trim_end_matches('/'));
    let rest = url
        .strip_prefix("http://")
        .ok_or("invalid summary endpoint")?;
    let (authority, path) = rest.split_once('/').unwrap_or((rest, "chat/completions"));
    let (host, port) = authority
        .rsplit_once(':')
        .map(|(h, p)| (h, p.parse::<u16>().unwrap_or(80)))
        .unwrap_or((authority, 80));
    let model_source = summary_model_source(rules);
    let prompt = format!(
        "Return ONLY one valid JSON object (no prose and no markdown fences). The object MUST use only these fields: current_status (string), key_decisions, open_questions, next_steps, files_resources_touched, commands_executed, operations_completed, errors_encountered, user_preferences, context_pointers (each non-current_status field is a string array). Preserve every verifiable fact from the source, especially operation/file/command facts; never invent facts or include secrets. Source rules summary: {}",
        serde_json::to_string(&model_source).map_err(|e| e.to_string())?
    );
    let body = json!({
        "model": config.model.as_deref().unwrap_or("summary"),
        "temperature": 0,
        "max_tokens": config.max_output_tokens,
        "response_format": {"type":"json_object"},
        "messages": [{"role":"system","content":"You are a factual memory summarizer. Output valid JSON only. Do not explain your answer."},{"role":"user","content":prompt}]
    });
    let body = serde_json::to_vec(&body).map_err(|e| e.to_string())?;
    let endpoint = format!("{host}:{port}")
        .to_socket_addrs()
        .map_err(|e| e.to_string())?
        .next()
        .ok_or("endpoint unavailable")?;
    let mut stream = TcpStream::connect_timeout(
        &endpoint,
        Duration::from_millis(config.max_latency_ms as u64),
    )
    .map_err(|e| e.to_string())?;
    let timeout = Duration::from_millis(config.max_latency_ms as u64);
    stream
        .set_read_timeout(Some(timeout))
        .map_err(|e| e.to_string())?;
    stream
        .set_write_timeout(Some(timeout))
        .map_err(|e| e.to_string())?;
    let mut headers = format!("POST /{path} HTTP/1.1\r\nHost: {authority}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n", body.len());
    if config.auth != "none" {
        let env = config
            .credential_env
            .as_deref()
            .ok_or("summary credential missing")?;
        let token = std::env::var(env).map_err(|_| "summary credential unavailable")?;
        if config.auth == "api_key_env" {
            headers.push_str(&format!("X-API-Key: {token}\r\n"));
        } else {
            headers.push_str(&format!("Authorization: Bearer {token}\r\n"));
        }
    }
    headers.push_str("\r\n");
    stream
        .write_all(headers.as_bytes())
        .and_then(|_| stream.write_all(&body))
        .map_err(|e| e.to_string())?;
    let mut response = Vec::new();
    stream
        .take((MAX_MODEL_RESPONSE_BYTES + 1) as u64)
        .read_to_end(&mut response)
        .map_err(|e| e.to_string())?;
    if response.len() > MAX_MODEL_RESPONSE_BYTES {
        return Err("summary model response is too large".to_owned());
    }
    let response = String::from_utf8_lossy(&response);
    let (status_line, payload) = response
        .split_once("\r\n\r\n")
        .ok_or("invalid model response")?;
    if !status_line.starts_with("HTTP/1.1 200") && !status_line.starts_with("HTTP/1.0 200") {
        return Err("summary model returned non-200".to_owned());
    }
    let envelope: Value = serde_json::from_str(payload).map_err(|e| e.to_string())?;
    let content = envelope
        .get("choices")
        .and_then(Value::as_array)
        .and_then(|v| v.first())
        .and_then(|v| v.get("message"))
        .and_then(|v| v.get("content"))
        .ok_or("summary model content missing")?;
    let mut patch = if let Some(object) = content.as_object() {
        Value::Object(object.clone())
    } else {
        let text = content
            .as_str()
            .ok_or("summary model content is not text")?
            .trim();
        parse_model_json(text)?
    };
    normalize_model_patch(&mut patch);
    // `fact_source_sequences` is derived on the server from the rules pass, so a
    // model echo is dropped here rather than failing the whole patch as an
    // unknown field. `decisions`/`unresolved_tasks`/`salient_facts` keep their
    // existing behaviour of being rejected outright.
    if let Some(object) = patch.as_object_mut() {
        object.remove("fact_source_sequences");
    }
    validate_summary_patch(&patch).map_err(|e| format!("invalid model summary: {e}"))?;
    if let Some(object) = patch.as_object_mut() {
        object.retain(|key, _| {
            !matches!(
                key.as_str(),
                "decisions" | "unresolved_tasks" | "salient_facts" | "fact_source_sequences"
            )
        });
    }
    Ok(patch)
}

/// Present the rules result using the model patch schema. Small instruction
/// models otherwise tend to copy the internal `decisions`,
/// `unresolved_tasks`, and `salient_facts` keys even though those keys are not
/// accepted from model output. Salient facts remain present in current_status.
fn summary_model_source(rules: &Value) -> Value {
    redact_summary_credentials(&json!({
        "current_status": rules.get("current_status").cloned().unwrap_or_else(|| json!("")),
        "key_decisions": rules.get("key_decisions").or_else(|| rules.get("decisions")).cloned().unwrap_or_else(|| json!([])),
        "open_questions": rules.get("open_questions").cloned().unwrap_or_else(|| json!([])),
        "next_steps": rules.get("next_steps").or_else(|| rules.get("unresolved_tasks")).cloned().unwrap_or_else(|| json!([])),
        "files_resources_touched": rules.get("files_resources_touched").cloned().unwrap_or_else(|| json!([])),
        "commands_executed": rules.get("commands_executed").cloned().unwrap_or_else(|| json!([])),
        "operations_completed": rules.get("operations_completed").cloned().unwrap_or_else(|| json!([])),
        "errors_encountered": rules.get("errors_encountered").cloned().unwrap_or_else(|| json!([])),
        "user_preferences": rules.get("user_preferences").cloned().unwrap_or_else(|| json!([])),
        "context_pointers": rules.get("context_pointers").cloned().unwrap_or_else(|| json!([]))
    }))
}

/// Remove credential assignments before summary content is sent to a model.
/// Output validation remains a second line of defense in case a model invents
/// or restores credential-like text.
fn redact_summary_credentials(value: &Value) -> Value {
    match value {
        Value::String(text) => Value::String(redact_summary_text(text)),
        Value::Array(items) => {
            Value::Array(items.iter().map(redact_summary_credentials).collect())
        }
        Value::Object(object) => Value::Object(
            object
                .iter()
                .map(|(key, item)| (key.clone(), redact_summary_credentials(item)))
                .collect(),
        ),
        _ => value.clone(),
    }
}

fn redact_summary_text(text: &str) -> String {
    const MARKERS: [&str; 13] = [
        "password=",
        "password:",
        "passwd=",
        "passwd:",
        "api_key=",
        "api-key=",
        "api_key:",
        "api-key:",
        "secret=",
        "secret:",
        "bearer ",
        "token=",
        "token:",
    ];

    let lowered = text.to_ascii_lowercase();
    let mut output = String::with_capacity(text.len());
    let mut cursor = 0;
    while cursor < text.len() {
        let next = MARKERS
            .iter()
            .filter_map(|marker| lowered[cursor..].find(marker).map(|offset| (offset, marker.len())))
            .min_by_key(|(offset, _)| *offset);
        let Some((offset, marker_len)) = next else {
            output.push_str(&text[cursor..]);
            break;
        };
        let start = cursor + offset;
        output.push_str(&text[cursor..start]);
        output.push_str("[REDACTED CREDENTIAL]");

        let mut end = start + marker_len;
        while end < text.len() {
            let character = text[end..].chars().next().unwrap_or_default();
            if character.is_whitespace() {
                end += character.len_utf8();
            } else {
                break;
            }
        }
        let quote = text[end..].chars().next().filter(|character| matches!(character, '\'' | '"'));
        if let Some(character) = quote {
            end += character.len_utf8();
        }
        while end < text.len() {
            let character = text[end..].chars().next().unwrap_or_default();
            if quote == Some(character)
                || character.is_whitespace()
                || matches!(character, ',' | ';' | '，' | '；' | '。' | '}' | ']')
            {
                if quote == Some(character) {
                    end += character.len_utf8();
                }
                break;
            }
            end += character.len_utf8();
        }
        cursor = end;
    }
    output
}

/// A few OpenAI-compatible instruction models emit a JSON map for list-like
/// fields (for example `{\"commands_executed\": {\"cmd\": \"done\"}}`).
/// Convert that unambiguous representation to the Memory contract's string
/// arrays before validation.  Unknown fields and scalar values are still
/// rejected by `validate_summary_patch`.
fn normalize_model_patch(value: &mut Value) {
    let Some(object) = value.as_object_mut() else {
        return;
    };
    for key in [
        "key_decisions",
        "open_questions",
        "next_steps",
        "files_resources_touched",
        "commands_executed",
        "operations_completed",
        "errors_encountered",
        "user_preferences",
        "context_pointers",
    ] {
        let Some(field) = object.get_mut(key) else {
            continue;
        };
        let Some(map) = field.as_object() else {
            continue;
        };
        let values = map
            .iter()
            .map(|(name, item)| {
                if item.is_string() {
                    format!("{name}: {}", item.as_str().unwrap_or_default())
                } else {
                    format!("{name}: {item}")
                }
            })
            .collect::<Vec<_>>();
        *field = Value::Array(values.into_iter().map(Value::String).collect());
    }
}

/// Parse JSON returned by small instruction models. The preferred response is
/// a bare object, but compatible servers may still wrap it in a markdown fence
/// or a short explanation. Only the first balanced JSON object is accepted;
/// `validate_summary_patch` applies the contract before persistence.
fn parse_model_json(text: &str) -> Result<Value, String> {
    if let Ok(value) = serde_json::from_str::<Value>(text) {
        return Ok(value);
    }
    let stripped = text
        .strip_prefix("```json")
        .or_else(|| text.strip_prefix("```JSON"))
        .unwrap_or(text)
        .trim()
        .trim_end_matches("```")
        .trim();
    if let Ok(value) = serde_json::from_str::<Value>(stripped) {
        return Ok(value);
    }
    let start = stripped
        .find('{')
        .ok_or("summary model JSON object is missing")?;
    let mut depth = 0usize;
    let mut quoted = false;
    let mut escaped = false;
    for (offset, byte) in stripped.as_bytes()[start..].iter().enumerate() {
        if quoted {
            if escaped {
                escaped = false;
            } else if *byte == b'\\' {
                escaped = true;
            } else if *byte == b'"' {
                quoted = false;
            }
            continue;
        }
        match *byte {
            b'"' => quoted = true,
            b'{' => depth = depth.saturating_add(1),
            b'}' => {
                depth = depth.saturating_sub(1);
                if depth == 0 {
                    let end = start + offset + 1;
                    return serde_json::from_str(&stripped[start..end]).map_err(|e| e.to_string());
                }
            }
            _ => {}
        }
    }
    Err("summary model JSON object is incomplete".to_owned())
}

fn validate_session_id(value: &str) -> ServiceResult<()> {
    if memory_core::valid_identifier(value) {
        Ok(())
    } else {
        Err(ServiceError::invalid("invalid session_id"))
    }
}

fn empty_summary() -> Value {
    json!({
        "current_status":"",
        "key_decisions":[],
        "open_questions":[],
        "next_steps":[],
        "files_resources_touched":[],
        "commands_executed":[],
        "operations_completed":[],
        "errors_encountered":[],
        "user_preferences":[],
        "context_pointers":[]
    })
}

fn validate_summary_patch(value: &Value) -> ServiceResult<()> {
    let object = value
        .as_object()
        .ok_or_else(|| ServiceError::invalid("summary patch must be an object"))?;
    let allowed = [
        "current_status",
        "key_decisions",
        "open_questions",
        "next_steps",
        "files_resources_touched",
        "commands_executed",
        "operations_completed",
        "errors_encountered",
        "user_preferences",
        "context_pointers",
    ];
    if object.is_empty() {
        return Err(ServiceError::invalid("summary patch must not be empty"));
    }
    for (key, value) in object {
        if !allowed.contains(&key.as_str()) {
            return Err(ServiceError::invalid(format!(
                "unknown summary field: {key}"
            )));
        }
        if key == "current_status" {
            if !value.as_str().is_some_and(|text| text.len() <= 8_192) {
                return Err(ServiceError::invalid("current_status must be a string"));
            }
        } else {
            let Some(array) = value.as_array() else {
                return Err(ServiceError::invalid(format!("{key} must be a string array")));
            };
            if array.len() > 100
                || array
                    .iter()
                    .any(|item| item.as_str().map_or(true, |text| text.len() > 8_192))
            {
                return Err(ServiceError::invalid(format!(
                    "{key} must contain at most 100 strings of 8192 bytes"
                )));
            }
        }
        if contains_credential_assignment(value) {
            return Err(ServiceError::invalid(
                "summary patch contains a credential-like assignment",
            ));
        }
    }
    Ok(())
}

fn contains_credential_assignment(value: &Value) -> bool {
    let Some(text) = value.as_str() else {
        return value
            .as_array()
            .is_some_and(|items| items.iter().any(contains_credential_assignment));
    };
    let lowered = text.to_ascii_lowercase();
    [
        "password=",
        "password:",
        "passwd=",
        "passwd:",
        "api_key=",
        "api-key=",
        "api_key:",
        "api-key:",
        "secret=",
        "secret:",
        "bearer ",
        "token=",
        "token:",
    ]
    .iter()
    .any(|marker| lowered.contains(marker))
}

fn merge_summary(target: &mut Value, patch: &Value) -> ServiceResult<()> {
    let target = target
        .as_object_mut()
        .ok_or_else(|| ServiceError::unavailable("stored summary is invalid"))?;
    let patch = patch
        .as_object()
        .ok_or_else(|| ServiceError::invalid("summary patch must be an object"))?;
    let mut replaced = Vec::new();
    for (key, value) in patch {
        if SUMMARY_SEQUENCED_KEYS.contains(&key.as_str()) {
            replaced.push(key.clone());
        }
        target.insert(key.clone(), value.clone());
    }
    // A client- or model-supplied array replaces the stored values wholesale, so
    // the server-derived per-item provenance for those keys no longer lines up.
    if !replaced.is_empty() {
        if let Some(sources) = target
            .get_mut("fact_source_sequences")
            .and_then(Value::as_object_mut)
        {
            for key in &replaced {
                sources.remove(key);
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn summary_model_source_uses_only_the_model_patch_schema() {
        let source = json!({
            "current_status": "当前状态",
            "decisions": ["使用 5168"],
            "unresolved_tasks": ["补充 K3 测试"],
            "salient_facts": ["事实已包含在当前状态"],
            "commands_executed": ["cargo test"]
        });

        let normalized = summary_model_source(&source);

        assert_eq!(normalized["key_decisions"], json!(["使用 5168"]));
        assert_eq!(normalized["next_steps"], json!(["补充 K3 测试"]));
        assert_eq!(normalized["commands_executed"], json!(["cargo test"]));
        assert!(normalized.get("decisions").is_none());
        assert!(normalized.get("unresolved_tasks").is_none());
        assert!(normalized.get("salient_facts").is_none());
        validate_summary_patch(&normalized).unwrap();
    }

    #[test]
    fn summary_model_source_redacts_credentials_before_inference() {
        let source = json!({
            "current_status": "调试含 api_key=acceptance-secret-123，不得进入摘要",
            "decisions": [concat!("Authorization: Bear", "er local-test-token; 继续验证")],
            "unresolved_tasks": ["检查 password='not-a-real-password' 后的降级"]
        });

        let normalized = summary_model_source(&source);
        let encoded = serde_json::to_string(&normalized).unwrap();

        assert!(!encoded.contains("acceptance-secret-123"));
        assert!(!encoded.contains("local-test-token"));
        assert!(!encoded.contains("not-a-real-password"));
        assert_eq!(encoded.matches("[REDACTED CREDENTIAL]").count(), 3);
        assert!(encoded.contains("不得进入摘要"));
        assert!(encoded.contains("继续验证"));
        assert!(encoded.contains("后的降级"));
        validate_summary_patch(&normalized).unwrap();
    }

    #[test]
    fn summary_sentences_split_on_cjk_and_spaced_ascii_punctuation() {
        assert_eq!(
            split_summary_sentences("我决定用 rust。你去改 embedding.rs！好了吗？"),
            vec!["我决定用 rust", "你去改 embedding.rs", "好了吗"]
        );
        // An ASCII full stop only ends a sentence when whitespace follows it,
        // so paths and versions stay in one piece.
        assert_eq!(
            split_summary_sentences("Step one. Step two; step three.\nStep four"),
            vec!["Step one", "Step two", "step three", "Step four"]
        );
        assert_eq!(
            split_summary_sentences("保留 embedding.rs 与 v1.2 的写法"),
            vec!["保留 embedding.rs 与 v1.2 的写法"]
        );
        assert!(split_summary_sentences("   ").is_empty());
    }

    #[test]
    fn summary_dedup_normalizes_separators_and_keeps_distinct_identifiers() {
        assert_eq!(normalize_summary_text("使用　ＲＵＳＴ\t编写"), "使用 RUST 编写");
        assert!(similar_summary_text("请记住：我用 uv", "请记住: 我用 uv"));
        assert!(similar_summary_text("我习惯用深色主题", "我习惯用深色主题。"));
        assert!(!similar_summary_text("以后默认用 vim", "以后默认用 emacs"));
        assert!(!similar_summary_text("我喜欢深色主题", "我喜欢浅色主题"));
        assert!(!similar_summary_text("我决定默认用 uv 管理依赖 1", "我决定默认用 uv 管理依赖 2"));
    }

    #[test]
    fn summary_resources_come_from_assistant_completion_sentences() {
        let (paths, commands) = extract_summary_resources(
            "已完成 crates/memory-store/src/embedding.rs 的裁剪，执行 cargo test --workspace 通过",
        );
        assert_eq!(paths, vec!["crates/memory-store/src/embedding.rs"]);
        assert_eq!(commands, vec!["cargo test --workspace"]);
        // A URL is not a touched resource.
        let (paths, _) = extract_summary_resources("已完成 https://example.com/a/b 的核对");
        assert!(paths.is_empty());
    }

    #[test]
    fn summary_budget_drops_oldest_items_until_the_payload_fits() {
        let mut fields = SummaryFields::from_previous(&Value::Null);
        let filler = "x".repeat(SUMMARY_SENTENCE_BYTES - 16);
        for index in 0..SUMMARY_TOOL_ITEM_CAP {
            let sequence = index as i64 + 1;
            fields
                .files_resources_touched
                .push(&format!("/tmp/{index:03}/{filler}"), sequence);
            fields
                .commands_executed
                .push(&format!("cargo run {index:03} {filler}"), sequence);
            fields
                .operations_completed
                .push(&format!("已修改 /tmp/{index:03}/{filler}"), sequence);
            fields
                .errors_encountered
                .push(&format!("失败 {index:03} {filler}"), sequence);
        }
        for index in 0..SUMMARY_TEXT_ITEM_CAP {
            let sequence = 1000 + index as i64;
            fields
                .decisions
                .push(&format!("决定采用 {index:03} {filler}"), sequence);
            fields
                .unresolved_tasks
                .push(&format!("待办 {index:03} {filler}"), sequence);
            fields
                .salient_facts
                .push(&format!("请记住 {index:03} {filler}"), sequence);
        }
        let mut summary = fields.into_payload("状态".to_owned(), "budget-session", 500);
        assert!(summary_bytes(&summary) > MAX_TEXT_BYTES);

        assert!(enforce_summary_budget(&mut summary));

        assert!(summary_bytes(&summary) <= MAX_TEXT_BYTES);
        let items = summary["files_resources_touched"].as_array().unwrap();
        let origins = summary["fact_source_sequences"]["files_resources_touched"]
            .as_array()
            .unwrap();
        assert_eq!(items.len(), origins.len());
        assert!(items.len() < SUMMARY_TOOL_ITEM_CAP);
        // Oldest first: the most recent item always survives.
        assert!(items
            .last()
            .and_then(Value::as_str)
            .unwrap()
            .starts_with(&format!("/tmp/{:03}/", SUMMARY_TOOL_ITEM_CAP - 1)));
        assert_eq!(
            origins.last().and_then(Value::as_i64),
            Some(SUMMARY_TOOL_ITEM_CAP as i64)
        );
    }
}
