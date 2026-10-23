use memory_core::PROTOCOL_VERSION;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::fmt;

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct RequestEnvelope {
    #[serde(default = "default_protocol")]
    pub protocol: String,
    pub request_id: String,
    pub trace_id: String,
    pub method: String,
    #[serde(default)]
    pub params: Value,
    #[serde(default)]
    pub idempotency_key: Option<String>,
    #[serde(default)]
    pub deadline_ms: Option<u64>,
}

fn default_protocol() -> String {
    PROTOCOL_VERSION.to_owned()
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ResponseEnvelope {
    pub status: &'static str,
    pub request_id: String,
    pub trace_id: String,
    pub server_protocol_version: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<ErrorBody>,
}

impl ResponseEnvelope {
    pub fn ok(request: &RequestEnvelope, result: Value) -> Self {
        Self {
            status: "ok",
            request_id: request.request_id.clone(),
            trace_id: request.trace_id.clone(),
            server_protocol_version: PROTOCOL_VERSION,
            result: Some(result),
            error: None,
        }
    }

    pub fn error(request: &RequestEnvelope, error: ServiceError) -> Self {
        Self {
            status: "error",
            request_id: request.request_id.clone(),
            trace_id: request.trace_id.clone(),
            server_protocol_version: PROTOCOL_VERSION,
            result: None,
            error: Some(error.into_body()),
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ErrorBody {
    pub code: String,
    pub message: String,
    pub retryable: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub retry_after_ms: Option<u64>,
}

#[derive(Clone, Debug)]
pub struct ServiceError {
    pub code: &'static str,
    pub message: String,
    pub retryable: bool,
    pub retry_after_ms: Option<u64>,
}

impl ServiceError {
    pub fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
            retryable: false,
            retry_after_ms: None,
        }
    }

    pub fn invalid(message: impl Into<String>) -> Self {
        Self::new("InvalidArgument", message)
    }

    pub fn permission_denied() -> Self {
        Self::new("PermissionDenied", "operation is not permitted")
    }

    pub fn not_found() -> Self {
        Self::new("NotFound", "record was not found")
    }

    pub fn conflict(message: impl Into<String>) -> Self {
        Self::new("Conflict", message)
    }

    pub fn unavailable(message: impl Into<String>) -> Self {
        let mut error = Self::new("ServerUnavailable", message);
        error.retryable = true;
        error.retry_after_ms = Some(1000);
        error
    }

    pub fn into_body(self) -> ErrorBody {
        ErrorBody {
            code: self.code.to_owned(),
            message: self.message,
            retryable: self.retryable,
            retry_after_ms: self.retry_after_ms,
        }
    }
}

impl fmt::Display for ServiceError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for ServiceError {}

pub fn required_capability(method: &str) -> Option<&'static str> {
    use memory_core::capability::*;
    match method {
        "instructions.put" | "instructions.revise" => Some(INSTRUCTION_WRITE),
        "instructions.list" => Some(INSTRUCTION_READ),
        "sessions.append_event"
        | "sessions.update_summary"
        | "sessions.summarize"
        | "sessions.compact"
        | "sessions.archive_old"
        | "maintenance.run" => Some(SHORT_TERM_WRITE),
        "sessions.get_context" | "sessions.get_recent_context" | "sessions.get_history" => {
            Some(SHORT_TERM_READ)
        }
        "runtime.prepare_checkpoint" => Some(SHORT_TERM_WRITE),
        "runtime.restore_checkpoint" | "runtime.list_checkpoints" => Some(SHORT_TERM_READ),
        "long_term.put" | "long_term.revise" | "long_term.reconcile_slots" => Some(LONG_TERM_WRITE),
        "long_term.search_headers" | "long_term.get_many" => Some(LONG_TERM_READ),
        "long_term.forget" | "long_term.wait_forget" => Some(FORGET),
        "projection.enqueue_turn" => Some(SHORT_TERM_WRITE),
        "projection.claim"
        | "projection.save_result"
        | "projection.complete"
        | "projection.fail"
        | "projection.upsert_mapping"
        | "projection.requeue_dead" => Some(LONG_TERM_WRITE),
        "projection.resolve" | "projection.status" | "projection.list_dead" => Some(LONG_TERM_READ),
        "sync.request" => Some(SYNC),
        "audit.status" => Some(AUDIT_STATUS),
        "audit.list" | "audit.export" => Some(AUDIT_READ),
        "audit.record" => Some(AUDIT_WRITE),
        "privacy.settings.get" | "privacy.export_data" => Some(PRIVACY_READ),
        "privacy.settings.update" | "privacy.import_data" => Some(PRIVACY_WRITE),
        "privacy.clear_history" | "privacy.clear_all" => Some(FORGET),
        _ => None,
    }
}
