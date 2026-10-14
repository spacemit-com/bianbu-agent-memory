# Hermes v0.21.7 provider adapter

Validated on K3 (`192.0.2.10`, Linux riscv64), 2026-10-13, with Hermes
`v0.21.7`, commit `71d34bad1045610252eda02ee79549436896e784`.
Use a user-installed plugin; no Hermes core files are changed.

## K3 isolated development entry point

```bash
/home/example/.local/bin/hermes-memory-v0217 --version
/home/example/.local/bin/hermes-memory-v0217 model
/home/example/.local/bin/hermes-memory-v0217 chat
```

The launcher starts the installed memoryd with `--development-identity` and a
separate encrypted persistent database, then stops that daemon when the CLI
exits. The profile supports one foreground launcher at a time.

- Source: `/home/example/projects/hermes-agent-memory-v0.21.7` (detached tag).
- Hermes home: `/home/example/.local/state/hermes-memory-v0217/hermes`.
- Database, secret, key package, socket and log:
  `/home/example/.local/state/hermes-memory-v0217/`.
- Plugin: `<Hermes home>/plugins/bianbu/__init__.py`.
- Config: `<Hermes home>/config.yaml`, `memory.provider: bianbu`.
- Interpreter/dependencies: historical checkout's `.venv/bin/python` (3.13.12).
  This reuses installed dependencies, **not a fresh installation of every
  v0.21.7 pinned dependency**. CLI and actual AIAgent initialization passed.
- SDK: installed `/usr/lib/python3/dist-packages/bianbu_agent_sdk`.

The historical profile and production service are preserved. Development uses
lexical recall (`AGENT_MEMORY_EMBEDDING_DISABLED=1`) without changing gateway
model instances. Configure an inference provider with `model` before chatting;
credentials are not copied from the historical profile. This launcher does not
establish production Runtime registration.

## Repeatable K3 acceptance

```bash
/home/example/projects/hermes-agent-base/.venv/bin/python \
  /home/example/mem-gateway-build/scripts/hermes_v0217_acceptance.py \
  --hermes-source /home/example/projects/hermes-agent-memory-v0.21.7 \
  --sdk-path /usr/lib/python3/dist-packages \
  --report /home/example/hermes-v0217-memory-acceptance.json
```

Uses temporary encrypted storage and a temporary Hermes home, the actual
plugin loader, MemoryManager and AIAgent initialization. Checks include
current-query recall without prewarming, cache invalidation, stable-ID revision,
refusal to mutate with `old_text` alone, pending-confirmation removal, session
events, summary revisions, session switching, provider recreation and subagent
write suppression. No inference requests are made; full LLM dialogue and
semantic embedding recall are separate acceptance stages.

## Manual installation

This adapter is intentionally outside the pinned Hermes checkout. To test it:

```bash
python -m pip install /path/to/bianbu-agent-memory/sdk/python
mkdir -p "$HERMES_HOME/plugins/bianbu"
cp /path/to/bianbu-agent-memory/adapters/hermes/__init__.py \
  "$HERMES_HOME/plugins/bianbu/__init__.py"
```

Then choose `bianbu` as the Hermes external memory provider using the normal Hermes provider configuration flow. The daemon must already be running and must resolve the Hermes PID through the Runtime registry. For a development-only run, set `BIANBU_AGENT_MEMORY_SOCKET` to the temporary daemon socket.

Lifecycle mapping:

| Hermes hook | Bianbu operation |
|---|---|
| `initialize` | UDS handshake and immutable identity |
| `system_prompt_block` | effective Instruction Memory snapshot |
| `queue_prefetch/prefetch` | FIFO recall; current query cache miss waits at most 2 seconds, returns empty on timeout |
| `sync_turn` | Runtime `MemoryBridge.record_turn`: FIFO Session Events, Mem0/rule extraction, summary/checkpoint, and content-hashed idempotency |
| `on_memory_write(add)` | Runtime `upsert_explicit`: validation, deduplication and personal-agent/target tags |
| `on_memory_write(replace)` | validated stable revision of an exact personal-agent or legacy Hermes entry selected by `previous_content` |
| `on_memory_write(remove)` | pending-confirmation Session marker; never forges a Forget confirmation |
| `on_pre_compress` | structured Summary revision with hashed context pointers |
| `on_session_switch` | switch Session binding and invalidate rewound caches |
| `shutdown` | stop intake, drain queued operations, close SDK clients on worker thread |

The adapter does not elevate recalled content into system authority and does not let Hermes supply `agent_id` or `account_id`.

Compression uses the legacy best-effort hook (checkpoint API version 1), not
a synchronous durable checkpoint. The bounded queue can drop work under
overload; background failures are logged as warnings.

The worker uses a dedicated asyncio event loop without Python 3.11's Runner.
K3 acceptance runs on Python 3.13; Python 3.10 has not been executed here.
Startup failures are raised to Hermes. Shutdown waits up to 15 seconds; if an
operation still runs, its client remains open until the worker drains. Process
termination can still lose unfinished work; this is not a durable outbox.
Queue exhaustion does not prevent shutdown from eventually closing clients.

`sync_turn` intentionally delegates to the SDK Runtime bridge rather than
duplicating the extraction rules. Explicit requests such as "请记住以后用
通俗易懂的方式回答" therefore follow the same automatic extraction path as
the Runtime integration; a successful assistant reply is not treated as a
write unless the bridge completes the turn ingestion.

The acceptance report also covers automatic extraction without tool calls,
standing preference recall on unrelated queries, duplicate writes, invalid
targets, slow shutdown and visible worker startup failure. Replace retains
exact-record semantics and removal requires trusted confirmation; these are
not claims of full Runtime mutation parity.

## K3 Web Dashboard deployment example

`hermes-memory-v0217` is a K3-specific sample shell entry point, not a portable
installer. The Python launcher takes explicit source/state/SDK paths. Keep
Dashboard and CLI state directories separate. Do not commit provider keys,
the Hermes `.env`, account secrets, key packages or databases.

Install Hermes dependencies in its interpreter environment, including
`anthropic==0.87.0` for MiniMax China and `snowballstemmer==3.1.1` for memory
tools. Ensure the installed Bianbu SDK is importable by Dashboard child
processes too: installing the SDK into that interpreter is preferred; the K3
setup links `/usr/lib/python3/dist-packages/bianbu_agent_sdk` into its venv.
Configure the model with the same profile before launching Dashboard.

K3 cannot build the frontend with the tested toolchain (WASM SIMD unavailable).
Build the web and TUI assets from the same pinned Hermes tag on a supported
build host using Hermes's frontend build instructions. Deploy the generated
`hermes_cli/web_dist` and `ui-tui/dist` into the pinned K3 checkout; keep build
trees and node_modules outside this repository. Use `--skip-build` only after
both assets have been deployed.

Example user service `~/.config/systemd/user/hermes-memory-dashboard.service`:

```ini
[Unit]
Description=Hermes v0.21.7 Bianbu memory Web Dashboard
[Service]
Type=simple
UMask=0077
Environment=HERMES_NODE=/usr/bin/node
Environment=HERMES_TUI_DIR=/home/example/projects/hermes-agent-memory-v0.21.7/ui-tui
Environment=HERMES_PYTHON=/home/example/projects/hermes-agent-base/.venv/bin/python
Environment=HERMES_PYTHON_SRC_ROOT=/home/example/projects/hermes-agent-memory-v0.21.7
ExecStart=/home/example/projects/hermes-agent-base/.venv/bin/python /home/example/mem-gateway-build/scripts/hermes_memory_dev.py --hermes-source /home/example/projects/hermes-agent-memory-v0.21.7 --state-dir /home/example/.local/state/hermes-memory-v0217-dashboard -- dashboard --host 127.0.0.1 --port 9119 --no-open --skip-build --isolated
Restart=on-failure
RestartSec=5
KillMode=control-group
TimeoutStopSec=30
[Install]
WantedBy=default.target
```

Run `systemctl --user daemon-reload` and
`systemctl --user enable --now hermes-memory-dashboard.service`.
Open `http://127.0.0.1:9119/chat` in K3 Chromium. Keep it bound to loopback;
use an SSH tunnel for remote access. In chat, ask "请记住以后说话要通俗易懂",
finish the turn, then start a new session and ask about an unrelated concept.
Verify the saved preference in backend records as well as the reply; an LLM's
"saved" statement alone does not prove persistence. Automated acceptance uses
temporary data and makes zero model requests; it does not verify API billing,
provider credentials, semantic embeddings or production Runtime registration.
