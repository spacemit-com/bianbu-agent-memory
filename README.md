# Bianbu Agent Memory

**Give your agent a memory, so it never starts from scratch.**

[English](README.md) | [简体中文](README_CN.md)

Hi, this is Bianbu Agent Memory, the memory subsystem of Bianbu Agent OS.

Think of it as three drawers that an agent carries around:

- **L1 session drawer**: what was just said, and which turns actually completed;
- **L2 working drawer**: a structured summary of a conversation, so the next
  session can pick up quickly;
- **L3 long-term drawer**: user preferences, development habits, important
  markers, plus the evidence and revisions behind them.

It lives on the device, and the authoritative data stays there. Basic storage
and full-text search need no model service. Embedding recall or model-generated
summaries are optional add-ons.

## Architecture: who does what?

```text
┌─────────────────────────────────────────────┐
│ Agent / host application                    │
│ Hermes · DeepSeek Harness · BiBit · yours   │
└──────────────────┬──────────────────────────┘
                   │ SDK / CLI / MCP / Runtime Bridge / Adapter
                   ▼
        ┌──────────────────────────┐
        │ bianbu-agent-memoryd     │
        │ Rust daemon (UDS/D-Bus)  │
        └────────────┬─────────────┘
                     │
      ┌──────────────┼───────────────┐
      ▼              ▼               ▼
 SQLite/SQLCipher  FTS5 full-text   optional Embedding / Mem0
 authoritative     index            semantic projection (rebuildable)
 L1/L2/L3
```

In one sentence each:

- **Clients do the talking**: SDK, CLI, MCP, Runtime Bridge, and host adapters;
- **memoryd does the remembering**: persistence, search, retention,
  export/import, and audit;
- **Indexes guess what you mean**: FTS5 is the baseline; Embedding and Mem0 are
  optional extras.

Indexes are always derived data. A semantic hit is resolved back to the current
canonical record before its text is returned, so vector-store text is never
injected straight into the model context.

To build your own agent or integrate an existing host, start with the
[Integration API](docs/INTEGRATION_API_EN.md).

## What can it do?

| Capability | In plain words |
| --- | --- |
| **Layered memory** | L1 keeps events and completed turns, L2 keeps working summaries, L3 keeps preferences, habits, and important markers |
| **Find it again** | Local FTS5 search plus bounded reads; optional semantic recall with lexical fallback |
| **Remember safely** | SQLCipher encryption, transactional writes, idempotent receipts, WAL, storage quotas |
| **Keep it separated** | Trusted Runtime identity, capability checks, account/agent/memory-space isolation |
| **Stay manageable** | Retention, privacy settings, clear history/all, JSON export/import, audit, Forget, checkpoints |
| **Connect easily** | Sync/async Python SDK, CLI, MCP, Runtime Bridge, and adapters for Hermes, DeepSeek Harness, and BiBit |

A few things worth pinning up:

- **The SDK is not the service**: `pip install` only installs a client;
  `bianbu-agent-memoryd` does the real work.
- **Import merges, it does not replace**: re-importing the same file skips
  records by original id instead of duplicating them.
- **Export is complete**: pages hold at most 500 rows, and the SDK follows the
  cursor to merge every page for the whole retention window.
- **Deletion is a tombstone**: canonical records and tombstones decide
  visibility; indexes are only derived data.

## Where is the code?

| Path | What it does |
| --- | --- |
| `crates/memory-core` | Memory domain model and core rules; start here to see what a memory looks like |
| `crates/memory-store` | SQLite/SQLCipher persistence, search, retention, export/import |
| `crates/memory-protocol` | Requests, responses, and error codes shared by both sides |
| `crates/memoryd` | The `bianbu-agent-memoryd` daemon, UDS server, and D-Bus control plane |
| `sdk/python` | Python SDK, CLI, MCP server, Runtime Bridge, BiBit adapter |
| `adapters/hermes` | External memory provider for Hermes v0.21.7 |
| `adapters/deepseek-harness` | Cordis plugin and Python bridge for DeepSeek Harness 0.1.0-rc.6 |
| `packaging` | systemd units, launch scripts, D-Bus policy |
| `debian` | Debian packaging metadata |
| `migrations` | Database schema migrations |
| `scripts` | Field acceptance and diagnostic scripts |
| `docs` | Protocol, configuration, integration, and test documentation |

If you are reading the code for the first time, follow
`memory-core` → `memory-store` → `memoryd` → `sdk/python`. If you just want to
run it, jump to the next section.

## Build and install

### Option 1: install from the Bianbu repository

If the device already has the Bianbu software source configured:

```bash
sudo apt update
sudo apt install bianbu-agent-memory bianbu-agent-memoryd
```

- `bianbu-agent-memory`: Python SDK + CLI;
- `bianbu-agent-memoryd`: the memory daemon.

The package creates the service account, key package, default configuration,
and systemd unit, and starts the daemon. On a device using the built-in Bianbu
Runtime or BiBit integration, the Runtime supplies the trusted identity
registration, so no manual configuration is normally needed. A custom program
connecting directly through the SDK/CLI needs a development identity or a
Runtime registration; otherwise the daemon rejects it. Consult the
[configuration guide](docs/CONFIGURATION_EN.md) when changing retention, summary
models, embeddings, quotas, or other non-default policies.

### Option 2: build from source

Linux is the target. The verified environment is riscv64 with Rust 1.93.1 and
Python 3.14.4. The locked dependency set requires **Rust 1.87+** (zbus 5.19 and
edition 2024), even though this workspace's own `rust-version` is 1.80. Also
required: Python 3.10+, a C toolchain, pkg-config, and OpenSSL development
headers (the bundled SQLCipher build links OpenSSL).

Run the commands below from the repository root. A cold release build takes
several minutes on K3.

```bash
sudo apt install build-essential pkg-config libssl-dev \
  python3-venv python3-dev libffi-dev

cargo build --workspace --release --locked

python3 -m venv .venv && . .venv/bin/activate
python -m pip install -e 'sdk/python'
```

## Run it in five minutes

The development instance uses a temporary database and a development identity.
**Do not put real personal data in it.**

### Step 1: start the daemon

```bash
umask 077
export BIANBU_MEMORY_DEV_DIR="$(mktemp -d /tmp/bianbu-memory-dev.XXXXXX)"
echo "$BIANBU_MEMORY_DEV_DIR"

target/release/bianbu-agent-memoryd \
  --development-identity \
  --allow-plaintext \
  --socket "$BIANBU_MEMORY_DEV_DIR/agent-memoryd.sock" \
  --database "$BIANBU_MEMORY_DEV_DIR/memory.db" \
  --background-device-id dev-local
```

### Step 2: write one memory, then find it

Open a second terminal, activate the virtual environment, and export the
directory printed by step 1 (for example,
`export BIANBU_MEMORY_DEV_DIR=/tmp/bianbu-memory-dev.abcd1234`):

```bash
python - <<'PY'
import os
from pathlib import Path
from bianbu_agent_sdk.memory import MemoryClient, MemoryKind

sock = Path(os.environ["BIANBU_MEMORY_DEV_DIR"]) / "agent-memoryd.sock"
with MemoryClient.connect(sock, timeout=2.0,
                          account_id="default", agent_name="default-agent") as client:
    receipt = client.long_term.put(
        kind=MemoryKind.PREFERENCE,
        text="I prefer a dark terminal theme.",
        summary="Terminal theme preference: dark",
        idempotency_key="readme:terminal-theme:v1",
    )
    print("saved:", receipt.memory_id, receipt.revision)

    for header in client.long_term.search_headers("terminal", top_k=5):
        record = client.long_term.get_many([header.memory_id])[0]
        print(record.memory_id, record.text)
PY
```

An idempotency key is the identity of one logical write: reuse it for retries,
use a new one for new content.

### Step 3: check health

```bash
bianbu-agent-memory --socket "$BIANBU_MEMORY_DEV_DIR/agent-memoryd.sock" health
```

Right after startup you may see `status: unknown` or
`database_check.state: busy`. That usually means the database self-check is
still running, not that reads or writes failed. The real proof is step 2:
can you find the memory you just wrote? Press Ctrl+C to stop the daemon.

## Integrating Hermes

Hermes keeps it tidy: memory is an **external provider**, so no Hermes core file
is changed. This adapter targets Hermes `v0.21.7` (validated commit
`71d34bad1045610252eda02ee79549436896e784`).

### Three steps

```bash
# 1. Install the SDK
python -m pip install /path/to/bianbu-agent-memory/sdk/python

# 2. Drop in the plugin
mkdir -p "$HERMES_HOME/plugins/bianbu"
cp /path/to/bianbu-agent-memory/adapters/hermes/__init__.py \
  "$HERMES_HOME/plugins/bianbu/__init__.py"

# 3. Select bianbu in the Hermes provider configuration
```

Two things to remember:

- `bianbu-agent-memoryd` must already be running, and the Runtime registry must
  be able to resolve the Hermes process;
- for local development only, point `BIANBU_AGENT_MEMORY_SOCKET` at the
  temporary daemon socket.

### When does it remember?

| Hermes hook | What the memory module does |
| --- | --- |
| `initialize` | Handshake and immutable identity |
| `system_prompt_block` | Supply the effective Instruction Memory |
| `prefetch` | Recall relevant memory; a cache miss waits at most 2 seconds, then returns empty |
| `sync_turn` | After a completed turn, write events, extract, and store summary/checkpoint |
| `on_memory_write(add/replace/remove)` | Explicit save, revision-checked update, pending-confirmation removal |
| `on_pre_compress` | Save a structured summary and context pointers before compression |
| `on_session_switch` | Switch sessions and invalidate stale caches |
| `shutdown` | Stop intake, drain the queue, close connections |

The queue is bounded and in-process; it is not a durable outbox, so unfinished
work may be lost when the process exits.

## Integrating DeepSeek Harness

On the DeepSeek Harness side there is a **Cordis plugin plus a small Python
bridge**. It uses the host's public plugin hooks and tool registry, again
without touching host source. It targets `0.1.0-rc.6`. Note that DeepSeek
Harness is an agent host, not the DeepSeek chat application.

### Two files, two jobs

- `adapters/deepseek-harness/index.mjs`: the plugin; wires lifecycle hooks and
  registers tools;
- `adapters/deepseek-harness/bridge.py`: a JSON-lines bridge that connects the
  plugin to the memory service;
- `adapters/deepseek-harness/acceptance.mjs`: the acceptance script.

Configure the plugin's `python`, `bridge`, and `state` options in the host
profile; add `sdk` when the SDK lives in a custom location. Keep the state
directory outside this repository, and never commit databases, keys, or logs.

### What tools does the model get?

| Tool | Purpose |
| --- | --- |
| `memory_search` | Structured search returning id, revision, source, tags, and dates |
| `memory_save` | Explicit save with validation, deduplication, and relative-date normalization |
| `memory_update` | Update by `memory_id` + `expected_revision`; stale revisions are refused |
| `memory_remove` | Show a preview first; delete only after the **user** sends the confirmation message |

Safety in one breath: recalled content is injected as untrusted context;
plugin-origin messages are excluded from writes; subagents are read-only.

## Optional extras

- **Embedding recall**: lexical + vector fusion with lexical fallback on error
  or timeout; enable it without changing authoritative storage.
- **Mem0 / D+ projection**: a rebuildable semantic index that never replaces
  authoritative storage. Install with
  `python -m pip install -e 'sdk/python[dplus]'`.
- **Model summaries**: an optional add-on that does not affect baseline memory
  or search.

## Development and testing

```bash
python -m pip install -e 'sdk/python[test]'

cargo fmt --all -- --check
cargo test --workspace --locked
python -m pytest sdk/python/tests -q
```

When submitting a change, describe the scenario, behavior change, and
verification. Protocol and SDK changes must update docs and regression tests.
Never commit credentials, private keys, user databases, exported memory, or
field logs.

## A few boundaries

- **Platform**: the core flow targets Linux and is verified on riscv64; other
  architectures must be built and tested by the integrator.
- **No LLM required for basic use**: storage and lexical search work on their
  own; semantic recall and summaries are optional enhancements.
- **The SDK does not start the daemon**: if connection fails, check the service
  and socket path first.
- **Indexes are not the truth**: canonical records and tombstones decide
  visibility; indexes are rebuildable derived data.

The Rust workspace and Python SDK metadata declare Apache-2.0.

That is the tour. Now go write a memory of your own.
