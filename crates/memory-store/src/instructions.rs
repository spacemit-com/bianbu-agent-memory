use crate::{db_error, hash_bytes, json_bytes, parse_params, ServiceResult, Store};
use memory_core::{allowed_instruction_scope, allowed_sync_policy, MemoryIdentity, MAX_TEXT_BYTES};
use memory_protocol::{RequestEnvelope, ServiceError};
use rusqlite::{params, OptionalExtension};
use serde::Deserialize;
use serde_json::{json, Value};
use uuid::Uuid;

#[derive(Deserialize)]
struct PutInstruction {
    text: String,
    scope: String,
    #[serde(default)]
    rationale: Option<String>,
    #[serde(default)]
    applicability: Option<String>,
    #[serde(default = "default_priority")]
    priority: i64,
    #[serde(default = "default_sync")]
    sync_policy: String,
    #[serde(default = "default_sensitivity")]
    sensitivity: String,
    #[serde(default)]
    expires_at: Option<i64>,
}

fn default_priority() -> i64 {
    100
}
fn default_sync() -> String {
    "account_agent".to_owned()
}
fn default_sensitivity() -> String {
    "private".to_owned()
}

#[derive(Deserialize)]
struct ListInstructions {
    #[serde(default = "default_true")]
    effective_only: bool,
    #[serde(default)]
    workspace_id: Option<String>,
}

fn default_true() -> bool {
    true
}

#[derive(Deserialize)]
struct ReviseInstruction {
    instruction_id: String,
    text: String,
    expected_revision: i64,
    #[serde(default)]
    rationale: Option<String>,
    #[serde(default)]
    applicability: Option<String>,
}

impl Store {
    pub(crate) fn put_instruction(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: PutInstruction = parse_params(request)?;
        validate_text(&input.text)?;
        if !allowed_instruction_scope(&input.scope) || input.scope == "managed" {
            return Err(ServiceError::permission_denied());
        }
        if !allowed_sync_policy(&input.sync_policy) || input.sync_policy == "full" {
            return Err(ServiceError::invalid("invalid instruction sync_policy"));
        }
        if !(0..=1000).contains(&input.priority) {
            return Err(ServiceError::invalid("priority must be between 0 and 1000"));
        }
        self.with_write(
            identity,
            request,
            "instructions.put",
            "instruction",
            |tx, hlc, now, _, _| {
                let expires_at = Store::effective_expiry(tx, identity, input.expires_at, now)?;
                let instruction_id = Uuid::new_v4().to_string();
                let payload = json!({
                    "instruction_id":instruction_id,
                    "scope":input.scope,
                    "text":input.text,
                    "rationale":input.rationale,
                    "applicability":input.applicability,
                    "priority":input.priority,
                    "revision":1,
                    "sync_policy":input.sync_policy,
                    "sensitivity":input.sensitivity,
                    "source":"agent",
                    "expires_at":expires_at
                });
                let blob = json_bytes(&payload)?;
                let payload_hash = hash_bytes(&blob);
                tx.execute(
                    "INSERT INTO instructions(
                    instruction_id,account_id,agent_id,memory_space_id,scope,text,rationale,
                    applicability,priority,source,current_revision,sync_policy,sensitivity,
                    expires_at,created_at,updated_at,hlc
                 ) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,'agent',1,?10,?11,?12,?13,?13,?14)",
                    params![
                        instruction_id,
                        identity.account_id,
                        identity.agent_id,
                        identity.memory_space_id,
                        input.scope,
                        input.text,
                        input.rationale,
                        input.applicability,
                        input.priority,
                        input.sync_policy,
                        input.sensitivity,
                        expires_at,
                        now,
                        hlc
                    ],
                )
                .map_err(db_error)?;
                tx.execute(
                    "INSERT INTO instruction_revisions(
                    instruction_id,revision,parent_revision,payload,payload_hash,author_device_id,
                    author_agent_id,created_at,hlc
                 ) VALUES(?1,1,NULL,?2,?3,?4,?5,?6,?7)",
                    params![
                        instruction_id,
                        blob,
                        payload_hash,
                        identity.device_id,
                        identity.agent_id,
                        now,
                        hlc
                    ],
                )
                .map_err(db_error)?;
                Store::enqueue_outbox(
                    tx,
                    identity,
                    "instruction",
                    &instruction_id,
                    "put",
                    1,
                    hlc,
                    &payload_hash,
                    &input.sync_policy,
                    now,
                )?;
                Ok((payload, Some(instruction_id)))
            },
        )
    }

    pub(crate) fn list_instructions(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: ListInstructions = parse_params(request)?;
        if let Some(workspace_id) = input.workspace_id.as_deref() {
            if workspace_id.len() > 256 {
                return Err(ServiceError::invalid("workspace_id is too long"));
            }
        }
        let connection = self.connection()?;
        let mut statement = connection
            .prepare(
                "SELECT instruction_id,scope,text,rationale,applicability,priority,current_revision,
                        sync_policy,source,sensitivity,expires_at,updated_at
                 FROM instructions
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3
                   AND (?4=0 OR expires_at IS NULL OR expires_at>?5)
                 ORDER BY priority DESC, updated_at DESC",
            )
            .map_err(db_error)?;
        let rows = statement
            .query_map(
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id,
                    input.effective_only as i64,
                    memory_core::now_ms()
                ],
                |row| {
                    Ok(json!({
                        "instruction_id":row.get::<_,String>(0)?,
                        "scope":row.get::<_,String>(1)?,
                        "text":row.get::<_,String>(2)?,
                        "rationale":row.get::<_,Option<String>>(3)?,
                        "applicability":row.get::<_,Option<String>>(4)?,
                        "priority":row.get::<_,i64>(5)?,
                        "revision":row.get::<_,i64>(6)?,
                        "sync_policy":row.get::<_,String>(7)?,
                        "source":row.get::<_,String>(8)?,
                        "sensitivity":row.get::<_,String>(9)?,
                        "expires_at":row.get::<_,Option<i64>>(10)?,
                        "updated_at":row.get::<_,i64>(11)?
                    }))
                },
            )
            .map_err(db_error)?;
        let values = rows.collect::<Result<Vec<_>, _>>().map_err(db_error)?;
        Ok(Value::Array(values))
    }

    pub(crate) fn revise_instruction(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: ReviseInstruction = parse_params(request)?;
        validate_text(&input.text)?;
        if input.expected_revision < 1 {
            return Err(ServiceError::invalid("expected_revision must be positive"));
        }
        self.with_write(identity, request, "instructions.revise", "instruction", |tx, hlc, now, _, _| {
            let current: Option<(String, i64, String, i64, String, String, Option<i64>, Option<String>, Option<String>)> = tx
                .query_row(
                    "SELECT scope,current_revision,sync_policy,priority,sensitivity,source,expires_at,rationale,applicability
                     FROM instructions
                     WHERE instruction_id=?1 AND account_id=?2 AND agent_id=?3 AND memory_space_id=?4",
                    params![input.instruction_id, identity.account_id, identity.agent_id, identity.memory_space_id],
                    |row| Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?,row.get(4)?,row.get(5)?,row.get(6)?,row.get(7)?,row.get(8)?)),
                )
                .optional()
                .map_err(db_error)?;
            let (scope, current_revision, sync_policy, priority, sensitivity, source, expires_at, old_rationale, old_applicability) =
                current.ok_or_else(ServiceError::not_found)?;
            if scope == "managed" {
                return Err(ServiceError::permission_denied());
            }
            if current_revision != input.expected_revision {
                return Err(ServiceError::conflict(format!(
                    "expected revision {}, current revision is {}",
                    input.expected_revision, current_revision
                )));
            }
            let revision = current_revision + 1;
            let rationale = input.rationale.clone().or(old_rationale);
            let applicability = input.applicability.clone().or(old_applicability);
            let payload = json!({
                "instruction_id":input.instruction_id,
                "scope":scope,
                "text":input.text,
                "rationale":rationale,
                "applicability":applicability,
                "priority":priority,
                "revision":revision,
                "sync_policy":sync_policy,
                "sensitivity":sensitivity,
                "source":source,
                "expires_at":expires_at
            });
            let blob = json_bytes(&payload)?;
            let payload_hash = hash_bytes(&blob);
            let affected = tx.execute(
                "UPDATE instructions SET text=?1,rationale=?2,applicability=?3,current_revision=?4,updated_at=?5,hlc=?6
                 WHERE instruction_id=?7 AND account_id=?8 AND agent_id=?9 AND current_revision=?10",
                params![input.text,rationale,applicability,revision,now,hlc,input.instruction_id,identity.account_id,identity.agent_id,input.expected_revision],
            ).map_err(db_error)?;
            if affected != 1 {
                return Err(ServiceError::conflict("instruction changed concurrently"));
            }
            tx.execute(
                "INSERT INTO instruction_revisions(
                    instruction_id,revision,parent_revision,payload,payload_hash,author_device_id,
                    author_agent_id,created_at,hlc
                 ) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9)",
                params![input.instruction_id,revision,current_revision,blob,payload_hash,identity.device_id,identity.agent_id,now,hlc],
            ).map_err(db_error)?;
            Store::enqueue_outbox(
                tx, identity, "instruction", &input.instruction_id, "revise", revision,
                hlc, &payload_hash, &sync_policy, now,
            )?;
            Ok((payload, Some(input.instruction_id.clone())))
        })
    }
}

fn validate_text(text: &str) -> ServiceResult<()> {
    if text.trim().is_empty() || text.len() > MAX_TEXT_BYTES {
        Err(ServiceError::invalid(
            "instruction text is empty or too large",
        ))
    } else {
        Ok(())
    }
}
