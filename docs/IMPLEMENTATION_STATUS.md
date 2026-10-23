# Requirements implementation status

Status date: 2026-08-25. “Implemented” means source plus automated K3 verification; it does not imply product security review or seven-day stability qualification.

| Requirement | Status | Evidence / boundary |
|---|---|---|
| AMS-01 multi-Agent namespace | Implemented in M0 core | Server-bound identity; SQL account/agent/space predicates; cross-Agent and forged-field tests |
| AMS-02 OIDC account | External contract blocked | `account.status` works locally; mutations explicitly report missing OIDC/Secret Broker adapter |
| AMS-03 instruction memory | Implemented | Scope, priority, source, revision, sync policy, expiry and immutable revision payload |
| AMS-04 short-term memory | Implemented | Append-only events, sequence, persistent L1 context, configurable recent-five cross-session restore, Summary patch/revision, Checkpoint ref; schema v8 migrations are covered by Rust tests |
| AMS-05 long-term memory | Implemented D+ Phase 1 | Seven kinds, evidence, FTS5/get-many/expected revision plus official Mem0/Qdrant semantic projection; semantic hits resolve through persistent ID mapping and canonical get-many; FTS5 remains fallback; profile/PII records are excluded from projection |
| AMS-06 compression | Implemented for rules/structured mode | Server creates an L2 Summary at the configured interval (default five turns), preserves raw events, archives expired summaries to L3; OpenAI-compatible model/resource fields and explicit model-unavailable degradation are retained |
| AMS-07 device sync | Deferred after local boundary | Transactional outbox/inbox/cursor/conflict schema and HLC exist; `default/local:*` never enqueue or request sync. Cloud exchange and two-device test wait for the second device, account credentials, Backup Adapter and token |
| AMS-08 encrypted backup | Retired 2026-10-16 | Cloud backup was removed from this branch per the product change: export/import replaces it. The implementation lives on `feature/mem-l4-cloud-backup` and must not return to main before cloud integration. Schema v12 keeps only pre-existing tables; `backup.*` methods are gone and `privacy.settings.get` no longer reports backup capability. The reserved `important` tag and `mark-important` entry remain (L3 feature, independent of backup) |
| AMS-09 Forget | Local state implemented | Tombstone, immediate hiding, outbox, audit and local receipt; D+ `/v1/prune` physically removes orphan projections; cloud/device/backup acknowledgements remain pending |
| AMS-10 audit | Local export implemented | Runtime run/tool/error hooks write local JSONL and AgentMemory metadata spool; `audit.status/list/export` verified; external SEC-06 exporter remains blocked |
| AMS-11 Checkpoint | Implemented for memory state | Marker, event sequence, Summary revision, pending jobs, outbox watermark, retention of 10 and restore API; complete Agent OS process/work-progress restoration remains Runtime-owned |
| AMS-12 K3 resources | Current L1-L3 baseline passed | On 2026-08-25 Rust workspace (37 tests including plaintext-backup cleanup and concurrent-write serialization), direct `rustfmt --check`, and Python SDK OAuth2/Runtime tests (27) passed. A K3 release smoke with 1,000 records and 8 concurrent writers completed with 0 write errors and SIGKILL recovery; query P95 was 180.205 ms, so the formal 100k/32-client performance and seven-day qualification remain open |

## D+ Phase 1 implementation

The authoritative store remains AgentMemory SQLite. Schema v6 includes durable semantic projection jobs, provider-to-canonical mappings, local management, and a privacy migration that upgrades legacy profile rows to `sensitive` and marks unsafe mappings for deletion. A Python sidecar calls the official `mem0ai 2.0.17` public `Memory` API with `infer=False`, stores embeddings in a rebuildable local Qdrant projection, and exposes bounded semantic recall to Runtime. Each result is resolved back to the current canonical revision before its text is returned. Pending/retry/dead job states, lease ownership, idempotent completion, rebuild, superseded mappings and FTS5/Scheme-A fallback are implemented.

Current extraction mode is deliberately `BIANBU_MEMORY_DPLUS_INFER_MODE=rules`. The explicit ontology maps both terminal color and system preference color to `appearance.terminal_color_scheme`, so a new value supersedes the old one across sessions. Official Mem0 `infer=True` is not qualified: the tested 0.6B model was semantically wrong and took about 116 seconds on its first turn; the 30B-A3B model used about 23.9 GiB RSS and returned no usable result after about 442 seconds. Do not report official Mem0 intelligent extraction as implemented.

K3 Runtime regression passed for light-to-dark preference replacement, two synonymous queries, canonical convergence, one active semantic mapping, projection rebuild, Embedding-outage fallback, cross-session recall, and restart recall. Profile records are retained only in the encrypted canonical store and are absent from D+. Scheme A is preserved at `/home/example/backups/agent-memory/scheme-a-20260811-before-dplus`.

## Transaction invariant

Every canonical M0 write uses `BEGIN IMMEDIATE` and commits these in one SQLite transaction:

```text
idempotency check
  -> persistent HLC tick
  -> canonical current record + immutable revision/event/Tombstone
  -> outbox reference only for authenticated, upload-eligible accounts
  -> metadata-only audit spool
  -> idempotent response
  -> COMMIT
```

Outbox payload bytes remain empty with `status=awaiting_encryption` until a real key provider exists. The daemon does not duplicate plaintext under a column named ciphertext or claim encrypted cloud readiness.

Semantic projection is not part of this cross-store transaction. The canonical turn and durable projection job commit first; the D+ worker then calls Mem0/Qdrant and records the resulting mapping. Runtime never returns the provider body directly: it resolves the mapping and reads the current AgentMemory record, while projection failure leaves canonical writes available through FTS5/Scheme A.

## Remaining M0 hardening

The present code is an engineering baseline, not a production release. Cloud backup was retired on 2026-10-16 (product change: local export/import replaces it) and its implementation is parked on `feature/mem-l4-cloud-backup`; this branch serves no Capsule or upload path. Before image integration, the platform teams also need to qualify a Mem0 `infer=True` model or formally retain rules mode, freeze the signed Runtime registry and SEC-06 exporter, and rerun the K3 acceptance for the shared Embedding path. That sharing question is settled in favour of the platform gateway: memoryd consumes the spacemit-ai-gateway Embed domain and this package no longer ships an inference unit or a model downloader, while the intent module's bibit-embed entry stays untouched (docs/GATEWAY_EMBEDDING_INTEGRATION.md). Local quota enforcement is now implemented: SQLite/WAL/SHM size and a minimum filesystem reserve are checked before writes, and health/stats/maintenance expose the measured values. The former Capsule directory is no longer part of the quota. Plaintext migration now validates regular files, cleans interrupted encrypted staging/sidecars, fsyncs the directory and installs the encrypted database with mode 0600; rollback backups remain explicit until the operator removes them. Additional qualification still includes abrupt-power tests, 32-client and 100k-record stress, systemd service-account/ACL integration, SBOM/CVE review and seven-day K3 stability. The daemon now serializes local write transactions in-process; this removes the observed `database writer is busy` write failure mode, but does not by itself satisfy the latency target under concurrent reads.

## 非 Memory 配置边界

需求清单“5.1 记忆相关隐私配置”中，以下项目应标注为关联能力：

- 命令执行预览：Runtime/Security 的命令授权和确认策略；
- 社区知识库、本地文件全盘检索、向量/rerank：BiBit/Agent OS 检索链路；
- 端云模型 Fallback 和网络白名单：Runtime/Security/模型路由；
- API-Key 的 TEE/Secret Broker 管理：安全模块和部署系统。

Memory 只接收已经由 Runtime 解析并授权的数据范围，不负责这些能力的 UI、模型
路由或命令执行策略。
