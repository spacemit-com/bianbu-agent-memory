use crate::{db_error, decode_json_payload, parse_params, ServiceResult, Store};
use memory_core::{valid_identifier, MemoryIdentity};
use memory_protocol::{RequestEnvelope, ServiceError};
use rusqlite::{
    params, params_from_iter, types::Value as SqlValue, OptionalExtension, TransactionBehavior,
};
use serde::Deserialize;
use serde_json::{json, Value};
use uuid::Uuid;

const MAX_PROVIDER_RESULT_BYTES: usize = 192 * 1024;

#[derive(Deserialize)]
struct EnqueueTurn {
    provider: String,
    run_id: String,
    session_id: String,
    user_event_id: String,
    assistant_event_id: String,
}

#[derive(Deserialize)]
struct ClaimJob {
    provider: String,
    worker_id: String,
    #[serde(default = "default_lease_ms")]
    lease_ms: i64,
}

fn default_lease_ms() -> i64 {
    120_000
}

#[derive(Deserialize)]
struct SaveProviderResult {
    job_id: String,
    worker_id: String,
    provider_result: Value,
}

#[derive(Clone, Deserialize)]
struct MappingInput {
    provider_memory_id: String,
    memory_id: String,
    canonical_revision: i64,
    #[serde(default = "default_mapping_state")]
    state: String,
    #[serde(default)]
    last_score: Option<f64>,
}

fn default_mapping_state() -> String {
    "active".to_owned()
}

#[derive(Deserialize)]
struct CompleteJob {
    job_id: String,
    worker_id: String,
    #[serde(default)]
    mappings: Vec<MappingInput>,
}

#[derive(Deserialize)]
struct FailJob {
    job_id: String,
    worker_id: String,
    error: String,
    #[serde(default = "default_retry_after_ms")]
    retry_after_ms: i64,
    #[serde(default)]
    terminal: bool,
}

fn default_retry_after_ms() -> i64 {
    5_000
}

#[derive(Deserialize)]
struct UpsertMapping {
    provider: String,
    mapping: MappingInput,
}

#[derive(Deserialize)]
struct ResolveMappings {
    provider: String,
    provider_memory_ids: Vec<String>,
}

#[derive(Deserialize)]
struct ProjectionStatus {
    #[serde(default)]
    provider: Option<String>,
}

#[derive(Deserialize)]
struct ListDeadJobs {
    provider: String,
    #[serde(default = "default_dead_job_limit")]
    limit: i64,
}

fn default_dead_job_limit() -> i64 {
    50
}

#[derive(Deserialize)]
struct RequeueDeadJob {
    provider: String,
    job_id: String,
}

impl Store {
    pub(crate) fn enqueue_projection_turn(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: EnqueueTurn = parse_params(request)?;
        validate_identifier(&input.provider, "provider")?;
        validate_identifier(&input.run_id, "run_id")?;
        validate_identifier(&input.session_id, "session_id")?;
        validate_identifier(&input.user_event_id, "user_event_id")?;
        validate_identifier(&input.assistant_event_id, "assistant_event_id")?;
        self.with_write(
            identity,
            request,
            "projection.enqueue_turn",
            "semantic_projection_job",
            |tx, _, now, _, _| {
                let event_record = |event_id: &str| -> ServiceResult<Option<(String, Vec<u8>)>> {
                    tx.query_row(
                        "SELECT event_type,payload,payload_encoding FROM session_events
                         WHERE event_id=?1 AND account_id=?2 AND agent_id=?3
                           AND memory_space_id=?4 AND session_id=?5",
                        params![
                            event_id,
                            identity.account_id,
                            identity.agent_id,
                            identity.memory_space_id,
                            input.session_id
                        ],
                        |row| {
                            let blob: Vec<u8> = row.get(1)?;
                            let encoding: String = row.get(2)?;
                            let payload =
                                decode_json_payload(&blob, &encoding).map_err(|error| {
                                    rusqlite::Error::ToSqlConversionFailure(Box::new(error))
                                })?;
                            let encoded = serde_json::to_vec(&payload).map_err(|error| {
                                rusqlite::Error::ToSqlConversionFailure(Box::new(error))
                            })?;
                            Ok((row.get(0)?, encoded))
                        },
                    )
                    .optional()
                    .map_err(db_error)
                };
                let Some((user_type, user_blob)) = event_record(&input.user_event_id)? else {
                    return Err(ServiceError::invalid(
                        "projection user event does not exist",
                    ));
                };
                let Some((assistant_type, assistant_blob)) =
                    event_record(&input.assistant_event_id)?
                else {
                    return Err(ServiceError::invalid(
                        "projection assistant event does not exist",
                    ));
                };
                let user_payload: Value = serde_json::from_slice(&user_blob).map_err(|_| {
                    ServiceError::invalid("projection user event payload is invalid")
                })?;
                let assistant_payload: Value =
                    serde_json::from_slice(&assistant_blob).map_err(|_| {
                        ServiceError::invalid("projection assistant event payload is invalid")
                    })?;
                let user_run_id = user_payload
                    .get("turn_id")
                    .or_else(|| user_payload.get("run_id"))
                    .and_then(Value::as_str);
                let assistant_run_id = assistant_payload
                    .get("turn_id")
                    .or_else(|| assistant_payload.get("run_id"))
                    .and_then(Value::as_str);
                let eligible = |payload: &Value| {
                    payload
                        .get("text")
                        .and_then(Value::as_str)
                        .map(str::trim)
                        .map(|text| !text.is_empty())
                        .unwrap_or(false)
                        && !matches!(
                            payload
                                .get("status")
                                .and_then(Value::as_str)
                                .unwrap_or_default()
                                .to_ascii_lowercase()
                                .as_str(),
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
                        )
                };
                if user_type != "user_message"
                    || assistant_type != "assistant_message"
                    || user_run_id != Some(input.run_id.as_str())
                    || assistant_run_id != Some(input.run_id.as_str())
                    || !eligible(&user_payload)
                    || !eligible(&assistant_payload)
                {
                    return Err(ServiceError::invalid(
                        "projection events do not form an eligible completed turn",
                    ));
                }
                let job_id = Uuid::new_v4().to_string();
                tx.execute(
                    "INSERT INTO semantic_projection_jobs(
                        job_id,account_id,agent_id,memory_space_id,provider,run_id,session_id,
                        user_event_id,assistant_event_id,state,created_at,updated_at
                     ) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,'pending',?10,?10)",
                    params![
                        job_id,
                        identity.account_id,
                        identity.agent_id,
                        identity.memory_space_id,
                        input.provider,
                        input.run_id,
                        input.session_id,
                        input.user_event_id,
                        input.assistant_event_id,
                        now
                    ],
                )
                .map_err(db_error)?;
                Ok((
                    json!({"job_id":job_id,"state":"pending","provider":input.provider}),
                    Some(job_id),
                ))
            },
        )
    }

    pub(crate) fn claim_projection_job(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: ClaimJob = parse_params(request)?;
        validate_identifier(&input.provider, "provider")?;
        validate_identifier(&input.worker_id, "worker_id")?;
        if !(1_000..=600_000).contains(&input.lease_ms) {
            return Err(ServiceError::invalid("lease_ms is out of range"));
        }
        let now = memory_core::now_ms();
        let mut connection = self.connection()?;
        let transaction = connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(db_error)?;
        let job: Option<(
            String,
            String,
            String,
            String,
            String,
            String,
            Option<String>,
            i64,
        )> = transaction
            .query_row(
                "SELECT job_id,run_id,session_id,user_event_id,assistant_event_id,state,
                            provider_result_json,attempt_count
                     FROM semantic_projection_jobs
                     WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND provider=?4
                       AND (
                         (state IN ('pending','retry') AND COALESCE(next_retry_at,0)<=?5)
                         OR (state='leased' AND COALESCE(lease_expires_at,0)<=?5)
                       )
                     ORDER BY CASE WHEN provider_result_json IS NOT NULL THEN 0 ELSE 1 END,
                              created_at
                     LIMIT 1",
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id,
                    input.provider,
                    now
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
                        row.get(7)?,
                    ))
                },
            )
            .optional()
            .map_err(db_error)?;
        let Some((
            job_id,
            run_id,
            session_id,
            user_event_id,
            assistant_event_id,
            previous_state,
            provider_result,
            attempt_count,
        )) = job
        else {
            transaction.commit().map_err(db_error)?;
            return Ok(Value::Null);
        };
        let lease_expires_at = now.saturating_add(input.lease_ms);
        transaction
            .execute(
                "UPDATE semantic_projection_jobs
                 SET state='leased',lease_owner=?1,lease_expires_at=?2,
                     attempt_count=attempt_count+1,updated_at=?3
                 WHERE job_id=?4",
                params![input.worker_id, lease_expires_at, now, job_id],
            )
            .map_err(db_error)?;
        let event_payload = |event_id: &str| -> ServiceResult<Value> {
            let payload: Value = transaction
                .query_row(
                    "SELECT payload,payload_encoding FROM session_events
                     WHERE event_id=?1 AND account_id=?2 AND agent_id=?3 AND memory_space_id=?4",
                    params![
                        event_id,
                        identity.account_id,
                        identity.agent_id,
                        identity.memory_space_id
                    ],
                    |row| {
                        let blob: Vec<u8> = row.get(0)?;
                        let encoding: String = row.get(1)?;
                        decode_json_payload(&blob, &encoding).map_err(|error| {
                            rusqlite::Error::ToSqlConversionFailure(Box::new(error))
                        })
                    },
                )
                .map_err(db_error)?;
            Ok(payload)
        };
        let user_payload = event_payload(&user_event_id)?;
        let assistant_payload = event_payload(&assistant_event_id)?;
        transaction.commit().map_err(db_error)?;
        Ok(json!({
            "job_id":job_id,
            "provider":input.provider,
            "run_id":run_id,
            "session_id":session_id,
            "user_event_id":user_event_id,
            "assistant_event_id":assistant_event_id,
            "user_text":user_payload.get("text").and_then(Value::as_str).unwrap_or_default(),
            "assistant_text":assistant_payload.get("text").and_then(Value::as_str).unwrap_or_default(),
            "provider_result":provider_result.and_then(|value| serde_json::from_str::<Value>(&value).ok()),
            "previous_state":previous_state,
            "attempt_count":attempt_count+1,
            "lease_expires_at":lease_expires_at
        }))
    }

    pub(crate) fn save_projection_provider_result(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: SaveProviderResult = parse_params(request)?;
        validate_identifier(&input.job_id, "job_id")?;
        validate_identifier(&input.worker_id, "worker_id")?;
        let encoded = serde_json::to_string(&input.provider_result)
            .map_err(|_| ServiceError::invalid("provider_result is invalid"))?;
        if encoded.len() > MAX_PROVIDER_RESULT_BYTES {
            return Err(ServiceError::invalid("provider_result is too large"));
        }
        self.with_write(
            identity,
            request,
            "projection.save_result",
            "semantic_projection_job",
            |tx, _, now, _, _| {
                let affected = tx
                    .execute(
                        "UPDATE semantic_projection_jobs
                         SET provider_result_json=?1,last_error=NULL,updated_at=?2
                         WHERE job_id=?3 AND account_id=?4 AND agent_id=?5
                           AND memory_space_id=?6 AND state='leased' AND lease_owner=?7",
                        params![
                            encoded,
                            now,
                            input.job_id,
                            identity.account_id,
                            identity.agent_id,
                            identity.memory_space_id,
                            input.worker_id
                        ],
                    )
                    .map_err(db_error)?;
                if affected != 1 {
                    return Err(ServiceError::conflict("projection lease is not owned"));
                }
                Ok((
                    json!({"job_id":input.job_id,"state":"leased","provider_result_saved":true}),
                    Some(input.job_id.clone()),
                ))
            },
        )
    }

    pub(crate) fn complete_projection_job(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: CompleteJob = parse_params(request)?;
        validate_identifier(&input.job_id, "job_id")?;
        validate_identifier(&input.worker_id, "worker_id")?;
        if input.mappings.len() > 64 {
            return Err(ServiceError::invalid("too many projection mappings"));
        }
        self.with_write(
            identity,
            request,
            "projection.complete",
            "semantic_projection_job",
            |tx, _, now, _, _| {
                let provider: Option<String> = tx
                    .query_row(
                        "SELECT provider FROM semantic_projection_jobs
                         WHERE job_id=?1 AND account_id=?2 AND agent_id=?3 AND memory_space_id=?4
                           AND state='leased' AND lease_owner=?5",
                        params![
                            input.job_id,
                            identity.account_id,
                            identity.agent_id,
                            identity.memory_space_id,
                            input.worker_id
                        ],
                        |row| row.get(0),
                    )
                    .optional()
                    .map_err(db_error)?;
                let provider = provider
                    .ok_or_else(|| ServiceError::conflict("projection lease is not owned"))?;
                for mapping in &input.mappings {
                    validate_mapping(tx, identity, mapping)?;
                    upsert_mapping(tx, identity, &provider, mapping, now)?;
                }
                tx.execute(
                    "UPDATE semantic_projection_jobs
                     SET state='completed',lease_owner=NULL,lease_expires_at=NULL,next_retry_at=NULL,
                         last_error=NULL,updated_at=?1
                     WHERE job_id=?2",
                    params![now, input.job_id],
                )
                .map_err(db_error)?;
                Ok((json!({"job_id":input.job_id,"state":"completed","mapping_count":input.mappings.len()}), Some(input.job_id.clone())))
            },
        )
    }

    pub(crate) fn fail_projection_job(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: FailJob = parse_params(request)?;
        validate_identifier(&input.job_id, "job_id")?;
        validate_identifier(&input.worker_id, "worker_id")?;
        if input.error.len() > 2_000 || !(1_000..=86_400_000).contains(&input.retry_after_ms) {
            return Err(ServiceError::invalid(
                "projection failure payload is out of range",
            ));
        }
        self.with_write(
            identity,
            request,
            "projection.fail",
            "semantic_projection_job",
            |tx, _, now, _, _| {
                let state = if input.terminal { "dead" } else { "retry" };
                let next_retry_at = if input.terminal {
                    None
                } else {
                    Some(now.saturating_add(input.retry_after_ms))
                };
                let affected = tx
                    .execute(
                        "UPDATE semantic_projection_jobs
                     SET state=?1,lease_owner=NULL,lease_expires_at=NULL,next_retry_at=?2,
                         last_error=?3,updated_at=?4
                     WHERE job_id=?5 AND account_id=?6 AND agent_id=?7 AND memory_space_id=?8
                       AND state='leased' AND lease_owner=?9",
                        params![
                            state,
                            next_retry_at,
                            input.error,
                            now,
                            input.job_id,
                            identity.account_id,
                            identity.agent_id,
                            identity.memory_space_id,
                            input.worker_id
                        ],
                    )
                    .map_err(db_error)?;
                if affected != 1 {
                    return Err(ServiceError::conflict("projection lease is not owned"));
                }
                Ok((
                    json!({"job_id":input.job_id,"state":state,"next_retry_at":next_retry_at}),
                    Some(input.job_id.clone()),
                ))
            },
        )
    }

    pub(crate) fn upsert_projection_mapping(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: UpsertMapping = parse_params(request)?;
        validate_identifier(&input.provider, "provider")?;
        self.with_write(
            identity,
            request,
            "projection.upsert_mapping",
            "semantic_projection_mapping",
            |tx, _, now, _, _| {
                validate_mapping(tx, identity, &input.mapping)?;
                upsert_mapping(tx, identity, &input.provider, &input.mapping, now)?;
                Ok((json!({"provider":input.provider,"provider_memory_id":input.mapping.provider_memory_id,"memory_id":input.mapping.memory_id,"state":input.mapping.state}), Some(input.mapping.memory_id.clone())))
            },
        )
    }

    pub(crate) fn resolve_projection_mappings(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: ResolveMappings = parse_params(request)?;
        validate_identifier(&input.provider, "provider")?;
        if input.provider_memory_ids.len() > 100 {
            return Err(ServiceError::invalid(
                "at most 100 provider memory ids are allowed",
            ));
        }
        for value in &input.provider_memory_ids {
            validate_identifier(value, "provider_memory_id")?;
        }
        if input.provider_memory_ids.is_empty() {
            return Ok(json!([]));
        }
        let connection = self.connection()?;
        let placeholders = (1..=input.provider_memory_ids.len())
            .map(|index| format!("?{}", index + 4))
            .collect::<Vec<_>>()
            .join(",");
        let sql = format!(
            "SELECT provider_memory_id,memory_id,canonical_revision,state,last_score,updated_at
             FROM semantic_projection_mappings
             WHERE provider=?1 AND account_id=?2 AND agent_id=?3 AND memory_space_id=?4
               AND provider_memory_id IN ({placeholders})"
        );
        let mut values = vec![
            SqlValue::Text(input.provider),
            SqlValue::Text(identity.account_id.clone()),
            SqlValue::Text(identity.agent_id.clone()),
            SqlValue::Text(identity.memory_space_id.clone()),
        ];
        values.extend(input.provider_memory_ids.into_iter().map(SqlValue::Text));
        let mut statement = connection.prepare(&sql).map_err(db_error)?;
        let rows = statement
            .query_map(params_from_iter(values), |row| {
                Ok(json!({
                    "provider_memory_id":row.get::<_,String>(0)?,
                    "memory_id":row.get::<_,String>(1)?,
                    "canonical_revision":row.get::<_,i64>(2)?,
                    "state":row.get::<_,String>(3)?,
                    "last_score":row.get::<_,Option<f64>>(4)?,
                    "updated_at":row.get::<_,i64>(5)?
                }))
            })
            .map_err(db_error)?;
        Ok(Value::Array(
            rows.collect::<Result<Vec<_>, _>>().map_err(db_error)?,
        ))
    }

    pub(crate) fn projection_status(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: ProjectionStatus = parse_params(request)?;
        if let Some(provider) = input.provider.as_deref() {
            validate_identifier(provider, "provider")?;
        }
        let connection = self.connection()?;
        let provider = input.provider.unwrap_or_default();
        let count = |state: &str| -> ServiceResult<i64> {
            connection
                .query_row(
                    "SELECT COUNT(*) FROM semantic_projection_jobs
                     WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3
                       AND (?4='' OR provider=?4) AND state=?5",
                    params![
                        identity.account_id,
                        identity.agent_id,
                        identity.memory_space_id,
                        provider,
                        state
                    ],
                    |row| row.get(0),
                )
                .map_err(db_error)
        };
        let mappings: i64 = connection
            .query_row(
                "SELECT COUNT(*) FROM semantic_projection_mappings
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3
                   AND (?4='' OR provider=?4) AND state='active'",
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id,
                    provider
                ],
                |row| row.get(0),
            )
            .map_err(db_error)?;
        Ok(json!({
            "provider":if provider.is_empty() {Value::Null} else {Value::String(provider.clone())},
            "pending":count("pending")?,"leased":count("leased")?,"retry":count("retry")?,
            "completed":count("completed")?,"dead":count("dead")?,"active_mappings":mappings
        }))
    }

    pub(crate) fn list_dead_projection_jobs(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: ListDeadJobs = parse_params(request)?;
        validate_identifier(&input.provider, "provider")?;
        if !(1..=100).contains(&input.limit) {
            return Err(ServiceError::invalid("limit must be between 1 and 100"));
        }
        let connection = self.connection()?;
        let mut statement = connection
            .prepare(
                "SELECT job_id,run_id,session_id,attempt_count,last_error,
                        provider_result_json IS NOT NULL,created_at,updated_at
                 FROM semantic_projection_jobs
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3
                   AND provider=?4 AND state='dead'
                 ORDER BY updated_at DESC,created_at DESC
                 LIMIT ?5",
            )
            .map_err(db_error)?;
        let rows = statement
            .query_map(
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id,
                    input.provider,
                    input.limit
                ],
                |row| {
                    Ok(json!({
                        "job_id":row.get::<_,String>(0)?,
                        "run_id":row.get::<_,String>(1)?,
                        "session_id":row.get::<_,String>(2)?,
                        "attempt_count":row.get::<_,i64>(3)?,
                        "last_error":row.get::<_,Option<String>>(4)?,
                        "provider_result_saved":row.get::<_,bool>(5)?,
                        "created_at":row.get::<_,i64>(6)?,
                        "updated_at":row.get::<_,i64>(7)?,
                    }))
                },
            )
            .map_err(db_error)?;
        Ok(Value::Array(
            rows.collect::<Result<Vec<_>, _>>().map_err(db_error)?,
        ))
    }

    pub(crate) fn requeue_dead_projection_job(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: RequeueDeadJob = parse_params(request)?;
        validate_identifier(&input.provider, "provider")?;
        validate_identifier(&input.job_id, "job_id")?;
        self.with_write(
            identity,
            request,
            "projection.requeue_dead",
            "semantic_projection_job",
            |tx, _, now, _, _| {
                let last_error: Option<Option<String>> = tx
                    .query_row(
                        "SELECT last_error FROM semantic_projection_jobs
                         WHERE job_id=?1 AND account_id=?2 AND agent_id=?3
                           AND memory_space_id=?4 AND provider=?5 AND state='dead'",
                        params![
                            input.job_id,
                            identity.account_id,
                            identity.agent_id,
                            identity.memory_space_id,
                            input.provider,
                        ],
                        |row| row.get(0),
                    )
                    .optional()
                    .map_err(db_error)?;
                let last_error = last_error.ok_or_else(|| {
                    ServiceError::conflict(
                        "projection job is not dead or is outside this namespace",
                    )
                })?;
                tx.execute(
                    "UPDATE semantic_projection_jobs
                     SET state='pending',lease_owner=NULL,lease_expires_at=NULL,
                         attempt_count=0,next_retry_at=NULL,updated_at=?1
                     WHERE job_id=?2 AND account_id=?3 AND agent_id=?4
                       AND memory_space_id=?5 AND provider=?6 AND state='dead'",
                    params![
                        now,
                        input.job_id,
                        identity.account_id,
                        identity.agent_id,
                        identity.memory_space_id,
                        input.provider,
                    ],
                )
                .map_err(db_error)?;
                Ok((
                    json!({
                        "job_id":input.job_id,
                        "provider":input.provider,
                        "state":"pending",
                        "attempt_count":0,
                        "previous_error":last_error,
                    }),
                    Some(input.job_id.clone()),
                ))
            },
        )
    }
}

fn validate_identifier(value: &str, name: &str) -> ServiceResult<()> {
    if !valid_identifier(value) {
        return Err(ServiceError::invalid(format!("invalid {name}")));
    }
    Ok(())
}

fn validate_mapping(
    transaction: &rusqlite::Transaction<'_>,
    identity: &MemoryIdentity,
    mapping: &MappingInput,
) -> ServiceResult<()> {
    validate_identifier(&mapping.provider_memory_id, "provider_memory_id")?;
    validate_identifier(&mapping.memory_id, "memory_id")?;
    if mapping.canonical_revision < 1
        || !matches!(
            mapping.state.as_str(),
            "active" | "delete_proposed" | "superseded"
        )
        || mapping
            .last_score
            .is_some_and(|score| !(0.0..=1.0).contains(&score))
    {
        return Err(ServiceError::invalid("invalid projection mapping"));
    }
    let revision: Option<i64> = transaction
        .query_row(
            "SELECT current_revision FROM long_term_memories
             WHERE memory_id=?1 AND account_id=?2 AND agent_id=?3 AND memory_space_id=?4",
            params![
                mapping.memory_id,
                identity.account_id,
                identity.agent_id,
                identity.memory_space_id
            ],
            |row| row.get(0),
        )
        .optional()
        .map_err(db_error)?;
    if revision != Some(mapping.canonical_revision) {
        return Err(ServiceError::conflict(
            "projection mapping does not reference the current canonical revision",
        ));
    }
    Ok(())
}

fn upsert_mapping(
    transaction: &rusqlite::Transaction<'_>,
    identity: &MemoryIdentity,
    provider: &str,
    mapping: &MappingInput,
    now: i64,
) -> ServiceResult<()> {
    transaction.execute(
        "INSERT INTO semantic_projection_mappings(
            provider,account_id,agent_id,memory_space_id,provider_memory_id,memory_id,
            canonical_revision,state,last_score,created_at,updated_at
         ) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?10)
         ON CONFLICT(provider,account_id,agent_id,memory_space_id,provider_memory_id)
         DO UPDATE SET memory_id=excluded.memory_id,canonical_revision=excluded.canonical_revision,
                       state=excluded.state,last_score=excluded.last_score,updated_at=excluded.updated_at",
        params![
            provider,
            identity.account_id,
            identity.agent_id,
            identity.memory_space_id,
            mapping.provider_memory_id,
            mapping.memory_id,
            mapping.canonical_revision,
            mapping.state,
            mapping.last_score,
            now
        ],
    ).map_err(db_error)?;
    Ok(())
}
