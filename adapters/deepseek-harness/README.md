# DeepSeek Harness adapter

Lifecycle extension: `agent/pre-step` records the run-start watermark and
`agent/session-start` clears pending confirmations. `session/event` records
aborted/failed `turn/end` events and persists `compaction/summary` at successful
`compaction/end`; `session/flush` waits for these writes. Compression stores a
summary/checkpoint, not a full snapshot of the host context.

`memory_search` now returns structured IDs, revision, source, tags and dates.
`memory_update` requires memory_id, target and expected_revision and normalizes
relative dates before validation; stale revisions are refused. Effective
Instruction Memory is fetched through the SDK on each step and injected as a
bounded, untrusted data block.

`memory_remove` returns a preview and a five-minute, session-bound confirmation
message. The user must send that exact message as a new user-origin prompt.
The model must display the message and wait, rather than call the tool again.
Repeated calls replace the old challenge and never delete the record.
Model booleans and plugin-origin messages cannot authorize deletion. Changed
record revisions invalidate approval. The private development daemon's token
is generated per process and never returned to the model; this is not a
production confirmation/identity deployment.

K3 regression additionally exercises structured recall, update conflicts,
confirmation and actual Forget, start/error lifecycle and compact boundaries.
Bridge errors carry type/code/message. Existing core gates remain passing.

This adapter targets DeepSeek Harness `0.1.0-rc.6` and connects its Cordis
agent lifecycle to Bianbu Memory. It uses `agent/pre-step` to recall memory and
injects it as explicitly untrusted context, then uses `agent/turn-stopping` to
send the user/assistant pair through `MemoryBridge.record_turn`. Plugin-origin
messages are excluded from writes and subagents are read-only.

The plugin also registers model-facing `memory_search` and `memory_save` tools
through the DSH tool registry. Explicit saves use Runtime `upsert_explicit`
validation and deduplication and return the actual success/failure result.
The usage prompt instructs the model to use these tools instead of workspace
files or direct database edits. Saving a dated note is not an alarm, and these
deletion requires the separate user-origin confirmation flow described above.

Relative days such as 明天 are resolved by the shared SDK at write time, using
`BIANBU_MEMORY_TIMEZONE` (Asia/Shanghai by default). Explicit saves and default
rule-based turn ingestion preserve each activity's clock while storing an
absolute date. Each model step receives current local time, so an unambiguous
relative date does not require an extra question. This does not schedule alarms.
The optional D+ asynchronous projection path is not covered by this date gate.

For private SDK deployment, set the plugin's `sdk` option to the directory
containing `bianbu_agent_sdk`; acceptance uses `BIANBU_MEMORY_SDK_PATH`.
K3 uses a private SDK snapshot without replacing the production SDK. All 12
automated checks passed, including tool saves and automatic turn extraction.
A live DeepSeek chat on 2026-10-13 saved and queried 明天 meeting/yoga notes as
2026-10-14 without requesting a date.

The K3 deployment runs the Node plugin with a small JSON-lines Python bridge.
The bridge owns an isolated development-identity memoryd and uses the installed
SDK. Production socket and the 18800 intent model are untouched. Configure the
bridge path and state directory in the DSH profile patch; never put API keys,
databases or key packages in this repository.

The web UI test on K3 completed a real DeepSeek turn and a new-session recall:
the recalled records contained `青禾839`, `C++ 程序员`, and `通俗易懂`. The
model correctly treated the block as untrusted and explained that it would not
assert those facts as verified identity; this is expected memory safety
behavior. The automated bridge gate covers turn pairing, automatic extraction,
cross-session recall, prompt-injection exclusion, duplicate turns and
subagent-write suppression without model requests.

Deletion regression covers repeated calls, replaced challenges, plugin-origin
confirmation rejection, expiry, changed revisions, and confirmed deletion.
These isolated checks use no model requests and do not delete user records.
