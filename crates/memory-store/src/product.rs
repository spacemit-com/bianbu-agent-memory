use crate::{ServiceResult, Store};
use memory_protocol::ServiceError;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use uuid::Uuid;

const PRODUCT_CONFIG_SCHEMA_VERSION: u32 = 1;
const MAX_PRODUCT_CONFIG_BYTES: u64 = 64 * 1024;
pub(crate) const DEFAULT_MAX_DATABASE_BYTES: i64 = 512 * 1024 * 1024;
pub(crate) const DEFAULT_MIN_FREE_BYTES: i64 = 64 * 1024 * 1024;

/// Where semantic indexing is served from since 0.1.17: the spacemit-ai-gateway
/// Embed domain. `EMBEDDING_MODEL` is the Gateway's own model id, which is
/// provisioned by `bianbu-agent-memory-embedding-provision`.
const EMBEDDING_BASE_URL: &str = "http://127.0.0.1:18790/v1/embed";
const EMBEDDING_MODEL: &str = "qwen3-embedding-0.6b-q8_0";

/// What 0.1.9 through 0.1.16 shipped, and therefore what they persisted into
/// `memory.config.json` the first time a device saved its settings. Both halves
/// are dead in 0.1.17: this package no longer runs a llama-server on 18801, and
/// `qwen3-embedding-0.6b` is not a Gateway model id.
const LEGACY_EMBEDDING_BASE_URL: &str = "http://127.0.0.1:18801/v1";
const LEGACY_EMBEDDING_MODEL: &str = "qwen3-embedding-0.6b";

/// Reserved tag a MEM-L3 record carries once the user designates it important.
///
/// Important marking is an attribute of a record, not a content category: the
/// record keeps whatever `kind` it already had (preference, habit, fact) and
/// merely carries this tag. It is deliberately a plain tag and not a
/// `slot:important` slot: `validate_tags` allows at most one `slot:` tag per
/// record, so a slot would collide with the record own field slot.
///
/// `pub` so the constant stays part of the module's documented surface: the
/// Python SDK mirrors it and the mark is enforced through `long_term` tags. It
/// no longer gates an upload scope, because cloud backup is retired.
#[allow(dead_code)]
pub(crate) const IMPORTANT_MEMORY_TAG: &str = "important";

/// Keys 0.1.28 wrote into `memory.config.json` for cloud backup, retired by the
/// 2026-10-16 product change. They are dropped before deserialization so that
/// `deny_unknown_fields` keeps rejecting real typos while a config left behind
/// by the previous package still loads.
///
/// Nothing in this build reads, validates, or writes them back: the next
/// settings write persists a file without these keys.
const RETIRED_BACKUP_KEYS: [&str; 4] = [
    "cloud_backup_enabled",
    "backup_frequency",
    "backup_scopes",
    "max_backup_bytes",
];

fn default_max_database_bytes() -> i64 {
    DEFAULT_MAX_DATABASE_BYTES
}

fn default_min_free_bytes() -> i64 {
    DEFAULT_MIN_FREE_BYTES
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct ProductConfig {
    pub schema_version: u32,
    pub default_account_id: String,
    pub local_device_id: String,
    pub timezone: String,
    pub memory_enabled: bool,
    pub retention_days: i64,
    pub audit_enabled: bool,
    pub sensitive_action_confirmation: String,
    pub preference_auto_apply_enabled: bool,
    pub development_habit_auto_apply_enabled: bool,
    pub recent_turn_limit: i64,
    pub recent_session_limit: i64,
    pub summary_interval_turns: i64,
    pub summary_retention_days: i64,
    pub summary_model: SummaryModelConfig,
    /// Native semantic-search configuration (MEM-NFR-02). Enabled by default
    /// against the on-device embedding server; memoryd embeds non-secret L3
    /// memories and fuses vector recall into long_term.search_headers with
    /// automatic FTS fallback, degrading to lexical-only when the model is
    /// absent.
    #[serde(default)]
    pub embedding: EmbeddingConfig,
    /// Hard local storage ceilings. These are product-level guardrails rather
    /// than per-Agent settings, so one namespace cannot exhaust the device.
    #[serde(default = "default_max_database_bytes")]
    pub max_database_bytes: i64,
    #[serde(default = "default_min_free_bytes")]
    pub min_free_bytes: i64,
}

impl Default for ProductConfig {
    fn default() -> Self {
        Self {
            schema_version: PRODUCT_CONFIG_SCHEMA_VERSION,
            default_account_id: "default".to_owned(),
            local_device_id: "k3-development".to_owned(),
            timezone: "Asia/Shanghai".to_owned(),
            memory_enabled: true,
            retention_days: 0,
            audit_enabled: true,
            sensitive_action_confirmation: "standard".to_owned(),
            preference_auto_apply_enabled: true,
            development_habit_auto_apply_enabled: true,
            recent_turn_limit: 5,
            recent_session_limit: 5,
            summary_interval_turns: 5,
            summary_retention_days: 7,
            summary_model: SummaryModelConfig::default(),
            embedding: EmbeddingConfig::default(),
            max_database_bytes: DEFAULT_MAX_DATABASE_BYTES,
            min_free_bytes: DEFAULT_MIN_FREE_BYTES,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct SummaryModelConfig {
    pub enabled: bool,
    pub base_url: Option<String>,
    pub model: Option<String>,
    pub auth: String,
    pub credential_env: Option<String>,
    pub max_context_tokens: i64,
    pub max_output_tokens: i64,
    pub cpu_threads: i64,
    pub ram_budget_mb: i64,
    pub max_latency_ms: i64,
}

impl Default for SummaryModelConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            base_url: None,
            model: None,
            auth: "none".to_owned(),
            credential_env: None,
            max_context_tokens: 8_192,
            max_output_tokens: 512,
            cpu_threads: 4,
            ram_budget_mb: 4_096,
            max_latency_ms: 15_000,
        }
    }
}

/// Native semantic-search settings. Mirrors the SummaryModelConfig shape:
/// plain http:// endpoints only (the loopback spacemit-ai-gateway Embed
/// domain), every value hot-reloaded per request through the product-config
/// path, and defaults that enable semantic indexing out of the box (see the
/// Default impl for why enabled/base_url/model ship as a triple).
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct EmbeddingConfig {
    pub enabled: bool,
    pub base_url: Option<String>,
    pub model: Option<String>,
    pub dims: i64,
    pub format: String,
    pub batch_size: i64,
    /// Token budget for the single text each embedding request carries. The
    /// server window (`--ctx-size`, pinned to 4096 by the gateway provisioning
    /// step for this model) is a hard limit it answers with HTTP 400 instead of
    /// truncating, so the client clips to this budget before the request leaves
    /// the process. The default is not
    /// window-proportional: prefill cost is superlinear, so the budget is
    /// bounded by `index_timeout_ms` rather than by the window — see
    /// `embedding::DEFAULT_EMBEDDING_TEXT_TOKENS`.
    pub max_text_tokens: i64,
    /// Interactive budget only: the search path clamps to 1500 ms regardless
    /// (MEM-NFR-02's 2 s end-to-end ceiling), so this value never lengthens
    /// queries. Indexing reads `index_timeout_ms` instead.
    pub max_latency_ms: i64,
    /// Per-request read/write timeout for the indexing paths (background
    /// backfill batches and the post-commit single-record embed). A full
    /// 32-record batch on the K3 0.6B CPU takes ~2.5 s, so indexing must not
    /// inherit the interactive budget — with max_latency_ms=1500 large
    /// spaces entered permanent ServerUnavailable retry loops.
    pub index_timeout_ms: i64,
    /// Read/write timeout for the post-commit single-record embed, the only
    /// indexing call that runs inside a writer's request. It deliberately does
    /// not inherit `index_timeout_ms`: the write is already committed when this
    /// call starts, so waiting out the 30 s indexing budget can only make a
    /// caller that gave up first (the MCP tools budget 2 s) report a failure
    /// for a row that exists. Past this bound the vector is left to the
    /// background backfill, which is the retry path a failed embed already
    /// takes.
    pub inline_timeout_ms: i64,
    pub query_instruct: String,
}

impl Default for EmbeddingConfig {
    fn default() -> Self {
        Self {
            // Enabled out of the box against the spacemit-ai-gateway Embed
            // domain on 127.0.0.1:18790, which owns the embedding model's
            // download, load and inference lifecycle (see
            // docs/GATEWAY_EMBEDDING_INTEGRATION.md). All three values must
            // ship together: validate_config requires endpoint+model when
            // enabled, and a missing base_url makes EmbeddingClient::new return
            // None, which silently disables semantic recall. Devices without
            // the gateway or the model degrade gracefully (lexical-only search,
            // deferred indexing), and users can still opt out through the
            // settings-page toggle.
            //
            // `model` is the Gateway's model id, not a free-form label: the
            // vector-staleness anti-join compares it verbatim, so it is what
            // makes a Gateway-side model switch invalidate every stored vector
            // instead of silently mixing two embedding spaces. Two gateway
            // knobs must never be repointed at this workload: embed.backend and
            // embed.default_args are shared with the Agent OS intent module's
            // `bibit-embed` entry, and the provisioning step pins this model's
            // start-up arguments through `extra_args` instead.
            //
            // AGENT_MEMORY_EMBEDDING_DISABLED=1 is the test/ops escape hatch
            // for this *default* only (same pattern as AGENT_MEMORY_CACHE_KB):
            // it keeps test suites hermetic when a real server happens to be
            // listening on the default port, and lets operators suppress the
            // out-of-the-box behavior without editing config files. Explicit
            // embedding sections are unaffected.
            enabled: std::env::var_os("AGENT_MEMORY_EMBEDDING_DISABLED")
                .map(|value| value != "1")
                .unwrap_or(true),
            base_url: Some(EMBEDDING_BASE_URL.to_owned()),
            model: Some(EMBEDDING_MODEL.to_owned()),
            dims: 1024,
            format: "int8".to_owned(),
            batch_size: 32,
            max_text_tokens: crate::embedding::DEFAULT_EMBEDDING_TEXT_TOKENS,
            max_latency_ms: 1500,
            // K3 measurement: one full-budget text (~1800 estimated tokens)
            // costs ~6 s on the 0.6B Q8_0 server, and `embedding_batch_len`
            // caps a batched request at twice the per-text budget, so 30 s
            // covers the worst batch plus queuing behind an in-flight request.
            // The old 10 s only fitted the 32-item probe batch (~2.5 s)
            // because that probe used short texts; once token clipping let
            // long memories reach the server, a single one of them could
            // already trip the timeout and re-enter the permanent
            // ServerUnavailable retry loop of 2026-09-12.
            index_timeout_ms: 30_000,
            // 500 ms: the measured K3 single-record embed is ~110-120 ms, so
            // the common case still lands inline, while a contended gateway
            // can no longer hold the writer for the indexing budget.
            inline_timeout_ms: 500,
            // Qwen3-Embedding's recommended retrieval instruction; kept as a
            // config field so the M6 prefix A/B evaluation can revise it
            // without a code change.
            query_instruct: "Instruct: Given a user query about a person's memories, retrieve the stored memory records that are semantically relevant to the query\nQuery: ".to_owned(),
        }
    }
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ProductConfigPatch {
    pub local_device_id: Option<String>,
    pub timezone: Option<String>,
    pub memory_enabled: Option<bool>,
    pub retention_days: Option<i64>,
    pub audit_enabled: Option<bool>,
    pub sensitive_action_confirmation: Option<String>,
    pub preference_auto_apply_enabled: Option<bool>,
    pub development_habit_auto_apply_enabled: Option<bool>,
    pub recent_turn_limit: Option<i64>,
    pub recent_session_limit: Option<i64>,
    pub summary_interval_turns: Option<i64>,
    pub summary_retention_days: Option<i64>,
    pub summary_model: Option<SummaryModelConfig>,
    pub embedding: Option<EmbeddingConfig>,
    pub max_database_bytes: Option<i64>,
    pub min_free_bytes: Option<i64>,
}

impl ProductConfigPatch {
    pub(crate) fn is_empty(&self) -> bool {
        self.local_device_id.is_none()
            && self.timezone.is_none()
            && self.memory_enabled.is_none()
            && self.retention_days.is_none()
            && self.audit_enabled.is_none()
            && self.sensitive_action_confirmation.is_none()
            && self.preference_auto_apply_enabled.is_none()
            && self.development_habit_auto_apply_enabled.is_none()
            && self.recent_turn_limit.is_none()
            && self.recent_session_limit.is_none()
            && self.summary_interval_turns.is_none()
            && self.summary_retention_days.is_none()
            && self.summary_model.is_none()
            && self.embedding.is_none()
            && self.max_database_bytes.is_none()
            && self.min_free_bytes.is_none()
    }
}

impl Store {
    pub fn configured_local_device_id(&self) -> ServiceResult<String> {
        Ok(self.product_config()?.local_device_id)
    }

    pub(crate) fn initialize_product_config(&self) -> ServiceResult<()> {
        let _guard = self
            .product_config_lock
            .lock()
            .map_err(|_| ServiceError::unavailable("product config lock is poisoned"))?;
        if self.product_config_path.exists() {
            let (config, converged) = read_config_converged(&self.product_config_path)?;
            // `converge_config` only rewrites the in-memory value, and the file
            // is the only copy an operator can read without the daemon: a device
            // upgraded from 0.1.16 kept the retired `18801` endpoint on disk
            // next to a daemon that talks to the Gateway, and nothing but a
            // settings write ever reconciled the two. Startup holds the config
            // lock, so it is the one point where no request can be racing an
            // update -- that is where the file is brought in line.
            if converged {
                // Best effort on purpose: a read-only or foreign-owned config
                // directory must not stop the daemon from starting, and every
                // reader already uses the converged value. `system.health`
                // publishes the effective endpoint and whether the file agrees.
                let _ = write_config(&self.product_config_path, &config);
            }
            return Ok(());
        }
        write_config(&self.product_config_path, &ProductConfig::default())
    }

    /// What the semantic stage will really use, and whether the file on disk
    /// already says so.
    ///
    /// `memory.config.json` is the only copy an operator can read without the
    /// daemon, and convergence lives in memory (see `converge_config`), so the
    /// two disagree until the converged file is persisted. `system.health`
    /// publishes both halves so nobody has to guess which one to believe.
    pub(crate) fn effective_embedding_status(&self) -> ServiceResult<Value> {
        let config = self.product_config()?;
        let persisted_matches = parse_config(&self.product_config_path)
            .map(|persisted| {
                persisted.embedding.base_url == config.embedding.base_url
                    && persisted.embedding.model == config.embedding.model
            })
            .unwrap_or(false);
        Ok(json!({
            "enabled":config.embedding.enabled,
            "base_url":config.embedding.base_url,
            "model":config.embedding.model,
            "dims":config.embedding.dims,
            "config_file_matches":persisted_matches
        }))
    }

    pub(crate) fn product_config(&self) -> ServiceResult<ProductConfig> {
        // MEM-NFR-01-01: the privacy gate in handle() and the semantic stage
        // of every search read this config, and re-reading + re-parsing the
        // file per request was a fixed millisecond slice of every acceptance
        // op. The stamp check (mtime + size, microseconds) preserves the
        // hot-reload contract for both daemon updates and external edits:
        // update_product_config republishes the file through rename, and any
        // other writer changes the mtime.
        let metadata = fs::symlink_metadata(&self.product_config_path)
            .map_err(|_| ServiceError::unavailable("product config is unavailable"))?;
        if !metadata.file_type().is_file() || metadata.len() > MAX_PRODUCT_CONFIG_BYTES {
            return Err(ServiceError::unavailable("product config is unsafe"));
        }
        let stamp = (
            metadata.modified().map_err(|_| {
                ServiceError::unavailable("product config timestamp is unavailable")
            })?,
            metadata.len(),
        );
        let mut cache = self.product_config_cache.lock().map_err(|_| {
            ServiceError::unavailable("product config cache lock is poisoned")
        })?;
        if let Some(config) = cache.hit(&stamp) {
            return Ok(config.clone());
        }
        let config = read_config(&self.product_config_path)?;
        cache.store(stamp, config.clone());
        Ok(config)
    }

    pub(crate) fn update_product_config(
        &self,
        patch: &ProductConfigPatch,
    ) -> ServiceResult<ProductConfig> {
        let _guard = self
            .product_config_lock
            .lock()
            .map_err(|_| ServiceError::unavailable("product config lock is poisoned"))?;
        let mut config = read_config(&self.product_config_path)?;
        if let Some(value) = patch.local_device_id.as_ref() {
            config.local_device_id = value.trim().to_owned();
        }
        if let Some(value) = patch.timezone.as_ref() {
            config.timezone = value.trim().to_owned();
        }
        if let Some(value) = patch.memory_enabled {
            config.memory_enabled = value;
        }
        if let Some(value) = patch.retention_days {
            config.retention_days = value;
        }
        if let Some(value) = patch.audit_enabled {
            config.audit_enabled = value;
        }
        if let Some(value) = patch.sensitive_action_confirmation.as_ref() {
            config.sensitive_action_confirmation = value.trim().to_owned();
        }
        if let Some(value) = patch.preference_auto_apply_enabled {
            config.preference_auto_apply_enabled = value;
        }
        if let Some(value) = patch.development_habit_auto_apply_enabled {
            config.development_habit_auto_apply_enabled = value;
        }
        if let Some(value) = patch.recent_turn_limit {
            config.recent_turn_limit = value;
        }
        if let Some(value) = patch.recent_session_limit {
            config.recent_session_limit = value;
        }
        if let Some(value) = patch.summary_interval_turns {
            config.summary_interval_turns = value;
        }
        if let Some(value) = patch.summary_retention_days {
            config.summary_retention_days = value;
        }
        if let Some(value) = patch.summary_model.as_ref() {
            config.summary_model = value.clone();
        }
        if let Some(value) = patch.embedding.as_ref() {
            config.embedding = value.clone();
        }
        if let Some(value) = patch.max_database_bytes {
            config.max_database_bytes = value;
        }
        if let Some(value) = patch.min_free_bytes {
            config.min_free_bytes = value;
        }
        validate_config(&config)?;
        write_config(&self.product_config_path, &config)?;
        // Embedding settings change fused search output, so the MEM-NFR-01-01
        // hot-query cache must not serve pre-update results. The parsed-config
        // cache above revalidates itself through the new file stamp.
        self.search_cache.invalidate();
        Ok(config)
    }

    pub(crate) fn product_config_json(
        &self,
        account_id: &str,
        device_id: &str,
    ) -> ServiceResult<Value> {
        let config = self.product_config()?;
        let upload_allowed = account_id != "default" && !account_id.starts_with("local:");
        Ok(json!({
            "schema_version":config.schema_version,
            "config_path":self.product_config_path,
            "identity":{
                "account_id":account_id,
                "device_id":device_id,
                "default_account_id":config.default_account_id,
                "configured_device_id":config.local_device_id,
                "timezone":config.timezone,
                "cloud_upload_allowed":upload_allowed
            },
            "memory_enabled":config.memory_enabled,
            "retention_days":config.retention_days,
            "audit_enabled":config.audit_enabled,
            "sensitive_action_confirmation":config.sensitive_action_confirmation,
            "preference_auto_apply_enabled":config.preference_auto_apply_enabled,
            "development_habit_auto_apply_enabled":config.development_habit_auto_apply_enabled,
            "recent_turn_limit":config.recent_turn_limit,
            "recent_session_limit":config.recent_session_limit,
            "summary_interval_turns":config.summary_interval_turns,
            "summary_retention_days":config.summary_retention_days,
            "summary_model":config.summary_model
            ,"embedding":config.embedding
            ,"max_database_bytes":config.max_database_bytes
            ,"min_free_bytes":config.min_free_bytes
        }))
    }
}

/// Parsed product config plus the (mtime, size) stamp it was read under.
/// See `Store::product_config` for why the parse is cached.
#[derive(Debug)]
pub(crate) struct ProductConfigCache {
    stamp: Option<(std::time::SystemTime, u64)>,
    config: Option<ProductConfig>,
}

impl ProductConfigCache {
    pub(crate) fn new() -> Self {
        Self {
            stamp: None,
            config: None,
        }
    }

    fn hit(&self, stamp: &(std::time::SystemTime, u64)) -> Option<&ProductConfig> {
        if self.stamp.as_ref() == Some(stamp) {
            self.config.as_ref()
        } else {
            None
        }
    }

    fn store(&mut self, stamp: (std::time::SystemTime, u64), config: ProductConfig) {
        self.stamp = Some(stamp);
        self.config = Some(config);
    }
}

fn parse_config(path: &Path) -> ServiceResult<ProductConfig> {
    let metadata = fs::symlink_metadata(path)
        .map_err(|_| ServiceError::unavailable("product config is unavailable"))?;
    if !metadata.file_type().is_file() || metadata.len() > MAX_PRODUCT_CONFIG_BYTES {
        return Err(ServiceError::unavailable("product config is unsafe"));
    }
    let encoded =
        fs::read(path).map_err(|_| ServiceError::unavailable("product config is unreadable"))?;
    let value: Value = serde_json::from_slice(&encoded)
        .map_err(|_| ServiceError::invalid("product config is malformed"))?;
    read_config_value(value)
}

fn read_config_value(mut value: Value) -> ServiceResult<ProductConfig> {
    // Retired cloud-backup keys are dropped before the typed parse so the
    // `deny_unknown_fields` contract still catches genuine typos while a file
    // written by 0.1.28 keeps loading. They are not carried in the struct and
    // therefore disappear from the file on the next settings write.
    if let Some(object) = value.as_object_mut() {
        for key in RETIRED_BACKUP_KEYS {
            object.remove(key);
        }
    }
    serde_json::from_value(value).map_err(|_| ServiceError::invalid("product config is malformed"))
}

/// Parse the file and converge everything an earlier package owned, reporting
/// whether converging changed anything.
fn read_config_converged(path: &Path) -> ServiceResult<(ProductConfig, bool)> {
    let mut config = parse_config(path)?;
    let persisted = config.clone();
    converge_config(&mut config);
    validate_config(&config)?;
    let converged = config != persisted;
    Ok((config, converged))
}

fn read_config(path: &Path) -> ServiceResult<ProductConfig> {
    Ok(read_config_converged(path)?.0)
}

/// Move values a previous package shipped but this one no longer owns onto what
/// this device actually serves.
fn converge_config(config: &mut ProductConfig) {
    // 0.1.17 moved semantic indexing onto the Gateway, but every device that
    // saved its settings between 0.1.9 and 0.1.16 persisted the old endpoint
    // and the old model *label*. The section is explicit, so the new default
    // would never apply and the device would silently fall back to lexical
    // recall -- with the old server retired, nothing would serve 18801 any
    // more. Converge exactly that shipped pair; anything else is an operator's
    // deliberate choice and is left alone. The in-memory value is what every
    // reader and the settings page see.
    converged_embedding(&mut config.embedding);
}

fn write_config(path: &Path, config: &ProductConfig) -> ServiceResult<()> {
    validate_config(config)?;
    let parent = path
        .parent()
        .ok_or_else(|| ServiceError::invalid("product config path has no parent"))?;
    fs::create_dir_all(parent)
        .map_err(|_| ServiceError::unavailable("product config directory is unavailable"))?;
    let temporary = temporary_path(path);
    let encoded = serde_json::to_vec_pretty(config)
        .map_err(|_| ServiceError::unavailable("product config serialization failed"))?;
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temporary)
        .map_err(|_| ServiceError::unavailable("product config temporary file is unavailable"))?;
    let result = (|| -> ServiceResult<()> {
        file.write_all(&encoded)
            .map_err(|_| ServiceError::unavailable("product config write failed"))?;
        file.write_all(b"\n")
            .map_err(|_| ServiceError::unavailable("product config write failed"))?;
        file.sync_all()
            .map_err(|_| ServiceError::unavailable("product config sync failed"))?;
        fs::rename(&temporary, path)
            .map_err(|_| ServiceError::unavailable("product config publish failed"))?;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))
            .map_err(|_| ServiceError::unavailable("product config permissions failed"))?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

fn temporary_path(path: &Path) -> PathBuf {
    let name = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("memory.config.json");
    path.with_file_name(format!(".{name}.{}.tmp", Uuid::new_v4()))
}

fn validate_config(config: &ProductConfig) -> ServiceResult<()> {
    if config.schema_version != PRODUCT_CONFIG_SCHEMA_VERSION {
        return Err(ServiceError::invalid(
            "unsupported product config schema_version",
        ));
    }
    if config.default_account_id != "default" {
        return Err(ServiceError::invalid("default_account_id must be default"));
    }
    if !memory_core::valid_identifier(&config.local_device_id) {
        return Err(ServiceError::invalid("local_device_id is invalid"));
    }
    if !valid_timezone(&config.timezone) {
        return Err(ServiceError::invalid(
            "timezone must be UTC or an IANA-style name",
        ));
    }
    if !matches!(config.retention_days, 0 | 7 | 30 | 90) {
        return Err(ServiceError::invalid(
            "retention_days must be 0, 7, 30, or 90",
        ));
    }
    if !matches!(
        config.sensitive_action_confirmation.as_str(),
        "loose" | "standard" | "strict"
    ) {
        return Err(ServiceError::invalid(
            "sensitive_action_confirmation must be loose, standard, or strict",
        ));
    }
    if !(1..=50).contains(&config.recent_turn_limit)
        || !(1..=20).contains(&config.recent_session_limit)
        || !(1..=50).contains(&config.summary_interval_turns)
        || !(1..=365).contains(&config.summary_retention_days)
    {
        return Err(ServiceError::invalid(
            "invalid conversation window configuration",
        ));
    }
    validate_summary_model(&config.summary_model)
        .and_then(|_| validate_embedding(&config.embedding))
        .and_then(|_| validate_storage_limits(config))
}

fn validate_storage_limits(config: &ProductConfig) -> ServiceResult<()> {
    const MIN_DATABASE_LIMIT: i64 = 16 * 1024 * 1024;
    const MAX_DATABASE_LIMIT: i64 = 8 * 1024 * 1024 * 1024;
    const MAX_FREE_LIMIT: i64 = 8 * 1024 * 1024 * 1024;
    if !(MIN_DATABASE_LIMIT..=MAX_DATABASE_LIMIT).contains(&config.max_database_bytes)
        || !(0..=MAX_FREE_LIMIT).contains(&config.min_free_bytes)
    {
        return Err(ServiceError::invalid("invalid local storage limits"));
    }
    Ok(())
}

/// Repoint the shipped legacy embedding endpoint at the Gateway.
///
/// Only the exact pair 0.1.9-0.1.16 wrote is touched, so a device whose
/// operator pointed `base_url` somewhere else (a self-hosted llama-server, a
/// different port) keeps that endpoint and its vectors: the model id, and
/// therefore vector staleness, stay under their control.
fn converged_embedding(config: &mut EmbeddingConfig) {
    if config.base_url.as_deref() == Some(LEGACY_EMBEDDING_BASE_URL)
        && config.model.as_deref() == Some(LEGACY_EMBEDDING_MODEL)
    {
        config.base_url = Some(EMBEDDING_BASE_URL.to_owned());
        config.model = Some(EMBEDDING_MODEL.to_owned());
    }
}

fn validate_summary_model(config: &SummaryModelConfig) -> ServiceResult<()> {
    if !matches!(config.auth.as_str(), "none" | "bearer_env" | "api_key_env") {
        return Err(ServiceError::invalid("invalid summary model auth mode"));
    }
    if let Some(base_url) = config.base_url.as_deref() {
        if base_url.len() > 2_048 || !base_url.starts_with("http://") {
            return Err(ServiceError::invalid(
                "summary model base_url must use local/trusted http://",
            ));
        }
    }
    if config
        .model
        .as_deref()
        .is_some_and(|value| value.is_empty() || value.len() > 256)
    {
        return Err(ServiceError::invalid("summary model name is invalid"));
    }
    if config.auth != "none"
        && !config
            .credential_env
            .as_deref()
            .is_some_and(valid_environment_name)
    {
        return Err(ServiceError::invalid(
            "credential_env is required for the selected auth mode",
        ));
    }
    if config.enabled && (config.base_url.is_none() || config.model.is_none()) {
        return Err(ServiceError::invalid(
            "enabled summary model requires base_url and model",
        ));
    }
    if !(1_024..=131_072).contains(&config.max_context_tokens)
        || !(64..=8_192).contains(&config.max_output_tokens)
        || !(1..=128).contains(&config.cpu_threads)
        || !(512..=262_144).contains(&config.ram_budget_mb)
        || !(100..=600_000).contains(&config.max_latency_ms)
    {
        return Err(ServiceError::invalid(
            "invalid summary model resource budget",
        ));
    }
    Ok(())
}

fn validate_embedding(config: &EmbeddingConfig) -> ServiceResult<()> {
    if let Some(base_url) = config.base_url.as_deref() {
        if base_url.len() > 2_048 || !base_url.starts_with("http://") {
            return Err(ServiceError::invalid(
                "embedding base_url must use local/trusted http://",
            ));
        }
    }
    if config
        .model
        .as_deref()
        .is_some_and(|value| value.is_empty() || value.len() > 256)
    {
        return Err(ServiceError::invalid("embedding model name is invalid"));
    }
    if config.enabled && (config.base_url.is_none() || config.model.is_none()) {
        return Err(ServiceError::invalid(
            "enabled embedding requires base_url and model",
        ));
    }
    if !(64..=8_192).contains(&config.dims)
        || !matches!(config.format.as_str(), "int8" | "f32")
        || !(1..=128).contains(&config.batch_size)
        || !(64..=8_192).contains(&config.max_text_tokens)
        || !(100..=60_000).contains(&config.max_latency_ms)
        || !(100..=600_000).contains(&config.index_timeout_ms)
        || !(100..=2_000).contains(&config.inline_timeout_ms)
        || config.query_instruct.len() > 512
    {
        return Err(ServiceError::invalid("invalid embedding configuration"));
    }
    Ok(())
}

fn valid_timezone(value: &str) -> bool {
    if value == "UTC" {
        return true;
    }
    !value.is_empty()
        && value.len() <= 64
        && value.contains('/')
        && value.split('/').all(|part| {
            !part.is_empty()
                && part
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'+'))
        })
}

fn valid_environment_name(value: &str) -> bool {
    let mut bytes = value.bytes();
    bytes
        .next()
        .is_some_and(|byte| byte.is_ascii_alphabetic() || byte == b'_')
        && bytes.all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_are_local_and_resource_bounded() {
        let config = ProductConfig::default();
        validate_config(&config).unwrap();
        assert_eq!(config.default_account_id, "default");
        // Cloud backup is retired: the serialized default carries no backup
        // key at all, so a fresh install never advertises it.
        let serialized = serde_json::to_value(&config).unwrap();
        for key in RETIRED_BACKUP_KEYS {
            assert!(serialized.get(key).is_none(), "{key} must not be written");
        }
        assert_eq!(config.recent_turn_limit, 5);
        assert_eq!(config.summary_interval_turns, 5);
        assert_eq!(config.summary_retention_days, 7);
        assert!(!config.summary_model.enabled);
        assert_eq!(config.max_database_bytes, DEFAULT_MAX_DATABASE_BYTES);
    }

    #[test]
    fn enabled_model_requires_endpoint_and_model() {
        let mut config = ProductConfig::default();
        config.summary_model.enabled = true;
        assert!(validate_config(&config).is_err());
        config.summary_model.base_url = Some("http://127.0.0.1:8080/v1".to_owned());
        config.summary_model.model = Some("qwen2.5-1.5b-instruct".to_owned());
        assert!(validate_config(&config).is_ok());
    }

    #[test]
    fn storage_limits_are_bounded_and_legacy_configs_get_defaults() {
        let mut config = ProductConfig::default();
        config.max_database_bytes = 15 * 1024 * 1024;
        assert!(validate_config(&config).is_err());
        config.max_database_bytes = DEFAULT_MAX_DATABASE_BYTES;

        let legacy = serde_json::json!({
            "schema_version": 1,
            "default_account_id": "default",
            "local_device_id": "k3-development",
            "timezone": "UTC",
            "memory_enabled": true,
            "retention_days": 0,
            "audit_enabled": true,
            "sensitive_action_confirmation": "standard",
            "cloud_backup_enabled": false,
            "backup_frequency": "weekly",
            "backup_scopes": ["preferences"],
            "preference_auto_apply_enabled": true,
            "development_habit_auto_apply_enabled": true,
            "recent_turn_limit": 5,
            "recent_session_limit": 5,
            "summary_interval_turns": 5,
            "summary_retention_days": 7,
            "summary_model": {}
        });
        // The typed parse goes through the same retired-key filter as the file
        // path; a bare `from_value` would (correctly) reject the dead keys.
        let restored = read_config_value(legacy).unwrap();
        assert_eq!(restored.max_database_bytes, DEFAULT_MAX_DATABASE_BYTES);
        assert_eq!(restored.min_free_bytes, DEFAULT_MIN_FREE_BYTES);
        validate_config(&restored).unwrap();
    }

    #[test]
    fn a_config_written_by_0_1_28_still_loads_and_drops_its_retired_keys() {
        // A test device that upgraded in place still has this section on disk.
        // `ProductConfig` denies unknown fields, so the retired keys are
        // stripped before the typed parse instead of becoming a start failure.
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("memory.config.json");
        let mut legacy = serde_json::json!({
            "schema_version": 1,
            "default_account_id": "default",
            "local_device_id": "k3-development",
            "timezone": "Asia/Shanghai",
            "memory_enabled": true,
            "retention_days": 30,
            "audit_enabled": true,
            "sensitive_action_confirmation": "standard",
            "cloud_backup_enabled": false,
            "backup_frequency": "weekly",
            "backup_scopes": ["preferences", "development_habits", "schedule_habits"],
            "preference_auto_apply_enabled": true,
            "development_habit_auto_apply_enabled": true,
            "recent_turn_limit": 5,
            "recent_session_limit": 5,
            "summary_interval_turns": 5,
            "summary_retention_days": 7,
            "summary_model": {},
            "max_database_bytes": 536870912,
            "max_backup_bytes": 536870912,
            "min_free_bytes": 67108864
        });
        std::fs::write(&path, serde_json::to_vec(&legacy).unwrap()).unwrap();
        let loaded = read_config(&path).unwrap();
        assert_eq!(loaded.retention_days, 30);
        let serialized = serde_json::to_value(&loaded).unwrap();
        for key in RETIRED_BACKUP_KEYS {
            assert!(serialized.get(key).is_none(), "{key} must not survive a parse");
        }

        // Reading alone does not rewrite the file; the retired keys disappear
        // the next time a setting is written.
        std::fs::write(&path, serde_json::to_vec(&legacy).unwrap()).unwrap();
        let (_, converged) = read_config_converged(&path).unwrap();
        assert!(!converged);
        let persisted: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(persisted["backup_frequency"], "weekly");

        // Any legacy value is tolerated, including one 0.1.28 would have
        // rejected: the keys no longer take part in validation at all.
        legacy["backup_frequency"] = serde_json::json!("hourly");
        legacy["backup_scopes"] = serde_json::json!(["bogus"]);
        std::fs::write(&path, serde_json::to_vec(&legacy).unwrap()).unwrap();
        assert!(read_config(&path).is_ok());

        // A genuinely unknown key is still rejected: tolerating four retired
        // names must not turn the config into a free-form document.
        legacy["memroy_enabled"] = serde_json::json!(true);
        std::fs::write(&path, serde_json::to_vec(&legacy).unwrap()).unwrap();
        assert_eq!(read_config(&path).unwrap_err().code, "InvalidArgument");
    }

    #[test]
    fn legacy_embedding_endpoint_converges_to_the_gateway() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("memory.config.json");
        let mut persisted = serde_json::to_value(ProductConfig::default()).unwrap();

        // Every device that saved settings between 0.1.9 and 0.1.16 persisted
        // exactly this pair. The section is explicit, so without convergence the
        // new default would never apply and the upgrade would look successful
        // while silently serving lexical-only recall from a retired endpoint.
        persisted["embedding"]["base_url"] = serde_json::json!(LEGACY_EMBEDDING_BASE_URL);
        persisted["embedding"]["model"] = serde_json::json!(LEGACY_EMBEDDING_MODEL);
        std::fs::write(&path, serde_json::to_vec(&persisted).unwrap()).unwrap();
        let loaded = read_config(&path).unwrap();
        assert_eq!(loaded.embedding.base_url.as_deref(), Some(EMBEDDING_BASE_URL));
        assert_eq!(loaded.embedding.model.as_deref(), Some(EMBEDDING_MODEL));
        // Everything else about the section is the device's, not the default's.
        assert_eq!(loaded.embedding.dims, 1024);
        assert_eq!(loaded.embedding.max_text_tokens, 2_560);

        // A self-hosted endpoint keeps its values: the operator owns both the
        // server and the model id that drives vector staleness.
        persisted["embedding"]["base_url"] = serde_json::json!("http://127.0.0.1:18901/v1");
        persisted["embedding"]["model"] = serde_json::json!("my-own-embedding");
        std::fs::write(&path, serde_json::to_vec(&persisted).unwrap()).unwrap();
        let loaded = read_config(&path).unwrap();
        assert_eq!(
            loaded.embedding.base_url.as_deref(),
            Some("http://127.0.0.1:18901/v1")
        );
        assert_eq!(loaded.embedding.model.as_deref(), Some("my-own-embedding"));

        // Half a match is not the shipped default either: keeping the old port
        // but swapping the model is a deliberate pairing too.
        persisted["embedding"]["base_url"] = serde_json::json!(LEGACY_EMBEDDING_BASE_URL);
        std::fs::write(&path, serde_json::to_vec(&persisted).unwrap()).unwrap();
        let loaded = read_config(&path).unwrap();
        assert_eq!(
            loaded.embedding.base_url.as_deref(),
            Some(LEGACY_EMBEDDING_BASE_URL)
        );
        assert_eq!(loaded.embedding.model.as_deref(), Some("my-own-embedding"));
    }

    #[test]
    fn convergence_is_reported_separately_from_a_reader_s_view() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("memory.config.json");
        let mut persisted = serde_json::to_value(ProductConfig::default()).unwrap();
        persisted["embedding"]["base_url"] = serde_json::json!(LEGACY_EMBEDDING_BASE_URL);
        persisted["embedding"]["model"] = serde_json::json!(LEGACY_EMBEDDING_MODEL);
        std::fs::write(&path, serde_json::to_vec(&persisted).unwrap()).unwrap();

        // Readers only ever see the converged value, and the file still carries
        // what 0.1.16 wrote; `initialize_product_config` is what persists it.
        let (loaded, converged) = read_config_converged(&path).unwrap();
        assert!(converged);
        assert_eq!(loaded.embedding.base_url.as_deref(), Some(EMBEDDING_BASE_URL));
        assert_eq!(
            parse_config(&path).unwrap().embedding.base_url.as_deref(),
            Some(LEGACY_EMBEDDING_BASE_URL)
        );

        // An endpoint the operator chose is not "converged", so startup has
        // nothing to rewrite.
        let mut configured = ProductConfig::default();
        configured.embedding.base_url = Some("http://127.0.0.1:18901/v1".to_owned());
        write_config(&path, &configured).unwrap();
        let (_, converged) = read_config_converged(&path).unwrap();
        assert!(!converged);

        // Retired cloud-backup keys never make the config look "converged":
        // convergence is about endpoint repair, and dropping a dead key is
        // not a rewrite startup has to perform.
        let mut retired = serde_json::to_value(ProductConfig::default()).unwrap();
        retired["backup_frequency"] = serde_json::json!("monthly");
        std::fs::write(&path, serde_json::to_vec(&retired).unwrap()).unwrap();
        let (loaded, converged) = read_config_converged(&path).unwrap();
        assert!(!converged);
        assert!(serde_json::to_value(&loaded)
            .unwrap()
            .get("backup_frequency")
            .is_none());
    }

    #[test]
    fn summary_model_limits_reject_unsafe_values() {
        let mut config = ProductConfig::default();
        config.summary_model.max_output_tokens = 8_193;
        assert!(validate_config(&config).is_err());
        config.summary_model.max_output_tokens = 512;
        config.summary_model.enabled = true;
        config.summary_model.base_url = Some("http://127.0.0.1:8080/v1".to_owned());
        config.summary_model.model = Some("summary".to_owned());
        assert!(validate_config(&config).is_ok());
    }

    #[test]
    fn embedding_defaults_are_enabled_and_valid() {
        // The default consults AGENT_MEMORY_EMBEDDING_DISABLED (the test/ops
        // escape hatch), so assert against the live environment instead of a
        // hardcoded bool: unset (production) expects enabled, set to "1"
        // (hermetic test run) expects disabled. The endpoint/model triple
        // ships either way.
        let disabled_by_env = std::env::var_os("AGENT_MEMORY_EMBEDDING_DISABLED")
            .is_some_and(|value| value == "1");
        let config = ProductConfig::default();
        assert_eq!(config.embedding.enabled, !disabled_by_env);
        // The default must be a complete triple: enabled without an endpoint
        // fails validation, and enabled without base_url silently no-ops in
        // EmbeddingClient::new.
        assert_eq!(
            config.embedding.base_url.as_deref(),
            Some("http://127.0.0.1:18790/v1/embed")
        );
        assert_eq!(
            config.embedding.model.as_deref(),
            Some("qwen3-embedding-0.6b-q8_0")
        );
        assert_eq!(config.embedding.dims, 1024);
        assert_eq!(config.embedding.format, "int8");
        validate_config(&config).unwrap();

        // Legacy config files predate the embedding section entirely; the
        // serde(default) backfill restores the enabled default, which is the
        // deliberate upgrade semantics: existing deployments activate
        // semantic indexing once the gateway provisioning step has made the
        // model available, and users who want the old behavior flip the
        // settings-page toggle (persisting an explicit disabled section).
        let legacy = serde_json::json!({
            "schema_version": 1,
            "default_account_id": "default",
            "local_device_id": "k3-development",
            "timezone": "UTC",
            "memory_enabled": true,
            "retention_days": 0,
            "audit_enabled": true,
            "sensitive_action_confirmation": "standard",
            "cloud_backup_enabled": false,
            "backup_frequency": "weekly",
            "backup_scopes": ["preferences"],
            "preference_auto_apply_enabled": true,
            "development_habit_auto_apply_enabled": true,
            "recent_turn_limit": 5,
            "recent_session_limit": 5,
            "summary_interval_turns": 5,
            "summary_retention_days": 7,
            "summary_model": {}
        });
        let restored = read_config_value(legacy).unwrap();
        assert_eq!(restored.embedding.enabled, !disabled_by_env);
        assert_eq!(
            restored.embedding.base_url.as_deref(),
            Some("http://127.0.0.1:18790/v1/embed")
        );
        validate_config(&restored).unwrap();
    }

    #[test]
    fn embedding_index_timeout_defaults_and_backfills_older_sections() {
        let config = ProductConfig::default();
        assert_eq!(config.embedding.index_timeout_ms, 30_000);
        assert_eq!(config.embedding.inline_timeout_ms, 500);
        assert_eq!(config.embedding.max_text_tokens, 2_560);

        // Config files written before the field existed (the production K3
        // shape: an embedding section with max_latency_ms only) deserialize
        // with the new default instead of being rejected.
        let pre_field = serde_json::json!({
            "enabled": true,
            "base_url": "http://127.0.0.1:18801/v1",
            "model": "qwen3-embedding-0.6b",
            "dims": 1024,
            "format": "int8",
            "batch_size": 32,
            "max_latency_ms": 1500,
            "query_instruct": ""
        });
        let restored: EmbeddingConfig = serde_json::from_value(pre_field).unwrap();
        assert_eq!(restored.index_timeout_ms, 30_000);
        assert_eq!(restored.inline_timeout_ms, 500);
        assert_eq!(restored.max_text_tokens, 2_560);
    }

    #[test]
    fn embedding_validation_rejects_unsafe_values() {
        let mut config = ProductConfig::default();
        // Pin the toggle explicitly: the default may be flipped off by
        // AGENT_MEMORY_EMBEDDING_DISABLED during hermetic test runs, and this
        // suite must exercise the enabled-without-endpoint rejection either
        // way. Clear the endpoint/model the defaults now carry.
        config.embedding.enabled = true;
        config.embedding.base_url = None;
        config.embedding.model = None;
        assert!(validate_config(&config).is_err()); // missing endpoint/model

        config.embedding.base_url = Some("https://127.0.0.1:18801/v1".to_owned());
        config.embedding.model = Some("qwen3-embedding".to_owned());
        assert!(validate_config(&config).is_err()); // https is rejected

        config.embedding.base_url = Some("http://127.0.0.1:18801/v1".to_owned());
        assert!(validate_config(&config).is_ok());

        config.embedding.dims = 8_193;
        assert!(validate_config(&config).is_err());
        config.embedding.dims = 1024;
        config.embedding.format = "int4".to_owned();
        assert!(validate_config(&config).is_err());
        config.embedding.format = "int8".to_owned();
        config.embedding.query_instruct = "x".repeat(513);
        assert!(validate_config(&config).is_err());
        config.embedding.query_instruct = String::new();
        config.embedding.index_timeout_ms = 99;
        assert!(validate_config(&config).is_err());
        config.embedding.index_timeout_ms = 600_001;
        assert!(validate_config(&config).is_err());
        config.embedding.index_timeout_ms = 30_000;
        config.embedding.inline_timeout_ms = 99;
        assert!(validate_config(&config).is_err());
        config.embedding.inline_timeout_ms = 2_001;
        assert!(validate_config(&config).is_err());
        config.embedding.inline_timeout_ms = 500;
        config.embedding.max_text_tokens = 63;
        assert!(validate_config(&config).is_err());
        config.embedding.max_text_tokens = 8_193;
        assert!(validate_config(&config).is_err());
        config.embedding.max_text_tokens = 2_560;
        assert!(validate_config(&config).is_ok());
    }
}
