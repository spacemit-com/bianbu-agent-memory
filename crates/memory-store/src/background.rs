use crate::{db_error, ServiceResult, Store};
use memory_core::{capability, now_ms, MemoryIdentity, PROTOCOL_VERSION};
use memory_protocol::{RequestEnvelope, ServiceError};
use rusqlite::{params, OptionalExtension, TransactionBehavior};
use serde_json::{json, Value};
use std::collections::BTreeSet;
use uuid::Uuid;

const MAINTENANCE_INTERVAL_MS: i64 = 60 * 60 * 1000;
const BACKUP_SCAN_INTERVAL_MS: i64 = 15 * 60 * 1000;
const EMBEDDING_BACKFILL_INTERVAL_MS: i64 = 60 * 1000;
const LEXICAL_BACKFILL_INTERVAL_MS: i64 = 60 * 1000;
/// Rows per fts2 backfill batch (MEM-NFR-01-01). Bounded so the Immediate
/// write transaction stays short — a 100k-row namespace drains in ~100
/// one-second-spaced batches instead of one long write lock.
const LEXICAL_BACKFILL_BATCH: i64 = 1000;
const RETRY_BASE_MS: i64 = 60 * 1000;
const RETRY_MAX_MS: i64 = 6 * 60 * 60 * 1000;

/// Recency-window slack for the embedding backlog marker. The scan filters on
/// l.updated_at while the marker records the high-water mark read before the
/// scan ran, so a row committed between the two moments would otherwise slip
/// through; the slack re-reads a small tail instead.
const BACKLOG_RECENCY_SLACK_MS: i64 = 2_000;

/// Per-namespace progress markers for the embedding backlog:
/// (account_id, agent_id, memory_space_id) -> (model, dims, l-side MAX(updated_at)).
/// Process-local — a restart degrades to one full scan, which is the same
/// cost the unfiltered pass already pays today.
static BACKLOG_MARKERS: std::sync::OnceLock<
    std::sync::Mutex<std::collections::BTreeMap<(String, String, String), (String, i64, i64)>>,
> = std::sync::OnceLock::new();

fn backlog_markers(
) -> &'static std::sync::Mutex<std::collections::BTreeMap<(String, String, String), (String, i64, i64)>> {
    BACKLOG_MARKERS.get_or_init(|| std::sync::Mutex::new(std::collections::BTreeMap::new()))
}

fn namespace_marker_key(identity: &MemoryIdentity) -> (String, String, String) {
    (
        identity.account_id.clone(),
        identity.agent_id.clone(),
        identity.memory_space_id.clone(),
    )
}

#[derive(Clone, Debug)]
struct Namespace {
    account_id: String,
    agent_id: String,
    memory_space_id: String,
}

impl Store {
    /// Executes due internal lifecycle work without requiring a Runtime conversation.
    /// Retry metadata is persisted so a daemon restart does not lose the backoff state.
    pub fn run_background_jobs(&self, device_id: &str) -> ServiceResult<Value> {
        if device_id.is_empty() || device_id.len() > 256 {
            return Err(ServiceError::invalid("invalid background worker device_id"));
        }
        let namespaces = self.background_namespaces()?;
        let now = now_ms();
        let mut attempted = 0_i64;
        let mut succeeded = 0_i64;
        let mut failed = 0_i64;
        let mut deferred = 0_i64;
        let mut results = Vec::new();

        for namespace in namespaces {
            let identity = background_identity(&namespace, device_id);
            for job_kind in ["maintenance", "backup", "embedding", "lexical"] {
                if !self.background_job_due(&namespace, job_kind, now)? {
                    deferred += 1;
                    continue;
                }
                attempted += 1;
                // Embedding/lexical batches write with their own Immediate
                // transaction instead of with_write() and run for seconds;
                // holding this guard keeps the integrity check from sampling
                // the database while they work
                // (docs/GATEWAY_EMBEDDING_INTEGRATION.md §8.2).
                let _job = self.activity.job();
                let request = background_request(&namespace, job_kind, now);
                let result = if !self.memory_is_enabled(&identity)? {
                    Ok(json!({"state":"skipped","reason":"memory_disabled"}))
                } else {
                    match job_kind {
                        "maintenance" => self.run_maintenance(&identity, &request),
                        "backup" => self.run_scheduled_backup(&identity, &request),
                        "embedding" => self.run_embedding_backfill(&identity),
                        "lexical" => self.run_lexical_backfill(&identity),
                        _ => unreachable!(),
                    }
                };
                match result {
                    Ok(value) => {
                        let interval = match job_kind {
                            "maintenance" => MAINTENANCE_INTERVAL_MS,
                            "backup" => BACKUP_SCAN_INTERVAL_MS,
                            "lexical" => LEXICAL_BACKFILL_INTERVAL_MS,
                            _ => EMBEDDING_BACKFILL_INTERVAL_MS,
                        };
                        self.record_background_success(
                            &namespace,
                            job_kind,
                            now,
                            now.saturating_add(interval),
                        )?;
                        succeeded += 1;
                        results.push(json!({
                            "account_id_hash":crate::hash_text(&namespace.account_id),
                            "agent_id":namespace.agent_id,
                            "memory_space_id":namespace.memory_space_id,
                            "job_kind":job_kind,"state":"success","result":value
                        }));
                    }
                    Err(error) => {
                        let retry_count =
                            self.record_background_failure(&namespace, job_kind, now, &error)?;
                        failed += 1;
                        results.push(json!({
                            "account_id_hash":crate::hash_text(&namespace.account_id),
                            "agent_id":namespace.agent_id,
                            "memory_space_id":namespace.memory_space_id,
                            "job_kind":job_kind,"state":"retry",
                            "error_code":error.code,"retry_count":retry_count
                        }));
                    }
                }
            }
        }
        Ok(json!({
            "completed_at":now,"attempted":attempted,"succeeded":succeeded,
            "failed":failed,"deferred":deferred,"results":results
        }))
    }

    /// Re-embed one namespace batch: missing vectors, stale revisions, and
    /// rows left behind by a model/dims switch. Transport failures propagate
    /// so the standard background backoff applies; secret memories never
    /// enter the backlog, mirroring the FTS indexing policy.
    fn run_embedding_backfill(&self, identity: &MemoryIdentity) -> ServiceResult<Value> {
        let config = self.product_config()?;
        if !config.embedding.enabled {
            return Ok(json!({"state":"skipped","reason":"embedding_disabled"}));
        }
        let Some(client) = crate::embedding::EmbeddingClient::new(
            &config.embedding,
            config.embedding.index_timeout_ms as u64,
        ) else {
            return Ok(json!({"state":"skipped","reason":"embedding_unconfigured"}));
        };
        let model = config.embedding.model.clone().unwrap_or_default();
        // Read the namespace's high-water mark BEFORE scanning: anything
        // committed after this read falls inside the next pass's recency
        // window. On error (0) the pass still runs with the previous
        // window, or fully when no marker exists yet.
        let namespace_max = self.long_term_max_updated_at(identity).unwrap_or(0);
        // The staleness anti-join cannot be served without row lookups into
        // memory_embeddings (revision/model/dims are row columns), so a
        // naive pass decrypts the whole namespace on every cycle — the K3
        // 100k acceptance measured ~27k page reads per pass. The marker
        // restricts steady-state passes to rows written since the previous
        // successful pass (plus slack for commit latency); a model or dims
        // switch forces one full pass to re-evaluate everything.
        let since_ms = {
            let markers = backlog_markers();
            let markers = markers.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
            match markers.get(&namespace_marker_key(identity)) {
                Some((model_seen, dims_seen, max_seen))
                    if *model_seen == model && *dims_seen == config.embedding.dims =>
                {
                    max_seen - BACKLOG_RECENCY_SLACK_MS
                }
                _ => 0,
            }
        };
        let backlog_started = std::time::Instant::now();
        let backlog = self.semantic_backlog(
            identity,
            &config.embedding,
            config.embedding.batch_size,
            since_ms,
        )?;
        if crate::embedding::cache_trace() {
            eprintln!(
                "[semantic-cache] embedding job backlog: {} pending, {}ms (since={since_ms})",
                backlog.len(),
                backlog_started.elapsed().as_millis()
            );
        }
        if backlog.is_empty() {
            if namespace_max > 0 {
                backlog_markers()
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .insert(
                        namespace_marker_key(identity),
                        (model, config.embedding.dims, namespace_max),
                    );
            }
            return Ok(json!({"state":"success","indexed":0}));
        }
        // One request carries at most `embedding_batch_len` items: the item cap
        // comes from the backlog query above, the token cap keeps the
        // single-slot server's per-request latency inside `index_timeout_ms`
        // now that a single full-budget text costs seconds rather than
        // milliseconds.
        let batch_len = {
            let references: Vec<&str> =
                backlog.iter().map(|(_, _, text)| text.as_str()).collect();
            crate::embedding::embedding_batch_len(
                &references,
                crate::embedding::max_text_tokens_of(&config.embedding),
            )
        };
        // The marker is a high-water mark, not a cursor. Advancing it while
        // rows are still pending hides those rows from every later pass (the
        // recency filter would exclude them forever), so only a pass that
        // drained the scan *and* ran the whole batch may move it.
        let limit = usize::try_from(config.embedding.batch_size).unwrap_or(0);
        let drained = batch_len == backlog.len() && backlog.len() < limit;
        let backlog: Vec<(String, i64, String)> = backlog.into_iter().take(batch_len).collect();
        let texts: Vec<String> = backlog.iter().map(|(_, _, text)| text.clone()).collect();
        let references: Vec<&str> = texts.iter().map(String::as_str).collect();
        let vectors = client
            .embed_texts(&references, config.embedding.dims)
            .map_err(|error| ServiceError::unavailable(error.message()))?;
        let entries: Vec<(String, i64, Vec<f32>)> = backlog
            .into_iter()
            .zip(vectors)
            .map(|((memory_id, revision, _), vector)| (memory_id, revision, vector))
            .collect();
        let batch_size = entries.len();
        let indexed = self.semantic_index_batch(
            identity,
            &entries,
            config.embedding.model.as_deref().unwrap_or_default(),
            &config.embedding.format,
        )?;
        if crate::embedding::cache_trace() {
            eprintln!(
                "[semantic-cache] embedding job indexed {indexed} rows (batch {batch_size}, drained {drained})"
            );
        }
        if namespace_max > 0 && drained {
            backlog_markers()
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .insert(
                    namespace_marker_key(identity),
                    (model, config.embedding.dims, namespace_max),
                );
        }
        Ok(json!({"state":"success","indexed":indexed,"batch":batch_size}))
    }

    /// MEM-NFR-01-01: cursor-backfill the fts2 bigram index for namespaces
    /// that already held rows when schema version 12 landed (their
    /// lexical_index_state row says ready=0). Every write path maintains
    /// fts2 inline from this version on, so the backfill only has to cover
    /// legacy rows; the persisted cursor makes interrupted runs resume
    /// where they stopped, and INSERT OR REPLACE keeps re-indexing
    /// idempotent. One batch per run keeps the Immediate transaction short.
    /// Namespaces without a state row (created after the migration) are
    /// ready by construction and return without opening a transaction.
    fn run_lexical_backfill(&self, identity: &MemoryIdentity) -> ServiceResult<Value> {
        let mut connection = self.connection()?;
        let transaction = connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(db_error)?;
        let state: Option<(i64, i64)> = transaction
            .query_row(
                "SELECT ready, backfill_cursor FROM lexical_index_state
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3",
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id
                ],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()
            .map_err(db_error)?;
        let Some((ready, cursor)) = state else {
            return Ok(json!({"state":"skipped","reason":"ready"}));
        };
        if ready != 0 {
            return Ok(json!({"state":"skipped","reason":"ready"}));
        }
        // Secret rows are skipped for indexing but still count towards
        // completion; against MAX(rowid) over ALL rows an all-secret tail
        // would otherwise spin empty batches forever.
        let max_rowid: i64 = transaction
            .query_row(
                "SELECT COALESCE(MAX(rowid),0) FROM long_term_memories
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3",
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id
                ],
                |row| row.get(0),
            )
            .map_err(db_error)?;
        let mut statement = transaction
            .prepare(
                "SELECT rowid, memory_id, content, summary, tags_json
                 FROM long_term_memories
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3
                   AND rowid>?4 AND sensitivity!='secret'
                 ORDER BY rowid LIMIT ?5",
            )
            .map_err(db_error)?;
        let scanned_rows = statement
            .query_map(
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id,
                    cursor,
                    LEXICAL_BACKFILL_BATCH
                ],
                |row| {
                    Ok((
                        row.get::<_, i64>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, Option<String>>(3)?,
                        row.get::<_, String>(4)?,
                    ))
                },
            )
            .map_err(db_error)?;
        let rows = scanned_rows.collect::<Result<Vec<_>, _>>().map_err(db_error)?;
        drop(statement);
        let mut indexed = 0_i64;
        let mut last_rowid = cursor;
        for (rowid, memory_id, content, summary, tags_json) in &rows {
            last_rowid = *rowid;
            let terms = crate::long_term::lexical_tokens(content, summary.as_deref(), tags_json);
            transaction
                .execute(
                    "INSERT OR REPLACE INTO long_term_fts2(rowid, memory_id, terms)
                     VALUES(?1,?2,?3)",
                    params![rowid, memory_id, terms],
                )
                .map_err(db_error)?;
            indexed += 1;
        }
        // When no non-secret rows remain the batch is empty; jump the cursor
        // to the namespace maximum so the secret tail cannot stall readiness.
        let new_cursor = if rows.is_empty() { max_rowid } else { last_rowid };
        let done = new_cursor >= max_rowid;
        transaction
            .execute(
                "UPDATE lexical_index_state SET backfill_cursor=?4, ready=?5
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3",
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id,
                    new_cursor,
                    if done { 1 } else { 0 }
                ],
            )
            .map_err(db_error)?;
        transaction.commit().map_err(db_error)?;
        // Newly indexed fts2 rows and especially the ready flip change which
        // lexical path serves the namespace, so memoized search results are
        // dropped for the whole store (wholesale invalidation, see
        // search_cache.rs). The skip paths above return before this point.
        self.search_cache.invalidate();
        Ok(json!({
            "state":"success","indexed":indexed,"scanned":rows.len() as i64,
            "cursor":new_cursor,"ready":done
        }))
    }

    /// MAX(updated_at) over the namespace's long-term rows. Answered from
    /// idx_long_term_namespace by seeking each kind group's rightmost entry,
    /// so it stays a handful of page reads even at 100k rows.
    fn long_term_max_updated_at(&self, identity: &MemoryIdentity) -> ServiceResult<i64> {
        let connection = self.connection()?;
        connection
            .query_row(
                "SELECT COALESCE(MAX(updated_at),0)
                 FROM long_term_memories
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3",
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id
                ],
                |row| row.get(0),
            )
            .map_err(db_error)
    }

    fn background_namespaces(&self) -> ServiceResult<Vec<Namespace>> {
        let connection = self.connection()?;
        let mut statement = connection
            .prepare(
                "SELECT account_id,agent_id,memory_space_id FROM memory_settings
                 UNION SELECT account_id,agent_id,memory_space_id FROM session_events
                 UNION SELECT account_id,agent_id,memory_space_id FROM session_summaries
                 UNION SELECT account_id,agent_id,memory_space_id FROM long_term_memories
                 UNION SELECT account_id,agent_id,memory_space_id FROM instructions
                 UNION SELECT account_id,agent_id,memory_space_id FROM backup_catalog
                    WHERE memory_space_id IS NOT NULL
                 ORDER BY account_id,agent_id,memory_space_id",
            )
            .map_err(db_error)?;
        let rows = statement
            .query_map([], |row| {
                Ok(Namespace {
                    account_id: row.get(0)?,
                    agent_id: row.get(1)?,
                    memory_space_id: row.get(2)?,
                })
            })
            .map_err(db_error)?;
        rows.collect::<Result<Vec<_>, _>>().map_err(db_error)
    }

    fn background_job_due(
        &self,
        namespace: &Namespace,
        job_kind: &str,
        now: i64,
    ) -> ServiceResult<bool> {
        let connection = self.connection()?;
        let next_retry_at: Option<Option<i64>> = connection
            .query_row(
                "SELECT next_retry_at FROM background_worker_state
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND job_kind=?4",
                params![
                    namespace.account_id,
                    namespace.agent_id,
                    namespace.memory_space_id,
                    job_kind
                ],
                |row| row.get(0),
            )
            .optional()
            .map_err(db_error)?;
        Ok(next_retry_at.flatten().map_or(true, |due| due <= now))
    }

    fn record_background_success(
        &self,
        namespace: &Namespace,
        job_kind: &str,
        now: i64,
        next_run_at: i64,
    ) -> ServiceResult<()> {
        let connection = self.connection()?;
        connection
            .execute(
                "INSERT INTO background_worker_state(
                account_id,agent_id,memory_space_id,job_kind,last_attempt_at,last_success_at,
                next_retry_at,retry_count,last_error_code,last_error_message,updated_at
             ) VALUES(?1,?2,?3,?4,?5,?5,?6,0,NULL,NULL,?5)
             ON CONFLICT(account_id,agent_id,memory_space_id,job_kind) DO UPDATE SET
                last_attempt_at=excluded.last_attempt_at,last_success_at=excluded.last_success_at,
                next_retry_at=excluded.next_retry_at,retry_count=0,last_error_code=NULL,
                last_error_message=NULL,updated_at=excluded.updated_at",
                params![
                    namespace.account_id,
                    namespace.agent_id,
                    namespace.memory_space_id,
                    job_kind,
                    now,
                    next_run_at
                ],
            )
            .map_err(db_error)?;
        Ok(())
    }

    fn record_background_failure(
        &self,
        namespace: &Namespace,
        job_kind: &str,
        now: i64,
        error: &ServiceError,
    ) -> ServiceResult<i64> {
        let connection = self.connection()?;
        let old_retry_count: i64 = connection
            .query_row(
                "SELECT retry_count FROM background_worker_state
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND job_kind=?4",
                params![
                    namespace.account_id,
                    namespace.agent_id,
                    namespace.memory_space_id,
                    job_kind
                ],
                |row| row.get(0),
            )
            .optional()
            .map_err(db_error)?
            .unwrap_or(0);
        let retry_count = old_retry_count.saturating_add(1).min(30);
        let shift = retry_count.saturating_sub(1).min(12) as u32;
        let backoff = RETRY_BASE_MS
            .saturating_mul(1_i64.checked_shl(shift).unwrap_or(i64::MAX))
            .min(RETRY_MAX_MS);
        let message = error.message.chars().take(500).collect::<String>();
        connection
            .execute(
                "INSERT INTO background_worker_state(
                account_id,agent_id,memory_space_id,job_kind,last_attempt_at,last_success_at,
                next_retry_at,retry_count,last_error_code,last_error_message,updated_at
             ) VALUES(?1,?2,?3,?4,?5,NULL,?6,?7,?8,?9,?5)
             ON CONFLICT(account_id,agent_id,memory_space_id,job_kind) DO UPDATE SET
                last_attempt_at=excluded.last_attempt_at,next_retry_at=excluded.next_retry_at,
                retry_count=excluded.retry_count,last_error_code=excluded.last_error_code,
                last_error_message=excluded.last_error_message,updated_at=excluded.updated_at",
                params![
                    namespace.account_id,
                    namespace.agent_id,
                    namespace.memory_space_id,
                    job_kind,
                    now,
                    now.saturating_add(backoff),
                    retry_count,
                    error.code,
                    message
                ],
            )
            .map_err(db_error)?;
        Ok(retry_count)
    }
}

fn background_identity(namespace: &Namespace, device_id: &str) -> MemoryIdentity {
    MemoryIdentity {
        account_id: namespace.account_id.clone(),
        device_id: device_id.to_owned(),
        agent_id: namespace.agent_id.clone(),
        agent_name: namespace.agent_id.clone(),
        agent_instance_id: "memoryd-background-worker".to_owned(),
        memory_space_id: namespace.memory_space_id.clone(),
        capabilities: capability::ALL
            .iter()
            .map(|value| (*value).to_owned())
            .collect::<BTreeSet<_>>(),
        peer_uid: 0,
        peer_gid: 0,
        peer_pid: 0,
        peer_start_time_ticks: 0,
    }
}

fn background_request(namespace: &Namespace, job_kind: &str, now: i64) -> RequestEnvelope {
    let interval = match job_kind {
        "maintenance" => MAINTENANCE_INTERVAL_MS,
        "backup" => BACKUP_SCAN_INTERVAL_MS,
        "lexical" => LEXICAL_BACKFILL_INTERVAL_MS,
        _ => EMBEDDING_BACKFILL_INTERVAL_MS,
    };
    RequestEnvelope {
        protocol: PROTOCOL_VERSION.to_owned(),
        request_id: Uuid::new_v4().to_string(),
        trace_id: Uuid::new_v4().to_string(),
        method: match job_kind {
            "maintenance" => "maintenance.run".to_owned(),
            "backup" => "backup.run_scheduled".to_owned(),
            "lexical" => "background.lexical".to_owned(),
            _ => "background.embedding".to_owned(),
        },
        params: if job_kind == "maintenance" {
            json!({"remove_expired":true})
        } else if job_kind == "backup" {
            json!({"force":false})
        } else {
            json!({"batch":true})
        },
        idempotency_key: Some(format!(
            "background:{job_kind}:{}:{}",
            namespace.memory_space_id,
            now / interval
        )),
        deadline_ms: None,
    }
}
