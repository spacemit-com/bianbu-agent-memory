mod administration;
mod background;
mod embedding;
mod instructions;
mod long_term;
mod product;
mod projection;
mod search_cache;
mod sessions;

use memory_core::{capability, now_ms, Hlc, MemoryIdentity};
use memory_protocol::{required_capability, RequestEnvelope, ServiceError};
use rusqlite::{params, Connection, OpenFlags, OptionalExtension, Transaction, TransactionBehavior};
use serde::de::DeserializeOwned;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::sync::atomic::{AtomicI64, Ordering};
use std::time::Duration;
use std::ops::{Deref, DerefMut};
use uuid::Uuid;

pub type ServiceResult<T> = Result<T, ServiceError>;

/// A connection checked out from the Store's small reusable pool.
///
/// Opening an SQLCipher connection performs key setup and several SQLite
/// pragmas.  The daemon handles one request at a time per client thread, so
/// returning the connection to this pool after each operation avoids paying
/// that setup cost for every UDS frame while retaining independent read
/// connections for concurrent clients.
struct PooledConnection {
    connection: Option<Connection>,
    pool: Arc<Mutex<Vec<Connection>>>,
}

const MAX_IDLE_CONNECTIONS: usize = 64;

impl Deref for PooledConnection {
    type Target = Connection;

    fn deref(&self) -> &Self::Target {
        self.connection
            .as_ref()
            .expect("pooled connection must be checked out")
    }
}

impl DerefMut for PooledConnection {
    fn deref_mut(&mut self) -> &mut Self::Target {
        self.connection
            .as_mut()
            .expect("pooled connection must be checked out")
    }
}

impl Drop for PooledConnection {
    fn drop(&mut self) {
        let Some(connection) = self.connection.take() else {
            return;
        };
        if let Ok(mut idle) = self.pool.lock() {
            if idle.len() < MAX_IDLE_CONNECTIONS {
                idle.push(connection);
            }
        }
    }
}

/// How long a conclusive `PRAGMA quick_check` verdict is reused before
/// `system.health` runs the check again.  The check reads every page of the
/// database (~400 ms on the K3 production database) and health is polled by
/// the settings UI and by monitoring, so the verdict is memoized instead of
/// re-measured per call.
const INTEGRITY_CHECK_DEFAULT_INTERVAL_MS: i64 = 30_000;

/// Retries, and the pause between them, used to obtain a conclusive verdict
/// before `system.health` reports the integrity check as inconclusive.
const INTEGRITY_CHECK_ATTEMPTS: usize = 3;
const INTEGRITY_CHECK_RETRY_PAUSE: Duration = Duration::from_millis(25);

/// `busy_timeout` of every pooled connection.  The integrity check lowers it to
/// zero (a writer that holds the database must be *reported*, not waited out)
/// and restores it before the connection returns to the pool.
const CONNECTION_BUSY_TIMEOUT_MS: u64 = 2000;

/// Quiet time (since the last database write) required before `system.health`
/// trusts a `PRAGMA quick_check` verdict.  See `integrity_check_settle_ms`.
const INTEGRITY_CHECK_DEFAULT_SETTLE_MS: i64 = 3_000;

/// `database_check.reason` for the FTS5 complaint this build produces for
/// indexes that are intact.  See `database_integrity`.
const FTS5_INVERTED_INDEX_REASON: &str = "fts5-inverted-index";

/// Wall-clock stamp of the last write to the database.
///
/// `PRAGMA quick_check` on the shipped SQLCipher build misreports an FTS5 index
/// (docs/GATEWAY_EMBEDDING_INTEGRATION.md §8.2), and the misreport survives the
/// writer being excluded from the check, the WAL being checkpointed and the
/// check taking the write lock: what makes a sample good is the database having
/// been *left alone*, not the absence of a writer at that instant.  So every
/// write path stamps this clock and `system.health` only takes a verdict after
/// the settle window, with no background job in flight.
#[derive(Debug)]
struct ActivityClock {
    last_write_ms: AtomicI64,
    jobs_in_flight: AtomicI64,
}

impl ActivityClock {
    fn new(now_ms: i64) -> Self {
        Self {
            last_write_ms: AtomicI64::new(now_ms),
            jobs_in_flight: AtomicI64::new(0),
        }
    }

    fn touch(&self) {
        self.last_write_ms.store(now_ms(), Ordering::Relaxed);
    }

    /// Marks a background job as running until the guard is dropped. Embedding
    /// and lexical batches write outside `with_write()` and can run for
    /// seconds, so the timestamp alone cannot tell an idle database from one
    /// that is being written between two batches.
    fn job(&self) -> ActivityJob<'_> {
        self.jobs_in_flight.fetch_add(1, Ordering::Relaxed);
        ActivityJob(self)
    }

    fn is_busy(&self, now_ms: i64, settle_ms: i64) -> bool {
        self.jobs_in_flight.load(Ordering::Relaxed) > 0
            || now_ms.saturating_sub(self.last_write_ms.load(Ordering::Relaxed)) < settle_ms
    }
}

/// Keeps the integrity check from sampling the database while a background job
/// is writing to it. See `ActivityClock::job`.
struct ActivityJob<'a>(&'a ActivityClock);

impl Drop for ActivityJob<'_> {
    fn drop(&mut self) {
        self.0.jobs_in_flight.fetch_sub(1, Ordering::Relaxed);
        self.0.touch();
    }
}

/// Memoized state of the exclusive `PRAGMA quick_check` behind `system.health`.
#[derive(Debug, Default)]
struct IntegrityCheckState {
    /// Stamp of the last published verdict.
    checked_at_ms: Option<i64>,
    /// Last published verdict: `ok`, or a defect that is reported as one.
    verdict: Option<String>,
    /// A complaint that is on file but is not a verdict - currently only the
    /// FTS5 inverted-index text this build produces for intact indexes.  It is
    /// exposed as `database_check.complaint` and never derives `status`.
    unconfirmed: Option<String>,
    /// When the last sample was taken, so both a published verdict and a
    /// complaint on file are re-sampled at most once per check interval.
    sampled_at_ms: Option<i64>,
}

/// One exclusive integrity check attempt.
enum IntegrityCheck {
    /// `PRAGMA quick_check` returned a verdict: `ok`, or a defect description.
    Verdict(String),
    /// A writer held the database, so the attempt produced no verdict either
    /// way.  This is the state that must never be rendered as `degraded`.
    Busy,
}

#[derive(Clone, Debug)]
pub struct Store {
    path: PathBuf,
    database_key_hex: Option<String>,
    product_config_path: PathBuf,
    product_config_lock: Arc<Mutex<()>>,
    // Parsed product config with its (mtime, size) stamp. The privacy gate in
    // handle() and the semantic stage of every search read the config, so the
    // parse is cached and re-validated against the file stamp — see
    // product_config().
    product_config_cache: Arc<Mutex<product::ProductConfigCache>>,
    connection_pool: Arc<Mutex<Vec<Connection>>>,
    // SQLite has one writer per database.  Serialize write transactions in
    // this daemon so concurrent Runtime clients queue behind a bounded local
    // critical section instead of racing into SQLITE_BUSY.
    write_lock: Arc<Mutex<()>>,
    // Last conclusive `PRAGMA quick_check` verdict and when it was taken. See
    // database_integrity().
    integrity_check: Arc<Mutex<IntegrityCheckState>>,
    // Last write to the database; the integrity check waits for this to age.
    activity: Arc<ActivityClock>,
    // In-process vector snapshots for the semantic scan. Shared across Store
    // clones: rebuilding one decrypts the namespace's whole vector payload
    // through SQLCipher, which is far too slow to repeat per query.
    semantic_cache: Arc<embedding::SemanticIndexCache>,
    // MEM-NFR-01-01 memoized search results between writes. See
    // search_cache.rs for the invalidation contract.
    search_cache: Arc<search_cache::SearchResultCache>,
}

impl Store {
    pub fn open(path: impl AsRef<Path>) -> ServiceResult<Self> {
        Self::open_internal(path.as_ref(), None)
    }

    pub fn open_encrypted(path: impl AsRef<Path>, database_key: &[u8]) -> ServiceResult<Self> {
        if database_key.len() != 32 {
            return Err(ServiceError::invalid("database key must contain 32 bytes"));
        }
        Self::open_internal(path.as_ref(), Some(hex::encode(database_key)))
    }

    fn open_internal(
        path: &Path,
        database_key_hex: Option<String>,
    ) -> ServiceResult<Self> {
        let path = path.to_path_buf();
        let product_config_path = path.with_extension("config.json");
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)
                .map_err(|_| ServiceError::unavailable("database directory is unavailable"))?;
        }
        let store = Self {
            path,
            database_key_hex,
            product_config_path,
            product_config_lock: Arc::new(Mutex::new(())),
            product_config_cache: Arc::new(Mutex::new(product::ProductConfigCache::new())),
            connection_pool: Arc::new(Mutex::new(Vec::new())),
            write_lock: Arc::new(Mutex::new(())),
            integrity_check: Arc::new(Mutex::new(IntegrityCheckState::default())),
            activity: Arc::new(ActivityClock::new(now_ms())),
            semantic_cache: Arc::new(embedding::SemanticIndexCache::default()),
            search_cache: Arc::new(search_cache::SearchResultCache::new()),
        };
        let mut connection = store.connection()?;
        // WAL is persistent database state.  Set it once while opening the
        // store rather than issuing the mode-changing pragma for every UDS
        // request, which otherwise serializes otherwise-independent reads.
        connection
            .query_row::<String, _, _>("PRAGMA journal_mode=WAL", [], |row| row.get(0))
            .map_err(db_error)?;
        let transaction = connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(db_error)?;
        transaction
            .execute_batch(include_str!("../../../migrations/0001_init.sql"))
            .map_err(db_error)?;
        transaction
            .execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?1)",
                [now_ms()],
            )
            .map_err(db_error)?;
        let schema_version: i64 = transaction
            .query_row(
                "SELECT COALESCE(MAX(version),0) FROM schema_migrations",
                [],
                |row| row.get(0),
            )
            .map_err(db_error)?;
        if schema_version < 2 {
            transaction
                .execute_batch(include_str!(
                    "../../../migrations/0002_add_memory_delete_proposed_event.sql"
                ))
                .map_err(db_error)?;
            transaction
                .execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES(2, ?1)",
                    [now_ms()],
                )
                .map_err(db_error)?;
        }
        let schema_version: i64 = transaction
            .query_row(
                "SELECT COALESCE(MAX(version),0) FROM schema_migrations",
                [],
                |row| row.get(0),
            )
            .map_err(db_error)?;
        if schema_version < 3 {
            transaction
                .execute_batch(include_str!(
                    "../../../migrations/0003_add_semantic_projection.sql"
                ))
                .map_err(db_error)?;
            transaction
                .execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES(3, ?1)",
                    [now_ms()],
                )
                .map_err(db_error)?;
        }
        let schema_version: i64 = transaction
            .query_row(
                "SELECT COALESCE(MAX(version),0) FROM schema_migrations",
                [],
                |row| row.get(0),
            )
            .map_err(db_error)?;
        if schema_version < 4 {
            transaction
                .execute_batch(include_str!(
                    "../../../migrations/0004_add_recent_session_context_indexes.sql"
                ))
                .map_err(db_error)?;
            transaction
                .execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES(4, ?1)",
                    [now_ms()],
                )
                .map_err(db_error)?;
        }
        let schema_version: i64 = transaction
            .query_row(
                "SELECT COALESCE(MAX(version),0) FROM schema_migrations",
                [],
                |row| row.get(0),
            )
            .map_err(db_error)?;
        if schema_version < 5 {
            transaction
                .execute_batch(include_str!(
                    "../../../migrations/0005_add_local_management.sql"
                ))
                .map_err(db_error)?;
            transaction
                .execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES(5, ?1)",
                    [now_ms()],
                )
                .map_err(db_error)?;
        }
        let schema_version: i64 = transaction
            .query_row(
                "SELECT COALESCE(MAX(version),0) FROM schema_migrations",
                [],
                |row| row.get(0),
            )
            .map_err(db_error)?;
        if schema_version < 6 {
            transaction
                .execute_batch(include_str!(
                    "../../../migrations/0006_harden_profile_privacy.sql"
                ))
                .map_err(db_error)?;
            transaction
                .execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES(6, ?1)",
                    [now_ms()],
                )
                .map_err(db_error)?;
        }
        let schema_version: i64 = transaction
            .query_row(
                "SELECT COALESCE(MAX(version),0) FROM schema_migrations",
                [],
                |row| row.get(0),
            )
            .map_err(db_error)?;
        if schema_version < 8 {
            transaction
                .execute_batch(include_str!(
                    "../../../migrations/0008_add_background_worker_state.sql"
                ))
                .map_err(db_error)?;
            transaction
                .execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES(8, ?1)",
                    [now_ms()],
                )
                .map_err(db_error)?;
        }
        let schema_version: i64 = transaction
            .query_row(
                "SELECT COALESCE(MAX(version),0) FROM schema_migrations",
                [],
                |row| row.get(0),
            )
            .map_err(db_error)?;
        if schema_version < 9 {
            transaction
                .execute_batch(include_str!(
                    "../../../migrations/0009_add_session_retention_and_payload_encoding.sql"
                ))
                .map_err(db_error)?;
            transaction
                .execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES(9, ?1)",
                    [now_ms()],
                )
                .map_err(db_error)?;
        }
        let schema_version: i64 = transaction
            .query_row(
                "SELECT COALESCE(MAX(version),0) FROM schema_migrations",
                [],
                |row| row.get(0),
            )
            .map_err(db_error)?;
        if schema_version < 10 {
            transaction
                .execute_batch(include_str!(
                    "../../../migrations/0010_scope_session_keys.sql"
                ))
                .map_err(db_error)?;
            transaction
                .execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES(10, ?1)",
                    [now_ms()],
                )
                .map_err(db_error)?;
        }
        let schema_version: i64 = transaction
            .query_row(
                "SELECT COALESCE(MAX(version),0) FROM schema_migrations",
                [],
                |row| row.get(0),
            )
            .map_err(db_error)?;
        if schema_version < 11 {
            transaction
                .execute_batch(include_str!(
                    "../../../migrations/0011_add_memory_embeddings.sql"
                ))
                .map_err(db_error)?;
            transaction
                .execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES(11, ?1)",
                    [now_ms()],
                )
                .map_err(db_error)?;
        }
        let schema_version: i64 = transaction
            .query_row(
                "SELECT COALESCE(MAX(version),0) FROM schema_migrations",
                [],
                |row| row.get(0),
            )
            .map_err(db_error)?;
        if schema_version < 12 {
            transaction
                .execute_batch(include_str!(
                    "../../../migrations/0012_add_long_term_fts2.sql"
                ))
                .map_err(db_error)?;
            transaction
                .execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES(12, ?1)",
                    [now_ms()],
                )
                .map_err(db_error)?;
        }
        // Deployed legacy feature branches may carry higher version markers
        // without this scheduler extension. Inspect the feature, not MAX(version).
        let has_summary_cursor: bool = transaction
            .query_row("SELECT EXISTS(SELECT 1 FROM pragma_table_info('background_worker_state') WHERE name='scan_offset')",
                [], |row| row.get(0)).map_err(db_error)?;
        if !has_summary_cursor {
            transaction.execute_batch(include_str!("../../../migrations/0014_add_summary_compensation.sql"))
                .map_err(db_error)?;
            transaction.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(14, ?1)", [now_ms()])
                .map_err(db_error)?;
        }
        transaction.commit().map_err(db_error)?;
        store.initialize_product_config()?;
        // Opening the store writes schema and product config; start the
        // integrity clock from there rather than from an unset value.
        store.activity.touch();
        Ok(store)
    }

    fn connection(&self) -> ServiceResult<PooledConnection> {
        if let Some(connection) = self
            .connection_pool
            .lock()
            .map_err(|_| ServiceError::unavailable("database connection pool is poisoned"))?
            .pop()
        {
            return Ok(PooledConnection {
                connection: Some(connection),
                pool: Arc::clone(&self.connection_pool),
            });
        }
        let mut connection = Connection::open(&self.path).map_err(db_error)?;
        if let Some(key) = self.database_key_hex.as_deref() {
            if key.len() != 64 || !key.bytes().all(|byte| byte.is_ascii_hexdigit()) {
                return Err(ServiceError::invalid("invalid database key"));
            }
            connection
                .execute_batch(&format!("PRAGMA key = \"x'{key}'\";"))
                .map_err(db_error)?;
            let cipher_version: Option<String> = connection
                .query_row("PRAGMA cipher_version", [], |row| row.get(0))
                .optional()
                .map_err(db_error)?;
            if !cipher_version
                .as_deref()
                .is_some_and(|version| !version.is_empty())
            {
                return Err(ServiceError::unavailable("SQLCipher is unavailable"));
            }
        }
        // Page cache per pooled connection (KB). The MEM-NFR-01-01 K3 runs
        // showed the hot fts2/f row set of a 100k-row namespace is a few MB:
        // a 4MB cache thrashes when concurrent requests rotate connections,
        // and every miss is a fresh software-AES page decrypt on riscv64.
        // AGENT_MEMORY_CACHE_KB overrides for device tuning.
        let cache_kb: i64 = std::env::var("AGENT_MEMORY_CACHE_KB")
            .ok()
            .and_then(|value| value.parse::<i64>().ok())
            .unwrap_or(8192)
            .clamp(1024, 65536);
        let busy_timeout_ms = CONNECTION_BUSY_TIMEOUT_MS;
        connection
            .execute_batch(&format!(
                "PRAGMA synchronous=FULL;
                 PRAGMA foreign_keys=ON;
                 PRAGMA busy_timeout={busy_timeout_ms};
                 PRAGMA trusted_schema=OFF;
                 PRAGMA cache_size=-{cache_kb};
                 PRAGMA mmap_size=0;",
            ))
            .map_err(db_error)?;
        // Prepared-statement cache per pooled connection. The search/read
        // path re-issues the same handful of SQL shapes (L1 per-run, backfill
        // scans, get_many IN() fetches at stable arities) on every request;
        // on the K3 riscv64 CPU each fresh prepare re-parses the statement
        // and re-instantiates the FTS5 tokenizers, which showed up as a
        // measurable slice of the MEM-NFR-01-01 p95 tail. Pooled connections
        // persist across requests, so cached statements amortize fully.
        connection.set_prepared_statement_cache_capacity(64);
        Ok(PooledConnection {
            connection: Some(connection),
            pool: Arc::clone(&self.connection_pool),
        })
    }

    /// Return bounded local storage telemetry used by health/maintenance and
    /// enforce product-level database/free-space guardrails.
    pub(crate) fn storage_status(&self) -> ServiceResult<Value> {
        let config = self.product_config()?;
        let database_bytes = database_size_bytes(&self.path)?;
        let free_bytes = filesystem_free_bytes(&self.path)?;
        Ok(json!({
            "database_bytes": database_bytes,
            "database_limit_bytes": config.max_database_bytes,
            "free_bytes": free_bytes,
            "minimum_free_bytes": config.min_free_bytes,
            "within_limits": database_bytes <= config.max_database_bytes as u64
                && free_bytes >= config.min_free_bytes as u64
        }))
    }

    pub(crate) fn ensure_storage_capacity(&self, additional_bytes: u64) -> ServiceResult<()> {
        let config = self.product_config()?;
        let database_bytes = database_size_bytes(&self.path)?;
        let projected = database_bytes.saturating_add(additional_bytes);
        if projected > config.max_database_bytes as u64 {
            return Err(ServiceError::new(
                "ResourceExhausted",
                format!(
                    "database storage limit exceeded ({} > {} bytes)",
                    projected, config.max_database_bytes
                ),
            ));
        }
        let free_bytes = filesystem_free_bytes(&self.path)?;
        if free_bytes < config.min_free_bytes as u64 {
            return Err(ServiceError::new(
                "ResourceExhausted",
                format!(
                    "filesystem free space is below the configured reserve ({} < {} bytes)",
                    free_bytes, config.min_free_bytes
                ),
            ));
        }
        Ok(())
    }

    pub fn migrate_development_account_to_default(&self, peer_uid: u32) -> ServiceResult<Value> {
        const LOCAL_TABLES: &[&str] = &[
            "account_sessions",
            "memory_spaces",
            "instructions",
            "session_events",
            "session_summaries",
            "session_retention_manifests",
            "long_term_memories",
            "tombstones",
            "forget_jobs",
            "idempotency_keys",
            "checkpoint_refs",
            "semantic_projection_jobs",
            "semantic_projection_mappings",
            "memory_embeddings",
            "lexical_index_state",
            "memory_settings",
            "background_worker_state",
        ];
        const CLOUD_STATE_TABLES: &[&str] = &[
            "outbox",
            "inbox",
            "sync_cursors",
            "sync_conflicts",
            "sync_jobs",
        ];
        let legacy_account = format!("local:uid:{peer_uid}");
        let mut connection = self.connection()?;
        let transaction = connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(db_error)?;
        let mut legacy_rows = 0_i64;
        let mut default_rows = 0_i64;
        for table in LOCAL_TABLES {
            legacy_rows += transaction
                .query_row(
                    &format!("SELECT COUNT(*) FROM {table} WHERE account_id=?1"),
                    [&legacy_account],
                    |row| row.get::<_, i64>(0),
                )
                .map_err(db_error)?;
            default_rows += transaction
                .query_row(
                    &format!("SELECT COUNT(*) FROM {table} WHERE account_id='default'"),
                    [],
                    |row| row.get::<_, i64>(0),
                )
                .map_err(db_error)?;
        }
        let mut discarded_cloud_rows = 0_usize;
        for table in CLOUD_STATE_TABLES {
            discarded_cloud_rows += transaction
                .execute(
                    &format!("DELETE FROM {table} WHERE account_id=?1 OR account_id='default'"),
                    [&legacy_account],
                )
                .map_err(db_error)?;
        }
        if legacy_rows == 0 {
            transaction.commit().map_err(db_error)?;
            return Ok(json!({
                "state":"not_needed",
                "migrated_rows":0,
                "discarded_cloud_rows":discarded_cloud_rows
            }));
        }
        if default_rows != 0 {
            return Err(ServiceError::conflict(
                "default account already contains data; automatic local account migration was refused",
            ));
        }
        let mut migrated_rows = 0_usize;
        for table in LOCAL_TABLES {
            migrated_rows += transaction
                .execute(
                    &format!("UPDATE {table} SET account_id='default' WHERE account_id=?1"),
                    [&legacy_account],
                )
                .map_err(db_error)?;
        }
        transaction
            .execute(
                "INSERT INTO service_meta(key,value) VALUES('default_account_migration',?1)
                 ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [format!("{legacy_account}:{}", now_ms())],
            )
            .map_err(db_error)?;
        transaction.commit().map_err(db_error)?;
        // Account migration relabels every local row; drop memoized search
        // results so no stale namespace survives the relabel.
        self.search_cache.invalidate();
        Ok(json!({
            "state":"migrated",
            "source_account":legacy_account,
            "target_account":"default",
            "migrated_rows":migrated_rows,
            "discarded_cloud_rows":discarded_cloud_rows
        }))
    }

    pub fn handle(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
        confirm_token_verified: bool,
    ) -> ServiceResult<Value> {
        // MEM-NFR-01-01 concurrency gating lives inside search_headers around
        // the lexical/FTS scan stages, not here: the K3 acceptance profile
        // (32 concurrent clients) showed a whole-request gate makes the cheap
        // follow-up reads (get_many of the search results) queue behind the
        // scans they follow, doubling the queue amplification of every
        // search+read round trip. Scan-shaped work is bounded where it is
        // expensive; everything else relies on SQLite's own locking and the
        // per-connection checkout.
        if identity_mismatch(identity, &request.params) {
            self.audit_access(identity, request, "identity_mismatch", "denied", None);
            return Err(ServiceError::permission_denied());
        }
        if let Some(required) = required_capability(&request.method) {
            if !identity.has(required) {
                self.audit_access(
                    identity,
                    request,
                    "capability_denied",
                    "denied",
                    Some(required),
                );
                return Err(ServiceError::permission_denied());
            }
        }
        if is_memory_data_method(&request.method) && !self.memory_is_enabled(identity)? {
            self.audit_access(identity, request, "memory_disabled", "skipped", None);
            return Err(ServiceError::new(
                "MemoryDisabled",
                "memory is disabled by the current privacy setting",
            ));
        }
        // Export is read-only, but the product's strict confirmation policy
        // treats it as a sensitive data boundary.  The daemon still verifies
        // the short-lived token before handing data to the caller.
        if request.method == "privacy.export_data"
            && self.product_config()?.sensitive_action_confirmation == "strict"
            && !confirm_token_verified
        {
            return Err(ServiceError::new(
                "PermissionDenied",
                "a verified confirmation token is required by the current privacy setting",
            ));
        }

        let result = match request.method.as_str() {
            "account.status" => self.account_status(identity),
            "account.begin_login" | "account.complete_login" | "account.logout" => {
                Err(ServiceError::new(
                    "AccountOffline",
                    "OIDC and Secret Broker adapters are not configured",
                ))
            }
            "instructions.put" => self.put_instruction(identity, request),
            "instructions.list" => self.list_instructions(identity, request),
            "instructions.revise" => self.revise_instruction(identity, request),
            "sessions.append_event" => self.append_event(identity, request),
            "sessions.update_summary" => self.update_summary(identity, request),
            "sessions.get_context" => self.get_context(identity, request),
            "sessions.get_recent_context" => self.get_recent_context(identity, request),
            "sessions.get_history" => self.get_history(identity, request),
            "sessions.archive_old" => self.archive_old_sessions(identity, request),
            "sessions.summarize" => self.summarize_session(identity, request),
            "sessions.compact" => self.compact_session(identity, request),
            "long_term.put" => self.put_long_term(identity, request),
            "long_term.search_headers" => self.search_headers(identity, request),
            "long_term.get_many" => self.get_many(identity, request),
            "long_term.revise" => self.revise_long_term(identity, request),
            "long_term.reconcile_slots" => self.reconcile_long_term_slots(identity, request),
            "long_term.forget" => {
                if !confirm_token_verified {
                    return Err(ServiceError::new(
                        "PermissionDenied",
                        "a verified confirmation token is required",
                    ));
                }
                self.forget_long_term(identity, request)
            }
            "long_term.wait_forget" => self.wait_forget(identity, request),
            "projection.enqueue_turn" => self.enqueue_projection_turn(identity, request),
            "projection.claim" => self.claim_projection_job(identity, request),
            "projection.save_result" => self.save_projection_provider_result(identity, request),
            "projection.complete" => self.complete_projection_job(identity, request),
            "projection.fail" => self.fail_projection_job(identity, request),
            "projection.upsert_mapping" => self.upsert_projection_mapping(identity, request),
            "projection.resolve" => self.resolve_projection_mappings(identity, request),
            "projection.status" => self.projection_status(identity, request),
            "projection.list_dead" => self.list_dead_projection_jobs(identity, request),
            "projection.requeue_dead" => self.requeue_dead_projection_job(identity, request),
            "sync.status" => self.sync_status(identity),
            "sync.request" => self.request_sync(identity, request),
            "audit.status" => self.audit_status(identity),
            "audit.list" => self.list_audit(identity, request),
            "audit.export" => self.export_audit(identity, request),
            "audit.record" => self.record_audit(identity, request),
            "privacy.settings.get" => self.get_privacy_settings(identity),
            "privacy.settings.update" => self.update_privacy_settings(identity, request),
            "privacy.export_data" => self.export_data(identity, request),
            "privacy.import_data" => {
                if !confirm_token_verified {
                    return Err(ServiceError::new(
                        "PermissionDenied",
                        "a verified confirmation token is required",
                    ));
                }
                self.import_data(identity, request)
            }
            "privacy.clear_history" => {
                if !confirm_token_verified {
                    return Err(ServiceError::new(
                        "PermissionDenied",
                        "a verified confirmation token is required",
                    ));
                }
                self.clear_history(identity, request)
            }
            "privacy.clear_all" => {
                if !confirm_token_verified {
                    return Err(ServiceError::new(
                        "PermissionDenied",
                        "a verified confirmation token is required",
                    ));
                }
                self.clear_all(identity, request)
            }
            "maintenance.run" => self.run_maintenance(identity, request),
            "runtime.prepare_checkpoint" => self.prepare_checkpoint(identity, request),
            "runtime.restore_checkpoint" => self.restore_checkpoint(identity, request),
            "runtime.list_checkpoints" => self.list_checkpoints(identity, request),
            "system.health" => self.health(),
            "system.stats" => self.stats(identity),
            _ => Err(ServiceError::new("NotFound", "unknown protocol method")),
        };
        if result.is_ok() && is_audited_read(&request.method) {
            self.audit_access(identity, request, "memory_read", "success", None);
        }
        result
    }

    fn require_capability(&self, identity: &MemoryIdentity, required: &str) -> ServiceResult<()> {
        // The daemon checks first; checking here again prevents alternate entry points
        // from bypassing the authoritative handler guard.
        if identity.has(required) {
            Ok(())
        } else {
            Err(ServiceError::permission_denied())
        }
    }

    fn account_status(&self, identity: &MemoryIdentity) -> ServiceResult<Value> {
        let connection = self.connection()?;
        let row: Option<(String, Option<i64>)> = connection
            .query_row(
                "SELECT state, token_expires_at FROM account_sessions WHERE account_id=?1",
                [&identity.account_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()
            .map_err(db_error)?;
        let (state, expires_at) = row.unwrap_or_else(|| ("SIGNED_OUT".to_owned(), None));
        Ok(json!({
            "state": state,
            "account_id": identity.account_id,
            "device_id": identity.device_id,
            "expires_at": expires_at,
            "online": false,
            "reauth_required": false,
            "last_error": "cloud_account_adapter_not_configured"
        }))
    }

    fn sync_status(&self, identity: &MemoryIdentity) -> ServiceResult<Value> {
        let connection = self.connection()?;
        let pending_upload: i64 = connection
            .query_row(
                "SELECT COUNT(*) FROM outbox WHERE account_id=?1 AND agent_id=?2 AND status!='acked'",
                params![identity.account_id, identity.agent_id],
                |row| row.get(0),
            )
            .map_err(db_error)?;
        let pending_download: i64 = connection
            .query_row(
                "SELECT COUNT(*) FROM inbox WHERE account_id=?1 AND agent_id=?2 AND apply_status='pending'",
                params![identity.account_id, identity.agent_id],
                |row| row.get(0),
            )
            .map_err(db_error)?;
        let conflict_count: i64 = connection
            .query_row(
                "SELECT COUNT(*) FROM sync_conflicts WHERE account_id=?1 AND agent_id=?2 AND state!='resolved'",
                params![identity.account_id, identity.agent_id],
                |row| row.get(0),
            )
            .map_err(db_error)?;
        let cursors: Option<(Option<String>, Option<String>, Option<i64>, Option<String>)> =
            connection
                .query_row(
                    "SELECT local_cursor,server_cursor,last_success_at,last_error FROM sync_cursors
                 WHERE account_id=?1 AND agent_id=?2",
                    params![identity.account_id, identity.agent_id],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
                )
                .optional()
                .map_err(db_error)?;
        let (local_cursor, server_cursor, last_success_at, stored_error) =
            cursors.unwrap_or((None, None, None, None));
        Ok(json!({
            "account_id": identity.account_id,
            "device_id": identity.device_id,
            "agent_id": identity.agent_id,
            "online": false,
            "state": "offline",
            "last_success_at": last_success_at,
            "local_cursor": local_cursor,
            "server_cursor": server_cursor,
            "pending_upload": pending_upload,
            "pending_download": pending_download,
            "conflict_count": conflict_count,
            "last_error": stored_error.unwrap_or_else(|| "cloud_sync_adapter_not_configured".to_owned())
        }))
    }

    fn request_sync(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        if identity.account_id == "default" || identity.account_id.starts_with("local:") {
            return Err(ServiceError::new(
                "Unauthenticated",
                "the local default account cannot upload or request cloud sync",
            ));
        }
        let scopes: Vec<String> = request
            .params
            .get("scopes")
            .and_then(Value::as_array)
            .ok_or_else(|| ServiceError::invalid("scopes must be an array"))?
            .iter()
            .map(|value| value.as_str().map(str::to_owned))
            .collect::<Option<Vec<_>>>()
            .ok_or_else(|| ServiceError::invalid("scopes must contain strings"))?;
        let scopes_blob =
            serde_json::to_string(&scopes).map_err(|_| ServiceError::invalid("invalid scopes"))?;
        self.with_write(identity, request, "sync.request", "sync_job", |transaction, _, now, _, _| {
            let job_id = Uuid::new_v4().to_string();
            transaction
                .execute(
                    "INSERT INTO sync_jobs(job_id,account_id,agent_id,scopes_json,state,created_at,updated_at)
                     VALUES(?1,?2,?3,?4,'offline_queued',?5,?5)",
                    params![job_id, identity.account_id, identity.agent_id, scopes_blob, now],
                )
                .map_err(db_error)?;
            Ok((json!({"job_id":job_id,"state":"offline_queued"}), Some(job_id)))
        })
    }

    fn audit_status(&self, identity: &MemoryIdentity) -> ServiceResult<Value> {
        self.require_capability(identity, capability::AUDIT_STATUS)?;
        let connection = self.connection()?;
        let pending: i64 = connection
            .query_row(
                "SELECT COUNT(*) FROM audit_spool WHERE export_state='pending'",
                [],
                |row| row.get(0),
            )
            .map_err(db_error)?;
        let total: i64 = connection
            .query_row("SELECT COUNT(*) FROM audit_spool", [], |row| row.get(0))
            .map_err(db_error)?;
        Ok(json!({
            "state":"ok",
            "records":total,
            "pending_external_events":pending,
            "export_adapter":"local_json",
            "external_adapter":"not_configured",
            "last_error":Value::Null
        }))
    }

    fn health(&self) -> ServiceResult<Value> {
        let (status, database, database_check) = self.database_integrity()?;
        let connection = self.connection()?;
        let schema: i64 = connection
            .query_row(
                "SELECT COALESCE(MAX(version),0) FROM schema_migrations",
                [],
                |row| row.get(0),
            )
            .map_err(db_error)?;
        let storage = self.storage_status()?;
        let embedding = self.effective_embedding_status()?;
        Ok(json!({
            "status":status,
            "database":database,
            "database_check":database_check,
            "schema_version":schema,
            "storage_encryption":if self.database_key_hex.is_some() {"sqlcipher"} else {"plaintext_explicit"},
            "audit_export":"local_json",
            "storage":storage,
            "embedding":embedding
        }))
    }

    /// Integrity verdict for `system.health`, plus how that verdict was
    /// obtained.
    ///
    /// `PRAGMA quick_check` reads every page and, on the shipped SQLCipher
    /// build, reports `malformed inverted index for FTS5 table
    /// main.long_term_fts[2]` for indexes that are intact
    /// (docs/GATEWAY_EMBEDDING_INTEGRATION.md §8.2).  Measured on K3
    /// (2026-09-22, production database, `system.health` every 5 s with a fresh
    /// check per call): the complaint appears after a sustained write burst,
    /// reproduces on every sample for ~100 s with the database left alone, then
    /// clears by itself and stays clear — and retrieval is correct throughout.
    /// A real defect does not clear: corrupting `long_term_fts_docsize`
    /// reproduces the same text on every sample, indefinitely.
    ///
    /// A verdict therefore has to be hard to fake.  Samples are taken only
    /// once:
    ///
    /// * client writers are excluded, by holding `write_lock` for the check;
    /// * the write-ahead log is empty — `wal_checkpoint(TRUNCATE)` must report
    ///   that it truncated everything, `BEGIN IMMEDIATE` freezes that state, and
    ///   `-wal` must still be zero bytes inside the transaction. A checkpoint
    ///   that cannot truncate (a reader holds a snapshot, a background batch is
    ///   writing) is reported as `busy` — an inconclusive answer, never a
    ///   verdict;
    /// * the database has been left alone for `AGENT_MEMORY_HEALTH_SETTLE_MS`
    ///   and no background job is running;
    /// * the FTS5 inverted-index text is *not* a verdict on this build: it is
    ///   the same text an intact index produces under load, it is not
    ///   actionable, and a complaint that reproduces for minutes and then
    ///   clears by itself cannot be told apart from one that is real by
    ///   sampling harder.  It is exposed as `database_check.complaint`
    ///   (`state="complaint"`) and never derives `status`; every other
    ///   complaint is published as `degraded` from the first quiet sample;
    /// * the last conclusive verdict is reused for
    ///   `AGENT_MEMORY_HEALTH_CHECK_INTERVAL_MS` (default 30 s), so pollers do
    ///   not each pay a whole-database read and two adjacent calls cannot
    ///   report different verdicts.
    ///
    /// An inconclusive check is never rendered as `degraded`: `degraded` is
    /// reserved for a defect that was confirmed, while a database that only
    /// looks unhealthy because it is being written to, or because of the known
    /// transient, keeps the status it already had.
    fn database_integrity(&self) -> ServiceResult<(String, String, Value)> {
        let interval_ms = integrity_check_interval_ms();
        let settle_ms = integrity_check_settle_ms();
        let mut state = self
            .integrity_check
            .lock()
            .map_err(|_| ServiceError::unavailable("database integrity check state is poisoned"))?;
        let now = now_ms();
        // Serve the last sample's conclusion for the interval. A complaint on
        // file is served too - re-deriving it per call would pay a
        // whole-database read on every poll and could not change the answer.
        if state
            .sampled_at_ms
            .is_some_and(|sampled| now.saturating_sub(sampled) < interval_ms)
        {
            if state.unconfirmed.is_some() {
                return Ok(integrity_report(
                    &state,
                    "complaint",
                    Some(FTS5_INVERTED_INDEX_REASON),
                    interval_ms,
                    0,
                ));
            }
            return Ok(integrity_report(&state, "cached", None, interval_ms, 0));
        }
        // A database that is being written to (or has just been written to) is
        // exactly the one that produces the artifact, so wait for it to be left
        // alone. Reported as `busy` with a reason, never as a verdict.
        if self.activity.is_busy(now, settle_ms) {
            return Ok(integrity_report(
                &state,
                "busy",
                Some("recent-activity"),
                interval_ms,
                0,
            ));
        }
        let mut attempts = 0_usize;
        let sample = loop {
            attempts += 1;
            match self.quick_check_exclusive()? {
                IntegrityCheck::Verdict(verdict) => break Some(verdict),
                IntegrityCheck::Busy if attempts < INTEGRITY_CHECK_ATTEMPTS => {
                    std::thread::sleep(INTEGRITY_CHECK_RETRY_PAUSE);
                }
                IntegrityCheck::Busy => break None,
            }
        };
        let Some(sample) = sample else {
            // Report the previous verdict when there is one, and otherwise say
            // that the check could not run rather than guessing. An
            // inconclusive attempt is not memoized: a writer that has since
            // left must be noticed on the next call, not half a minute later.
            return Ok(integrity_report(
                &state,
                "busy",
                Some("database-busy"),
                interval_ms,
                attempts,
            ));
        };
        state.sampled_at_ms = Some(now_ms());
        if sample == "ok" {
            state.verdict = Some("ok".to_owned());
            state.checked_at_ms = Some(now);
            state.unconfirmed = None;
            return Ok(integrity_report(
                &state,
                "checked",
                None,
                interval_ms,
                attempts,
            ));
        }
        // The one text this build produces for indexes that are intact. Held as
        // a complaint instead of a verdict: it is not actionable, and calling
        // it `degraded` is what produced the false alarms this release fixes.
        if is_fts5_inverted_index_complaint(&sample) {
            state.unconfirmed = Some(sample);
            // The sample still concludes "no defect this check can act on", so
            // the database keeps a healthy verdict and the complaint rides
            // alongside it rather than replacing it.
            state.verdict = Some("ok".to_owned());
            state.checked_at_ms = Some(now);
            return Ok(integrity_report(
                &state,
                "complaint",
                Some(FTS5_INVERTED_INDEX_REASON),
                interval_ms,
                attempts,
            ));
        }
        state.verdict = Some(sample);
        state.checked_at_ms = Some(now);
        state.unconfirmed = None;
        Ok(integrity_report(
            &state,
            "checked",
            None,
            interval_ms,
            attempts,
        ))
    }

    /// One `PRAGMA quick_check` attempt that cannot overlap a commit and does
    /// not read through a populated write-ahead log.
    fn quick_check_exclusive(&self) -> ServiceResult<IntegrityCheck> {
        // Client writes hold write_lock for their whole transaction, so taking
        // it here means no client commit can land inside the check. Background
        // batches take an Immediate transaction without this lock; they are
        // caught by the fail-fast begin below.
        let _write_guard = self
            .write_lock
            .lock()
            .map_err(|_| ServiceError::unavailable("database writer lock is poisoned"))?;
        let mut connection = self.connection()?;
        connection
            .busy_timeout(Duration::ZERO)
            .map_err(db_error)?;
        let outcome = empty_wal_quick_check(&mut connection, &self.path);
        // Restore the pool-wide pragma even when the check failed.
        connection
            .busy_timeout(Duration::from_millis(CONNECTION_BUSY_TIMEOUT_MS))
            .map_err(db_error)?;
        outcome
    }

    fn stats(&self, identity: &MemoryIdentity) -> ServiceResult<Value> {
        let connection = self.connection()?;
        let count = |table: &str| -> ServiceResult<i64> {
            let sql = format!("SELECT COUNT(*) FROM {table} WHERE account_id=?1 AND agent_id=?2");
            connection
                .query_row(
                    &sql,
                    params![identity.account_id, identity.agent_id],
                    |row| row.get(0),
                )
                .map_err(db_error)
        };
        let outbox_pending: i64 = connection.query_row(
            "SELECT COUNT(*) FROM outbox WHERE account_id=?1 AND agent_id=?2 AND status!='acked'",
            params![identity.account_id,identity.agent_id],
            |row| row.get(0),
        ).map_err(db_error)?;
        let storage = self.storage_status()?;
        Ok(json!({
            "instructions":count("instructions")?,
            "session_events":count("session_events")?,
            "session_summaries":count("session_summaries")?,
            "long_term_memories":count("long_term_memories")?,
            "tombstones":count("tombstones")?,
            "outbox_pending":outbox_pending,
            "semantic_projection_jobs":count("semantic_projection_jobs")?,
            "semantic_projection_mappings":count("semantic_projection_mappings")?,
            "memory_embeddings":count("memory_embeddings")?,
            "storage":storage
        }))
    }

    fn audit_access(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
        event_type: &str,
        result: &str,
        required_capability: Option<&str>,
    ) {
        let Ok(connection) = self.connection() else {
            return;
        };
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
            .ok()
            .flatten()
            .unwrap_or(1)
            != 0;
        if !audit_enabled && !matches!(event_type, "identity_mismatch" | "capability_denied") {
            return;
        }
        let record_id = request
            .params
            .get("memory_id")
            .or_else(|| request.params.get("instruction_id"))
            .or_else(|| request.params.get("session_id"))
            .or_else(|| request.params.get("job_id"))
            .and_then(Value::as_str)
            .map(hash_text);
        let details = json!({"required_capability":required_capability}).to_string();
        self.activity.touch();
        let _ = connection.execute(
            "INSERT INTO audit_spool(
                audit_id,event_type,account_id_hash,device_id,agent_id,agent_instance_id,
                record_type,record_id_hash,action,result,trace_id,details_json,created_at,export_state
             ) VALUES(?1,?2,?3,?4,?5,?6,'protocol',?7,?8,?9,?10,?11,?12,'pending')",
            params![
                Uuid::new_v4().to_string(),event_type,hash_text(&identity.account_id),
                identity.device_id,identity.agent_id,identity.agent_instance_id,record_id,
                request.method,result,request.trace_id,details,now_ms()
            ],
        );
    }

    pub(crate) fn with_write<F>(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
        action: &str,
        record_type: &str,
        write: F,
    ) -> ServiceResult<Value>
    where
        F: FnOnce(
            &Transaction<'_>,
            &str,
            i64,
            &str,
            &str,
            ) -> ServiceResult<(Value, Option<String>)>,
    {
        // Every client write (and the background maintenance job, which goes
        // through here as well) resets the window in which an integrity check
        // is trusted; see ActivityClock.
        self.activity.touch();
        // Privacy settings are the controlled escape hatch for correcting a
        // limit. Other writes fail fast instead of allowing SQLite/WAL growth
        // to consume the device when the configured local guardrail is hit.
        if !matches!(
            action,
            "privacy.settings.update"
                | "privacy.clear_history"
                | "privacy.clear_all"
                | "long_term.forget"
                | "maintenance.run"
        ) {
            self.ensure_storage_capacity(0)?;
        }
        let _write_guard = self
            .write_lock
            .lock()
            .map_err(|_| ServiceError::unavailable("database writer lock is poisoned"))?;
        let mut connection = self.connection()?;
        let transaction = connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(db_error)?;
        let idempotency_key = request
            .idempotency_key
            .as_deref()
            .unwrap_or(&request.request_id);
        if idempotency_key.len() > 512 {
            return Err(ServiceError::invalid("idempotency_key is too long"));
        }
        let request_hash = hash_json(&json!({"method":request.method,"params":request.params}));
        if let Some((old_hash, response_blob)) = transaction
            .query_row(
                "SELECT request_hash,response_blob FROM idempotency_keys
                 WHERE account_id=?1 AND agent_id=?2 AND operation=?3 AND idempotency_key=?4 AND expires_at>?5",
                params![identity.account_id, identity.agent_id, action, idempotency_key, now_ms()],
                |row| Ok((row.get::<_, String>(0)?, row.get::<_, Vec<u8>>(1)?)),
            )
            .optional()
            .map_err(db_error)?
        {
            if old_hash != request_hash {
                return Err(ServiceError::conflict(
                    "idempotency key was already used with different parameters",
                ));
            }
            return serde_json::from_slice(&response_blob)
                .map_err(|_| ServiceError::unavailable("stored idempotent response is invalid"));
        }

        let now = now_ms();
        let hlc = next_hlc(&transaction, now, &identity.device_id)?;
        let audit_id = Uuid::new_v4().to_string();
        let (result, record_id) = write(&transaction, &hlc, now, &audit_id, &request_hash)?;

        let audit_enabled = transaction
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
        if audit_enabled || is_mandatory_audit_action(action) {
            transaction
                .execute(
                "INSERT INTO audit_spool(
                    audit_id,event_type,account_id_hash,device_id,agent_id,agent_instance_id,
                    record_type,record_id_hash,action,result,trace_id,details_json,created_at,export_state
                 ) VALUES(?1,'memory_operation',?2,?3,?4,?5,?6,?7,?8,'success',?9,'{}',?10,'pending')",
                params![
                    audit_id,
                    hash_text(&identity.account_id),
                    identity.device_id,
                    identity.agent_id,
                    identity.agent_instance_id,
                    record_type,
                    record_id.as_deref().map(hash_text),
                    action,
                    request.trace_id,
                    now
                ],
                )
                .map_err(db_error)?;
        }
        let response_blob = serde_json::to_vec(&result)
            .map_err(|_| ServiceError::unavailable("response serialization failed"))?;
        transaction
            .execute(
                "INSERT INTO idempotency_keys(
                    account_id,agent_id,operation,idempotency_key,request_hash,response_blob,expires_at
                 ) VALUES(?1,?2,?3,?4,?5,?6,?7)",
                params![
                    identity.account_id,
                    identity.agent_id,
                    action,
                    idempotency_key,
                    request_hash,
                    response_blob,
                    now.saturating_add(7 * 24 * 60 * 60 * 1000)
                ],
            )
            .map_err(db_error)?;
        transaction.commit().map_err(db_error)?;
        // MEM-NFR-01-01 hot-query cache: any committed write outside the
        // session/instruction bookkeeping domains can change long-term
        // recall results (content, FTS rows, embeddings, lexical backfill
        // state, a restored database, projection mappings), so it drops the
        // memoized search results. Session appends and instruction edits are
        // the high-frequency write paths and never touch long-term recall,
        // which is exactly why they are excluded. Unknown future record
        // types invalidate too: stale recall after a write is the one
        // failure this cache must never produce.
        if !matches!(
            record_type,
            "session"
                | "session_event"
                | "session_summary"
                | "checkpoint"
                | "instruction"
                | "sync_job"
                | "settings"
        ) {
            self.search_cache.invalidate();
        }
        Ok(result)
    }

    pub(crate) fn cached_write_response(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
        action: &str,
    ) -> ServiceResult<Option<Value>> {
        let idempotency_key = request
            .idempotency_key
            .as_deref()
            .unwrap_or(&request.request_id);
        if idempotency_key.len() > 512 {
            return Err(ServiceError::invalid("idempotency_key is too long"));
        }
        let request_hash = hash_json(&json!({"method":request.method,"params":request.params}));
        let connection = self.connection()?;
        let cached: Option<(String, Vec<u8>)> = connection
            .query_row(
                "SELECT request_hash,response_blob FROM idempotency_keys
                 WHERE account_id=?1 AND agent_id=?2 AND operation=?3 AND idempotency_key=?4 AND expires_at>?5",
                params![identity.account_id, identity.agent_id, action, idempotency_key, now_ms()],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()
            .map_err(db_error)?;
        let Some((old_hash, response_blob)) = cached else {
            return Ok(None);
        };
        if old_hash != request_hash {
            return Err(ServiceError::conflict(
                "idempotency key was already used with different parameters",
            ));
        }
        serde_json::from_slice(&response_blob)
            .map(Some)
            .map_err(|_| ServiceError::unavailable("stored idempotent response is invalid"))
    }

    pub(crate) fn enqueue_outbox(
        transaction: &Transaction<'_>,
        identity: &MemoryIdentity,
        record_type: &str,
        record_id: &str,
        operation: &str,
        revision: i64,
        hlc: &str,
        payload_hash: &str,
        sync_policy: &str,
        now: i64,
    ) -> ServiceResult<()> {
        if sync_policy == "local_only"
            || identity.account_id == "default"
            || identity.account_id.starts_with("local:")
        {
            return Ok(());
        }
        transaction
            .execute(
                "INSERT INTO outbox(
                    op_id,account_id,agent_id,device_id,record_type,record_id,operation,revision,
                    hybrid_clock,payload_ciphertext,payload_hash,sync_policy,created_at,status
                 ) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,X'',?10,?11,?12,'awaiting_encryption')",
                params![
                    Uuid::new_v4().to_string(),
                    identity.account_id,
                    identity.agent_id,
                    identity.device_id,
                    record_type,
                    record_id,
                    operation,
                    revision,
                    hlc,
                    payload_hash,
                    sync_policy,
                    now
                ],
            )
            .map_err(db_error)?;
        Ok(())
    }

    pub(crate) fn effective_expiry(
        transaction: &Transaction<'_>,
        identity: &MemoryIdentity,
        explicit: Option<i64>,
        now: i64,
    ) -> ServiceResult<Option<i64>> {
        if explicit.is_some() {
            return Ok(explicit);
        }
        let retention_days: Option<i64> = transaction
            .query_row(
                "SELECT retention_days FROM memory_settings
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3",
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id
                ],
                |row| row.get(0),
            )
            .optional()
            .map_err(db_error)?
            .flatten();
        Ok(retention_days.map(|days| now.saturating_add(days.saturating_mul(24 * 60 * 60 * 1000))))
    }
}

pub fn migrate_plaintext_database(
    path: impl AsRef<Path>,
    database_key: &[u8],
) -> ServiceResult<PathBuf> {
    let path = path.as_ref();
    if database_key.len() != 32 {
        return Err(ServiceError::invalid("database key must contain 32 bytes"));
    }
    let source_metadata = fs::symlink_metadata(path)
        .map_err(|_| ServiceError::not_found())?;
    if !source_metadata.file_type().is_file() {
        return Err(ServiceError::not_found());
    }
    let parent = path
        .parent()
        .ok_or_else(|| ServiceError::invalid("database path has no parent"))?;
    // A process killed between export and publish can leave an encrypted
    // staging file behind. It is safe to remove only our exact, hidden
    // migration prefix before starting a new migration; unrelated files and
    // the plaintext rollback copy are never touched here.
    cleanup_migration_temporary_files(path)?;
    let temporary = parent.join(format!(
        ".{}.encrypted-{}",
        path.file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("memory.db"),
        Uuid::new_v4()
    ));
    let backup = parent.join(format!(
        "{}.plaintext-backup-{}-{}",
        path.file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("memory.db"),
        now_ms(),
        Uuid::new_v4()
    ));
    let connection = Connection::open(path).map_err(db_error)?;
    let integrity: String = connection
        .query_row("PRAGMA quick_check", [], |row| row.get(0))
        .map_err(db_error)?;
    if integrity != "ok" {
        return Err(ServiceError::unavailable(
            "plaintext database failed integrity validation",
        ));
    }
    connection
        .execute_batch("PRAGMA wal_checkpoint(TRUNCATE);")
        .map_err(db_error)?;
    let key_literal = format!("x'{}'", hex::encode(database_key));
    connection
        .execute(
            &format!("ATTACH DATABASE ?1 AS encrypted KEY \"{key_literal}\""),
            [temporary.to_string_lossy()],
        )
        .map_err(db_error)?;
    let export_result = connection
        .query_row("SELECT sqlcipher_export('encrypted')", [], |row| {
            row.get::<_, Option<String>>(0)
        })
        .map_err(db_error);
    let detach_result = connection.execute_batch("DETACH DATABASE encrypted;");
    drop(connection);
    if let Err(error) = export_result {
        let _ = fs::remove_file(&temporary);
        return Err(error);
    }
    if let Err(error) = detach_result {
        let _ = fs::remove_file(&temporary);
        return Err(db_error(error));
    }
    if let Err(error) = Store::open_encrypted(&temporary, database_key)
        .and_then(|store| store.health().map(|_| ()))
    {
        cleanup_migration_artifacts(&temporary);
        return Err(error);
    }
    // Opening the staging DB performs migrations and creates a product config
    // next to the staging filename. It is only a validation artifact: the
    // live database must keep using the existing `<db>.config.json` path.
    cleanup_migration_sidecars(&temporary);
    fs::rename(path, &backup)
        .map_err(|error| {
            cleanup_migration_artifacts(&temporary);
            ServiceError::unavailable(format!("unable to preserve plaintext database: {error}"))
        })?;
    if let Err(error) = fs::set_permissions(&backup, fs::Permissions::from_mode(0o600)) {
        let _ = fs::rename(&backup, path);
        cleanup_migration_artifacts(&temporary);
        return Err(ServiceError::unavailable(format!(
            "unable to restrict plaintext migration backup permissions: {error}"
        )));
    }
    if let Err(error) = fs::rename(&temporary, path) {
        let _ = fs::rename(&backup, path);
        cleanup_migration_artifacts(&temporary);
        return Err(ServiceError::unavailable(format!(
            "unable to install encrypted database: {error}"
        )));
    }
    if let Err(error) = fs::set_permissions(path, fs::Permissions::from_mode(0o600)) {
        let _ = fs::remove_file(path);
        let _ = fs::rename(&backup, path);
        cleanup_migration_artifacts(&temporary);
        return Err(ServiceError::unavailable(format!(
            "unable to restrict encrypted database permissions: {error}"
        )));
    }
    sync_directory(parent)?;
    for suffix in ["-shm", "-wal"] {
        let stale = PathBuf::from(format!("{}{}", path.display(), suffix));
        let _ = fs::remove_file(stale);
    }
    Ok(backup)
}

fn cleanup_migration_sidecars(path: &Path) {
    for sidecar in [
        PathBuf::from(format!("{}-wal", path.display())),
        PathBuf::from(format!("{}-shm", path.display())),
        path.with_extension("config.json"),
    ] {
        let _ = fs::remove_file(sidecar);
    }
}

fn cleanup_migration_artifacts(path: &Path) {
    let _ = fs::remove_file(path);
    cleanup_migration_sidecars(path);
}

/// Read-only inventory of every memory namespace in one database file.
///
/// The daemon already walks this exact namespace union for its background
/// scheduler (`background_namespaces`, background.rs), but nothing outside the
/// process can enumerate it: every protocol method is scoped to one identity's
/// `(account_id, agent_id, memory_space_id)`, so "is a retired test namespace
/// still holding rows" could only be answered by guessing account ids. This is
/// the same union plus per-table row counts and the `slot:` tags in use -- the
/// evidence behind `bianbu-agent-memoryd --report-namespaces`.
///
/// The connection is opened read-only and takes no schema action, so it is safe
/// to run while the service is serving: WAL readers do not block the writer,
/// and a report can never migrate or rewrite the database it describes.
pub fn report_namespaces(
    path: impl AsRef<Path>,
    database_key: Option<&[u8]>,
) -> ServiceResult<Value> {
    let path = path.as_ref();
    let connection = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(db_error)?;
    if let Some(key) = database_key {
        if key.len() != 32 {
            return Err(ServiceError::invalid("database key must contain 32 bytes"));
        }
        connection
            .execute_batch(&format!("PRAGMA key = \"x'{}'\";", hex::encode(key)))
            .map_err(db_error)?;
    }
    connection
        .execute_batch("PRAGMA busy_timeout=5000;")
        .map_err(db_error)?;
    let count_for = |table: &str| {
        format!(
            "(SELECT COUNT(*) FROM {table} AS counted
              WHERE counted.account_id=namespaces.account_id
                AND counted.agent_id=namespaces.agent_id
                AND counted.memory_space_id=namespaces.memory_space_id)"
        )
    };
    // Tombstones carry no memory_space_id (a deletion is namespace-independent
    // at the account/agent level), so they are counted one level up and report
    // as the same number for every space of that account/agent pair.
    let sql = format!(
        "WITH namespaces(account_id, agent_id, memory_space_id) AS (
             SELECT account_id, agent_id, memory_space_id FROM memory_settings
             UNION SELECT account_id, agent_id, memory_space_id FROM session_events
             UNION SELECT account_id, agent_id, memory_space_id FROM session_summaries
             UNION SELECT account_id, agent_id, memory_space_id FROM long_term_memories
             UNION SELECT account_id, agent_id, memory_space_id FROM instructions
         )
         SELECT namespaces.account_id, namespaces.agent_id, namespaces.memory_space_id,
                {}, {}, {}, {}, {},
                (SELECT COUNT(*) FROM tombstones AS counted
                  WHERE counted.account_id=namespaces.account_id
                    AND counted.agent_id=namespaces.agent_id)
         FROM namespaces
         ORDER BY namespaces.account_id, namespaces.agent_id, namespaces.memory_space_id",
        count_for("long_term_memories"),
        count_for("memory_embeddings"),
        count_for("session_events"),
        count_for("session_summaries"),
        count_for("instructions"),
    );
    let mut statement = connection.prepare(&sql).map_err(db_error)?;
    let rows = statement
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, i64>(3)?,
                row.get::<_, i64>(4)?,
                row.get::<_, i64>(5)?,
                row.get::<_, i64>(6)?,
                row.get::<_, i64>(7)?,
                row.get::<_, i64>(8)?,
            ))
        })
        .map_err(db_error)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(db_error)?;
    drop(statement);
    let mut slot_statement = connection
        .prepare(
            "SELECT DISTINCT substr(tag.value, 6)
             FROM long_term_memories AS memory, json_each(memory.tags_json) AS tag
             WHERE memory.account_id=?1 AND memory.agent_id=?2 AND memory.memory_space_id=?3
               -- `tag.type` is the array element's type; json_type() would
               -- parse the element as a JSON document instead (and fail on
               -- `slot:...`).
               AND tag.type='text'
               AND tag.value LIKE 'slot:%' AND length(tag.value) > 5
             ORDER BY 1",
        )
        .map_err(db_error)?;
    let mut namespaces = Vec::with_capacity(rows.len());
    for (account_id, agent_id, memory_space_id, memories, embeddings, events, summaries, instructions, tombstones) in rows {
        // The declared slots are what MEM-L3-02 is about: a `slot:` tag is the
        // memory claiming "this text is that field's value", so the caller can
        // check every name here against the ontology dictionary. Reported as
        // names only -- never the text they were written with.
        let declared_slots = slot_statement
            .query_map(params![account_id, agent_id, memory_space_id], |row| {
                row.get::<_, String>(0)
            })
            .map_err(db_error)?
            .collect::<Result<Vec<_>, _>>()
            .map_err(db_error)?;
        namespaces.push(json!({
            "account_id":account_id,
            "agent_id":agent_id,
            "memory_space_id":memory_space_id,
            "counts":{
                "long_term_memories":memories,
                "memory_embeddings":embeddings,
                "session_events":events,
                "session_summaries":summaries,
                "instructions":instructions,
                "tombstones":tombstones
            },
            "declared_slots":declared_slots
        }));
    }
    let long_term_total: i64 = namespaces
        .iter()
        .map(|entry| entry["counts"]["long_term_memories"].as_i64().unwrap_or(0))
        .sum();
    Ok(json!({
        "database":path,
        "storage_encryption":if database_key.is_some() {"sqlcipher"} else {"plaintext_explicit"},
        "namespace_count":namespaces.len(),
        "long_term_memories":long_term_total,
        "namespaces":namespaces
    }))
}

/// Remove only migration backups created by `migrate_plaintext_database`.
///
/// The exact filename prefix prevents an operator cleanup from touching the
/// live database, capsules, or unrelated files in the data directory.
pub fn cleanup_plaintext_backups(path: impl AsRef<Path>) -> ServiceResult<u64> {
    let path = path.as_ref();
    let parent = path
        .parent()
        .ok_or_else(|| ServiceError::invalid("database path has no parent"))?;
    let filename = path
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| ServiceError::invalid("database path has no filename"))?;
    let prefix = format!("{filename}.plaintext-backup-");
    let entries = fs::read_dir(parent)
        .map_err(|_| ServiceError::unavailable("database directory is unavailable"))?;
    let mut removed = 0_u64;
    for entry in entries {
        let entry =
            entry.map_err(|_| ServiceError::unavailable("database directory is unreadable"))?;
        let name = entry.file_name();
        let Some(name) = name.to_str() else { continue };
        if !name.starts_with(&prefix) {
            continue;
        }
        let metadata = fs::symlink_metadata(entry.path())
            .map_err(|_| ServiceError::unavailable("migration backup metadata is unavailable"))?;
        if !metadata.file_type().is_file() {
            return Err(ServiceError::new(
                "IntegrityError",
                "refusing to remove a non-regular plaintext migration backup",
            ));
        }
        fs::remove_file(entry.path()).map_err(|_| {
            ServiceError::unavailable("plaintext migration backup cannot be removed")
        })?;
        removed = removed.saturating_add(1);
    }
    Ok(removed)
}

/// Remove interrupted encrypted migration staging files for one database.
/// This intentionally does not remove plaintext rollback backups; those are
/// operator-owned until an explicit `cleanup_plaintext_backups` action.
pub fn cleanup_migration_temporary_files(path: impl AsRef<Path>) -> ServiceResult<u64> {
    let path = path.as_ref();
    let parent = path
        .parent()
        .ok_or_else(|| ServiceError::invalid("database path has no parent"))?;
    let filename = path
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| ServiceError::invalid("database path has no filename"))?;
    let prefix = format!(".{filename}.encrypted-");
    let entries = fs::read_dir(parent)
        .map_err(|_| ServiceError::unavailable("database directory is unavailable"))?;
    let mut removed = 0_u64;
    for entry in entries {
        let entry = entry
            .map_err(|_| ServiceError::unavailable("database directory is unreadable"))?;
        let name = entry.file_name();
        let Some(name) = name.to_str() else { continue };
        if !name.starts_with(&prefix) {
            continue;
        }
        let metadata = fs::symlink_metadata(entry.path())
            .map_err(|_| ServiceError::unavailable("migration temporary metadata is unavailable"))?;
        if !metadata.file_type().is_file() {
            return Err(ServiceError::new(
                "IntegrityError",
                "refusing to remove a non-regular migration temporary",
            ));
        }
        fs::remove_file(entry.path()).map_err(|_| {
            ServiceError::unavailable("migration temporary file cannot be removed")
        })?;
        removed = removed.saturating_add(1);
    }
    Ok(removed)
}

fn sync_directory(path: &Path) -> ServiceResult<()> {
    let directory = fs::File::open(path)
        .map_err(|_| ServiceError::unavailable("database directory cannot be opened"))?;
    directory
        .sync_all()
        .map_err(|_| ServiceError::unavailable("database directory sync failed"))
}

fn identity_mismatch(identity: &MemoryIdentity, params: &Value) -> bool {
    let Some(object) = params.as_object() else {
        return false;
    };
    [
        ("account_id", identity.account_id.as_str()),
        ("device_id", identity.device_id.as_str()),
        ("agent_id", identity.agent_id.as_str()),
        ("agent_instance_id", identity.agent_instance_id.as_str()),
        ("memory_space_id", identity.memory_space_id.as_str()),
    ]
    .iter()
    .any(|(key, expected)| {
        object
            .get(*key)
            .and_then(Value::as_str)
            .is_some_and(|provided| provided != *expected)
    })
}

fn is_audited_read(method: &str) -> bool {
    matches!(
        method,
        "instructions.list"
            | "sessions.get_context"
            | "sessions.get_recent_context"
            | "long_term.search_headers"
            | "long_term.get_many"
            | "projection.resolve"
            | "projection.status"
            | "projection.list_dead"
            | "long_term.wait_forget"
            | "account.status"
            | "sync.status"
            | "runtime.restore_checkpoint"
            | "runtime.list_checkpoints"
            | "privacy.settings.get"
            | "privacy.export_data"
            | "audit.list"
            | "audit.export"
    )
}

fn is_memory_data_method(method: &str) -> bool {
    matches!(
        method,
        "privacy.import_data"
            | "instructions.put"
            | "instructions.list"
            | "instructions.revise"
            | "sessions.append_event"
            | "sessions.update_summary"
            | "sessions.get_context"
            | "sessions.get_recent_context"
            | "sessions.summarize"
            | "sessions.compact"
            | "long_term.put"
            | "long_term.search_headers"
            | "long_term.get_many"
            | "long_term.revise"
            | "long_term.reconcile_slots"
            | "projection.enqueue_turn"
            | "projection.resolve"
            | "projection.list_dead"
            | "projection.requeue_dead"
    )
}

fn is_mandatory_audit_action(action: &str) -> bool {
    matches!(
        action,
        "long_term.forget"
            | "privacy.clear_history"
            | "privacy.clear_all"
            | "privacy.import_data"
            | "privacy.settings.update"
            | "projection.requeue_dead"
            | "long_term.reconcile_slots"
    )
}

pub(crate) fn parse_params<T: DeserializeOwned>(request: &RequestEnvelope) -> ServiceResult<T> {
    serde_json::from_value(request.params.clone())
        .map_err(|error| ServiceError::invalid(format!("invalid parameters: {error}")))
}

pub(crate) fn db_error(error: rusqlite::Error) -> ServiceError {
    match error {
        rusqlite::Error::SqliteFailure(code, _)
            if code.code == rusqlite::ErrorCode::DatabaseBusy =>
        {
            let mut result = ServiceError::new("ResourceExhausted", "database writer is busy");
            result.retryable = true;
            result.retry_after_ms = Some(50);
            result
        }
        _ => ServiceError::unavailable(&format!(
            "database operation failed: {error}"
        )),
    }
}

/// Run `PRAGMA quick_check` on a database whose write-ahead log is empty.
///
/// A populated WAL is what made this check untrustworthy on the shipped
/// SQLCipher build: an FTS5 index whose pages are still in the WAL was reported
/// as `malformed inverted index` while the same check on the checkpointed
/// database returned `ok` (docs/GATEWAY_EMBEDDING_INTEGRATION.md §8.2).  So the
/// verdict is only taken from a state that provably changed nothing while it
/// was read:
///
/// 1. `wal_checkpoint(TRUNCATE)` folds every frame into the main database and
///    truncates the log. It reports `busy` when a reader holds a snapshot or a
///    background batch holds the writer, and then the attempt is inconclusive;
/// 2. `BEGIN IMMEDIATE` takes the write lock, after which no other connection
///    can commit, so the log must stay empty. A background batch that slipped in
///    between 1 and 2 leaves frames behind and the attempt is inconclusive;
/// 3. only then is `quick_check` run.
fn empty_wal_quick_check(
    connection: &mut Connection,
    database: &Path,
) -> ServiceResult<IntegrityCheck> {
    let checkpoint = connection.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |row| {
        Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?))
    });
    match checkpoint {
        Ok((0, 0)) => {}
        Ok(_) => return Ok(IntegrityCheck::Busy),
        Err(error) if is_database_busy(&error) => return Ok(IntegrityCheck::Busy),
        Err(error) => return Err(db_error(error)),
    }
    let transaction = match connection.transaction_with_behavior(TransactionBehavior::Immediate) {
        Ok(transaction) => transaction,
        Err(error) if is_database_busy(&error) => return Ok(IntegrityCheck::Busy),
        Err(error) => return Err(db_error(error)),
    };
    if wal_file_bytes(database)? != 0 {
        transaction.rollback().map_err(db_error)?;
        return Ok(IntegrityCheck::Busy);
    }
    let check = transaction.query_row("PRAGMA quick_check", [], |row| row.get::<_, String>(0));
    match check {
        Ok(verdict) => {
            transaction.rollback().map_err(db_error)?;
            Ok(IntegrityCheck::Verdict(verdict))
        }
        Err(error) if is_database_busy(&error) => {
            transaction.rollback().map_err(db_error)?;
            Ok(IntegrityCheck::Busy)
        }
        Err(error) => Err(db_error(error)),
    }
}

/// Bytes currently in the `-wal` file; a missing file means an empty log.
fn wal_file_bytes(path: &Path) -> ServiceResult<u64> {
    match fs::metadata(PathBuf::from(format!("{}-wal", path.display()))) {
        Ok(metadata) => Ok(metadata.len()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(0),
        // Without the file size the check cannot prove that it read a
        // checkpointed database, so it must not return a verdict.
        Err(_) => Err(ServiceError::unavailable("database WAL metadata is unavailable")),
    }
}

/// A busy or locked database is an inconclusive integrity check, not a failure.
fn is_database_busy(error: &rusqlite::Error) -> bool {
    matches!(
        error,
        rusqlite::Error::SqliteFailure(code, _)
            if matches!(
                code.code,
                rusqlite::ErrorCode::DatabaseBusy | rusqlite::ErrorCode::DatabaseLocked
            )
    )
}

/// Shape the `system.health` integrity fields from the memoized check state.
///
/// `state.verdict` is the last published answer (`ok`, or a defect that is
/// reported as one); `state.unconfirmed` carries a complaint that is on file
/// but is not a verdict, and never derives `status`.
fn integrity_report(
    state: &IntegrityCheckState,
    state_name: &str,
    reason: Option<&str>,
    interval_ms: i64,
    attempts: usize,
) -> (String, String, Value) {
    let verdict = state.verdict.as_deref();
    let status = match verdict {
        Some("ok") => "ok",
        Some(_) => "degraded",
        None => "unknown",
    };
    let database = match verdict {
        Some(value) => value.to_owned(),
        // No verdict to report yet: distinguish "a writer was in the way" from
        // "nothing has been sampled", so a caller is not told `busy` when the
        // check simply has not run.
        None if state_name == "busy" => "busy".to_owned(),
        None => "unknown".to_owned(),
    };
    let age_ms = state
        .checked_at_ms
        .map(|checked_at_ms| now_ms().saturating_sub(checked_at_ms));
    (
        status.to_string(),
        database,
        json!({
            "state":state_name,
            "reason":reason,
            "verdict":verdict,
            "complaint":state.unconfirmed,
            "checked_at_ms":state.checked_at_ms,
            "age_ms":age_ms,
            "interval_ms":interval_ms,
            "attempts":attempts
        }),
    )
}

/// Minimum age of the memoized verdict before `system.health` runs the
/// exclusive `PRAGMA quick_check` again.
fn integrity_check_interval_ms() -> i64 {
    std::env::var("AGENT_MEMORY_HEALTH_CHECK_INTERVAL_MS")
        .ok()
        .and_then(|value| value.parse::<i64>().ok())
        .unwrap_or(INTEGRITY_CHECK_DEFAULT_INTERVAL_MS)
        .max(0)
}

/// Minimum quiet time since the last database write before `system.health`
/// trusts a `PRAGMA quick_check` verdict.
///
/// Calibrated on K3 (2026-09-22): with writers running, a check returns
/// `malformed inverted index for FTS5 table main.long_term_fts[2]` even when it
/// excludes the writer, holds the write lock and reads a checkpointed database;
/// the same check returns `ok` once the database has been left alone. `0`
/// disables the wait (diagnostics only) - see
/// docs/GATEWAY_EMBEDDING_INTEGRATION.md §8.2.
fn integrity_check_settle_ms() -> i64 {
    std::env::var("AGENT_MEMORY_HEALTH_SETTLE_MS")
        .ok()
        .and_then(|value| value.parse::<i64>().ok())
        .unwrap_or(INTEGRITY_CHECK_DEFAULT_SETTLE_MS)
        .max(0)
}

/// Does this `quick_check` complaint name an FTS5 inverted index?
///
/// The shipped SQLCipher build reports exactly this for indexes that are
/// intact, so it is the one complaint `system.health` does not treat as a
/// verdict - see `database_integrity`.
fn is_fts5_inverted_index_complaint(verdict: &str) -> bool {
    verdict.contains("malformed inverted index for FTS5 table")
}

fn database_size_bytes(path: &Path) -> ServiceResult<u64> {
    let mut total = 0_u64;
    let wal = PathBuf::from(format!("{}-wal", path.display()));
    let shm = PathBuf::from(format!("{}-shm", path.display()));
    for candidate in [path.to_path_buf(), wal, shm] {
        match fs::metadata(candidate) {
            Ok(metadata) if metadata.is_file() => total = total.saturating_add(metadata.len()),
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(_) => {
                return Err(ServiceError::unavailable(
                    "database storage metadata is unavailable",
                ))
            }
        }
    }
    Ok(total)
}

fn filesystem_free_bytes(path: &Path) -> ServiceResult<u64> {
    let directory = path.parent().unwrap_or_else(|| Path::new("."));
    let encoded = std::ffi::CString::new(directory.as_os_str().as_bytes())
        .map_err(|_| ServiceError::invalid("database path contains an invalid character"))?;
    let mut stats = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    // SAFETY: `encoded` is NUL terminated and `stats` points to writable
    // storage for libc; the initialized value is read only on success.
    let result = unsafe { libc::statvfs(encoded.as_ptr(), stats.as_mut_ptr()) };
    if result != 0 {
        return Err(ServiceError::unavailable("filesystem capacity is unavailable"));
    }
    let stats = unsafe { stats.assume_init() };
    (stats.f_bavail as u128)
        .checked_mul(stats.f_frsize as u128)
        .and_then(|bytes| u64::try_from(bytes).ok())
        .ok_or_else(|| ServiceError::unavailable("filesystem capacity is invalid"))
}

pub(crate) fn hash_text(value: &str) -> String {
    hash_bytes(value.as_bytes())
}

pub(crate) fn hash_json(value: &Value) -> String {
    let encoded = serde_json::to_vec(value).unwrap_or_default();
    hash_bytes(&encoded)
}

pub(crate) fn hash_bytes(value: &[u8]) -> String {
    hex::encode(Sha256::digest(value))
}

pub(crate) fn json_bytes(value: &Value) -> ServiceResult<Vec<u8>> {
    serde_json::to_vec(value).map_err(|_| ServiceError::invalid("value cannot be serialized"))
}

pub(crate) fn encode_json_payload(value: &Value) -> ServiceResult<(Vec<u8>, &'static str)> {
    let plain = json_bytes(value)?;
    if plain.len() < 4096 {
        return Ok((plain, "json"));
    }
    use flate2::{write::GzEncoder, Compression};
    use std::io::Write;
    let mut encoder = GzEncoder::new(Vec::new(), Compression::default());
    encoder
        .write_all(&plain)
        .map_err(|_| ServiceError::unavailable("payload compression failed"))?;
    let compressed = encoder
        .finish()
        .map_err(|_| ServiceError::unavailable("payload compression failed"))?;
    if compressed.len() < plain.len() {
        Ok((compressed, "gzip-json"))
    } else {
        Ok((plain, "json"))
    }
}

pub(crate) fn decode_json_payload(blob: &[u8], encoding: &str) -> ServiceResult<Value> {
    let plain = match encoding {
        "json" | "" => blob.to_vec(),
        "gzip-json" => {
            use flate2::read::GzDecoder;
            use std::io::Read;
            let mut decoder = GzDecoder::new(blob);
            let mut output = Vec::new();
            decoder
                .read_to_end(&mut output)
                .map_err(|_| ServiceError::unavailable("stored payload decompression failed"))?;
            output
        }
        _ => {
            return Err(ServiceError::unavailable(
                "stored payload encoding is unsupported",
            ))
        }
    };
    serde_json::from_slice(&plain)
        .map_err(|_| ServiceError::unavailable("stored payload JSON is invalid"))
}

fn next_hlc(transaction: &Transaction<'_>, now: i64, device_id: &str) -> ServiceResult<String> {
    let previous: Option<String> = transaction
        .query_row(
            "SELECT value FROM service_meta WHERE key='last_hlc'",
            [],
            |row| row.get(0),
        )
        .optional()
        .map_err(db_error)?;
    let old = previous.as_deref().and_then(Hlc::decode);
    let next = Hlc::tick(old.as_ref(), now, device_id).encode();
    transaction
        .execute(
            "INSERT INTO service_meta(key,value) VALUES('last_hlc',?1)
             ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [&next],
        )
        .map_err(db_error)?;
    Ok(next)
}

/// Bounded re-entrant gate around the CPU-heavy scan stages of search
/// (MEM-NFR-01-01). A thread that already holds a permit (nested
/// `search_headers` call, or store work spawned from inside a request such
/// as the post-commit embedding) passes straight through, so a scan can
/// never block waiting for a slot it itself holds — the gate is
/// deadlock-free by construction.
mod request_gate {
    use std::cell::Cell;
    use std::sync::{Condvar, Mutex, OnceLock};

    fn limit() -> usize {
        static LIMIT: OnceLock<usize> = OnceLock::new();
        *LIMIT.get_or_init(|| {
            // AGENT_MEMORY_REQUEST_PERMITS overrides the CPU-derived default —
            // the K3 acceptance tuning used exactly this knob to compare
            // queueing depth against per-request contention. The default
            // leaves two cores for the client/IPC work that shares the box
            // with the daemon (the MEM-NFR-01-01 bench drives 32 concurrent
            // clients from the same 8-core device): the K3 sweep measured
            // search p95 130ms at 6 permits vs 137/152/165ms at 8/10/12.
            std::env::var("AGENT_MEMORY_REQUEST_PERMITS")
                .ok()
                .and_then(|value| value.parse::<usize>().ok())
                .unwrap_or_else(|| {
                    std::thread::available_parallelism()
                        .map(|value| value.get())
                        .unwrap_or(4)
                        .saturating_sub(2)
                        .max(2)
                })
                .clamp(1, 64)
        })
    }

    struct State {
        available: Mutex<usize>,
        released: Condvar,
    }

    fn state() -> &'static State {
        static STATE: OnceLock<State> = OnceLock::new();
        STATE.get_or_init(|| State {
            available: Mutex::new(limit()),
            released: Condvar::new(),
        })
    }

    thread_local! {
        static DEPTH: Cell<usize> = const { Cell::new(0) };
    }

    pub(crate) struct Permit {
        /// whether this permit actually consumed a concurrency slot
        /// (only the outermost acquisition on a thread does)
        held_slot: bool,
    }

    impl Drop for Permit {
        fn drop(&mut self) {
            DEPTH.with(|depth| depth.set(depth.get().saturating_sub(1)));
            if !self.held_slot {
                return;
            }
            let state = state();
            if let Ok(mut available) = state.available.lock() {
                *available += 1;
                state.released.notify_one();
            }
        }
    }

    pub(crate) fn acquire() -> Permit {
        let depth = DEPTH.with(|depth| {
            let value = depth.get();
            depth.set(value + 1);
            value
        });
        if depth > 0 {
            return Permit { held_slot: false };
        }
        let state = state();
        let mut available = state
            .available
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        while *available == 0 {
            available = state
                .released
                .wait(available)
                .unwrap_or_else(|poisoned| poisoned.into_inner());
        }
        *available -= 1;
        Permit { held_slot: true }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const FTS5_DEFECT: &str = "malformed inverted index for FTS5 table main.long_term_fts";
    const FTS5_DEFECT_SIBLING: &str = "malformed inverted index for FTS5 table main.long_term_fts2";

    #[test]
    fn fts5_inverted_index_text_is_recognised_in_both_spellings() {
        // The build names whichever of the two FTS tables the sample picked;
        // both are the same non-verdict complaint.
        assert!(is_fts5_inverted_index_complaint(FTS5_DEFECT));
        assert!(is_fts5_inverted_index_complaint(FTS5_DEFECT_SIBLING));
        assert!(!is_fts5_inverted_index_complaint(
            "database disk image is malformed"
        ));
        // A page-level defect text that merely mentions FTS5 is still a defect.
        assert!(!is_fts5_inverted_index_complaint(
            "wrong number of entries in index long_term_fts"
        ));
    }

    #[test]
    fn a_complaint_on_file_never_derives_the_status() {
        let mut state = IntegrityCheckState::default();
        state.unconfirmed = Some(FTS5_DEFECT_SIBLING.to_owned());
        let (status, database, check) =
            integrity_report(&state, "complaint", Some(FTS5_INVERTED_INDEX_REASON), 30_000, 1);
        assert_eq!(status, "unknown");
        assert_eq!(database, "unknown");
        assert_eq!(check["state"], "complaint");
        assert_eq!(check["reason"], FTS5_INVERTED_INDEX_REASON);
        assert_eq!(check["verdict"], Value::Null);
        assert_eq!(check["complaint"], FTS5_DEFECT_SIBLING);
    }

    #[test]
    fn a_published_verdict_is_what_derives_the_status() {
        let mut state = IntegrityCheckState::default();
        state.verdict = Some("ok".to_owned());
        let (status, database, _) = integrity_report(&state, "checked", None, 30_000, 1);
        assert_eq!(status, "ok");
        assert_eq!(database, "ok");
        state.verdict = Some("database disk image is malformed".to_owned());
        let (status, database, check) = integrity_report(&state, "checked", None, 30_000, 1);
        assert_eq!(status, "degraded");
        assert_eq!(database, "database disk image is malformed");
        assert_eq!(check["complaint"], Value::Null);
    }

    #[test]
    fn an_unknown_database_is_reported_as_unknown_not_busy() {
        let state = IntegrityCheckState::default();
        let (status, database, check) = integrity_report(&state, "complaint", None, 30_000, 1);
        assert_eq!(status, "unknown");
        assert_eq!(database, "unknown");
        assert_eq!(check["state"], "complaint");
        // A busy sample is what `busy` means, and only that.
        let (status, database, _) = integrity_report(&state, "busy", Some("database-busy"), 30_000, 3);
        assert_eq!(status, "unknown");
        assert_eq!(database, "busy");
    }
}
