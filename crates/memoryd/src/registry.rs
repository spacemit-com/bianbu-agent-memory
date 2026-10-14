use memory_core::{capability, MemoryIdentity};
use memory_protocol::ServiceError;
use serde::Deserialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::fs;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};

#[derive(Clone, Debug)]
pub struct PeerIdentity {
    pub uid: u32,
    pub gid: u32,
    pub pid: i32,
}

#[derive(Clone, Debug)]
pub struct IdentityResolver {
    registry_dir: Option<PathBuf>,
    development_identity: bool,
    development_device_id: String,
    development_account_id: String,
    development_agent_name: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RuntimeRegistration {
    uid: u32,
    #[serde(default)]
    gid: Option<u32>,
    pid_start_time_ticks: u64,
    account_id: String,
    device_id: String,
    agent_id: String,
    #[serde(default)]
    agent_name: Option<String>,
    agent_instance_id: String,
    memory_space_id: String,
    capabilities: BTreeSet<String>,
    #[serde(default)]
    executable_path: Option<PathBuf>,
    #[serde(default)]
    expires_at_ms: Option<i64>,
    #[serde(default)]
    allow_runtime_context: bool,
}

#[derive(Clone, Debug)]
pub struct ResolvedIdentity {
    pub identity: MemoryIdentity,
    pub allow_runtime_context: bool,
}

impl IdentityResolver {
    pub fn new(
        registry_dir: Option<PathBuf>,
        development_identity: bool,
        development_device_id: String,
    ) -> Self {
        Self {
            registry_dir,
            development_identity,
            development_device_id,
            development_account_id: "default".to_owned(),
            development_agent_name: "default-agent".to_owned(),
        }
    }

    pub fn with_development_context(mut self, account_id: String, agent_name: String) -> Self {
        self.development_account_id = account_id;
        self.development_agent_name = agent_name;
        self
    }

    pub fn resolve(&self, peer: &PeerIdentity) -> Result<ResolvedIdentity, ServiceError> {
        let start_time = process_start_time(peer.pid)?;
        if let Some(directory) = self.registry_dir.as_deref() {
            return resolve_registered(directory, peer, start_time);
        }
        if !self.development_identity {
            return Err(ServiceError::new(
                "Unauthenticated",
                "runtime identity registry is not configured",
            ));
        }
        let capabilities = capability::ALL
            .iter()
            .map(|value| (*value).to_owned())
            .collect();
        Ok(ResolvedIdentity {
            identity: MemoryIdentity {
                account_id: self.development_account_id.clone(),
                device_id: self.development_device_id.clone(),
                agent_id: format!("dev.uid.{}", peer.uid),
                agent_name: self.development_agent_name.clone(),
                agent_instance_id: format!("dev.pid.{}.{}", peer.pid, start_time),
                memory_space_id: format!("dev.uid.{}.{}", peer.uid, self.development_account_id),
                capabilities,
                peer_uid: peer.uid,
                peer_gid: peer.gid,
                peer_pid: peer.pid,
                peer_start_time_ticks: start_time,
            },
            allow_runtime_context: false,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn development_identity_uses_the_non_uploadable_default_account() {
        let resolver = IdentityResolver::new(None, true, "test-device".to_owned());
        let peer = PeerIdentity {
            uid: nix::unistd::geteuid().as_raw(),
            gid: nix::unistd::getegid().as_raw(),
            pid: std::process::id() as i32,
        };
        let identity = resolver.resolve(&peer).unwrap();
        assert_eq!(identity.identity.account_id, "default");
        assert_eq!(identity.identity.device_id, "test-device");
        assert_eq!(identity.identity.agent_name, "default-agent");
        assert!(identity.identity.memory_space_id.ends_with(".default"));
    }

    #[test]
    fn mac_addresses_are_normalized_and_invalid_values_rejected() {
        assert_eq!(
            normalize_mac("AA:bb:01:02:03:ff"),
            Some("aa:bb:01:02:03:ff".to_owned())
        );
        assert_eq!(normalize_mac("00:00:00:00:00:00"), None);
        assert_eq!(normalize_mac("01:00:00:00:00:00"), None);
        assert_eq!(normalize_mac("not-a-mac"), None);
    }

    #[test]
    fn registered_runtime_context_is_scoped_when_allowed() {
        let base = MemoryIdentity {
            account_id: "account-a".to_owned(),
            device_id: "device-a".to_owned(),
            agent_id: "agent-a".to_owned(),
            agent_name: "agent-default".to_owned(),
            agent_instance_id: "runtime-1".to_owned(),
            memory_space_id: "account-a.agent-a.device-a".to_owned(),
            capabilities: capability::ALL.iter().map(|v| (*v).to_owned()).collect(),
            peer_uid: 1,
            peer_gid: 1,
            peer_pid: 1,
            peer_start_time_ticks: 1,
        };
        let resolved = ResolvedIdentity {
            identity: base.clone(),
            allow_runtime_context: true,
        };
        let scoped = bind_runtime_context(
            &resolved,
            &serde_json::json!({"account_id":"account-b","agent_name":"agent-child"}),
        )
        .unwrap();
        assert_eq!(scoped.account_id, "account-b");
        assert_eq!(scoped.agent_id, "agent-child");
        assert_eq!(scoped.agent_name, "agent-child");
        assert_ne!(scoped.memory_space_id, base.memory_space_id);
        assert!(scoped.memory_space_id.starts_with("runtime-context."));
    }

    #[test]
    fn runtime_context_is_rejected_when_not_allowed() {
        let resolved = ResolvedIdentity {
            identity: MemoryIdentity {
                account_id: "account-a".to_owned(),
                device_id: "device-a".to_owned(),
                agent_id: "agent-a".to_owned(),
                agent_name: "agent-default".to_owned(),
                agent_instance_id: "runtime-1".to_owned(),
                memory_space_id: "space-a".to_owned(),
                capabilities: capability::ALL.iter().map(|v| (*v).to_owned()).collect(),
                peer_uid: 1,
                peer_gid: 1,
                peer_pid: 1,
                peer_start_time_ticks: 1,
            },
            allow_runtime_context: false,
        };
        let error = bind_runtime_context(&resolved, &serde_json::json!({"account_id":"account-b"}))
            .unwrap_err();
        assert_eq!(error.code, "Unauthenticated");
    }
}

fn resolve_registered(
    directory: &Path,
    peer: &PeerIdentity,
    actual_start_time: u64,
) -> Result<ResolvedIdentity, ServiceError> {
    // A bwrap Worker has a private PID namespace: the registration file is
    // named with its namespace-local PID while SO_PEERCRED exposes the host
    // PID here. Prefer the direct filename, then locate the unique entry
    // whose trusted UID/GID/start-time tuple matches this peer.
    let direct_path = directory.join(format!("{}.json", peer.pid));
    let (_path, metadata, encoded) = match fs::symlink_metadata(&direct_path) {
        Ok(metadata) => {
            let encoded = fs::read(&direct_path).map_err(|_| {
                ServiceError::new("Unauthenticated", "runtime registration is unreadable")
            })?;
            (direct_path, metadata, encoded)
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            let mut matched: Option<(PathBuf, std::fs::Metadata, Vec<u8>)> = None;
            let entries = fs::read_dir(directory).map_err(|_| {
                ServiceError::new("Unauthenticated", "runtime registration was not found")
            })?;
            for entry in entries.flatten() {
                let candidate = entry.path();
                if candidate.extension().and_then(|value| value.to_str()) != Some("json") {
                    continue;
                }
                let candidate_metadata = match fs::symlink_metadata(&candidate) {
                    Ok(value) => value,
                    Err(_) => continue,
                };
                if !candidate_metadata.file_type().is_file()
                    || candidate_metadata.len() > 64 * 1024
                    || candidate_metadata.uid() != peer.uid
                    || candidate_metadata.mode() & 0o027 != 0
                {
                    continue;
                }
                let candidate_encoded = match fs::read(&candidate) {
                    Ok(value) => value,
                    Err(_) => continue,
                };
                let registration: RuntimeRegistration =
                    match serde_json::from_slice(&candidate_encoded) {
                        Ok(value) => value,
                        Err(_) => continue,
                    };
                if registration.uid == peer.uid
                    && registration.gid.is_none_or(|gid| gid == peer.gid)
                    && registration.pid_start_time_ticks == actual_start_time
                    && !registration
                        .expires_at_ms
                        .is_some_and(|value| value <= memory_core::now_ms())
                {
                    if matched.is_some() {
                        return Err(ServiceError::new(
                            "Unauthenticated",
                            "runtime registration is ambiguous",
                        ));
                    }
                    matched = Some((candidate, candidate_metadata, candidate_encoded));
                }
            }
            matched.ok_or_else(|| {
                ServiceError::new("Unauthenticated", "runtime registration was not found")
            })?
        }
        Err(_) => {
            return Err(ServiceError::new(
                "Unauthenticated",
                "runtime registration was not found",
            ));
        }
    };
    if !metadata.file_type().is_file() || metadata.len() > 64 * 1024 {
        return Err(ServiceError::new(
            "Unauthenticated",
            "runtime registration is invalid",
        ));
    }
    let directory_metadata = fs::symlink_metadata(directory)
        .map_err(|_| ServiceError::new("Unauthenticated", "runtime registry is unavailable"))?;
    // The directory is owned by the trusted Runtime group.  On a stock
    // install it is root-owned, while Securityd may take ownership of the
    // directory to apply its ACL policy (for example `bianbu-security`).
    // Requiring root ownership here would reject the latter valid layout and
    // make every sandbox Worker fail identity authentication.  The group is
    // the memoryd process' supplementary/primary service group; other-writable
    // directories remain forbidden.
    let trusted_group = nix::unistd::getegid().as_raw();
    if directory_metadata.gid() != trusted_group || directory_metadata.mode() & 0o002 != 0 {
        return Err(ServiceError::new(
            "Unauthenticated",
            "runtime registry permissions are not trusted",
        ));
    }
    if metadata.uid() != peer.uid || metadata.mode() & 0o027 != 0 {
        return Err(ServiceError::new(
            "Unauthenticated",
            "runtime registration owner is not trusted",
        ));
    }
    let registration: RuntimeRegistration = serde_json::from_slice(&encoded)
        .map_err(|_| ServiceError::new("Unauthenticated", "runtime registration is malformed"))?;
    if registration.uid != peer.uid
        || registration.gid.is_some_and(|gid| gid != peer.gid)
        || registration.pid_start_time_ticks != actual_start_time
        || registration
            .expires_at_ms
            .is_some_and(|value| value <= memory_core::now_ms())
    {
        return Err(ServiceError::new(
            "Unauthenticated",
            "runtime registration does not match the peer process",
        ));
    }
    if let Some(expected) = registration.executable_path.as_deref() {
        match fs::read_link(format!("/proc/{}/exe", peer.pid)) {
            Ok(actual) if actual != expected => {
                return Err(ServiceError::new(
                    "Unauthenticated",
                    "peer executable does not match the runtime registration",
                ));
            }
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::PermissionDenied => {
                // Some K3 images mount /proc with hidepid restrictions.  The
                // registry is already bound to peer UID, PID start time and
                // a root-owned non-world-writable directory, so inability to
                // inspect /proc/<pid>/exe is not treated as an identity error.
            }
            Err(_) => {
                return Err(ServiceError::new(
                    "Unauthenticated",
                    "peer executable is unavailable",
                ));
            }
        }
    }
    if registration
        .capabilities
        .iter()
        .any(|value| !capability::ALL.contains(&value.as_str()))
    {
        return Err(ServiceError::new(
            "Unauthenticated",
            "runtime registration contains an unknown capability",
        ));
    }
    let agent_name = registration
        .agent_name
        .clone()
        .unwrap_or_else(|| registration.agent_id.clone());
    for identifier in [
        &registration.account_id,
        &registration.device_id,
        &registration.agent_id,
        &agent_name,
        &registration.agent_instance_id,
        &registration.memory_space_id,
    ] {
        if !memory_core::valid_identifier(identifier) {
            return Err(ServiceError::new(
                "Unauthenticated",
                "runtime registration contains an invalid identity",
            ));
        }
    }
    Ok(ResolvedIdentity {
        identity: MemoryIdentity {
            account_id: registration.account_id,
            device_id: registration.device_id,
            agent_id: registration.agent_id,
            agent_name,
            agent_instance_id: registration.agent_instance_id,
            memory_space_id: registration.memory_space_id,
            capabilities: registration.capabilities,
            peer_uid: peer.uid,
            peer_gid: peer.gid,
            peer_pid: peer.pid,
            peer_start_time_ticks: actual_start_time,
        },
        allow_runtime_context: registration.allow_runtime_context,
    })
}

/// Bind a registered Runtime connection to the account and Agent selected for
/// this SDK connection. The peer process remains the authorization boundary.
pub fn bind_runtime_context(
    resolved: &ResolvedIdentity,
    params: &Value,
) -> Result<MemoryIdentity, ServiceError> {
    let base = &resolved.identity;
    let account_id = params
        .get("account_id")
        .and_then(Value::as_str)
        .unwrap_or(base.account_id.as_str());
    let agent_name = params
        .get("agent_name")
        .and_then(Value::as_str)
        .unwrap_or(base.agent_name.as_str());
    for (field, value) in [("account_id", account_id), ("agent_name", agent_name)] {
        if !memory_core::valid_identifier(value) {
            return Err(ServiceError::new(
                "Unauthenticated",
                format!("runtime {field} is invalid"),
            ));
        }
    }
    if account_id == base.account_id && agent_name == base.agent_name {
        return Ok(base.clone());
    }
    if !resolved.allow_runtime_context {
        return Err(ServiceError::new(
            "Unauthenticated",
            "runtime account_id or agent_name does not match the trusted identity",
        ));
    }
    let mut hasher = Sha256::new();
    for value in [
        base.agent_id.as_str(),
        base.device_id.as_str(),
        account_id,
        agent_name,
    ] {
        hasher.update((value.len() as u64).to_be_bytes());
        hasher.update(value.as_bytes());
    }
    let mut scoped = base.clone();
    scoped.account_id = account_id.to_owned();
    // The Runtime-facing `agent_name` is the current Agent package id.  The
    // registry's base `agent_id` identifies the signed Runtime application,
    // so retaining it here would collapse every child Agent into one SQL
    // namespace.  Keep the application id in the hash input, but expose and
    // persist the selected Agent id on the scoped connection.
    scoped.agent_id = agent_name.to_owned();
    scoped.agent_name = agent_name.to_owned();
    scoped.memory_space_id = format!("runtime-context.{}", hex::encode(hasher.finalize()));
    Ok(scoped)
}

/// Resolve a stable physical device identifier for development deployments.
/// Runtime registrations should pass the same normalized MAC explicitly.
pub fn detect_device_mac() -> Option<String> {
    let mut candidates = fs::read_dir("/sys/class/net")
        .ok()?
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let name = entry.file_name().to_string_lossy().into_owned();
            if name == "lo"
                || ["docker", "veth", "br-", "virbr", "tun", "tap", "tailscale"]
                    .iter()
                    .any(|prefix| name.starts_with(prefix))
            {
                return None;
            }
            let address = fs::read_to_string(entry.path().join("address")).ok()?;
            Some((name, normalize_mac(address.trim())?))
        })
        .collect::<Vec<_>>();
    candidates.sort_by(|left, right| left.0.cmp(&right.0));
    candidates.into_iter().next().map(|(_, mac)| mac)
}

fn normalize_mac(value: &str) -> Option<String> {
    let octets = value.split(':').collect::<Vec<_>>();
    if octets.len() != 6
        || octets
            .iter()
            .any(|octet| octet.len() != 2 || u8::from_str_radix(octet, 16).is_err())
    {
        return None;
    }
    let bytes = octets
        .iter()
        .map(|octet| u8::from_str_radix(octet, 16).ok())
        .collect::<Option<Vec<_>>>()?;
    if bytes.iter().all(|byte| *byte == 0) || bytes[0] & 1 != 0 {
        return None;
    }
    Some(
        bytes
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<Vec<_>>()
            .join(":"),
    )
}

pub(crate) fn process_start_time(pid: i32) -> Result<u64, ServiceError> {
    let stat = fs::read_to_string(format!("/proc/{pid}/stat")).map_err(|_| {
        ServiceError::new("Unauthenticated", "peer process metadata is unavailable")
    })?;
    let after_name = stat
        .rfind(')')
        .and_then(|index| stat.get(index + 2..))
        .ok_or_else(|| {
            ServiceError::new("Unauthenticated", "peer process metadata is malformed")
        })?;
    after_name
        .split_whitespace()
        .nth(19)
        .and_then(|value| value.parse().ok())
        .ok_or_else(|| ServiceError::new("Unauthenticated", "peer start time is unavailable"))
}
