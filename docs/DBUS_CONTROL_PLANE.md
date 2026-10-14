# memoryd D-Bus 控制面（MEM-INT-06-02）

状态：接口实现随 0.1.16 交付，K3 现网验收通过（2026-09-23，11/11 门禁）。

- 验收脚本：`scripts/k3_dbus_acceptance.py`（只读 + 拒绝路径 + 注册路径，不改动生产命名空间）
- 证据：`artifacts/k3-dbus-acceptance.json`
- 实现：`crates/memoryd/src/dbus.rs`、policy `packaging/com.bianbu.AgentMemory1.conf`
- 协议信封：`docs/PROTOCOL.md`、`crates/memory-protocol/src/lib.rs`

适用对象：BiBit、系统设置等受信系统组件。按 PRD（V0.9 第 1253 行 / V0.91 第 871 行），
「记忆服务 DBus」是 **M4（2026.11）** 交付内容，本期提前交付服务端接口，
首个 D-Bus 消费方随 M4 落地；在此之前 Agent 业务一律走 SDK/UDS。

## 1. 契约

### 1.1 总线、服务名与对象路径

| 项 | 值 |
|---|---|
| 总线 | 系统总线（`memoryd --dbus`；`--dbus-session` 仅开发调试） |
| 服务名 | `com.bianbu.AgentMemory1` |
| 对象路径 | `/com/bianbu/AgentMemory1` |
| 接口 | `com.bianbu.AgentMemory1` |
| 名称属主 | 服务账号 `bianbu-memory`（`bianbu-agent-memoryd.service`） |

启动日志会打印一行，可用它确认控制面已挂上：

```text
bianbu-agent-memoryd: DBus listening on com.bianbu.AgentMemory1 /com/bianbu/AgentMemory1 (system)
```

### 1.2 方法 `Call`

```text
Call(s request_json) -> s response_json
```

`request_json` 与 `response_json` 就是 UDS 上的同一套信封（`docs/PROTOCOL.md`），
因此客户端不需要第二套序列化：**D-Bus 只是把 UDS 的单行 JSON 当成一个字符串参数传输**。

- 两个方向都是 UTF-8 JSON，字符串本身不含换行。
- 单帧上限 4 MiB（`memory_core::MAX_FRAME_BYTES`）。超限返回
  `InvalidArgument: request exceeds protocol limit`，不会分配内存去解析。
- 请求不是合法 JSON → `InvalidArgument: request is not valid JSON`。
- 信封里的 `protocol` 不等于 `agent-memory.v1` → `ProtocolMismatch`。
- **D-Bus 上不允许 `system.hello`**：身份由总线提供，发送 `system.hello` 会得到
  `InvalidArgument: DBus calls are authenticated by the bus and do not use system.hello`。

### 1.3 属性 `ProtocolVersion`

只读、`emits-change`，取值恒为 `agent-memory.v1`（`crates/memory-core/src/lib.rs`）。
客户端可先读它再决定是否调用，避免协议漂移。

### 1.4 请求信封

```json
{
  "protocol": "agent-memory.v1",
  "request_id": "caller-uuid",
  "trace_id": "caller-uuid",
  "method": "system.health",
  "params": {},
  "idempotency_key": "optional-stable-key",
  "deadline_ms": 5000
}
```

`protocol` 缺省即为当前版本；`params` 缺省为空对象；`idempotency_key`、`deadline_ms` 可选。

成功响应：

```json
{
  "status": "ok",
  "request_id": "caller-uuid",
  "trace_id": "caller-uuid",
  "server_protocol_version": "agent-memory.v1",
  "result": {}
}
```

失败响应：

```json
{
  "status": "error",
  "request_id": "caller-uuid",
  "trace_id": "caller-uuid",
  "server_protocol_version": "agent-memory.v1",
  "error": { "code": "Unauthenticated", "message": "runtime registration was not found", "retryable": false }
}
```

注意：被拒时 `request_id`/`trace_id` 可能为 `unknown` —— 请求在解析阶段就被拒，
服务端还没拿到可回填的标识。

### 1.5 方法集合

`Call` 是通用透传，方法集合与 UDS 完全一致（`docs/PROTOCOL.md` 的方法表：
`system.*`、`account.status`、`instructions.*`、`sessions.*`、`long_term.*`、
`privacy.*`、`projection.*`、`sync.*`、`backup.*`、`audit.*`、`runtime.*`）。
未知方法返回 `NotFound: unknown protocol method`。

## 2. 身份与授权

D-Bus 调用与 UDS 复用同一套身份解析器（`crates/memoryd/src/registry.rs`），步骤如下：

1. 取消息头 `sender`（总线唯一名）；
2. 向 `org.freedesktop.DBus` 调 `GetConnectionUnixProcessID` 拿调用者 PID；
3. 读 `/proc/<pid>/status` 的 `Uid:`/`Gid:`；
4. 读 `/proc/<pid>/stat` 的进程启动时刻；
5. 在注册目录里匹配 `uid + gid + 启动时刻` 完全一致的注册记录。

| 情形 | 结果 |
|---|---|
| 未注册的调用者 | `Unauthenticated: runtime registration was not found` |
| 注册记录与进程不匹配 | `Unauthenticated: runtime registration does not match the peer process` |
| 注册目录属组/权限不信任 | `Unauthenticated: runtime registry permissions are not trusted` |
| 注册记录里出现未知能力 | `Unauthenticated`（capability 必须在 `memory_core::capability::ALL` 内） |
| 注册记录已过期 | 视同未注册 |

注册目录：`/run/bianbu/agent-runtime/memory-registry/<pid>.json`，由 Runtime 进程自己写入：

- 目录属主 `root`（或 Securityd 接管后的属主）、属组 = memoryd 的服务组、**非 other-writable**；
- 文件属主必须等于调用者的 uid，`mode & 0o027 == 0`（Runtime 用 `0640`）；
- 必填字段：`uid`、`pid_start_time_ticks`、`account_id`、`device_id`、`agent_id`、
  `agent_instance_id`、`memory_space_id`、`capabilities`；
- 可选字段：`gid`、`agent_name`、`executable_path`、`expires_at_ms`、`allow_runtime_context`。

与 UDS 的两点差别，消费方需要知道：

- **没有 Runtime context 通道。** UDS 握手允许注册身份再选一次 account/agent 上下文，
  D-Bus 请求不带这个字段，因此服务端使用注册记录里的**基础身份**（`allow_runtime_context` 被忽略）。
- **没有开发确认令牌以外的旁路。** `--development-identity` 模式下，只有
  `BIANBU_MEMORY_DEV_CONFIRM_TOKEN` 环境变量与 `params.confirm_token` 相等才算"已确认"，
  且该模式不用于生产。

## 3. D-Bus policy

`packaging/com.bianbu.AgentMemory1.conf`（安装到 `/usr/share/dbus-1/system.d/`）：

- `bianbu-memory` 允许 own `com.bianbu.AgentMemory1`；
- 默认策略只放行向 `com.bianbu.AgentMemory1` 发送 `com.bianbu.AgentMemory1`、
  `org.freedesktop.DBus.Introspectable`、`org.freedesktop.DBus.Properties` 三个接口。

即 **policy 只放宽"发现"，不放宽"授权"**：任何进程都能 introspect 到接口，
但真正的方法调用仍由守护进程按注册身份裁决。

## 4. 客户端调用方式

### 4.1 发现接口

```bash
busctl --system list | grep AgentMemory
# com.bianbu.AgentMemory1  9121 bianbu-agent-me bianbu-memory  :1.105  bianbu-agent-memoryd.service  -  -

busctl --system introspect com.bianbu.AgentMemory1 /com/bianbu/AgentMemory1
# .Call             method    s  s
# .ProtocolVersion  property  s  "agent-memory.v1"  emits-change

busctl --system get-property com.bianbu.AgentMemory1 /com/bianbu/AgentMemory1 \
  com.bianbu.AgentMemory1 ProtocolVersion
# s "agent-memory.v1"
```

### 4.2 发起调用

```bash
busctl --system call com.bianbu.AgentMemory1 /com/bianbu/AgentMemory1 \
  com.bianbu.AgentMemory1 Call s \
  '{"protocol":"agent-memory.v1","request_id":"r1","trace_id":"r1","method":"system.health","params":{}}'
```

从 shell 传 JSON 时注意引号：整个 JSON 是**一个** `s` 参数，外层用单引号包住，
内部双引号不要再转义。

### 4.3 未注册的调用者会看到什么

`busctl`/`dbus-send`/`gdbus` 进程自己的 PID 不在注册表里，因此即使调用格式完全正确，
也只能得到拒绝（2026-09-23 K3 实测）：

```json
{"status":"error","request_id":"unknown","trace_id":"unknown",
 "server_protocol_version":"agent-memory.v1",
 "error":{"code":"Unauthenticated","message":"runtime registration was not found","retryable":false}}
```

这是设计意图，不是故障：**D-Bus 不是绕开身份校验的运维后门**。命令行工具
（`bianbu-agent-memory`）同样需要进程自己已注册，未注册时会直接报
`ServerUnavailable`/`Unauthenticated`。

### 4.4 已注册的调用者

消费方需要在自己的进程里完成注册，再发请求。Gio 最小片段：

```python
import json
from gi.repository import Gio, GLib

connection = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
request = json.dumps({
    "protocol": "agent-memory.v1",
    "request_id": "r1", "trace_id": "r1",
    "method": "system.health", "params": {},
})
reply = connection.call_sync(
    "com.bianbu.AgentMemory1", "/com/bianbu/AgentMemory1",
    "com.bianbu.AgentMemory1", "Call",
    GLib.Variant("(s)", (request,)), GLib.VariantType.new("(s)"),
    Gio.DBusCallFlags.NONE, 15000, None,
)
print(json.loads(reply.unpack()[0]))
```

一键验收（K3，普通用户 `bianbu` 即可）：

```bash
python3 scripts/k3_dbus_acceptance.py --json /tmp/k3-dbus-acceptance.json
```

脚本自己注册探针身份（15 分钟兜底过期）、跑完 11 条门禁、退出前删除注册文件。

## 5. typed facade 目标设计（M4）

《Bianbu_5.0_Agent_Memory_Server_需求设计_V1.1.md》§5 规划了按场景分组的 typed facade；
当前实现只有通用 `Call` 透传。目标分组与消费方：

| 组 | 方法（目标） | 预期的受信消费方 |
|---|---|---|
| Account | GetStatus / BeginLogin / CompleteLogin / Logout | 系统设置、BiBit UI（按 2026-09-04「本版不提供账号登录」口径收敛为兼容入口） |
| Policy | GetPolicy / SetMemoryEnabled / SetRetention / SetSyncPolicy | 受信策略服务 |
| Sync | GetStatus / RequestSync / Pause / Resume | 系统设置、网络服务 |
| Backup | Create / List / PreviewRestore / Restore / Cancel | 系统设置、BiBit UI |
| Privacy | ClearConversation / ClearAll / ExportJson | 受信 UI |
| Runtime | RegisterInstance / PrepareCheckpoint / RestoreCheckpoint / UnregisterInstance | Agent Runtime |
| Audit | GetHealth / Flush | Agent OS Audit |

这些场景目前分别由 UDS、BiBit HTTP adapter 与 HTTPS 云适配承接；
D-Bus 上的 typed 方法与 D-Bus 形式的 Runtime 注册都留待 M4。

## 6. 验收记录（2026-09-23，K3 `192.0.2.10`）

环境：`bianbu-agent-memoryd` 0.1.20（开发期编号，非发布版本），
总线名 owner = `bianbu-memory`，接口已在系统总线上，`ProtocolVersion` = `agent-memory.v1`。

| 门禁 | 内容 | 实测结果 |
|---|---|---|
| G1 | 总线名归属 memoryd | owner `bianbu-agent-memoryd.service` / 用户 `bianbu-memory` |
| G2 | `Introspect` 暴露 `Call(s)->s` 与 `ProtocolVersion` | 见 §4.1 输出 |
| G3 | `ProtocolVersion == agent-memory.v1` | `s "agent-memory.v1"` |
| G4 | 未注册调用者被拒 | `Unauthenticated: runtime registration was not found` |
| G5 | 已注册调用者 `system.health` | `status=ok`，`schema_version=13`，`database=ok` |
| G6 | 已注册调用者 `system.stats` | `status=ok` |
| G7 | 协议版本不符 | `ProtocolMismatch: unsupported protocol version` |
| G8 | `system.hello` | `InvalidArgument`（按要求拒绝） |
| G9 | 超 4 MiB 帧 | `InvalidArgument: request exceeds protocol limit` |
| G10 | 非 JSON 请求 | `InvalidArgument: request is not valid JSON` |
| G11 | 删除注册文件后同进程再调 | 重新变成 `Unauthenticated` |

`system.health` 同时确认了语义检索已走网关 Embed 域（`base_url=http://127.0.0.1:18790/v1/embed`、
`model=qwen3-embedding-0.6b-q8_0`、`dims=1024`、`config_file_matches=true`），
且数据库一致性判定为 `verdict=ok`（带 `state=complaint` 的 FTS5 索引文案属
`docs/GATEWAY_EMBEDDING_INTEGRATION.md` §8.2 已记录的该构建瞬时判定）。

## 7. 边界与未闭环

- **没有 D-Bus 生产消费方**：按 PRD，首个消费方随 M4 落地；本期交付的是接口与调用方式。
- **typed facade 未实现**：当前只有通用 `Call` 透传，见 §5。
- **D-Bus ≠ 运维入口**：未注册的 `busctl`/CLI 一律被拒；需要人工排障时走 SDK/UDS 的已注册路径。
- **验收环境是单台 K3**：没有第二台设备、没有跨设备/多云场景，D-Bus 侧也不涉及云备份。
