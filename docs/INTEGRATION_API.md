# Agent Memory 对接 API 与维护指南

[English](INTEGRATION_API_EN.md) | [简体中文](INTEGRATION_API.md)

本文说明 AgentMemory 与 Runtime、BiBit、其他 Agent 以及 memoryd 之间的稳定对接边界。
文中的接口并不都是“外部 HTTP API”：业务方应优先使用 Python SDK；BiBit 设置页走
宿主能力 `service:memory`；原始 UDS 协议和 D-Bus 属于高级集成面，不要直接读写
SQLite、`memory.config.json` 或内部表结构。

| 接口层 | 入口 | 定位 |
| --- | --- | --- |
| Python SDK | `MemoryClient` / `AsyncMemoryClient` | 业务方首选的稳定接口 |
| BiBit 宿主能力 | `service:memory` | 设置页与受信宿主的集成面，不是 HTTP |
| Runtime Tool（MCP） | `memory` / `memory_record` / `memory_retrieve` / `session_search` | 供运行时模型调用，不是通用 SDK API |
| 原始 UDS 协议 | `agent-memory.v1` | 自研客户端/诊断使用，需自行处理握手与身份 |
| D-Bus | `com.bianbu.AgentMemory1` | 可选控制面，普通 Agent 不建议直接使用 |

## 1. 组件边界

```text
BiBit 设置页 / 外部 Agent / Runtime Hook 或 Tool
                 │
                 ├─ BiBit 宿主能力 service:memory
                 ├─ Runtime MCP Tools
                 │    memory / memory_record / memory_retrieve / session_search
                 └─ Python Memory SDK
                              │ Unix Domain Socket
                              ▼
                       bianbu-agent-memoryd
                         ├─ SQLite 权威数据
                         ├─ memory.config.json
                         └─ L1/L2/L3、审计、导出/导入
```

- Runtime 负责会话生命周期、可信账号/Agent 身份、Hook/Tool 调度和用户确认。
- Memory SDK 负责客户端协议、参数编码、重连和类型转换。
- memoryd 负责身份校验、Capability、命名空间隔离、校验、事务、审计和持久化。
- SQLite 是权威数据源；FTS/向量索引属于可重建派生数据。
- 云备份已于 2026-10-16 下线，本服务不生成、上传或恢复云端备份包；本地备份/恢复走
  `privacy.export_data` / `privacy.import_data`，导出文档带命名空间 `scope`，导入要求
  与当前 account/agent/space 一致。
- Memory 不承载社区知识库、文件全盘检索或命令执行预览；这些由 BiBit/Agent OS
  和 Runtime/Security 负责。

## 2. Runtime 身份握手

Runtime 启动 Worker 时注入受信任的环境变量：

```text
BIANBU_AGENT_MEMORY_SOCKET=/run/bianbu/agent-memoryd.sock
BIANBU_AGENT_MEMORY_REGISTRY_DIR=/run/bianbu/agent-runtime/memory-registry
BIANBU_AGENT_MEMORY_ACCOUNT_ID=default
BIANBU_AGENT_MEMORY_AGENT_NAME=default-agent
BIANBU_AGENT_MEMORY_ENABLED=true
```

上例是开发环境的示例值；生产值由 Runtime 的账户与 Agent 配置决定。

SDK 建立 UDS 连接后的第一帧必须是 `system.hello`：

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

`request_id` 和 `trace_id` 是必填字段；`idempotency_key` 可省略，`deadline_ms` 可选。
上面展示的是完整请求信封，不能只发送 `params`。

memoryd 将 UDS peer credentials（UID/GID/PID）、Runtime registry、进程启动时间和
Capability 与握手身份绑定。普通 Agent 在请求体中传 `account_id`/`agent_name` 不能覆盖已绑定身份；只有注册时声明
`allow_runtime_context` 的受信 Runtime 才能选择其他账户/Agent 上下文，并由服务端派生
独立的 `memory_space_id`。

## 3. Python SDK

安装 `bianbu-agent-memory` 后，Runtime 或受信任的集成方可以使用：

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

上面的示例要求调用进程已经由 Runtime 注册，或连接的是隔离的 `--development-identity`
daemon。普通 shell 直接连接生产 socket 会返回
`Unauthenticated: runtime registration was not found`，这是身份校验，不是 SDK 不可用。

常用 API：

| SDK 调用 | memoryd 方法 | 用途 |
| --- | --- | --- |
| `client.sessions.append_event(...)` | `sessions.append_event` | 写入 L1 Session Event |
| `client.sessions.get_recent_context(...)` | `sessions.get_recent_context` | 获取最近上下文 |
| `client.sessions.get_history(...)` | `sessions.get_history` | 查询会话历史 |
| `client.long_term.put(...)` | `long_term.put` | 写入 L3 长期记忆 |
| `client.long_term.search_headers(...)` | `long_term.search_headers` | 检索长期记忆索引（语义检索启用时为词法+向量 RRF 融合排序） |
| `client.long_term.get_many(...)` | `long_term.get_many` | 读取权威记忆 |
| `client.privacy.settings()` | `privacy.settings.get` | 读取配置 |
| `client.privacy.update_settings(...)` | `privacy.settings.update` | 部分更新配置 |
| `client.privacy.export_data(...)` | `privacy.export_data` | 导出数据 |
| `client.privacy.import_data(...)` | `privacy.import_data` | 从导出文档恢复数据（需确认） |
| `client.privacy.clear_history(...)` | `privacy.clear_history` | 清除会话历史（需确认） |
| `client.privacy.clear_all(...)` | `privacy.clear_all` | 清除全部记忆（需确认） |
| `client.runtime.prepare_checkpoint(...)` | `runtime.prepare_checkpoint` | 创建记忆水位检查点 |
| `client.runtime.restore_checkpoint(...)` | `runtime.restore_checkpoint` | 统计检查点之后的事件与待同步队列变化 |

异步代码使用 `AsyncMemoryClient`，方法名称和参数一致。

L3 记录的 `tags` 中，`important` 是用户重要标记的保留标签；SDK 提供
`long_term.mark_important(...)` / `unmark_important(...)`，只切换该标签，不改写
正文和其他标签。

本地导出/导入也可以直接用 CLI。CLI 与 SDK 一样依赖 Runtime 注册；本地开发应连接
隔离的 `--development-identity` daemon。`export` 只需要读权限，`import` 还需要
调用方提供确认令牌：

```bash
bianbu-agent-memory export ~/memory-export.json --limit 500 --without-audit
bianbu-agent-memory import ~/memory-export.json --confirm-token "$TOKEN"
```

`--limit` / `limit_per_collection` 是**单页大小**，不是导出总量。SDK 与 CLI 默认
自动跟随 `next_cursor` 拉完所有页；单页上限 500 用来把单次 UDS 响应压在 4 MiB 帧
预算内，单页内容仍过大时会返回 `ResourceExhausted`。
`export_data(..., all_pages=False)` 只返回第一页，适合诊断；
当前公开 SDK 不暴露 `snapshot_at`/`snapshot`/`cursor` 参数，不能靠它自行续页。

自研 `agent-memory.v1` 客户端需要把首屏固定的 `snapshot` 与 `next_cursor` 原样回传，
导出期间新写入的记忆才不会混入本次文件。三张表各有独立 rowid 序列，因此响应同时带
`snapshot.session_events/session_summaries/long_term_memories`；只回传 `snapshot_at`
是兼容旧客户端的降级路径。SDK 返回合并后的文档，CLI 再把它写成一个
`bianbu-agent-memory.export.v1` 文件；分页合并结果标记 `complete=true` 并带
`page_count`，单页导出（含旧版 daemon）保持原有文档形状，不强制补该字段。

`import` 是合并语义：已存在的记录按原 id 跳过，不会覆盖或删除现有记忆；文档的
`scope` 必须与当前命名空间一致。

`long_term.search_headers` 的语义检索对调用方完全透明：方法签名、参数与返回结构
不变。启用 embedding 后，daemon 内部先跑词法级联（FTS5），再做向量召回；两路
都有结果时以 RRF(k=60) 融合，`score` 承载 RRF 分值；只有一路有结果时保留该路原始
分数（FTS 或 cosine）。无论哪种情况，SDK 都只应消费排序，不应解释数值大小。
embedding 禁用、推理服务不可用或超时（查询路径上限 1500ms）时静默回退纯词法
结果，调用方无感知、无错误码。

## 4. BiBit 设置页与受信宿主

BiBit 的“设置 > 记忆”页面通过宿主能力 `service:memory` 调用下列 operation：

- `privacy.settings.get` / `privacy.settings.update`
- `privacy.clear_history` / `privacy.clear_all`
- `privacy.export_data` / `privacy.import_data`

页面只负责 UI；可信身份由宿主注入，Adapter 再把请求转给 memoryd。删除、导出和
导入类 operation 的确认令牌由 Runtime/Security 或可信 Adapter 注入，页面弹窗不承担
安全校验（`strict` 模式下导出也需要令牌）。Runtime 已在 2026-09-21 移除 `/api/v1/plugin-consoles/...` 管理路由，K3 上
该路径返回 404；不要按旧文档再接这个 HTTP 接口。

其他受信集成方直接使用 Python SDK。Runtime 的 `memory`/`memory_record`/
`memory_retrieve`/`session_search` MCP Tool 会使用当前 `RunContext` 的
`account_id` 和 `agent_id`，因此多 Agent 数据按命名空间隔离。当前没有把 `settings.update` 暴露为大模型 Tool；修改隐私配置应由
UI、BiBit 或受信任集成方发起，并保留审计和用户确认边界。

## 5. 原生协议和 D-Bus

UDS 使用换行分隔的 JSON frame，协议版本为 `agent-memory.v1`，单帧上限 4 MiB
（含换行符；服务端会在 `system.hello` 响应中返回生效的 `max_frame_bytes`）。
所有 UDS 请求必须先完成 `system.hello`，之后才能调用其他方法。

memoryd 还提供可选 D-Bus 服务：

```text
Bus name: com.bianbu.AgentMemory1
Object path: /com/bianbu/AgentMemory1
Interface: com.bianbu.AgentMemory1
Method: Call(string request_json) -> string response_json
```

D-Bus 通过总线发送方的进程凭据认证，不使用 `system.hello`。普通 Agent 不应直接
依赖 D-Bus；Runtime/SDK 的 UDS 路径能够携带 Runtime 选择的 Agent 上下文，兼容性
和隔离性更好。

## 6. 错误和重试约定

- `MemoryDisabled`：记忆开关关闭；不得将其解释为写入成功。
- `Unauthenticated`/`PermissionDenied`：身份、Capability 或确认令牌不满足。
- `ResourceExhausted`：本地存储配额不足、响应超过帧上限，或数据库写锁忙；
  只有响应标记 `retryable=true` 时才退避重试。
- `ServerUnavailable`（SDK 异常）/ `backend_unavailable`（Tool 结果）：memoryd 不可用；
  保留原始事件，不能伪造成功。
- 带 `idempotency_key` 的写操作可以安全重试；同一 key 不得复用到不同参数。

## 7. 部署检查

```bash
sudo systemctl status bianbu-agent-memoryd.service --no-pager
test -S /run/bianbu/agent-memoryd.sock
ls -l /run/bianbu/agent-runtime/memory-registry
curl -sS http://127.0.0.1:9899/api/v1/runtime/status
```

如果 Runtime 返回 `backend_unavailable`，先检查 memoryd 日志、UDS、registry 权限和
Runtime/Memory 包版本；不要切换到 Runtime-local 文件后宣称 Memory 已写入。
