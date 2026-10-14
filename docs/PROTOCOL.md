# `agent-memory.v1` local protocol

## Transport

- UDS default: `/run/bianbu/agent-memoryd.sock`.
- Current M0 diagnostic wire format: one UTF-8 JSON object per line.
- Maximum frame: 4 MiB including the newline (`memory_core::MAX_FRAME_BYTES`); the daemon
  advertises the effective value as `max_frame_bytes` in the `system.hello` result.
- Every UDS connection must send `system.hello` first.  D-Bus calls are exempt: the bus
  supplies the identity, so `system.hello` is rejected there (`docs/DBUS_CONTROL_PLANE.md`).
- The server resolves `SO_PEERCRED`, PID start time and Runtime registration before returning identity.
- A connection is pinned to one identity snapshot until it closes.

Request envelope:

```json
{
  "protocol": "agent-memory.v1",
  "request_id": "uuid",
  "trace_id": "uuid",
  "method": "long_term.put",
  "params": {},
  "idempotency_key": "caller-stable-key",
  "deadline_ms": 5000
}
```

Success and error envelopes:

```json
{
  "status": "ok",
  "request_id": "uuid",
  "trace_id": "uuid",
  "server_protocol_version": "agent-memory.v1",
  "result": {}
}
```

```json
{
  "status": "error",
  "request_id": "uuid",
  "trace_id": "uuid",
  "server_protocol_version": "agent-memory.v1",
  "error": {
    "code": "Conflict",
    "message": "expected revision 1, current revision is 2",
    "retryable": false,
    "retry_after_ms": null
  }
}
```

## Security properties

- Top-level identity fields are consistency assertions only. A mismatch is denied and audited.
- Capability is checked at dispatch and namespace is repeated in every canonical SQL predicate.
- `PermissionDenied` does not distinguish a missing record from a record outside the namespace.
- Memory text, event bodies and tool results are never put in ordinary daemon logs or audit details.
- Production Forget remains denied until an Agent OS confirm-token verifier is connected.
- Development confirmation uses an explicitly enabled daemon mode and an environment-only token.

## Methods implemented in the current local service

| Group | Methods |
|---|---|
| System | `system.hello`, `system.health`, `system.stats` |
| Account | `account.status`; mutation methods return explicit adapter-unavailable errors |
| Instruction | `instructions.put/list/revise` |
| Session | `sessions.append_event/update_summary/get_context/get_recent_context/compact/summarize` |
| Long term | `long_term.put/search_headers/get_many/revise/reconcile_slots/forget/wait_forget`; `reconcile_slots` defaults to dry-run and, when applied, keeps the newest same-slot record by HLC/revision while creating Tombstones/outbox forget operations for superseded records |

Long-term records may carry at most one `slot:<name>` tag. Slot names use ASCII
letters, digits, `.`, `_`, and `-`; this keeps preference updates deterministic and
prevents one record from becoming authoritative for unrelated fields.
| Privacy / maintenance | `privacy.settings.get/update`, `privacy.export_data`, `privacy.clear_history/clear_all`, `maintenance.run` |
| Projection | `projection.enqueue_turn/claim/save_result/complete/fail/status/list_dead/requeue_dead/resolve/upsert_mapping`; dead-job listing is metadata-only and requeue is namespace/Capability scoped |
| Sync | `sync.status/request` (durable offline queue; no cloud exchange adapter yet) |
| Backup | `backup.create/run_scheduled/list/preview_restore/restore`; disabled by default and returns `BackupUnavailable` until both a Capsule key provider and storage Adapter are configured. `run_scheduled` performs Server-side manual/daily/weekly/monthly due evaluation. `memoryd` independently scans due maintenance/backup work and persists retry state in schema v8. Local-directory and optional HTTPS `X-API-Key` adapters are available; OAHU2 account authentication and Secret Broker key recovery remain external |
| Audit | `audit.status/list/export/record`; local JSON export is configured, external SEC-06 export is pending |
| Runtime | `runtime.prepare_checkpoint/list_checkpoints/restore_checkpoint` |

CBOR length-prefix framing can be added as a negotiated encoding without changing the method schema. JSON remains useful for diagnostics and compatibility tests.

## D-Bus control plane

The same envelope runs over the system bus as `com.bianbu.AgentMemory1`
(`/com/bianbu/AgentMemory1`): `Call(s request_json) -> s response_json` plus a read-only
`ProtocolVersion` property.  Identity comes from the bus (caller PID -> `/proc` uid/gid/start
time -> Runtime registration), so `system.hello` is refused.  Contract, `busctl` examples and
the 2026-09-23 K3 acceptance record are in `docs/DBUS_CONTROL_PLANE.md`.
