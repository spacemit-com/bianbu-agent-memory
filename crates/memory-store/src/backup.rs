use crate::product::{converged_backup_scopes, BACKUP_SCOPES, IMPORTANT_MEMORY_TAG};
use crate::{db_error, hash_bytes, json_bytes, parse_params, ServiceResult, Store};
use aes_gcm::aead::{Aead, KeyInit};
use aes_gcm::{Aes256Gcm, Nonce};
use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine;
use flate2::read::GzDecoder;
use flate2::write::GzEncoder;
use flate2::Compression;
use memory_core::{now_ms, MemoryIdentity};
use memory_protocol::{RequestEnvelope, ServiceError};
use rand::rngs::OsRng;
use rand::RngCore;
use rusqlite::{params, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use uuid::Uuid;

const CAPSULE_FORMAT: &str = "bianbu-agent-memory.capsule.v1";
const CAPSULE_VERSION: i64 = 1;
const SCHEMA_VERSION: i64 = 1;
const CRYPTO_SUITE: &str = "AES-256-GCM+SHA256";
const MAX_CAPSULE_BYTES: u64 = 64 * 1024 * 1024;

#[derive(Clone, Debug)]
pub struct BackupObject {
    pub object_ref: String,
    pub object_size: u64,
}

#[derive(Clone, Debug, Default)]
pub struct BackupMetadata {
    pub account_id_hash: String,
    pub agent_id: String,
    pub device_id: String,
    pub memory_space_id: String,
}

pub trait BackupStorageAdapter: Send + Sync + std::fmt::Debug {
    fn name(&self) -> &'static str;
    /// Whether this adapter stores objects in a remote cloud service.
    fn is_cloud(&self) -> bool {
        false
    }
    fn put(&self, backup_id: &str, contents: &[u8]) -> ServiceResult<BackupObject>;
    fn put_with_metadata(
        &self,
        backup_id: &str,
        contents: &[u8],
        _metadata: &BackupMetadata,
    ) -> ServiceResult<BackupObject> {
        self.put(backup_id, contents)
    }
    /// Enforce an adapter-local byte ceiling before creating a new object.
    /// Remote adapters may rely on a server-side quota and keep the default.
    fn check_capacity(&self, _additional_bytes: u64, _max_bytes: u64) -> ServiceResult<()> {
        Ok(())
    }
    fn get(&self, object_ref: &str, max_bytes: u64) -> ServiceResult<Vec<u8>>;
    /// Best-effort cleanup for an object created before catalog commit. Existing
    /// cloud adapters may omit deletion until they provide safe namespace-scoped GC.
    fn remove(&self, _object_ref: &str) -> ServiceResult<()> {
        Err(ServiceError::unavailable(
            "backup adapter object cleanup is not configured",
        ))
    }
}

/// HTTP file adapter for encrypted Capsule objects.
///
/// Transport is delegated to the system curl binary so the daemon uses the
/// platform trust store without adding a large TLS dependency. Capsule bytes
/// are written to mode-0600 temporary files and the API key is supplied via
/// curl's config on stdin, never as a process argument or log message.
#[derive(Clone)]
pub struct HttpBackupStorage {
    base_url: String,
    api_key: zeroize::Zeroizing<String>,
    curl_program: String,
    timeout_seconds: u64,
}

impl std::fmt::Debug for HttpBackupStorage {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("HttpBackupStorage")
            .field("base_url", &self.base_url)
            .field("api_key", &"[redacted]")
            .field("curl_program", &self.curl_program)
            .field("timeout_seconds", &self.timeout_seconds)
            .finish()
    }
}

impl HttpBackupStorage {
    pub fn new(base_url: impl Into<String>, api_key: impl Into<String>) -> ServiceResult<Self> {
        let base_url = normalize_base_url(&base_url.into())?;
        let api_key = validate_api_key(&api_key.into())?;
        Ok(Self {
            base_url,
            api_key: zeroize::Zeroizing::new(api_key),
            curl_program: "curl".to_owned(),
            timeout_seconds: 30,
        })
    }

    /// Override curl executable and timeout for isolated tests or custom images.
    pub fn with_transport(mut self, curl_program: impl Into<String>, timeout_seconds: u64) -> Self {
        self.curl_program = curl_program.into();
        self.timeout_seconds = timeout_seconds.clamp(1, 300);
        self
    }

    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    fn endpoint(&self) -> String {
        format!("{}/v1/agent-memory/files", self.base_url)
    }

    fn object_url(&self, object_ref: &str) -> ServiceResult<String> {
        if object_ref.is_empty() || object_ref.chars().any(|character| character.is_control()) {
            return Err(ServiceError::new(
                "IntegrityError",
                "cloud object reference is invalid",
            ));
        }
        if object_ref.starts_with("https://") || object_ref.starts_with("http://") {
            if url_origin(object_ref) != url_origin(&self.base_url) {
                return Err(ServiceError::new(
                    "IntegrityError",
                    "cloud object reference is outside the configured origin",
                ));
            }
            return Ok(object_ref.to_owned());
        }
        if object_ref.starts_with('/') {
            let origin = self
                .base_url
                .split_once("//")
                .map(|(scheme, rest)| {
                    let host = rest.split('/').next().unwrap_or(rest);
                    format!("{scheme}//{host}")
                })
                .ok_or_else(|| ServiceError::new("IntegrityError", "cloud base URL is invalid"))?;
            return Ok(format!("{origin}{object_ref}"));
        }
        Ok(format!("{}/{}", self.endpoint(), object_ref))
    }

    fn request(
        &self,
        method: &str,
        url: &str,
        body: Option<&[u8]>,
        max_response_bytes: u64,
        extra_headers: &[(&str, String)],
    ) -> ServiceResult<(u16, Vec<u8>)> {
        if !matches!(method, "GET" | "POST" | "DELETE") {
            return Err(ServiceError::invalid(
                "unsupported cloud backup HTTP method",
            ));
        }
        if url.chars().any(|character| character.is_control()) {
            return Err(ServiceError::new(
                "IntegrityError",
                "cloud URL contains control characters",
            ));
        }
        let response_file = tempfile::NamedTempFile::new()
            .map_err(|_| ServiceError::unavailable("cloud response file cannot be created"))?;
        restrict_temp_file(response_file.as_file())?;
        let body_file = if let Some(contents) = body {
            let file = tempfile::NamedTempFile::new()
                .map_err(|_| ServiceError::unavailable("cloud request file cannot be created"))?;
            restrict_temp_file(file.as_file())?;
            fs::write(file.path(), contents)
                .map_err(|_| ServiceError::unavailable("cloud request file cannot be written"))?;
            Some(file)
        } else {
            None
        };
        let mut config = String::new();
        config.push_str(&format!("url = {}\n", curl_config_value(url)));
        config.push_str(&format!("request = {}\n", curl_config_value(method)));
        config.push_str(&format!(
            "output = {}\n",
            curl_config_value(&response_file.path().to_string_lossy())
        ));
        config.push_str("silent\nshow-error\n");
        config.push_str(&format!(
            "max-time = {}\nconnect-timeout = {}\n",
            self.timeout_seconds,
            self.timeout_seconds.min(10)
        ));
        config.push_str(&format!("max-filesize = {}\n", max_response_bytes));
        config.push_str(&format!(
            "header = {}\n",
            curl_config_value(&format!("X-API-Key: {}", self.api_key.as_str()))
        ));
        for (name, value) in extra_headers {
            config.push_str(&format!(
                "header = {}\n",
                curl_config_value(&format!("{name}: {value}"))
            ));
        }
        if let Some(file) = body_file.as_ref() {
            config.push_str(&format!(
                "data-binary = {}\n",
                curl_config_value(&format!("@{}", file.path().display()))
            ));
        }
        config.push_str("write-out = %{http_code}\n");

        let mut child = Command::new(&self.curl_program)
            // Do not load a user's ~/.curlrc: it could override TLS, proxy,
            // output or header settings for a privileged daemon request.
            .arg("-q")
            .arg("--config")
            .arg("-")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|_| ServiceError::unavailable("curl is not installed or cannot be started"))?;
        child
            .stdin
            .take()
            .ok_or_else(|| ServiceError::unavailable("curl stdin is unavailable"))?
            .write_all(config.as_bytes())
            .map_err(|_| ServiceError::unavailable("cloud request configuration failed"))?;
        let output = child
            .wait_with_output()
            .map_err(|_| ServiceError::unavailable("cloud request failed"))?;
        if !output.status.success() {
            if output.status.code() == Some(63) {
                return Err(ServiceError::new(
                    "ResourceExhausted",
                    "cloud response exceeds the size limit",
                ));
            }
            return Err(ServiceError::unavailable(format!(
                "cloud request transport failed (curl exit {})",
                output.status.code().unwrap_or(-1)
            )));
        }
        let status = String::from_utf8_lossy(&output.stdout)
            .trim()
            .parse::<u16>()
            .map_err(|_| ServiceError::unavailable("cloud response status is invalid"))?;
        let metadata = response_file
            .as_file()
            .metadata()
            .map_err(|_| ServiceError::unavailable("cloud response metadata is unavailable"))?;
        if metadata.len() > max_response_bytes {
            return Err(ServiceError::new(
                "ResourceExhausted",
                "cloud response exceeds the size limit",
            ));
        }
        let response = fs::read(response_file.path())
            .map_err(|_| ServiceError::unavailable("cloud response cannot be read"))?;
        Ok((status, response))
    }
}

impl BackupStorageAdapter for HttpBackupStorage {
    fn name(&self) -> &'static str {
        "http_api"
    }

    fn is_cloud(&self) -> bool {
        true
    }

    fn put(&self, backup_id: &str, contents: &[u8]) -> ServiceResult<BackupObject> {
        self.put_with_metadata(backup_id, contents, &BackupMetadata::default())
    }

    fn put_with_metadata(
        &self,
        backup_id: &str,
        contents: &[u8],
        metadata: &BackupMetadata,
    ) -> ServiceResult<BackupObject> {
        if !memory_core::valid_identifier(backup_id) {
            return Err(ServiceError::invalid("invalid backup_id"));
        }
        let mut headers = vec![
            ("Content-Type", "application/octet-stream".to_owned()),
            ("X-Agent-Memory-Backup-Id", backup_id.to_owned()),
            (
                "X-Agent-Memory-Capsule-Version",
                CAPSULE_VERSION.to_string(),
            ),
            ("X-Agent-Memory-Object-SHA256", crate::hash_bytes(contents)),
        ];
        for (name, value) in [
            (
                "X-Agent-Memory-Account-Id-Hash",
                metadata.account_id_hash.as_str(),
            ),
            ("X-Agent-Memory-Agent-Id", metadata.agent_id.as_str()),
            ("X-Agent-Memory-Device-Id", metadata.device_id.as_str()),
            ("X-Agent-Memory-Space-Id", metadata.memory_space_id.as_str()),
        ] {
            if !value.is_empty() {
                if value.chars().any(|character| character.is_control()) {
                    return Err(ServiceError::invalid(
                        "cloud backup metadata contains control characters",
                    ));
                }
                headers.push((name, value.to_owned()));
            }
        }
        let (status, response) = self.request(
            "POST",
            &self.endpoint(),
            Some(contents),
            1024 * 1024,
            &headers,
        )?;
        ensure_http_success(status)?;
        let parsed: Value = if response.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&response).map_err(|_| {
                ServiceError::new("IntegrityError", "cloud upload response is invalid JSON")
            })?
        };
        let data = parsed.get("data").unwrap_or(&parsed);
        let returned_id = data.get("backup_id").and_then(Value::as_str);
        if returned_id.is_some_and(|value| value != backup_id) {
            return Err(ServiceError::new(
                "IntegrityError",
                "cloud upload returned a different backup_id",
            ));
        }
        let object_ref = data
            .get("object_ref")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .map(str::to_owned)
            .unwrap_or_else(|| format!("{}/{}", self.endpoint(), backup_id));
        Ok(BackupObject {
            object_ref,
            object_size: data
                .get("object_size")
                .and_then(Value::as_u64)
                .unwrap_or(contents.len() as u64),
        })
    }

    fn get(&self, object_ref: &str, max_bytes: u64) -> ServiceResult<Vec<u8>> {
        let url = self.object_url(object_ref)?;
        let (status, response) = self.request("GET", &url, None, max_bytes, &[])?;
        ensure_http_success(status)?;
        Ok(response)
    }

    fn remove(&self, object_ref: &str) -> ServiceResult<()> {
        let url = self.object_url(object_ref)?;
        let (status, _) = self.request("DELETE", &url, None, 1024 * 1024, &[])?;
        if status == 404 {
            return Ok(());
        }
        ensure_http_success(status)
    }
}

fn normalize_base_url(value: &str) -> ServiceResult<String> {
    let trimmed = value.trim().trim_end_matches('/');
    if trimmed.is_empty()
        || !(trimmed.starts_with("https://") || trimmed.starts_with("http://"))
        || trimmed.chars().any(|character| character.is_control())
        || trimmed.contains('?')
        || trimmed.contains('#')
    {
        return Err(ServiceError::invalid(
            "backup base URL must be an http(s) URL without query or fragment",
        ));
    }
    let authority = trimmed
        .split_once("//")
        .and_then(|(_, rest)| rest.split('/').next())
        .unwrap_or_default();
    if authority.is_empty()
        || authority.chars().any(|character| character.is_whitespace())
        || authority.contains('@')
    {
        return Err(ServiceError::invalid("backup base URL host is invalid"));
    }
    if trimmed.starts_with("http://") && !is_loopback_url(trimmed) {
        return Err(ServiceError::invalid(
            "cloud backup requires HTTPS (HTTP is allowed only for loopback testing)",
        ));
    }
    Ok(trimmed.to_owned())
}

fn url_origin(value: &str) -> Option<String> {
    let (scheme, rest) = value.split_once("//")?;
    let authority = rest.split('/').next()?;
    Some(format!(
        "{}//{}",
        scheme.to_ascii_lowercase(),
        authority.to_ascii_lowercase()
    ))
}

fn is_loopback_url(value: &str) -> bool {
    value
        .split_once("//")
        .and_then(|(_, rest)| rest.split('/').next())
        .map(|host| {
            host.starts_with("127.0.0.1")
                || host.starts_with("localhost")
                || host.starts_with("[::1]")
        })
        .unwrap_or(false)
}

fn validate_api_key(value: &str) -> ServiceResult<String> {
    let trimmed = value.trim_matches([' ', '\t', '\r', '\n']);
    if trimmed.is_empty() || trimmed.chars().any(|character| character.is_control()) {
        return Err(ServiceError::invalid(
            "backup API key must be non-empty and contain no control characters",
        ));
    }
    Ok(trimmed.to_owned())
}

fn curl_config_value(value: &str) -> String {
    format!(
        "\"{}\"",
        value
            .replace('\\', "\\\\")
            .replace('\"', "\\\"")
            .replace('\n', "\\n")
            .replace('\r', "\\r")
    )
}

fn restrict_temp_file(file: &std::fs::File) -> ServiceResult<()> {
    file.set_permissions(fs::Permissions::from_mode(0o600))
        .map_err(|_| ServiceError::unavailable("temporary cloud file permissions cannot be set"))
}

fn ensure_http_success(status: u16) -> ServiceResult<()> {
    match status {
        200..=299 => Ok(()),
        404 => Err(ServiceError::not_found()),
        401 | 403 => Err(ServiceError::new(
            "PermissionDenied",
            "cloud backup API key was rejected",
        )),
        408 | 425 | 429 | 500..=599 => Err(ServiceError::unavailable(format!(
            "cloud backup service returned HTTP {status}"
        ))),
        _ => Err(ServiceError::new(
            "RemoteError",
            format!("cloud backup service returned HTTP {status}"),
        )),
    }
}

#[derive(Clone, Debug)]
pub struct LocalDirectoryBackupStorage {
    directory: PathBuf,
}

impl LocalDirectoryBackupStorage {
    pub fn new(directory: impl AsRef<Path>) -> Self {
        Self {
            directory: directory.as_ref().to_path_buf(),
        }
    }

    fn prepare_directory(&self) -> ServiceResult<()> {
        fs::create_dir_all(&self.directory)
            .map_err(|_| ServiceError::unavailable("backup adapter directory is unavailable"))?;
        fs::set_permissions(&self.directory, fs::Permissions::from_mode(0o700)).map_err(|_| {
            ServiceError::unavailable("backup adapter directory permissions cannot be set")
        })
    }
}

impl BackupStorageAdapter for LocalDirectoryBackupStorage {
    fn name(&self) -> &'static str {
        "local_directory"
    }

    fn put(&self, backup_id: &str, contents: &[u8]) -> ServiceResult<BackupObject> {
        self.prepare_directory()?;
        let object_path = self.directory.join(format!("{backup_id}.capsule"));
        atomic_write(&object_path, contents)?;
        Ok(BackupObject {
            object_ref: object_path.to_string_lossy().into_owned(),
            object_size: contents.len() as u64,
        })
    }

    fn check_capacity(&self, additional_bytes: u64, max_bytes: u64) -> ServiceResult<()> {
        self.prepare_directory()?;
        let mut current = 0_u64;
        for entry in fs::read_dir(&self.directory)
            .map_err(|_| ServiceError::unavailable("backup adapter directory is unavailable"))?
        {
            let entry = entry
                .map_err(|_| ServiceError::unavailable("backup adapter directory is unreadable"))?;
            let metadata = fs::symlink_metadata(entry.path())
                .map_err(|_| ServiceError::unavailable("backup object metadata is unavailable"))?;
            if metadata.file_type().is_symlink() {
                return Err(ServiceError::new(
                    "IntegrityError",
                    "backup adapter directory contains a symlink",
                ));
            }
            if metadata.is_file() {
                current = current.saturating_add(metadata.len());
            }
        }
        if current.saturating_add(additional_bytes) > max_bytes {
            return Err(ServiceError::new(
                "ResourceExhausted",
                format!(
                    "backup storage limit exceeded ({} > {} bytes)",
                    current.saturating_add(additional_bytes),
                    max_bytes
                ),
            ));
        }
        Ok(())
    }

    fn get(&self, object_ref: &str, max_bytes: u64) -> ServiceResult<Vec<u8>> {
        self.prepare_directory()?;
        let requested = PathBuf::from(object_ref);
        let canonical_directory = fs::canonicalize(&self.directory)
            .map_err(|_| ServiceError::unavailable("backup adapter directory is unavailable"))?;
        let canonical_object =
            fs::canonicalize(&requested).map_err(|_| ServiceError::not_found())?;
        if !canonical_object.starts_with(&canonical_directory) {
            return Err(ServiceError::new(
                "IntegrityError",
                "capsule object is outside the configured adapter",
            ));
        }
        let metadata = fs::metadata(&canonical_object).map_err(|_| ServiceError::not_found())?;
        if !metadata.is_file() || metadata.len() > max_bytes {
            return Err(ServiceError::new(
                "ResourceExhausted",
                "invalid capsule object size",
            ));
        }
        fs::read(canonical_object).map_err(|_| ServiceError::not_found())
    }

    fn remove(&self, object_ref: &str) -> ServiceResult<()> {
        self.prepare_directory()?;
        let requested = PathBuf::from(object_ref);
        let canonical_directory = fs::canonicalize(&self.directory)
            .map_err(|_| ServiceError::unavailable("backup adapter directory is unavailable"))?;
        let canonical_object = match fs::canonicalize(&requested) {
            Ok(path) => path,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(_) => return Err(ServiceError::not_found()),
        };
        if !canonical_object.starts_with(&canonical_directory) {
            return Err(ServiceError::new(
                "IntegrityError",
                "capsule object is outside the configured adapter",
            ));
        }
        let metadata = fs::metadata(&canonical_object).map_err(|_| ServiceError::not_found())?;
        if !metadata.is_file() {
            return Err(ServiceError::new(
                "IntegrityError",
                "capsule object is not a regular file",
            ));
        }
        fs::remove_file(canonical_object)
            .map_err(|_| ServiceError::unavailable("capsule object cannot be removed"))
    }
}

#[derive(Debug, Deserialize)]
struct CreateBackup {
    scopes: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct ListBackups {
    #[serde(default = "default_limit")]
    limit: i64,
    #[serde(default)]
    device_id: Option<String>,
}

#[derive(Debug, Deserialize)]
struct BackupReference {
    backup_id: String,
}

#[derive(Debug, Deserialize)]
struct RestoreBackup {
    backup_id: String,
    #[serde(default)]
    confirm_token: Option<String>,
}

#[derive(Debug, Deserialize)]
struct RunScheduledBackup {
    #[serde(default)]
    force: bool,
}

#[derive(Debug, Serialize, Deserialize)]
struct CapsuleEnvelope {
    format: String,
    format_version: i64,
    schema_version: i64,
    backup_id: String,
    account_id_hash: String,
    agent_id: String,
    #[serde(default)]
    agent_name: String,
    device_id: String,
    memory_space_id: String,
    created_at: i64,
    crypto_suite: String,
    nonce_b64: String,
    payload_checksum: String,
    manifest_hash: String,
    ciphertext_b64: String,
}

fn default_limit() -> i64 {
    100
}

fn backup_interval_ms(frequency: &str) -> ServiceResult<i64> {
    let days = match frequency {
        "daily" => 1,
        "weekly" => 7,
        "monthly" => 30,
        "manual" => 0,
        _ => return Err(ServiceError::invalid("invalid configured backup frequency")),
    };
    Ok(days * 24 * 60 * 60 * 1000)
}

impl Store {
    pub(crate) fn run_scheduled_backup(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: RunScheduledBackup = parse_params(request)?;
        if is_default_account(&identity.account_id) {
            return Ok(json!({"state":"skipped","reason":"account_not_authenticated"}));
        }
        let connection = self.connection()?;
        let settings: Option<(i64, String, String)> = connection
            .query_row(
                "SELECT backup_enabled,backup_frequency,backup_scopes_json FROM memory_settings
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3",
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id
                ],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional()
            .map_err(db_error)?;
        let Some((enabled, frequency, scopes_json)) = settings else {
            return Ok(json!({"state":"skipped","reason":"backup_disabled"}));
        };
        if enabled == 0 {
            return Ok(json!({"state":"skipped","reason":"backup_disabled"}));
        }
        if frequency == "manual" && !input.force {
            return Ok(json!({"state":"not_due","frequency":frequency,"reason":"manual_only"}));
        }
        let last_created_at: Option<i64> = connection
            .query_row(
                "SELECT MAX(created_at) FROM backup_catalog
                 WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3",
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id
                ],
                |row| row.get(0),
            )
            .map_err(db_error)?;
        let now = now_ms();
        let interval_ms = backup_interval_ms(&frequency)?;
        let next_due_at = last_created_at.map(|created| created.saturating_add(interval_ms));
        if !input.force && next_due_at.is_some_and(|due| now < due) {
            return Ok(json!({
                "state":"not_due","frequency":frequency,
                "last_created_at":last_created_at,"next_due_at":next_due_at
            }));
        }
        // The stored list can still contain the retired `summaries` scope, so
        // converge it before it reaches backup.create: an unauthorized scope
        // would otherwise turn every scheduled pass into a background error.
        let stored_scopes = serde_json::from_str::<Vec<String>>(&scopes_json)
            .map_err(|_| ServiceError::unavailable("configured backup scopes are invalid"))?;
        let scopes = converged_backup_scopes(&stored_scopes);
        if scopes.is_empty() {
            // A 0.1.15 device could hold exactly ["summaries"]; nothing is
            // uploadable, which is a skip and not a failure.
            return Ok(json!({"state":"skipped","reason":"backup_scopes_converged"}));
        }
        let scheduled_request = RequestEnvelope {
            params: json!({"scopes":scopes}),
            idempotency_key: Some(format!(
                "scheduled:{}:{}:{}:{}",
                identity.memory_space_id,
                identity.device_id,
                frequency,
                next_due_at.unwrap_or(now)
            )),
            ..request.clone()
        };
        let mut result = self.create_backup(identity, &scheduled_request)?;
        result["scheduled"] = json!(true);
        result["frequency"] = json!(frequency);
        Ok(result)
    }

    pub(crate) fn create_backup(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: CreateBackup = parse_params(request)?;
        validate_scopes(&input.scopes)?;
        if is_default_account(&identity.account_id) {
            return Err(ServiceError::new(
                "Unauthenticated",
                "the local default account cannot create a cloud backup",
            ));
        }
        if let Some(cached) = self.cached_write_response(identity, request, "backup.create")? {
            return Ok(cached);
        }
        let storage = self.configured_backup_storage()?;
        let key = self.configured_capsule_key()?;
        let connection = self.connection()?;
        let settings: Option<(i64, String)> = connection
            .query_row(
                "SELECT backup_enabled,backup_scopes_json FROM memory_settings
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
        let (backup_enabled, allowed_scopes) = settings.unwrap_or((0, "[]".to_owned()));
        if backup_enabled == 0 {
            return Err(ServiceError::new(
                "PermissionDenied",
                "cloud backup is disabled by the current privacy setting",
            ));
        }
        let allowed: BTreeSet<String> = serde_json::from_str::<Vec<String>>(&allowed_scopes)
            .unwrap_or_default()
            .into_iter()
            .collect();
        if input.scopes.iter().any(|scope| !allowed.contains(scope)) {
            return Err(ServiceError::new(
                "PermissionDenied",
                "requested backup scope is not authorized",
            ));
        }

        let backup_id = Uuid::new_v4().to_string();
        let job_id = Uuid::new_v4().to_string();
        let created_at = now_ms();
        let payload =
            self.collect_capsule_payload(identity, &backup_id, created_at, &input.scopes)?;
        let manifest = payload
            .get("manifest")
            .cloned()
            .ok_or_else(|| ServiceError::unavailable("capsule manifest is missing"))?;
        let manifest_hash = crate::hash_json(&manifest);
        let payload_bytes = json_bytes(&payload)?;
        let compressed = gzip_compress(&payload_bytes)?;
        let payload_checksum = hash_bytes(&compressed);
        let mut nonce = [0_u8; 12];
        OsRng.fill_bytes(&mut nonce);
        let cipher = Aes256Gcm::new_from_slice(&key)
            .map_err(|_| ServiceError::unavailable("capsule cipher initialization failed"))?;
        let ciphertext = cipher
            .encrypt(Nonce::from_slice(&nonce), compressed.as_ref())
            .map_err(|_| ServiceError::unavailable("capsule encryption failed"))?;
        let envelope = CapsuleEnvelope {
            format: CAPSULE_FORMAT.to_owned(),
            format_version: CAPSULE_VERSION,
            schema_version: SCHEMA_VERSION,
            backup_id: backup_id.clone(),
            account_id_hash: crate::hash_text(&identity.account_id),
            agent_id: identity.agent_id.clone(),
            agent_name: identity.agent_name.clone(),
            device_id: identity.device_id.clone(),
            memory_space_id: identity.memory_space_id.clone(),
            created_at,
            crypto_suite: CRYPTO_SUITE.to_owned(),
            nonce_b64: BASE64.encode(nonce),
            payload_checksum,
            manifest_hash: manifest_hash.clone(),
            ciphertext_b64: BASE64.encode(ciphertext),
        };
        let encoded = serde_json::to_vec(&envelope)
            .map_err(|_| ServiceError::unavailable("capsule serialization failed"))?;
        let max_backup_bytes = self.product_config()?.max_backup_bytes.max(0) as u64;
        if encoded.len() as u64 > MAX_CAPSULE_BYTES || encoded.len() as u64 > max_backup_bytes {
            return Err(ServiceError::new(
                "ResourceExhausted",
                "capsule exceeds the configured backup size limit",
            ));
        }
        storage.check_capacity(encoded.len() as u64, max_backup_bytes)?;
        let object = storage.put_with_metadata(
            &backup_id,
            &encoded,
            &BackupMetadata {
                account_id_hash: crate::hash_text(&identity.account_id),
                agent_id: identity.agent_id.clone(),
                device_id: identity.device_id.clone(),
                memory_space_id: identity.memory_space_id.clone(),
            },
        )?;
        let scopes_json = serde_json::to_string(&input.scopes)
            .map_err(|_| ServiceError::invalid("invalid backup scopes"))?;

        let result = json!({
            "job_id":job_id,"state":"completed","backup_id":backup_id,
            "created_at":created_at,"object_size":object.object_size,"manifest_hash":manifest_hash,
            "adapter":storage.name(),"cloud_uploaded":storage.is_cloud()
        });
        let write_result = self.with_write(identity, request, "backup.create", "backup", |tx, _, _, _, _| {
            tx.execute(
                "INSERT INTO backup_jobs(job_id,account_id,agent_id,job_type,state,backup_id,progress,error_code,created_at,updated_at)
                 VALUES(?1,?2,?3,'create','completed',?4,100,NULL,?5,?5)",
                params![job_id, identity.account_id, identity.agent_id, backup_id, created_at],
            ).map_err(db_error)?;
            tx.execute(
                "INSERT INTO backup_catalog(
                    backup_id,account_id,agent_id,capsule_version,base_backup_id,sync_cursor,
                    manifest_hash,object_ref,created_at,retention_until,verification_state,
                    device_id,memory_space_id,scopes_json,object_size,crypto_suite
                 ) VALUES(?1,?2,?3,?4,NULL,NULL,?5,?6,?7,NULL,'verified_local',?8,?9,?10,?11,?12)",
                params![backup_id,identity.account_id,identity.agent_id,CAPSULE_VERSION,manifest_hash,
                        object.object_ref,created_at,identity.device_id,identity.memory_space_id,
                        scopes_json,object.object_size as i64,CRYPTO_SUITE],
            ).map_err(db_error)?;
            Ok((result.clone(), Some(backup_id.clone())))
        });
        if write_result.is_err() {
            // The Adapter created this exact opaque reference in this request, so removing it is
            // safe. Implementations must still validate their own namespace before deletion.
            let _ = storage.remove(&object.object_ref);
        }
        write_result
    }

    pub(crate) fn list_backups(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: ListBackups = parse_params(request)?;
        if !(1..=500).contains(&input.limit) {
            return Err(ServiceError::invalid("limit must be between 1 and 500"));
        }
        let connection = self.connection()?;
        let mut statement = connection
            .prepare(
                "SELECT backup_id,capsule_version,base_backup_id,manifest_hash,created_at,
                        verification_state,device_id,memory_space_id,scopes_json,object_size,crypto_suite
                 FROM backup_catalog
                 WHERE account_id=?1 AND agent_id=?2 AND (?3 IS NULL OR device_id=?3)
                 ORDER BY created_at DESC,backup_id DESC LIMIT ?4",
            )
            .map_err(db_error)?;
        let rows = statement
            .query_map(
                params![identity.account_id, identity.agent_id, input.device_id, input.limit],
                |row| {
                    let scopes: String = row.get(8)?;
                    Ok(json!({
                        "backup_id":row.get::<_,String>(0)?,
                        "capsule_version":row.get::<_,i64>(1)?,
                        "base_backup_id":row.get::<_,Option<String>>(2)?,
                        "manifest_hash":row.get::<_,String>(3)?,
                        "created_at":row.get::<_,i64>(4)?,
                        "verification_state":row.get::<_,String>(5)?,
                        "device_id":row.get::<_,Option<String>>(6)?,
                        "memory_space_id":row.get::<_,Option<String>>(7)?,
                        "scopes":serde_json::from_str::<Value>(&scopes).unwrap_or_else(|_| json!([])),
                        "object_size":row.get::<_,i64>(9)?,
                        "crypto_suite":row.get::<_,String>(10)?
                    }))
                },
            )
            .map_err(db_error)?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(db_error)
            .map(Value::Array)
    }

    pub(crate) fn preview_restore(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: BackupReference = parse_params(request)?;
        let payload = self.load_capsule(identity, &input.backup_id)?;
        let connection = self.connection()?;
        let mut add = 0_i64;
        let mut update = 0_i64;
        let mut unchanged = 0_i64;
        let mut deleted = 0_i64;
        let mut conflicts = Vec::new();
        for item in array(&payload, "long_term_memories") {
            let Some(memory_id) = item.get("memory_id").and_then(Value::as_str) else {
                continue;
            };
            let remote_revision = item.get("revision").and_then(Value::as_i64).unwrap_or(1);
            let local: Option<(i64, String)> = connection
                .query_row(
                    "SELECT current_revision,content FROM long_term_memories
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
            match local {
                None => add += 1,
                Some((revision, content)) if revision < remote_revision => update += 1,
                Some((revision, content))
                    if revision == remote_revision
                        && item.get("text").and_then(Value::as_str) == Some(content.as_str()) =>
                {
                    unchanged += 1
                }
                Some((revision, _)) => {
                    conflicts.push(json!({"record_type":"long_term","record_id":memory_id,"local_revision":revision,"remote_revision":remote_revision,"resolution":"local_or_explicit_newer"}));
                }
            }
        }
        for item in array(&payload, "tombstones") {
            if item.get("record_id").and_then(Value::as_str).is_some() {
                deleted += 1;
            }
        }
        let mut summaries_add = 0_i64;
        let mut summaries_update = 0_i64;
        let mut summaries_unchanged = 0_i64;
        for item in array(&payload, "session_summaries") {
            let Some(session_id) = item.get("session_id").and_then(Value::as_str) else {
                continue;
            };
            let remote_revision = item.get("revision").and_then(Value::as_i64).unwrap_or(1);
            let local_revision:Option<i64>=connection.query_row(
                "SELECT revision FROM session_summaries WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND session_id=?4 AND is_current=1",
                params![identity.account_id,identity.agent_id,identity.memory_space_id,session_id],|row|row.get(0)
            ).optional().map_err(db_error)?;
            match local_revision {
                None => summaries_add += 1,
                Some(revision) if revision < remote_revision => summaries_update += 1,
                Some(_) => summaries_unchanged += 1,
            }
        }
        Ok(json!({
            "backup_id":input.backup_id,
            "manifest":payload.get("manifest").cloned().unwrap_or(Value::Null),
            "changes":{"add":add,"update":update,"unchanged":unchanged,"delete":deleted,
                       "summaries_add":summaries_add,"summaries_update":summaries_update,"summaries_unchanged":summaries_unchanged},
            "conflicts":conflicts,
            "requires_confirmation":true
        }))
    }

    pub(crate) fn restore_backup(
        &self,
        identity: &MemoryIdentity,
        request: &RequestEnvelope,
    ) -> ServiceResult<Value> {
        let input: RestoreBackup = parse_params(request)?;
        if !input
            .confirm_token
            .as_deref()
            .is_some_and(|value| !value.is_empty())
        {
            return Err(ServiceError::invalid("confirm_token is required"));
        }
        let payload = self.load_capsule(identity, &input.backup_id)?;
        let job_id = Uuid::new_v4().to_string();
        let now = now_ms();
        self.with_write(identity, request, "backup.restore", "backup", |tx, hlc, _, _, _| {
            let mut deleted = 0_i64;
            let mut added = 0_i64;
            let mut updated = 0_i64;
            let mut unchanged = 0_i64;
            let mut conflicts = 0_i64;
            let mut tombstone_epochs = BTreeMap::<String,i64>::new();
            {
            let mut local_tombstones = tx.prepare(
                    "SELECT record_id,MAX(delete_epoch) FROM tombstones
                     WHERE account_id=?1 AND agent_id=?2 AND record_type='long_term'
                     GROUP BY record_id"
                ).map_err(db_error)?;
                let rows = local_tombstones.query_map(
                    params![identity.account_id,identity.agent_id],
                    |row| Ok((row.get::<_,String>(0)?,row.get::<_,i64>(1)?)),
                ).map_err(db_error)?;
                for row in rows {
                    let (record_id, epoch) = row.map_err(db_error)?;
                    tombstone_epochs.insert(record_id, epoch);
                }
                drop(local_tombstones);
            }
            for item in array(&payload, "tombstones") {
                let Some(record_id) = item.get("record_id").and_then(Value::as_str) else { continue; };
                if item.get("record_type").and_then(Value::as_str) != Some("long_term") { continue; }
                let epoch = item.get("delete_epoch").and_then(Value::as_i64).unwrap_or(0);
                tombstone_epochs.entry(record_id.to_owned()).and_modify(|value| *value=(*value).max(epoch)).or_insert(epoch);
                tx.execute("DELETE FROM long_term_fts WHERE memory_id=?1",[record_id]).map_err(db_error)?;
                super::long_term::delete_fts2_row(tx,record_id)?;
                tx.execute("DELETE FROM semantic_projection_mappings WHERE memory_id=?1",[record_id]).map_err(db_error)?;
                tx.execute("DELETE FROM memory_embeddings WHERE memory_id=?1",[record_id]).map_err(db_error)?;
                deleted += tx.execute(
                    "DELETE FROM long_term_memories WHERE memory_id=?1 AND account_id=?2 AND agent_id=?3 AND memory_space_id=?4 AND updated_at<=?5",
                    params![record_id,identity.account_id,identity.agent_id,identity.memory_space_id,epoch],
                ).map_err(db_error)? as i64;
                tx.execute(
                    "INSERT OR IGNORE INTO tombstones(tombstone_id,account_id,agent_id,record_type,record_id,deleted_revision,delete_epoch,reason_code,created_at,hlc)
                     VALUES(?1,?2,?3,'long_term',?4,?5,?6,'capsule_restore',?6,?7)",
                    params![item.get("tombstone_id").and_then(Value::as_str).unwrap_or(record_id),identity.account_id,identity.agent_id,record_id,
                            item.get("deleted_revision").and_then(Value::as_i64).unwrap_or(1),epoch,hlc],
                ).map_err(db_error)?;
            }
            for item in array(&payload, "long_term_memories") {
                let Some(memory_id) = item.get("memory_id").and_then(Value::as_str) else { continue; };
                let remote_revision = item.get("revision").and_then(Value::as_i64).unwrap_or(1);
                let remote_updated = item.get("updated_at").and_then(Value::as_i64).unwrap_or(0);
                if tombstone_epochs.get(memory_id).is_some_and(|epoch| *epoch >= remote_updated) {
                    unchanged += 1;
                    continue;
                }
                let local: Option<(i64,String)> = tx.query_row(
                    "SELECT current_revision,content FROM long_term_memories WHERE memory_id=?1 AND account_id=?2 AND agent_id=?3 AND memory_space_id=?4",
                    params![memory_id,identity.account_id,identity.agent_id,identity.memory_space_id],
                    |row| Ok((row.get(0)?,row.get(1)?)),
                ).optional().map_err(db_error)?;
                if let Some((local_revision, local_content)) = local {
                    let remote_content = item.get("text").and_then(Value::as_str).unwrap_or("");
                    if local_revision >= remote_revision {
                        if local_revision == remote_revision && local_content != remote_content { conflicts += 1; } else { unchanged += 1; }
                        continue;
                    }
                    let restored_revision = local_revision.max(remote_revision).saturating_add(1);
                    let mut revision_payload = item.clone();
                    revision_payload["revision"] = json!(restored_revision);
                    revision_payload["source_backup_id"] = json!(&input.backup_id);
                    let revision_blob = json_bytes(&revision_payload)?;
                    let revision_hash = hash_bytes(&revision_blob);
                    tx.execute(
                        "UPDATE long_term_memories SET content=?1,summary=?2,tags_json=?3,source=?4,sensitivity=?5,current_revision=?6,updated_at=?7,hlc=?8
                         WHERE memory_id=?9 AND account_id=?10 AND agent_id=?11 AND memory_space_id=?12",
                        params![remote_content,item.get("summary").and_then(Value::as_str),tags_json(item),item.get("source").and_then(Value::as_str).unwrap_or("capsule.restore"),
                                item.get("sensitivity").and_then(Value::as_str).unwrap_or("private"),restored_revision,now,hlc,memory_id,
                                identity.account_id,identity.agent_id,identity.memory_space_id],
                    ).map_err(db_error)?;
                    tx.execute(
                        "INSERT INTO memory_revisions(memory_id,revision,parent_revision,payload,payload_hash,author_device_id,author_agent_id,created_at,hlc)
                         VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9)",
                        params![memory_id,restored_revision,local_revision,revision_blob,revision_hash,identity.device_id,identity.agent_id,now,hlc],
                    ).map_err(db_error)?;
                    restore_evidence(tx,&payload,memory_id,remote_revision,restored_revision)?;
                    tx.execute("DELETE FROM long_term_fts WHERE memory_id=?1",[memory_id]).map_err(db_error)?;
                    super::long_term::delete_fts2_row(tx,memory_id)?;
                    tx.execute("DELETE FROM memory_embeddings WHERE memory_id=?1",[memory_id]).map_err(db_error)?;
                    index_restored(tx,memory_id,item)?;
                    updated += 1;
                } else {
                    let text = item.get("text").and_then(Value::as_str).unwrap_or("");
                    let created_at = item.get("created_at").and_then(Value::as_i64).unwrap_or(remote_updated);
                    let restored_revision = remote_revision.saturating_add(1);
                    let mut revision_payload = item.clone();
                    revision_payload["revision"] = json!(restored_revision);
                    revision_payload["source_backup_id"] = json!(&input.backup_id);
                    let revision_blob = json_bytes(&revision_payload)?;
                    let revision_hash = hash_bytes(&revision_blob);
                    tx.execute(
                        "INSERT INTO long_term_memories(memory_id,account_id,agent_id,memory_space_id,kind,content,summary,tags_json,source,confidence,sensitivity,current_revision,sync_policy,expires_at,created_at,updated_at,hlc)
                         VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,NULL,?10,?11,'account_agent',NULL,?12,?13,?14)",
                        params![memory_id,identity.account_id,identity.agent_id,identity.memory_space_id,item.get("kind").and_then(Value::as_str).unwrap_or("fact"),text,
                                item.get("summary").and_then(Value::as_str),tags_json(item),item.get("source").and_then(Value::as_str).unwrap_or("capsule.restore"),
                                item.get("sensitivity").and_then(Value::as_str).unwrap_or("private"),restored_revision,created_at,now,hlc],
                    ).map_err(db_error)?;
                    tx.execute(
                        "INSERT INTO memory_revisions(memory_id,revision,parent_revision,payload,payload_hash,author_device_id,author_agent_id,created_at,hlc)
                         VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9)",
                        params![memory_id,restored_revision,remote_revision,revision_blob,revision_hash,identity.device_id,identity.agent_id,now,hlc],
                    ).map_err(db_error)?;
                    restore_evidence(tx,&payload,memory_id,remote_revision,restored_revision)?;
                    index_restored(tx,memory_id,item)?;
                    added += 1;
                }
            }
            let mut summaries_added = 0_i64;
            let mut summaries_updated = 0_i64;
            for item in array(&payload, "session_summaries") {
                let Some(session_id) = item.get("session_id").and_then(Value::as_str) else {
                    continue;
                };
                let remote_revision = item.get("revision").and_then(Value::as_i64).unwrap_or(1);
                let local_revision: Option<i64> = tx.query_row(
                    "SELECT revision FROM session_summaries
                     WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND session_id=?4 AND is_current=1",
                    params![identity.account_id,identity.agent_id,identity.memory_space_id,session_id],
                    |row| row.get(0),
                ).optional().map_err(db_error)?;
                if local_revision.is_some_and(|revision| revision >= remote_revision) {
                    continue;
                }
                let restored_revision = local_revision.unwrap_or(0).max(remote_revision).saturating_add(1);
                let summary_payload = json_bytes(item.get("summary").unwrap_or(&Value::Null))?;
                tx.execute(
                    "UPDATE session_summaries SET is_current=0
                     WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND session_id=?4 AND is_current=1",
                    params![identity.account_id,identity.agent_id,identity.memory_space_id,session_id],
                ).map_err(db_error)?;
                tx.execute(
                    "INSERT INTO session_summaries(session_id,account_id,agent_id,memory_space_id,revision,parent_revision,summary_payload,
                        source_event_sequence,sync_policy,producer,model_ref,policy_version,created_at,hlc,is_current,expires_at)
                     VALUES(?1,?2,?3,?4,?5,?6,?7,?8,'summary_only','capsule.restore',NULL,1,?9,?10,1,NULL)",
                    params![session_id,identity.account_id,identity.agent_id,identity.memory_space_id,restored_revision,local_revision,
                            summary_payload,item.get("source_event_sequence").and_then(Value::as_i64).unwrap_or(0),now,hlc],
                ).map_err(db_error)?;
                if local_revision.is_some() { summaries_updated += 1; } else { summaries_added += 1; }
            }
            tx.execute(
                "INSERT INTO backup_jobs(job_id,account_id,agent_id,job_type,state,backup_id,progress,error_code,created_at,updated_at)
                 VALUES(?1,?2,?3,'restore','completed',?4,100,NULL,?5,?5)",
                params![job_id,identity.account_id,identity.agent_id,input.backup_id,now],
            ).map_err(db_error)?;
            Ok((json!({"job_id":job_id,"state":"completed","backup_id":input.backup_id,
                       "applied":{"added":added,"updated":updated,"deleted":deleted,"unchanged":unchanged,"conflicts":conflicts,
                                  "summaries_added":summaries_added,"summaries_updated":summaries_updated},
                       "indexes_rebuilt":true}),Some(input.backup_id.clone())))
        })
    }

    fn configured_backup_storage(&self) -> ServiceResult<std::sync::Arc<dyn BackupStorageAdapter>> {
        self.backup_storage.clone().ok_or_else(|| {
            ServiceError::new(
                "BackupUnavailable",
                "cloud backup adapter is not configured",
            )
        })
    }

    fn configured_capsule_key(&self) -> ServiceResult<[u8; 32]> {
        self.capsule_key.ok_or_else(|| {
            ServiceError::new(
                "BackupUnavailable",
                "capsule key provider is not configured",
            )
        })
    }

    fn collect_capsule_payload(
        &self,
        identity: &MemoryIdentity,
        backup_id: &str,
        created_at: i64,
        scopes: &[String],
    ) -> ServiceResult<Value> {
        let connection = self.connection()?;
        let scope_set = scopes.iter().map(String::as_str).collect::<BTreeSet<_>>();
        // MEM-L4-02: a Capsule must not contain L2 conversation summaries, so
        // the payload never collects them. The key stays an empty array to keep
        // the Capsule shape stable for the preview/restore readers; Capsules
        // written by 0.1.15 remain restorable, they just do not grow a new copy.
        let summaries: Vec<Value> = Vec::new();
        let mut memories = Vec::new();
        let mut statement = connection.prepare(
            "SELECT json_object('memory_id',memory_id,'kind',kind,'text',content,'summary',summary,'tags',json(tags_json),'source',source,'sensitivity',sensitivity,
                    'revision',current_revision,'sync_policy',sync_policy,'created_at',created_at,'updated_at',updated_at,'hlc',hlc)
             FROM long_term_memories WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3 AND sync_policy!='local_only' AND sensitivity NOT IN ('secret','sensitive')
             ORDER BY updated_at,memory_id"
        ).map_err(db_error)?;
        let rows = statement
            .query_map(
                params![
                    identity.account_id,
                    identity.agent_id,
                    identity.memory_space_id
                ],
                |row| row.get::<_, String>(0),
            )
            .map_err(db_error)?;
        for row in rows {
            let encoded = row.map_err(db_error)?;
            let value = serde_json::from_str::<Value>(&encoded)
                .map_err(|_| ServiceError::unavailable("canonical record is invalid"))?;
            if memory_matches_scopes(&value, &scope_set) {
                memories.push(value);
            }
        }
        let mut revision_evidence = Vec::new();
        for value in &memories {
            let Some(memory_id) = value.get("memory_id").and_then(Value::as_str) else {
                continue;
            };
            let current_revision = value.get("revision").and_then(Value::as_i64).unwrap_or(1);
            let row: Option<(Vec<u8>, String, String, String, i64)> = connection
                .query_row(
                    "SELECT payload,payload_hash,author_device_id,author_agent_id,created_at
                 FROM memory_revisions WHERE memory_id=?1 AND revision=?2",
                    params![memory_id, current_revision],
                    |row| {
                        Ok((
                            row.get(0)?,
                            row.get(1)?,
                            row.get(2)?,
                            row.get(3)?,
                            row.get(4)?,
                        ))
                    },
                )
                .optional()
                .map_err(db_error)?;
            if let Some((
                payload,
                payload_hash,
                author_device_id,
                author_agent_id,
                revision_created_at,
            )) = row
            {
                revision_evidence.push(json!({
                    "memory_id":memory_id,"revision":current_revision,
                    "payload":serde_json::from_slice::<Value>(&payload).unwrap_or(Value::Null),
                    "payload_hash":payload_hash,"author_device_id":author_device_id,
                    "author_agent_id":author_agent_id,"created_at":revision_created_at,
                    "evidence":collect_evidence(&connection,memory_id,current_revision)?
                }));
            }
        }
        let tombstones = collect_values(&connection,
            "SELECT json_object('tombstone_id',tombstone_id,'record_type',record_type,'record_id',record_id,'deleted_revision',deleted_revision,'delete_epoch',delete_epoch,'reason_code',reason_code,'hlc',hlc)
             FROM tombstones WHERE account_id=?1 AND agent_id=?2 AND ?3 IS NOT NULL ORDER BY delete_epoch,tombstone_id", identity)?;
        let max_sequence: i64 = connection.query_row(
            "SELECT COALESCE(MAX(event_sequence),0) FROM session_events WHERE account_id=?1 AND agent_id=?2 AND memory_space_id=?3",
            params![identity.account_id,identity.agent_id,identity.memory_space_id], |row| row.get(0)).map_err(db_error)?;
        Ok(json!({
            "manifest":{"format":CAPSULE_FORMAT,"backup_id":backup_id,"schema_version":SCHEMA_VERSION,
                "account_id_hash":crate::hash_text(&identity.account_id),"agent_id":identity.agent_id,
                "agent_name":identity.agent_name,"device_id":identity.device_id,
                "memory_space_id":identity.memory_space_id,"created_at":created_at,"scopes":scopes,
                "event_sequence_high_watermark":max_sequence,"tombstone_count":tombstones.len(),"record_count":summaries.len()+memories.len()},
            "session_summaries":summaries,"long_term_memories":memories,
            "revision_evidence":revision_evidence,"tombstones":tombstones
        }))
    }

    fn load_capsule(&self, identity: &MemoryIdentity, backup_id: &str) -> ServiceResult<Value> {
        if !memory_core::valid_identifier(backup_id) {
            return Err(ServiceError::invalid("invalid backup_id"));
        }
        let storage = self.configured_backup_storage()?;
        let key = self.configured_capsule_key()?;
        let connection = self.connection()?;
        let object_ref: Option<String> = connection.query_row(
            "SELECT object_ref FROM backup_catalog WHERE backup_id=?1 AND account_id=?2 AND agent_id=?3",
            params![backup_id,identity.account_id,identity.agent_id], |row| row.get(0)).optional().map_err(db_error)?;
        let object_ref = object_ref.ok_or_else(ServiceError::not_found)?;
        let encoded = storage.get(&object_ref, MAX_CAPSULE_BYTES)?;
        let envelope: CapsuleEnvelope = serde_json::from_slice(&encoded)
            .map_err(|_| ServiceError::new("IntegrityError", "capsule envelope is invalid"))?;
        if envelope.format != CAPSULE_FORMAT
            || envelope.format_version != CAPSULE_VERSION
            || envelope.schema_version > SCHEMA_VERSION
            || envelope.backup_id != backup_id
            || envelope.account_id_hash != crate::hash_text(&identity.account_id)
            || envelope.agent_id != identity.agent_id
            || (!envelope.agent_name.is_empty() && envelope.agent_name != identity.agent_name)
        {
            return Err(ServiceError::new(
                "IntegrityError",
                "capsule identity or version check failed",
            ));
        }
        let nonce = BASE64
            .decode(&envelope.nonce_b64)
            .map_err(|_| ServiceError::new("IntegrityError", "capsule nonce is invalid"))?;
        let ciphertext = BASE64
            .decode(&envelope.ciphertext_b64)
            .map_err(|_| ServiceError::new("IntegrityError", "capsule ciphertext is invalid"))?;
        if nonce.len() != 12 {
            return Err(ServiceError::new(
                "IntegrityError",
                "capsule nonce length is invalid",
            ));
        }
        let cipher = Aes256Gcm::new_from_slice(&key)
            .map_err(|_| ServiceError::unavailable("capsule cipher initialization failed"))?;
        let compressed = cipher
            .decrypt(Nonce::from_slice(&nonce), ciphertext.as_ref())
            .map_err(|_| ServiceError::new("IntegrityError", "capsule authentication failed"))?;
        if hash_bytes(&compressed) != envelope.payload_checksum {
            return Err(ServiceError::new(
                "IntegrityError",
                "capsule checksum failed",
            ));
        }
        let payload_bytes = gzip_decompress(&compressed)?;
        let payload: Value = serde_json::from_slice(&payload_bytes)
            .map_err(|_| ServiceError::new("IntegrityError", "capsule payload is invalid"))?;
        if payload.get("manifest").map(crate::hash_json).as_deref()
            != Some(envelope.manifest_hash.as_str())
        {
            return Err(ServiceError::new(
                "IntegrityError",
                "capsule manifest hash failed",
            ));
        }
        Ok(payload)
    }
}

fn validate_scopes(scopes: &[String]) -> ServiceResult<()> {
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

fn is_default_account(account_id: &str) -> bool {
    account_id == "default" || account_id.starts_with("local:")
}

fn memory_matches_scopes(value: &Value, scopes: &BTreeSet<&str>) -> bool {
    let kind = value.get("kind").and_then(Value::as_str).unwrap_or("");
    let tags = value
        .get("tags")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let tagged = |prefix: &str| {
        tags.iter()
            .filter_map(Value::as_str)
            .any(|tag| tag.starts_with(prefix))
    };
    // Exact match, not a prefix: `important` is a reserved tag, and a prefix
    // test would also swallow look-alikes such as `important_note`.
    let has_tag = |name: &str| tags.iter().filter_map(Value::as_str).any(|tag| tag == name);
    (scopes.contains("preferences")
        && kind == "preference"
        && !tagged("slot:development.")
        && !tagged("slot:schedule."))
        || (scopes.contains("development_habits")
            && (tagged("slot:development.") || matches!(kind, "project" | "decision")))
        || (scopes.contains("schedule_habits") && tagged("slot:schedule."))
        // The user-designated important mark is orthogonal to `kind`, so a
        // marked `fact` or `decision` travels under this scope instead of being
        // silently dropped by the three content scopes above.
        || (scopes.contains("important") && has_tag(IMPORTANT_MEMORY_TAG))
}

fn collect_values(
    connection: &rusqlite::Connection,
    sql: &str,
    identity: &MemoryIdentity,
) -> ServiceResult<Vec<Value>> {
    let mut statement = connection.prepare(sql).map_err(db_error)?;
    let rows = statement
        .query_map(
            params![
                identity.account_id,
                identity.agent_id,
                identity.memory_space_id
            ],
            |row| row.get::<_, String>(0),
        )
        .map_err(db_error)?;
    rows.map(|row| {
        row.map_err(db_error).and_then(|encoded| {
            serde_json::from_str(&encoded)
                .map_err(|_| ServiceError::unavailable("canonical record is invalid"))
        })
    })
    .collect()
}

fn collect_evidence(
    connection: &rusqlite::Connection,
    memory_id: &str,
    revision: i64,
) -> ServiceResult<Vec<Value>> {
    let mut statement = connection
        .prepare(
            "SELECT evidence_type,evidence_id,evidence_hash FROM evidence_refs
         WHERE memory_id=?1 AND revision=?2 ORDER BY evidence_type,evidence_id",
        )
        .map_err(db_error)?;
    let rows = statement
        .query_map(params![memory_id, revision], |row| {
            Ok(json!({
                "evidence_type":row.get::<_,String>(0)?,
                "evidence_id":row.get::<_,String>(1)?,
                "evidence_hash":row.get::<_,Option<String>>(2)?
            }))
        })
        .map_err(db_error)?;
    rows.collect::<Result<Vec<_>, _>>().map_err(db_error)
}

fn restore_evidence(
    transaction: &rusqlite::Transaction<'_>,
    payload: &Value,
    memory_id: &str,
    source_revision: i64,
    restored_revision: i64,
) -> ServiceResult<()> {
    let evidence = array(payload, "revision_evidence")
        .iter()
        .find(|item| {
            item.get("memory_id").and_then(Value::as_str) == Some(memory_id)
                && item.get("revision").and_then(Value::as_i64) == Some(source_revision)
        })
        .and_then(|item| item.get("evidence"))
        .and_then(Value::as_array);
    for item in evidence.into_iter().flatten() {
        let Some(evidence_type) = item.get("evidence_type").and_then(Value::as_str) else {
            continue;
        };
        let Some(evidence_id) = item.get("evidence_id").and_then(Value::as_str) else {
            continue;
        };
        transaction.execute(
            "INSERT OR IGNORE INTO evidence_refs(memory_id,revision,evidence_type,evidence_id,evidence_hash)
             VALUES(?1,?2,?3,?4,?5)",
            params![memory_id,restored_revision,evidence_type,evidence_id,item.get("evidence_hash").and_then(Value::as_str)],
        ).map_err(db_error)?;
    }
    Ok(())
}

fn array<'a>(value: &'a Value, key: &str) -> &'a [Value] {
    value
        .get(key)
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[])
}
fn tags_json(value: &Value) -> String {
    value
        .get("tags")
        .cloned()
        .unwrap_or_else(|| json!([]))
        .to_string()
}
fn index_restored(
    tx: &rusqlite::Transaction<'_>,
    memory_id: &str,
    item: &Value,
) -> ServiceResult<()> {
    if !matches!(
        item.get("sensitivity").and_then(Value::as_str),
        Some("secret" | "sensitive")
    ) {
        let text = item.get("text").and_then(Value::as_str).unwrap_or("");
        let summary = item.get("summary").and_then(Value::as_str);
        let tags = tags_json(item);
        tx.execute(
            "INSERT INTO long_term_fts(memory_id,content,summary,tags) VALUES(?1,?2,?3,?4)",
            params![memory_id, text, summary, tags],
        )
        .map_err(db_error)?;
        // Bigram index twin (MEM-NFR-01-01). Restored rows index inline so
        // they never depend on the background backfill.
        let terms = super::long_term::lexical_tokens(text, summary, &tags);
        tx.execute(
            "INSERT OR REPLACE INTO long_term_fts2(rowid, memory_id, terms)
             SELECT rowid, ?2, ?3 FROM long_term_memories WHERE memory_id=?1",
            params![memory_id, memory_id, terms],
        )
        .map_err(db_error)?;
    }
    Ok(())
}

fn gzip_compress(input: &[u8]) -> ServiceResult<Vec<u8>> {
    let mut encoder = GzEncoder::new(Vec::new(), Compression::default());
    encoder
        .write_all(input)
        .map_err(|_| ServiceError::unavailable("capsule compression failed"))?;
    encoder
        .finish()
        .map_err(|_| ServiceError::unavailable("capsule compression failed"))
}
fn gzip_decompress(input: &[u8]) -> ServiceResult<Vec<u8>> {
    let decoder = GzDecoder::new(input);
    let mut output = Vec::new();
    decoder
        .take(MAX_CAPSULE_BYTES)
        .read_to_end(&mut output)
        .map_err(|_| ServiceError::new("IntegrityError", "capsule decompression failed"))?;
    Ok(output)
}

fn atomic_write(path: &Path, contents: &[u8]) -> ServiceResult<()> {
    let parent = path
        .parent()
        .ok_or_else(|| ServiceError::invalid("capsule path has no parent"))?;
    fs::create_dir_all(parent)
        .map_err(|_| ServiceError::unavailable("capsule directory is unavailable"))?;
    let temporary = parent.join(format!(".capsule-{}.tmp", Uuid::new_v4()));
    let mut file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .mode(0o600)
        .open(&temporary)
        .map_err(|_| ServiceError::unavailable("capsule temporary file cannot be created"))?;
    if let Err(error) = file.write_all(contents).and_then(|_| file.sync_all()) {
        let _ = fs::remove_file(&temporary);
        return Err(ServiceError::unavailable(format!(
            "capsule write failed: {error}"
        )));
    }
    drop(file);
    fs::rename(&temporary, path)
        .map_err(|_| ServiceError::unavailable("capsule cannot be committed"))?;
    Ok(())
}

#[cfg(test)]
mod http_tests {
    use super::*;
    use std::net::{TcpListener, TcpStream};
    use std::sync::mpsc;
    use std::time::Duration;

    fn read_http_request(stream: &mut TcpStream) -> (String, String, Vec<u8>) {
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .expect("set timeout");
        let mut bytes = Vec::new();
        let mut chunk = [0_u8; 4096];
        let header_end = loop {
            let count = stream.read(&mut chunk).expect("read request");
            assert!(count > 0, "request ended before headers");
            bytes.extend_from_slice(&chunk[..count]);
            if let Some(position) = bytes.windows(4).position(|window| window == b"\r\n\r\n") {
                break position + 4;
            }
            assert!(bytes.len() < 128 * 1024, "request headers are too large");
        };
        let header = String::from_utf8(bytes[..header_end].to_vec()).expect("utf8 headers");
        let content_length = header
            .lines()
            .find_map(|line| line.strip_prefix("Content-Length:"))
            .and_then(|value| value.trim().parse::<usize>().ok())
            .unwrap_or(0);
        while bytes.len() < header_end + content_length {
            let count = stream.read(&mut chunk).expect("read request body");
            assert!(count > 0, "request ended before body");
            bytes.extend_from_slice(&chunk[..count]);
        }
        let mut lines = header.lines();
        let request_line = lines.next().expect("request line");
        let mut request_parts = request_line.split_whitespace();
        let method = request_parts.next().unwrap_or_default().to_owned();
        let path = request_parts.next().unwrap_or_default().to_owned();
        let body = bytes[header_end..header_end + content_length].to_vec();
        (format!("{method} {path}"), header, body)
    }

    fn respond(stream: &mut TcpStream, status: &str, body: &[u8], content_type: &str) {
        let response = format!(
            "HTTP/1.1 {status}\r\nContent-Length: {}\r\nContent-Type: {content_type}\r\nConnection: close\r\n\r\n",
            body.len()
        );
        stream
            .write_all(response.as_bytes())
            .expect("write response");
        stream.write_all(body).expect("write body");
    }

    #[test]
    fn http_adapter_round_trips_capsule_and_api_key() {
        if Command::new("curl").arg("--version").output().is_err() {
            return;
        }
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind mock server");
        let address = listener.local_addr().expect("mock address");
        let (observed_sender, observed_receiver) = mpsc::channel();
        let server = std::thread::spawn(move || {
            for index in 0..3 {
                let (mut stream, _) = listener.accept().expect("accept mock request");
                let (request, header, body) = read_http_request(&mut stream);
                assert!(header.contains("X-API-Key: test-api-key"));
                if index == 0 {
                    assert!(header.contains("X-Agent-Memory-Agent-Id: agent-1"));
                    assert!(header.contains("X-Agent-Memory-Device-Id: device-1"));
                }
                observed_sender
                    .send((request.clone(), body.clone()))
                    .expect("send observation");
                match index {
                    0 => respond(
                        &mut stream,
                        "201 Created",
                        br#"{"backup_id":"backup-1","object_ref":"/v1/agent-memory/files/backup-1","object_size":7}"#,
                        "application/json",
                    ),
                    1 => respond(&mut stream, "200 OK", b"capsule", "application/octet-stream"),
                    _ => respond(&mut stream, "204 No Content", b"", "text/plain"),
                }
            }
        });
        let adapter = HttpBackupStorage::new(format!("http://{address}"), "test-api-key")
            .expect("construct adapter");
        let object = adapter
            .put_with_metadata(
                "backup-1",
                b"capsule",
                &BackupMetadata {
                    agent_id: "agent-1".to_owned(),
                    device_id: "device-1".to_owned(),
                    ..BackupMetadata::default()
                },
            )
            .expect("upload");
        assert_eq!(object.object_size, 7);
        assert_eq!(
            adapter.get(&object.object_ref, 1024).expect("download"),
            b"capsule"
        );
        adapter.remove(&object.object_ref).expect("delete");
        assert_eq!(
            observed_receiver.recv().expect("post observation").0,
            "POST /v1/agent-memory/files"
        );
        assert_eq!(
            observed_receiver.recv().expect("get observation").0,
            "GET /v1/agent-memory/files/backup-1"
        );
        assert_eq!(
            observed_receiver.recv().expect("delete observation").0,
            "DELETE /v1/agent-memory/files/backup-1"
        );
        server.join().expect("join mock server");
    }

    #[test]
    fn http_adapter_rejects_non_loopback_http_and_redacts_key() {
        assert!(HttpBackupStorage::new("http://memory.example.invalid", "secret").is_err());
        let adapter = HttpBackupStorage::new("https://memory.example.invalid", "secret")
            .expect("construct adapter");
        let debug = format!("{adapter:?}");
        assert!(!debug.contains("secret"));
        assert!(debug.contains("[redacted]"));
    }
}

#[cfg(test)]
mod important_scope_tests {
    use super::*;
    use serde_json::json;

    fn set<'a>(names: &'a [&'a str]) -> BTreeSet<&'a str> {
        names.iter().copied().collect()
    }

    #[test]
    fn important_scope_carries_marked_records_of_any_kind() {
        // The mark is orthogonal to the content category, so a marked `fact`
        // travels under the important scope. No content scope matches a `fact`,
        // which is exactly why the mark cannot ride along with `preferences`.
        let marked_fact = json!({"kind":"fact","text":"k3 power-loss probe","tags":["important"]});
        assert!(memory_matches_scopes(&marked_fact, &set(&["important"])));
        assert!(!memory_matches_scopes(&marked_fact, &set(&["preferences"])));

        let marked_preference = json!({
            "kind":"preference",
            "text":"terminal color: dark",
            "tags":["slot:appearance.terminal_color_scheme","important"]
        });
        assert!(memory_matches_scopes(
            &marked_preference,
            &set(&["important"])
        ));
        assert!(memory_matches_scopes(
            &marked_preference,
            &set(&["preferences"])
        ));

        let unmarked = json!({"kind":"fact","text":"unmarked","tags":[]});
        assert!(!memory_matches_scopes(&unmarked, &set(&["important"])));
    }

    #[test]
    fn important_tag_is_exact_and_never_inferred_from_text() {
        // A look-alike tag must not be promoted, and the mark is never derived
        // from the record text: only the user designating it creates the mark.
        let look_alike = json!({"kind":"fact","text":"x","tags":["important_note"]});
        assert!(!memory_matches_scopes(&look_alike, &set(&["important"])));

        let prose_only =
            json!({"kind":"fact","text":"this one is important, remember it","tags":[]});
        assert!(!memory_matches_scopes(&prose_only, &set(&["important"])));
    }
}
