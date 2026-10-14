use crate::registry::{IdentityResolver, PeerIdentity};
use memory_core::PROTOCOL_VERSION;
use memory_protocol::{RequestEnvelope, ResponseEnvelope, ServiceError};
use memory_store::Store;
use serde_json::json;
use std::fs;
use std::sync::Arc;
use std::thread;
use zbus::blocking::{Connection, Proxy};
use zbus::{interface, message::Header};

const SERVICE: &str = "com.bianbu.AgentMemory1";
const PATH: &str = "/com/bianbu/AgentMemory1";

struct Adapter {
    identity_connection: Connection,
    store: Arc<Store>,
    resolver: Arc<IdentityResolver>,
    development_identity: bool,
}

#[interface(name = "com.bianbu.AgentMemory1")]
impl Adapter {
    fn call(&self, #[zbus(header)] header: Header<'_>, request_json: &str) -> String {
        self.call_inner(&header, request_json)
            .unwrap_or_else(|error| unbound_error(error))
    }

    #[zbus(property)]
    fn protocol_version(&self) -> &str {
        PROTOCOL_VERSION
    }
}

impl Adapter {
    fn call_inner(&self, header: &Header<'_>, request_json: &str) -> Result<String, ServiceError> {
        if request_json.len() > memory_core::MAX_FRAME_BYTES {
            return Err(ServiceError::invalid("request exceeds protocol limit"));
        }
        let request: RequestEnvelope = serde_json::from_str(request_json)
            .map_err(|_| ServiceError::invalid("request is not valid JSON"))?;
        if request.protocol != PROTOCOL_VERSION {
            return serialize_response(ResponseEnvelope::error(
                &request,
                ServiceError::new("ProtocolMismatch", "unsupported protocol version"),
            ));
        }
        if request.method == "system.hello" {
            return Err(ServiceError::invalid(
                "DBus calls are authenticated by the bus and do not use system.hello",
            ));
        }
        let sender = header
            .sender()
            .ok_or_else(|| ServiceError::new("Unauthenticated", "DBus sender is unavailable"))?;
        let proxy = Proxy::new(
            &self.identity_connection,
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
        )
        .map_err(|_| ServiceError::new("Unauthenticated", "DBus daemon is unavailable"))?;
        let pid: u32 = proxy
            .call("GetConnectionUnixProcessID", &(sender.as_str(),))
            .map_err(|_| {
                ServiceError::new("Unauthenticated", "DBus process identity is unavailable")
            })?;
        let (uid, gid) = process_credentials(pid as i32)?;
        let resolved = self.resolver.resolve(&PeerIdentity {
            uid,
            gid,
            pid: pid as i32,
        })?;
        // D-Bus authentication is bound to the registered Runtime process.
        // Unlike the UDS SDK handshake, D-Bus requests do not carry a
        // Runtime-selected context, so use the trusted base identity.
        let identity = resolved.identity;
        let confirmed = verify_development_confirmation(&request, self.development_identity);
        let response = match self.store.handle(&identity, &request, confirmed) {
            Ok(result) => ResponseEnvelope::ok(&request, result),
            Err(error) => ResponseEnvelope::error(&request, error),
        };
        serialize_response(response)
    }
}

pub fn start(
    store: Arc<Store>,
    resolver: Arc<IdentityResolver>,
    development_identity: bool,
    session_bus: bool,
) -> Result<(), Box<dyn std::error::Error>> {
    let connection = if session_bus {
        Connection::session()?
    } else {
        Connection::system()?
    };
    let identity_connection = if session_bus {
        Connection::session()?
    } else {
        Connection::system()?
    };
    connection.request_name(SERVICE)?;
    connection.object_server().at(
        PATH,
        Adapter {
            identity_connection,
            store,
            resolver,
            development_identity,
        },
    )?;
    thread::Builder::new()
        .name("agent-memory-dbus".to_owned())
        .spawn(move || {
            let _connection = connection;
            loop {
                thread::park();
            }
        })?;
    eprintln!(
        "bianbu-agent-memoryd: DBus listening on {} {} ({})",
        SERVICE,
        PATH,
        if session_bus { "session" } else { "system" }
    );
    Ok(())
}

fn process_credentials(pid: i32) -> Result<(u32, u32), ServiceError> {
    let status = fs::read_to_string(format!("/proc/{pid}/status")).map_err(|_| {
        ServiceError::new("Unauthenticated", "DBus peer credentials are unavailable")
    })?;
    let parse = |prefix: &str| -> Option<u32> {
        status
            .lines()
            .find(|line| line.starts_with(prefix))
            .and_then(|line| line.split_whitespace().nth(1))
            .and_then(|value| value.parse().ok())
    };
    Ok((
        parse("Uid:")
            .ok_or_else(|| ServiceError::new("Unauthenticated", "DBus peer uid is unavailable"))?,
        parse("Gid:")
            .ok_or_else(|| ServiceError::new("Unauthenticated", "DBus peer gid is unavailable"))?,
    ))
}

fn verify_development_confirmation(request: &RequestEnvelope, development_identity: bool) -> bool {
    if !development_identity {
        return false;
    }
    let expected = match std::env::var("BIANBU_MEMORY_DEV_CONFIRM_TOKEN") {
        Ok(value) if !value.is_empty() => value,
        _ => return false,
    };
    request
        .params
        .get("confirm_token")
        .and_then(serde_json::Value::as_str)
        .is_some_and(|provided| provided == expected)
}

fn serialize_response(response: ResponseEnvelope) -> Result<String, ServiceError> {
    serde_json::to_string(&response)
        .map_err(|_| ServiceError::unavailable("response serialization failed"))
}

fn unbound_error(error: ServiceError) -> String {
    json!({
        "status":"error",
        "request_id":"unknown",
        "trace_id":"unknown",
        "server_protocol_version":PROTOCOL_VERSION,
        "error":error.into_body()
    })
    .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_effective_process_credentials() {
        let (uid, gid) = process_credentials(std::process::id() as i32).unwrap();
        assert_eq!(uid, nix::unistd::geteuid().as_raw());
        assert_eq!(gid, nix::unistd::getegid().as_raw());
    }

    #[test]
    fn dbus_constants_are_stable() {
        assert_eq!(SERVICE, "com.bianbu.AgentMemory1");
        assert_eq!(PATH, "/com/bianbu/AgentMemory1");
    }
}
