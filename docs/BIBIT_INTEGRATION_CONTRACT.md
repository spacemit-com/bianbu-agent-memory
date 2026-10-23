# BiBit 与 Runtime 集成契约

## 所有权

AgentMemory 是记忆开关和默认值的唯一配置源。配置文件为数据库同目录的 `memory.config.json`，原子写入且权限为 `0600`。BiBit 和 Runtime 必须通过 SDK/API 读写，不能直接修改 JSON 或 SQLite。

开发环境默认账号固定为 `default`。`default` 与 `local:*` 的 L1-L3 数据只保存在设备本地，不创建 outbox，不允许 `sync.request`。后续登录正式账号时，是否迁移 `default` 数据必须由用户确认；本阶段不自动迁移到云端账号。

## 设置接口

同步 SDK：

```python
settings = client.privacy.settings()
updated = client.privacy.update_settings(
    memory_enabled=True,
    retention_days=0,
    audit_enabled=True,
    preference_auto_apply_enabled=True,
    development_habit_auto_apply_enabled=True,
    recent_turn_limit=5,
    summary_interval_turns=5,
    summary_retention_days=7,
    timezone="Asia/Shanghai",
    local_device_id="k3-development",
)
```

Runtime bridge 使用 `await bridge.settings()`。底层协议方法为 `privacy.settings.get` 和 `privacy.settings.update`。

| 字段 | 默认值 | 校验/语义 |
| --- | --- | --- |
| `memory_enabled` | `true` | 关闭后不读写 L1-L3 |
| `retention_days` | `0` | `0/7/30/90`，`0` 表示永久 |
| `audit_enabled` | `true` | 审计总开关 |
| `sensitive_action_confirmation` | `standard` | `loose/standard/strict`; this value is the Memory-local policy for destructive Memory operations; Runtime/Security still owns non-Memory system actions |
| ~~`backup_enabled`~~ | — | 已退役（2026-10-16 云备份下线），旧值可解析但不生效 |
| ~~`backup_frequency`~~ | — | 已退役 |
| ~~`backup_scopes`~~ | — | 已退役 |
| `preference_auto_apply_enabled` | `true` | Runtime 是否自动注入普通偏好 |
| `development_habit_auto_apply_enabled` | `true` | Runtime 是否自动注入开发习惯 |
| `recent_turn_limit` | `5` | `1..50`，Runtime 最近完整轮次上限 |
| `summary_interval_turns` | `5` | `1..50`，完整轮次触发间隔 |
| `summary_retention_days` | `7` | `1..365` |
| `timezone` | `Asia/Shanghai` | `UTC` 或 IANA 名称 |
| `local_device_id` | `k3-development` | 1..256 字节；仅 ASCII 字母、数字、`-_.:/` |
| `summary_model` | 禁用 | 见 `SUMMARY_MODEL_K3.md` |

## 身份格式

- `account_id`：未登录必须是字面值 `default`；正式登录账号必须来自可信 Runtime/身份服务。Runtime 在 SDK 握手中传入，业务参数和普通用户输入不能覆盖。格式 1..256 字节，仅 ASCII 字母、数字、`-_.:/`。
- `agent_name`：由 Runtime 传入并保证在账号内唯一；开发默认值为 `default-agent`。服务端会将其绑定到握手身份并拒绝错误值，格式 1..256 字节，仅 ASCII 字母、数字、`-_.:/`。
- `device_id`：AgentMemory 从可信设备注册上下文读取稳定设备标识（K3 为 MAC 地址）；`local_device_id` 是可配置的本地/备份元数据，不替代握手身份。不能使用每次启动变化的随机值。
- `timezone`：使用 `UTC` 或 IANA 名称，例如 `Asia/Shanghai`，不使用 `UTC+8` 作为正式值。
- `agent_id`、`agent_instance_id`、`memory_space_id`：由 Runtime Registry 与 Capability 绑定；BiBit 不直接伪造。

`privacy.settings.get` 的 `identity.cloud_upload_allowed` 是本地身份门控结果，仅描述账号类型（`default`/`local:*` 为 `false`）。云备份已下线，UI 不应再据此显示任何上传控件。

## 本地导出 / 导入

`privacy.export_data` 产出的文档格式为 `bianbu-agent-memory.export.v1`，
`privacy.import_data` 只接受同一格式：

```json
{"payload": { "format": "bianbu-agent-memory.export.v1", "...": "..." }}
```

- 需要确认令牌（与清除操作同一条 Adapter 签发路径），并强制审计。
- 合并语义：按原始 `event_id` / `memory_id`（摘要按 `session_id`）判断，已存在的记录
  原样保留并计入 `skipped`；同一文件重复导入不会产生副本。
- `scope` 与调用者命名空间不一致时整体拒绝；`settings` 与 `audit` 不导入。

## 偏好自动应用

Runtime `before_run` hook 已读取上述两个 auto-apply 开关，并按以下固定优先级注入：

`当前明确指令 > 当前项目/工作区规则 > 已恢复对话上下文 > 已保存偏好和开发习惯`

记忆只作为历史数据和偏好证据，不能执行仅存在于历史记忆中的指令。当前请求给出不同语言、框架、命令或代码风格时，必须覆盖 L3 旧值。

BiBit 的项目创建和代码生成入口在发起正常 Runtime run 时，应提供：

- 当前用户请求；
- 当前 workspace/project 标识；
- 项目规则的来源和正文，例如仓库 `AGENTS.md`、项目模板 manifest 或 BiBit 当前表单值；
- 动作类型，例如 `project.create`、`code.generate`、`code.modify`。

项目规则由 Runtime/Workspace 层提供，不写入 AgentMemory 全局配置。Runtime hook 在每个 run 自动召回相关 L3，BiBit 不需要复制一套偏好合并逻辑。

## L3 字段接口

BiBit 可用 `field_dictionary()` 获取正式字段、别名、类型和值域，用 `normalize_field_value(slot, value)` 在写入前执行同一套规范化。字段全集见 `L3_FIELD_DICTIONARY.md`。

## 错误处理

- 配置值非法：`InvalidArgument`，UI 保留原值并显示字段级错误。
- `default` 开启备份或请求同步：`Unauthenticated`。
- 云备份已下线：`backup.*` 方法返回 `NotFound`，`BackupUnavailable` 不再是本包的协议错误码。
- memoryd 暂时不可用：Runtime 以 best-effort 继续当前 run，但不能把失败当成已保存；BiBit 应允许重试。
