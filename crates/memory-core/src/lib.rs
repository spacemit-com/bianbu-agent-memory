use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use std::time::{SystemTime, UNIX_EPOCH};

pub const PROTOCOL_VERSION: &str = "agent-memory.v1";
// Newline-framed request/response ceiling for the UDS protocol. Bulk
// operations (privacy.export_data) against real namespaces exceed the old
// 256 KiB budget — full exports observed at ~140 KiB and growing with
// session count, which made them fail with ResourceExhausted. 4 MiB matches
// the runtime worker RPC message cap and still bounds a local socket peer.
// The daemon advertises the effective value in the system.hello response so
// clients adopt whatever limit their daemon enforces.
pub const MAX_FRAME_BYTES: usize = 4 * 1024 * 1024;
pub const MAX_TEXT_BYTES: usize = 128 * 1024;
pub const MAX_ID_BYTES: usize = 256;

pub mod capability {
    pub const INSTRUCTION_READ: &str = "memory.instruction.read";
    pub const INSTRUCTION_WRITE: &str = "memory.instruction.write";
    pub const SHORT_TERM_READ: &str = "memory.short_term.read";
    pub const SHORT_TERM_WRITE: &str = "memory.short_term.write";
    pub const LONG_TERM_READ: &str = "memory.long_term.read";
    pub const LONG_TERM_WRITE: &str = "memory.long_term.write";
    pub const FORGET: &str = "memory.forget";
    pub const ACCOUNT_READ: &str = "memory.account.read";
    pub const SYNC: &str = "memory.sync";
    pub const BACKUP: &str = "memory.backup";
    pub const RESTORE: &str = "memory.restore";
    pub const AUDIT_STATUS: &str = "memory.audit.status";
    pub const AUDIT_READ: &str = "memory.audit.read";
    pub const AUDIT_WRITE: &str = "memory.audit.write";
    pub const PRIVACY_READ: &str = "memory.privacy.read";
    pub const PRIVACY_WRITE: &str = "memory.privacy.write";

    pub const ALL: &[&str] = &[
        INSTRUCTION_READ,
        INSTRUCTION_WRITE,
        SHORT_TERM_READ,
        SHORT_TERM_WRITE,
        LONG_TERM_READ,
        LONG_TERM_WRITE,
        FORGET,
        ACCOUNT_READ,
        SYNC,
        BACKUP,
        RESTORE,
        AUDIT_STATUS,
        AUDIT_READ,
        AUDIT_WRITE,
        PRIVACY_READ,
        PRIVACY_WRITE,
    ];
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct MemoryIdentity {
    pub account_id: String,
    pub device_id: String,
    pub agent_id: String,
    /// Runtime-provided stable Agent name used in backup metadata.
    /// `agent_id` remains the canonical namespace key for compatibility.
    #[serde(default)]
    pub agent_name: String,
    pub agent_instance_id: String,
    pub memory_space_id: String,
    #[serde(default)]
    pub capabilities: BTreeSet<String>,
    pub peer_uid: u32,
    pub peer_gid: u32,
    pub peer_pid: i32,
    pub peer_start_time_ticks: u64,
}

impl MemoryIdentity {
    pub fn has(&self, capability: &str) -> bool {
        self.capabilities.contains(capability)
    }
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct Hlc {
    pub physical_ms: i64,
    pub logical: u32,
    pub device_id: String,
}

impl Hlc {
    pub fn tick(previous: Option<&Hlc>, now_ms: i64, device_id: &str) -> Self {
        match previous {
            Some(old) if now_ms <= old.physical_ms => Self {
                physical_ms: old.physical_ms,
                logical: old.logical.saturating_add(1),
                device_id: device_id.to_owned(),
            },
            _ => Self {
                physical_ms: now_ms,
                logical: 0,
                device_id: device_id.to_owned(),
            },
        }
    }

    pub fn encode(&self) -> String {
        format!(
            "{:020}:{:010}:{}",
            self.physical_ms.max(0),
            self.logical,
            self.device_id
        )
    }

    pub fn decode(value: &str) -> Option<Self> {
        let mut parts = value.splitn(3, ':');
        Some(Self {
            physical_ms: parts.next()?.parse().ok()?,
            logical: parts.next()?.parse().ok()?,
            device_id: parts.next()?.to_owned(),
        })
    }
}

pub fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .min(i64::MAX as u128) as i64
}

pub fn allowed_sync_policy(value: &str) -> bool {
    matches!(
        value,
        "local_only" | "summary_only" | "account_agent" | "full"
    )
}

pub fn allowed_instruction_scope(value: &str) -> bool {
    matches!(
        value,
        "managed" | "account" | "agent" | "workspace" | "device"
    )
}

pub fn allowed_event_type(value: &str) -> bool {
    matches!(
        value,
        "user_message"
            | "assistant_message"
            | "tool_call"
            | "tool_result"
            | "task_started"
            | "task_completed"
            | "task_failed"
            | "checkpoint_marker"
            | "summary_updated"
            | "memory_delete_proposed"
    )
}

pub fn allowed_memory_kind(value: &str) -> bool {
    matches!(
        value,
        "profile" | "preference" | "fact" | "project" | "episodic" | "decision" | "reference"
    )
}

pub fn valid_identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= MAX_ID_BYTES
        && value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b':' | b'/')
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hlc_survives_clock_rollback() {
        let first = Hlc::tick(None, 100, "k3");
        let second = Hlc::tick(Some(&first), 90, "k3");
        assert_eq!(second.physical_ms, 100);
        assert_eq!(second.logical, 1);
        assert!(second.encode() > first.encode());
    }

    #[test]
    fn mem0_delete_proposal_is_a_valid_session_event() {
        assert!(allowed_event_type("memory_delete_proposed"));
    }
}
