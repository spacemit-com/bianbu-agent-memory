use crate::product::EmbeddingConfig;
use crate::{db_error, ServiceResult, Store};
use memory_core::{now_ms, MemoryIdentity};
use rusqlite::{params, params_from_iter, Connection, OptionalExtension, TransactionBehavior};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet, BinaryHeap};
use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// Default budget for the text fed to the embedding model per memory: the
/// authoritative content plus its summary when present.
///
/// The self-hosted embedding server runs with a fixed context window
/// (`--ctx-size 4096` on K3) and answers HTTP 400 for anything longer instead
/// of truncating, so the client has to clip or the memory never gets a vector.
/// The budget is counted in tokens rather than bytes because bytes say nothing
/// about the model window: the previous 16 KiB byte cap admitted ~5461 Chinese
/// characters (~3500 tokens) and ~16 KiB of ASCII (~4100 tokens), both
/// unindexable. `embedding.max_text_tokens` exposes this value and must stay
/// below the server window with headroom for the query instruction and the
/// special tokens the server adds itself.
///
/// The default is deliberately not window-proportional. A text at this budget
/// measures 10.3 s on K3, and the composite that matters is one such request
/// plus the inline `index_memory_after_commit` that may queue behind it: the
/// pair has to fit `index_timeout_ms` (30 s), so the budget stops at ~45% of
/// the window. Prefill cost is superlinear on this device (1291 real tokens
/// 6.5 s, 1843 -> 10.3 s, 2571 -> 18.2 s), so a window-proportional 3584
/// would need `index_timeout_ms` >= 45 s to stay safe.
pub(crate) const DEFAULT_EMBEDDING_TEXT_TOKENS: i64 = 2_560;
/// Share of an over-long text reserved for its tail: memories are usually
/// front-loaded with context and back-loaded with the newest fact, so keeping
/// both ends preserves more meaning than keeping the head alone.
const EMBEDDING_TEXT_TAIL_PERCENT: usize = 25;
/// ASCII bytes charged per token by the estimator. English runs tokenize at
/// roughly four bytes per token; three keeps the estimate on the safe side.
const ASCII_BYTES_PER_TOKEN: usize = 3;
/// Inserted between the kept head and the kept tail so the model reads the
/// text as partial instead of as a sentence nobody ever wrote.
const EMBEDDING_TEXT_ELISION: &str = "\n[...]\n";
/// Estimated prompt tokens one batched embedding request may carry.
///
/// Latency-driven, not window-driven, and deliberately independent of
/// `embedding.max_text_tokens`: raising the per-text budget must not silently
/// multiply the worst-case request time. The server runs a single slot
/// (`--parallel 1`), so request time tracks the tokens sent — K3 measures
/// ~5.6 ms/token for a single 1843-token text (10.3 s) and ~3.2 ms/token for a
/// batch of short ones, both of which fit `index_timeout_ms` (30 s) with room
/// for an inline embed queueing behind them. Packing more tokens buys no
/// throughput either (short texts measure ~3.2 ms/token at 17 items and ~3.4
/// ms/token at 32), so extra rows only add latency and delay interactive
/// embedding. A single text over this budget still goes alone — see
/// `embedding_batch_len` — and short memories are additionally bounded by
/// `embedding.batch_size`.
const EMBEDDING_BATCH_TEXT_TOKENS: usize = 2_560;
/// One 1024-dim f32 payload is ~4 KiB of JSON per text; 128 texts stays an
/// order of magnitude below this ceiling even at 8192 dims.
const MAX_EMBEDDING_RESPONSE_BYTES: usize = 16 * 1024 * 1024;
/// Hard cap mirroring the config validation bound (embedding.batch_size).
const MAX_EMBEDDING_BATCH: usize = 128;
/// RRF smoothing constant; 60 is the canonical value from the original paper.
const RRF_K: f64 = 60.0;

/// Row count above which a namespace is no longer cached: snapshots must stay
/// bounded so a pathological namespace cannot pin unbounded daemon RSS
/// (MEM-NFR-04). Larger namespaces keep the uncached SQL scan below.
const SEMANTIC_CACHE_MAX_ROWS: i64 = 150_000;
/// Total heap bytes all cached snapshots may occupy together; the
/// least-recently-used namespaces are evicted beyond this budget.
const SEMANTIC_CACHE_MAX_TOTAL_BYTES: usize = 256 * 1024 * 1024;
/// Snapshots up to this size reload on every watermark change: a small
/// namespace rebuilds in milliseconds, so a freshly put memory is searchable
/// through the vector branch on the very next query (the put-then-ask UX
/// pattern and the smoke gates depend on this).
const SEMANTIC_CACHE_ALWAYS_FRESH_ROWS: usize = 5_000;
/// Reload throttle for larger namespaces: while a big snapshot is being
/// rebuilt (seconds at 100k rows), searches keep serving the previous
/// snapshot instead of re-triggering a rebuild on every write.
const SEMANTIC_CACHE_RELOAD_MIN_INTERVAL_MS: u64 = 500;

#[derive(Debug)]
pub(crate) enum EmbeddingError {
    Timeout,
    Transport(String),
    Status(u16),
    Malformed(String),
}

impl EmbeddingError {
    pub(crate) fn message(&self) -> String {
        match self {
            EmbeddingError::Timeout => "embedding request timed out".to_owned(),
            EmbeddingError::Transport(detail) => format!("embedding transport failed: {detail}"),
            EmbeddingError::Status(code) => format!("embedding service returned {code}"),
            EmbeddingError::Malformed(detail) => format!("embedding response malformed: {detail}"),
        }
    }
}

/// Minimal OpenAI-compatible /embeddings client. Deliberately the same
/// hand-rolled TcpStream/HTTP-1.1 shape as `request_model_summary` so the
/// daemon keeps zero HTTP-runtime dependencies and the call stays outside any
/// SQLite transaction.
pub(crate) struct EmbeddingClient {
    authority: String,
    path: String,
    model: String,
    timeout: Duration,
    max_text_tokens: usize,
}

impl EmbeddingClient {
    /// `timeout_ms` lets callers tighten the budget: the search path clamps to
    /// the 2s MEM-NFR-02 budget minus the lexical stage, while indexing uses
    /// the configured `max_latency_ms` as-is.
    pub(crate) fn new(config: &EmbeddingConfig, timeout_ms: u64) -> Option<Self> {
        let base_url = config.base_url.as_deref()?;
        if !base_url.starts_with("http://") {
            return None;
        }
        let model = config.model.as_deref()?;
        let url = format!("{}/embeddings", base_url.trim_end_matches('/'));
        let rest = url.strip_prefix("http://")?;
        let (authority, path) = rest.split_once('/').unwrap_or((rest, "embeddings"));
        Some(Self {
            authority: authority.to_owned(),
            path: path.to_owned(),
            model: model.to_owned(),
            timeout: Duration::from_millis(timeout_ms.max(1)),
            max_text_tokens: max_text_tokens_of(config),
        })
    }

    pub(crate) fn embed_texts(
        &self,
        texts: &[&str],
        expected_dims: i64,
    ) -> Result<Vec<Vec<f32>>, EmbeddingError> {
        if texts.is_empty() || texts.len() > MAX_EMBEDDING_BATCH {
            return Err(EmbeddingError::Malformed(
                "embedding batch size is out of range".to_owned(),
            ));
        }
        let clipped: Vec<String> = texts
            .iter()
            .map(|text| clip_to_token_budget(text, self.max_text_tokens))
            .collect();
        let body = serde_json::to_vec(&json!({
            "model": self.model,
            "input": clipped,
        }))
        .map_err(|error| EmbeddingError::Malformed(error.to_string()))?;
        let endpoint = format!("{}:{}", host_of(&self.authority), port_of(&self.authority))
            .to_socket_addrs()
            .map_err(|error| EmbeddingError::Transport(error.to_string()))?
            .next()
            .ok_or_else(|| EmbeddingError::Transport("embedding endpoint unavailable".to_owned()))?;
        let mut stream = TcpStream::connect_timeout(&endpoint, self.timeout)
            .map_err(map_io_error)?;
        stream
            .set_read_timeout(Some(self.timeout))
            .map_err(map_io_error)?;
        stream
            .set_write_timeout(Some(self.timeout))
            .map_err(map_io_error)?;
        let headers = format!(
            "POST /{} HTTP/1.1\r\nHost: {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
            self.path,
            self.authority,
            body.len()
        );
        stream
            .write_all(headers.as_bytes())
            .and_then(|_| stream.write_all(&body))
            .map_err(map_io_error)?;
        let mut response = Vec::new();
        stream
            .take((MAX_EMBEDDING_RESPONSE_BYTES + 1) as u64)
            .read_to_end(&mut response)
            .map_err(map_io_error)?;
        if response.len() > MAX_EMBEDDING_RESPONSE_BYTES {
            return Err(EmbeddingError::Malformed(
                "embedding response is too large".to_owned(),
            ));
        }
        let response = String::from_utf8_lossy(&response);
        let (status_line, payload) = response
            .split_once("\r\n\r\n")
            .ok_or_else(|| EmbeddingError::Malformed("invalid embedding response".to_owned()))?;
        let status = status_line
            .split_ascii_whitespace()
            .nth(1)
            .and_then(|code| code.parse::<u16>().ok())
            .ok_or_else(|| EmbeddingError::Malformed("invalid embedding status".to_owned()))?;
        if status != 200 {
            return Err(EmbeddingError::Status(status));
        }
        let envelope: Value =
            serde_json::from_str(payload).map_err(|error| EmbeddingError::Malformed(error.to_string()))?;
        let data = envelope
            .get("data")
            .and_then(Value::as_array)
            .ok_or_else(|| EmbeddingError::Malformed("embedding data missing".to_owned()))?;
        if data.len() != texts.len() {
            return Err(EmbeddingError::Malformed(
                "embedding data count mismatch".to_owned(),
            ));
        }
        let mut slots = vec![None::<Vec<f32>>; texts.len()];
        for item in data {
            let index = item
                .get("index")
                .and_then(Value::as_i64)
                .ok_or_else(|| EmbeddingError::Malformed("embedding index missing".to_owned()))?;
            if !(0..texts.len() as i64).contains(&index) || slots[index as usize].is_some() {
                return Err(EmbeddingError::Malformed(
                    "embedding index is out of range or duplicated".to_owned(),
                ));
            }
            let vector = item
                .get("embedding")
                .and_then(Value::as_array)
                .ok_or_else(|| EmbeddingError::Malformed("embedding vector missing".to_owned()))?;
            if vector.len() as i64 != expected_dims {
                return Err(EmbeddingError::Malformed(
                    "embedding dimensionality mismatch".to_owned(),
                ));
            }
            let mut values = Vec::with_capacity(vector.len());
            for component in vector {
                let value = component
                    .as_f64()
                    .ok_or_else(|| EmbeddingError::Malformed("embedding value is not numeric".to_owned()))?;
                if !value.is_finite() {
                    return Err(EmbeddingError::Malformed(
                        "embedding value is not finite".to_owned(),
                    ));
                }
                values.push(value as f32);
            }
            slots[index as usize] = Some(values);
        }
        slots
            .into_iter()
            .map(|slot| slot.ok_or_else(|| EmbeddingError::Malformed("embedding gap".to_owned())))
            .collect()
    }
}

fn map_io_error(error: std::io::Error) -> EmbeddingError {
    if matches!(
        error.kind(),
        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
    ) {
        EmbeddingError::Timeout
    } else {
        EmbeddingError::Transport(error.to_string())
    }
}

fn host_of(authority: &str) -> &str {
    match authority.rsplit_once(':') {
        Some((host, _)) => host,
        None => authority,
    }
}

fn port_of(authority: &str) -> u16 {
    authority
        .rsplit_once(':')
        .and_then(|(_, port)| port.parse::<u16>().ok())
        .unwrap_or(80)
}

/// Conservative token count for one text, without shipping a tokenizer.
///
/// Every non-ASCII character is charged a full token (Qwen's BPE averages
/// ~0.64 tokens per Chinese character on real text) and ASCII runs one token
/// per `ASCII_BYTES_PER_TOKEN` bytes (English tokenizes closer to four bytes
/// per token). Both are upper bounds in practice, and that direction is the
/// safe one here: undercounting produces the HTTP 400 that a memory can never
/// recover from, while overcounting only shortens a text that was already over
/// budget.
fn estimate_tokens(value: &str) -> usize {
    let mut tokens: usize = 0;
    let mut ascii: usize = 0;
    for character in value.chars() {
        if character.is_ascii() {
            ascii += 1;
        } else {
            tokens += ascii.div_ceil(ASCII_BYTES_PER_TOKEN) + 1;
            ascii = 0;
        }
    }
    tokens + ascii.div_ceil(ASCII_BYTES_PER_TOKEN)
}

/// Byte offset of the last character that still fits `budget` tokens when the
/// text is read from the start.
fn head_byte_end(value: &str, budget: usize) -> usize {
    let mut tokens = 0;
    let mut ascii = 0;
    for (index, character) in value.char_indices() {
        if character.is_ascii() {
            ascii += 1;
            if ascii == ASCII_BYTES_PER_TOKEN {
                ascii = 0;
                tokens += 1;
            }
        } else {
            if ascii > 0 {
                ascii = 0;
                tokens += 1;
            }
            tokens += 1;
        }
        if tokens > budget {
            return index;
        }
    }
    value.len()
}

/// Byte offset of the first character that still fits `budget` tokens when the
/// text is read from the end.
fn tail_byte_start(value: &str, budget: usize) -> usize {
    let characters: Vec<(usize, char)> = value.char_indices().collect();
    let mut tokens = 0;
    let mut ascii = 0;
    let mut start = 0;
    for (index, character) in characters.into_iter().rev() {
        if character.is_ascii() {
            ascii += 1;
            if ascii == ASCII_BYTES_PER_TOKEN {
                ascii = 0;
                tokens += 1;
            }
        } else {
            if ascii > 0 {
                ascii = 0;
                tokens += 1;
            }
            tokens += 1;
        }
        if tokens > budget {
            break;
        }
        start = index;
    }
    start
}

/// Clip `value` to `budget` estimated tokens, keeping the head *and* the tail.
///
/// The embedding server answers HTTP 400 for any prompt past its context
/// window and never truncates, so an unclipped memory never gets a vector and
/// stays invisible to semantic recall. Keeping the tail matters for the same
/// reason: the decisive fact in a long memory is as likely to sit at the end
/// as at the start. The elision is logged as well as applied, because a silent
/// drop is what made the original failure invisible.
fn clip_to_token_budget(value: &str, budget: usize) -> String {
    let estimate = estimate_tokens(value);
    if estimate <= budget || budget == 0 {
        return value.to_owned();
    }
    let usable = budget.saturating_sub(estimate_tokens(EMBEDDING_TEXT_ELISION));
    let tail_budget = usable * EMBEDDING_TEXT_TAIL_PERCENT / 100;
    let head_budget = usable - tail_budget;
    let head_end = head_byte_end(value, head_budget);
    let tail_start = tail_byte_start(value, tail_budget);
    eprintln!(
        "[semantic-embed] clipped text: about {estimate} tokens over the {budget} token budget ({} bytes); kept first {head_budget} and last {tail_budget} tokens",
        value.len()
    );
    if tail_start <= head_end {
        return value[..head_end].to_owned();
    }
    format!(
        "{}{}{}",
        &value[..head_end],
        EMBEDDING_TEXT_ELISION,
        &value[tail_start..]
    )
}

/// Configured per-text token budget, with the same fallback
/// `EmbeddingClient::new` applies to unusable values. Batch planning in
/// `background.rs` and the client's own clipping must agree on one budget, so
/// neither side re-derives it.
pub(crate) fn max_text_tokens_of(config: &EmbeddingConfig) -> usize {
    usize::try_from(config.max_text_tokens)
        .ok()
        .filter(|value| *value > 0)
        .unwrap_or(DEFAULT_EMBEDDING_TEXT_TOKENS as usize)
}

/// How many leading texts one embedding request may carry.
///
/// Two caps apply: the caller's backlog query already limits the item count
/// (`embedding.batch_size`), and this adds a token cap so a backlog of
/// full-length memories cannot turn one request into minutes of single-slot
/// server time. The token budget is the fixed `EMBEDDING_BATCH_TEXT_TOKENS`,
/// not a multiple of `max_text_tokens`, so a larger per-text budget cannot
/// lengthen a batch unnoticed. At least one item is always returned, so a
/// single over-budget memory still makes progress instead of stalling the
/// namespace; the backlog is ordered by recency, so the remainder simply
/// waits for the next cycle.
pub(crate) fn embedding_batch_len(texts: &[&str], max_text_tokens: usize) -> usize {
    let token_budget = EMBEDDING_BATCH_TEXT_TOKENS;
    let mut total = 0_usize;
    let mut length = 0_usize;
    for text in texts {
        // A text costs at most the per-text budget once clipped, and the
        // estimate is an upper bound, so this never under-counts the request.
        let cost = estimate_tokens(text).min(max_text_tokens);
        if length > 0 && total.saturating_add(cost) > token_budget {
            break;
        }
        total += cost;
        length += 1;
    }
    length.min(MAX_EMBEDDING_BATCH)
}

/// The single canonical text fed to the embedding model for a memory, shared
/// by the inline post-commit path and the background backfill so a re-embed
/// always produces the same vector.
fn embed_text(content: &str, summary: Option<&str>) -> String {
    match summary {
        Some(summary) if !summary.trim().is_empty() => format!("{content}\n{summary}"),
        _ => content.to_owned(),
    }
}

/// Symmetric int8 quantization. Cosine is scale-invariant, so the per-vector
/// maximum needs no storage: quantizing the query with its own maximum keeps
/// the cosine identical in expectation while every stored row stays exactly
/// `dims` bytes.
pub(crate) fn quantize_int8(vector: &[f32]) -> (Vec<u8>, f64) {
    let max = vector
        .iter()
        .fold(0.0_f32, |acc, value| acc.max(value.abs()));
    if !max.is_finite() || max <= 0.0 {
        return (vec![0; vector.len()], 0.0);
    }
    let mut bytes = Vec::with_capacity(vector.len());
    let mut norm_sq = 0.0_f64;
    for value in vector {
        let quantized = (value / max * 127.0)
            .round()
            .clamp(-127.0, 127.0) as i8;
        bytes.push(quantized as u8);
        norm_sq += f64::from(quantized) * f64::from(quantized);
    }
    (bytes, norm_sq.sqrt())
}

fn encode_f32_blob(vector: &[f32]) -> Vec<u8> {
    let mut bytes = Vec::with_capacity(vector.len() * 4);
    for value in vector {
        bytes.extend_from_slice(&value.to_le_bytes());
    }
    bytes
}

pub(crate) fn vector_norm(vector: &[f32]) -> f64 {
    vector
        .iter()
        .map(|value| f64::from(*value) * f64::from(*value))
        .sum::<f64>()
        .sqrt()
}

/// Dot product of an f32 query against an int8-quantized row, accumulated in
/// four independent f32 lanes. The lane split breaks the scalar add-to-add
/// dependency chain (the scan loop is latency-bound, not throughput-bound, on
/// the K3's in-order cores) and f32 accumulation is plenty for a ranking
/// score: relative error stays ~1e-5 against an f64 reference.
fn dot_query_int8(query: &[f32], stored: &[u8]) -> Option<f64> {
    if query.len() != stored.len() {
        return None;
    }
    let mut acc = [0.0_f32; 4];
    for (q, b) in query.chunks_exact(4).zip(stored.chunks_exact(4)) {
        acc[0] += q[0] * f32::from(b[0] as i8);
        acc[1] += q[1] * f32::from(b[1] as i8);
        acc[2] += q[2] * f32::from(b[2] as i8);
        acc[3] += q[3] * f32::from(b[3] as i8);
    }
    let mut tail = 0.0_f32;
    for (component, byte) in query
        .chunks_exact(4)
        .remainder()
        .iter()
        .zip(stored.chunks_exact(4).remainder())
    {
        tail += component * f32::from(*byte as i8);
    }
    Some(f64::from(acc[0] + acc[1] + acc[2] + acc[3] + tail))
}

/// Cosine between a live f32 query and a stored row. Returns `None` when the
/// row cannot be scored (dimensionality/format mismatch or zero norm) so the
/// scan skips it instead of poisoning the ranking.
///
/// The int8 path (the default storage format and the only one the 100k-row
/// acceptance scan exercises) accumulates through `dot_query_int8`'s f32
/// lanes; the f32 path stays on exact f64 accumulation because its contract
/// is a bit-exact round trip, not a 0.01-tolerance ranking score.
pub(crate) fn cosine_stored(
    query: &[f32],
    query_norm: f64,
    stored: &[u8],
    stored_norm: f64,
    format: i64,
) -> Option<f64> {
    if query_norm <= 0.0 || stored_norm <= 0.0 {
        return None;
    }
    let dot = match format {
        1 => dot_query_int8(query, stored)?,
        2 => {
            if stored.len() != query.len() * 4 {
                return None;
            }
            let mut dot = 0.0_f64;
            for (index, component) in query.iter().enumerate() {
                let mut buffer = [0_u8; 4];
                buffer.copy_from_slice(&stored[index * 4..index * 4 + 4]);
                dot += f64::from(*component) * f64::from(f32::from_le_bytes(buffer));
            }
            dot
        }
        _ => return None,
    };
    let score = dot / (query_norm * stored_norm);
    score.is_finite().then_some(score)
}

/// Reciprocal Rank Fusion over exactly two ranked id lists. A record seen in
/// both lists outranks single-evidence records; ties break on id so results
/// are deterministic.
pub(crate) fn reciprocal_rank_fusion(
    lexical: &[String],
    semantic: &[String],
) -> Vec<(String, f64)> {
    let mut scores: BTreeMap<String, f64> = BTreeMap::new();
    for ids in [lexical, semantic] {
        for (rank, id) in ids.iter().enumerate() {
            *scores.entry(id.clone()).or_insert(0.0) += 1.0 / (RRF_K + rank as f64 + 1.0);
        }
    }
    let mut fused: Vec<(String, f64)> = scores.into_iter().collect();
    fused.sort_by(|left, right| {
        right
            .1
            .partial_cmp(&left.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| left.0.cmp(&right.0))
    });
    fused
}

/// Fuse the lexical cascade output with the semantic stage output. When both
/// lists are present the result is ordered by RRF score (dual-evidence
/// records first) and every returned header carries its RRF score; with only
/// one evidence source the input list is returned untouched so FTS scores
/// and cosine scores survive unchanged.
pub(crate) fn fuse_search_results(lexical: Vec<Value>, semantic: Vec<Value>) -> Vec<Value> {
    if semantic.is_empty() {
        return with_lexical_relevance(lexical);
    }
    if lexical.is_empty() {
        return semantic;
    }
    let lexical_ids = ranked_ids(&lexical);
    let semantic_ids = ranked_ids(&semantic);
    let fused = reciprocal_rank_fusion(&lexical_ids, &semantic_ids);
    let mut headers: BTreeMap<String, Value> = BTreeMap::new();
    for value in lexical.into_iter().chain(semantic.into_iter()) {
        if let Some(id) = value.get("memory_id").and_then(Value::as_str) {
            headers.insert(id.to_owned(), value);
        }
    }
    fused
        .into_iter()
        .filter_map(|(id, score)| {
            headers.remove(&id).map(|mut header| {
                if let Some(object) = header.as_object_mut() {
                    object.insert("score".to_owned(), json!(score));
                    // Semantic evidence already carries its cosine; a record
                    // only the lexical stage ranked is a keyword hit, which is
                    // relevant by definition (its FTS score is not a
                    // comparable similarity).
                    object
                        .entry("relevance".to_owned())
                        .or_insert_with(|| json!(1.0));
                }
                header
            })
        })
        .collect()
}

/// Tag keyword-only evidence with a full relevance score.
fn with_lexical_relevance(values: Vec<Value>) -> Vec<Value> {
    values
        .into_iter()
        .map(|mut value| {
            if let Some(object) = value.as_object_mut() {
                object
                    .entry("relevance".to_owned())
                    .or_insert_with(|| json!(1.0));
            }
            value
        })
        .collect()
}

/// Outcome of the query-side semantic stage. The three states are kept apart
/// because only a stage that actually ran may be memoized: `Unavailable` means
/// vector recall was never attempted (embedding disabled, query too short, or
/// the endpoint config cannot build a client) so the lexical cascade *is* the
/// answer, `Degraded` means it was attempted and failed (timeout, service
/// down, scan or header error) so the lexical cascade is only the MEM-NFR-02
/// keyword fallback, and `Computed` carries the headers to fuse.
#[derive(Debug)]
pub(crate) enum SemanticStage {
    Unavailable,
    Degraded,
    Computed(Vec<Value>),
}

impl SemanticStage {
    /// Number of semantic headers the stage produced (0 unless it computed).
    pub(crate) fn rows(&self) -> usize {
        match self {
            SemanticStage::Computed(values) => values.len(),
            SemanticStage::Unavailable | SemanticStage::Degraded => 0,
        }
    }

    /// Stage label for the `[search-trace]` diagnostics.
    pub(crate) fn label(&self) -> &'static str {
        match self {
            SemanticStage::Unavailable => "unavailable",
            SemanticStage::Degraded => "degraded",
            SemanticStage::Computed(_) => "computed",
        }
    }
}

/// Fuse the lexical cascade with the semantic stage and report whether the
/// fused result is authoritative enough to memoize. A degraded stage still
/// returns its lexical-only result — the MEM-NFR-02 keyword fallback has to
/// answer — but marks it uncacheable: memoizing it would serve the lexical
/// subset as the fused answer until the next write rotates the generation.
pub(crate) fn fuse_semantic_stage(lexical: Vec<Value>, stage: SemanticStage) -> (Vec<Value>, bool) {
    match stage {
        SemanticStage::Unavailable => (lexical, true),
        SemanticStage::Degraded => (lexical, false),
        SemanticStage::Computed(semantic) => (fuse_search_results(lexical, semantic), true),
    }
}

fn ranked_ids(values: &[Value]) -> Vec<String> {
    values
        .iter()
        .filter_map(|value| value.get("memory_id").and_then(Value::as_str))
        .map(str::to_owned)
        .collect()
}

/// Heap entry for the bounded top-N scan. `Ord` encodes the final ranking
/// order — score descending, then memory_id ascending — so a plain
/// `BinaryHeap` roots at the worst retained entry and eviction is a single
/// `candidate < *worst`. The comparison must be on the values, not
/// `to_bits`: bits ordering is not monotonic for negative scores, and the
/// id tie-break has to keep the smaller id. `Eq` is a manual marker because
/// f64 is only `PartialEq`, while the heap needs `Ord` (and therefore `Eq`).
#[derive(PartialEq)]
struct Scored {
    score: f64,
    memory_id: String,
    /// Revision of the vector that produced the score — carried to
    /// `semantic_headers`, which drops the candidate when the authoritative
    /// row has moved on (stale-vector evidence must not surface).
    revision: i64,
}

impl Eq for Scored {}

impl Ord for Scored {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        // Scores are finite (cosine_stored filters non-finite values).
        other
            .score
            .partial_cmp(&self.score)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| self.memory_id.cmp(&other.memory_id))
    }
}

impl PartialOrd for Scored {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

/// One cached embedding row. It mirrors the columns the SQL scan filters on;
/// `semantic_headers` re-validates every scored candidate against the
/// authoritative tables, so a stale snapshot entry can cost a ranking slot
/// but can never surface a deleted, expired, secret, or stale-revision
/// memory.
struct SemanticEntry {
    memory_id: String,
    kind: String,
    model: String,
    dims: i64,
    format: i64,
    norm: f64,
    revision: i64,
    vector: Vec<u8>,
}

impl SemanticEntry {
    /// Rough heap footprint used for the cache budget (payload plus the
    /// string allocations and the struct itself).
    fn heap_bytes(&self) -> usize {
        self.vector.len()
            + self.memory_id.capacity()
            + self.kind.capacity()
            + self.model.capacity()
            + std::mem::size_of::<SemanticEntry>()
    }
}

/// Immutable namespace snapshot published behind an `Arc` so concurrent
/// scans share one allocation and a reload is just a pointer swap.
struct SemanticSnapshot {
    /// Sorted by memory_id (the table's primary key order).
    entries: Vec<SemanticEntry>,
    heap_bytes: usize,
}

/// Bookkeeping for one namespace. `watermark` is the MAX(updated_at) the
/// snapshot was built from; any insert or re-embed moves it and the next
/// lookup triggers a rebuild. `uncacheable` remembers a namespace that
/// measured above the byte budget so it is not retried on every query.
struct SemanticCacheEntry {
    snapshot: Option<Arc<SemanticSnapshot>>,
    watermark: i64,
    uncacheable: Option<i64>,
    reloading: bool,
    last_reload: Option<Instant>,
    last_access: Instant,
}

impl Default for SemanticCacheEntry {
    fn default() -> Self {
        Self {
            snapshot: None,
            watermark: 0,
            uncacheable: None,
            reloading: false,
            last_reload: None,
            last_access: Instant::now(),
        }
    }
}

type NamespaceKey = (String, String, String);

/// Process-wide vector cache, shared across `Store` clones. The 100k-row K3
/// acceptance run showed the SQLCipher row walk dominating search latency —
/// every query re-decrypted the namespace's whole vector payload — so the
/// scan scores an in-memory snapshot and a cheap aggregate query decides
/// when to rebuild it.
pub(crate) struct SemanticIndexCache {
    namespaces: Mutex<BTreeMap<NamespaceKey, SemanticCacheEntry>>,
}

impl Default for SemanticIndexCache {
    fn default() -> Self {
        Self {
            namespaces: Mutex::new(BTreeMap::new()),
        }
    }
}

impl std::fmt::Debug for SemanticIndexCache {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SemanticIndexCache").finish_non_exhaustive()
    }
}

/// Env-gated diagnostics for the semantic index cache and search stages.
/// Set `SEMANTIC_CACHE_TRACE=1` on the daemon to log every watermark probe,
/// reload decision, and load result to stderr — the 100k-row K3 acceptance
/// runs needed exactly this to tell "cache hit" from "silent SQL fallback".
pub(crate) fn cache_trace() -> bool {
    std::env::var_os("SEMANTIC_CACHE_TRACE").is_some()
}

/// Why a snapshot load produced nothing. `Oversized` namespaces are marked
/// uncacheable until their watermark moves (no retry storm of doomed loads);
/// `Error` is transient and simply retries on the next query.
enum SnapshotLoadFailure {
    Oversized,
    Error,
}

impl SemanticIndexCache {
    /// Freshness watermark for the namespace: MAX(updated_at) over its
    /// embedding rows, answered by a rightmost seek on
    /// idx_memory_embeddings_namespace. The earlier COUNT(*)+MAX pair forced
    /// a full walk of the namespace's index entries on every query —
    /// SQLCipher decrypts every page fetch, and the 4MiB per-connection page
    /// cache cannot hold a 100k-entry index, so the probe alone cost a
    /// measurable slice of every steady-state query. MAX alone still catches
    /// every insert and re-embed (each writes now_ms()). Deleting a
    /// non-latest row leaves MAX unchanged, so the snapshot may briefly keep
    /// serving that id — `semantic_headers` revalidates every candidate
    /// against the authoritative row, which turns it into a wasted ranking
    /// slot, never a stale result (the same contract as reload lag).
    fn watermark(connection: &Connection, identity: &MemoryIdentity) -> Option<i64> {
        let result = connection.query_row(
            "SELECT COALESCE(MAX(updated_at),0)
             FROM memory_embeddings
             WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3",
            params![
                identity.account_id,
                identity.agent_id,
                identity.memory_space_id
            ],
            |row| row.get(0),
        );
        match result {
            Ok(value) => Some(value),
            Err(error) => {
                // A failed probe must not poison the cache: report it loudly
                // under trace so "why did this query do a full scan" stays
                // answerable, then let the caller fall back to the SQL scan.
                if cache_trace() {
                    eprintln!("[semantic-cache] watermark query failed: {error}");
                }
                None
            }
        }
    }

    /// Fetch the namespace's embedding rows in primary-key order. Fails with
    /// [`SnapshotLoadFailure::Oversized`] once the row cap is exceeded (the
    /// count itself is the cap check — the watermark probe no longer carries
    /// a COUNT, see [`Self::watermark`]), and with `Error` for transient
    /// statement/decode problems so the next query can retry.
    fn load_snapshot(
        connection: &Connection,
        identity: &MemoryIdentity,
    ) -> Result<SemanticSnapshot, SnapshotLoadFailure> {
        let started = std::time::Instant::now();
        let mut statement = match connection.prepare(
            "SELECT memory_id,revision,kind,dims,format,norm,model,vector
             FROM memory_embeddings
             WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3
             ORDER BY memory_id",
        ) {
            Ok(statement) => statement,
            Err(error) => {
                if cache_trace() {
                    eprintln!("[semantic-cache] load prepare failed: {error}");
                }
                return Err(SnapshotLoadFailure::Error);
            }
        };
        let rows = match statement.query_map(
            params![
                identity.account_id,
                identity.agent_id,
                identity.memory_space_id
            ],
            |row| {
                Ok(SemanticEntry {
                    memory_id: row.get(0)?,
                    revision: row.get(1)?,
                    kind: row.get(2)?,
                    dims: row.get(3)?,
                    format: row.get(4)?,
                    norm: row.get(5)?,
                    model: row.get(6)?,
                    vector: row.get(7)?,
                })
            },
        ) {
            Ok(rows) => rows,
            Err(error) => {
                if cache_trace() {
                    eprintln!("[semantic-cache] load query failed: {error}");
                }
                return Err(SnapshotLoadFailure::Error);
            }
        };
        let mut entries = Vec::new();
        for row in rows {
            match row {
                Ok(entry) => {
                    entries.push(entry);
                    if entries.len() > SEMANTIC_CACHE_MAX_ROWS as usize {
                        if cache_trace() {
                            eprintln!(
                                "[semantic-cache] load oversized: >{SEMANTIC_CACHE_MAX_ROWS} rows"
                            );
                        }
                        return Err(SnapshotLoadFailure::Oversized);
                    }
                }
                Err(error) => {
                    if cache_trace() {
                        eprintln!(
                            "[semantic-cache] load row failed after {} entries: {error}",
                            entries.len()
                        );
                    }
                    return Err(SnapshotLoadFailure::Error);
                }
            }
        }
        let heap_bytes = entries.iter().map(SemanticEntry::heap_bytes).sum();
        if cache_trace() {
            eprintln!(
                "[semantic-cache] load ok: {} entries, {} bytes, {}ms",
                entries.len(),
                heap_bytes,
                started.elapsed().as_millis()
            );
        }
        Ok(SemanticSnapshot { entries, heap_bytes })
    }

    /// Snapshot for the namespace, rebuilt when the watermark moved. Returns
    /// `None` when the namespace is too large to cache or nothing could be
    /// built — callers then run the uncached SQL scan, which is always
    /// correct, just slower.
    fn snapshot(
        &self,
        connection: &Connection,
        identity: &MemoryIdentity,
    ) -> Option<Arc<SemanticSnapshot>> {
        let key: NamespaceKey = (
            identity.account_id.clone(),
            identity.agent_id.clone(),
            identity.memory_space_id.clone(),
        );
        let watermark = Self::watermark(connection, identity)?;
        // All decisions happen under a short lock; the multi-second row load
        // runs with the lock released so concurrent scans never queue on it.
        // Lock poisoning is recovered from rather than propagated: a panicked
        // holder leaves consistent BTreeMap state (mutations here are plain
        // field writes), while giving up would strand `reloading=true`
        // forever and permanently force every query onto the SQL scan.
        let stale = {
            let mut namespaces = self.lock_namespaces();
            let entry = namespaces.entry(key.clone()).or_default();
            entry.last_access = Instant::now();
            if entry.reloading {
                // Another caller is rebuilding; serve the previous snapshot.
                return entry.snapshot.clone();
            }
            if entry.uncacheable == Some(watermark) {
                return None;
            }
            if entry.watermark == watermark {
                return entry.snapshot.clone();
            }
            let small = entry
                .snapshot
                .as_ref()
                .map_or(true, |snapshot| {
                    snapshot.entries.len() <= SEMANTIC_CACHE_ALWAYS_FRESH_ROWS
                });
            let throttled = !small
                && entry.last_reload.is_some_and(|at| {
                    (at.elapsed().as_millis() as u64) < SEMANTIC_CACHE_RELOAD_MIN_INTERVAL_MS
                });
            if throttled {
                if cache_trace() {
                    eprintln!(
                        "[semantic-cache] throttled reload; serving stale (wm={})",
                        watermark
                    );
                }
                return entry.snapshot.clone();
            }
            if cache_trace() {
                eprintln!(
                    "[semantic-cache] reload start (wm={watermark}, published={})",
                    entry.watermark
                );
            }
            entry.reloading = true;
            entry.snapshot.clone()
        };
        let loaded = Self::load_snapshot(connection, identity);
        let oversized = match &loaded {
            Ok(snapshot) => snapshot.heap_bytes > SEMANTIC_CACHE_MAX_TOTAL_BYTES,
            Err(SnapshotLoadFailure::Oversized) => true,
            Err(SnapshotLoadFailure::Error) => false,
        };
        let publishable =
            loaded.ok().filter(|snapshot| snapshot.heap_bytes <= SEMANTIC_CACHE_MAX_TOTAL_BYTES);
        let mut outcome = stale.clone();
        if let Some(snapshot) = publishable {
            let arc = Arc::new(snapshot);
            let mut namespaces = self.lock_namespaces();
            evict_for_budget(&mut namespaces, &key, arc.heap_bytes);
            if let Some(entry) = namespaces.get_mut(&key) {
                // Publish with the pre-load watermark: rows committed
                // while the load ran keep the watermark mismatched, so
                // the next query rebuilds instead of trusting a snapshot
                // that cannot contain them.
                entry.snapshot = Some(Arc::clone(&arc));
                entry.watermark = watermark;
                entry.uncacheable = None;
                entry.reloading = false;
                entry.last_reload = Some(Instant::now());
                if cache_trace() {
                    eprintln!(
                        "[semantic-cache] published wm={watermark} entries={} bytes={}",
                        arc.entries.len(),
                        arc.heap_bytes
                    );
                }
            }
            outcome = Some(arc);
        } else {
            let mut namespaces = self.lock_namespaces();
            if let Some(entry) = namespaces.get_mut(&key) {
                entry.reloading = false;
                entry.last_reload = Some(Instant::now());
                if oversized {
                    entry.uncacheable = Some(watermark);
                    entry.snapshot = None;
                }
                if cache_trace() {
                    eprintln!(
                        "[semantic-cache] load failed (oversized={oversized}); SQL fallback"
                    );
                }
            }
        }
        outcome
    }

    /// Lock the namespace map, recovering from poisoning instead of
    /// propagating it (see `snapshot` for why stranding `reloading=true`
    /// would be worse than trusting map state after a panic).
    fn lock_namespaces(
        &self,
    ) -> std::sync::MutexGuard<'_, BTreeMap<NamespaceKey, SemanticCacheEntry>> {
        match self.namespaces.lock() {
            Ok(guard) => guard,
            Err(poisoned) => {
                if cache_trace() {
                    eprintln!("[semantic-cache] namespaces lock poisoned; recovering");
                }
                poisoned.into_inner()
            }
        }
    }
}

/// Drop least-recently-used snapshots (never `keep`) until `incoming` bytes
/// fit inside the total budget.
fn evict_for_budget(
    namespaces: &mut BTreeMap<NamespaceKey, SemanticCacheEntry>,
    keep: &NamespaceKey,
    incoming: usize,
) {
    let total = namespaces
        .values()
        .filter_map(|entry| entry.snapshot.as_ref().map(|s| s.heap_bytes))
        .sum::<usize>()
        + incoming;
    if total <= SEMANTIC_CACHE_MAX_TOTAL_BYTES {
        return;
    }
    let mut victims: Vec<(Instant, NamespaceKey)> = namespaces
        .iter()
        .filter(|(key, entry)| key != &keep && entry.snapshot.is_some())
        .map(|(key, entry)| (entry.last_access, key.clone()))
        .collect();
    victims.sort_by_key(|(accessed, _)| *accessed);
    let mut over = total - SEMANTIC_CACHE_MAX_TOTAL_BYTES;
    for (_, victim) in victims {
        if over == 0 {
            break;
        }
        if let Some(entry) = namespaces.get_mut(&victim) {
            if let Some(snapshot) = entry.snapshot.take() {
                over = over.saturating_sub(snapshot.heap_bytes);
            }
        }
    }
}

/// Score a snapshot in memory: identical filtering and ranking semantics to
/// the SQL scan, minus the per-row SQLCipher decrypt.
fn scan_entries(
    entries: &[SemanticEntry],
    query: &[f32],
    query_norm: f64,
    kinds: &BTreeSet<String>,
    model: &str,
    limit: usize,
) -> Vec<(String, f64, i64)> {
    let dims = query.len() as i64;
    let mut heap: BinaryHeap<Scored> = BinaryHeap::new();
    for entry in entries {
        if entry.dims != dims || entry.model != model {
            continue;
        }
        if !kinds.is_empty() && !kinds.contains(&entry.kind) {
            continue;
        }
        if let Some(score) = cosine_stored(
            query,
            query_norm,
            &entry.vector,
            entry.norm,
            entry.format,
        ) {
            let candidate = Scored {
                score,
                memory_id: entry.memory_id.clone(),
                revision: entry.revision,
            };
            if heap.len() < limit {
                heap.push(candidate);
            } else if let Some(worst) = heap.peek() {
                if candidate < *worst {
                    heap.pop();
                    heap.push(candidate);
                }
            }
        }
    }
    drain_ranked(heap)
}

/// Drain the bounded top-N heap into score-descending, id-ascending order.
fn drain_ranked(heap: BinaryHeap<Scored>) -> Vec<(String, f64, i64)> {
    let mut entries: Vec<Scored> = heap.into_iter().collect();
    entries.sort_by(|left, right| {
        right
            .score
            .partial_cmp(&left.score)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| left.memory_id.cmp(&right.memory_id))
    });
    entries
        .into_iter()
        .map(|entry| (entry.memory_id, entry.score, entry.revision))
        .collect()
}

/// Best-effort single-memory embedding after the write transaction commits.
/// All embedding and persistence failures degrade to `Ok(false)`: the vector
/// is derived data and the background embedding job is the retry path.
pub(crate) fn index_memory_after_commit(
    store: &Store,
    identity: &MemoryIdentity,
    memory_id: &str,
) -> bool {
    let Ok(config) = store.product_config() else {
        return false;
    };
    index_with_config(store, identity, memory_id, &config.embedding).unwrap_or(false)
}

fn index_with_config(
    store: &Store,
    identity: &MemoryIdentity,
    memory_id: &str,
    config: &EmbeddingConfig,
) -> ServiceResult<bool> {
    if !config.enabled {
        return Ok(false);
    }
    // The narrow inline budget, not the indexing budget: this call runs inside
    // the writer's request (see `EmbeddingConfig::inline_timeout_ms`).
    let Some(client) = EmbeddingClient::new(config, config.inline_timeout_ms as u64) else {
        return Ok(false);
    };
    let connection = store.connection()?;
    let row: Option<(String, Option<String>, i64, String)> = connection
        .query_row(
            "SELECT content,summary,current_revision,sensitivity
             FROM long_term_memories
             WHERE memory_id=?1 AND account_id=?2 AND agent_id=?3 AND memory_space_id=?4",
            params![
                memory_id,
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
                ))
            },
        )
        .optional()
        .map_err(db_error)?;
    let Some((content, summary, revision, sensitivity)) = row else {
        return Ok(false);
    };
    if sensitivity == "secret" {
        return Ok(false);
    }
    let text = embed_text(&content, summary.as_deref());
    // `embed_texts` applies the token budget, so every outbound text is
    // clipped on one path instead of two that can drift apart.
    let vectors = match client.embed_texts(&[text.as_str()], config.dims) {
        Ok(vectors) => vectors,
        Err(error) => {
            // Best-effort by design: the vector is derived data and the
            // background backfill is the retry path. It must not be silent
            // though, because a silent drop is what left over-long memories
            // permanently unindexed.
            eprintln!(
                "[semantic-embed] embedding failed for memory {memory_id} \
                 (inline budget {}ms, backfill will retry): {}",
                config.inline_timeout_ms,
                error.message()
            );
            return Ok(false);
        }
    };
    let Some(vector) = vectors.first() else {
        return Ok(false);
    };
    persist_embedding(
        store,
        identity,
        memory_id,
        revision,
        vector,
        config.model.as_deref().unwrap_or_default(),
        &config.format,
    )
    .map(|persisted| persisted.is_some())
}

/// Upsert one embedding row in its own small transaction. Returns the row's
/// primary key when persisted; `None` means the authoritative record went
/// away or changed revision while the vector was computed.
fn persist_embedding(
    store: &Store,
    identity: &MemoryIdentity,
    memory_id: &str,
    revision: i64,
    vector: &[f32],
    model: &str,
    format: &str,
) -> ServiceResult<Option<String>> {
    let dims = vector.len() as i64;
    if !(1..=8192).contains(&dims) {
        return Ok(None);
    }
    let (blob, norm, format_code) = match format {
        "f32" => (encode_f32_blob(vector), vector_norm(vector), 2_i64),
        _ => {
            let (blob, norm) = quantize_int8(vector);
            (blob, norm, 1_i64)
        }
    };
    if norm <= 0.0 {
        return Ok(None);
    }
    store.ensure_storage_capacity((dims as u64) * 2 + 512)?;
    let mut connection = store.connection()?;
    let transaction = connection
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(db_error)?;
    let row: Option<(i64, String)> = transaction
        .query_row(
            "SELECT current_revision,kind FROM long_term_memories
             WHERE memory_id=?1 AND account_id=?2 AND agent_id=?3 AND memory_space_id=?4",
            params![
                memory_id,
                identity.account_id,
                identity.agent_id,
                identity.memory_space_id
            ],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()
        .map_err(db_error)?;
    let Some((current_revision, kind)) = row else {
        return Ok(None);
    };
    if current_revision != revision {
        return Ok(None);
    }
    let now = now_ms();
    transaction
        .execute(
            "INSERT INTO memory_embeddings(
                memory_id,account_id,agent_id,memory_space_id,kind,revision,dims,format,
                vector,norm,model,created_at,updated_at
             ) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?12)
             ON CONFLICT(memory_id) DO UPDATE SET
               kind=excluded.kind,revision=excluded.revision,dims=excluded.dims,
               format=excluded.format,vector=excluded.vector,norm=excluded.norm,
               model=excluded.model,updated_at=excluded.updated_at",
            params![
                memory_id,
                identity.account_id,
                identity.agent_id,
                identity.memory_space_id,
                kind,
                revision,
                dims,
                format_code,
                blob,
                norm,
                model,
                now
            ],
        )
        .map_err(db_error)?;
    transaction.commit().map_err(db_error)?;
    // The new vector changes fused search output for its namespace; the
    // MEM-NFR-01-01 hot-query cache must not serve pre-embedding results.
    store.search_cache.invalidate();
    Ok(Some(memory_id.to_owned()))
}

impl Store {
    /// Query-side semantic stage: embed the query (with the Qwen3 instruct
    /// prefix), brute-force scan the namespace's stored vectors, and return
    /// the ranked headers wrapped in [`SemanticStage`]. The three states are
    /// what the caller's caching decision hinges on: `Unavailable` means
    /// vector recall was never attempted (disabled, query too short, unusable
    /// endpoint config) so the lexical cascade *is* the answer, `Degraded`
    /// means it was attempted and failed (timeout, service down, scan or
    /// header error) so the lexical cascade is only the MEM-NFR-02 keyword
    /// fallback, and `Computed` carries the headers to fuse.
    /// The per-request timeout is clamped to 1500ms so the whole search
    /// stays inside the 2s budget after the lexical stage has run.
    pub(crate) fn semantic_stage(
        &self,
        identity: &MemoryIdentity,
        query: &str,
        kinds: &BTreeSet<String>,
        limit: usize,
    ) -> SemanticStage {
        let config = match self.product_config() {
            Ok(config) => config,
            Err(_) => return SemanticStage::Unavailable,
        };
        if !config.embedding.enabled || query.trim().is_empty() || query.chars().count() < 3 {
            return SemanticStage::Unavailable;
        }
        let timeout = config.embedding.max_latency_ms.min(1_500) as u64;
        let client = match EmbeddingClient::new(&config.embedding, timeout) {
            Some(client) => client,
            None => return SemanticStage::Unavailable,
        };
        let model = config.embedding.model.clone().unwrap_or_default();
        let query_text = format!("{}{}", config.embedding.query_instruct, query);
        let trace_stages = cache_trace();
        let stage_started = std::time::Instant::now();
        let vectors = match client.embed_texts(&[query_text.as_str()], config.embedding.dims) {
            Ok(vectors) => vectors,
            Err(_) => return SemanticStage::Degraded,
        };
        if trace_stages {
            eprintln!(
                "[semantic-trace] embed {}ms",
                stage_started.elapsed().as_millis()
            );
        }
        let vector = match vectors.first() {
            Some(vector) => vector,
            None => return SemanticStage::Degraded,
        };
        let query_norm = vector_norm(vector);
        if query_norm <= 0.0 {
            return SemanticStage::Degraded;
        }
        let scan_started = std::time::Instant::now();
        let scan_outcome = self.semantic_scan(identity, vector, query_norm, kinds, &model, limit);
        let candidates = match scan_outcome {
            Ok(candidates) => candidates,
            Err(_) => return SemanticStage::Degraded,
        };
        if trace_stages {
            eprintln!(
                "[semantic-trace] scan {}ms ({} candidates)",
                scan_started.elapsed().as_millis(),
                candidates.len()
            );
        }
        let headers_started = std::time::Instant::now();
        let headers = self.semantic_headers(identity, &candidates);
        if trace_stages {
            eprintln!(
                "[semantic-trace] headers {}ms",
                headers_started.elapsed().as_millis()
            );
        }
        match headers {
            Ok(headers) => SemanticStage::Computed(headers),
            Err(_) => SemanticStage::Degraded,
        }
    }
    /// Brute-force cosine scan over the namespace's embedding rows. Only rows
    /// matching the current model, dimensionality, and authoritative revision
    /// are scored, so config switches and pending backfills degrade to "no
    /// semantic evidence" instead of garbage cosines.
    ///
    /// The scan prefers the in-process snapshot cache: the uncached path
    /// re-decrypts the namespace's whole vector payload through SQLCipher on
    /// every query, which the 100k-row K3 acceptance measured as the dominant
    /// search cost. Cached and uncached paths are semantically identical —
    /// the snapshot may lag the table by one reload window, and every scored
    /// candidate is still re-validated against the authoritative row in
    /// `semantic_headers`.
    pub(crate) fn semantic_scan(
        &self,
        identity: &MemoryIdentity,
        query: &[f32],
        query_norm: f64,
        kinds: &BTreeSet<String>,
        model: &str,
        limit: usize,
    ) -> ServiceResult<Vec<(String, f64, i64)>> {
        if query.is_empty() || limit == 0 {
            return Ok(Vec::new());
        }
        let connection = self.connection()?;
        if let Some(snapshot) = self.semantic_cache.snapshot(&connection, identity) {
            return Ok(scan_entries(
                &snapshot.entries,
                query,
                query_norm,
                kinds,
                model,
                limit,
            ));
        }
        if cache_trace() {
            eprintln!("[semantic-trace] scan on SQL fallback path (no snapshot served)");
        }
        let dims = query.len() as i64;
        let mut sql = String::from(
            "SELECT e.memory_id,e.vector,e.norm,e.format,e.revision
             FROM memory_embeddings e
             JOIN long_term_memories l ON l.memory_id=e.memory_id
             WHERE e.account_id=?1 AND e.agent_id=?2 AND e.memory_space_id=?3
               AND l.account_id=?1 AND l.agent_id=?2 AND l.memory_space_id=?3
               AND e.dims=?4 AND e.model=?5 AND e.revision=l.current_revision
               AND (l.expires_at IS NULL OR l.expires_at>?6)
               AND l.sensitivity!='secret'",
        );
        let mut parameters: Vec<rusqlite::types::Value> = vec![
            identity.account_id.clone().into(),
            identity.agent_id.clone().into(),
            identity.memory_space_id.clone().into(),
            dims.into(),
            model.to_string().into(),
            now_ms().into(),
        ];
        if !kinds.is_empty() {
            let placeholders = (0..kinds.len())
                .map(|index| format!("?{}", index + 7))
                .collect::<Vec<_>>()
                .join(",");
            sql.push_str(&format!(" AND e.kind IN ({placeholders})"));
            parameters.extend(kinds.iter().cloned().map(rusqlite::types::Value::Text));
        }
        let mut statement = connection.prepare(&sql).map_err(db_error)?;
        let rows = statement
            .query_map(params_from_iter(parameters.iter()), |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, Vec<u8>>(1)?,
                    row.get::<_, f64>(2)?,
                    row.get::<_, i64>(3)?,
                    row.get::<_, i64>(4)?,
                ))
            })
            .map_err(db_error)?;
        // Bounded heap: the root is the worst retained entry (Ord ranks
        // better-first), so eviction compares one candidate against it and
        // keeps memory flat even when scanning six-figure row counts.
        let mut heap: BinaryHeap<Scored> = BinaryHeap::new();
        for row in rows {
            let (memory_id, blob, norm, format, revision) = row.map_err(db_error)?;
            if let Some(score) = cosine_stored(query, query_norm, &blob, norm, format) {
                let candidate = Scored {
                    score,
                    memory_id,
                    revision,
                };
                if heap.len() < limit {
                    heap.push(candidate);
                } else if let Some(worst) = heap.peek() {
                    if candidate < *worst {
                        heap.pop();
                        heap.push(candidate);
                    }
                }
            }
        }
        Ok(drain_ranked(heap))
    }

    /// Map scan candidates back to response headers under the authoritative
    /// filters (namespace, expiry, non-secret, current-revision vector).
    /// Returns headers in candidate (rank) order with cosine as `score`.
    ///
    /// The query filters ONLY on `memory_id IN (...)` plus non-sargable row
    /// predicates: `memory_id` is the table's global primary key, so the IN
    /// list is answered by one index seek per candidate. Namespace equality
    /// is enforced in Rust instead — with the account/agent/space terms in
    /// the WHERE, SQLite's un-ANALYZEd planner judged the (account, agent,
    /// space, kind, updated_at) namespace index as cheap as the PK seeks,
    /// picked it, and row-fetched all 100k entries (that index carries no
    /// memory_id) — ~1.3s per query on the K3 100k acceptance DB, measured
    /// by the headers stage trace. Each candidate already carries its
    /// vector revision, so staleness is likewise a plain in-memory
    /// comparison against the fetched row.
    pub(crate) fn semantic_headers(
        &self,
        identity: &MemoryIdentity,
        candidates: &[(String, f64, i64)],
    ) -> ServiceResult<Vec<Value>> {
        if candidates.is_empty() {
            return Ok(Vec::new());
        }
        let ids: Vec<&str> = candidates.iter().map(|(id, _, _)| id.as_str()).collect();
        let placeholders = (0..ids.len())
            .map(|index| format!("?{}", index + 1))
            .collect::<Vec<_>>()
            .join(",");
        let now_param = ids.len() + 1;
        let sql = format!(
            "SELECT l.memory_id,l.account_id,l.agent_id,l.memory_space_id,l.kind,l.summary,
                    l.current_revision,l.sync_policy,l.source,l.sensitivity,l.updated_at,l.hlc
             FROM long_term_memories l
             WHERE l.memory_id IN ({placeholders})
               AND (l.expires_at IS NULL OR l.expires_at>?{now})
               AND l.sensitivity!='secret'",
            now = now_param,
        );
        let mut parameters: Vec<rusqlite::types::Value> = ids
            .into_iter()
            .map(|id| rusqlite::types::Value::Text(id.to_owned()))
            .collect();
        parameters.push(now_ms().into());
        let connection = self.connection()?;
        let mut statement = connection.prepare(&sql).map_err(db_error)?;
        let rows = statement
            .query_map(params_from_iter(parameters.iter()), |row| {
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
                        "summary":row.get::<_,Option<String>>(5)?,
                        "revision":row.get::<_,i64>(6)?,
                        "sync_policy":row.get::<_,String>(7)?,
                        "source":row.get::<_,String>(8)?,
                        "sensitivity":row.get::<_,String>(9)?,
                        "updated_at":row.get::<_,i64>(10)?,
                        "hlc":row.get::<_,String>(11)?
                    }),
                ))
            })
            .map_err(db_error)?;
        let mut headers: BTreeMap<String, (bool, Value)> = BTreeMap::new();
        for row in rows {
            let (memory_id, namespace, header) = row.map_err(db_error)?;
            // Namespace isolation is enforced here rather than in the WHERE
            // (see the doc comment): candidate ids come from this
            // namespace's embedding scan, so this is a defense-in-depth
            // check against snapshot-lag id reuse across namespaces.
            let namespace_ok = namespace
                == (
                    identity.account_id.clone(),
                    identity.agent_id.clone(),
                    identity.memory_space_id.clone(),
                );
            headers.insert(memory_id, (namespace_ok, header));
        }
        Ok(candidates
            .iter()
            .filter_map(|(id, score, revision)| {
                let (namespace_ok, header) = headers.get(id)?;
                if !namespace_ok {
                    return None;
                }
                // Stale vector: the authoritative row has moved past the
                // revision this score was computed from — the old text is
                // gone, so the row surfaces with no semantic evidence (the
                // lexical stage still ranks it on its own merits).
                if header.get("revision").and_then(Value::as_i64) != Some(*revision) {
                    return None;
                }
                let mut header = header.clone();
                if let Some(object) = header.as_object_mut() {
                    object.insert("score".to_owned(), json!(score));
                    // Cosine similarity is comparable across queries, so it is
                    // also what a relevance floor must read. The RRF value the
                    // fusion pass writes into "score" is not.
                    object.insert("relevance".to_owned(), json!(score));
                }
                Some(header)
            })
            .collect())
    }

    /// Persist a background batch in one transaction. Each entry re-checks
    /// the authoritative row so a vector computed for revision N never
    /// overwrites a memory that has already moved to revision N+1.
    pub(crate) fn semantic_index_batch(
        &self,
        identity: &MemoryIdentity,
        entries: &[(String, i64, Vec<f32>)],
        model: &str,
        format: &str,
    ) -> ServiceResult<usize> {
        if entries.is_empty() {
            return Ok(0);
        }
        let mut connection = self.connection()?;
        let transaction = connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(db_error)?;
        let now = now_ms();
        let mut persisted = 0_usize;
        for (memory_id, revision, vector) in entries {
            let row: Option<(i64, String)> = transaction
                .query_row(
                    "SELECT current_revision,kind FROM long_term_memories
                     WHERE memory_id=?1 AND account_id=?2 AND agent_id=?3 AND memory_space_id=?4",
                    params![
                        memory_id,
                        identity.account_id,
                        identity.agent_id,
                        identity.memory_space_id
                    ],
                    |row| Ok((row.get(0)?, row.get(1)?)),
                )
                .optional()
                .map_err(db_error)?;
            let Some((current_revision, kind)) = row else {
                continue;
            };
            if current_revision != *revision {
                continue;
            }
            let dims = vector.len() as i64;
            if !(1..=8192).contains(&dims) {
                continue;
            }
            let (blob, norm, format_code) = match format {
                "f32" => (encode_f32_blob(vector), vector_norm(vector), 2_i64),
                _ => {
                    let (blob, norm) = quantize_int8(vector);
                    (blob, norm, 1_i64)
                }
            };
            if norm <= 0.0 {
                continue;
            }
            transaction
                .execute(
                    "INSERT INTO memory_embeddings(
                        memory_id,account_id,agent_id,memory_space_id,kind,revision,dims,format,
                        vector,norm,model,created_at,updated_at
                     ) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?12)
                     ON CONFLICT(memory_id) DO UPDATE SET
                       kind=excluded.kind,revision=excluded.revision,dims=excluded.dims,
                       format=excluded.format,vector=excluded.vector,norm=excluded.norm,
                       model=excluded.model,updated_at=excluded.updated_at",
                    params![
                        memory_id,
                        identity.account_id,
                        identity.agent_id,
                        identity.memory_space_id,
                        kind,
                        revision,
                        dims,
                        format_code,
                        blob,
                        norm,
                        model,
                        now
                    ],
                )
                .map_err(db_error)?;
            persisted += 1;
        }
        transaction.commit().map_err(db_error)?;
        // Batch-persisted vectors change fused search output; see the
        // invalidation note in persist_embedding.
        if persisted > 0 {
            self.search_cache.invalidate();
        }
        Ok(persisted)
    }

    /// Read the texts that need (re-)embedding for one namespace: rows with
    /// no vector, a stale revision, or a model/dims switch. Drives both the
    /// background job and integration tests.
    ///
    /// `since_ms` restricts the scan to rows updated after that instant. The
    /// staleness anti-join (e.revision/model/dims) has no covering index —
    /// checking it row-by-row forces a memory_embeddings lookup (a ~1KB
    /// SQLCipher-decrypted payload) for every candidate — so the background
    /// job passes a recency window computed from its previous successful
    /// pass; 0 scans the whole namespace (first pass, model switch, tests).
    pub(crate) fn semantic_backlog(
        &self,
        identity: &MemoryIdentity,
        config: &EmbeddingConfig,
        limit: i64,
        since_ms: i64,
    ) -> ServiceResult<Vec<(String, i64, String)>> {
        let connection = self.connection()?;
        let mut statement = connection
            .prepare(
                "SELECT l.memory_id,l.current_revision,l.content,l.summary
                 FROM long_term_memories l
                 LEFT JOIN memory_embeddings e ON e.memory_id=l.memory_id
                 WHERE l.account_id=?1 AND l.agent_id=?2 AND l.memory_space_id=?3
                   AND l.sensitivity!='secret'
                   AND l.updated_at > ?7
                   AND (e.memory_id IS NULL OR e.revision!=l.current_revision
                        OR e.model!=?4 OR e.dims!=?5)
                 ORDER BY l.updated_at DESC LIMIT ?6",
            )
            .map_err(db_error)?;
        let rows = statement
            .query_map(
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id,
                    config.model.as_deref().unwrap_or_default(),
                    config.dims,
                    limit,
                    since_ms
                ],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, i64>(1)?,
                        embed_text(
                            &row.get::<_, String>(2)?,
                            row.get::<_, Option<String>>(3)?.as_deref(),
                        ),
                    ))
                },
            )
            .map_err(db_error)?;
        rows.collect::<Result<Vec<_>, _>>().map_err(db_error)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader, Read as _, Write as _};
    use std::net::{TcpListener, TcpStream};
    use std::thread;

    const TEST_DIMS: i64 = 8;

    fn base_config(base_url: &str) -> EmbeddingConfig {
        EmbeddingConfig {
            enabled: true,
            base_url: Some(base_url.to_owned()),
            model: Some("test-embedding".to_owned()),
            ..EmbeddingConfig::default()
        }
    }

    /// Deterministic test embedding: mostly-zero vector with a single 1.0 at
    /// a position derived from the text so cosine is meaningful and stable.
    fn test_vector(text: &str) -> Vec<f32> {
        let mut vector = vec![0.01_f32; TEST_DIMS as usize];
        let position = text.bytes().map(|byte| byte as usize).sum::<usize>()
            % TEST_DIMS as usize;
        vector[position] = 1.0;
        vector
    }

    #[derive(Clone, Copy)]
    enum FakeMode {
        Healthy,
        Status500,
        Blackhole,
    }

    fn spawn_fake_embedding_server(mode: FakeMode) -> (u16, thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let handle = thread::spawn(move || {
            for stream in listener.incoming() {
                let mut stream: TcpStream = match stream {
                    Ok(stream) => stream,
                    Err(_) => return,
                };
                match mode {
                    FakeMode::Blackhole => {
                        thread::sleep(Duration::from_secs(30));
                        return;
                    }
                    FakeMode::Status500 => {
                        // Drain the full request (headers + body) before
                        // answering, or the socket closes with unread data
                        // and the OS turns that into a connection reset
                        // instead of a clean 500.
                        let mut reader = BufReader::new(&mut stream);
                        let mut request_line = String::new();
                        let _ = reader.read_line(&mut request_line);
                        let mut content_length = 0_usize;
                        loop {
                            let mut header = String::new();
                            if reader.read_line(&mut header).unwrap_or(0) == 0 {
                                break;
                            }
                            if header.trim().is_empty() {
                                break;
                            }
                            if let Some(value) = header
                                .to_ascii_lowercase()
                                .strip_prefix("content-length:")
                            {
                                content_length = value.trim().parse().unwrap_or(0);
                            }
                        }
                        let mut body = vec![0_u8; content_length];
                        if content_length > 0 {
                            let _ = reader.read_exact(&mut body);
                        }
                        let _ = stream.write_all(
                            b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                        );
                        continue;
                    }
                    FakeMode::Healthy => {
                        let mut reader = BufReader::new(&mut stream);
                        let mut request_line = String::new();
                        let _ = reader.read_line(&mut request_line);
                        let mut content_length = 0_usize;
                        loop {
                            let mut header = String::new();
                            if reader.read_line(&mut header).unwrap_or(0) == 0 {
                                break;
                            }
                            if header.trim().is_empty() {
                                break;
                            }
                            if let Some(value) = header
                                .to_ascii_lowercase()
                                .strip_prefix("content-length:")
                            {
                                content_length = value.trim().parse().unwrap_or(0);
                            }
                        }
                        let mut body = vec![0_u8; content_length];
                        if content_length > 0 {
                            let _ = reader.read_exact(&mut body);
                        }
                        let request: Value = serde_json::from_slice(&body).unwrap_or(json!({}));
                        let texts: Vec<String> = request
                            .get("input")
                            .and_then(Value::as_array)
                            .map(|items| {
                                items
                                    .iter()
                                    .filter_map(Value::as_str)
                                    .map(str::to_owned)
                                    .collect()
                            })
                            .unwrap_or_default();
                        // Shuffle the data order to prove index-based reassembly.
                        let mut data: Vec<Value> = texts
                            .iter()
                            .enumerate()
                            .map(|(index, text)| {
                                json!({"index": index, "embedding": test_vector(text)})
                            })
                            .collect();
                        data.reverse();
                        let response = serde_json::to_vec(&json!({"data": data})).unwrap();
                        let headers = format!(
                            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                            response.len()
                        );
                        let _ = stream.write_all(headers.as_bytes());
                        let _ = stream.write_all(&response);
                    }
                }
            }
        });
        (port, handle)
    }

    /// Same HTTP shape as the healthy fake, but it records the texts it was
    /// actually asked to embed so a test can assert what left the client.
    fn spawn_capturing_embedding_server() -> (u16, Arc<Mutex<Vec<String>>>, thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let captured: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
        let sink = Arc::clone(&captured);
        let handle = thread::spawn(move || {
            let (mut stream, _) = match listener.accept() {
                Ok(connection) => connection,
                Err(_) => return,
            };
            let mut reader = BufReader::new(&mut stream);
            let mut request_line = String::new();
            let _ = reader.read_line(&mut request_line);
            let mut content_length = 0_usize;
            loop {
                let mut header = String::new();
                if reader.read_line(&mut header).unwrap_or(0) == 0 {
                    break;
                }
                if header.trim().is_empty() {
                    break;
                }
                if let Some(value) = header
                    .to_ascii_lowercase()
                    .strip_prefix("content-length:")
                {
                    content_length = value.trim().parse().unwrap_or(0);
                }
            }
            let mut body = vec![0_u8; content_length];
            if content_length > 0 {
                let _ = reader.read_exact(&mut body);
            }
            let request: Value = serde_json::from_slice(&body).unwrap_or(json!({}));
            let texts: Vec<String> = request
                .get("input")
                .and_then(Value::as_array)
                .map(|items| {
                    items
                        .iter()
                        .filter_map(Value::as_str)
                        .map(str::to_owned)
                        .collect()
                })
                .unwrap_or_default();
            let data: Vec<Value> = texts
                .iter()
                .enumerate()
                .map(|(index, text)| json!({"index": index, "embedding": test_vector(text)}))
                .collect();
            *sink.lock().unwrap() = texts;
            let response = serde_json::to_vec(&json!({"data": data})).unwrap();
            let headers = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                response.len()
            );
            let _ = stream.write_all(headers.as_bytes());
            let _ = stream.write_all(&response);
        });
        (port, captured, handle)
    }

    #[test]
    fn int8_cosine_matches_f64_within_tolerance() {
        let vector: Vec<f32> = (0..256)
            .map(|index| ((index as f32 * 0.137).sin() * 0.9) + 0.05)
            .collect();
        let (bytes, norm) = quantize_int8(&vector);
        let query_norm = vector_norm(&vector);
        let stored = cosine_stored(&vector, query_norm, &bytes, norm, 1).unwrap();
        let reference = {
            let dot: f64 = vector
                .iter()
                .map(|value| f64::from(*value) * f64::from(*value))
                .sum();
            dot / (query_norm * query_norm)
        };
        assert!((stored - reference).abs() <= 0.01);
        // A zero vector cannot be scored.
        assert_eq!(
            cosine_stored(&vector, query_norm, &vec![0; 256], 0.0, 1),
            None
        );
        // Dimensionality mismatch is skipped, not scored.
        assert_eq!(
            cosine_stored(&vector, query_norm, &bytes[..200], norm, 1),
            None
        );
        // f32 storage round-trips exactly (cosine of a vector with itself is
        // 1 up to float rounding: x / (sqrt(x) * sqrt(x)) is not exactly 1).
        let blob = encode_f32_blob(&vector);
        let f32_norm = vector_norm(&vector);
        let round_trip = cosine_stored(&vector, query_norm, &blob, f32_norm, 2).unwrap();
        assert!((round_trip - 1.0).abs() <= 1e-12);
    }

    #[test]
    fn scan_entries_filters_and_ranks_like_the_sql_scan() {
        let query: Vec<f32> = vec![1.0, 0.0, 0.0, 0.0];
        let query_norm = vector_norm(&query);
        let (aligned, aligned_norm) = quantize_int8(&query);
        let (orthogonal, orthogonal_norm) = quantize_int8(&[0.0, 1.0, 0.0, 0.0]);
        // Anti-aligned: scores -1.0; negative scores must not corrupt the
        // heap ordering (a to_bits comparison inverts their order).
        let (opposed, opposed_norm) = quantize_int8(&[-1.0, 0.0, 0.0, 0.0]);
        let entry = |memory_id: &str, kind: &str, model: &str, dims: i64, vector: Vec<u8>, norm| SemanticEntry {
            memory_id: memory_id.to_owned(),
            kind: kind.to_owned(),
            model: model.to_owned(),
            dims,
            format: 1,
            norm,
            revision: 1,
            vector,
        };
        let entries = vec![
            entry("a", "fact", "m", 4, orthogonal.clone(), orthogonal_norm),
            entry("b", "preference", "m", 4, aligned.clone(), aligned_norm),
            // Different model: filtered like the SQL scan's e.model=?.
            entry("c", "preference", "other", 4, aligned.clone(), aligned_norm),
            // Different dimensionality: unscoreable, skipped.
            entry("d", "preference", "m", 8, vec![0; 8], 1.0),
            // Ties "b" on score; id ascending breaks the tie.
            entry("e", "preference", "m", 4, aligned.clone(), aligned_norm),
            entry("f", "fact", "m", 4, opposed.clone(), opposed_norm),
        ];
        let ranked = scan_entries(&entries, &query, query_norm, &BTreeSet::new(), "m", 10);
        let ids: Vec<&str> = ranked.iter().map(|(id, _, _)| id.as_str()).collect();
        assert_eq!(ids, ["b", "e", "a", "f"]);
        // Kind filtering keeps only the preference rows.
        let kinds: BTreeSet<String> = ["preference".to_owned()].into_iter().collect();
        let ranked = scan_entries(&entries, &query, query_norm, &kinds, "m", 10);
        let ids: Vec<&str> = ranked.iter().map(|(id, _, _)| id.as_str()).collect();
        assert_eq!(ids, ["b", "e"]);
        // The bounded heap truncates to `limit` best entries: a mid-scan tie
        // (e arrives after b) must not evict b, and a negative score must not
        // displace a positive one.
        let ranked = scan_entries(&entries, &query, query_norm, &BTreeSet::new(), "m", 1);
        assert_eq!(ranked.len(), 1);
        assert_eq!(ranked[0].0, "b");
        let ranked = scan_entries(&entries, &query, query_norm, &BTreeSet::new(), "m", 3);
        let ids: Vec<&str> = ranked.iter().map(|(id, _, _)| id.as_str()).collect();
        assert_eq!(ids, ["b", "e", "a"]);
        // Negative-vs-negative eviction: a to_bits comparison would rank
        // -0.5 below -1.0 (sign-magnitude bits invert) and keep the worse
        // entry at the heap root.
        let (slightly_opposed, slightly_opposed_norm) = quantize_int8(&[-0.5, 0.866, 0.0, 0.0]);
        let negatives = vec![
            entry("f", "fact", "m", 4, opposed.clone(), opposed_norm),
            entry("g", "fact", "m", 4, slightly_opposed, slightly_opposed_norm),
        ];
        let ranked = scan_entries(&negatives, &query, query_norm, &BTreeSet::new(), "m", 1);
        assert_eq!(ranked.len(), 1);
        assert_eq!(ranked[0].0, "g");
    }

    #[test]
    fn rrf_prefers_dual_evidence_and_breaks_ties_on_id() {
        let lexical = vec!["a".to_owned(), "b".to_owned(), "c".to_owned()];
        let semantic = vec!["b".to_owned(), "d".to_owned()];
        let fused = reciprocal_rank_fusion(&lexical, &semantic);
        assert_eq!(fused[0].0, "b"); // present in both lists
        // Single-evidence records follow by score, then id order on ties.
        let ids: Vec<&str> = fused.iter().map(|(id, _)| id.as_str()).collect();
        assert_eq!(ids, ["b", "a", "d", "c"]);
        // Empty semantic evidence leaves the lexical order untouched.
        let only_lexical = reciprocal_rank_fusion(&lexical, &[]);
        assert_eq!(
            only_lexical
                .iter()
                .map(|(id, _)| id.as_str())
                .collect::<Vec<_>>(),
            ["a", "b", "c"]
        );
    }

    #[test]
    fn fusion_exposes_a_dimension_free_relevance() {
        let lexical = vec![
            json!({"memory_id":"a","score":-3.5}),
            json!({"memory_id":"b","score":12.0}),
        ];
        let semantic = vec![
            json!({"memory_id":"b","score":0.71,"relevance":0.71}),
            json!({"memory_id":"c","score":0.02,"relevance":0.02}),
        ];
        let fused = fuse_search_results(lexical.clone(), semantic);
        let by_id: BTreeMap<String, Value> = fused
            .iter()
            .map(|value| {
                (
                    value["memory_id"].as_str().unwrap().to_owned(),
                    value.clone(),
                )
            })
            .collect();
        // Dual evidence keeps its cosine; the RRF value stays in "score".
        assert_eq!(by_id["b"]["relevance"], json!(0.71));
        assert!(by_id["b"]["score"].as_f64().unwrap() < 0.05);
        // A keyword hit is relevant even when its FTS score is negative.
        assert_eq!(by_id["a"]["relevance"], json!(1.0));
        // A weak semantic-only hit keeps its low cosine so the SDK floor can
        // drop it instead of injecting it.
        assert_eq!(by_id["c"]["relevance"], json!(0.02));
        // Keyword-only responses carry relevance as well.
        let keyword_only = fuse_search_results(lexical, Vec::new());
        assert!(keyword_only
            .iter()
            .all(|value| value["relevance"] == json!(1.0)));
    }

    #[test]
    fn client_parses_and_reassembles_embeddings() {
        let (port, _server) = spawn_fake_embedding_server(FakeMode::Healthy);
        let client = EmbeddingClient::new(
            &base_config(&format!("http://127.0.0.1:{port}/v1")),
            5_000,
        )
        .unwrap();
        let vectors = client
            .embed_texts(&["alpha", "beta", "gamma"], TEST_DIMS)
            .unwrap();
        assert_eq!(vectors.len(), 3);
        assert_eq!(vectors[0], test_vector("alpha"));
        assert_eq!(vectors[2], test_vector("gamma"));
        // Dimensionality mismatch surfaces as a malformed error.
        assert!(client.embed_texts(&["alpha"], TEST_DIMS + 1).is_err());
    }

    #[test]
    fn client_maps_status_and_transport_failures() {
        let (port, _server) = spawn_fake_embedding_server(FakeMode::Status500);
        let client = EmbeddingClient::new(
            &base_config(&format!("http://127.0.0.1:{port}")),
            5_000,
        )
        .unwrap();
        match client.embed_texts(&["alpha"], TEST_DIMS) {
            Err(EmbeddingError::Status(500)) => {}
            other => panic!("expected status error, got {other:?}"),
        }

        // Port 1 on localhost is never listening: connection refused.
        let client = EmbeddingClient::new(&base_config("http://127.0.0.1:1"), 2_000).unwrap();
        match client.embed_texts(&["alpha"], TEST_DIMS) {
            Err(EmbeddingError::Transport(_)) => {}
            other => panic!("expected transport error, got {other:?}"),
        }
    }

    #[test]
    fn client_times_out_on_blackhole_server() {
        let (port, _server) = spawn_fake_embedding_server(FakeMode::Blackhole);
        let client = EmbeddingClient::new(
            &base_config(&format!("http://127.0.0.1:{port}")),
            300,
        )
        .unwrap();
        match client.embed_texts(&["alpha"], TEST_DIMS) {
            Err(EmbeddingError::Timeout) => {}
            other => panic!("expected timeout, got {other:?}"),
        }
    }

    #[test]
    fn from_config_rejects_unusable_endpoints() {
        // The shipped defaults now carry a usable http:// endpoint (and may
        // be force-disabled via AGENT_MEMORY_EMBEDDING_DISABLED), so build
        // the no-endpoint case explicitly instead of relying on the default.
        let mut config = base_config("https://127.0.0.1:18801/v1");
        assert!(EmbeddingClient::new(&config, 1_000).is_none());
        config.base_url = None;
        assert!(EmbeddingClient::new(&config, 1_000).is_none());
        assert!(EmbeddingClient::new(&base_config("http://127.0.0.1:18801/v1"), 1_000).is_some());
    }

    #[test]
    fn token_estimate_is_conservative_for_chinese_and_ascii() {
        // K3 measurement: 4000 Chinese characters cost 2565 tokens (0.64 per
        // character), so charging a full token per character over-estimates on
        // purpose and keeps the estimate on the safe side of the context limit.
        assert_eq!(estimate_tokens(&"远".repeat(1_000)), 1_000);
        assert_eq!(estimate_tokens("abcdef"), 2);
        assert_eq!(estimate_tokens("中文abc"), 3);
        assert_eq!(estimate_tokens(""), 0);
    }

    #[test]
    fn clip_keeps_short_text_untouched() {
        let text = "短记忆".repeat(10);
        assert_eq!(
            clip_to_token_budget(&text, DEFAULT_EMBEDDING_TEXT_TOKENS as usize),
            text
        );
    }

    #[test]
    fn clip_keeps_head_and_tail_within_the_token_budget() {
        let text = format!("{}{}", "甲".repeat(4_000), "乙".repeat(1_000));
        let clipped = clip_to_token_budget(&text, 200);
        assert!(estimate_tokens(&clipped) <= 200);
        assert!(clipped.starts_with(&"甲".repeat(100)));
        // Head and tail split the budget 75/25 after the elision marker.
        assert!(clipped.ends_with(&"乙".repeat(45)));
        assert!(clipped.contains(EMBEDDING_TEXT_ELISION));
        // A 5000-character memory keeps a usable prefix *and* suffix instead of
        // being dropped or reduced to its opening sentence.
        assert!(clipped.chars().count() > 150);
    }

    #[test]
    fn clip_stays_on_utf8_boundaries_for_multibyte_text() {
        let clipped = clip_to_token_budget(&"🧠记忆".repeat(500), 64);
        assert!(estimate_tokens(&clipped) <= 64);
        assert!(clipped
            .chars()
            .all(|character| "🧠记忆[].\n".contains(character)));
    }

    #[test]
    fn batch_length_respects_item_and_token_caps() {
        // Short texts are capped by the caller's item count, not by the budget.
        let short = ["alpha", "beta", "gamma", "delta", "epsilon"];
        assert_eq!(embedding_batch_len(&short, 2_560), 5);

        // The request budget is fixed, so a text at the default per-text budget
        // fills a whole request on its own; the second waits for the next pass.
        let long = "长".repeat(2_560);
        let texts = [long.as_str(), long.as_str()];
        assert_eq!(embedding_batch_len(&texts, 2_560), 1);

        // Two half-budget texts still share one request; the third waits.
        let half = "长".repeat(1_280);
        let pair = [half.as_str(), half.as_str(), half.as_str()];
        assert_eq!(embedding_batch_len(&pair, 1_280), 2);

        // A single over-budget text still makes progress instead of stalling.
        let huge = "长".repeat(50_000);
        assert_eq!(embedding_batch_len(&[huge.as_str()], 2_560), 1);
        assert_eq!(embedding_batch_len(&[], 2_560), 0);
    }

    #[test]
    fn embedding_request_is_clipped_before_transport() {
        let (port, captured, handle) = spawn_capturing_embedding_server();
        let mut config = base_config(&format!("http://127.0.0.1:{port}/v1"));
        config.max_text_tokens = 128;
        let client = EmbeddingClient::new(&config, 1_000).unwrap();
        let text = "长".repeat(4_000);
        let vectors = client.embed_texts(&[text.as_str()], TEST_DIMS).unwrap();
        assert_eq!(vectors.len(), 1);
        let sent = captured.lock().unwrap().clone();
        assert_eq!(sent.len(), 1);
        assert!(estimate_tokens(&sent[0]) <= 128);
        assert!(sent[0].chars().count() < text.chars().count());
        drop(client);
        let _ = handle.join();
    }
}
