# K3 检索尾延迟与语义降级归因（2026-10-23）

## 结论

本次检索尾延迟主要来自**返回前的同步审计写入竞争**，不是 `get_many` 的数据库读取，也不能笼统归为 IPC 底噪。

语义降级的直接触发点是 **Memory 的查询 Embedding HTTP 客户端等待超时**。诊断日志确认 `stage=embed`，约 1506～1531ms 后返回 `embedding request timed out`；该请求尚未执行向量扫描。不能仅据此断言是 Gateway 内部排队还是模型/CPU 调度导致了响应延迟。

## 环境和口径

- 设备：192.0.2.11；已安装 SDK/memoryd 0.1.30、Runtime 0.1.200、Gateway 0.0.19。
- Runtime、Gateway、意图模型保持运行；经用户授权，bibit-filesearch 暂停。
- 不以 `load1 < 3` 阻止集成测试，负载随证据记录。
- 同一个隔离数据库、独立 socket、development identity：10 万条记录、10 万条 1024 维合成存储向量；查询向量使用 Gateway 上的真实 Q8 模型。
- 合成存储向量只验证规模和耗时，不验证语义准确率。
- 检索：16 客户端 × 200 次，逐客户端排除前 5 次，每相 3120 个有效样本；统计 SDK 墙钟 `search_headers + get_many`。
- “冷相”仅重启 memoryd，未清除系统页缓存。原脚本的 hot 是第一进程相，并非所有不同查询均已预热。
- 候选二进制只运行在隔离栈，不覆盖生产安装包。构建在另一台 K3（192.0.2.10）完成，避免构建负载污染测试设备。

## 尾延迟证据

| 对照项，16 并发 | 开启审计 P95 | 仅隔离库临时关闭审计 P95 |
|---|---:|---:|
| 设置读取 | 105.538ms | 8.328ms |
| 缓存命中的检索 | 131.254ms | 13.446ms |
| 批量读取 `get_many` | 133.691ms | 17.014ms |
| 缓存检索 + 批量读取 | 317.450ms | 26.893ms |

关闭审计只用于归因，测试结束后已恢复；修复和正式复测均开启审计。

另用仅增加计时日志的基线构建复现：检索 + 读取 P95 374.916ms。服务端分段统计（包含原负载和预热后的分项对照）：

| 方法 | 业务处理 P95 | 返回前审计 P95 |
|---|---:|---:|
| `search_headers` | 0.101ms | 130.177ms |
| `get_many` | 3.104ms | 132.424ms |
| `privacy.settings.get` | 0.252ms | 107.340ms |

其中 761 次超过 100ms 的 `get_many`，审计耗时均占业务处理与审计合计耗时的 80% 以上。缓存命中占多数，故 `search_headers` 的业务 P95 很低；这不代表不同查询的真实语义扫描也只需 0.1ms。

### 原来的调用路径

```text
SDK search_headers / get_many
  → 身份、能力、记忆开关校验
  → 检索缓存 / 数据库读取
  → audit_access：各连接直接 INSERT audit_spool
      → SQLite 写锁竞争、busy_timeout 等待、FULL 模式同步提交
  → 返回 SDK
```

业务写入已使用 `Store.write_lock`，但读审计未加入这套写入准入机制。多连接争抢同一 SQLite writer，形成尾延迟；即使检索缓存命中，审计也无法跳过。

### 修复

`audit_access` 在取连接前获取与 `with_write` 共用的写入锁。等待的请求不占数据库连接；审计与业务写入不再彼此在 SQLite busy handler 中争抢。**不关闭审计、不改异步、不降低同步提交级别。**

增加并发回归测试：8 个读线程与业务写入并行，每次读取返回后，从独立连接检查对应 trace 的审计已存在且恰好一条。

## 语义降级证据与边界

```text
search_headers
  → semantic_stage
  → timeout = min(embedding.max_latency_ms, 1500)
  → EmbeddingClient：connect/read/write socket timeout
      → 等待 Gateway 响应超时
  → SemanticStage::Degraded
  → 使用关键词结果，且不缓存这次降级结果
  → 同步审计 → 返回 SDK
```

1500ms 是 Memory 客户端的 I/O 超时设置，不是整条检索的总截止时间，也不是向量扫描超时。连接、读写、词法处理、扫描、审计和排队仍需分别计时；源码旧注释所称“保证整体在 2 秒内”过于绝对。

基线语义归因：8 并发、3200 个不同查询，P95 709.263ms，零接口错误；24 次在 Embedding 阶段超时降级。单独做 800 次、8 并发对照：Gateway P95 437.703ms、直接模型端点 P95 306.442ms，均无超时。后者不能代表完整 Memory 压测，也不足以把偶发超时归责到某个 Gateway 内部组件。

本修复新增受 `SEMANTIC_CACHE_TRACE=1` 控制的分段计时和 Embedding 降级原因。日志不输出用户查询、记忆正文或响应正文。保留 1500ms 安全阈值，不通过放大超时掩盖问题。

## 修复后正式检索复测

| 相位 | 修复前 P95 | 修复后 P95 | ≤100ms |
|---|---:|---:|---|
| 首相 | 340.421ms | 74.061ms | 通过 |
| memoryd 重启后 | 435.439ms | 72.441ms | 通过 |

两相均为 3120 个有效样本、零接口错误。首次加载、语义不同查询和降级结果独立报告，不能把这一项通过扩展为全部性能需求通过。

语义专项复测（各相 8 并发 × 400 个不同查询）：`search_headers` P95 为 712.366/718.777ms，零接口错误；每相仍有 24 次 Embedding 超时降级。首次查询为 2593.546/2346.793ms，未解决首次加载超过 2 秒的问题。单客户端健康查询 3000 次 P95 0.401ms，仅作为 SDK 往返参考。

因此本补丁只收口“同步读审计放大尾延迟”，不宣称已修复 Embedding 偶发超时或首次加载。Gateway 内部耗时仍需后续按请求关联计时，不能通过延长 Memory 超时直接宣布通过。

验证：K3 `AGENT_MEMORY_EMBEDDING_DISABLED=1 cargo test --workspace --release --offline` 共 123 项全部通过（含新增并发审计持久性测试），`cargo fmt --all -- --check` 通过；候选构建运行 `k3_field_value_gate.py` 与 `k3_requirements_acceptance.py` 均通过。源码测试/功能验收在构建设备执行，正式性能复测在 192.0.2.11 执行。

正式复测使用既有 `scripts/k3_perf_acceptance.py`，保留已配置的真实 Gateway 语义路径：

```sh
python3 k3_perf_acceptance.py \
  --memoryd /home/example/memory-acceptance-20261023/memoryd-candidate \
  --root /home/example/memory-acceptance-20261023/corpus-100k \
  --skip-seed --records 100000 --clients 16 --ops 200 --warmup-ops 5 \
  --output candidate-retrieval.json
```

跳过播种时原脚本的 `write_ack_pass=true` 是默认值，不代表本次测量了写入性能。

## 原始证据

设备目录 `/home/example/memory-acceptance-20261023`，本地归档目录 `K3验收记录/20261023_192.0.2.11`：

- `retrieval-performance.json`、`formal-extra-performance.json`：最初正式测试。
- `attribution.json/.trace`、`attribution-no-audit.json/.trace`：审计开关对照。
- `diagnostic-baseline.json/.trace`、`diagnostic-baseline-stages.json`：业务与审计计时。
- `semantic-diagnostic-baseline.json/.trace`、`embed-attribution.json`：超时原因与端点对照。
- `candidate-retrieval.json`、`candidate-semantic-performance.json`、`candidate-system-load.log`：候选复测。

没有修改 Runtime/Gateway 源码或参数，没有修改发布版本号或 `debian/changelog`。

## 正式 Runtime 真实模型聊天与合入评估

在 192.0.2.11 临时将候选二进制部署到正式 memoryd 服务，通过已安装 Runtime 9899 API、新建专用测试会话执行 4 轮真实模型聊天：`hi`、短会话确认、临时代号追问、开发工具记忆查询。所有 run 完成，before_run/after_run 均完成；追问正确回答“青竹47”，业务查询实际调用 MCP `memory_retrieve`，返回 `success=true, code=no_match`。当前库无长期记忆，因此不以此宣称已覆盖有记忆的语义召回质量。只读库存报告确认会话事件为 16 条。

有效聊天轮次 hook 耗时：before_run 425.0/178.5/158.2/192.6ms，after_run 343.6/20.9/18.0/20.4ms。它们包含 hook 进程启动和上下文准备，不等同于前文单个 Memory 接口的延迟指标。聊天结束后恢复软件源二进制，SHA256 与备份一致，并重新启动 agent worker 绑定恢复后的 socket。

首次聊天尝试暴露部署边界：只重启 memoryd 时，Runtime worker 的 bwrap 文件绑定仍指向旧 socket（宿主 inode 794883，worker 内 745591），模型 MCP 返回 `backend_unavailable`；hook `completed` 不能证明记忆调用成功。通过 Runtime 的 agent restart API 重建 worker 后重测，严格断言无 `backend_unavailable`，上述四轮通过。记录原失败尝试，不能删去或算作通过。该现有 socket 生命周期问题需要部署顺序配合，不属于本次审计锁改动；本轮没有修改 Runtime 源码。

合入评估：审计锁修复可提交团队 main，保持为独立的性能修复提交。Rust 123 项、隔离功能验收、正式性能及真实模型聊天均有证据；语义超时与首次加载仍未解决，不阻止这个范围明确的修复，但不能据此宣布完整性能验收通过。团队 main fetch 后确认本补丁仅领先一个提交，无落后提交。工作区其他 README/接口文档修改不属于本轮提交，勿一并提交。

聊天证据：`real-chat-candidate-rebound/summary.json` 及各轮完整 history，原失败轮为 `real-chat-candidate/`；设备与本地验收目录均归档。

## 后续优化与验收口径复核

审计修复已经基于团队最新 main（含 `c46837e` 文档更新）迁移，并以 `9d9ff2c`、`e31d86c` 推送团队 main。未带入工作区其他文档修改，未改 changelog、未打 tag。

再次运行同一隔离语料、8 并发 × 400 个不同查询，增加逐请求时间与查询记录：3200 次请求零接口错误，search P50/P95/P99/max 为 619.537/727.031/1416.128/2838.383ms；24 次 Embedding 超时集中在两个时间窗口。Gateway 日志在对应窗口出现约 4 秒、约 1.7 秒的上游响应空档；模型 slot 日志显示相应空档位于前一任务结束与下一任务开始之间，未发现这两个空档由单次长推理占据。仍需进一步区分 Gateway 请求处理、转发和系统调度，不能直接认定模型推理慢或某一查询有问题。证据为 `semantic-query-attribution.json/.trace`、`semantic-query-gateway.log`、`semantic-query-model-q8.log`。

这次首次请求约 2556ms，其中缓存加载 2219ms、Embedding 88ms。缓存加载 SQL 包含 `ORDER BY memory_id`，而向量评分使用独立的分数/ID 确定性排序；下一步只验证去掉加载排序的效果，不减少扫描行数、候选数或返回内容。

验收口径纠正：已有语义性能验收使用 **8 并发、10 万条、search P95≤2秒**。候选版本两相 712.366/718.777ms 已满足这一延迟门槛；单个首次请求超过 2 秒不能独立推翻 P95 门槛。规格明确要求 Embedding 超时自动回退词法检索，并没有“所有请求均不降级”的量化门槛。24 次意外降级仍是待定位的质量风险，不能算作成功完成语义计算；也不能自行将零降级追加为产品性能要求。整体验收还应分别核对其他规格项及语义召回质量，不能只依据混合返回耗时判定。

优化以功能完整性为前提：保持本地权威读写、摘要、完整向量评分、权限/隐私过滤、同步审计与正常语义召回。不采用关闭语义、减少检索数据、缩短超时使正常请求提前降级，或降低审计持久化等级来满足延迟。当前证据不足以认定 P95≤2秒不合理，不调整产品指标；如后续发现功能与资源预算有不可兼容的约束，应分别报告稳定态 P95、启动准备耗时、降级比例和召回质量，再讨论指标边界。

### 去掉向量缓存加载排序：验证结果

缓存加载去掉 SQL `ORDER BY memory_id`，所有行仍加载，评分、候选上限、权限过滤和分数/ID 排序保持原逻辑。扩展评分测试，对反转与轮转后的行顺序、不同 top-k 验证完整排名/分数相同。Rust 123 项通过，两个隔离功能闸门分别返回 `K3_FIELD_VALUE_GATE_OK`、`K3_REQUIREMENTS_ACCEPTANCE_OK`。

在 192.0.2.11 交替启动原版/优化版，每版 3 次，保持同一查询、语料和后台服务。统一模型预热后，原版首次请求 2689/2617/2663ms，优化版 2250/2227/2247ms，约减少 15%；6 次前 10 条 ID 与顺序完全一致。最初未统一模型预热的试验有一次基线结果与后续 5 次不同，保留在 `cache-order-cold-probe.json`，该次不能用于证明跨版本结果一致；预热后的独立重试 `cache-order-cold-probe-warm-model.json` 才是上述一致性结论依据。真实模型推理仍可能存在数值差异，此处合成存储向量也不能替代语义质量集。

同数据、8 并发 × 400 次/客户端，两相正式语义负载复测：search P95 **633.860/644.475ms**，每相 3200 次请求、零接口错误，均满足 P95≤2秒；每相仍有 24 次 Embedding 超时降级，未修复。首次查询 **2224.684/1953.881ms**，改善但仍不能承诺每次首次请求≤2秒。不同 SQL 读取顺序也改变向量在缓存中的遍历次序，常规 P95 的改善包含运行波动，不能将全部差值归给加载排序。

证据：`cache-order-tests.log`、`cache-order-field.log`、`cache-order-requirements.log`、`cache-order-cold-probe-warm-model.json`、`cache-order-semantic-performance.json` 及 hot/cold trace，均归档到本地验收目录。优化版仅运行于隔离测试栈；未再次覆盖生产二进制、未改变 Gateway/18800/18802 参数，测试结束隔离进程已退出。生产 memoryd、Runtime、Gateway 均 active，filesearch 保持用户授权的停止状态。

以下补测完成前，新增加载优化的验证仅包含 Rust、隔离功能闸门、真实 Embedding 与规模负载；正式 Runtime 聊天结果以本节后续补测为准。

### 包含缓存优化的正式 Runtime 聊天补测

在 192.0.2.11 临时部署包含缓存优化的二进制（SHA256 `1fffdb90958413d7ccd4678092d7f3fa04c39404a527f8d92587638427bc7caa`），重建正式 Runtime agent worker 后，通过已安装 Runtime 的 9899 API 执行真实模型聊天。会话 `codex-formal-cache-order-20261023-9c9d35cb` 共 4 轮：`hi`、短会话确认、临时代号追问、开发工具记忆查询。4 轮均 `run/completed`，before_run/after_run 均 completed，没有 `backend_unavailable`；短会话回答“收到”、追问回答“青竹47”。业务轮实际 MCP `bianbu.memory/memory/memory_retrieve` 返回 `status=success`，内层 `success=true, code=no_match, count=0`，模型未虚构开发工具。

before_run 耗时为 405.6/196.9/156.2/154.8ms；after_run 为 345.6/21.8/21.0/28.9ms，包含 Hook 进程启动等开销，不等于 Memory 单接口延迟。该正式库本轮无长期记忆命中，MCP 实际查询为空字符串；本轮证明候选版本在正式 Runtime 上的调用兼容性和空结果行为，不代替有长期记忆的语义召回质量或 10 万条缓存加载验证，后两者分别受质量集和前述隔离测试边界约束。模型关于“未调用工具所以未写入记忆”的额外解释不作为事实依据，after_run 仍会写入会话事件。

测试后已恢复软件源二进制，SHA256 与备份一致（`516afdadbbe180ed429f866bc66afdbd66fa436dde2bd98b0d78007e00dcfa99`）。恢复后首次 agent restart 返回 HTTP 503，原始脚本因此非零退出；4 轮聊天结果与该清理失败分别记录，未将脚本整次执行标为成功。随后重试 restart 成功，memory registration active，`/api/health` 为 `AGENT_READY`，宿主与 worker socket inode 均为 `811786`，确认恢复完成。未修改 Runtime/Gateway 源码或模型参数。

证据已归档：设备验收目录及本地 `K3验收记录/20261023_192.0.2.11/real-chat-cache-order/` 的 summary 与逐轮 history，另保留 `real-chat-cache-order.log`（包括恢复阶段 503）和执行脚本。源码优化已在团队 main 的 `4d763b4`；本次补充回归证据后提交报告，不修改版本号或触发发布。
