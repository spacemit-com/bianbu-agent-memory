# Agent Memory Integration API and Operations Guide

[English](INTEGRATION_API_EN.md) | [简体中文](INTEGRATION_API.md)

This document describes the stable integration boundary between AgentMemory and
Runtime, BiBit, other Agents, and memoryd. Not every interface here is an external
HTTP API: business integrations should use the Python SDK; the BiBit settings page
uses the host capability `service:memory`; the raw UDS protocol and D-Bus are
advanced integration surfaces. Do not read or write SQLite, `memory.config.json`, or
internal tables directly.

| Layer | Entry point | Role |
| --- | --- | --- |
| Python SDK | `MemoryClient` / `AsyncMemoryClient` | Preferred stable interface for business integrations |
| BiBit host capability | `service:memory` | Integration surface for the settings page and trusted hosts; not HTTP |
| Runtime Tool (MCP) | `memory` / `memory_record` / `memory_retrieve` / `session_search` | Called by the runtime model; not a general SDK API |
| Raw UDS protocol | `agent-memory.v1` | For custom clients and diagnostics; the client handles handshake and identity |
| D-Bus | `com.bianbu.AgentMemory1` | Optional control plane; ordinary Agents should not use it directly |

## 1. Component boundaries

```text
BiBit settings page / external Agent / Runtime Hook or Tool
                 │
                 ├─ BiBit host capability service:memory
                 ├─ Runtime MCP Tools
                 │    memory / memory_record / memory_retrieve / session_search
                 └─ Python Memory SDK
                              │ Unix Domain Socket
                              ▼
                       bianbu-agent-memoryd
                         ├─ SQLite authoritative data
                         ├─ memory.config.json
                         └─ L1/L2/L3, audit, export/import
```

- Runtime owns session lifecycle, trusted account/Agent identity, Hook/Tool dispatch,
  and user confirmation.
- The Memory SDK owns the client protocol, parameter encoding, reconnects, and type
  conversion.
- memoryd owns identity checks, capabilities, namespace isolation, validation,
  transactions, audit, and persistence.
- SQLite is the authoritative source; FTS/vector indexes are rebuildable derived data.
- Cloud backup was retired on 2026-10-16. This service does not create, upload, or
  restore cloud backup packages. Local backup/restore uses `privacy.export_data` /
  `privacy.import_data`; the exported document carries a namespace `scope`, and import
  requires the current account/agent/space to match.
- Memory does not own the community knowledge base, whole-disk file search, or command
  preview. Those belong to BiBit/Agent OS and Runtime/Security.

## 2. Runtime identity handshake

Runtime injects trusted environment variables when it starts a Worker:

```text
BIANBU_AGENT_MEMORY_SOCKET=/run/bianbu/agent-memoryd.sock
BIANBU_AGENT_MEMORY_REGISTRY_DIR=/run/bianbu/agent-runtime/memory-registry
BIANBU_AGENT_MEMORY_ACCOUNT_ID=default
BIANBU_AGENT_MEMORY_AGENT_NAME=default-agent
BIANBU_AGENT_MEMORY_ENABLED=true
```

The values above are development examples; production values come from the Runtime
account and Agent configuration.

The first frame after an SDK opens a UDS connection must be `system.hello`:

```json
{
  "protocol": "agent-memory.v1",
  "request_id": "uuid",
  "trace_id": "uuid",
  "method": "system.hello",
  "params": {
    "sdk_language": "python",
    "sdk_version": "0.1.6",
    "account_id": "default",
    "agent_name": "default-agent"
  }
}
```

`request_id` and `trace_id` are required fields. `idempotency_key` may be omitted,
and `deadline_ms` is optional. The example is a complete request envelope; do not
send only `params`.

memoryd binds the UDS peer credentials (UID/GID/PID), Runtime registry, process start
time, and capabilities to the handshake identity. An ordinary Agent cannot override the bound identity by sending `account_id` /
`agent_name`. Only a trusted Runtime registered with `allow_runtime_context` may
select another account/Agent context; the service then derives an independent
`memory_space_id`.

## 3. Python SDK

After installing `bianbu-agent-memory`, Runtime or a trusted integrator can use:

```python
from bianbu_agent_sdk.memory import MemoryClient

with MemoryClient.connect(
    "/run/bianbu/agent-memoryd.sock",
    timeout=2.0,
    account_id="default",
    agent_name="default-agent",
) as client:
    settings = client.privacy.settings()
    client.privacy.update_settings(
        memory_enabled=True,
        retention_days=7,
        audit_enabled=True,
        recent_turn_limit=5,
    )
```

The example requires the calling process to be registered by Runtime, or to connect
to an isolated `--development-identity` daemon. Connecting directly to the production
socket from an ordinary shell returns
`Unauthenticated: runtime registration was not found`; this is identity validation,
not an SDK failure.

Common APIs:

| SDK call | memoryd method | Purpose |
| --- | --- | --- |
| `client.sessions.append_event(...)` | `sessions.append_event` | Write an L1 Session Event |
| `client.sessions.get_recent_context(...)` | `sessions.get_recent_context` | Read recent context |
| `client.sessions.get_history(...)` | `sessions.get_history` | Query session history |
| `client.long_term.put(...)` | `long_term.put` | Write an L3 long-term memory |
| `client.long_term.search_headers(...)` | `long_term.search_headers` | Search long-term memory headers; with semantic search enabled, results use lexical + vector RRF ranking |
| `client.long_term.get_many(...)` | `long_term.get_many` | Read authoritative memories |
| `client.privacy.settings()` | `privacy.settings.get` | Read configuration |
| `client.privacy.update_settings(...)` | `privacy.settings.update` | Partially update configuration |
| `client.privacy.export_data(...)` | `privacy.export_data` | Export data |
| `client.privacy.import_data(...)` | `privacy.import_data` | Restore data from an export document (confirmation required) |
| `client.privacy.clear_history(...)` | `privacy.clear_history` | Clear session history (confirmation required) |
| `client.privacy.clear_all(...)` | `privacy.clear_all` | Clear all memory (confirmation required) |
| `client.runtime.prepare_checkpoint(...)` | `runtime.prepare_checkpoint` | Create a memory watermark checkpoint |
| `client.runtime.restore_checkpoint(...)` | `runtime.restore_checkpoint` | Count events and pending-sync queue changes after a checkpoint |

For asynchronous code use `AsyncMemoryClient`; method names and parameters match the
synchronous client.

On L3 records, `important` is the reserved tag for the user-designated important
mark. The SDK provides `long_term.mark_important(...)` / `unmark_important(...)`,
which toggle only that tag and leave the text and other tags unchanged.

Local export/import can also use the CLI. Like the SDK, the CLI depends on Runtime
registration; for local development, connect to an isolated `--development-identity`
daemon. `export` needs only read access; `import` also needs a confirmation token
from the caller:

```bash
bianbu-agent-memory export ~/memory-export.json --limit 500 --without-audit
bianbu-agent-memory import ~/memory-export.json --confirm-token "$TOKEN"
```

`--limit` / `limit_per_collection` is the **page size**, not the total export size.
The SDK and CLI follow `next_cursor` by default and fetch every page. The SDK returns
the merged document and the CLI writes it to a single
`bianbu-agent-memory.export.v1` file. The 500-record page limit keeps one UDS response
inside the 4 MiB frame budget; an oversized page still returns `ResourceExhausted`.
`export_data(..., all_pages=False)` returns only the first page and is intended for
diagnostics; the current public SDK does not expose `snapshot_at` / `snapshot` /
`cursor`, so it cannot resume paging through those arguments.

A custom `agent-memory.v1` client must echo the first page's fixed `snapshot` and
`next_cursor` unchanged, so memories written during the export do not enter the
snapshot. The three tables have independent rowid sequences, so the response includes
`snapshot.session_events/session_summaries/long_term_memories`. Echoing only
`snapshot_at` is the compatibility path for older clients. A merged paginated export
marks `complete=true` and includes `page_count`; single-page exports (including those
from older daemons) keep the legacy document shape and do not force that field.

`import` merges data: records whose original ID already exists are skipped; existing
memories are never overwritten or deleted. The document's `scope` must match the
current namespace.

Semantic search in `long_term.search_headers` is fully transparent to callers: the
method signature, parameters, and response shape do not change. With Embedding
enabled, the daemon runs the lexical cascade (FTS5) first, then vector recall. When
both lists have results, it fuses them with RRF(k=60) and `score` carries the RRF
value; when only one list has results, that list's original score (FTS or cosine) is
preserved. In either case the SDK should use `score` for ordering only and not
interpret its magnitude. If Embedding is disabled, the inference service is
unavailable, or the query times out (1500 ms query budget), the daemon silently falls
back to lexical results with no caller-visible error.

## 4. BiBit settings page and trusted host

The BiBit "Settings > Memory" page calls these operations through the host capability
`service:memory`:

- `privacy.settings.get` / `privacy.settings.update`
- `privacy.clear_history` / `privacy.clear_all`
- `privacy.export_data` / `privacy.import_data`

The page owns only the UI. The host injects the trusted identity, and the Adapter
forwards the request to memoryd. Confirmation tokens for delete, export, and import
operations are injected by Runtime/Security or a trusted Adapter; the page dialog is
not a security boundary (export also needs a token in `strict` mode). Runtime removed the `/api/v1/plugin-consoles/...` management routes
on 2026-09-21, and K3 returns 404 for that path. Do not integrate with that old HTTP
interface.

Other trusted integrators use the Python SDK directly. Runtime's `memory` /
`memory_record` / `memory_retrieve` / `session_search` MCP Tools use the current
`RunContext` `account_id` and `agent_id`, so multi-Agent data is isolated by
namespace. `settings.update` is not exposed as a model
Tool; privacy configuration changes must come from the UI, BiBit, or a trusted
integrator and must preserve audit and user-confirmation boundaries.

## 5. Native protocol and D-Bus

UDS uses newline-delimited JSON frames. The protocol version is `agent-memory.v1`,
and the maximum frame size is 4 MiB including the newline. The server advertises the
effective limit as `max_frame_bytes` in the `system.hello` response. Every UDS request
must complete `system.hello` before calling any other method.

memoryd also provides an optional D-Bus service:

```text
Bus name: com.bianbu.AgentMemory1
Object path: /com/bianbu/AgentMemory1
Interface: com.bianbu.AgentMemory1
Method: Call(string request_json) -> string response_json
```

D-Bus authenticates the sender through bus process credentials and does not use
`system.hello`. Ordinary Agents should not depend on D-Bus; the Runtime/SDK UDS path
can carry the Agent context selected by Runtime and provides better compatibility and
isolation.

## 6. Error and retry conventions

- `MemoryDisabled`: memory is disabled; never interpret it as a successful write.
- `Unauthenticated` / `PermissionDenied`: identity, capability, or confirmation token
  requirements are not satisfied.
- `ResourceExhausted`: local storage quota is exhausted, the response exceeds the
  frame limit, or the database writer is busy. Back off and retry only when the
  response has `retryable=true`.
- `ServerUnavailable` (SDK exception) / `backend_unavailable` (Tool result): memoryd is
  unavailable. Preserve the original event and never fabricate success.
- Write operations with an `idempotency_key` can be retried safely; never reuse the
  same key for different parameters.

## 7. Deployment checks

```bash
sudo systemctl status bianbu-agent-memoryd.service --no-pager
test -S /run/bianbu/agent-memoryd.sock
ls -l /run/bianbu/agent-runtime/memory-registry
curl -sS http://127.0.0.1:9899/api/v1/runtime/status
```

If Runtime returns `backend_unavailable`, first check the memoryd logs, UDS, registry
permissions, and Runtime/Memory package versions. Do not switch to Runtime-local files
and claim that Memory has been written.
