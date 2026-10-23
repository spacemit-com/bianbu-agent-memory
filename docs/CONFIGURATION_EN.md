# Agent Memory Configuration

[English](CONFIGURATION_EN.md) | [简体中文](CONFIGURATION.md)

## 1. Configuration ownership

Product configuration is owned by memoryd. The default file is:

```text
/var/lib/bianbu-agent-memory/memory.config.json
```

The file name is derived from the database path `memory.db`. memoryd creates it the
first time it opens the database, stores it with mode `0600`, and updates it through
a temporary file, `fsync`, and an atomic `rename`. Runtime, BiBit, and Agents must
not edit this file or the SQLite database directly.

Configuration API:

```text
privacy.settings.get
privacy.settings.update
```

Updates are partial: omitted fields keep their current values. memoryd performs the
final validation and writes the change to the audit log.

## 2. Configuration scope

| Setting (API name) | Default | Scope | Can an Agent set it directly? | Notes |
| --- | --- | --- | --- | --- |
| `memory_enabled` | `true` | Account/Agent/space override; product file supplies the default | Trusted integrators may request it | When disabled, memoryd rejects L1-L3 writes and recall |
| `retention_days` | `0` (forever) | Account/Agent/space override; product file supplies the default | Trusted integrators may request it | Only `0/7/30/90` are accepted |
| `audit_enabled` | `true` | Account/Agent/space override; product file supplies the default | Trusted integrators may request it | Disabling it cannot bypass mandatory audit actions |
| ~~`backup_enabled`~~ | — | Retired | — | Cloud backup was retired on 2026-10-16; `privacy.settings.update` still accepts the old field but ignores it, the `cloud_backup_enabled` key in `memory.config.json` is dropped on read, and it is absent from responses |
| ~~`backup_frequency`~~ | — | Retired | — | Same as above; the old field may be sent but is ignored, and the key disappears from an old file after the next write |
| ~~`backup_scopes`~~ | — | Retired | — | Same as above; `important` remains a reserved tag on L3 records and is unrelated to cloud backup |
| `sensitive_action_confirmation` | `standard` | Product-level | Controlled management path only | `loose/standard/strict`; command confirmation is owned by Runtime/Security |
| `preference_auto_apply_enabled` | `true` | Product-level | Controlled management path only | Whether Runtime automatically applies preferences |
| `development_habit_auto_apply_enabled` | `true` | Product-level | Controlled management path only | Whether Runtime automatically applies development habits |
| `recent_turn_limit` | `5` | Product-level | Controlled management path only | `1..50` |
| `recent_session_limit` | `5` | Product-level | Controlled management path only | `1..20` |
| `summary_interval_turns` | `5` | Product-level | Controlled management path only | `1..50`; trigger a summary every N completed turns |
| `summary_retention_days` | `7` | Product-level | Controlled management path only | `1..365` |
| `timezone` | `Asia/Shanghai` | Product-level | Controlled management path only | `UTC` or an IANA time zone name |
| `local_device_id` | `k3-development` | Product-level | Controlled management path only | Local device identifier metadata, not a Runtime identity |
| `summary_model` | disabled | Product-level | Controlled management path only | See the next section |
| `max_database_bytes` | `536870912` | Product-level local resource limit | Controlled management path only | Total byte limit for the SQLite database plus WAL/SHM, `16 MiB..8 GiB` |
| ~~`max_backup_bytes`~~ | — | Retired | — | Retired with cloud backup; the old field may be sent but is ignored, and the key disappears from an old file after the next write |
| `min_free_bytes` | `67108864` | Product-level local resource limit | Controlled management path only | Filesystem free-space reserve required before writes, `0..8 GiB` |

Retired fields are accepted only when the same request also carries at least one
active setting; sending only retired fields still returns
`InvalidArgument: at least one setting is required`.

Account/Agent/space overrides are stored in the SQLite `memory_settings` table with
this primary key:

```text
(account_id, agent_id, memory_space_id)
```

`retention_days=0` is stored as `NULL` in the override table to mean permanent
retention. Values in the product file are defaults used when no override exists.
`summary_model`, the auto-apply switches, recent-count limits, timezone, and similar
settings are currently product-level and are not stored per Agent.

## 3. Agent identity and process switch

The following values are not privacy settings and cannot be set through a normal
Agent request body:

```text
account_id
agent_id
agent_name
agent_instance_id
memory_space_id
```

They come from the Runtime registry, the systemd environment, and the `system.hello`
handshake. memoryd validates UID/GID/PID, process start time, registration-file
permissions, and capabilities to prevent an Agent from impersonating another account
or Agent.

There is also a Runtime process-level switch:

```text
BIANBU_AGENT_MEMORY_ENABLED=true|false
```

It controls whether Runtime loads and calls the external Memory backend. It is
enabled by default. It differs from memoryd's persisted `memory_enabled`: when the
Runtime switch is off, Runtime does not issue SDK requests; when `memory_enabled` is
off, requests that reach memoryd return `MemoryDisabled`.

## 4. Summary model configuration

The `summary_model` structure is:

```json
{
  "enabled": true,
  "base_url": "http://127.0.0.1:8080/v1",
  "model": "qwen2.5-1.5b-instruct",
  "auth": "none",
  "credential_env": null,
  "max_context_tokens": 8192,
  "max_output_tokens": 512,
  "cpu_threads": 4,
  "ram_budget_mb": 4096,
  "max_latency_ms": 15000
}
```

When enabled, a trusted `http://` `base_url` and a `model` are required. Authentication
must be `none`, `bearer_env`, or `api_key_env`; `credential_env` stores only the
environment-variable name, never the token itself. If the configured summary model
fails, keep the rule-based summary and the raw L1 events, and return the degradation
reason. Never report the failure as success.

## 5. Semantic-search Embedding configuration

The `embedding` section is passed as a whole through `privacy.settings.update`. It is
enabled by default and points at the Embed domain of spacemit-ai-gateway. If the
gateway or model is unavailable, search degrades to lexical retrieval. It can be
disabled from the settings page or with an explicit `enabled: false`.

```json
{
  "enabled": true,
  "base_url": "http://127.0.0.1:18790/v1/embed",
  "model": "qwen3-embedding-0.6b-q8_0",
  "dims": 1024,
  "format": "int8",
  "batch_size": 32,
  "max_text_tokens": 2560,
  "max_latency_ms": 1500,
  "index_timeout_ms": 30000,
  "inline_timeout_ms": 500,
  "query_instruct": "Instruct: Given a user query about a person's memories, retrieve the stored memory records that are semantically relevant to the query\nQuery: "
}
```

| Field | Default | Range | Notes |
| --- | --- | --- | --- |
| `enabled` | `true` | Boolean | Enables semantic search; when off, only lexical search runs |
| `base_url` | `http://127.0.0.1:18790/v1/embed` | `http://` URL, up to 2048 characters | Embedding service URL; required when enabled, defaults to the local gateway |
| `model` | `qwen3-embedding-0.6b-q8_0` | Gateway model ID | Must be the real model ID on the gateway; changing the model or dimensions invalidates old vectors and triggers a rebuild |
| `dims` | `1024` | `64..8192` | Vector dimensions; must match the model output |
| `format` | `int8` | `int8` / `f32` | Vector storage format; `int8` means symmetric quantization |
| `batch_size` | `32` | `1..128` | Maximum records per background backfill request; each request is also capped by the fixed 2560-token budget |
| `max_text_tokens` | `2560` | `64..8192` | Token budget for one text before embedding; text over budget keeps its head and tail and omits the middle |
| `max_latency_ms` | `1500` | `100..60000` | Query-side latency budget; the query path uses `min(max_latency_ms, 1500)` to leave time for the lexical stage |
| `index_timeout_ms` | `30000` | `100..600000` | Per-request timeout for background batch backfill and indexing |
| `inline_timeout_ms` | `500` | `100..2000` | Timeout for the single-record inline embed after a write commits; on timeout the background backfill takes over |
| `query_instruct` | Qwen3-Embedding recommended query prefix | Up to 512 characters | Applied on the query side only; empty means no prefix |

Behavior:

- After `put` / `revise` commits, one best-effort inline embed is attempted. Failures
  are logged and completed by the background backfill; search falls back to FTS.
- When both paths return results, search uses vector recall plus RRF fusion. If
  Embedding is disabled, fails, or times out, search always falls back to lexical
  retrieval.
- Records with `sensitivity=secret` are never embedded.
- Vectors are derived data: they are rebuilt after restore and removed by `forget` /
  `clear_all`.
- spacemit-ai-gateway downloads and loads the model. This section documents only the
  connection parameters used by memoryd.

## 6. Retired configuration and compatibility

Product change on 2026-10-16: cloud backup for memory data was retired in favor of
local export/import. This package no longer reads `backup.env` and has removed the
legacy CLI arguments `--backup-directory`, `--capsule-key-file`, `--backup-base-url`,
and `--backup-api-key-*`; the unit shipped by the package no longer passes them. It
also no longer creates `/var/lib/bianbu-agent-memory/capsules` or
`/etc/bianbu-agent-memory/capsule-key`. If an old unit or drop-in on the system still
passes those arguments, memoryd fails to start on the unknown option, so replace it
with the current unit during upgrade. The cloud-backup implementation remains on the
`feature/mem-l4-cloud-backup` branch and will not enter main before integration
testing.

The keys `cloud_backup_enabled`, `backup_frequency`, `backup_scopes`, and
`max_backup_bytes` written by older versions are **discarded** when the configuration
is read, are not validated, and are not written back. Old files still load, and the
four keys disappear after the next settings write. Other unknown keys are still
rejected by `deny_unknown_fields`. `privacy.settings.get` no longer returns
`backup_enabled` or `backup_supported`.

## 7. Configuring through a trusted host or the SDK

The BiBit settings page calls `privacy.settings.update` through the host capability
`service:memory`. Other trusted integrators call
`client.privacy.update_settings(...)` directly. Runtime removed the
`/api/v1/plugin-consoles/...` management routes on 2026-09-21; do not integrate with
that old HTTP interface.

The request must not contain account or Agent identity fields, which come from the
trusted Runtime process context. The structured `summary_model` and `embedding`
settings are passed through the same entry point as whole objects; missing fields are
filled with defaults, so send the complete structure every time. Configuration changes
are not exposed as model Tools, so a model cannot change memory, retention, or audit
policy without user confirmation.

## 8. Process-level environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `BIANBU_AGENT_MEMORY_ENABLED` | `true` | Whether Runtime loads and calls the external Memory backend; when off, Runtime does not issue SDK requests |
| `AGENT_MEMORY_EMBEDDING_DISABLED` | unset | Suppresses only the out-of-the-box Embedding default; explicit configuration sections are unaffected. Intended for hermetic tests and troubleshooting |
| `AGENT_MEMORY_HEALTH_SETTLE_MS` | `3000` | Skip health sampling when the latest write is younger than this; `0` means do not wait |
| `AGENT_MEMORY_HEALTH_CHECK_INTERVAL_MS` | `30000` | Reuse the previous health sample for this window; `0` means check every time |

Fields in `system.health` are diagnostic output, not writable configuration.
