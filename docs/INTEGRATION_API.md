# Agent Memory 对接 API 与维护指南

本文说明 AgentMemory 与 Runtime、BiBit、其他 Agent 以及 memoryd 之间的稳定对接边界。
业务方应优先使用 Python SDK 或 Runtime Plugin Console，不要直接读写 SQLite、
`memory.config.json` 或内部表结构。

## 1. 组件边界

```text
BiBit / 外部 Agent / Runtime Hook 或 Tool
                 │
                 ├─ Runtime Plugin Console（HTTP 管理接口）
                 │
                 └─ Python Memory SDK
                              │ Unix Domain Socket
                              ▼
                       bianbu-agent-memoryd
                         ├─ SQLite 权威数据
                         ├─ memory.config.json
                         └─ L1/L2/L3、审计、Capsule
```

- Runtime 负责会话生命周期、可信账号/Agent 身份、Hook/Tool 调度和用户确认。
- Memory SDK 负责客户端协议、参数编码、重连和类型转换。
- memoryd 负责身份校验、Capability、命名空间隔离、校验、事务、审计和持久化。
- SQLite 是权威数据源；FTS/向量索引属于可重建派生数据。
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

SDK 建立 UDS 连接后的第一帧必须是 `system.hello`：

```json
{
  "protocol": "agent-memory.v1",
  "method": "system.hello",
  "params": {
    "sdk_language": "python",
    "sdk_version": "0.1.6",
    "account_id": "default",
    "agent_name": "default-agent"
  }
}
```

memoryd 将 UDS peer credentials（UID/GID/PID）、Runtime registry、进程启动时间和
Capability 与握手身份绑定。请求体中的 `account_id`/`agent_id` 不能覆盖已绑定身份；
Runtime 选择 Agent 上下文时由服务端派生独立的 `memory_space_id`。

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
| `client.privacy.clear_history(...)` | `privacy.clear_history` | 清除会话历史（需确认） |
| `client.privacy.clear_all(...)` | `privacy.clear_all` | 清除全部记忆（需确认） |
| `client.runtime.prepare_checkpoint(...)` | `runtime.prepare_checkpoint` | 创建记忆水位检查点 |
| `client.runtime.restore_checkpoint(...)` | `runtime.restore_checkpoint` | 查询检查点之后的变化 |

异步代码使用 `AsyncMemoryClient`，方法名称和参数一致。

`long_term.search_headers` 的语义检索对调用方完全透明：方法签名、参数与返回结构
不变。启用 embedding 后，daemon 内部先跑词法级联（FTS5），再做向量召回，最后以
RRF(k=60) 融合两路列表——返回的 `score` 字段承载 RRF 融合分值（原本就是跨阶段
异质分值，SDK 只应消费排序，不应解释数值大小）。embedding 禁用、推理服务不可用
或超时（查询路径上限 1500ms）时静默回退纯词法结果，调用方无感知、无错误码。

## 4. Runtime Plugin Console

Runtime 的 `bianbu.memory` Console 将管理请求转发给 SDK：

```text
POST /api/v1/plugin-consoles/bianbu.memory/actions/settings.get
POST /api/v1/plugin-consoles/bianbu.memory/actions/settings.update
```

请求体必须是 JSON 对象；`settings.update` 支持部分字段更新，例如：

```bash
curl -sS -X POST \
  http://127.0.0.1:9899/api/v1/plugin-consoles/bianbu.memory/actions/settings.update \
  -H 'content-type: application/json' \
  -d '{
    "memory_enabled": true,
    "retention_days": 7,
    "audit_enabled": true,
    "sensitive_action_confirmation": "strict",
    "preference_auto_apply_enabled": true,
    "development_habit_auto_apply_enabled": false,
    "recent_turn_limit": 5,
    "recent_session_limit": 5,
    "summary_interval_turns": 5,
    "summary_retention_days": 7
  }'
```

查询：

```bash
curl -sS -X POST \
  http://127.0.0.1:9899/api/v1/plugin-consoles/bianbu.memory/actions/settings.get \
  -H 'content-type: application/json' -d '{}'
```

生产环境若启用 Runtime 管理认证，还必须携带 `Authorization: Bearer <token>`。
HTTP 请求不应携带或修改 `account_id`、`agent_id`、`agent_name`；这些值来自 Runtime
的可信进程上下文。

Runtime 的 `memory_record`/`memory_retrieve` Tool 会使用当前 `RunContext` 的
`account_id` 和 `agent_id`，因此多 Agent 数据按命名空间隔离。当前没有把
`settings.update` 暴露为大模型 Tool；修改隐私配置应由 UI、BiBit 或受信任集成方
发起，并保留审计和用户确认边界。

## 5. 原生协议和 D-Bus

UDS 使用换行分隔的 JSON frame，协议版本为 `agent-memory.v1`，单帧上限 256 KiB。
所有请求必须先完成 `system.hello`，之后才能调用其他方法。

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
- `BackupUnavailable`：云备份适配器或 Capsule 密钥未配置。
- `ResourceExhausted`：数据库写锁忙；仅对标记为 retryable 的错误进行退避重试。
- `Unavailable`/`backend_unavailable`：memoryd 不可用；保留原始事件，不能伪造成功。
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
