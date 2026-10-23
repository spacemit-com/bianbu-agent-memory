# Agent Memory 配置说明

[English](CONFIGURATION_EN.md) | [简体中文](CONFIGURATION.md)

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
| `memory_enabled` | `true` | 账号/Agent/空间覆盖，产品文件提供默认值 | 受信集成方可请求 | 关闭后 memoryd 拒绝 L1-L3 的写入与召回操作 |
| `retention_days` | `0`（永久） | 账号/Agent/空间覆盖，产品文件提供默认值 | 受信集成方可请求 | 仅允许 `0/7/30/90` |
| `audit_enabled` | `true` | 账号/Agent/空间覆盖，产品文件提供默认值 | 受信集成方可请求 | 关闭也不能绕过强制审计动作 |
| ~~`backup_enabled`~~ | — | 已退役 | — | 2026-10-16 起云备份下线；`privacy.settings.update` 仍接受旧字段但忽略，`memory.config.json` 中的 `cloud_backup_enabled` 读取时丢弃，不出现在响应里 |
| ~~`backup_frequency`~~ | — | 已退役 | — | 同上；旧字段可发送但被忽略，旧文件中的键在下一次写入后消失 |
| ~~`backup_scopes`~~ | — | 已退役 | — | 同上；`important` 仍是 L3 记录上的保留标签，与云备份无关 |
| `sensitive_action_confirmation` | `standard` | 产品级 | 只能经受控管理接口 | 允许 `loose/standard/strict`；命令确认由 Runtime/Security 管理 |
| `preference_auto_apply_enabled` | `true` | 产品级 | 只能经受控管理接口 | Runtime 是否自动应用偏好 |
| `development_habit_auto_apply_enabled` | `true` | 产品级 | 只能经受控管理接口 | Runtime 是否自动应用开发习惯 |
| `recent_turn_limit` | `5` | 产品级 | 只能经受控管理接口 | `1..50` |
| `recent_session_limit` | `5` | 产品级 | 只能经受控管理接口 | `1..20` |
| `summary_interval_turns` | `5` | 产品级 | 只能经受控管理接口 | `1..50`，每 N 个完整轮次触发摘要 |
| `summary_retention_days` | `7` | 产品级 | 只能经受控管理接口 | `1..365` |
| `timezone` | `Asia/Shanghai` | 产品级 | 只能经受控管理接口 | UTC 或 IANA 时区名 |
| `local_device_id` | `k3-development` | 产品级 | 只能经受控管理接口 | 本地设备标识元数据，不是 Runtime 身份 |
| `summary_model` | disabled | 产品级 | 只能经受控管理接口 | 见下节 |
| `max_database_bytes` | `536870912` | 产品级本地资源上限 | 只能经受控管理接口 | SQLite 主库及 WAL/SHM 的总字节上限，`16 MiB..8 GiB` |
| ~~`max_backup_bytes`~~ | — | 已退役 | — | 随云备份下线；旧字段可发送但被忽略，旧文件中的键在下一次写入后消失 |
| `min_free_bytes` | `67108864` | 产品级本地资源上限 | 只能经受控管理接口 | 数据写入前必须保留的文件系统可用空间，`0..8 GiB` |

退役字段只在同一请求还包含至少一个有效设置时才会被接受；只发送退役字段仍会
返回 `InvalidArgument: at least one setting is required`。

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

`embedding` 段通过 `privacy.settings.update` 整体传入，默认开启，指向
spacemit-ai-gateway 的 Embed 域；gateway 或模型不可用时自动降级为纯词法检索。
设置页开关或显式 `enabled: false` 可关闭。

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

| 字段 | 默认值 | 取值范围 | 说明 |
| --- | --- | --- | --- |
| `enabled` | `true` | 布尔值 | 是否启用语义检索；关闭后只走词法检索 |
| `base_url` | `http://127.0.0.1:18790/v1/embed` | `http://` 地址，最长 2048 字符 | Embedding 服务地址；启用时必填，默认指向本机网关 |
| `model` | `qwen3-embedding-0.6b-q8_0` | gateway 模型 id | 必须是 gateway 侧真实模型 id；更换模型或维度会使旧向量失效并触发重建 |
| `dims` | `1024` | `64..8192` | 向量维度，必须与模型输出一致 |
| `format` | `int8` | `int8` / `f32` | 向量存储格式；`int8` 为对称量化 |
| `batch_size` | `32` | `1..128` | 后台回填单次请求的条数上限；单次请求同时受固定 2560 token 预算约束 |
| `max_text_tokens` | `2560` | `64..8192` | 单条文本进入 Embedding 前的 token 预算；超预算时保留头尾并省略中间 |
| `max_latency_ms` | `1500` | `100..60000` | 查询侧延迟预算；实际取 `min(max_latency_ms, 1500)`，为词法阶段留出时间 |
| `index_timeout_ms` | `30000` | `100..600000` | 后台批量回填与索引路径的每请求超时 |
| `inline_timeout_ms` | `500` | `100..2000` | 写事务提交后单条内联嵌入的超时；超时后交给后台回填 |
| `query_instruct` | Qwen3-Embedding 推荐查询前缀 | 最长 512 字符 | 只加在查询侧，写入侧不加前缀；留空表示不添加 |

行为约定：

- `put` / `revise` 提交后做一次 best-effort 内联嵌入；失败只记日志，由后台补齐，
  检索自动回退到 FTS。
- 两路都有结果时使用向量召回 + RRF 融合；Embedding 关闭、失败或超时一律回退词法检索。
- `sensitivity=secret` 的记忆永不嵌入。
- 向量是派生数据：restore 后自动重建，`forget` / `clear_all` 时同步清理。
- 模型的下载与加载由 spacemit-ai-gateway 负责；本节只描述 memoryd 侧的连接参数。

## 6. 已退役配置与兼容项

2026-10-16 产品变更：记忆数据云备份下线，改为本地导出/导入。本包不再读取
`backup.env`，并已从 CLI 移除 `--backup-directory`、`--capsule-key-file`、
`--backup-base-url`、`--backup-api-key-*` 等旧参数；安装包提供的 unit 也不再包含
它们。本包同样不再创建 `/var/lib/bianbu-agent-memory/capsules` 和
`/etc/bianbu-agent-memory/capsule-key`。如果系统中残留旧版 unit 或 drop-in 仍带这
些参数，memoryd 会因未知参数启动失败，升级时需同步替换为当前 unit。云备份实现
不在本分支，当前版本也不提供相关入口。

`memory.config.json` 中由旧版本写入的 `cloud_backup_enabled`、
`backup_frequency`、`backup_scopes`、`max_backup_bytes` 在读取时被**丢弃**、
不参与校验、也不会写回：旧文件仍能加载，下一次设置写入后这四个键即从文件中
消失；其它未知键依旧按 `deny_unknown_fields` 拒绝。`privacy.settings.get`
不再返回 `backup_enabled`/`backup_supported`。

## 7. 通过受信宿主或 SDK 设置

BiBit 设置页通过宿主能力 `service:memory` 调用 `privacy.settings.update`；其他
受信集成方直接调用 SDK 的 `client.privacy.update_settings(...)`。Runtime 已在
2026-09-21 移除 `/api/v1/plugin-consoles/...` 管理路由，不要再按旧接口接入。

请求体不应传入账号或 Agent 身份字段；这些值来自 Runtime 的可信进程上下文。
`summary_model` 与 `embedding` 两个结构化配置也通过同一入口整体传入（缺失字段按
默认值补齐，因此每次都应发送完整结构）。当前工程没有把配置修改暴露给大模型 Tool，
避免模型在没有用户确认的情况下改变记忆、保留期限或审计策略。

## 8. 进程级环境变量

| 变量 | 缺省 | 作用 |
| --- | --- | --- |
| `BIANBU_AGENT_MEMORY_ENABLED` | `true` | Runtime 是否加载/调用外部 Memory 后端；关闭时 Runtime 不发起 SDK 请求 |
| `AGENT_MEMORY_EMBEDDING_DISABLED` | 未设置 | 仅压制 Embedding 的“开箱默认”，不影响显式配置段落；供密闭测试和排障使用 |
| `AGENT_MEMORY_HEALTH_SETTLE_MS` | `3000` | 距最近一次写入不足该时间就不做 health 取样；`0` 表示不等待 |
| `AGENT_MEMORY_HEALTH_CHECK_INTERVAL_MS` | `30000` | 复用上一次 health 取样的时间窗口；`0` 表示每次重新检查 |

health 各字段属于运行诊断输出，不是可写配置。
