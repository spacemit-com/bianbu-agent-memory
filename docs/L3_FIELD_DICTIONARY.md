# L3 业务字段词典

正式机器可读定义位于 `bianbu_agent_sdk.memory.ontology.L3_FIELD_DICTIONARY`。调用方使用：

```python
from bianbu_agent_sdk.memory import field_dictionary, normalize_field_value

fields = field_dictionary()
canonical = normalize_field_value("development.package_manager", "PNPM")
assert canonical == "pnpm"
```

## 字段表

| 正式名称 | 中文/英文别名示例 | 类型和值校验 |
| --- | --- | --- |
| `profile.user_name` | 姓名、名字、user name | 非空字符串，最多 64 UTF-8 字节 |
| `profile.preferred_address` | 称呼、昵称、preferred address、nickname | 非空字符串，最多 64 UTF-8 字节 |
| `schedule.timezone` | 时区、timezone | `UTC` 或 IANA 名称，最多 64 字节 |
| `schedule.week_start` | 每周第一天、一周开始日 | monday..sunday；周一/星期一等归一化 |
| `schedule.default_reminder_time` | 默认提醒时间 | 24 小时制 `HH:MM` |
| `appearance.wallpaper_color_scheme` | 壁纸配色、桌面背景主题 | `light/dark/system` |
| `appearance.terminal_color_scheme` | 终端配色、终端主题 | `light/dark/system` |
| `appearance.interface_color_scheme` | 界面配色、系统外观 | `light/dark/system` |
| `application.default_browser` | 默认浏览器、browser | `firefox/chromium/chrome/edge/safari` |
| `interaction.response_language` | 回答语言、回复语言 | 支持的 BCP 47 language tag |
| `development.programming_language` | 编程语言、开发语言 | 非空字符串，最多 64 字节 |
| `development.project_template` | 项目模板、工程模板、脚手架 | 非空字符串，最多 512 字节 |
| `development.package_manager` | 依赖管理、包管理器 | `uv/pip/poetry/cargo/npm/pnpm/yarn/maven/gradle` |
| `development.test_framework` | 测试框架 | 非空字符串，最多 64 字节 |
| `development.formatter` | 格式化工具、formatter | 非空字符串，最多 64 字节 |
| `development.linter` | 代码检查工具、linter | 非空字符串，最多 64 字节 |
| `development.indentation` | 代码缩进、缩进宽度 | 整数 `1..8` |
| `development.line_length` | 行宽、每行字符 | 整数 `40..240` |
| `development.naming_convention` | 命名风格、命名规范 | 非空字符串，最多 128 字节 |
| `development.code_style` | 代码风格 | 非空字符串，最多 256 字节 |
| `development.editor` | 编辑器、IDE | 非空字符串，最多 64 字节 |
| `development.shell` | 默认 Shell、常用 Shell | 非空字符串，最多 64 字节 |
| `development.runtime_version.{python,rust,node,java,go}` | Python 版本、Rust version 等 | `v?数字[.数字]{0,3}`，可带 prerelease/build |
| `development.command.{test,build,format,lint,run,install}` | test 命令、build command 等 | 非空命令字符串，最多 2048 字节 |

## 写入约定

- 每条稳定字段写为 `slot:<正式名称>` tag；同一账号、Agent、memory space 和 slot 只保留当前权威 revision。
- 所有字符串去除首尾空白并压缩内部空白；禁止控制字符。
- enum 大小写不敏感并保存为小写 canonical value。
- **显式带 `slot` 的写入声明“这段文本就是该字段的值”**（MEM-L3-03）。因此写入路径会对该文本执行词典声明
  （`allowed_values`、`value_type`、`max_length`）：可识别时按 canonical 值落库（`深色`→`dark`、`Google Chrome`→`chrome`、
  `100`→`100`），不可识别时拒绝并返回 `invalid_value`，同时在结果中给出 `slot`、`value_type`、`max_length`、
  `allowed_values` 和 `assistant_instruction`，不写入任何记录。
- 需要保存叙述性文本（例如“用户喜欢的终端颜色是浅色，不是深色。”）时**不要带 slot**：不带 slot 的文本按目标预算
  （`user` 1375 / `memory` 2200 字符）存储，或交给 `preference_slot()` 自动分类，推断出的槽位只是分类标签，
  永远不参与值校验、也不会改写文本。
- 低层 `long_term.put/revise` 用 `tags=("slot:...",)` 直写的路径（内部组件与验收脚本）不做值校验；值闸门位于
  SDK 写入闸门 `MemoryBridge.upsert_explicit` / `memory` Tool，Rust 侧只保留字符集与文本长度兜底。
- 项目自己的规则只在当前 workspace/run 中生效，不要写成用户全局 L3，除非用户明确要求以后都采用。
- 常用命令只保存用户明确指定或项目规则确认过的命令；不得仅因一次工具调用就推断成长期习惯。
- 用户当前明确值与旧 L3 冲突时，当前请求优先；若用户明确表达长期变更，再创建新 revision 并使旧值失效。

## 重要标记（`important`）

`important` 是保留标签，不是 `slot:`：`validate_tags` 限制每条记录最多一个槽位，
用槽位会与记录自己的字段槽位冲突。标记与记录的内容类型正交，因此单独存在于
`tags` 中，并由召回结果原样回显。

- 只由显式动作产生，**不从字面推断**：是否重要由用户决定，模型不得代用户判断；
- 标记/取消标记是一次普通 `long_term.revise`，复用既有 revision 与合规检查，
  不引入新的存储格式；
- 入口：CLI `mark-important`/`unmark-important`、SDK `long_term.mark_important`、
  MCP 工具 `action="mark_important"`；
- 召回结果在 `tags` 里回显该标记（非空时才输出）：模型和用户据此判断哪些记忆
  被标记为重要，不必从正文推断；
- 云备份退役前，`important` 还是一个上传范围；该范围随云备份一起删除，标签与
  `mark-important` 入口保留（PRD 把“重要标记”定义为 L3 长期记忆的组成部分，
  与备份无关）。


## 历史数据的收口（0.1.17 之前落库的行）

写入闸门只拦新写入：0.1.17 之前落库的 `slot:` 标签不会自己消失，而且**对任何身份都不可见** ——
协议里每个方法只作用于调用者自己的 memory space，退役的验收/联调空间（临时 `agent_id`、探针空间、
`runtime-context.*`）只能由守护进程自己列出来。因此设备侧的收口分两步：

1. `bianbu-agent-memoryd --report-namespaces` 只读清册（逐空间行数 + 在用的 `slot:` 标签，不含正文），
   覆盖脚本 `scripts/k3_namespace_inventory.py` 按本词典判定每个空间是否合规，期望
   `K3_NAMESPACE_INVENTORY_OK`；
2. 命中词典外槽位的行由 `scripts/k3_memory_namespace_cleanup.py` 逐条 `long_term.forget`
   （`reason_code=l3_slot_gate_cleanup`）收口，期望 `K3_NAMESPACE_CLEANUP_OK`。
   以 `long_term.reconcile_slots` 收敛**重复**槽位在这里不适用：它的 `valid_slot` 只做字符集校验，
   且语义是“同槽位留最新一条”，删不掉整组越界槽位。

K3 现场（本地开发包，开发期编号 0.1.20）：18 个命名空间 / 107 条 L3 / 4 条词典外槽位（`test.marker`、
`workflow.deploy_action`、`development.example_code_language`、`development.python_dependency_manager`，
均在验收脚本留下的退役空间里）→ 收口后清册为 0，用户 Runtime 空间同时通过
`scripts/k3_memory_residue_report.py`（`K3_MEMORY_RESIDUE_OK`）。用法与证据见
`docs/TEST_CASES.md` §4.3。

`sdk/python/tests/test_ontology.py` 覆盖字段导出、别名映射、枚举、时间、时区、版本、行宽和非法输入。