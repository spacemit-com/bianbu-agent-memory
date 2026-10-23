mod dbus;
mod key_package;
mod registry;

use clap::Parser;
use memory_core::{now_ms, MAX_FRAME_BYTES, PROTOCOL_VERSION};
use memory_protocol::{RequestEnvelope, ResponseEnvelope, ServiceError};
use memory_store::{
    cleanup_migration_temporary_files, cleanup_plaintext_backups, migrate_plaintext_database,
    report_namespaces, Store,
};
use nix::sys::socket::{getsockopt, sockopt::PeerCredentials};
use registry::{bind_runtime_context, detect_device_mac, IdentityResolver, PeerIdentity};
use serde_json::{json, Value};
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::fs::{FileTypeExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::thread;
use zeroize::Zeroizing;
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::sync::{Mutex, OnceLock};

/// Reported by `--version`.  Stamped from `debian/changelog` by `build.rs` so
/// the daemon cannot drift from the released package version.
const VERSION: &str = match option_env!("BIANBU_AGENT_MEMORY_VERSION") {
    Some(version) => version,
    None => env!("CARGO_PKG_VERSION"),
};

#[derive(Debug, Parser)]
#[command(
    name = "bianbu-agent-memoryd",
    version = VERSION,
    about = "Bianbu local authoritative Agent Memory service"
)]
struct Arguments {
    #[arg(long, default_value = "/run/bianbu/agent-memoryd.sock")]
    socket: PathBuf,
    #[arg(long, default_value = "/var/lib/bianbu-agent-memory/memory.db")]
    database: PathBuf,
    #[arg(long)]
    runtime_registry_dir: Option<PathBuf>,
    #[arg(long, default_value_t = false)]
    development_identity: bool,
    #[arg(long)]
    development_device_id: Option<String>,
    #[arg(long, default_value = "default")]
    development_account_id: String,
    #[arg(long, default_value = "default-agent")]
    development_agent_name: String,
    #[arg(long)]
    key_package: Option<PathBuf>,
    #[arg(long)]
    account_secret_file: Option<PathBuf>,
    #[arg(long, default_value_t = false)]
    initialize_key_package: bool,
    /// Initialize the encrypted database key package and exit.
    /// This is used by Debian post-installation; it never starts the UDS listener.
    #[arg(long, default_value_t = false)]
    initialize_only: bool,
    #[arg(long, default_value_t = false)]
    migrate_plaintext: bool,
    /// Remove all narrowly-scoped `memory.db.plaintext-backup-*` files after
    /// a successful migration. This is intentionally opt-in for rollback safety.
    #[arg(long, default_value_t = false)]
    discard_plaintext_backups: bool,
    /// Remove narrowly-scoped plaintext migration backups and exit.
    #[arg(long, default_value_t = false)]
    cleanup_plaintext_backups: bool,
    /// Report every memory namespace in the database and exit.
    ///
    /// Read-only stdout report for operators: per-namespace row counts plus the
    /// `slot:` tags in use. Namespaces no Runtime registers any more (retired
    /// test spaces, probe spaces) keep their rows forever and are invisible to
    /// every identity-scoped protocol method, so this is the only way to audit
    /// what a device is actually holding.
    #[arg(long, default_value_t = false)]
    report_namespaces: bool,
    /// Remove only interrupted encrypted migration staging files for the
    /// configured database. Plaintext rollback backups are never removed by
    /// this option.
    #[arg(long, default_value_t = false)]
    cleanup_migration_temporaries: bool,
    #[arg(long)]
    rotate_account_secret_file: Option<PathBuf>,
    #[arg(long)]
    export_key_package: Option<PathBuf>,
    #[arg(long, default_value_t = false)]
    allow_plaintext: bool,
    #[arg(long, default_value_t = false)]
    dbus: bool,
    /// Shared secret used by the trusted local Memory UI adapter to create
    /// short-lived, one-time confirmation tokens for destructive operations.
    /// The secret is read once at startup and is never exposed through UDS or
    /// the HTTP adapter.
    #[arg(long)]
    adapter_confirmation_secret_file: Option<PathBuf>,
    #[arg(long, default_value_t = false)]
    dbus_session_bus: bool,
    #[arg(long, default_value_t = 60)]
    background_interval_seconds: u64,
    /// Trusted device identifier used only for daemon-owned lifecycle metadata.
    /// In production it must be supplied by packaging; otherwise the worker is disabled.
    #[arg(long)]
    background_device_id: Option<String>,
}

fn main() {
    if let Err(error) = run(Arguments::parse()) {
        eprintln!("bianbu-agent-memoryd: {error}");
        std::process::exit(1);
    }
}

fn run(arguments: Arguments) -> Result<(), Box<dyn std::error::Error>> {
    if arguments.cleanup_plaintext_backups {
        let removed = cleanup_plaintext_backups(&arguments.database)?;
        eprintln!("bianbu-agent-memoryd: removed {removed} plaintext migration backup(s)");
        return Ok(());
    }
    if arguments.cleanup_migration_temporaries {
        let removed = cleanup_migration_temporary_files(&arguments.database)?;
        eprintln!("bianbu-agent-memoryd: removed {removed} interrupted migration temporary file(s)");
        return Ok(());
    }
    if arguments.development_identity {
        eprintln!("bianbu-agent-memoryd: DEVELOPMENT identity mode is enabled");
    } else if arguments.runtime_registry_dir.is_none()
        && !arguments.initialize_only
        && !arguments.report_namespaces
    {
        // The registry is what a serving daemon authenticates callers with;
        // the one-shot key-package and inventory actions never serve anything.
        return Err("--runtime-registry-dir is required outside development mode".into());
    }
    let database_key = prepare_database_key(&arguments)?;
    if arguments.initialize_only {
        if !arguments.initialize_key_package {
            return Err("--initialize-only requires --initialize-key-package".into());
        }
        if database_key.is_none() {
            return Err("--initialize-only requires an encrypted key package".into());
        }
        eprintln!("bianbu-agent-memoryd: encrypted key package initialized");
        return Ok(());
    }
    if arguments.rotate_account_secret_file.is_some() {
        eprintln!("bianbu-agent-memoryd: account-secret key package rotation completed");
        return Ok(());
    }
    if let Some(destination) = arguments.export_key_package.as_deref() {
        key_package::export(
            arguments
                .key_package
                .as_deref()
                .ok_or("--key-package is required for export")?,
            destination,
        )?;
        return Ok(());
    }
    if arguments.report_namespaces {
        let report = report_namespaces(
            &arguments.database,
            database_key.as_deref().map(Vec::as_slice),
        )?;
        println!("{}", serde_json::to_string_pretty(&report)?);
        return Ok(());
    }
    prepare_socket(&arguments.socket)?;
    let adapter_confirmation_secret = read_adapter_confirmation_secret(&arguments)?;
    let store = match database_key.as_deref() {
        Some(key) => Store::open_encrypted(&arguments.database, key)?,
        None => Store::open(&arguments.database)?,
    };
    if arguments.development_identity {
        let migration =
            store.migrate_development_account_to_default(nix::unistd::geteuid().as_raw())?;
        if migration.get("state").and_then(Value::as_str) == Some("migrated") {
            eprintln!("bianbu-agent-memoryd: development account migration {migration}");
        }
    }
    let development_device_id = arguments
        .development_device_id
        .clone()
        .or_else(detect_device_mac)
        .unwrap_or(store.configured_local_device_id()?);
    let store = Arc::new(store);
    let background_device_id = arguments.background_device_id.clone().or_else(|| {
        arguments
            .development_identity
            .then(|| development_device_id.clone())
    });
    let resolver = Arc::new(
        IdentityResolver::new(
            arguments.runtime_registry_dir,
            arguments.development_identity,
            development_device_id,
        )
        .with_development_context(
            arguments.development_account_id.clone(),
            arguments.development_agent_name.clone(),
        ),
    );
    if arguments.background_interval_seconds < 10 {
        return Err("--background-interval-seconds must be at least 10".into());
    }
    if let Some(background_device_id) = background_device_id {
        let background_store = Arc::clone(&store);
        let background_interval =
            std::time::Duration::from_secs(arguments.background_interval_seconds);
        thread::spawn(move || loop {
            match background_store.run_background_jobs(&background_device_id) {
                Ok(result) => {
                    if result.get("attempted").and_then(Value::as_i64).unwrap_or(0) > 0 {
                        eprintln!("bianbu-agent-memoryd: background jobs {result}");
                    }
                }
                Err(error) => eprintln!("bianbu-agent-memoryd: background worker failed: {error}"),
            }
            thread::sleep(background_interval);
        });
    } else {
        eprintln!(
            "bianbu-agent-memoryd: background worker disabled; provide --background-device-id"
        );
    }
    // D-Bus is a compatibility control plane for trusted system components, so
    // it must never be able to take the UDS service down: firmware images and
    // chroot builds have no system bus at all, and a bus that is not up yet is
    // transient during early boot.  Report the failure and keep serving UDS.
    if arguments.dbus {
        if let Err(error) = dbus::start(
            Arc::clone(&store),
            Arc::clone(&resolver),
            arguments.development_identity,
            arguments.dbus_session_bus,
        ) {
            eprintln!("bianbu-agent-memoryd: DBus unavailable, continuing without it: {error}");
        }
    }
    let listener = UnixListener::bind(&arguments.socket)?;
    fs::set_permissions(&arguments.socket, fs::Permissions::from_mode(0o660))?;
    eprintln!(
        "bianbu-agent-memoryd: listening on {} (protocol {})",
        arguments.socket.display(),
        PROTOCOL_VERSION
    );
    for incoming in listener.incoming() {
        match incoming {
            Ok(stream) => {
                let store = Arc::clone(&store);
                let resolver = Arc::clone(&resolver);
                let development_identity = arguments.development_identity;
                let adapter_confirmation_secret = adapter_confirmation_secret.clone();
                thread::spawn(move || {
                    if let Err(error) = handle_connection(
                        stream,
                        &store,
                        &resolver,
                        development_identity,
                        adapter_confirmation_secret.as_deref(),
                    )
                    {
                        eprintln!("bianbu-agent-memoryd: connection ended: {error}");
                    }
                });
            }
            Err(error) => eprintln!("bianbu-agent-memoryd: accept failed: {error}"),
        }
    }
    Ok(())
}

fn read_adapter_confirmation_secret(
    arguments: &Arguments,
) -> Result<Option<Vec<u8>>, Box<dyn std::error::Error>> {
    let Some(path) = arguments.adapter_confirmation_secret_file.as_deref() else {
        return Ok(None);
    };
    let metadata = fs::metadata(path)?;
    if !metadata.is_file() || metadata.len() == 0 || metadata.len() > 4096 {
        return Err(format!(
            "adapter confirmation secret must be a non-empty regular file <= 4096 bytes: {}",
            path.display()
        )
        .into());
    }
    let bytes = fs::read(path)?;
    if bytes.iter().all(|byte| byte.is_ascii_whitespace()) {
        return Err("adapter confirmation secret is empty".into());
    }
    Ok(Some(bytes))
}

fn prepare_database_key(
    arguments: &Arguments,
) -> Result<Option<Zeroizing<Vec<u8>>>, Box<dyn std::error::Error>> {
    let Some(package_path) = arguments.key_package.as_deref() else {
        if arguments.allow_plaintext {
            eprintln!("bianbu-agent-memoryd: WARNING plaintext database mode is enabled");
            return Ok(None);
        }
        return Err("--key-package and --account-secret-file are required; use --allow-plaintext only for explicit migration recovery".into());
    };
    let secret_path = arguments
        .account_secret_file
        .as_deref()
        .ok_or("--account-secret-file is required with --key-package")?;
    let account_secret = key_package::read_secret_file(secret_path)?;
    if let Some(new_secret_path) = arguments.rotate_account_secret_file.as_deref() {
        let new_secret = key_package::read_secret_file(new_secret_path)?;
        key_package::rotate(package_path, &account_secret, &new_secret)?;
        return Ok(None);
    }
    let database_key = if package_path.exists() {
        key_package::unlock(package_path, &account_secret)?
    } else if arguments.initialize_key_package {
        key_package::initialize(package_path, &account_secret)?
    } else {
        return Err(
            "key package does not exist; pass --initialize-key-package for first use".into(),
        );
    };
    if arguments.migrate_plaintext && arguments.database.exists() {
        let backup = migrate_plaintext_database(&arguments.database, &database_key)?;
        eprintln!(
            "bianbu-agent-memoryd: migrated plaintext database; recoverable backup is {}",
            backup.display()
        );
        if arguments.discard_plaintext_backups {
            let removed = cleanup_plaintext_backups(&arguments.database)?;
            eprintln!("bianbu-agent-memoryd: removed {removed} plaintext migration backup(s)");
        }
    }
    Ok(Some(database_key))
}

fn prepare_socket(path: &Path) -> Result<(), Box<dyn std::error::Error>> {
    let parent = path.parent().ok_or("socket path has no parent")?;
    fs::create_dir_all(parent)?;
    if path.exists() {
        let metadata = fs::symlink_metadata(path)?;
        if !metadata.file_type().is_socket() {
            return Err(format!("refusing to replace non-socket path {}", path.display()).into());
        }
        fs::remove_file(path)?;
    }
    Ok(())
}

fn handle_connection(
    mut stream: UnixStream,
    store: &Store,
    resolver: &IdentityResolver,
    development_identity: bool,
    adapter_confirmation_secret: Option<&[u8]>,
) -> Result<(), Box<dyn std::error::Error>> {
    let credentials = getsockopt(&stream, PeerCredentials)?;
    let peer = PeerIdentity {
        uid: credentials.uid(),
        gid: credentials.gid(),
        pid: credentials.pid(),
    };
    let resolved_identity = resolver.resolve(&peer);
    let mut connection_identity = None;
    let reader_stream = stream.try_clone()?;
    let mut reader = BufReader::new(reader_stream);
    let mut handshake_complete = false;

    loop {
        let Some(frame) = read_frame(&mut reader)? else {
            break;
        };
        let request: RequestEnvelope = match serde_json::from_slice(&frame) {
            Ok(value) => value,
            Err(_) => {
                write_unbound_error(&mut stream, "InvalidArgument", "request is not valid JSON")?;
                break;
            }
        };
        if request.protocol != PROTOCOL_VERSION {
            write_response(
                &mut stream,
                &ResponseEnvelope::error(
                    &request,
                    ServiceError::new("ProtocolMismatch", "unsupported protocol version"),
                ),
            )?;
            break;
        }
        if !handshake_complete {
            if request.method != "system.hello" {
                write_response(
                    &mut stream,
                    &ResponseEnvelope::error(
                        &request,
                        ServiceError::new(
                            "Unauthenticated",
                            "system.hello must be the first request",
                        ),
                    ),
                )?;
                break;
            }
            match resolved_identity.as_ref() {
                Ok(resolved) => {
                    let identity = match bind_runtime_context(resolved, &request.params) {
                        Ok(identity) => identity,
                        Err(error) => {
                            write_response(&mut stream, &ResponseEnvelope::error(&request, error))?;
                            break;
                        }
                    };
                    let result = json!({
                        "identity":&identity,
                        "protocol":PROTOCOL_VERSION,
                        "max_frame_bytes":MAX_FRAME_BYTES,
                        "identity_source":if development_identity {"development_peercred"} else {"runtime_registry"}
                    });
                    write_response(&mut stream, &ResponseEnvelope::ok(&request, result))?;
                    connection_identity = Some(identity);
                    handshake_complete = true;
                    continue;
                }
                Err(error) => {
                    write_response(
                        &mut stream,
                        &ResponseEnvelope::error(&request, error.clone()),
                    )?;
                    break;
                }
            }
        }
        if request.method == "system.hello" {
            write_response(
                &mut stream,
                &ResponseEnvelope::error(
                    &request,
                    ServiceError::invalid("system.hello may only be sent once"),
                ),
            )?;
            continue;
        }
        let identity = match connection_identity.as_ref() {
            Some(identity) => identity,
            None => break,
        };
        let confirm_verified = verify_confirmation(
            &request,
            development_identity,
            adapter_confirmation_secret,
        );
        let response = match store.handle(identity, &request, confirm_verified) {
            Ok(result) => ResponseEnvelope::ok(&request, result),
            Err(error) => ResponseEnvelope::error(&request, error),
        };
        write_response(&mut stream, &response)?;
    }
    Ok(())
}

fn read_frame(reader: &mut BufReader<UnixStream>) -> Result<Option<Vec<u8>>, std::io::Error> {
    let mut frame = Vec::new();
    let read = reader
        .by_ref()
        .take((MAX_FRAME_BYTES + 1) as u64)
        .read_until(b'\n', &mut frame)?;
    if read == 0 {
        return Ok(None);
    }
    if frame.len() > MAX_FRAME_BYTES || frame.last() != Some(&b'\n') {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "request frame exceeds the configured limit",
        ));
    }
    frame.pop();
    Ok(Some(frame))
}

fn write_response(
    stream: &mut UnixStream,
    response: &ResponseEnvelope,
) -> Result<(), std::io::Error> {
    let mut encoded = serde_json::to_vec(response).map_err(|_| {
        std::io::Error::new(std::io::ErrorKind::InvalidData, "response encoding failed")
    })?;
    if encoded.len() > MAX_FRAME_BYTES {
        encoded = serde_json::to_vec(&json!({
            "status":"error",
            "request_id":response.request_id,
            "trace_id":response.trace_id,
            "server_protocol_version":PROTOCOL_VERSION,
            "error":{
                "code":"ResourceExhausted",
                "message":"response exceeds the frame limit",
                "retryable":false
            }
        }))
        .expect("static response is serializable");
    }
    encoded.push(b'\n');
    stream.write_all(&encoded)
}

fn write_unbound_error(
    stream: &mut UnixStream,
    code: &str,
    message: &str,
) -> Result<(), std::io::Error> {
    let mut encoded = serde_json::to_vec(&json!({
        "status":"error",
        "request_id":"unknown",
        "trace_id":"unknown",
        "server_protocol_version":PROTOCOL_VERSION,
        "error":{"code":code,"message":message,"retryable":false}
    }))
    .expect("static response is serializable");
    encoded.push(b'\n');
    stream.write_all(&encoded)
}

fn verify_confirmation(
    request: &RequestEnvelope,
    development_identity: bool,
    adapter_confirmation_secret: Option<&[u8]>,
) -> bool {
    if request.method != "long_term.forget"
        && request.method != "privacy.clear_history"
        && request.method != "privacy.clear_all"
        && request.method != "privacy.export_data"
        && request.method != "privacy.import_data"
    {
        return false;
    }
    if development_identity {
        let expected = match std::env::var("BIANBU_MEMORY_DEV_CONFIRM_TOKEN") {
            Ok(value) if !value.is_empty() => value,
            _ => return false,
        };
        return request
            .params
            .get("confirm_token")
            .and_then(Value::as_str)
            .is_some_and(|provided| provided == expected);
    }
    let Some(secret) = adapter_confirmation_secret else {
        return false;
    };
    verify_adapter_confirmation(request, secret)
}

const CONFIRMATION_PREFIX: &str = "v1";
static USED_CONFIRMATIONS: OnceLock<Mutex<HashSet<String>>> = OnceLock::new();

/// Verify and consume a token minted by the trusted local adapter.  Tokens
/// bind the operation name and expire quickly; the in-memory nonce set makes a
/// token one-shot for the lifetime of this daemon.
fn verify_adapter_confirmation(request: &RequestEnvelope, secret: &[u8]) -> bool {
    let Some(token) = request.params.get("confirm_token").and_then(Value::as_str) else {
        return false;
    };
    let mut parts = token.splitn(4, ':');
    if parts.next() != Some(CONFIRMATION_PREFIX) {
        return false;
    }
    let Some(expiry) = parts.next().and_then(|value| value.parse::<i64>().ok()) else {
        return false;
    };
    let Some(nonce) = parts.next() else {
        return false;
    };
    let Some(signature) = parts.next() else {
        return false;
    };
    if nonce.is_empty() || nonce.len() > 128 || signature.len() != 64 || now_ms() > expiry {
        return false;
    }
    let payload = format!("{CONFIRMATION_PREFIX}:{expiry}:{nonce}:{}", request.method);
    let mut hasher = Sha256::new();
    hasher.update(secret);
    hasher.update(payload.as_bytes());
    let expected = hex::encode(hasher.finalize());
    if !constant_time_equal(expected.as_bytes(), signature.as_bytes()) {
        return false;
    }
    let used = USED_CONFIRMATIONS.get_or_init(|| Mutex::new(HashSet::new()));
    let Ok(mut used) = used.lock() else {
        return false;
    };
    if !used.insert(token.to_owned()) {
        return false;
    }
    if used.len() > 4096 {
        // Tokens include an expiry and are only a replay guard.  Bounding this
        // set avoids unbounded growth on a long-running device.
        used.clear();
    }
    true
}

fn constant_time_equal(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    left.iter()
        .zip(right)
        .fold(0_u8, |difference, (a, b)| difference | (a ^ b))
        == 0
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::BufReader;
    use std::os::unix::net::UnixStream;

    fn export_request() -> RequestEnvelope {
        serde_json::from_str(
            r#"{"protocol":"agent-memory.v1","request_id":"frame-test","trace_id":"frame-test","method":"privacy.export_data","params":{},"deadline_ms":1000}"#,
        )
        .expect("request envelope parses")
    }

    #[test]
    fn frames_carry_export_sized_responses() {
        // A 300 KiB payload was swapped for a ResourceExhausted error under
        // the old 256 KiB frame budget; full production exports of real
        // namespaces already reach ~140 KiB and grow with session count.
        // Writing happens on a thread: a socketpair buffer is smaller than
        // the frame, so a synchronous write would deadlock before the
        // reader ever runs.
        let (client, mut server) = UnixStream::pair().expect("socketpair");
        let response =
            ResponseEnvelope::ok(&export_request(), json!({"blob": "x".repeat(300 * 1024)}));
        let writer = std::thread::spawn(move || {
            write_response(&mut server, &response).expect("write");
        });

        let mut reader = BufReader::new(client);
        let frame = read_frame(&mut reader).expect("read").expect("frame");
        let parsed: serde_json::Value = serde_json::from_slice(&frame).expect("frame parses");
        assert_eq!(parsed["status"], "ok");
        assert_eq!(
            parsed["result"]["blob"].as_str().map(str::len),
            Some(300 * 1024)
        );
        writer.join().expect("writer");
    }

    #[test]
    fn oversized_responses_degrade_to_resource_exhausted() {
        let (client, mut server) = UnixStream::pair().expect("socketpair");
        let response = ResponseEnvelope::ok(
            &export_request(),
            json!({"blob": "x".repeat(MAX_FRAME_BYTES)}),
        );
        write_response(&mut server, &response).expect("write");

        let mut reader = BufReader::new(client);
        let frame = read_frame(&mut reader).expect("read").expect("frame");
        let parsed: serde_json::Value = serde_json::from_slice(&frame).expect("frame parses");
        assert_eq!(parsed["status"], "error");
        assert_eq!(parsed["error"]["code"], "ResourceExhausted");
        assert_eq!(
            parsed["error"]["message"], "response exceeds the frame limit"
        );
    }
}
