use crate::product::{EmbeddingConfig, ProductConfigPatch, SummaryModelConfig};
use crate::{
    db_error, decode_json_payload, encode_json_payload, hash_bytes, hash_text, json_bytes,
    parse_params, ServiceResult, Store,
};
use memory_core::{now_ms, MemoryIdentity};
use memory_protocol::{RequestEnvelope, ServiceError};
use rusqlite::{params, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::BTreeSet;
use uuid::Uuid;

#[derive(Deserialize)]
struct AuditQuery {
    #[serde(default = "default_limit")]
    limit: i64,
    #[serde(default)]
    before: Option<i64>,
}

#[derive(Deserialize)]
struct AuditRecord {
    action: String,
    #[serde(default = "default_audit_result")]
    result: String,
    #[serde(default)]
    details: Value,
}

#[derive(Deserialize)]
struct UpdateSettings {
    #[serde(default)]
    memory_enabled: Option<bool>,
    #[serde(default)]
    retention_days: Option<i64>,
    #[serde(default)]
    audit_enabled: Option<bool>,
    /// Retired cloud-backup settings (product change 2026-10-16). A client
    /// built against 0.1.28 still sends these; accepting them keeps that
    /// client's *other* setting changes working. The values are ignored and
    /// never reach the config file, which is how the dead keys get dropped.
    #[serde(default)]
    #[allow(dead_code)]
    backup_enabled: Option<bool>,
    #[serde(default)]
    #[allow(dead_code)]
    backup_frequency: Option<String>,
    #[serde(default)]
    #[allow(dead_code)]
    backup_scopes: Option<Vec<String>>,
    #[serde(default)]
    local_device_id: Option<String>,
    #[serde(default)]
    timezone: Option<String>,
    #[serde(default)]
    sensitive_action_confirmation: Option<String>,
    #[serde(default)]
    preference_auto_apply_enabled: Option<bool>,
    #[serde(default)]
    development_habit_auto_apply_enabled: Option<bool>,
    #[serde(default)]
    recent_turn_limit: Option<i64>,
    recent_session_limit: Option<i64>,
    #[serde(default)]
    summary_interval_turns: Option<i64>,
    #[serde(default)]
    summary_retention_days: Option<i64>,
    #[serde(default)]
    summary_model: Option<SummaryModelConfig>,
    #[serde(default)]
    embedding: Option<EmbeddingConfig>,
    /// Product-level storage guardrails. Runtime integrations should normally
    /// leave these unset; they are exposed here so an image integrator can
    /// manage them through the authenticated privacy settings path.
    #[serde(default)]
    max_database_bytes: Option<i64>,
    /// Retired with cloud backup; accepted and ignored for the same reason.
    #[serde(default)]
    #[allow(dead_code)]
    max_backup_bytes: Option<i64>,
    #[serde(default)]
    min_free_bytes: Option<i64>,
}

#[derive(Deserialize)]
struct ExportData {
    #[serde(default = "default_export_limit")]
    limit_per_collection: i64,
    #[serde(default = "default_true")]
    include_audit: bool,
    /// Opaque rowid watermark fixed by the first page. Re-echoing it on every
    /// later page keeps rows inserted during the export out of the snapshot.
    #[serde(default)]
    snapshot_at: Option<i64>,
    /// The three tables have independent rowid sequences, so one watermark
    /// cannot bound all of them.  The daemon returns one per collection and
    /// the client echoes it unchanged.
    #[serde(default)]
    snapshot: Option<ExportSnapshot>,
    /// Per-collection keyset cursors for the next page. A collection missing
    /// from this object has already been exhausted.
    #[serde(default)]
    cursor: Option<ExportCursor>,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize)]
struct ExportCursor {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    session_events: Option<ExportPosition>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    session_summaries: Option<ExportPosition>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    long_term_memories: Option<ExportPosition>,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize)]
struct ExportSnapshot {
    /// Compatibility scalar for older clients: the maximum of the three.
    #[serde(default)]
    snapshot_at: i64,
    #[serde(default)]
    session_events: i64,
    #[serde(default)]
    session_summaries: i64,
    #[serde(default)]
    long_term_memories: i64,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize)]
struct ExportPosition {
    /// Sort timestamp of the last row on the page: created_at for events and
    /// summaries, updated_at for L3.
    at: i64,
    /// Tiebreaker within the same millisecond.
    rowid: i64,
}

#[derive(Clone, Copy, Debug, Default, Serialize)]
struct ExportHasMore {
    session_events: bool,
    session_summaries: bool,
    long_term_memories: bool,
}

struct ExportPage {
    rows: Value,
    next: Option<ExportPosition>,
}

/// Wire format of a full local export. Import accepts exactly this document so
/// a file from another product (or a hand-edited blob) is not half-applied.
const EXPORT_FORMAT: &str = "bianbu-agent-memory.export.v1";

#[derive(Deserialize)]
struct ImportData {
    /// The whole `bianbu-agent-memory.export.v1` document, as returned by
    /// `privacy.export_data`.
    payload: Value,
}

#[derive(Deserialize)]
struct ConfirmedOperation {
    #[serde(default)]
    confirm_token: Option<String>,
    #[serde(default = "default_clear_reason")]
    reason_code: String,
}

#[derive(Deserialize)]
struct MaintenanceRequest {
    #[serde(default = "default_true")]
    remove_expired: bool,
}

fn default_limit() -> i64 {
    100
}

fn default_export_limit() -> i64 {
    200
}

fn default_true() -> bool {
    true
}

fn default_audit_result() -> String {
    "success".to_owned()
}

fn default_clear_reason() -> String {
    "user_request".to_owned()
}

impl Store {
    pub(crate) fn memory_is_enabled(&self, identity: &MemoryIdentity) -> ServiceResult<bool> {
        if !self.product_config()?.memory_enabled {
            return Ok(false);
        }
        let connection = self.connection()?;
        // MEM-NFR-01-01: this gate runs on every memory-data request, so the
        // statement is cached on the pooled connection like the search reads.
        let mut statement = connection
            .prepare_cached(
                "SELECT memory_enabled FROM memory_settings
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3",
            )
            .map_err(db_error)?;
        statement
            .query_row(
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id
                ],
                |row| row.get::<_, i64>(0),
            )
            .optional()
            .map(|value| value.unwrap_or(1) != 0)
            .map_err(db_error)
    }

    pub(crate) fn get_privacy_settings(&self, identity: &MemoryIdentity) -> ServiceResult<Value> {
        let product = self.product_config()?;
        let connection = self.connection()?;
        let row: Option<(i64, Option<i64>, i64, i64)> = connection
            .query_row(
                "SELECT memory_enabled,retention_days,audit_enabled,updated_at
                 FROM memory_settings
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3",
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id
                ],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .optional()
            .map_err(db_error)?;
        let (memory_enabled, retention_days, audit_enabled, updated_at) = row.unwrap_or((
            product.memory_enabled as i64,
            (product.retention_days != 0).then_some(product.retention_days),
            product.audit_enabled as i64,
            0,
        ));
        let mut result = self.product_config_json(&identity.account_id, &identity.device_id)?;
        let object = result
            .as_object_mut()
            .ok_or_else(|| ServiceError::unavailable("product config response is invalid"))?;
        object.insert("memory_enabled".to_owned(), json!(memory_enabled != 0));
        object.insert(
            "retention_days".to_owned(),
            json!(retention_days.unwrap_or(0)),
        );
        object.insert("audit_enabled".to_owned(), json!(audit_enabled != 0));
        object.insert("updated_at".to_owned(), json!(updated_at));
        Ok(result)
    }

    pub(crate) fn update_privacy_settings(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: UpdateSettings = parse_params(request)?;
        if input.memory_enabled.is_none()
            && input.retention_days.is_none()
            && input.audit_enabled.is_none()
            && input.local_device_id.is_none()
            && input.timezone.is_none()
            && input.sensitive_action_confirmation.is_none()
            && input.preference_auto_apply_enabled.is_none()
            && input.development_habit_auto_apply_enabled.is_none()
            && input.recent_turn_limit.is_none()
            && input.recent_session_limit.is_none()
            && input.summary_interval_turns.is_none()
            && input.summary_retention_days.is_none()
            && input.summary_model.is_none()
            && input.embedding.is_none()
            && input.max_database_bytes.is_none()
            && input.min_free_bytes.is_none()
        {
            return Err(ServiceError::invalid("at least one setting is required"));
        }
        if input
            .retention_days
            .is_some_and(|days| !matches!(days, 0 | 7 | 30 | 90))
        {
            return Err(ServiceError::invalid(
                "retention_days must be 0, 7, 30, or 90; 0 means permanent",
            ));
        }
        if let Some(cached) =
            self.cached_write_response(identity, request, "privacy.settings.update")?
        {
            return Ok(cached);
        }
        let product_patch = ProductConfigPatch {
            local_device_id: input.local_device_id.clone(),
            timezone: input.timezone.clone(),
            memory_enabled: input.memory_enabled,
            retention_days: input.retention_days,
            audit_enabled: input.audit_enabled,
            sensitive_action_confirmation: input.sensitive_action_confirmation.clone(),
            preference_auto_apply_enabled: input.preference_auto_apply_enabled,
            development_habit_auto_apply_enabled: input.development_habit_auto_apply_enabled,
            recent_turn_limit: input.recent_turn_limit,
            recent_session_limit: input.recent_session_limit,
            summary_interval_turns: input.summary_interval_turns,
            summary_retention_days: input.summary_retention_days,
            summary_model: input.summary_model.clone(),
            embedding: input.embedding.clone(),
            max_database_bytes: input.max_database_bytes,
            min_free_bytes: input.min_free_bytes,
        };
        if product_patch.is_empty() {
            return Err(ServiceError::invalid("at least one setting is required"));
        }
        let product = self.update_product_config(&product_patch)?;
        let mut product_view =
            self.product_config_json(&identity.account_id, &identity.device_id)?;
        self.with_write(identity, request, "privacy.settings.update", "settings", move |tx, _, now, _, _| {
            let current: Option<(i64, Option<i64>, i64)> = tx.query_row(
                "SELECT memory_enabled,retention_days,audit_enabled FROM memory_settings
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3",
                params![identity.account_id,identity.agent_id,identity.memory_space_id],
                |row| Ok((row.get(0)?,row.get(1)?,row.get(2)?)),
            ).optional().map_err(db_error)?;
            let (old_memory, old_retention, old_audit) = current.unwrap_or((
                product.memory_enabled as i64,
                (product.retention_days != 0).then_some(product.retention_days),
                product.audit_enabled as i64,
            ));
            let memory_enabled = input.memory_enabled.map(|value| value as i64).unwrap_or(old_memory);
            let retention_days = match input.retention_days {
                Some(0) => None,
                Some(days) => Some(days),
                None => old_retention,
            };
            let audit_enabled = input.audit_enabled.map(|value| value as i64).unwrap_or(old_audit);
            tx.execute(
                "INSERT INTO memory_settings(account_id,agent_id,memory_space_id,memory_enabled,retention_days,audit_enabled,updated_at)
                 VALUES(?1,?2,?3,?4,?5,?6,?7)
                 ON CONFLICT(account_id,agent_id,memory_space_id) DO UPDATE SET
                   memory_enabled=excluded.memory_enabled,retention_days=excluded.retention_days,
                   audit_enabled=excluded.audit_enabled,updated_at=excluded.updated_at",
                params![identity.account_id,identity.agent_id,identity.memory_space_id,memory_enabled,retention_days,audit_enabled,now],
            ).map_err(db_error)?;
            // MEM-PRD retention: tightening the window is retroactive. Rows
            // stored under a wider setting (or permanent) are capped at
            // created_at + the new window so the hourly maintenance pass can
            // archive/purge them like any other expired row. The cap never
            // extends an existing stamp (explicit shorter expiries such as
            // the 7-day tool-event window are preserved), and widening or
            // going permanent does not touch stored stamps. The sweep is
            // keyed off the request value, not the stored one: the product
            // config patch is applied before this closure runs, so the
            // fallback for namespaces without a memory_settings row already
            // reflects the new value (and a repeated non-tightening update
            // simply matches no rows).
            let restamped = if let Some(days) = input.retention_days.filter(|days| *days > 0) {
                let window_ms = days.saturating_mul(24 * 60 * 60 * 1000);
                let mut restamped = 0_usize;
                for table in ["session_events", "long_term_memories", "instructions"] {
                    restamped += tx
                        .execute(
                            &format!(
                                "UPDATE {table} SET expires_at = created_at + ?4
                                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3
                                   AND (expires_at IS NULL OR expires_at > created_at + ?4)"
                            ),
                            params![
                                identity.account_id,
                                identity.agent_id,
                                identity.memory_space_id,
                                window_ms
                            ],
                        )
                        .map_err(db_error)?;
                }
                restamped
            } else {
                0
            };
            let object = product_view
                .as_object_mut()
                .ok_or_else(|| ServiceError::unavailable("product config response is invalid"))?;
            object.insert("memory_enabled".to_owned(), json!(memory_enabled != 0));
            object.insert("retention_days".to_owned(), json!(retention_days.unwrap_or(0)));
            object.insert("audit_enabled".to_owned(), json!(audit_enabled != 0));
            object.insert("updated_at".to_owned(), json!(now));
            object.insert("retention_restamped".to_owned(), json!(restamped));
            Ok((product_view,Some(identity.memory_space_id.clone())))
        })
    }

    pub(crate) fn list_audit(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: AuditQuery = parse_params(request)?;
        if !(1..=500).contains(&input.limit) {
            return Err(ServiceError::invalid("limit must be between 1 and 500"));
        }
        let before = input.before.unwrap_or(i64::MAX);
        let account_hash = hash_text(&identity.account_id);
        let connection = self.connection()?;
        let mut statement = connection
            .prepare(
                "SELECT audit_id,event_type,record_type,record_id_hash,action,result,trace_id,
                    details_json,created_at,export_state
             FROM audit_spool
             WHERE account_id_hash=?1 AND agent_id=?2 AND created_at<?3
             ORDER BY created_at DESC,audit_id DESC LIMIT ?4",
            )
            .map_err(db_error)?;
        let rows = statement
            .query_map(
                params![account_hash, identity.agent_id, before, input.limit],
                |row| {
                    let details: String = row.get(7)?;
                    Ok(json!({
                        "audit_id":row.get::<_,String>(0)?,
                        "event_type":row.get::<_,String>(1)?,
                        "record_type":row.get::<_,Option<String>>(2)?,
                        "record_id_hash":row.get::<_,Option<String>>(3)?,
                        "action":row.get::<_,String>(4)?,
                        "result":row.get::<_,String>(5)?,
                        "trace_id":row.get::<_,String>(6)?,
                        "details":serde_json::from_str::<Value>(&details).unwrap_or(Value::Null),
                        "created_at":row.get::<_,i64>(8)?,
                        "export_state":row.get::<_,String>(9)?
                    }))
                },
            )
            .map_err(db_error)?;
        let records = rows.collect::<Result<Vec<_>, _>>().map_err(db_error)?;
        let next_before = records
            .last()
            .and_then(|value| value.get("created_at"))
            .and_then(Value::as_i64);
        Ok(json!({"records":records,"next_before":next_before}))
    }

    pub(crate) fn export_audit(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let listed = self.list_audit(identity, request)?;
        Ok(json!({
            "format":"bianbu-agent-memory.audit.v1",
            "exported_at":now_ms(),
            "agent_id":identity.agent_id,
            "records":listed.get("records").cloned().unwrap_or_else(|| json!([])),
            "next_before":listed.get("next_before").cloned().unwrap_or(Value::Null)
        }))
    }

    pub(crate) fn record_audit(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: AuditRecord = parse_params(request)?;
        if input.action.trim().is_empty() || input.action.len() > 256 {
            return Err(ServiceError::invalid("invalid audit action"));
        }
        if input.result.trim().is_empty() || input.result.len() > 64 {
            return Err(ServiceError::invalid("invalid audit result"));
        }
        let details = serde_json::to_string(&input.details)
            .map_err(|_| ServiceError::invalid("invalid audit details"))?;
        if details.len() > 32_768 {
            return Err(ServiceError::invalid("audit details are too large"));
        }
        let connection = self.connection()?;
        let audit_enabled = connection
            .query_row(
                "SELECT audit_enabled FROM memory_settings
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3",
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id
                ],
                |row| row.get::<_, i64>(0),
            )
            .optional()
            .map_err(db_error)?
            .unwrap_or(1)
            != 0;
        if !audit_enabled {
            return Ok(json!({"recorded":false,"reason":"audit_disabled"}));
        }
        let audit_id = Uuid::new_v4().to_string();
        let created_at = now_ms();
        connection.execute(
            "INSERT INTO audit_spool(
                audit_id,event_type,account_id_hash,device_id,agent_id,agent_instance_id,
                record_type,record_id_hash,action,result,trace_id,details_json,created_at,export_state
             ) VALUES(?1,'runtime_operation',?2,?3,?4,?5,'runtime',NULL,?6,?7,?8,?9,?10,'local')",
            params![audit_id,hash_text(&identity.account_id),identity.device_id,identity.agent_id,
                    identity.agent_instance_id,input.action,input.result,request.trace_id,details,created_at],
        ).map_err(db_error)?;
        Ok(json!({"audit_id":audit_id,"created_at":created_at}))
    }

    pub(crate) fn export_data(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: ExportData = parse_params(request)?;
        if !(1..=500).contains(&input.limit_per_collection) {
            return Err(ServiceError::invalid(
                "limit_per_collection must be between 1 and 500",
            ));
        }
        let connection = self.connection()?;
        let settings = self.get_privacy_settings(identity)?;
        let snapshot = match (input.snapshot, input.snapshot_at) {
            (Some(snapshot), _) => snapshot,
            // Older SDKs only know the scalar. Repeating it per collection is
            // the safest interpretation and keeps their paging monotonic.
            (None, Some(legacy)) => ExportSnapshot {
                snapshot_at: legacy,
                session_events: legacy,
                session_summaries: legacy,
                long_term_memories: legacy,
            },
            (None, None) => ExportSnapshot {
                snapshot_at: 0,
                session_events: max_rowid(&connection, "session_events", identity),
                session_summaries: max_rowid(&connection, "session_summaries", identity),
                long_term_memories: max_rowid(&connection, "long_term_memories", identity),
            },
        };
        let snapshot_at = snapshot
            .session_events
            .max(snapshot.session_summaries)
            .max(snapshot.long_term_memories);
        // Keep the compatibility scalar self-consistent on the first page too:
        // a caller may echo `snapshot` verbatim without patching this field.
        let snapshot = ExportSnapshot {
            snapshot_at,
            ..snapshot
        };
        let cursor = input.cursor.unwrap_or_default();
        let event_page = collect_session_event_page(
            &connection,
            identity,
            snapshot.session_events,
            cursor.session_events,
            input.limit_per_collection,
        )?;
        let summary_page = collect_export_page(
            &connection,
            "SELECT json_object('session_id',session_id,'revision',revision,'summary',json(summary_payload),
                    'source_event_sequence',source_event_sequence,'producer',producer,'created_at',created_at),
                    created_at,rowid
             FROM session_summaries
             WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND is_current=1
               AND rowid<=?4
               AND (?5 IS NULL OR created_at<?5 OR (created_at=?5 AND rowid<?6))
             ORDER BY created_at DESC,rowid DESC LIMIT ?7",
            identity,
            snapshot.session_summaries,
            cursor.session_summaries,
            input.limit_per_collection,
        )?;
        let memory_page = collect_export_page(
            &connection,
            "SELECT json_object('memory_id',memory_id,'kind',kind,'text',content,'summary',summary,
                    'tags',json(tags_json),'source',source,'sensitivity',sensitivity,
                    'revision',current_revision,'created_at',created_at,'updated_at',updated_at),
                    updated_at,rowid
             FROM long_term_memories
             WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3
               AND rowid<=?4
               AND (?5 IS NULL OR updated_at<?5 OR (updated_at=?5 AND rowid<?6))
             ORDER BY updated_at DESC,rowid DESC LIMIT ?7",
            identity,
            snapshot.long_term_memories,
            cursor.long_term_memories,
            input.limit_per_collection,
        )?;
        let audit = if input.include_audit {
            let audit_request = RequestEnvelope {
                params: json!({"limit":input.limit_per_collection}),
                ..request.clone()
            };
            self.list_audit(identity, &audit_request)?["records"].clone()
        } else {
            json!([])
        };
        let has_more = ExportHasMore {
            session_events: event_page.next.is_some(),
            session_summaries: summary_page.next.is_some(),
            long_term_memories: memory_page.next.is_some(),
        };
        let next_cursor = ExportCursor {
            session_events: event_page.next,
            session_summaries: summary_page.next,
            long_term_memories: memory_page.next,
        };
        Ok(json!({
            "format":EXPORT_FORMAT,
            "exported_at":now_ms(),
            "scope":{"account_id":identity.account_id,"agent_id":identity.agent_id,"memory_space_id":identity.memory_space_id},
            "settings":settings,
            "session_events":event_page.rows,
            "session_summaries":summary_page.rows,
            "long_term_memories":memory_page.rows,
            "audit":audit,
            "limit_per_collection":input.limit_per_collection,
            "snapshot_at":snapshot_at,
            "snapshot":snapshot,
            "has_more":has_more,
            "has_more_any":has_more.session_events || has_more.session_summaries || has_more.long_term_memories,
            "next_cursor":next_cursor
        }))
    }

    /// Restore a `privacy.export_data` document into the caller's namespace.
    ///
    /// The import is a merge, never a replace: rows are keyed by their original
    /// id (`event_id` / `session_id` + revision / `memory_id`), an existing row
    /// is left untouched, and only records inside the caller's own namespace
    /// are considered. Re-importing the same file therefore reports every
    /// record as skipped instead of duplicating it, and a record the user has
    /// since edited or deleted stays as it is: an import must not silently undo
    /// a later correction.
    ///
    /// Only L1 events, L2 summaries, and L3 memories are restored. Settings and
    /// audit records are not imported, and a record the export could not read
    /// (legacy event encoding) is counted as skipped rather than guessed at.
    ///
    /// Restored rows are written with `sync_policy=local_only`: an import is a
    /// local restore, so it must not enqueue an upload of the restored data.
    pub(crate) fn import_data(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: ImportData = parse_params(request)?;
        let document = input
            .payload
            .as_object()
            .ok_or_else(|| ServiceError::invalid("import payload must be an object"))?;
        if document.get("format").and_then(Value::as_str) != Some(EXPORT_FORMAT) {
            return Err(ServiceError::invalid(format!(
                "import payload must use format {EXPORT_FORMAT}"
            )));
        }
        // Rows land in this namespace only. A document carrying a different
        // scope is refused outright instead of being silently displaced into
        // this space: that would mix one agent's memory into another's.
        if let Some(scope) = document.get("scope").and_then(Value::as_object) {
            for (key, expected) in [
                ("account_id", identity.account_id.as_str()),
                ("agent_id", identity.agent_id.as_str()),
                ("memory_space_id", identity.memory_space_id.as_str()),
            ] {
                if scope
                    .get(key)
                    .and_then(Value::as_str)
                    .is_some_and(|provided| provided != expected)
                {
                    return Err(ServiceError::invalid(format!(
                        "import payload belongs to a different {key}"
                    )));
                }
            }
        }
        let events = import_array(document, "session_events")?;
        let summaries = import_array(document, "session_summaries")?;
        let memories = import_array(document, "long_term_memories")?;
        let summary_retention_days = self.product_config()?.summary_retention_days;
        // Collected inside the transaction, embedded after it commits: the
        // vector is derived data and the background job is the retry path.
        let mut imported_memory_ids = Vec::new();
        let result = self.with_write(
            identity,
            request,
            "privacy.import_data",
            "import",
            |tx, hlc, now, _, _| {
                let mut imported = ImportCounts::default();
                let mut skipped = ImportCounts::default();
                for event in &events {
                    match import_event(tx, identity, event, hlc, now) {
                        Ok(true) => imported.session_events += 1,
                        Ok(_) => skipped.session_events += 1,
                        Err(_) => skipped.session_events += 1,
                    }
                }
                for summary in &summaries {
                    match import_summary(tx, identity, summary, hlc, now, summary_retention_days) {
                        Ok(true) => imported.session_summaries += 1,
                        Ok(_) => skipped.session_summaries += 1,
                        Err(_) => skipped.session_summaries += 1,
                    }
                }
                for memory in &memories {
                    match import_memory(tx, identity, memory, hlc, now) {
                        Ok(Some(memory_id)) => {
                            imported.long_term_memories += 1;
                            imported_memory_ids.push(memory_id);
                        }
                        Ok(None) => skipped.long_term_memories += 1,
                        Err(_) => skipped.long_term_memories += 1,
                    }
                }
                Ok((
                    json!({
                        "imported_at":now,
                        "format":EXPORT_FORMAT,
                        "imported":imported,
                        "skipped":skipped,
                    }),
                    Some(identity.memory_space_id.clone()),
                ))
            },
        )?;
        for memory_id in &imported_memory_ids {
            crate::embedding::index_memory_after_commit(self, identity, memory_id);
        }
        Ok(result)
    }

    pub(crate) fn clear_history(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: ConfirmedOperation = parse_params(request)?;
        validate_confirmed_operation(&input)?;
        self.with_write(identity, request, "privacy.clear_history", "conversation_history", |tx, _, now, audit_id, _| {
            let events = tx.execute(
                "DELETE FROM session_events WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3",
                params![identity.account_id,identity.agent_id,identity.memory_space_id],
            ).map_err(db_error)?;
            let summaries = tx.execute(
                "DELETE FROM session_summaries WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3",
                params![identity.account_id,identity.agent_id,identity.memory_space_id],
            ).map_err(db_error)?;
            let checkpoints = tx.execute(
                "DELETE FROM checkpoint_refs WHERE account_id=?1 AND agent_id=?2",
                params![identity.account_id,identity.agent_id],
            ).map_err(db_error)?;
            let archived_ids = scoped_memory_ids(tx, identity, "kind='episodic' AND tags_json LIKE '%session-summary%'")?;
            for memory_id in &archived_ids {
                tx.execute("DELETE FROM long_term_fts WHERE memory_id=?1",[memory_id]).map_err(db_error)?;
                super::long_term::delete_fts2_row(tx,memory_id)?;
                tx.execute("DELETE FROM evidence_refs WHERE memory_id=?1",[memory_id]).map_err(db_error)?;
                tx.execute("DELETE FROM memory_revisions WHERE memory_id=?1",[memory_id]).map_err(db_error)?;
                tx.execute("DELETE FROM semantic_projection_mappings WHERE memory_id=?1",[memory_id]).map_err(db_error)?;
                tx.execute("DELETE FROM memory_embeddings WHERE memory_id=?1",[memory_id]).map_err(db_error)?;
                tx.execute("DELETE FROM long_term_memories WHERE memory_id=?1",[memory_id]).map_err(db_error)?;
            }
            Ok((json!({
                "cleared_at":now,"reason_code":input.reason_code,"audit_id":audit_id,
                "deleted":{"session_events":events,"session_summaries":summaries,"checkpoints":checkpoints,"archived_summaries":archived_ids.len()},
                "long_term_preferences_retained":true
            }),Some(identity.memory_space_id.clone())))
        })
    }

    pub(crate) fn clear_all(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: ConfirmedOperation = parse_params(request)?;
        validate_confirmed_operation(&input)?;
        self.with_write(identity, request, "privacy.clear_all", "all_memory", |tx, _, now, audit_id, _| {
            let ids = scoped_memory_ids(tx, identity, "1=1")?;
            for memory_id in &ids {
                tx.execute("DELETE FROM long_term_fts WHERE memory_id=?1",[memory_id]).map_err(db_error)?;
                super::long_term::delete_fts2_row(tx,memory_id)?;
                tx.execute("DELETE FROM evidence_refs WHERE memory_id=?1",[memory_id]).map_err(db_error)?;
                tx.execute("DELETE FROM memory_revisions WHERE memory_id=?1",[memory_id]).map_err(db_error)?;
                tx.execute("DELETE FROM semantic_projection_mappings WHERE memory_id=?1",[memory_id]).map_err(db_error)?;
                tx.execute("DELETE FROM memory_embeddings WHERE memory_id=?1",[memory_id]).map_err(db_error)?;
            }
            let memories = tx.execute(
                "DELETE FROM long_term_memories WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3",
                params![identity.account_id,identity.agent_id,identity.memory_space_id],
            ).map_err(db_error)?;
            let events = tx.execute(
                "DELETE FROM session_events WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3",
                params![identity.account_id,identity.agent_id,identity.memory_space_id],
            ).map_err(db_error)?;
            let summaries = tx.execute(
                "DELETE FROM session_summaries WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3",
                params![identity.account_id,identity.agent_id,identity.memory_space_id],
            ).map_err(db_error)?;
            let instructions = tx.execute(
                "DELETE FROM instructions WHERE account_id=?1 AND (agent_id IS NULL OR agent_id=?2)",
                params![identity.account_id,identity.agent_id],
            ).map_err(db_error)?;
            tx.execute("DELETE FROM checkpoint_refs WHERE account_id=?1 AND agent_id=?2",params![identity.account_id,identity.agent_id]).map_err(db_error)?;
            tx.execute("DELETE FROM semantic_projection_jobs WHERE account_id=?1 AND agent_id=?2",params![identity.account_id,identity.agent_id]).map_err(db_error)?;
            // The namespace is now empty, so drop its backfill state too —
            // a missing state row reads as ready and any new row is indexed
            // inline from here on.
            tx.execute(
                "DELETE FROM lexical_index_state WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3",
                params![identity.account_id,identity.agent_id,identity.memory_space_id],
            ).map_err(db_error)?;
            Ok((json!({
                "cleared_at":now,"reason_code":input.reason_code,"audit_id":audit_id,
                "deleted":{"session_events":events,"session_summaries":summaries,"long_term_memories":memories,"instructions":instructions}
            }),Some(identity.memory_space_id.clone())))
        })
    }

    pub(crate) fn run_maintenance(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: MaintenanceRequest = parse_params(request)?;
        let mut result = self.with_write(identity, request, "maintenance.run", "maintenance", |tx, _, now, _, _| {
            let mut expired_events = 0;
            let mut expired_memories = 0;
            let mut expired_instructions = 0;
            let mut archived_summaries = 0;
            if input.remove_expired {
                let mut summary_statement = tx.prepare(
                    "SELECT session_id,summary_payload,created_at FROM session_summaries
                     WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND is_current=1
                       AND expires_at IS NOT NULL AND expires_at<=?4",
                ).map_err(db_error)?;
                let summary_rows = summary_statement.query_map(
                    params![identity.account_id,identity.agent_id,identity.memory_space_id,now],
                    |row| Ok((row.get::<_,String>(0)?,row.get::<_,Vec<u8>>(1)?,row.get::<_,i64>(2)?)),
                ).map_err(db_error)?;
                let summaries = summary_rows.collect::<Result<Vec<_>,_>>().map_err(db_error)?;
                drop(summary_statement);
                for (session_id, summary_blob, created_at) in &summaries {
                    let summary = serde_json::from_slice::<Value>(summary_blob).unwrap_or(Value::Null);
                    let text = lifecycle_archive_text(&summary);
                    if text.is_empty() {
                        continue;
                    }
                    // Which session events the archived facts came from.
                    // Summaries written before this field existed, and facts
                    // produced by the summary model, stay "unknown" instead of
                    // failing the archive.
                    let source_event_sequences = lifecycle_source_sequences(&summary);
                    let memory_id = Uuid::new_v4().to_string();
                    let mut tag_values = vec![
                        json!("session-summary"),
                        json!(format!("session:{session_id}")),
                    ];
                    if !source_event_sequences.is_empty() {
                        tag_values.push(json!(format!(
                            "session:{session_id}:events:{}",
                            summarize_sequence_list(&source_event_sequences)
                        )));
                    }
                    let tags = Value::Array(tag_values);
                    let mut revision_payload = json!({
                        "memory_id":memory_id,"kind":"episodic","text":text,"summary":Value::Null,
                        "tags":tags,"source":"memoryd.lifecycle","confidence":1.0,
                        "sensitivity":"private","revision":1,"sync_policy":"local_only",
                        "expires_at":Value::Null
                    });
                    if !source_event_sequences.is_empty() {
                        revision_payload["source_event_sequences"] = json!(source_event_sequences);
                    }
                    let revision_blob = json_bytes(&revision_payload)?;
                    let payload_hash = hash_bytes(&revision_blob);
                    tx.execute(
                        "INSERT INTO long_term_memories(
                            memory_id,account_id,agent_id,memory_space_id,kind,content,summary,tags_json,
                            source,confidence,sensitivity,current_revision,sync_policy,expires_at,
                            created_at,updated_at,hlc
                         ) VALUES(?1,?2,?3,?4,'episodic',?5,NULL,?6,'memoryd.lifecycle',1.0,
                                  'private',1,'local_only',NULL,?7,?8,?9)",
                        params![memory_id,identity.account_id,identity.agent_id,identity.memory_space_id,
                                text,tags.to_string(),created_at,now,format!("{:020}:0000000000:{}",now.max(0),identity.device_id)],
                    ).map_err(db_error)?;
                    tx.execute(
                        "INSERT INTO memory_revisions(
                            memory_id,revision,parent_revision,payload,payload_hash,author_device_id,
                            author_agent_id,created_at,hlc
                         ) VALUES(?1,1,NULL,?2,?3,?4,?5,?6,?7)",
                        params![memory_id,revision_blob,payload_hash,identity.device_id,identity.agent_id,now,
                                format!("{:020}:0000000000:{}",now.max(0),identity.device_id)],
                    ).map_err(db_error)?;
                    super::long_term::index_memory(tx,&memory_id,&text,None,&tags.to_string(),"private")?;
                    archived_summaries += 1;
                }
                tx.execute(
                    "DELETE FROM session_summaries WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3
                     AND expires_at IS NOT NULL AND expires_at<=?4",
                    params![identity.account_id,identity.agent_id,identity.memory_space_id,now],
                ).map_err(db_error)?;
                expired_events = tx.execute(
                    "DELETE FROM session_events WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3
                     AND expires_at IS NOT NULL AND expires_at<=?4",
                    params![identity.account_id,identity.agent_id,identity.memory_space_id,now],
                ).map_err(db_error)?;
                let expired_ids = scoped_memory_ids(tx, identity, "expires_at IS NOT NULL AND expires_at<=unixepoch('subsec')*1000")?;
                for memory_id in &expired_ids {
                    tx.execute("DELETE FROM long_term_fts WHERE memory_id=?1",[memory_id]).map_err(db_error)?;
                    super::long_term::delete_fts2_row(tx,memory_id)?;
                    tx.execute("DELETE FROM evidence_refs WHERE memory_id=?1",[memory_id]).map_err(db_error)?;
                    tx.execute("DELETE FROM memory_revisions WHERE memory_id=?1",[memory_id]).map_err(db_error)?;
                    tx.execute("DELETE FROM semantic_projection_mappings WHERE memory_id=?1",[memory_id]).map_err(db_error)?;
                    tx.execute("DELETE FROM memory_embeddings WHERE memory_id=?1",[memory_id]).map_err(db_error)?;
                    tx.execute("DELETE FROM long_term_memories WHERE memory_id=?1",[memory_id]).map_err(db_error)?;
                }
                expired_memories = expired_ids.len();
                expired_instructions = tx
                    .execute(
                        "DELETE FROM instructions WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3
                         AND expires_at IS NOT NULL AND expires_at<=?4",
                        params![identity.account_id,identity.agent_id,identity.memory_space_id,now],
                    )
                    .map_err(db_error)?;
                tx.execute("DELETE FROM idempotency_keys WHERE expires_at<=?1",[now]).map_err(db_error)?;
                tx.execute(
                    "DELETE FROM audit_spool WHERE event_type='runtime_operation' AND created_at<=?1",
                    [now.saturating_sub(7 * 24 * 60 * 60 * 1000)],
                ).map_err(db_error)?;
            }
            Ok((json!({
                "completed_at":now,"expired_session_events":expired_events,
                "expired_long_term_memories":expired_memories,
                "expired_instructions":expired_instructions,
                "archived_session_summaries":archived_summaries
            }),Some(identity.memory_space_id.clone())))
        })?;
        result["storage"] = self.storage_status()?;
        Ok(result)
    }
}


fn lifecycle_archive_text(summary: &Value) -> String {
    let mut sections = Vec::new();
    // Archive the rules-produced fields together with the optional
    // model-produced fields, so enabling the summary model actually reaches
    // long-term memory instead of only rewriting the session status text.
    for (label, keys) in [
        ("稳定事实", ["salient_facts", "user_preferences"]),
        ("决策", ["decisions", "key_decisions"]),
        ("未完成事项", ["unresolved_tasks", "next_steps"]),
    ] {
        let mut values: Vec<String> = Vec::new();
        for key in keys {
            for value in summary
                .get(key)
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
            {
                if !values.iter().any(|existing| existing.as_str() == value) {
                    values.push(value.to_owned());
                }
            }
        }
        if !values.is_empty() {
            sections.push(format!("{label}: {}", values.join("；")));
        }
    }
    sections.join("\n")
}

/// Collect the session event sequences recorded for the summary fields that
/// actually contributed text to the archived memory. Items written by the
/// summary model carry no provenance and are simply not traceable.
fn lifecycle_source_sequences(summary: &Value) -> Vec<i64> {
    let Some(recorded) = summary.get("fact_source_sequences").and_then(Value::as_object) else {
        return Vec::new();
    };
    let mut sequences = BTreeSet::new();
    for key in [
        "salient_facts",
        "user_preferences",
        "decisions",
        "key_decisions",
        "unresolved_tasks",
        "next_steps",
    ] {
        let Some(items) = summary.get(key).and_then(Value::as_array) else {
            continue;
        };
        let Some(origins) = recorded.get(key).and_then(Value::as_array) else {
            continue;
        };
        for (index, item) in items.iter().enumerate() {
            if item.as_str().map_or(true, |value| value.trim().is_empty()) {
                continue;
            }
            if let Some(sequence) = origins.get(index).and_then(Value::as_i64) {
                if sequence > 0 {
                    sequences.insert(sequence);
                }
            }
        }
    }
    sequences.into_iter().collect()
}

/// Bounded event pointer for the L3 `tags` column: the exact list for short
/// spans, otherwise the covered range. The full list is kept in the revision
/// payload as `source_event_sequences`.
fn summarize_sequence_list(sequences: &[i64]) -> String {
    const MAX_EXACT: usize = 32;
    if sequences.len() <= MAX_EXACT {
        return sequences
            .iter()
            .map(i64::to_string)
            .collect::<Vec<_>>()
            .join(",");
    }
    format!("{}-{}", sequences[0], sequences[sequences.len() - 1])
}

#[derive(Clone, Copy, Default, serde::Serialize)]
struct ImportCounts {
    session_events: u64,
    session_summaries: u64,
    long_term_memories: u64,
}

fn import_array(document: &serde_json::Map<String, Value>, key: &str) -> ServiceResult<Vec<Value>> {
    match document.get(key) {
        None => Ok(Vec::new()),
        Some(Value::Array(items)) => Ok(items.clone()),
        Some(_) => Err(ServiceError::invalid(format!("{key} must be an array"))),
    }
}

fn import_timestamp(value: &Value, fallback: i64) -> i64 {
    value.as_i64().filter(|stamp| *stamp > 0).unwrap_or(fallback)
}

/// Insert one L1 event. `Ok(false)` means it was skipped, not that it failed:
/// a duplicate id, or a field the store would refuse (unknown event type,
/// oversized payload), is left out instead of aborting the whole import.
fn import_event(
    tx: &rusqlite::Transaction<'_>,
    identity: &MemoryIdentity,
    value: &Value,
    hlc: &str,
    now: i64,
) -> ServiceResult<bool> {
    let event_id = value
        .get("event_id")
        .and_then(Value::as_str)
        .filter(|value| memory_core::valid_identifier(value))
        .ok_or_else(|| ServiceError::invalid("invalid event_id"))?;
    let session_id = value
        .get("session_id")
        .and_then(Value::as_str)
        .ok_or_else(|| ServiceError::invalid("invalid session_id"))?;
    super::sessions::validate_session_id(session_id)?;
    let event_type = value
        .get("event_type")
        .and_then(Value::as_str)
        .filter(|value| memory_core::allowed_event_type(value))
        .ok_or_else(|| ServiceError::invalid("invalid event_type"))?;
    let payload = value
        .get("payload")
        .ok_or_else(|| ServiceError::invalid("event payload is missing"))?;
    let logical = json_bytes(payload)?;
    if logical.len() > memory_core::MAX_TEXT_BYTES {
        return Err(ServiceError::invalid("event payload is too large"));
    }
    let exists: bool = tx
        .query_row(
            "SELECT 1 FROM session_events WHERE event_id=?1",
            [event_id],
            |_| Ok(()),
        )
        .optional()
        .map_err(db_error)?
        .is_some();
    if exists {
        return Ok(false);
    }
    // Keep the exported sequence when it is still free in this namespace;
    // otherwise append after the newest event so the UNIQUE key holds.
    let requested = value
        .get("event_sequence")
        .and_then(Value::as_i64)
        .filter(|sequence| *sequence > 0);
    let sequence: i64 = match requested {
        Some(sequence)
            if !tx
                .query_row(
                    "SELECT 1 FROM session_events
                     WHERE account_id=?1 AND agent_id=?2 AND session_id=?3 AND event_sequence=?4",
                    params![
                        identity.account_id,
                        identity.agent_id,
                        session_id,
                        sequence
                    ],
                    |_| Ok(()),
                )
                .optional()
                .map_err(db_error)?
                .is_some() =>
        {
            sequence
        }
        _ => tx
            .query_row(
                "SELECT COALESCE(MAX(event_sequence),0)+1 FROM session_events
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND session_id=?4",
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id,
                    session_id
                ],
                |row| row.get(0),
            )
            .map_err(db_error)?,
    };
    let (payload_blob, payload_encoding) = encode_json_payload(payload)?;
    tx.execute(
        "INSERT INTO session_events(
            event_id,account_id,agent_id,memory_space_id,session_id,event_sequence,event_type,
            payload,payload_encoding,payload_hash,sync_policy,expires_at,created_at,hlc
         ) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,'local_only',?11,?12,?13)",
        params![
            event_id,
            identity.account_id,
            identity.agent_id,
            identity.memory_space_id,
            session_id,
            sequence,
            event_type,
            payload_blob,
            payload_encoding,
            hash_bytes(&logical),
            value.get("expires_at").and_then(Value::as_i64),
            import_timestamp(value.get("created_at").unwrap_or(&Value::Null), now),
            hlc
        ],
    )
    .map_err(db_error)?;
    Ok(true)
}

/// Insert one L2 summary. A session that already has a current summary keeps
/// it: summaries are derived, so replacing one would rewrite history the user
/// or the summariser has moved past.
fn import_summary(
    tx: &rusqlite::Transaction<'_>,
    identity: &MemoryIdentity,
    value: &Value,
    hlc: &str,
    now: i64,
    retention_days: i64,
) -> ServiceResult<bool> {
    let session_id = value
        .get("session_id")
        .and_then(Value::as_str)
        .ok_or_else(|| ServiceError::invalid("invalid session_id"))?;
    super::sessions::validate_session_id(session_id)?;
    let summary = value
        .get("summary")
        .ok_or_else(|| ServiceError::invalid("summary is missing"))?;
    super::sessions::validate_summary_patch(summary)?;
    let exists: bool = tx
        .query_row(
            "SELECT 1 FROM session_summaries
             WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND session_id=?4",
            params![
                identity.account_id,
                identity.agent_id,
                identity.memory_space_id,
                session_id
            ],
            |_| Ok(()),
        )
        .optional()
        .map_err(db_error)?
        .is_some();
    if exists {
        return Ok(false);
    }
    let source_event_sequence = value
        .get("source_event_sequence")
        .and_then(Value::as_i64)
        .filter(|sequence| *sequence >= 0)
        .unwrap_or(0);
    let max_sequence: i64 = tx
        .query_row(
            "SELECT COALESCE(MAX(event_sequence),0) FROM session_events
             WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND session_id=?4",
            params![
                identity.account_id,
                identity.agent_id,
                identity.memory_space_id,
                session_id
            ],
            |row| row.get(0),
        )
        .map_err(db_error)?;
    // A summary may not point past the newest event of its session; when the
    // export dropped events (or the event import skipped them) the pointer is
    // clamped so the row stays consistent with what actually exists.
    let source_event_sequence = source_event_sequence.min(max_sequence);
    let created_at = import_timestamp(value.get("created_at").unwrap_or(&Value::Null), now);
    let blob = json_bytes(summary)?;
    if blob.len() > memory_core::MAX_TEXT_BYTES {
        return Err(ServiceError::invalid("summary is too large"));
    }
    tx.execute(
        "INSERT INTO session_summaries(
            session_id,account_id,agent_id,memory_space_id,revision,parent_revision,
            summary_payload,source_event_sequence,sync_policy,producer,model_ref,
            policy_version,created_at,hlc,is_current,expires_at
         ) VALUES(?1,?2,?3,?4,?11,NULL,?5,?6,'local_only',?7,NULL,1,?8,?9,1,?10)",
        params![
            session_id,
            identity.account_id,
            identity.agent_id,
            identity.memory_space_id,
            blob,
            source_event_sequence,
            value
                .get("producer")
                .and_then(Value::as_str)
                .unwrap_or("agent"),
            created_at,
            hlc,
            created_at.saturating_add(retention_days.saturating_mul(24 * 60 * 60 * 1000)),
            value.get("revision").and_then(Value::as_i64).filter(|n| *n > 0).unwrap_or(1)
        ],
    )
    .map_err(db_error)?;
    Ok(true)
}

/// Insert one L3 memory and its first revision. Returns the new id so the
/// caller can rebuild the semantic vector after commit; `Ok(None)` means the
/// record was skipped because it already exists.
fn import_memory(
    tx: &rusqlite::Transaction<'_>,
    identity: &MemoryIdentity,
    value: &Value,
    hlc: &str,
    now: i64,
) -> ServiceResult<Option<String>> {
    let memory_id = value
        .get("memory_id")
        .and_then(Value::as_str)
        .filter(|value| memory_core::valid_identifier(value))
        .ok_or_else(|| ServiceError::invalid("invalid memory_id"))?;
    let kind = value
        .get("kind")
        .and_then(Value::as_str)
        .filter(|value| memory_core::allowed_memory_kind(value))
        .ok_or_else(|| ServiceError::invalid("invalid memory kind"))?;
    let text = value
        .get("text")
        .and_then(Value::as_str)
        .ok_or_else(|| ServiceError::invalid("memory text is missing"))?;
    super::long_term::validate_memory_text(text)?;
    let summary = value.get("summary").and_then(Value::as_str);
    let tags = value
        .get("tags")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    super::long_term::validate_tags(&tags)?;
    let exists: bool = tx
        .query_row(
            "SELECT 1 FROM long_term_memories WHERE memory_id=?1",
            [memory_id],
            |_| Ok(()),
        )
        .optional()
        .map_err(db_error)?
        .is_some();
    if exists {
        return Ok(None);
    }
    // Preserve the exported classification during restore. Import is a data
    // round-trip operation; normal write-time policy is applied when the
    // record is first created. Credential-like content is still forced to
    // `secret` so a crafted export cannot make credentials indexable.
    let mut sensitivity = match value.get("sensitivity").and_then(Value::as_str) {
        Some(value @ ("public" | "private" | "sensitive" | "secret")) => value,
        _ => "private",
    };
    if super::long_term::looks_like_credential(text) {
        sensitivity = "secret";
    }
    let source = value
        .get("source")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty() && value.len() <= 64)
        .unwrap_or("agent");
    let created_at = import_timestamp(value.get("created_at").unwrap_or(&Value::Null), now);
    let updated_at = import_timestamp(value.get("updated_at").unwrap_or(&Value::Null), created_at);
    let tags_json = serde_json::to_string(&tags)
        .map_err(|_| ServiceError::invalid("invalid tags"))?;
    let payload = super::long_term::memory_payload(
        memory_id,
        kind,
        text,
        summary,
        &tags,
        source,
        None,
        sensitivity,
        1,
        "local_only",
        None,
        &[],
    );
    let blob = json_bytes(&payload)?;
    let payload_hash = hash_bytes(&blob);
    tx.execute(
        "INSERT INTO long_term_memories(
            memory_id,account_id,agent_id,memory_space_id,kind,content,summary,tags_json,
            source,confidence,sensitivity,current_revision,sync_policy,expires_at,
            created_at,updated_at,hlc
         ) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,NULL,?10,1,'local_only',NULL,?11,?12,?13)",
        params![
            memory_id,
            identity.account_id,
            identity.agent_id,
            identity.memory_space_id,
            kind,
            text,
            summary,
            tags_json,
            source,
            sensitivity,
            created_at,
            updated_at,
            hlc
        ],
    )
    .map_err(db_error)?;
    tx.execute(
        "INSERT INTO memory_revisions(
            memory_id,revision,parent_revision,payload,payload_hash,author_device_id,
            author_agent_id,created_at,hlc
         ) VALUES(?1,1,NULL,?2,?3,?4,?5,?6,?7)",
        params![
            memory_id,
            blob,
            payload_hash,
            identity.device_id,
            identity.agent_id,
            created_at,
            hlc
        ],
    )
    .map_err(db_error)?;
    super::long_term::index_memory(
        tx,
        memory_id,
        text,
        summary,
        &tags_json,
        sensitivity,
    )?;
    Ok(Some(memory_id.to_owned()))
}

fn validate_confirmed_operation(input: &ConfirmedOperation) -> ServiceResult<()> {
    if !input
        .confirm_token
        .as_deref()
        .is_some_and(|token| !token.is_empty())
    {
        return Err(ServiceError::invalid("confirm_token is required"));
    }
    if input.reason_code.is_empty() || input.reason_code.len() > 128 {
        return Err(ServiceError::invalid("invalid reason_code"));
    }
    Ok(())
}

fn collect_export_page(
    connection: &rusqlite::Connection,
    sql: &str,
    identity: &MemoryIdentity,
    snapshot_at: i64,
    cursor: Option<ExportPosition>,
    limit: i64,
) -> ServiceResult<ExportPage> {
    let mut statement = connection.prepare(sql).map_err(db_error)?;
    // Ask for one extra row so `has_more` is answered without a COUNT scan.
    let query_limit = limit.saturating_add(1);
    let rows = statement
        .query_map(
            params![
                identity.account_id,
                identity.agent_id,
                identity.memory_space_id,
                snapshot_at,
                cursor.map(|position| position.at),
                cursor.map(|position| position.rowid).unwrap_or(i64::MAX),
                query_limit
            ],
            |row| {
                let encoded: String = row.get(0)?;
                Ok((
                    serde_json::from_str::<Value>(&encoded).unwrap_or(Value::Null),
                    ExportPosition {
                        at: row.get(1)?,
                        rowid: row.get(2)?,
                    },
                ))
            },
        )
        .map_err(db_error)?;
    let mut rows = rows
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_error)?;
    let next = if rows.len() as i64 > limit {
        rows.truncate(limit as usize);
        rows.last().map(|(_, position)| *position)
    } else {
        None
    };
    Ok(ExportPage {
        rows: Value::Array(rows.into_iter().map(|(value, _)| value).collect()),
        next,
    })
}

fn max_rowid(
    connection: &rusqlite::Connection,
    table: &str,
    identity: &MemoryIdentity,
) -> i64 {
    // Table names are internal constants selected by the caller, never input.
    connection
        .query_row(
            &format!(
                "SELECT COALESCE(MAX(rowid),0) FROM {table}
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3"
            ),
            params![
                identity.account_id,
                identity.agent_id,
                identity.memory_space_id
            ],
            |row| row.get::<_, i64>(0),
        )
        .unwrap_or(0)
}

fn collect_session_event_page(
    connection: &rusqlite::Connection,
    identity: &MemoryIdentity,
    snapshot_at: i64,
    cursor: Option<ExportPosition>,
    limit: i64,
) -> ServiceResult<ExportPage> {
    let mut statement = connection
        .prepare(
            "SELECT event_id,session_id,event_sequence,event_type,payload,payload_encoding,
                    created_at,expires_at,rowid
             FROM session_events
             WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3
               AND rowid<=?4
               AND (?5 IS NULL OR created_at<?5 OR (created_at=?5 AND rowid<?6))
             ORDER BY created_at DESC,rowid DESC LIMIT ?7",
        )
        .map_err(db_error)?;
    let query_limit = limit.saturating_add(1);
    let rows = statement
        .query_map(
            params![
                identity.account_id,
                identity.agent_id,
                identity.memory_space_id,
                snapshot_at,
                cursor.map(|position| position.at),
                cursor.map(|position| position.rowid).unwrap_or(i64::MAX),
                query_limit
            ],
            |row| {
                let blob: Vec<u8> = row.get(4)?;
                let encoding: String = row.get(5)?;
                let payload = decode_json_payload(&blob, &encoding)
                    .map_err(|error| rusqlite::Error::ToSqlConversionFailure(Box::new(error)))?;
                Ok((
                    json!({
                        "event_id":row.get::<_,String>(0)?,
                        "session_id":row.get::<_,String>(1)?,
                        "event_sequence":row.get::<_,i64>(2)?,
                        "event_type":row.get::<_,String>(3)?,
                        "payload":payload,
                        "created_at":row.get::<_,i64>(6)?,
                        "expires_at":row.get::<_,Option<i64>>(7)?
                    }),
                    ExportPosition {
                        at: row.get(6)?,
                        rowid: row.get(8)?,
                    },
                ))
            },
        )
        .map_err(db_error)?;
    let mut rows = rows
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_error)?;
    let next = if rows.len() as i64 > limit {
        rows.truncate(limit as usize);
        rows.last().map(|(_, position)| *position)
    } else {
        None
    };
    Ok(ExportPage {
        rows: Value::Array(rows.into_iter().map(|(value, _)| value).collect()),
        next,
    })
}

fn scoped_memory_ids(
    transaction: &rusqlite::Transaction<'_>,
    identity: &MemoryIdentity,
    extra_where: &str,
) -> ServiceResult<Vec<String>> {
    let sql = format!(
        "SELECT memory_id FROM long_term_memories
         WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND ({extra_where})"
    );
    let mut statement = transaction.prepare(&sql).map_err(db_error)?;
    let rows = statement
        .query_map(
            params![
                identity.account_id,
                identity.agent_id,
                identity.memory_space_id
            ],
            |row| row.get(0),
        )
        .map_err(db_error)?;
    rows.collect::<Result<Vec<_>, _>>().map_err(db_error)
}
