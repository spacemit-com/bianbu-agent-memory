use crate::{db_error, hash_bytes, json_bytes, parse_params, ServiceResult, Store};
use memory_core::{allowed_memory_kind, allowed_sync_policy, MemoryIdentity, MAX_TEXT_BYTES};
use memory_protocol::{RequestEnvelope, ServiceError};
use rusqlite::{params, params_from_iter, types::Value as SqlValue, OptionalExtension};
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use uuid::Uuid;

#[derive(Clone, Deserialize)]
struct EvidenceInput {
    evidence_type: String,
    evidence_id: String,
    #[serde(default)]
    evidence_hash: Option<String>,
}

#[derive(Deserialize)]
struct PutMemory {
    kind: String,
    text: String,
    #[serde(default)]
    summary: Option<String>,
    #[serde(default)]
    tags: Vec<String>,
    #[serde(default)]
    evidence: Vec<EvidenceInput>,
    #[serde(default = "default_sync")]
    sync_policy: String,
    #[serde(default = "default_sensitivity")]
    sensitivity: String,
    #[serde(default)]
    confidence: Option<f64>,
    #[serde(default)]
    expires_at: Option<i64>,
}

fn default_sync() -> String {
    "account_agent".to_owned()
}
fn default_sensitivity() -> String {
    "private".to_owned()
}

#[derive(Deserialize)]
struct SearchHeaders {
    query: String,
    #[serde(default)]
    kinds: Vec<String>,
    #[serde(default = "default_top_k")]
    top_k: i64,
}

fn default_top_k() -> i64 {
    10
}

#[derive(Deserialize)]
struct GetMany {
    memory_ids: Vec<String>,
}

#[derive(Deserialize)]
struct ReviseMemory {
    memory_id: String,
    text: String,
    expected_revision: i64,
    #[serde(default)]
    summary: Option<String>,
    #[serde(default)]
    tags: Option<Vec<String>>,
    #[serde(default)]
    evidence: Vec<EvidenceInput>,
}

#[derive(Deserialize)]
struct ReconcileSlots {
    #[serde(default)]
    slots: Vec<String>,
    #[serde(default = "default_true")]
    dry_run: bool,
}

fn default_true() -> bool {
    true
}

#[derive(Deserialize)]
struct ForgetMemory {
    memory_id: String,
    #[serde(default = "default_reason")]
    reason_code: String,
    #[serde(default)]
    confirm_token: Option<String>,
}

fn default_reason() -> String {
    "user_request".to_owned()
}

#[derive(Deserialize)]
struct WaitForget {
    job_id: String,
}

impl Store {
    pub(crate) fn put_long_term(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: PutMemory = parse_params(request)?;
        validate_memory_text(&input.text)?;
        validate_kind(&input.kind)?;
        validate_sync_policy(&input.sync_policy)?;
        validate_sensitivity(&input.sensitivity)?;
        validate_tags(&input.tags)?;
        validate_evidence(&input.evidence)?;
        if input
            .confidence
            .is_some_and(|value| !(0.0..=1.0).contains(&value))
        {
            return Err(ServiceError::invalid("confidence must be between 0 and 1"));
        }
        let result = self.with_write(
            identity,
            request,
            "long_term.put",
            "long_term",
            |tx, hlc, now, _, _| {
                let sensitivity = if looks_like_credential(&input.text) {
                    "secret"
                } else {
                    input.sensitivity.as_str()
                };
                let expires_at = Store::effective_expiry(tx, identity, input.expires_at, now)?;
                let memory_id = Uuid::new_v4().to_string();
                let tags_json = serde_json::to_string(&input.tags)
                    .map_err(|_| ServiceError::invalid("invalid tags"))?;
                let payload = memory_payload(
                    &memory_id,
                    &input.kind,
                    &input.text,
                    input.summary.as_deref(),
                    &input.tags,
                    "agent",
                    input.confidence,
                    sensitivity,
                    1,
                    &input.sync_policy,
                    expires_at,
                    &[],
                );
                let blob = json_bytes(&payload)?;
                let payload_hash = hash_bytes(&blob);
                tx.execute(
                    "INSERT INTO long_term_memories(
                    memory_id,account_id,agent_id,memory_space_id,kind,content,summary,tags_json,
                    source,confidence,sensitivity,current_revision,sync_policy,expires_at,
                    created_at,updated_at,hlc
                 ) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,'agent',?9,?10,1,?11,?12,?13,?13,?14)",
                    params![
                        memory_id,
                        identity.account_id,
                        identity.agent_id,
                        identity.memory_space_id,
                        input.kind,
                        input.text,
                        input.summary,
                        tags_json,
                        input.confidence,
                        sensitivity,
                        input.sync_policy,
                        expires_at,
                        now,
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
                        now,
                        hlc
                    ],
                )
                .map_err(db_error)?;
                insert_evidence(tx, &memory_id, 1, &input.evidence)?;
                index_memory(
                    tx,
                    &memory_id,
                    &input.text,
                    input.summary.as_deref(),
                    &tags_json,
                    sensitivity,
                )?;
                Store::enqueue_outbox(
                    tx,
                    identity,
                    "long_term",
                    &memory_id,
                    "put",
                    1,
                    hlc,
                    &payload_hash,
                    &input.sync_policy,
                    now,
                )?;
                Ok((
                    json!({
                        "memory_id":memory_id,
                        "kind":input.kind,
                        "revision":1,
                        "sync_policy":input.sync_policy,
                        "source":"agent",
                        "created_at":now,
                        "hlc":hlc
                    }),
                    Some(memory_id),
                ))
            },
        )?;
        // Semantic indexing is best-effort and strictly after the commit: the
        // write lock is released, so the HTTP call never blocks other clients,
        // and any miss is retried by the background embedding job.
        if let Some(memory_id) = result.get("memory_id").and_then(Value::as_str) {
            crate::embedding::index_memory_after_commit(self, identity, memory_id);
        }
        Ok(result)
    }

    pub(crate) fn search_headers(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: SearchHeaders = parse_params(request)?;
        if input.query.len() > 4096 || !(1..=100).contains(&input.top_k) {
            return Err(ServiceError::invalid("query or top_k is out of range"));
        }
        let kinds = input.kinds.iter().cloned().collect::<BTreeSet<_>>();
        for kind in &kinds {
            validate_kind(kind)?;
        }
        let mut values = Vec::new();
        // Keep the historical fan-out so kind filtering and lexical fallback
        // retain the same recall quality; cap the bounded work for top_k=100.
        let candidate_limit = input.top_k.saturating_mul(5).clamp(10, 500);
        let trace_stages = crate::embedding::cache_trace();
        let mut stage_started = std::time::Instant::now();
        // MEM-NFR-01-01 hot-query cache: identical recall requests between
        // writes return the memoized result without entering the scan gate
        // or touching the connection pool. Misses run the full cascade and
        // repopulate the cache below; the lookup captures the invalidation
        // generation up front, so a write that lands mid-pipeline makes the
        // finished result uncacheable rather than stale.
        let query_key = input.query.trim().to_owned();
        let kinds_key = kinds.iter().cloned().collect::<Vec<_>>();
        let cache_lookup = self
            .search_cache
            .begin(identity, &query_key, input.top_k, &kinds_key);
        if let Some(cached) = self.search_cache.get(&cache_lookup) {
            if trace_stages {
                eprintln!("[search-trace] cache-hit");
            }
            return Ok(Value::Array((*cached).clone()));
        }
        // MEM-NFR-01-01: the indexed bigram cascade serves every query run
        // with >=2 characters (CJK bigrams in long_term_fts2, substring
        // phrases for longer ASCII runs in the trigram table). The
        // historical SQL cascade — trigram phrase/fuzzy, the %term% overlap
        // scan, and the LIKE backfill — stays as the fallback for
        // degenerate queries without any indexable run and for namespaces
        // still awaiting the upgrade backfill.
        let runs = lexical_query_runs(input.query.trim());
        let mut lexical_exhausted = false;
        // MEM-NFR-01-01: the scan gate bounds concurrent lexical/FTS scan
        // work to the device's CPU budget. Gating only search_headers keeps
        // the cheap follow-up get_many of the search results out of the
        // scan queue — a whole-request gate made every get_many pay the
        // (clients/permits) queue amplification a second time per
        // search+read round trip. The permit is taken BEFORE the connection
        // checkout: parked requests then pin no connection, so the live
        // pool stays at the scan concurrency and each pooled connection's
        // page cache stays warm. A checkout placed before the gate let all
        // 32 acceptance clients pin their own connection, fragmenting the
        // per-connection SQLCipher page caches and tripling the scan tail
        // (probe: lexical-stage p95 26ms -> 87ms). One acquisition covers
        // the lexical cascade and the backfill passes below; nested
        // acquisitions (semantic recall inside a search, post-commit
        // embedding) pass through freely, so the gate cannot self-deadlock.
        let _scan_permit = crate::request_gate::acquire();
        let connection = self.connection()?;
        let lexical_ready = if runs.is_empty() {
            false
        } else {
            self.lexical_index_ready(identity)?
        };
        if lexical_ready {
            // Long CJK runs expand into OR groups whose union can be much
            // larger than a single phrase's match set; the newest-first
            // fetch then needs headroom over the short-run budget so the
            // coverage re-rank below actually sees the relevant rows (the
            // historical fuzzy stage leaned on bm25 for this). The K3 100k
            // acceptance run showed the historical flat 500 fetch dominated
            // the MEM-NFR-01-01 p95 tail: at top_k=10 each two-run CJK
            // query materialized 500 encrypted rows to return 10. Scaling
            // with the candidate fan-out (5x, halved from the flat 500 for
            // small top_k) keeps the headroom for full-window matches that
            // sit below the newest partial matches while halving the row
            // budget; the acceptance recall checks and the crowding
            // regression test pin the ranking behavior.
            let fetch_limit = if runs.iter().any(|run| run.verify_terms.len() > 1) {
                (candidate_limit.saturating_mul(5)).clamp(250, 500) as i64
            } else {
                (candidate_limit.saturating_mul(2)).clamp(100, 500) as i64
            };
            values = query_lexical_headers(
                &connection,
                identity,
                &runs,
                fetch_limit,
                candidate_limit as usize,
            )?;
        } else if !input.query.trim().is_empty() && input.query.chars().count() >= 3 {
            let fts_query = format!("\"{}\"", input.query.replace('"', "\"\""));
            // Fetch a small bounded candidate set.  The previous 5x fan-out
            // caused SQLite to rank and materialize up to 50 encrypted rows
            // for every Runtime recall even though only top_k records are
            // returned, multiplying work under concurrent clients.
            values = query_fts_headers(&connection, identity, &fts_query, candidate_limit)?;
            if values.is_empty() {
                if let Some(fuzzy_query) = fuzzy_fts_query(&input.query) {
                    values = query_fts_headers(&connection, identity, &fuzzy_query, candidate_limit)?;
                }
            }
            if values.is_empty() {
                lexical_exhausted = true;
            }
        }
        if trace_stages {
            eprintln!(
                "[search-trace] lexical {}ms ({} rows)",
                stage_started.elapsed().as_millis(),
                values.len()
            );
            stage_started = std::time::Instant::now();
        }
        // MEM-NFR-02 semantic stage: vector recall fused with the lexical
        // cascade via RRF. Any embedding failure — disabled, service down,
        // timeout — degrades to the lexical result above, which is the
        // contract's keyword fallback, and the degradation is kept out of the
        // MEM-NFR-01-01 hot-query cache: that cache has no TTL, so memoizing
        // a keyword-only answer would serve the lexical subset for the same
        // question until the next write rotates the generation.
        let semantic = self.semantic_stage(identity, &input.query, &kinds, 100);
        if trace_stages {
            eprintln!(
                "[search-trace] semantic {}ms ({} rows, {})",
                stage_started.elapsed().as_millis(),
                semantic.rows(),
                semantic.label()
            );
            stage_started = std::time::Instant::now();
        }
        let (fused, cacheable) = crate::embedding::fuse_semantic_stage(values, semantic);
        values = fused;
        if values.len() < input.top_k as usize {
            // Same scan permit as the lexical cascade above (still held):
            // the backfill passes (AND-joined run phrases, the overlap
            // scan, the LIKE fallback) are scan-shaped work in the same
            // CPU class.
            if lexical_ready {
                // Indexed backfill: AND the run phrases, then verify the
                // exact raw substring per field — the same rows the LIKE
                // scan below returns, minus the full-namespace scan.
                let backfill = query_lexical_backfill(
                    &connection,
                    identity,
                    &runs,
                    input.query.trim(),
                    candidate_limit as usize,
                )?;
                for row in backfill {
                    let id = row
                        .get("memory_id")
                        .and_then(Value::as_str)
                        .unwrap_or_default();
                    if !values
                        .iter()
                        .any(|old| old.get("memory_id").and_then(Value::as_str) == Some(id))
                    {
                        values.push(row);
                    }
                }
                if trace_stages {
                    eprintln!(
                        "[search-trace] lexical-backfill {}ms ({} rows)",
                        stage_started.elapsed().as_millis(),
                        values.len()
                    );
                    stage_started = std::time::Instant::now();
                }
            } else {
                if lexical_exhausted {
                    let overlap = query_overlap_headers(
                        &connection,
                        identity,
                        &input.query,
                        candidate_limit,
                    )?;
                    values = crate::embedding::fuse_search_results(values, overlap);
                    if trace_stages {
                        eprintln!(
                            "[search-trace] overlap {}ms ({} rows)",
                            stage_started.elapsed().as_millis(),
                            values.len()
                        );
                        stage_started = std::time::Instant::now();
                    }
                }
                let like = format!("%{}%", escape_like(input.query.trim()));
                let mut statement = connection.prepare_cached(
                    "SELECT memory_id,kind,summary,current_revision,sync_policy,source,sensitivity,
                            updated_at,hlc,0.0 AS score
                     FROM long_term_memories
                     WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3
                       AND (expires_at IS NULL OR expires_at>?4)
                       AND sensitivity!='secret'
                       AND (?5='' OR content LIKE ?6 ESCAPE '\\' OR COALESCE(summary,'') LIKE ?6 ESCAPE '\\' OR tags_json LIKE ?6 ESCAPE '\\')
                     ORDER BY updated_at DESC LIMIT ?7"
                ).map_err(db_error)?;
                let rows = statement
                    .query_map(
                        params![
                            identity.account_id,
                            identity.agent_id,
                            identity.memory_space_id,
                            memory_core::now_ms(),
                            input.query.trim(),
                            like,
                            candidate_limit
                        ],
                        header_row,
                    )
                    .map_err(db_error)?;
                for row in rows {
                    let row = row.map_err(db_error)?;
                    let id = row
                        .get("memory_id")
                        .and_then(Value::as_str)
                        .unwrap_or_default();
                    if !values
                        .iter()
                        .any(|old| old.get("memory_id").and_then(Value::as_str) == Some(id))
                    {
                        values.push(row);
                    }
                }
                if trace_stages {
                    eprintln!(
                        "[search-trace] like-backfill {}ms ({} rows)",
                        stage_started.elapsed().as_millis(),
                        values.len()
                    );
                }
            }
        }
        values.retain(|value| {
            kinds.is_empty()
                || value
                    .get("kind")
                    .and_then(Value::as_str)
                    .is_some_and(|kind| kinds.contains(kind))
        });
        values.truncate(input.top_k as usize);
        if cacheable {
            self.search_cache.insert(&cache_lookup, values.clone());
        } else if trace_stages {
            eprintln!("[search-trace] cache-skip (semantic degraded)");
        }
        Ok(Value::Array(values))
    }

    /// MEM-NFR-01-01: whether the namespace's fts2 bigram index covers all
    /// of its rows. Namespaces without a state row were created at or after
    /// schema version 12 and are maintained inline by index_memory, so they
    /// count as ready; the background "lexical" job flips upgraded
    /// namespaces to ready after cursor-backfilling their legacy rows.
    pub(crate) fn lexical_index_ready(&self, identity: &MemoryIdentity) -> ServiceResult<bool> {
        let connection = self.connection()?;
        let mut statement = connection
            .prepare_cached(
                "SELECT ready FROM lexical_index_state
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3",
            )
            .map_err(db_error)?;
        let ready = statement
            .query_row(
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id
                ],
                |row| row.get::<_, i64>(0),
            )
            .optional()
            .map_err(db_error)?;
        Ok(ready.is_none_or(|value| value != 0))
    }

    pub(crate) fn get_many(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: GetMany = parse_params(request)?;
        if input.memory_ids.len() > 100 {
            return Err(ServiceError::invalid("at most 100 memory_ids are allowed"));
        }
        if input.memory_ids.is_empty() {
            return Ok(Value::Array(Vec::new()));
        }
        let connection = self.connection()?;
        // Fetch all requested rows in one indexed query. The old implementation
        // performed one SELECT (and one evidence SELECT) per ID, which became a
        // measurable N+1 bottleneck for Runtime batch recalls.
        let placeholders = (0..input.memory_ids.len())
            .map(|index| format!("?{}", index + 1))
            .collect::<Vec<_>>()
            .join(",");
        let mut parameters = input
            .memory_ids
            .iter()
            .cloned()
            .map(SqlValue::Text)
            .collect::<Vec<_>>();
        // `memory_id` is the table's global primary key, so the IN list is
        // answered by one seek per id. Namespace equality is enforced in
        // Rust below — with the account/agent/space terms in the WHERE,
        // SQLite's un-ANALYZEd planner judged the (account, agent, space,
        // kind, updated_at) namespace index as cheap as the PK seeks, picked
        // it, and row-fetched the whole namespace (that index carries no
        // memory_id) — measured as a ~1s full-table walk per get_many call
        // on the K3 100k acceptance database.
        let now_param = parameters.len() + 1;
        parameters.push(SqlValue::Integer(memory_core::now_ms()));
        let mut statement = connection
            .prepare_cached(&format!(
                "SELECT memory_id,account_id,agent_id,memory_space_id,kind,content,summary,tags_json,
                        source,confidence,sensitivity,current_revision,sync_policy,expires_at,
                        created_at,updated_at,hlc
                 FROM long_term_memories
                 WHERE memory_id IN ({placeholders})
                   AND (expires_at IS NULL OR expires_at>?{now})",
                now = now_param,
            ))
            .map_err(db_error)?;
        let rows = statement
            .query_map(params_from_iter(parameters.iter()), |row| {
                let tags: String = row.get(7)?;
                Ok((
                    row.get::<_, String>(0)?,
                    (
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, String>(3)?,
                    ),
                    json!({
                        "memory_id":row.get::<_,String>(0)?,
                        "kind":row.get::<_,String>(4)?,
                        "text":row.get::<_,String>(5)?,
                        "summary":row.get::<_,Option<String>>(6)?,
                        "tags":serde_json::from_str::<Value>(&tags).unwrap_or(json!([])),
                        "source":row.get::<_,String>(8)?,
                        "confidence":row.get::<_,Option<f64>>(9)?,
                        "sensitivity":row.get::<_,String>(10)?,
                        "revision":row.get::<_,i64>(11)?,
                        "sync_policy":row.get::<_,String>(12)?,
                        "expires_at":row.get::<_,Option<i64>>(13)?,
                        "created_at":row.get::<_,i64>(14)?,
                        "updated_at":row.get::<_,i64>(15)?,
                        "hlc":row.get::<_,String>(16)?
                    }),
                ))
            })
            .map_err(db_error)?;
        let mut records = BTreeMap::new();
        for row in rows {
            let (memory_id, namespace, value) = row.map_err(db_error)?;
            if namespace
                != (
                    identity.account_id.clone(),
                    identity.agent_id.clone(),
                    identity.memory_space_id.clone(),
                )
            {
                continue;
            }
            records.insert(memory_id, value);
        }

        // Evidence is also read in one query and grouped by memory ID. Joining
        // against the current row prevents stale revision evidence from leaking
        // into the response while preserving the existing ordering contract;
        // foreign-namespace ids are dropped by the records-map attachment
        // below, so the join carries only the revision/expiry filters.
        let evidence_sql = format!(
            "SELECT e.memory_id,evidence_type,evidence_id,evidence_hash
             FROM evidence_refs e
             JOIN long_term_memories l ON l.memory_id=e.memory_id AND l.current_revision=e.revision
             WHERE e.memory_id IN ({placeholders})
               AND (l.expires_at IS NULL OR l.expires_at>?{now})
             ORDER BY e.memory_id,e.evidence_type,e.evidence_id",
            now = now_param,
        );
        let mut evidence_statement = connection.prepare_cached(&evidence_sql).map_err(db_error)?;
        let evidence_rows = evidence_statement
            .query_map(params_from_iter(parameters.iter()), |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    json!({
                        "evidence_type":row.get::<_,String>(1)?,
                        "evidence_id":row.get::<_,String>(2)?,
                        "evidence_hash":row.get::<_,Option<String>>(3)?
                    }),
                ))
            })
            .map_err(db_error)?;
        let mut evidence = BTreeMap::<String, Vec<Value>>::new();
        for row in evidence_rows {
            let (memory_id, item) = row.map_err(db_error)?;
            evidence.entry(memory_id).or_default().push(item);
        }
        for (memory_id, value) in &mut records {
            if let Some(object) = value.as_object_mut() {
                object.insert(
                    "evidence".to_owned(),
                    Value::Array(evidence.remove(memory_id).unwrap_or_default()),
                );
            }
        }

        // Preserve request order (and the historical duplicate-ID behavior).
        let values = input
            .memory_ids
            .iter()
            .filter_map(|memory_id| records.get(memory_id).cloned())
            .collect();
        Ok(Value::Array(values))
    }

    pub(crate) fn revise_long_term(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: ReviseMemory = parse_params(request)?;
        validate_memory_text(&input.text)?;
        if input.expected_revision < 1 {
            return Err(ServiceError::invalid("expected_revision must be positive"));
        }
        if let Some(tags) = input.tags.as_ref() {
            validate_tags(tags)?;
        }
        validate_evidence(&input.evidence)?;
        let result = self.with_write(identity, request, "long_term.revise", "long_term", |tx, hlc, now, _, _| {
            let current: Option<(String,Option<String>,String,String,Option<f64>,String,i64,String,Option<i64>,String)> = tx.query_row(
                "SELECT kind,summary,tags_json,source,confidence,sensitivity,current_revision,sync_policy,expires_at,content
                 FROM long_term_memories
                 WHERE memory_id=?1 AND account_id=?2 AND agent_id=?3 AND memory_space_id=?4",
                params![input.memory_id,identity.account_id,identity.agent_id,identity.memory_space_id],
                |row| Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?,row.get(4)?,row.get(5)?,row.get(6)?,row.get(7)?,row.get(8)?,row.get(9)?)),
            ).optional().map_err(db_error)?;
            let (kind,old_summary,old_tags_json,source,confidence,sensitivity,current_revision,sync_policy,expires_at,_) =
                current.ok_or_else(ServiceError::not_found)?;
            if current_revision != input.expected_revision {
                return Err(ServiceError::conflict(format!(
                    "expected revision {}, current revision is {}",input.expected_revision,current_revision
                )));
            }
            let revision = current_revision+1;
            let summary = input.summary.clone().or(old_summary);
            let tags = match input.tags.clone() {
                Some(tags) => tags,
                None => serde_json::from_str(&old_tags_json).unwrap_or_default(),
            };
            let tags_json = serde_json::to_string(&tags).map_err(|_| ServiceError::invalid("invalid tags"))?;
            // Provenance recorded when a session summary was archived; a revise
            // must not silently drop it from the new revision.
            let source_event_sequences: Vec<i64> = tx
                .query_row(
                    "SELECT payload FROM memory_revisions WHERE memory_id=?1 AND revision=?2",
                    params![input.memory_id, current_revision],
                    |row| row.get::<_, Vec<u8>>(0),
                )
                .optional()
                .map_err(db_error)?
                .and_then(|blob| serde_json::from_slice::<Value>(&blob).ok())
                .and_then(|value| value.get("source_event_sequences").cloned())
                .and_then(|value| value.as_array().cloned())
                .map(|values| values.iter().filter_map(Value::as_i64).collect())
                .unwrap_or_default();
            let payload = memory_payload(
                &input.memory_id,&kind,&input.text,summary.as_deref(),&tags,&source,confidence,
                &sensitivity,revision,&sync_policy,expires_at,&source_event_sequences,
            );
            let blob = json_bytes(&payload)?;
            let payload_hash = hash_bytes(&blob);
            let affected = tx.execute(
                "UPDATE long_term_memories SET content=?1,summary=?2,tags_json=?3,current_revision=?4,updated_at=?5,hlc=?6
                 WHERE memory_id=?7 AND account_id=?8 AND agent_id=?9 AND current_revision=?10",
                params![input.text,summary,tags_json,revision,now,hlc,input.memory_id,identity.account_id,identity.agent_id,input.expected_revision],
            ).map_err(db_error)?;
            if affected != 1 {
                return Err(ServiceError::conflict("memory changed concurrently"));
            }
            tx.execute(
                "INSERT INTO memory_revisions(
                    memory_id,revision,parent_revision,payload,payload_hash,author_device_id,
                    author_agent_id,created_at,hlc
                 ) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9)",
                params![input.memory_id,revision,current_revision,blob,payload_hash,identity.device_id,identity.agent_id,now,hlc],
            ).map_err(db_error)?;
            insert_evidence(tx,&input.memory_id,revision,&input.evidence)?;
            tx.execute("DELETE FROM long_term_fts WHERE memory_id=?1",[&input.memory_id]).map_err(db_error)?;
            delete_fts2_row(tx,&input.memory_id)?;
            index_memory(tx,&input.memory_id,&input.text,summary.as_deref(),&tags_json,&sensitivity)?;
            Store::enqueue_outbox(
                tx,identity,"long_term",&input.memory_id,"revise",revision,hlc,&payload_hash,&sync_policy,now,
            )?;
            Ok((json!({
                "memory_id":input.memory_id,"kind":kind,"revision":revision,
                "parent_revision":current_revision,"sync_policy":sync_policy,"updated_at":now,"hlc":hlc
            }),Some(input.memory_id.clone())))
        })?;
        // Same post-commit best-effort semantic indexing as put: the revision
        // just changed, so the old vector is stale until this re-embeds it.
        crate::embedding::index_memory_after_commit(self, identity, &input.memory_id);
        Ok(result)
    }

    pub(crate) fn reconcile_long_term_slots(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: ReconcileSlots = parse_params(request)?;
        if input.slots.len() > 100 || input.slots.iter().any(|slot| !valid_slot(slot)) {
            return Err(ServiceError::invalid("invalid slots"));
        }
        self.with_write(
            identity,
            request,
            "long_term.reconcile_slots",
            "long_term",
            |tx, hlc, now, _, _| {
                let requested = input.slots.iter().cloned().collect::<BTreeSet<_>>();
                let mut statement = tx
                    .prepare(
                        "SELECT memory_id,current_revision,tags_json,updated_at,sync_policy,hlc
                         FROM long_term_memories
                         WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3
                         ORDER BY updated_at DESC,current_revision DESC,memory_id DESC",
                    )
                    .map_err(db_error)?;
                let rows = statement
                    .query_map(
                        params![
                            identity.account_id,
                            identity.agent_id,
                            identity.memory_space_id
                        ],
                        |row| {
                            Ok((
                                row.get::<_, String>(0)?,
                                row.get::<_, i64>(1)?,
                                row.get::<_, String>(2)?,
                                row.get::<_, i64>(3)?,
                                row.get::<_, String>(4)?,
                                row.get::<_, String>(5)?,
                            ))
                        },
                    )
                    .map_err(db_error)?;
                let records = rows.collect::<Result<Vec<_>, _>>().map_err(db_error)?;
                drop(statement);

                let mut groups = std::collections::BTreeMap::<
                    String,
                    Vec<(String, i64, i64, String, String)>,
                >::new();
                for (memory_id, revision, tags_json, updated_at, sync_policy, memory_hlc) in records
                {
                    let tags = serde_json::from_str::<Vec<String>>(&tags_json).unwrap_or_default();
                    if let Some(slot) = tags.iter().find_map(|tag| tag.strip_prefix("slot:")) {
                        if valid_slot(slot) && (requested.is_empty() || requested.contains(slot)) {
                            groups.entry(slot.to_owned()).or_default().push((
                                memory_id.clone(),
                                revision,
                                updated_at,
                                sync_policy.clone(),
                                memory_hlc.clone(),
                            ));
                        }
                    }
                }

                let mut duplicate_groups = 0;
                let mut superseded = 0;
                let mut details = Vec::new();
                for (slot, mut group) in groups {
                    if group.len() < 2 {
                        continue;
                    }
                    duplicate_groups += 1;
                    group.sort_by(|left, right| {
                        (&right.4, right.2, right.1, &right.0)
                            .cmp(&(&left.4, left.2, left.1, &left.0))
                    });
                    let winner = group[0].0.clone();
                    let duplicates = group
                        .iter()
                        .skip(1)
                        .map(|item| item.0.clone())
                        .collect::<Vec<_>>();
                    if !input.dry_run {
                        for memory_id in &duplicates {
                            let deleted_revision = group
                                .iter()
                                .find(|item| &item.0 == memory_id)
                                .map(|item| item.1)
                                .unwrap_or(1);
                            let tombstone_id = Uuid::new_v4().to_string();
                            let sync_policy = group
                                .iter()
                                .find(|item| &item.0 == memory_id)
                                .map(|item| item.3.as_str())
                                .unwrap_or("local_only");
                            let tombstone_payload = json!({
                                "tombstone_id":tombstone_id,
                                "record_type":"long_term",
                                "record_id":memory_id,
                                "deleted_revision":deleted_revision,
                                "delete_epoch":now,
                                "reason_code":"slot_reconciliation",
                                "superseded_by":winner
                            });
                            let payload_hash = crate::hash_json(&tombstone_payload);
                            tx.execute(
                                "INSERT INTO tombstones(
                                    tombstone_id,account_id,agent_id,record_type,record_id,
                                    deleted_revision,delete_epoch,reason_code,created_at,hlc
                                 ) VALUES(?1,?2,?3,'long_term',?4,?5,?6,
                                          'slot_reconciliation',?6,?7)",
                                params![
                                    tombstone_id,
                                    identity.account_id,
                                    identity.agent_id,
                                    memory_id,
                                    deleted_revision,
                                    now,
                                    hlc
                                ],
                            )
                            .map_err(db_error)?;
                            Store::enqueue_outbox(
                                tx,
                                identity,
                                "tombstone",
                                memory_id,
                                "forget",
                                deleted_revision,
                                hlc,
                                &payload_hash,
                                sync_policy,
                                now,
                            )?;
                            tx.execute("DELETE FROM long_term_fts WHERE memory_id=?1", [memory_id])
                                .map_err(db_error)?;
                            delete_fts2_row(tx, memory_id)?;
                            tx.execute(
                                "DELETE FROM semantic_projection_mappings WHERE memory_id=?1",
                                [memory_id],
                            )
                            .map_err(db_error)?;
                            tx.execute(
                                "DELETE FROM memory_embeddings WHERE memory_id=?1",
                                [memory_id],
                            )
                            .map_err(db_error)?;
                            tx.execute(
                                "DELETE FROM long_term_memories
                                 WHERE memory_id=?1 AND account_id=?2 AND agent_id=?3
                                   AND memory_space_id=?4",
                                params![
                                    memory_id,
                                    identity.account_id,
                                    identity.agent_id,
                                    identity.memory_space_id
                                ],
                            )
                            .map_err(db_error)?;
                            superseded += 1;
                        }
                    }
                    details.push(json!({
                        "slot":slot,
                        "winner_memory_id":winner,
                        "duplicate_memory_ids":duplicates
                    }));
                }
                Ok((
                    json!({
                        "dry_run":input.dry_run,
                        "duplicate_groups":duplicate_groups,
                        "superseded_memories":superseded,
                        "groups":details
                    }),
                    None,
                ))
            },
        )
    }

    pub(crate) fn forget_long_term(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: ForgetMemory = parse_params(request)?;
        // The token value is intentionally ignored here. The daemon verifies it through
        // the configured verifier before dispatching this method.
        let _token_present = input
            .confirm_token
            .as_deref()
            .is_some_and(|token| !token.is_empty());
        if !memory_core::valid_identifier(&input.memory_id) || input.reason_code.len() > 128 {
            return Err(ServiceError::invalid("invalid forget request"));
        }
        self.with_write(identity, request, "long_term.forget", "long_term", |tx, hlc, now, audit_id, _| {
            let current: Option<(i64,String)> = tx.query_row(
                "SELECT current_revision,sync_policy FROM long_term_memories
                 WHERE memory_id=?1 AND account_id=?2 AND agent_id=?3 AND memory_space_id=?4",
                params![input.memory_id,identity.account_id,identity.agent_id,identity.memory_space_id],
                |row| Ok((row.get(0)?,row.get(1)?)),
            ).optional().map_err(db_error)?;
            let (revision,sync_policy) = current.ok_or_else(ServiceError::not_found)?;
            let tombstone_id = Uuid::new_v4().to_string();
            let job_id = Uuid::new_v4().to_string();
            let tombstone_payload = json!({
                "tombstone_id":tombstone_id,"record_type":"long_term","record_id":input.memory_id,
                "deleted_revision":revision,"delete_epoch":now,"reason_code":input.reason_code
            });
            let payload_hash = crate::hash_json(&tombstone_payload);
            tx.execute(
                "INSERT INTO tombstones(
                    tombstone_id,account_id,agent_id,record_type,record_id,deleted_revision,
                    delete_epoch,reason_code,created_at,hlc
                 ) VALUES(?1,?2,?3,'long_term',?4,?5,?6,?7,?6,?8)",
                params![tombstone_id,identity.account_id,identity.agent_id,input.memory_id,revision,now,input.reason_code,hlc],
            ).map_err(db_error)?;
            tx.execute("DELETE FROM long_term_fts WHERE memory_id=?1",[&input.memory_id]).map_err(db_error)?;
            delete_fts2_row(tx,&input.memory_id)?;
            tx.execute("DELETE FROM semantic_projection_mappings WHERE memory_id=?1",[&input.memory_id]).map_err(db_error)?;
            tx.execute("DELETE FROM memory_embeddings WHERE memory_id=?1",[&input.memory_id]).map_err(db_error)?;
            let affected = tx.execute(
                "DELETE FROM long_term_memories WHERE memory_id=?1 AND account_id=?2 AND agent_id=?3",
                params![input.memory_id,identity.account_id,identity.agent_id],
            ).map_err(db_error)?;
            if affected != 1 {
                return Err(ServiceError::not_found());
            }
            tx.execute(
                "INSERT INTO forget_jobs(job_id,tombstone_id,account_id,agent_id,record_id,state,sync_policy,audit_id,created_at,updated_at)
                 VALUES(?1,?2,?3,?4,?5,'local_hidden',?6,?7,?8,?8)",
                params![job_id,tombstone_id,identity.account_id,identity.agent_id,input.memory_id,sync_policy,audit_id,now],
            ).map_err(db_error)?;
            Store::enqueue_outbox(
                tx,identity,"tombstone",&input.memory_id,"forget",revision,hlc,&payload_hash,&sync_policy,now,
            )?;
            Ok((json!({
                "job_id":job_id,"state":"local_hidden",
                "receipt":{
                    "tombstone_id":tombstone_id,"record_id":input.memory_id,
                    "deleted_revision":revision,"delete_epoch":now,"audit_id":audit_id,
                    "local_hidden":true,
                    "cloud_acked":if sync_policy=="local_only" {Value::Bool(true)} else {Value::Bool(false)},
                    "devices_acked":if sync_policy=="local_only" {"not_applicable"} else {"pending"},
                    "backup_ledger_acked":if sync_policy=="local_only" {"not_applicable"} else {"pending"},
                    "physical_cleanup_pending":true
                }
            }),Some(input.memory_id.clone())))
        })
    }

    pub(crate) fn wait_forget(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: WaitForget = parse_params(request)?;
        let connection = self.connection()?;
        let row: Option<(String, String, String, String, String, i64)> = connection
            .query_row(
                "SELECT job_id,tombstone_id,record_id,state,sync_policy,updated_at FROM forget_jobs
             WHERE job_id=?1 AND account_id=?2 AND agent_id=?3",
                params![input.job_id, identity.account_id, identity.agent_id],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                        row.get(5)?,
                    ))
                },
            )
            .optional()
            .map_err(db_error)?;
        let (job_id, tombstone_id, record_id, state, sync_policy, updated_at) =
            row.ok_or_else(ServiceError::not_found)?;
        Ok(json!({
            "job_id":job_id,"tombstone_id":tombstone_id,"record_id":record_id,
            "state":state,"local_hidden":true,"cloud_acked":sync_policy=="local_only",
            "devices_acked":if sync_policy=="local_only" {"not_applicable"} else {"pending"},
            "backup_ledger_acked":if sync_policy=="local_only" {"not_applicable"} else {"pending"},
            "physical_cleanup_pending":true,"updated_at":updated_at
        }))
    }
}

pub(crate) fn memory_payload(
    memory_id: &str,
    kind: &str,
    text: &str,
    summary: Option<&str>,
    tags: &[String],
    source: &str,
    confidence: Option<f64>,
    sensitivity: &str,
    revision: i64,
    sync_policy: &str,
    expires_at: Option<i64>,
    source_event_sequences: &[i64],
) -> Value {
    let mut payload = json!({
        "memory_id":memory_id,"kind":kind,"text":text,"summary":summary,"tags":tags,
        "source":source,"confidence":confidence,"sensitivity":sensitivity,
        "revision":revision,"sync_policy":sync_policy,"expires_at":expires_at
    });
    // Session events a lifecycle-archived memory was derived from. Absent for
    // memories that were not produced by archiving a session summary.
    if !source_event_sequences.is_empty() {
        payload["source_event_sequences"] = json!(source_event_sequences);
    }
    payload
}

fn valid_slot(slot: &str) -> bool {
    !slot.is_empty()
        && slot.len() <= 128
        && slot
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

fn insert_evidence(
    transaction: &rusqlite::Transaction<'_>,
    memory_id: &str,
    revision: i64,
    evidence: &[EvidenceInput],
) -> ServiceResult<()> {
    for item in evidence {
        transaction.execute(
            "INSERT INTO evidence_refs(memory_id,revision,evidence_type,evidence_id,evidence_hash)
             VALUES(?1,?2,?3,?4,?5)",
            params![memory_id,revision,item.evidence_type,item.evidence_id,item.evidence_hash],
        ).map_err(db_error)?;
    }
    Ok(())
}

pub(crate) fn index_memory(
    transaction: &rusqlite::Transaction<'_>,
    memory_id: &str,
    text: &str,
    summary: Option<&str>,
    tags_json: &str,
    sensitivity: &str,
) -> ServiceResult<()> {
    if sensitivity != "secret" {
        transaction
            .execute(
                "INSERT INTO long_term_fts(memory_id,content,summary,tags) VALUES(?1,?2,?3,?4)",
                params![memory_id, text, summary, tags_json],
            )
            .map_err(db_error)?;
        // Bigram index twin (MEM-NFR-01-01): the fts2 rowid mirrors the
        // parent row so deletes resolve by rowid; INSERT OR REPLACE keeps
        // re-indexing (revise paths) idempotent.
        let terms = lexical_tokens(text, summary, tags_json);
        transaction
            .execute(
                "INSERT OR REPLACE INTO long_term_fts2(rowid, memory_id, terms)
                 SELECT rowid, ?2, ?3 FROM long_term_memories WHERE memory_id=?1",
                params![memory_id, memory_id, terms],
            )
            .map_err(db_error)?;
    }
    Ok(())
}

/// Removes the fts2 row for a memory. Must run while the parent row still
/// exists (the rowid is resolved through long_term_memories), i.e. before
/// the parent DELETE in the same transaction — the same ordering the other
/// derived-index deletes already use.
pub(crate) fn delete_fts2_row(
    transaction: &rusqlite::Transaction<'_>,
    memory_id: &str,
) -> ServiceResult<()> {
    transaction
        .execute(
            "DELETE FROM long_term_fts2
             WHERE rowid=(SELECT rowid FROM long_term_memories WHERE memory_id=?1)",
            [memory_id],
        )
        .map_err(db_error)?;
    Ok(())
}

/// Segments `text + summary + tags_json` (after the same normalization the
/// query side applies) into space-separated tokens for long_term_fts2:
/// CJK runs of >=2 chars emit every bigram, ASCII runs of >=2 chars emit the
/// lowercased word. The normalization mirrors the overlap stage this index
/// replaces, so rewritten queries ("颜色" -> "配色", scoped color
/// preferences) keep matching the same rows.
pub(crate) fn lexical_tokens(text: &str, summary: Option<&str>, tags_json: &str) -> String {
    let combined = format!(
        "{text} {} {tags_json}",
        summary.unwrap_or_default()
    );
    let normalized = normalize_search_query(&combined);
    let mut tokens = String::with_capacity(normalized.len() * 2);
    let mut run = String::new();
    let flush = |run: &mut String, tokens: &mut String| {
        let characters: Vec<char> = run.chars().collect();
        if characters.len() >= 2 {
            if characters.iter().all(|character| character.is_ascii()) {
                if !tokens.is_empty() {
                    tokens.push(' ');
                }
                tokens.push_str(run);
            } else {
                for window in characters.windows(2) {
                    if !tokens.is_empty() {
                        tokens.push(' ');
                    }
                    tokens.extend(window.iter());
                }
            }
        }
        run.clear();
    };
    for character in normalized.chars() {
        if character.is_alphanumeric() {
            run.push(character);
        } else {
            flush(&mut run, &mut tokens);
        }
    }
    flush(&mut run, &mut tokens);
    tokens
}

fn header_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<Value> {
    Ok(json!({
        "memory_id":row.get::<_,String>(0)?,
        "kind":row.get::<_,String>(1)?,
        "summary":row.get::<_,Option<String>>(2)?,
        "revision":row.get::<_,i64>(3)?,
        "sync_policy":row.get::<_,String>(4)?,
        "source":row.get::<_,String>(5)?,
        "sensitivity":row.get::<_,String>(6)?,
        "updated_at":row.get::<_,i64>(7)?,
        "hlc":row.get::<_,String>(8)?,
        "score":row.get::<_,f64>(9)?
    }))
}

fn query_fts_headers(
    connection: &rusqlite::Connection,
    identity: &MemoryIdentity,
    fts_query: &str,
    limit: i64,
) -> ServiceResult<Vec<Value>> {
    let mut statement = connection
        .prepare(
            "SELECT l.memory_id,l.kind,l.summary,l.current_revision,l.sync_policy,l.source,
                    l.sensitivity,l.updated_at,l.hlc,-bm25(long_term_fts) AS score
             FROM long_term_fts
             JOIN long_term_memories l ON l.memory_id=long_term_fts.memory_id
             WHERE long_term_fts MATCH ?1
               AND l.account_id=?2 AND l.agent_id=?3 AND l.memory_space_id=?4
               AND (l.expires_at IS NULL OR l.expires_at>?5)
             ORDER BY bm25(long_term_fts), l.updated_at DESC LIMIT ?6",
        )
        .map_err(db_error)?;
    let rows = statement
        .query_map(
            params![
                fts_query,
                identity.account_id,
                identity.agent_id,
                identity.memory_space_id,
                memory_core::now_ms(),
                limit
            ],
            header_row,
        )
        .map_err(db_error)?;
    rows.collect::<Result<Vec<_>, _>>().map_err(db_error)
}

fn fuzzy_fts_query(query: &str) -> Option<String> {
    let normalized = normalize_search_query(query);
    let mut terms = BTreeSet::new();
    let mut run = Vec::new();
    let flush = |run: &mut Vec<char>, terms: &mut BTreeSet<String>| {
        if run.len() >= 3 {
            for window in run.windows(3) {
                terms.insert(window.iter().collect::<String>());
                if terms.len() >= 64 {
                    break;
                }
            }
        }
        run.clear();
    };
    for character in normalized.chars() {
        if character.is_alphanumeric() {
            run.push(character);
        } else {
            flush(&mut run, &mut terms);
        }
        if terms.len() >= 64 {
            break;
        }
    }
    flush(&mut run, &mut terms);
    if terms.is_empty() {
        None
    } else {
        Some(
            terms
                .into_iter()
                .map(|term| format!("\"{}\"", term.replace('"', "\"\"")))
                .collect::<Vec<_>>()
                .join(" OR "),
        )
    }
}

fn normalize_search_query(query: &str) -> String {
    let mut normalized = query.to_lowercase();
    // String::replace allocates even when the pattern is absent, and the
    // indexed verify path runs this over every fetched candidate (up to a
    // few hundred per search), so skip each rewrite — and its allocation —
    // unless the phrase is actually present.
    for phrase in [
        "请问一下",
        "请告诉我",
        "我想知道",
        "是什么颜色",
        "什么颜色",
        "是什么",
        "你好",
        "您好",
        "请问",
        "目前",
        "当前",
        "现在",
        "告诉我",
        "什么",
    ] {
        if normalized.contains(phrase) {
            normalized = normalized.replace(phrase, "");
        }
    }
    for (term, canonical) in [("色彩", "配色"), ("颜色", "配色"), ("colour", "color")] {
        if normalized.contains(term) {
            normalized = normalized.replace(term, canonical);
        }
    }
    if is_scoped_color_preference(&normalized) {
        for term in [
            "操作系统",
            "系统",
            "终端",
            "界面",
            "terminal",
            "system",
            "配色",
            "主题",
            "偏好",
            "preference",
            "prefer",
        ] {
            if normalized.contains(term) {
                normalized = normalized.replace(term, "");
            }
        }
        normalized = format!("终端配色偏好{}", normalized.trim());
    }
    normalized
}

fn is_scoped_color_preference(text: &str) -> bool {
    let has_scope = ["系统", "终端", "界面", "terminal", "system"]
        .iter()
        .any(|term| text.contains(term));
    let has_color = ["配色", "主题", "color"]
        .iter()
        .any(|term| text.contains(term));
    let has_preference = ["偏好", "preference", "prefer"]
        .iter()
        .any(|term| text.contains(term));
    has_scope && has_color && has_preference
}

fn overlap_terms(text: &str) -> BTreeSet<String> {
    let mut terms = BTreeSet::new();
    let mut run = Vec::new();
    let flush = |run: &mut Vec<char>, terms: &mut BTreeSet<String>| {
        if run.iter().all(|character| character.is_ascii()) {
            if run.len() >= 2 {
                terms.insert(run.iter().collect::<String>());
            }
        } else if run.len() >= 2 {
            for window in run.windows(2) {
                terms.insert(window.iter().collect::<String>());
                if terms.len() >= 24 {
                    break;
                }
            }
        }
        run.clear();
    };
    for character in normalize_search_query(text).chars() {
        if character.is_alphanumeric() {
            run.push(character);
        } else {
            flush(&mut run, &mut terms);
        }
        if terms.len() >= 24 {
            break;
        }
    }
    flush(&mut run, &mut terms);
    terms
}

fn query_overlap_headers(
    connection: &rusqlite::Connection,
    identity: &MemoryIdentity,
    query: &str,
    limit: i64,
) -> ServiceResult<Vec<Value>> {
    let query_terms = overlap_terms(query);
    if query_terms.len() < 2 {
        return Ok(Vec::new());
    }
    let mut clauses = Vec::new();
    for index in 0..query_terms.len() {
        let parameter = index + 5;
        clauses.push(format!(
            "(content LIKE ?{parameter} ESCAPE '\\' OR COALESCE(summary,'') LIKE ?{parameter} ESCAPE '\\' OR tags_json LIKE ?{parameter} ESCAPE '\\')"
        ));
    }
    let limit_parameter = query_terms.len() + 5;
    let sql = format!(
        "SELECT memory_id,kind,summary,current_revision,sync_policy,source,sensitivity,
                updated_at,hlc,content,tags_json
         FROM long_term_memories
         WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3
           AND (expires_at IS NULL OR expires_at>?4)
           AND sensitivity!='secret'
           AND ({})
         ORDER BY updated_at DESC LIMIT ?{limit_parameter}",
        clauses.join(" OR ")
    );
    let mut parameters = vec![
        SqlValue::Text(identity.account_id.clone()),
        SqlValue::Text(identity.agent_id.clone()),
        SqlValue::Text(identity.memory_space_id.clone()),
        SqlValue::Integer(memory_core::now_ms()),
    ];
    parameters.extend(
        query_terms
            .iter()
            .map(|term| SqlValue::Text(format!("%{}%", escape_like(term)))),
    );
    parameters.push(SqlValue::Integer((limit * 20).clamp(100, 2000)));

    let mut statement = connection.prepare(&sql).map_err(db_error)?;
    let rows = statement
        .query_map(params_from_iter(parameters.iter()), |row| {
            let memory_id: String = row.get(0)?;
            let kind: String = row.get(1)?;
            let summary: Option<String> = row.get(2)?;
            let revision: i64 = row.get(3)?;
            let sync_policy: String = row.get(4)?;
            let source: String = row.get(5)?;
            let sensitivity: String = row.get(6)?;
            let updated_at: i64 = row.get(7)?;
            let hlc: String = row.get(8)?;
            let content: String = row.get(9)?;
            let tags: String = row.get(10)?;
            let candidate_terms = overlap_terms(&format!(
                "{} {} {}",
                content,
                summary.as_deref().unwrap_or_default(),
                tags
            ));
            let matches = query_terms.intersection(&candidate_terms).count();
            let score = matches as f64 / query_terms.len() as f64;
            Ok((
                matches,
                updated_at,
                json!({
                    "memory_id":memory_id,
                    "kind":kind,
                    "summary":summary,
                    "revision":revision,
                    "sync_policy":sync_policy,
                    "source":source,
                    "sensitivity":sensitivity,
                    "updated_at":updated_at,
                    "hlc":hlc,
                    "score":score
                }),
            ))
        })
        .map_err(db_error)?;
    let mut candidates = Vec::new();
    for row in rows {
        let candidate = row.map_err(db_error)?;
        if candidate.0 >= 2 && candidate.0 * 2 >= query_terms.len() {
            candidates.push(candidate);
        }
    }
    candidates.sort_by(|left, right| right.0.cmp(&left.0).then_with(|| right.1.cmp(&left.1)));
    candidates.truncate(limit as usize);
    Ok(candidates
        .into_iter()
        .map(|candidate| candidate.2)
        .collect())
}

/// One alphanumeric run of a normalized query, mapped to the index that can
/// serve it. Two-character CJK runs go to long_term_fts2 as a single bigram
/// phrase — the trigram tokenizer's blind spot that forced the historical
/// full-namespace scans. Longer CJK runs expand to their sliding trigram
/// windows, each a two-bigram phrase in fts2, OR-joined like the historical
/// fuzzy stage's trigram union: a window phrase "ab bc" matches iff the
/// document contains the three-character substring, so a natural-language
/// query no longer needs its whole (space-free) run to appear verbatim.
/// ASCII runs keep the legacy trigram table, which serves them as substring
/// phrases exactly like the historical fuzzy stage did.
#[derive(Debug, PartialEq)]
struct LexicalRun {
    trigram: bool,
    /// MATCH expression fragment — a quoted phrase, or a parenthesized OR
    /// group of trigram-window phrases.
    expression: String,
    /// substring terms scored in Rust against the normalized candidate text:
    /// the run itself for short/ASCII runs, its trigram windows for long CJK
    /// runs.
    verify_terms: Vec<String>,
}

/// Extracts the query runs the same way `lexical_tokens` segments text, so
/// the phrases a query looks for are exactly what indexing emitted. Mirrors
/// the historical overlap stage's 24-run and fuzzy stage's 64-term budgets.
fn lexical_query_runs(query: &str) -> Vec<LexicalRun> {
    let mut runs = Vec::new();
    let mut run = String::new();
    let flush = |run: &mut String, runs: &mut Vec<LexicalRun>| {
        let characters: Vec<char> = run.chars().collect();
        if characters.len() >= 2 && runs.len() < 24 {
            let text: String = characters.iter().collect();
            if characters.iter().all(|character| character.is_ascii()) {
                runs.push(LexicalRun {
                    trigram: characters.len() >= 3,
                    expression: fts_phrase(&text),
                    verify_terms: vec![text],
                });
            } else if characters.len() == 2 {
                runs.push(LexicalRun {
                    trigram: false,
                    expression: fts_phrase(&text),
                    verify_terms: vec![text],
                });
            } else {
                let mut expression = String::new();
                let mut verify_terms = Vec::new();
                for window in characters.windows(3) {
                    if verify_terms.len() >= 64 {
                        break;
                    }
                    let trigram: String = window.iter().collect();
                    if !expression.is_empty() {
                        expression.push_str(" OR ");
                    }
                    let first: String = window[..2].iter().collect();
                    let second: String = window[1..].iter().collect();
                    expression.push_str(&fts_phrase(&format!("{first} {second}")));
                    verify_terms.push(trigram);
                }
                runs.push(LexicalRun {
                    trigram: false,
                    expression: format!("({expression})"),
                    verify_terms,
                });
            }
        }
        run.clear();
    };
    for character in normalize_search_query(query).chars() {
        if character.is_alphanumeric() {
            run.push(character);
        } else {
            flush(&mut run, &mut runs);
        }
    }
    flush(&mut run, &mut runs);
    runs
}

struct LexicalCandidate {
    memory_id: String,
    kind: String,
    summary: Option<String>,
    revision: i64,
    sync_policy: String,
    source: String,
    sensitivity: String,
    updated_at: i64,
    hlc: String,
    combined: String,
    /// sum of per-run coverage: each run contributes the fraction of its
    /// verify terms present in the candidate's normalized text
    score: f64,
}

impl LexicalCandidate {
    fn header(&self, run_count: usize) -> Value {
        json!({
            "memory_id":self.memory_id,
            "kind":self.kind,
            "summary":self.summary,
            "revision":self.revision,
            "sync_policy":self.sync_policy,
            "source":self.source,
            "sensitivity":self.sensitivity,
            "updated_at":self.updated_at,
            "hlc":self.hlc,
            "score": self.score / run_count.max(1) as f64
        })
    }
}

fn fts_phrase(term: &str) -> String {
    format!("\"{}\"", term.replace('"', "\"\""))
}

/// Indexed lexical stage (MEM-NFR-01-01). Queries each run against the
/// table that can serve it — fts2 bigram phrases for CJK runs (a single
/// bigram for two-character terms, OR-joined trigram-window phrases for
/// longer runs), substring phrases in the trigram table for longer ASCII
/// runs — fetching a bounded newest-first candidate set from each
/// (ORDER BY fts.rowid DESC LIMIT, no bm25 ranking over the whole match
/// set). Candidates are then verified and scored in Rust against the same
/// normalized content+summary+tags concatenation the overlap stage used:
/// each run contributes the fraction of its verify terms the candidate
/// contains, and rows sort by (coverage, updated_at).
fn query_lexical_headers(
    connection: &rusqlite::Connection,
    identity: &MemoryIdentity,
    runs: &[LexicalRun],
    fetch_limit: i64,
    candidate_limit: usize,
) -> ServiceResult<Vec<Value>> {
    let mut candidates: std::collections::BTreeMap<String, LexicalCandidate> =
        std::collections::BTreeMap::new();
    // The trigram table cannot be aliased: a whole-table MATCH needs the
    // bare table name as its left-hand side ("f MATCH" reads as a column
    // reference), so each entry carries its own FROM shape.
    for (from, join, filter, order, trigram_table) in [
        (
            "long_term_fts2 f",
            "JOIN long_term_memories l ON l.rowid=f.rowid",
            "f.terms",
            "f.rowid",
            false,
        ),
        (
            "long_term_fts",
            "JOIN long_term_memories l ON l.memory_id=long_term_fts.memory_id",
            "long_term_fts",
            "long_term_fts.rowid",
            true,
        ),
    ] {
        let table_runs: Vec<&LexicalRun> = runs
            .iter()
            .filter(|run| run.trigram == trigram_table)
            .collect();
        if table_runs.is_empty() {
            continue;
        }
        // MEM-NFR-01-01: one statement per run, never an OR merge. A merged
        // "docs" OR "4074" MATCH cannot push ORDER BY rowid DESC + LIMIT into
        // FTS5: SQLite materializes the dense run's whole match set (measured
        // 53ms cold per query at 100k rows) and the newest-first cut then
        // drops the rare run's matches from the candidate set entirely —
        // "docs-4074" returned 0/10 rows containing 4074. Per-run statements
        // keep the LIMIT early-stop for dense runs and give every run its own
        // slice of the fetch budget, so rare matches are always candidates.
        let per_run_limit = (fetch_limit / table_runs.len() as i64).max(25);
        let sql = format!(
            "SELECT l.memory_id,l.kind,l.summary,l.current_revision,l.sync_policy,l.source,
                    l.sensitivity,l.updated_at,l.hlc,l.content,l.tags_json
             FROM {from}
             {join}
             WHERE {filter} MATCH ?1
               AND l.account_id=?2 AND l.agent_id=?3 AND l.memory_space_id=?4
               AND (l.expires_at IS NULL OR l.expires_at>?5)
             ORDER BY {order} DESC LIMIT ?6"
        );
        for run in table_runs {
            let mut statement = connection.prepare_cached(&sql).map_err(db_error)?;
            let rows = statement
                .query_map(
                    params![
                        run.expression,
                        identity.account_id,
                        identity.agent_id,
                        identity.memory_space_id,
                        memory_core::now_ms(),
                        per_run_limit
                    ],
                    |row| {
                        let summary: Option<String> = row.get(2)?;
                        let content: String = row.get(9)?;
                        let tags: String = row.get(10)?;
                        Ok(LexicalCandidate {
                            memory_id: row.get(0)?,
                            kind: row.get(1)?,
                            combined: format!("{content} {} {tags}", summary.as_deref().unwrap_or_default()),
                            summary,
                            revision: row.get(3)?,
                            sync_policy: row.get(4)?,
                            source: row.get(5)?,
                            sensitivity: row.get(6)?,
                            updated_at: row.get(7)?,
                            hlc: row.get(8)?,
                            score: 0.0,
                        })
                    },
                )
                .map_err(db_error)?;
            for row in rows {
                let candidate = row.map_err(db_error)?;
                candidates
                    .entry(candidate.memory_id.clone())
                    .or_insert(candidate);
            }
        }
    }
    let mut scored: Vec<&mut LexicalCandidate> = candidates.values_mut().collect();
    for candidate in &mut scored {
        let normalized = normalize_search_query(&candidate.combined);
        candidate.score = runs
            .iter()
            .map(|run| {
                let hits = run
                    .verify_terms
                    .iter()
                    .filter(|term| normalized.contains(term.as_str()))
                    .count();
                hits as f64 / run.verify_terms.len() as f64
            })
            .sum();
    }
    scored.sort_by(|left, right| {
        right
            .score
            .total_cmp(&left.score)
            .then_with(|| right.updated_at.cmp(&left.updated_at))
    });
    Ok(scored
        .into_iter()
        .take(candidate_limit)
        .map(|candidate| candidate.header(runs.len()))
        .collect())
}

/// Indexed replacement for the LIKE backfill: rows containing every query
/// run somewhere are found by AND-ing the run phrases in fts2/trigram, then
/// the exact whole-query substring (raw trimmed text, per field) is verified
/// in Rust — the same rows the historical `LIKE '%query%'` scan returned,
/// without the full-namespace scan.
fn query_lexical_backfill(
    connection: &rusqlite::Connection,
    identity: &MemoryIdentity,
    runs: &[LexicalRun],
    raw_query: &str,
    candidate_limit: usize,
) -> ServiceResult<Vec<Value>> {
    let mut id_sets: Vec<std::collections::BTreeSet<String>> = Vec::new();
    // Same unaliased trigram shape as the L1 loop above (whole-table MATCH).
    for (from, join, filter, order) in [
        (
            "long_term_fts2 f",
            "JOIN long_term_memories l ON l.rowid=f.rowid",
            "f.terms",
            "f.rowid",
        ),
        (
            "long_term_fts",
            "JOIN long_term_memories l ON l.memory_id=long_term_fts.memory_id",
            "long_term_fts",
            "long_term_fts.rowid",
        ),
    ] {
        // Run expressions are parenthesized when they carry OR groups, so
        // AND-joining them keeps each run's group intact.
        let phrases = runs
            .iter()
            .filter(|run| run.trigram == (from == "long_term_fts"))
            .map(|run| run.expression.clone())
            .collect::<Vec<_>>();
        if phrases.is_empty() {
            continue;
        }
        let sql = format!(
            "SELECT l.memory_id FROM {from}
             {join}
             WHERE {filter} MATCH ?1
               AND l.account_id=?2 AND l.agent_id=?3 AND l.memory_space_id=?4
               AND (l.expires_at IS NULL OR l.expires_at>?5)
             ORDER BY {order} DESC LIMIT ?6"
        );
        let mut statement = connection.prepare_cached(&sql).map_err(db_error)?;
        let rows = statement
            .query_map(
                params![
                    phrases.join(" AND "),
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id,
                    memory_core::now_ms(),
                    candidate_limit.saturating_mul(4).min(2000) as i64
                ],
                |row| row.get::<_, String>(0),
            )
            .map_err(db_error)?;
        let mut ids = std::collections::BTreeSet::new();
        for row in rows {
            ids.insert(row.map_err(db_error)?);
        }
        id_sets.push(ids);
    }
    if id_sets.is_empty() {
        return Ok(Vec::new());
    }
    // SQLite's LIKE is ASCII case-insensitive; matching that keeps the
    // historical backfill's behavior for mixed-case content.
    let needle = raw_query.to_lowercase();
    let shared = id_sets
        .iter()
        .skip(1)
        .fold(id_sets[0].clone(), |acc, set| {
            acc.intersection(set).cloned().collect()
        });
    if shared.is_empty() {
        return Ok(Vec::new());
    }
    let placeholders = shared.iter().map(|_| "?").collect::<Vec<_>>().join(",");
    let mut statement = connection
        .prepare_cached(&format!(
            // All-positional placeholders: a bare ? before explicit ?N would
            // collide with ?N's numbering (SQLite numbers ? from the largest
            // index seen so far), so every parameter stays bare and ordered.
            // Namespace equality is enforced in Rust below, not in SQL: with
            // the account/agent/space terms in the WHERE, the un-ANALYZEd
            // planner picks the (account,agent,space,kind,updated_at) index
            // and filters the IN list through the whole namespace — the same
            // plan get_many already fixed after measuring a ~1s full-table
            // walk per call on the K3 100k database (280ms unencrypted,
            // 1.27s under SQLCipher). The ids are already namespace-scoped
            // by the MATCH queries above, so the PK IN-seek is the plan.
            "SELECT memory_id,account_id,agent_id,memory_space_id,kind,summary,
                    current_revision,sync_policy,source,sensitivity,updated_at,hlc,
                    content,tags_json
             FROM long_term_memories
             WHERE (expires_at IS NULL OR expires_at>?)
               AND memory_id IN ({placeholders})
             ORDER BY updated_at DESC"
        ))
        .map_err(db_error)?;
    let mut parameters = vec![SqlValue::Integer(memory_core::now_ms())];
    for id in &shared {
        parameters.push(SqlValue::Text(id.clone()));
    }
    let namespace = (
        identity.account_id.clone(),
        identity.agent_id.clone(),
        identity.memory_space_id.clone(),
    );
    let rows = statement
        .query_map(params_from_iter(parameters.iter()), |row| {
            let namespace_row = (
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
            );
            let summary: Option<String> = row.get(5)?;
            let content: String = row.get(12)?;
            let tags: String = row.get(13)?;
            let hit = summary
                .as_deref()
                .is_some_and(|value| value.to_lowercase().contains(&needle))
                || content.to_lowercase().contains(&needle)
                || tags.to_lowercase().contains(&needle);
            Ok((
                namespace_row == namespace && hit,
                json!({
                    "memory_id":row.get::<_,String>(0)?,
                    "kind":row.get::<_,String>(4)?,
                    "summary":summary,
                    "revision":row.get::<_,i64>(6)?,
                    "sync_policy":row.get::<_,String>(7)?,
                    "source":row.get::<_,String>(8)?,
                    "sensitivity":row.get::<_,String>(9)?,
                    "updated_at":row.get::<_,i64>(10)?,
                    "hlc":row.get::<_,String>(11)?,
                    "score":0.0
                }),
            ))
        })
        .map_err(db_error)?;
    let mut verified = Vec::new();
    for row in rows {
        let (hit, value) = row.map_err(db_error)?;
        if hit {
            verified.push(value);
        }
    }
    verified.sort_by(|left, right| {
        right
            .get("updated_at")
            .and_then(Value::as_i64)
            .cmp(&left.get("updated_at").and_then(Value::as_i64))
    });
    verified.truncate(candidate_limit);
    Ok(verified)
}

/// Last-resort per-record text budget in characters; mirrors the SDK gate for
/// the "memory" target (see `sdk/python/src/bianbu_agent_sdk/memory/ontology.py`).
const DURABLE_TEXT_CHAR_LIMIT: usize = 2200;

pub(crate) fn validate_memory_text(text: &str) -> ServiceResult<()> {
    if text.trim().is_empty() || text.len() > MAX_TEXT_BYTES {
        return Err(ServiceError::invalid("memory text is empty or too large"));
    }
    // MEM-L3-03 backstop: the SDK write gate enforces the per-target budgets
    // (user 1375 / memory 2200 characters). A direct UDS client can bypass that
    // gate, so the store refuses oversized or control-character payloads too.
    if text.chars().count() > DURABLE_TEXT_CHAR_LIMIT {
        return Err(ServiceError::invalid(
            "memory text exceeds the durable budget",
        ));
    }
    if text
        .chars()
        .any(|character| character.is_control() && character != '\n' && character != '\t')
    {
        return Err(ServiceError::invalid(
            "memory text contains unsupported control characters",
        ));
    }
    Ok(())
}

fn validate_kind(kind: &str) -> ServiceResult<()> {
    if allowed_memory_kind(kind) {
        Ok(())
    } else {
        Err(ServiceError::invalid("invalid memory kind"))
    }
}

fn validate_sync_policy(policy: &str) -> ServiceResult<()> {
    if allowed_sync_policy(policy) && policy != "full" && policy != "summary_only" {
        Ok(())
    } else {
        Err(ServiceError::invalid(
            "long-term memory requires local_only or account_agent",
        ))
    }
}

fn validate_sensitivity(sensitivity: &str) -> ServiceResult<()> {
    if matches!(sensitivity, "public" | "private" | "sensitive" | "secret") {
        Ok(())
    } else {
        Err(ServiceError::invalid("invalid memory sensitivity"))
    }
}

pub(crate) fn looks_like_credential(text: &str) -> bool {
    let lowered = text.to_lowercase();
    [
        "password",
        "passwd",
        "api key",
        "api_key",
        "access token",
        "refresh token",
        "bearer ",
        "private key",
        "ssh key",
        "密码",
        "口令",
        "令牌",
        "密钥",
    ]
    .iter()
    .any(|marker| lowered.contains(marker))
}

pub(crate) fn validate_tags(tags: &[String]) -> ServiceResult<()> {
    let slots = tags
        .iter()
        .filter_map(|tag| tag.strip_prefix("slot:"))
        .collect::<Vec<_>>();
    if tags.len() > 64
        || tags.iter().any(|tag| tag.is_empty() || tag.len() > 128)
        || slots.len() > 1
        || slots.iter().any(|slot| !valid_slot(slot))
    {
        Err(ServiceError::invalid("invalid tags"))
    } else {
        Ok(())
    }
}

fn validate_evidence(evidence: &[EvidenceInput]) -> ServiceResult<()> {
    if evidence.len() > 100
        || evidence.iter().any(|item| {
            item.evidence_type.is_empty()
                || item.evidence_type.len() > 64
                || !memory_core::valid_identifier(&item.evidence_id)
        })
    {
        Err(ServiceError::invalid("invalid evidence references"))
    } else {
        Ok(())
    }
}

fn escape_like(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('%', "\\%")
        .replace('_', "\\_")
}
