# Mem0 D+ adaptation

The project pins official Mem0 OSS `mem0ai 2.0.17` (Apache-2.0) as a public-API Provider, not as the Bianbu canonical store and not as copied or forked source. Scheme A's reimplemented extraction/merge behavior is preserved as the offline and failure fallback.

## Current architecture

```text
Runtime turn
  -> AgentMemory Session Event / canonical long-term record
  -> durable schema-v3 projection job
  -> D+ sidecar
       -> official mem0.Memory.from_config()
       -> add(..., infer=False)
       -> Qwen3 Embedding
       -> local Qdrant derived projection
  -> provider_memory_id <-> memory_id + canonical_revision mapping

Runtime recall
  -> D+ semantic search
  -> mapping resolution
  -> AgentMemory canonical get-many + scope/revision/Forget checks
  -> FTS5 / Scheme A fallback on failure
```

SQLite revision, namespace, evidence, idempotency, audit, Tombstone, sync policy and backup state remain owned by `bianbu-agent-memoryd`. Qdrant can be deleted and rebuilt from canonical records. Provider text is never accepted directly as authoritative Runtime context.

The projection worker supports persistent pending/leased/retry/dead/completed jobs, lease ownership, provider-result persistence, idempotent completion, superseded mappings and rebuild. An automatic Mem0/Planner `DELETE` remains only a `memory_delete_proposed` Session Event; a trusted UI must obtain a valid confirm token before calling Forget.

## Extraction boundary

K3 currently sets:

```text
BIANBU_MEMORY_DPLUS_INFER_MODE=rules
```

Rules recognize only explicit preference/development-habit statements. A small product ontology normalizes both “终端配色” and “系统偏好颜色” to `appearance.terminal_color_scheme`, allowing a later explicit value to replace the earlier value across sessions. The resulting memory is then projected through official Mem0 with `infer=False` and recalled semantically through Qdrant.

Official Mem0 `infer=True` was tested but did not pass the K3 model gate:

- the 0.6B model took about 116 seconds on its first turn and incorrectly extracted a terminal-color preference as an outdoor activity;
- the 30B-A3B model used about 23.9 GiB RSS and produced no usable result after about 442 seconds.

Therefore D+ Phase 1 means **official Mem0 Provider + semantic projection + rules-compatible extraction**. It must not be described as official Mem0 intelligent extraction. Enabling `infer=True` requires a qualified model, fixed Chinese regression set, accuracy threshold, bounded first/warm latency and acceptable RSS.

## K3 verification

- Rust workspace: 16 non-doc tests passed, including schema v6 privacy migration, projection durability and canonical resolution.
- Python SDK: 12 tests passed.
- Runtime Memory Hook: 4 tests passed.
- Two-session Runtime regression: light preference updated to dark; both terminal-color and system-color questions returned dark; only one active semantic mapping remained.
- Embedding outage: job entered retry, canonical write remained available through Scheme A/FTS5, and projection could be rebuilt after recovery.
- D+ semantic query, 30 sequential calls: P50 `60.499 ms`, P95 `66.578 ms`, maximum `70.954 ms`.
- RSS: memoryd about `6.5–6.9 MiB`, sidecar `136,464 KiB`, Runtime `127,576 KiB`, Embedding `1,681,044 KiB`.

Scheme A backup: `/home/example/backups/agent-memory/scheme-a-20260811-before-dplus`.

Upstream source: <https://github.com/mem0ai/mem0/tree/v2.0.17>
