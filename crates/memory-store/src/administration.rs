use crate::product::{
    converged_backup_scopes, EmbeddingConfig, ProductConfigPatch, SummaryModelConfig, BACKUP_SCOPES,
};
use crate::{
    db_error, decode_json_payload, hash_bytes, hash_text, json_bytes, parse_params, ServiceResult,
    Store,
};
use memory_core::{now_ms, MemoryIdentity};
use memory_protocol::{RequestEnvelope, ServiceError};
use rusqlite::{params, OptionalExtension};
use serde::Deserialize;
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
    #[serde(default)]
    backup_enabled: Option<bool>,
    #[serde(default)]
    backup_frequency: Option<String>,
    #[serde(default)]
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
    #[serde(default)]
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
        let row: Option<(i64, Option<i64>, i64, i64, String, String, i64)> = connection
            .query_row(
                "SELECT memory_enabled,retention_days,audit_enabled,backup_enabled,
                        backup_frequency,backup_scopes_json,updated_at
                 FROM memory_settings
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3",
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id
                ],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                        row.get(5)?,
                        row.get(6)?,
                    ))
                },
            )
            .optional()
            .map_err(db_error)?;
        let (
            memory_enabled,
            retention_days,
            audit_enabled,
            backup_enabled,
            backup_frequency,
            backup_scopes_json,
            updated_at,
        ) = row.unwrap_or((
            product.memory_enabled as i64,
            (product.retention_days != 0).then_some(product.retention_days),
            product.audit_enabled as i64,
            product.cloud_backup_enabled as i64,
            product.backup_frequency.clone(),
            serde_json::to_string(&product.backup_scopes).unwrap_or_else(|_| "[]".to_owned()),
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
        object.insert("backup_enabled".to_owned(), json!(backup_enabled != 0));
        object.insert("backup_frequency".to_owned(), json!(backup_frequency));
        // Converge on read as well: rows written before 0.1.16 still carry the
        // retired `summaries` scope, and the settings view must not offer it.
        object.insert(
            "backup_scopes".to_owned(),
            json!(converged_backup_scopes(
                &serde_json::from_str::<Vec<String>>(&backup_scopes_json).unwrap_or_default()
            )),
        );
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
            && input.backup_enabled.is_none()
            && input.backup_frequency.is_none()
            && input.backup_scopes.is_none()
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
            && input.max_backup_bytes.is_none()
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
        if input
            .backup_frequency
            .as_deref()
            .is_some_and(|value| !matches!(value, "manual" | "daily" | "weekly" | "monthly"))
        {
            return Err(ServiceError::invalid(
                "backup_frequency must be manual, daily, weekly, or monthly",
            ));
        }
        if input.backup_enabled == Some(true)
            && (identity.account_id == "default" || identity.account_id.starts_with("local:"))
        {
            return Err(ServiceError::new(
                "Unauthenticated",
                "the local default account cannot enable cloud backup",
            ));
        }
        if input.backup_enabled == Some(true)
            && (self.backup_storage.is_none() || self.capsule_key.is_none())
        {
            return Err(ServiceError::new(
                "BackupUnavailable",
                "capsule key provider and backup storage adapter are not configured",
            ));
        }
        if let Some(scopes) = input.backup_scopes.as_deref() {
            validate_backup_scopes(scopes)?;
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
            cloud_backup_enabled: input.backup_enabled,
            backup_frequency: input.backup_frequency.clone(),
            backup_scopes: input.backup_scopes.clone(),
            preference_auto_apply_enabled: input.preference_auto_apply_enabled,
            development_habit_auto_apply_enabled: input.development_habit_auto_apply_enabled,
            recent_turn_limit: input.recent_turn_limit,
            recent_session_limit: input.recent_session_limit,
            summary_interval_turns: input.summary_interval_turns,
            summary_retention_days: input.summary_retention_days,
            summary_model: input.summary_model.clone(),
            embedding: input.embedding.clone(),
            max_database_bytes: input.max_database_bytes,
            max_backup_bytes: input.max_backup_bytes,
            min_free_bytes: input.min_free_bytes,
        };
        if product_patch.is_empty() {
            return Err(ServiceError::invalid("at least one setting is required"));
        }
        let product = self.update_product_config(&product_patch)?;
        let mut product_view =
            self.product_config_json(&identity.account_id, &identity.device_id)?;
        self.with_write(identity, request, "privacy.settings.update", "settings", move |tx, _, now, _, _| {
            let current: Option<(i64, Option<i64>, i64, i64, String, String)> = tx.query_row(
                "SELECT memory_enabled,retention_days,audit_enabled,backup_enabled,backup_frequency,backup_scopes_json FROM memory_settings
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3",
                params![identity.account_id,identity.agent_id,identity.memory_space_id],
                |row| Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?,row.get(4)?,row.get(5)?)),
            ).optional().map_err(db_error)?;
            let (old_memory, old_retention, old_audit, old_backup, old_frequency, old_scopes) = current.unwrap_or((
                product.memory_enabled as i64,
                (product.retention_days != 0).then_some(product.retention_days),
                product.audit_enabled as i64,
                product.cloud_backup_enabled as i64,
                product.backup_frequency.clone(),
                serde_json::to_string(&product.backup_scopes).unwrap_or_else(|_| "[]".to_owned()),
            ));
            let memory_enabled = input.memory_enabled.map(|value| value as i64).unwrap_or(old_memory);
            let retention_days = match input.retention_days {
                Some(0) => None,
                Some(days) => Some(days),
                None => old_retention,
            };
            let audit_enabled = input.audit_enabled.map(|value| value as i64).unwrap_or(old_audit);
            let backup_enabled = input.backup_enabled.map(|value| value as i64).unwrap_or(old_backup);
            let backup_frequency = input.backup_frequency.clone().unwrap_or(old_frequency);
            // A settings write always converges the persisted list, so a device
            // that still holds the retired `summaries` scope is cleaned up on the
            // first unrelated setting change (or on any explicit scope update).
            let backup_scopes_json = match input.backup_scopes.as_ref() {
                Some(value) => {
                    serde_json::to_string(value).unwrap_or_else(|_| "[]".to_owned())
                }
                None => serde_json::to_string(&converged_backup_scopes(
                    &serde_json::from_str::<Vec<String>>(&old_scopes).unwrap_or_default(),
                ))
                .unwrap_or_else(|_| "[]".to_owned()),
            };
            tx.execute(
                "INSERT INTO memory_settings(account_id,agent_id,memory_space_id,memory_enabled,retention_days,audit_enabled,backup_enabled,backup_frequency,backup_scopes_json,updated_at)
                 VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)
                 ON CONFLICT(account_id,agent_id,memory_space_id) DO UPDATE SET
                   memory_enabled=excluded.memory_enabled,retention_days=excluded.retention_days,
                   audit_enabled=excluded.audit_enabled,backup_enabled=excluded.backup_enabled,
                   backup_frequency=excluded.backup_frequency,backup_scopes_json=excluded.backup_scopes_json,
                   updated_at=excluded.updated_at",
                params![identity.account_id,identity.agent_id,identity.memory_space_id,memory_enabled,retention_days,audit_enabled,backup_enabled,backup_frequency,backup_scopes_json,now],
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
            object.insert("backup_enabled".to_owned(), json!(backup_enabled != 0));
            object.insert("backup_frequency".to_owned(), json!(backup_frequency));
            object.insert(
                "backup_scopes".to_owned(),
                serde_json::from_str::<Value>(&backup_scopes_json).unwrap_or_else(|_| json!([])),
            );
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
        let session_events =
            collect_session_event_rows(&connection, identity, input.limit_per_collection)?;
        let summaries = collect_json_rows(
            &connection,
            "SELECT json_object('session_id',session_id,'revision',revision,'summary',json(summary_payload),
                    'source_event_sequence',source_event_sequence,'producer',producer,'created_at',created_at)
             FROM session_summaries WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND is_current=1
             ORDER BY created_at DESC LIMIT ?4",
            identity,
            input.limit_per_collection,
        )?;
        let memories = collect_json_rows(
            &connection,
            "SELECT json_object('memory_id',memory_id,'kind',kind,'text',content,'summary',summary,
                    'tags',json(tags_json),'source',source,'sensitivity',sensitivity,
                    'revision',current_revision,'created_at',created_at,'updated_at',updated_at)
             FROM long_term_memories WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3
             ORDER BY updated_at DESC LIMIT ?4",
            identity,
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
        Ok(json!({
            "format":"bianbu-agent-memory.export.v1",
            "exported_at":now_ms(),
            "scope":{"account_id":identity.account_id,"agent_id":identity.agent_id,"memory_space_id":identity.memory_space_id},
            "settings":settings,
            "session_events":session_events,
            "session_summaries":summaries,
            "long_term_memories":memories,
            "audit":audit,
            "limit_per_collection":input.limit_per_collection
        }))
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

fn validate_backup_scopes(scopes: &[String]) -> ServiceResult<()> {
    if scopes.is_empty()
        || scopes.len() > BACKUP_SCOPES.len()
        || scopes
            .iter()
            .any(|scope| !BACKUP_SCOPES.contains(&scope.as_str()))
    {
        Err(ServiceError::invalid("invalid backup scopes"))
    } else {
        Ok(())
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

fn collect_json_rows(
    connection: &rusqlite::Connection,
    sql: &str,
    identity: &MemoryIdentity,
    limit: i64,
) -> ServiceResult<Value> {
    let mut statement = connection.prepare(sql).map_err(db_error)?;
    let rows = statement
        .query_map(
            params![
                identity.account_id,
                identity.agent_id,
                identity.memory_space_id,
                limit
            ],
            |row| {
                let encoded: String = row.get(0)?;
                Ok(serde_json::from_str::<Value>(&encoded).unwrap_or(Value::Null))
            },
        )
        .map_err(db_error)?;
    Ok(Value::Array(
        rows.collect::<Result<Vec<_>, _>>().map_err(db_error)?,
    ))
}

fn collect_session_event_rows(
    connection: &rusqlite::Connection,
    identity: &MemoryIdentity,
    limit: i64,
) -> ServiceResult<Value> {
    let mut statement = connection
        .prepare(
            "SELECT event_id,session_id,event_sequence,event_type,payload,payload_encoding,
                    created_at,expires_at
             FROM session_events WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3
             ORDER BY created_at DESC LIMIT ?4",
        )
        .map_err(db_error)?;
    let rows = statement
        .query_map(
            params![
                identity.account_id,
                identity.agent_id,
                identity.memory_space_id,
                limit
            ],
            |row| {
                let blob: Vec<u8> = row.get(4)?;
                let encoding: String = row.get(5)?;
                let payload = decode_json_payload(&blob, &encoding)
                    .map_err(|error| rusqlite::Error::ToSqlConversionFailure(Box::new(error)))?;
                Ok(json!({
                    "event_id":row.get::<_,String>(0)?,
                    "session_id":row.get::<_,String>(1)?,
                    "event_sequence":row.get::<_,i64>(2)?,
                    "event_type":row.get::<_,String>(3)?,
                    "payload":payload,
                    "created_at":row.get::<_,i64>(6)?,
                    "expires_at":row.get::<_,Option<i64>>(7)?
                }))
            },
        )
        .map_err(db_error)?;
    Ok(Value::Array(
        rows.collect::<Result<Vec<_>, _>>().map_err(db_error)?,
    ))
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
