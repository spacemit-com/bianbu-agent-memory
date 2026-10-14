# Agent Memory 配置说明

## 1. 配置所有权

Memory 的产品配置由 memoryd 管理，默认文件为：

```text
/var/lib/bianbu-agent-memory/memory.config.json
```

文件名由数据库路径 `memory.db` 自动推导。memoryd 首次打开数据库时创建该文件，
以 `0600` 权限保存，并通过临时文件、`fsync` 和原子 `rename` 更新。Runtime、BiBit
和 Agent 不得直接编辑该文件或 SQLite。

配置接口：

```text
privacy.settings.get
privacy.settings.update
```

更新接口是部分更新；未提供的字段保持原值。memoryd 负责最终校验并将变更写入
审计记录。

## 2. 配置项作用域

| 配置项（API 名称） | 默认值 | 作用域 | Agent 是否可直接配置 | 说明 |
| --- | --- | --- | --- | --- |
| `memory_enabled` | `true` | 账号/Agent/空间覆盖，产品文件提供默认值 | 受信集成方可请求 | 关闭后 memoryd 拒绝 L1-L3 数据操作 |
| `retention_days` | `0`（永久） | 账号/Agent/空间覆盖，产品文件提供默认值 | 受信集成方可请求 | 仅允许 `0/7/30/90` |
| `audit_enabled` | `true` | 账号/Agent/空间覆盖，产品文件提供默认值 | 受信集成方可请求 | 关闭也不能绕过强制审计动作 |
| `backup_enabled` | `false` | 账号/Agent/空间覆盖；文件字段为 `cloud_backup_enabled` | 受信集成方可请求 | `default`/`local:*` 账号不能开启 |
| `backup_frequency` | `weekly` | 账号/Agent/空间覆盖 | 受信集成方可请求 | `manual/daily/weekly/monthly` |
| `backup_scopes` | 四类（`preferences`/`development_habits`/`schedule_habits`/`important`） | 账号/Agent/空间覆盖 | 受信集成方可请求 | 仅表示范围，不等于已授权上传；不含对话摘要（MEM-L4-02）；`important` 承载用户显式标记为重要的 L3 记录（保留标签 `important`，非 `slot:`），见 §6.1；存量值在读取/写回/定时备份时自动收敛 |
| `sensitive_action_confirmation` | `standard` | 产品级 | 只能经受控管理接口 | 允许 `standard/strict`；命令确认由 Runtime/Security 管理 |
| `preference_auto_apply_enabled` | `true` | 产品级 | 只能经受控管理接口 | Runtime 是否自动应用偏好 |
| `development_habit_auto_apply_enabled` | `true` | 产品级 | 只能经受控管理接口 | Runtime 是否自动应用开发习惯 |
| `recent_turn_limit` | `5` | 产品级 | 只能经受控管理接口 | `1..50` |
| `recent_session_limit` | `5` | 产品级 | 只能经受控管理接口 | `1..20` |
| `summary_interval_turns` | `5` | 产品级 | 只能经受控管理接口 | `1..50`，完整轮次触发摘要 |
| `summary_retention_days` | `7` | 产品级 | 只能经受控管理接口 | `1..365` |
| `timezone` | `Asia/Shanghai` | 产品级 | 只能经受控管理接口 | UTC 或 IANA 时区名 |
| `local_device_id` | `k3-development` | 产品级 | 只能经受控管理接口 | 设备备份元数据，不是 Runtime 身份 |
| `summary_model` | disabled | 产品级 | 只能经受控管理接口 | 见下节 |
| `max_database_bytes` | `536870912` | 产品级本地资源上限 | 只能经受控管理接口 | SQLite 主库及 WAL/SHM 的总字节上限，`16 MiB..8 GiB` |
| `max_backup_bytes` | `536870912` | 产品级本地资源上限 | 只能经受控管理接口 | 本地 Capsule 目录上限，`16 MiB..8 GiB`；远端 Adapter 仍由服务端配额负责 |
| `min_free_bytes` | `67108864` | 产品级本地资源上限 | 只能经受控管理接口 | 数据写入前必须保留的文件系统可用空间，`0..8 GiB` |

账号/Agent/空间覆盖项实际存储在 SQLite 的 `memory_settings` 表中，主键为：

```text
(account_id, agent_id, memory_space_id)
```

`retention_days=0` 在覆盖表中以 `NULL` 表示永久保留。产品文件中的值是未创建覆盖
记录时的默认值。当前 `summary_model`、自动应用开关、最近轮数、时区等属于产品级
配置，不按 Agent 单独保存。

## 3. Agent 身份和进程开关

以下值不是隐私配置，不能由普通 Agent 的请求体设置：

```text
account_id
agent_id
agent_name
agent_instance_id
memory_space_id
```

它们由 Runtime registry、systemd 环境和 `system.hello` 握手提供。memoryd 会校验
UID/GID/PID、进程启动时间、注册文件权限和 Capability，防止 Agent 冒充其他账号或
Agent。

另有一个 Runtime 进程级开关：

```text
BIANBU_AGENT_MEMORY_ENABLED=true|false
```

它控制 Runtime 是否加载/调用外部 Memory 后端，默认启用。它与 memoryd 的持久化
`memory_enabled` 不同：前者关闭时 Runtime 不发起 SDK 请求，后者关闭时请求到达
memoryd 后返回 `MemoryDisabled`。

## 4. 摘要模型配置

`summary_model` 结构如下：

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

启用时必须提供可信的 `http://` `base_url` 和 `model`。鉴权模式只能是
`none`、`bearer_env` 或 `api_key_env`；`credential_env` 只保存环境变量名，不能
把 Token 写入 JSON。正式摘要模型失败时保留规则摘要和 L1 原始事件，并返回降级
原因，不得把失败报告为成功。

## 5. 语义检索 Embedding 配置

`embedding` 结构如下（`privacy.settings.update` 传入；**0.1.9 起默认开启**，
指向 spacemit-ai-gateway 的 Embed 域 `http://127.0.0.1:18790/v1/embed`，
gateway 或模型缺失时静默降级为纯词法检索；设置页开关或显式 `enabled:false`
段落可关闭）：

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

- `base_url` 只接受 `http://`（本机服务，gateway 的 Embed 域），启用时必填；
  `model` 是 **gateway 侧的模型 id**（不是自由标签），同时参与向量过期检测——
  更换模型或维度后，旧向量自动判 stale 并由后台任务（60 秒周期）按新配置重建，
  无需人工清理。把该字段钉在真实服务的模型 id 上，是为了让 gateway 侧换模型能
  被自动发现，而不是在两边维度都是 1024 时静默混用两个向量空间；
- **升级迁移**：0.1.9~0.1.16 会把当时的默认值
  （`http://127.0.0.1:18801/v1` + `qwen3-embedding-0.6b`）持久化进这一节，
  而这一节是显式配置、新默认值对它的设备不生效。0.1.17 起 `read_config` 会把
  **恰好等于这对旧默认值**的配置在读取时收敛到网关 endpoint 与网关模型 id
  （见 `converged_embedding`，与 `converged_backup_scopes` 同一套做法）：其余
  字段不动，任何自定义 endpoint 也不动。所以运维自托管的设备只需要保证
  `base_url` 或 `model` 与旧默认值不完全一致，就不会被迁移；
- **收敛值在启动时写回文件**：收敛只发生在内存里，而 `memory.config.json` 是运维
  在守护进程之外唯一能读到的副本，于是升级后的设备会出现“文件写着 `18801`、服务实际
  在打 gateway”的错位。`initialize_product_config` 在启动时（持有配置锁、不可能与任何
  请求竞争的时刻）把收敛结果落盘，失败只记日志、不阻塞启动。`system.health.embedding`
  把两半都报出来：`base_url`/`model` 是生效值，`config_file_matches=false` 表示磁盘
  副本还没写回（见 §9）；
- `dims` 64..=8192；`format` 支持 `int8`（默认，对称 int8 量化，余弦相似度对
  量化尺度不变）与 `f32`；`max_latency_ms` 100..=60000（查询路径实际取
  `min(max_latency_ms, 1500)`，为 2 秒检索预算保留词法阶段）；
- `batch_size` 1..=128，是**后台回填单次请求的条数上限**。2026-09-17 起它不再
  是唯一约束：单次请求还会按 token 预算截断（固定 2560 estimated token，见
  `max_text_tokens`），于是短文本按条数截断、长文本按 token 截断。K3 实测
  （`--parallel 1`、`-t 8`、Q8_0，文本唯一即无 prompt cache 命中）：200 字符
  条目 12 条 ≈ 7 s（3.2 ms/token），17/32 条分别是 7.9 s / 15.9 s（3.2~3.4
  ms/token）——每 token 成本几乎不变，把更多 token 塞进一次请求既不提高吞吐、
  又会拉长单次延迟并挤占在线嵌入，所以 token 上限取保守值；
- `index_timeout_ms` 100..=600000，是**索引路径**（后台批量回填与提交后单条
  嵌入）的每请求读写超时，与检索预算解耦。K3 实测（唯一文本、无缓存命中）：
  1291 prompt token 单条 6.5 s、1843 10.3 s、2211 13.5 s、2571 18.2 s ——
  **prefill 成本是超线性的**（约 5.0 → 7.1 ms/token）。默认从 10000 提到
  **30000**，依据是「最坏单次请求 + 可能排在它后面的提交后单条嵌入」这一组合：
  当前参数下为 10.3 s + 10.3 s ≈ 20.6 s，占预算 69%；沿用交互预算 1500ms 会让
  大批量空间进入永久 ServerUnavailable 重试循环（2026-09-12 生产事故根因）。
  旧配置文件缺此字段时按默认 30000 反序列化；
- `inline_timeout_ms` 100..=2000，是**提交后单条内联嵌入**的读写超时（默认
  **500**）。自 2026-09-24 起，提交后单条内联嵌入不再使用 `index_timeout_ms`，
  改由本字段约束（`index_timeout_ms` 只约束后台批量回填）：写事务在此之前已经
  提交，让写响应一直等到 30 s 索引预算只会制造"客户端已超时、库里其实有这条"
  的假失败（MCP 工具预算 2 s）。超预算时该行交给后台回填补齐，journal 记一行
  `[semantic-embed] embedding failed for memory ... (inline budget 500ms,
  backfill will retry)`。老配置文件缺失此字段时按默认 500 反序列化。
- `max_text_tokens` 64..=8192，是单条文本送入 embedding 服务前的 **token 预算**
  （默认 **2560**，约等于 1843 个真实 token）。服务端窗口（provisioner 为该模型
  钉住的 `--ctx-size=4096`）是硬上限，超出的**单条**请求直接被 400
  （`exceed_context_size_error`）拒绝而不会自动截断（实测 2048 窗口下 2560
  字符即 1843 token 通过、3072 字符即 2211 token 被拒），因此裁剪必须发生在
  客户端。超预算的文本按「保留头部 + 保留尾部、中间以 `[...]` 省略」处理
  （尾部占 25%），并在 journal 打一行 `[semantic-embed] clipped text` 警告；
  旧配置文件缺此字段时按默认 2560 反序列化。**默认值不是按窗口比例取的**：
  窗口翻倍到 4096 后按比例应取 3584，但实测单条 2571 token 要 18.2 s，
  与「排在后面的单条嵌入」组合会到 36 s，超出 30 s 超时，所以停在 2560；
  若要取 3584 必须同时把 `index_timeout_ms` 提到 ≥ 45000。调整该值前必须先
  确认服务的 `--ctx-size`：取值接近或超过窗口会让长记忆重新落到
  「400 → 永远没有向量」的失败路径。单次批量请求的 token 上限是**固定常量**
  `EMBEDDING_BATCH_TEXT_TOKENS = 2560`（不随本字段放大，避免调大本值后单批耗时
  悄悄翻倍），且至少保证一条文本单独成批。实测 2 条旧预算文本（2×1291 token）
  = 2582 token ≈ 12.7 s、3 条 = 3873 token ≈ 19.0 s 都成功返回——单槽服务会把
  超过 `--ctx-size` 的**批量**逐条送进上下文，不会因此返回 400，所以上限是
  **延迟**约束而非窗口约束；
- `query_instruct` 只加在查询侧（Qwen3-Embedding 惯例），记忆写入侧不加前缀；
  前缀 A/B 正式评估结论见 `docs/K3_EMBEDDING_BENCHMARK.md`；
- 行为：put/revise 事务提交后 best-effort 单条嵌入（失败在 journal 记一行
  `[semantic-embed] embedding failed for memory ...`，由后台补齐，检索本身仍
  静默回退）；
  `sensitivity=secret` 永不嵌入（与 FTS 同策略）；检索在 `long_term.search_headers`
  内做向量召回 + RRF(k=60) 融合，embedding 禁用、失败或超时一律静默回退到
  FTS 级联（MEM-NFR-02 降级要求）；
- 向量属于派生数据：不进入备份胶囊，restore 后自动重建；`forget`、`clear_all`
  等删除路径同步清理；
- 打包部署：Memory 不再自带推理 unit 与模型下载脚本，模型的下载、加载、检索
  全部由 spacemit-ai-gateway 的 Embed 域承担（`/v1/embed/models`、
  `/models/{id}/download`、`/models/load`、`/embeddings`）。装包只会 enable
  并后台触发一次 `bianbu-agent-memory-embedding-provision.service`；失败只记
  日志，FTS5 降级不受影响：

```bash
# 自动路径：装包即后台 provision（下载 → 加载 → 金样自检）。gateway 不在时
# 记一行日志并以 0 退出，不阻塞、不报错。
# 手动补齐（首次下载失败、gateway 后装、或 gateway 重启后模型被卸载）：
sudo systemctl restart bianbu-agent-memory-embedding-provision.service
# 只查看状态、不做任何变更：
sudo /usr/libexec/bianbu-agent-memory-embedding-provision --check
# 0.1.9 起语义索引默认开启：无需再改 embedding.enabled。关闭走设置页
# 开关（或 settings.update 发整段 enabled:false）；另有
# AGENT_MEMORY_EMBEDDING_DISABLED=1 环境变量可只压制"开箱默认"
# （显式配置段落不受影响，供运维/密闭测试用）。
```

provisioner 用**显式 `extra_args`** 加载该模型（`--pooling last
--embd-normalize 2 --ctx-size 4096 --parallel 1`），而不是继承 gateway 的
`embed.default_args`；加载后还会拿打包进 deb 的金样向量
`/usr/share/bianbu-agent-memoryd/embedding_parity_golden.json` 自检
（余弦 ≥ 0.999），低于阈值即判失败。

> **为什么不能依赖 gateway 的 embed 默认参数**：`embed.backend` 与
> `embed.default_args` 是 embed 域**全局**项，Agent OS 意图模块的
> `bibit-embed`（Q4_0）与 Memory 共用该域，而
> `/opt/bibit-intent/deploy/configure-gateway-embedding.py` 会在部署时改写
> `embed.default_args`。若 Memory 的模型继承这些默认值，一次 bibit 重新部署就
> 可能让它在维度不变（1024）的前提下换到另一个向量空间，memoryd 无从察觉。
> 因此 Memory 既不读也不写这两项，也不调用 `/models/switch`（避免抢走 bibit 的
> `_current_model` 指针）。详见 `docs/GATEWAY_EMBEDDING_INTEGRATION.md`。
>
> **平台约束（K3 实测）**：推理进程由 gateway 拉起
> （`spacemit-ai-gateway.service` 以交互用户 `bianbu` 运行，其 postinst 继承
> 安装者身份）——厂商 SPERT NPU 运行时通过 `/dev/shm/spert-arbiter-*`
> （0600，首个 NPU 进程创建）仲裁 NPU 访问，**拒绝属主 uid 之外的一切进程**
> （含 root）。与 bibit/Hermes 栈同 uid 才能共享 NPU；多 uid 共享需平台升级
> libspert。详见 `docs/K3_EMBEDDING_BENCHMARK.md` §7.5。
>
> **升级残留**：从自带推理 unit 的版本升级后，
> `/var/lib/bianbu-agent-memory/models/` 与 `/etc/bianbu-agent-memory/embedding.env`
> 不再被任何组件读取，包**不会**自动删除它们；确认 gateway 侧模型可用后可手工清理。

## 6. 云备份相关配置

云备份 API-Key 不放在 `memory.config.json`。服务环境文件示例：

```text
/etc/bianbu-agent-memory/backup.env
BIANBU_MEMORY_BACKUP_BASE_URL=https://memory.example.invalid
BIANBU_MEMORY_BACKUP_API_KEY_FILE=/etc/bianbu-agent-memory/backup-api-key
```

API-Key 文件应由 root 拥有、权限 `0640`，仅允许 memory 服务组读取。开启
`backup_enabled` 还必须同时满足：正式认证账号、Capsule 密钥、Backup Adapter、
用户授权范围和有效的云端 API-Key。

### 6.1 用户显式标记的“重要标记”

`important` 是保留标签，不是 `slot:`：`validate_tags` 限制每条记录最多一个槽位，用槽位会与记录自己的字段槽位冲突。标记与记录的内容类型正交，因此单独占一个备份范围：`preferences` 范围只匹配`kind == "preference"`，一条被标记的重要 `fact` 挂在那里会被静默漏掉。

- 只由显式动作产生，**不从字面推断**：标记决定记录能否离开设备，模型不得代用户决定；
- 标记/取消标记是一次普通 `long_term.revise`，复用既有 revision 与 Capsule 路径，`tags` 本来就在 Capsule 里，因此不引入新的端云格式；
- 入口：CLI `mark-important`/`unmark-important`、SDK `long_term.mark_important`、MCP 工具 `action="mark_important"`；
- 标记本身不上传任何东西：真正授权上传的是 `backup_scopes` 里的 `important`；
- 升级时只对“仍是出厂默认值”的设备追加该范围（`migrations/0013_add_important_backup_scope.sql`），用户自定义过的范围列表不动。
- 召回结果在 `tags` 里回显该标记（非空时才输出）：模型和用户据此判断哪些记忆被标记为重要，不必从正文推断；

## 7. 通过 Runtime 设置

受信集成方应调用 Runtime Plugin Console：

```text
POST /api/v1/plugin-consoles/bianbu.memory/actions/settings.update
```

例如：

```json
{
  "memory_enabled": true,
  "retention_days": 7,
  "audit_enabled": true,
  "preference_auto_apply_enabled": true,
  "development_habit_auto_apply_enabled": false,
  "recent_turn_limit": 5,
  "recent_session_limit": 5,
  "summary_interval_turns": 5,
  "summary_retention_days": 7,
  "sensitive_action_confirmation": "strict"
}
```

Runtime 读取自己的受信身份并调用 `client.privacy.update_settings(...)`；请求体不
应传入账号或 Agent 身份字段。`summary_model` 与 `embedding` 两个结构化配置也通过
同一入口整体传入（缺失字段按默认值补齐，因此每次都应发送完整结构）。当前工程
没有把配置修改暴露给大模型 Tool，避免模型
在没有用户确认的情况下改变记忆、保留期限、审计或云备份策略。

## 8. 需求清单中的非 Memory 配置

需求清单“5.1 记忆相关隐私配置”中，以下项目应标注为关联能力：

- 命令执行预览：Runtime/Security 的命令授权和确认策略；
- 社区知识库、本地文件全盘检索、向量/rerank：BiBit/Agent OS 检索链路；
- 端云模型 Fallback 和网络白名单：Runtime/Security/模型路由；
- API-Key 的 TEE/Secret Broker 管理：安全模块和部署系统。

Memory 只接收已经由 Runtime 解析并授权的数据范围，不负责这些能力的 UI、模型
路由或命令执行策略。

## 9. 运行诊断（health 完整性检查）

`system.health` 的 `status` / `database` 来自一次独占的 `PRAGMA quick_check`。整库
检查在 K3 现网库上约 406 ms，且该库是 SQLCipher：**索引完好时它也会报**
`malformed inverted index for FTS5 table main.long_term_fts[2]`，实测在写入高峰后
仍会持续复现约 100 s（连续 15 次取样）随后自行消失，之后不再出现，检索全程正确。
因此 0.1.18 起，health 只在"安静、WAL 为空、与写者互斥"时取样，并且**那条判词
不再算判词**：它以 `database_check.complaint` 原文呈现，不参与 `status`。两个环境
变量（均为 memoryd 进程级）：

```text
AGENT_MEMORY_HEALTH_SETTLE_MS=3000
AGENT_MEMORY_HEALTH_CHECK_INTERVAL_MS=30000
```

| 变量 | 缺省 | 含义 |
| --- | --- | --- |
| `AGENT_MEMORY_HEALTH_SETTLE_MS` | `3000` | 距最近一次写入（含后台批处理）不足这个时间就不取样，报 `busy`/`recent-activity`。`0` = 不等待（诊断用） |
| `AGENT_MEMORY_HEALTH_CHECK_INTERVAL_MS` | `30000` | 上一次取样（含在案的投诉）被复用多久。`0` = 每次 `system.health` 都重新做一次独占检查（会更频繁地占用写锁） |

响应字段：

| 字段 | 取值 | 含义 |
| --- | --- | --- |
| `status` | `ok` / `degraded` / `unknown` | 只由最近一次**已发布**的判词推导；FTS5 那条判词、写者占库都不改变它 |
| `database` | `ok` / 缺陷描述 / `busy` / `unknown` | 判词本体；`busy` = 本次没测到（写者占库或刚写过），`unknown` = 还没有取过样 |
| `database_check.state` | `checked` / `cached` / `busy` / `complaint` | 本次判词的来源：刚取样 / 复用缓存 / 没测到 / 有在案的投诉 |
| `database_check.reason` | `null` / `recent-activity` / `database-busy` / `fts5-inverted-index` | 为什么是现在这个 `state` |
| `database_check.verdict` | `ok` / 缺陷描述 / `null` | 已发布的判词 |
| `database_check.complaint` | `null` / 缺陷描述 | 不当作判词的投诉原文（目前只有 FTS5 inverted-index 那一类） |
| `database_check.checked_at_ms` · `age_ms` · `interval_ms` · `attempts` | 数值 | 判词时间戳、年龄、缓存窗口、本次独占检查尝试次数 |
| `embedding` | 对象 | 语义检索**当前生效**的配置：`enabled` / `base_url` / `model` / `dims`，以及 `config_file_matches`（磁盘上的 `memory.config.json` 是否已与本进程一致，见 §5）|

`degraded` 只在"`quick_check` 报了不是 FTS5 inverted-index 的缺陷"时出现。监控规则
请针对 `degraded` 触发，不要用 `status != "ok"`（`unknown` 是合法取值：可能是当前有
写在并发、本次没测到，也可能是还没取过样）。若想观察那条瞬时判词而不告警，看
`database_check.state == "complaint"` 与 `database_check.complaint`。细节与实测见
`docs/GATEWAY_EMBEDDING_INTEGRATION.md` §8.2。

`health.embedding` 存在的理由是“磁盘副本与生效值错位”：升级到 0.1.17 的设备上，文件里
可能仍写着 0.1.16 的 `18801` endpoint，而服务已经在打 gateway。`config_file_matches`
为 `false` 说明这一份文件还没被写回（多为只读目录或外部改过文件），排查时以
`base_url`/`model` 为准。

另一条排查入口是只读全库清册 —— 协议里每个方法只作用于调用者自己的 memory space，
所以“某个退役测试空间里还剩什么”只能由守护进程自己回答：

```bash
sudo /usr/libexec/bianbu-agent-memoryd --report-namespaces \
  --database /var/lib/bianbu-agent-memory/memory.db \
  --key-package /var/lib/bianbu-agent-memory/memory.keypkg \
  --account-secret-file /etc/bianbu-agent-memory/account-secret
```

输出每个 namespace 的逐表行数与正在使用的 `slot:` 标签（不含记忆正文）。它以
`SQLITE_OPEN_READ_ONLY` 打开库，不迁移、不改写、不加写锁，因此可以在服务运行期间执行；
覆盖脚本为 `scripts/k3_namespace_inventory.py`（清册 + 槽位合规）与
`scripts/k3_memory_namespace_cleanup.py`（按同一判据收口），用法与现场结果见
`docs/TEST_CASES.md` §4.3。
