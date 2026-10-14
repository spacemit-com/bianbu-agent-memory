# AgentMemory 完整测试用例

本文是 Memory 模块的统一测试入口。测试分为四类：

- **自动化回归**：可在源码目录或 CI 中重复执行；
- **K3 集成测试**：验证 `memoryd`、SDK、Runtime、D+ 和 systemd；
- **安全/数据边界测试**：验证加密、隔离、删除和敏感信息不外泄；
- **性能与可靠性测试**：验证容量、并发、重启和长期运行指标。

状态含义：`已验证` 表示当前已有测试证据；`需执行` 表示用例已定义但当前没有完整验收证据；`负向通过` 表示预期是拒绝、降级或返回不可用。

## 1. 测试环境和安全规则

### 1.1 源码和 K3 路径

| 项目 | 值 |
| --- | --- |
| Memory 源码 | `D:\code\agent-memory\bianbu-agent-memory` |
| Memory 远端 | `https://github.com/spacemit-com/bianbu-agent-memory.git` |
| K3 地址 | `bianbu@192.0.2.10` |
| K3 Memory 源码 | `/home/example/bianbu-agent-memory`（以实际部署目录为准） |
| K3 Memory UDS | `/run/bianbu/agent-memoryd.sock` |
| K3 Runtime API | `http://192.0.2.10:9899` |
| K3 D+ API | `http://127.0.0.1:18920` 或部署脚本指定端口 |

生产安装使用 `/run/bianbu/agent-memoryd.sock` 和 Runtime registry；独立 SDK 测试使用 `--development-identity`、临时 UDS 和临时 SQLCipher 数据库。不得用开发身份连接生产数据库。

文档里的 `0.1.x` 有两个来源，读结果时不要混用：**发布版本**只由 git tag（`bianbu/<ver>`）决定，软件源和设备装的是它；**开发期编号**是 main 分支 `debian/changelog` 的累计编号，本地开发包（`dpkg-buildpackage`）用它，因此可能比当前发布号更大（例如本地包显示 `0.1.20` 时，源上的发布版本仍是 `0.1.17`）。文中凡是标注「本地开发包 / 开发期编号」的结果都来自后者。

### 1.2 基本环境检查

```bash
cd /path/to/bianbu-agent-memory
git status --short --branch
git log -1 --oneline
rustc --version
cargo --version
python3 --version
```

K3 登录：

```bash
ssh bianbu@192.0.2.10
```

测试过程中不得直接修改正在运行的 SQLite 文件。涉及 `clear_all`、迁移、替换 DEB 或删除数据库的用例，必须先停止相关服务并保留备份。

## 2. 自动化测试用例

### 2.1 Rust 核心、协议和 memoryd

| 编号 | 用例 | 执行命令 | 预期结果 | 当前状态 |
| --- | --- | --- | --- | --- |
| AUTO-RUST-01 | 全 workspace 单元、集成和 doc 测试 | `cargo test --workspace --all-targets --locked` | 所有测试通过，无 panic | 已验证（K3 有完整通过记录） |
| AUTO-RUST-02 | Rust 格式检查 | `cargo fmt --all -- --check`；若 K3 只有工具链，使用 `rustfmt --check <目标文件>` | 无格式差异 | 已验证 |
| AUTO-RUST-03 | Release 构建 | `cargo build --workspace --release --locked` | 生成 `target/release/bianbu-agent-memoryd` | 已验证 |
| AUTO-RUST-04 | 协议常量、HLC、事件类型和标识符 | `cargo test -p memory-core -p memory-protocol` | 协议版本和合法性校验通过 | 已验证 |
| AUTO-RUST-05 | Store 全量流程、SQLCipher、FTS5、revision、Tombstone | `cargo test -p memory-store --lib --tests` | 写入、检索、冲突、删除和索引测试通过 | 已验证 |
| AUTO-RUST-06 | memoryd UDS/DBus/peer credential | `cargo test -p bianbu-agent-memoryd` | 身份和 DBus 常量测试通过 | 已验证 |
| AUTO-RUST-07 | 明文迁移暂存和回滚副本清理 | `cargo test -p memory-store --test plaintext_cleanup` | 只删除目标数据库关联文件，拒绝目录/非目标文件 | 已验证 |
| AUTO-RUST-08 | 并发写入和 SIGKILL 恢复 | `cargo test -p memory-store --test store_flow` | 写事务串行化，恢复后数据完整 | 已验证 |

### 2.2 Python SDK、规则抽取和适配器

在 Windows PowerShell：

```powershell
cd D:\code\agent-memory\bianbu-agent-memory
$env:PYTHONPATH = "sdk/python/src"
python -m pytest -q sdk/python/tests
python -m pytest -q sdk/python/tests tests/test_external_api_contract_demo.py `
  tests/test_chinese_coreference_goldset.py tests/test_weather_memory_demo.py
python -m compileall -q sdk/python/src scripts weather_memory_demo
```

| 编号 | 用例 | 覆盖范围 | 预期结果 | 当前状态 |
| --- | --- | --- | --- | --- |
| AUTO-PY-01 | SDK 同步/异步客户端 | UDS 连接、重连、超时、协议帧、类型转换 | SDK 测试全部通过 | 已验证（96 项 SDK 测试） |
| AUTO-PY-02 | Runtime MemoryBridge | before/after、显式 record/retrieve、失败 Run | Hook 测试通过，失败轮不进入可召回上下文 | 已验证 |
| AUTO-PY-03 | L3 ontology | 姓名、浏览器、语言、包管理器、模板、命令和值校验；显式 slot 写入值闸门（归一 canonical 值、域外值/超长值拒绝） | 合法值规范化，非法值拒绝，拒绝时不落库 | 已验证 |
| AUTO-PY-04 | 敏感信息过滤 | password、token、API key、私钥、邮箱、电话、地址 | 凭证不抽取、不投影；PII 只进入加密 canonical | 已验证 |
| AUTO-PY-05 | Mem0 规则 Planner | ADD/UPDATE/NONE、重复合并、问题句和标题过滤 | 只从明确陈述抽取，查询不产生记忆 | 已验证 |
| AUTO-PY-06 | D+ 投影 | Mem0/Qdrant 映射、canonical 回表、重建、死信重试 | 派生索引可重建，结果始终回 canonical | 已验证 |
| AUTO-PY-07 | OAuth2 客户端契约 | HTTPS、授权码、token、userinfo、业务错误 | 合同错误被识别，`client_secret` 不进入请求体 | 已验证（协议客户端；不代表生产云认证完成） |
| AUTO-PY-08 | API-Key Backup Adapter | 加密 Capsule 上传/下载/删除、对象引用、SHA-256 | Mock/本地 HTTP 契约通过 | 已验证（不代表真实云服务完成） |
| AUTO-PY-09 | 中文指代金标集结构 | `tests/data/chinese_coreference_goldset.jsonl` | 字段、类别和覆盖面校验通过 | 已验证（真实模型准确率另测） |
| AUTO-PY-10 | Hermes 生命周期适配 | MemoryProvider 连接、写入和读取 | Hermes smoke 返回成功 | 已验证 |

### 2.3 一键规格验收

在 K3 的 Memory 构建目录执行：

```bash
cd /home/example/test/build-memory-current-*/src
PYTHONPATH="$PWD/sdk/python/src" \
python3.13 scripts/k3_requirements_acceptance.py \
  --memoryd target/release/bianbu-agent-memoryd
```

预期输出以 `K3_REQUIREMENTS_ACCEPTANCE_OK` 开头，并包含：

- `storage_encryption=sqlcipher`；
- 敏感数据加密持久化且不进入长期投影；
- 五轮自动摘要、L3 召回和 Checkpoint 保留 10 条；
- 投影死信可列出、重排队和再次领取；
- 审计导出、记忆开关、清除确认、重启恢复；
- account secret 和 key package 权限为 `0600`。

该脚本使用隔离临时数据库，不会清空正式 K3 记忆。

### 2.4 写入闸门现场验收（MEM-L3-02/MEM-L3-03）

同一构建目录执行：

```bash
cd /home/example/mem-gateway-build
PYTHONPATH=sdk/python/src \
python3 scripts/k3_field_value_gate.py \
  --memoryd target/release/bianbu-agent-memoryd
```

预期输出 `K3_FIELD_VALUE_GATE_OK`，且 `checks` 全部为 `true`（任一断言失败即以非零退出码终止）：

- `MEM-L3-03` 值闸门：显式 `slot` 的别名归一为规范值落库（`深色`→`dark`）；域外值、超长值（`development.shell` 65 字符）与“散文冒充字段值”返回 `invalid_value`（带 `value_type`/`max_length`/`allowed_values`/`assistant_instruction`）且一条都不落库；词典外槽位返回 `invalid_slot`（带 `known_slots`）；同一散文不带 `slot` 原样存储、不被静默改写；规范值可被召回（`slot=` 命中，返回 `dark`）。
- `MEM-L2-01` 摘要闸门：4 个完整轮次后连写 20 条 `task_started`/`tool_call`/`tool_result`，`revision` 仍为 0；第 5 个完整轮次触发 `revision=1`、`producer=memoryd.rules`；此后追加工具事件不再重新摘要。

该脚本同样使用隔离临时数据库（`--development-identity` + `AGENT_MEMORY_EMBEDDING_DISABLED=1`），不影响生产栈与 `bibit-intent`。

## 3. memoryd 独立功能测试

### 3.1 启动临时 development identity daemon

```bash
cd /home/example/test/build-memory-current-*/src
tmp="$(mktemp -d /tmp/agent-memory-test.XXXXXX)"
printf '%s\n' "$(openssl rand -hex 32)" > "$tmp/account-secret"
chmod 600 "$tmp/account-secret"
target/release/bianbu-agent-memoryd \
  --development-identity \
  --socket "$tmp/memoryd.sock" \
  --database "$tmp/memory.db" \
  --key-package "$tmp/memory.keypkg" \
  --account-secret-file "$tmp/account-secret" \
  --initialize-key-package \
  --background-interval-seconds 3600
```

另开终端设置：

```bash
export PYTHONPATH=/home/example/test/build-memory-current-*/src/sdk/python/src
SOCKET=/tmp/agent-memory-test.xxxxxx/memoryd.sock
```

### 3.2 基础 API 用例

| 编号 | 操作 | 步骤 | 预期结果 |
| --- | --- | --- | --- |
| MEM-API-01 | `system.hello` | SDK 首次连接 | 返回固定 `account_id`、`agent_id/agent_name`、`memory_space_id` 和 capability |
| MEM-API-02 | 首帧约束 | 未发送 `system.hello` 直接调用普通方法 | 返回未绑定/认证错误，服务不崩溃 |
| MEM-API-03 | 健康检查 | `client.health()` | `status=ok`、`database=ok`、`storage_encryption=sqlcipher` |
| MEM-API-04 | 统计遥测 | `client.stats()` | 返回 session、summary、L3、outbox、tombstone、projection 和存储字节数 |
| MEM-API-05 | 无效标识 | 传入空、超长或非法 `agent_name` | 返回参数错误，不写入数据库 |
| MEM-API-06 | 幂等写入 | 同一 `idempotency_key` 重复发送相同请求 | 返回同一结果，不新增记录 |
| MEM-API-07 | 幂等冲突 | 同一 key 发送不同参数 | 返回冲突，不覆盖原记录 |
| MEM-API-08 | 超大帧 | 发送超过 256 KiB 的 JSON frame | 返回帧大小错误或断开，不影响后续连接 |

### 3.3 L1/L2/L3 记忆用例

| 编号 | 场景 | 测试步骤 | 预期结果 |
| --- | --- | --- | --- |
| MEM-LIFE-01 | 完整轮进入 L1 | 同一 `run_id` 写 user + completed assistant，再读取 context | `messages` 包含一对完整消息 |
| MEM-LIFE-02 | 中断轮过滤 | 写入 failed/cancelled/timeout 或孤立 user 事件，再读取 context | 事件可审计，但不进入 L1/L2/L3 可召回消息 |
| MEM-LIFE-03 | 最近 5 个 session | 创建超过 5 个完整 session，调用 `get_recent_context(selection_mode=recent_sessions)` | 只返回最近 5 组，按成功完成轮次排序 |
| MEM-LIFE-04 | 历史分页 | 调用 `get_history`，改变 limit/offset 或 cursor | 分页稳定，不重复、不丢失 |
| MEM-LIFE-05 | 五轮自动摘要 | 完成 5 个完整轮次后读取 session context | 自动出现 1 个版本化 L2 Summary，原始事件保留 |
| MEM-LIFE-06 | 摘要失败保护 | 注入失败的摘要 Planner 或非法响应 | 原始事件保留，不删除、不伪造摘要成功 |
| MEM-LIFE-07 | 摘要响应校验 | 运行 `scripts/summary_quality_acceptance.py summary.json --expect <事实>` | 字段、长度、空补丁、凭证样式和预期事实校验通过；非法输入失败 |
| MEM-LIFE-08 | TTL 归档 | 将测试时钟推进超过 7 天，运行 `maintenance.run` | 稳定摘要/重要事实归档 L3，普通历史过期清理 |
| MEM-LIFE-09 | L3 新增和召回 | `long_term.put` 后 `search_headers` + `get_many` | 检索结果回查 canonical，namespace 和 revision 正确 |
| MEM-LIFE-10 | L3 修订 | 同 slot 写旧值，再用新值 revise/upsert | 同一 slot 只有当前权威值，旧 revision 失效 |
| MEM-LIFE-11 | revision 冲突 | 使用过期 `expected_revision` revise | 返回 `Conflict`，原值不变 |
| MEM-LIFE-12 | Tombstone 不复活 | Forget 后再次 FTS/语义检索、重建索引 | 已删除值不可见，派生索引不得复活 |
| MEM-LIFE-13 | Embedding 降级 | 停止 Embedding/Projection provider 后查询 | 回退 FTS/关键词，canonical 读写仍正常 |
| MEM-LIFE-14 | Checkpoint | prepare、list、restore checkpoint | 返回事件序号/摘要 revision/水位；可读取锚点后的变化 |
| MEM-LIFE-15 | 字段值校验（MEM-L3-03） | 现场脚本 `scripts/k3_field_value_gate.py`；显式带 `slot` 分别写入 `深色`、`unknown-manager`、65 字符的 shell、以及一句叙述文本 | 别名归一为 `dark` 落库；域外值/超长值/叙述文本返回 `invalid_value`（带 `allowed_values`、`max_length`、`assistant_instruction`）且不落库；同一叙述文本不带 slot 仍可写入 |

## 4. 隐私、安全和配置测试

### 4.1 加密、权限和敏感数据

| 编号 | 用例 | 步骤 | 预期结果 | 当前状态 |
| --- | --- | --- | --- | --- |
| SEC-01 | SQLCipher 拒绝明文 | 用普通 SQLite 打开加密 `memory.db` | 查询 schema 失败；不得泄露明文 | 已验证 |
| SEC-02 | Secret 权限 | 检查 `account-secret`、`memory.keypkg`、Capsule key 和配置文件 | Secret/key/config 为最小权限，测试基线为 `0600` | 已验证（仍需生产安全复核） |
| SEC-03 | 凭证过滤 | 输入 password/token/API key/private key 文本 | 不进入 L3、摘要、D+、普通日志或审计详情 | 已验证 |
| SEC-04 | PII 边界 | 输入姓名、电话、邮箱、地址 | 仅允许加密 canonical 路径；不进入语义投影 | 已验证 |
| SEC-05 | 明文迁移 | 停止 daemon，执行 `--migrate-plaintext`，检查 staging/sidecar/backup；再跑 `scripts/k3_plaintext_replica_sweep.py` 扫全盘明文副本 | 生成加密库；暂存文件可清理；回滚副本默认保留且为 `0600`；全盘无明文 `memory.db*` 与明文导出 | 已验证（K3 历史副本已现场清理，见 §4.3） |
| SEC-06 | 精确清理 | 执行 `--cleanup-plaintext-backups` 和 `--cleanup-migration-temporaries` | 只删除当前数据库关联的目标文件，不删除无关文件 | 已验证 |
| SEC-07 | ACL | `getfacl /run/bianbu /run/bianbu/agent-runtime` | Runtime/Memory 具备所需最小访问，owner/mask 不被错误放宽 | 已验证基础 ACL |
| SEC-08 | 清除历史 | 使用正确 confirm token 调用 `privacy.clear_history` | L1/L2/Session checkpoint 清除，L3 保留，并写审计 |
| SEC-09 | 清除全部错误令牌 | 使用错误 token 调用 `privacy.clear_all` | 返回 `PermissionDenied`，数据不变 |
| SEC-10 | 清除全部正确令牌 | 明确备份后调用 `privacy.clear_all` | 所有本地记忆清除，Tombstone/审计结果符合契约 |

### 4.2 配置和门控

| 编号 | 用例 | 步骤 | 预期结果 |
| --- | --- | --- | --- |
| CFG-01 | 默认配置 | `privacy.settings.get` | memory/audit 默认开启，永久保留 wire 值为 `retention_days=0`，云备份关闭 |
| CFG-02 | 配置原子写入 | 更新 retention、audit、summary、quota，再杀进程并重启 | 配置完整保留，不出现半文件；文件权限正确 |
| CFG-03 | 记忆开关 | 更新 `memory_enabled=false` 后读写 | 返回 `MemoryDisabled`/fail-closed；重新启用后恢复 |
| CFG-04 | 保留周期校验 | 设置 7/30/90/0 及非法值 | 合法值成功，非法值拒绝 |
| CFG-05 | 存储配额 | 设置数据库、Capsule 和最低剩余空间阈值，制造超限条件 | 写入被拒绝并返回 `ResourceExhausted`；清理操作仍可执行 |
| CFG-06 | 云备份门控 | default/local 账号、无授权、无 key、无 Adapter 分别调用备份 | `account_not_authenticated`/`BackupUnavailable`，不得上传 |

### 4.3 记忆空间残留与明文副本现场收口（MEM-L3-02 / SEC-05）

四个现场脚本都直连设备（现网守护进程 + 只读全库清册），都不启动测试实例，判定全部来自词典或
规格、不做主观判断。以下命令在 K3（`192.0.2.10`）执行并留下 JSON 证据。

```bash
cd /home/example/mem-gateway-build

# 1) 全库命名空间与槽位合规清册（MEM-L3-02 的“体检”，只读）
echo bianbu | sudo -S python3 scripts/k3_namespace_inventory.py
#    期望 K3_NAMESPACE_INVENTORY_OK

# 2) 词典外槽位的历史行收口（只 forget 命中这一条判据的行）
echo bianbu | sudo -S python3 scripts/k3_memory_namespace_cleanup.py \
  [--apply] [--prune-registrations]
#    期望 K3_NAMESPACE_CLEANUP_OK

# 3) 用户 Runtime 空间的残留自检（MEM-L3-02 / MEM-L3-03）
echo bianbu | sudo -S python3 scripts/k3_memory_residue_report.py
#    期望 K3_MEMORY_RESIDUE_OK

# 4) 明文记忆副本现场清理（SEC-05）
echo bianbu | sudo -S python3 scripts/k3_plaintext_replica_sweep.py [--apply]
#    期望 K3_PLAINTEXT_REPLICA_SWEEP_OK
```

- 第 1、2 步依赖 `bianbu-agent-memoryd --report-namespaces`：它是设备上唯一能列出**全部**
  memory namespace 的口子（协议里每个方法只作用于调用者自己的 space，退役测试空间、
  探针空间、`runtime-context.*` 对任何身份都不可见）。该子命令以只读方式打开库，
  不迁移、不改写，可在服务运行期间执行。
- 第 2 步走 `long_term.forget`（Tombstone + outbox 同步语义），不直接改 SQL —— 否则云端旧
  备份会把删掉的行“复活”；`reason_code=l3_slot_gate_cleanup`。同一个脚本还会列出
  `memory-registry` 里已死进程留下的注册文件（`--prune-registrations` 时一并删除），
  它们既不参与判定也不影响 MEM-L3-02 的结论。
- 第 4 步的明文判定：文件名属于 `memory.db` 家族且头 16 字节是 `SQLite format 3`
  （SQLCipher 库没有这个头），或 JSON 导出里真的含有记忆集合。现网库、`capsules/`、
  key/secret 与任何**正被进程打开**的文件一律跳过；`--apply` 后脚本重新扫描复核。

**K3 现场结果（2026-09-23，本地开发包 —— 开发期编号 0.1.20，非发布版本）**

| 检查 | 结果 |
| --- | --- |
| 命名空间清册 | 18 个空间 / 111 条 L3 / 4 个词典外槽位 → 收口后 **18 / 107 / 0**，`K3_NAMESPACE_INVENTORY_OK` |
| 越界槽位行 | 4 条（`test.marker`、`workflow.deploy_action`、`development.example_code_language`、`development.python_dependency_manager`，全在验收脚本留下的退役空间里；用户 Runtime 空间本就合规）→ `K3_NAMESPACE_CLEANUP_OK`，另清掉 11 个死进程的注册文件 |
| 用户空间残留 | `K3_MEMORY_RESIDUE_OK`（6 条 L3 / 347 事件 / 209 摘要 / 8 tombstone）；tombstone 是 forget 的同步记录，属预期，不清理 |
| 明文副本 | 删除 77 个 `memory.db*` 家族文件（28 个库本体 + 24 个 `-wal` + 24 个 `-shm` + 1 个 `memory.db.plaintext-backup-*` 回滚副本；含 `memfix/*`、`memv2/*`、`memverify/*`、`qcheck-iso-*`、`dplus-*` 隔离栈）与 21 个明文 JSON 导出（`/home/example/Downloads`、回收站、`/tmp`），共 98 个文件 → `K3_PLAINTEXT_REPLICA_SWEEP_OK` |
| 明文副本复核 | 全盘（除 `/proc`、`/sys`、`/dev`）再无头 16 字节为 `SQLite format 3` 的 `memory.db*`；内容级扫描只剩仓库里含 `CREATE TABLE long_term_memories` 字样的 Python 脚本 |

## 5. K3 安装、systemd 和 Runtime 联调

### 5.1 DEB 安装检查

安装由流水线或 K3 本地构建产生的两个包：

```bash
sudo apt install --reinstall \
  ./bianbu-agent-memory_0.1.6_all.deb \
  ./bianbu-agent-memoryd_0.1.6_riscv64.deb
sudo systemctl daemon-reload
sudo systemctl enable --now bianbu-agent-memoryd.service
```

| 编号 | 用例 | 命令/步骤 | 预期结果 |
| --- | --- | --- | --- |
| K3-PKG-01 | 包名和版本 | `dpkg-query -W -f='${Package} ${Version} ${Status} ${Architecture}\n' bianbu-agent-memory bianbu-agent-memoryd` | SDK 包为 `bianbu-agent-memory`，daemon 包为 `bianbu-agent-memoryd`，版本一致 |
| K3-PKG-02 | SDK 导入 | `python3 -c 'import bianbu_agent_sdk.memory'` | 导入成功 |
| K3-PKG-03 | postinst 初始化 | 查看 `/etc/bianbu-agent-memory`、`/var/lib/bianbu-agent-memory` | key、secret、database/capsule 目录已创建，权限符合要求 |
| K3-PKG-04 | 服务启停 | `systemctl is-enabled/is-active bianbu-agent-memoryd.service` | 安装后 enabled/active；停止和启动可重复 |
| K3-PKG-05 | UDS | `test -S /run/bianbu/agent-memoryd.sock`、`ss -lxnp` | socket 存在且由 memoryd 监听 |
| K3-PKG-06 | 服务重启 | `sudo systemctl restart bianbu-agent-memoryd.service` | 服务恢复，数据库和记忆不丢失 |
| K3-PKG-07 | Runtime 包 | 安装 Runtime DEB 后检查其 systemd unit 和依赖 | Runtime 能启动并注册 registry；若依赖包缺失，必须在安装日志中明确失败 |

### 5.2 Runtime Hook 和身份

Runtime 启动后：

```bash
curl -fsS http://127.0.0.1:9899/api/health
curl -fsS http://127.0.0.1:9899/api/v1/runtime/status
ls -l /run/bianbu/agent-runtime/memory-registry
```

| 编号 | 用例 | 测试步骤 | 预期结果 |
| --- | --- | --- | --- |
| K3-RT-01 | Runtime 健康 | `GET /api/health` | 返回 `AGENT_READY` |
| K3-RT-02 | Memory registry | 查看 registry JSON | 含可信 `account_id`、`agent_id/agent_name`、PID、启动时间和 capability |
| K3-RT-03 | `before_run` | 新建会话并询问已保存偏好 | Runtime 从 L1/L2/L3 召回，回答包含正确值 |
| K3-RT-04 | `after_run` | 完成一轮对话后查询 Memory | 只写入成功完整 user/assistant 轮，带 run/session 标识 |
| K3-RT-05 | 失败 Run | 制造超时、取消或工具异常 | 写入 `task_failed` 恢复元数据和 checkpoint，但不进入可召回消息 |
| K3-RT-06 | 身份不可伪造 | 请求 metadata 携带不同 account/agent | Memory 使用 Runtime registry 身份并拒绝覆盖 |
| K3-RT-07 | 多 Agent 隔离 | Agent-A 写入“只属于 A”，Agent-B 查询同一关键词 | B 不可读取 A 的数据；各自 revision/配置独立 |
| K3-RT-08 | 多账号隔离 | 使用两个 account namespace 重复上述测试 | 账号之间不可互读 |
| K3-RT-09 | 关闭 Memory | 角色去掉任一 Memory Hook 或显式关闭环境变量 | 工具返回 `memory_disabled`，不应误报写入成功 |
| K3-RT-10 | memoryd 不可用 | 停止 memoryd 后执行写入 | 返回 `backend_unavailable`/`Unavailable`，不得伪造成功；恢复服务后可重试 |
| K3-RT-11 | 重启召回 | 重启 memoryd 和 Runtime，再询问历史偏好 | L3 和允许恢复的 L1/L2 正确召回 |
| K3-RT-12 | 标题隔离 | 触发 Runtime 自动标题生成 | 不新增 Memory Session Event 或长期记忆 |

推荐自动化联调：

```bash
PYTHONPATH=/home/example/bianbu-agent-memory/sdk/python/src \
python3.13 /home/example/bianbu-agent-memory/scripts/k3_runtime_memory_regression.py \
  --base-url http://127.0.0.1:9899 \
  --database /var/lib/bianbu-agent-memory/memory.db
```

预期：`K3_RUNTIME_MEMORY_REGRESSION_OK`。如果使用旧的手测 9900 栈，应把 `--base-url` 和数据库路径替换为该栈实际值。

### 5.3 D+、Embedding 和 Mem0

```bash
PYTHONPATH=/home/example/bianbu-agent-memory/sdk/python/src \
python3.13 scripts/k3_dplus_runtime_regression.py \
  --base-url http://127.0.0.1:9899 \
  --dplus-url http://127.0.0.1:18920 \
  --database /var/lib/bianbu-agent-memory/memory.db
```

预期 `K3_DPLUS_RUNTIME_REGRESSION_OK`：新值覆盖旧 slot、同义问题召回新值、active mapping 为 1、dead/retry 为 0。Provider 不可用时另执行 `Embedding` 降级用例，确认 FTS 仍可用。

## 6. Capsule、同步和 DBus

| 编号 | 用例 | 步骤 | 预期结果 | 当前状态 |
| --- | --- | --- | --- | --- |
| BK-01 | 本地 Capsule 创建 | 配置 capsule key 和 local adapter，调用 `backup.create` | 仅包含授权范围内 L2/L3，gzip + AES-GCM，manifest/hash 完整 |
| BK-02 | Capsule 预览 | `backup.preview_restore` | 只读展示 revision/HLC/Tombstone 冲突，不修改数据 |
| BK-03 | Capsule 恢复 | 通过确认令牌调用 `backup.restore` | 幂等合并、Tombstone 优先、重建索引、写审计 |
| BK-04 | 重复恢复 | 重复相同 restore 请求 | 不产生重复记录或 revision 漂移 |
| BK-05 | 篡改检测 | 修改 Capsule 内容或 manifest | 完整性校验失败，不导入 |
| BK-06 | API-Key HTTP 适配器 | 使用本地 HTTPS/Mock 文件 API 配置 URL 和 key | 携带 `X-API-Key`，只上传密文，下载/删除同源对象通过 |
| BK-07 | default 账号门控 | default 账号调用 `backup.run_scheduled` | 返回未认证/拒绝，不创建云端上传任务 |
| BK-08 | Outbox | authenticated account 写入 account-agent scope 记录后查询 `sync.status` | 生成 metadata-only outbox；无真实 key 时 payload 为空，不伪造密文 |
| BK-09 | DBus Call | 启动 `--dbus`，调用 `com.bianbu.AgentMemory1` | 返回与 UDS 等价的协议响应；普通 Agent 仍优先使用 SDK/UDS | 已验证（2026-09-23 K3 现网 11/11 门禁：发现接口 / 未注册调用者被拒 / 注册后 `system.health`+`system.stats` 成功 / 协议版本不符 / `system.hello` / 超 4 MiB 帧 / 非 JSON / 注销后重新被拒；脚本 `scripts/k3_dbus_acceptance.py`，证据 `artifacts/k3-dbus-acceptance.json`，契约见 `docs/DBUS_CONTROL_PLANE.md`。未注册的 `busctl`/`bianbu-agent-memory` CLI 仍被拒，D-Bus 不构成运维入口） |

真实云服务、正式 OAuth2/API-Key、Secret Broker、第二设备和跨设备恢复必须使用真实资源执行；Mock 契约通过不能标记云端规格完成。

## 7. 性能、恢复和长期稳定性

### 7.1 基线性能

```bash
PYTHONPATH=/home/example/bianbu-agent-memory/sdk/python/src \
python3.13 scripts/k3_benchmark.py /run/bianbu/agent-memoryd.sock \
  --count 300 --prefix "manual-$(date +%s)"
```

记录 health、put、search、get_many 的 P50/P95/P99、吞吐和 memoryd RSS。该脚本是基线，不替代正式门槛。

### 7.2 正式容量/并发门槛

```bash
PYTHONPATH=/home/example/bianbu-agent-memory/sdk/python/src \
python3.13 scripts/k3_scale_recovery_acceptance.py \
  --memoryd target/release/bianbu-agent-memoryd \
  --records 100000 --clients 32 --write-clients 32 \
  --query-samples-per-client 10
```

必须同时满足：

- 10 万条记录全部写入且无写错误；
- 32 并发客户端查询 + canonical 回表 P95 ≤100 ms；
- SIGKILL 后 health 正常、记录数不变、抽样记录可读；
- 重启后的冷/热查询结果和性能均符合门槛。

当前已知小规模临时库 P95 约 12 ms；历史 10 万条/32 客户端测试未达到 100 ms，因此该项仍须单独验收。

### 7.3 语义检索（MEM-NFR-02）

| 编号 | 用例 | 步骤 | 预期结果 | 当前状态 |
| --- | --- | --- | --- | --- |
| VEC-01 | 禁用态回归 | 显式关闭 embedding（设置页开关或 `AGENT_MEMORY_EMBEDDING_DISABLED=1`）跑全部既有测试与基线脚本 | 行为与旧版本一致；`memory_embeddings` 表存在但为空。0.1.9 起默认开启，本用例改为显式禁用态回归（历史记录为默认禁用时代跑出） | 已验证（K3 第 12 轮构建 cargo test 全绿 + 设备 smoke；0.1.9 改动后待复跑） |
| VEC-02 | 写入即嵌入 | 启用 embedding 后 `long_term.put` 一条 | `system.stats` 的 `memory_embeddings` 计数 0→1；`forget` 后 1→0 | 已验证（K3 设备 smoke） |
| VEC-03 | 语义召回 | 写"深色主题配色"类记忆，用无共享词改写查询检索 | 向量分支命中；禁用 embedding 同查询不命中（A/B 对照） | 已验证（M6 金标集同义改写 0.45→0.90；M7 e2e FTS rank=0 vs 语义 rank=1） |
| VEC-04 | 静默降级 | 停 llama-server 后持续检索 | 无错误码，结果回退 FTS；恢复后后台任务（60s）自动补齐向量 | 已验证（M5 复跑 fallback 400 查询零错误；M7 e2e 停服召回成功） |
| VEC-05 | secret 不嵌入 | 写 `sensitivity=secret` 记忆 | 不产生向量行；检索不返回 | 已有单测（M6 顺带实测：4 条含密钥特征词语料按设计排除） |
| VEC-06 | 模型切换重建 | 更换 `model`/`dims` 配置 | 旧向量判 stale（检索不可见），后台任务按新配置重建 | 已有单测 |
| VEC-07 | 十万条门控 | `scripts/k3_vector_perf_acceptance.py --records 100000 --clients 8 --ops 400` | 8 并发热/冷两相 search P95 ≤ 2000ms；降级路径零错误 | 已验证（M5 复跑 2026-09-10：热 1594 / 冷 1601ms PASS，基准文档 §7.8） |
| VEC-08 | 检索质量与前缀 A/B | `scripts/k3_embedding_quality_eval.py` | recall@5(instruct) ≥ fts + 0.15 且 ≥ noinstruct；失败案例转储归档 | 已验证（M6 2026-09-10：增益 +0.30 PASS、前缀持平，基准文档 §7.4） |
| VEC-09 | Runtime e2e | `BIANBU_MEMORY_ENGINE=native` 隔离栈（`scripts/k3_semantic_e2e_stack_start.sh` 起 9898 的 Runtime + 开发态 memoryd）+ `scripts/k3_semantic_e2e_runtime.py`（测试专家） | 改写查询硬断言 top-1 + Runtime 回答含标记；停服后 FTS 回退可用 | 已验证（0.1.19，2026-09-23 K3 连跑两次全 PASS：改写查询 rank 1 而关 embedding 时 rank 0、Runtime 回答含标记、unload 后 FTS 回退可用且按钉死参数恢复；见 `docs/GATEWAY_EMBEDDING_INTEGRATION.md` §8.6） |
| VEC-10 | Provisioning 单元 | `bianbu-agent-memory-embedding-provision` 在 gateway 缺失 / 模型 id 未知时的退出码；`POST /v1/embed/models/unload` 卸载模型后跑查询；重跑该单元 | gateway 缺失或模型 id 未知 → 记一行日志、退出 0、不阻塞 boot；卸载模型不构成中断（下一个指名请求由 gateway 自动加载，冷启动约 6 s），gateway 整体不可达时 memoryd 存活并静默回退 FTS；重跑该单元把参数恢复成钉死取值 | 已验证（0.1.17，2026-09-22 K3：缺 gateway / 未知模型均退出 0；unload 0.33s 且 `bibit-embed` 不受影响；auto-load 6.3s；provisioner 端到端 parity 1.000000，RSS 4.98GB→1.78GB） |

Rust 单元/集成测试覆盖：int8 量化余弦精度（Δ≤0.01）、RRF 双证据优先与 tie-break、
HTTP 客户端解析/重组/错误映射/超时、命名空间隔离、stale 排除与后台补齐、
embedding 配置校验（https 拒绝、缺端点拒绝、dims 越界拒绝）。

### 7.4 可靠性和升级

| 编号 | 用例 | 步骤 | 预期结果 | 当前状态 |
| --- | --- | --- | --- | --- |
| REL-01 | SIGKILL 恢复 | `kill -9 memoryd` 后重新启动 | WAL 恢复，无损坏、无重复、无半轮可召回消息 | 已验证 |
| REL-02 | Runtime 崩溃续跑 | 在 Run 中终止 Runtime，重启后检查 checkpoint/ON_ERROR | 失败 Run 有恢复元数据，成功续跑不重复写入 | Memory 侧已验证，Runtime 联合验收需执行 |
| REL-03 | systemd 自动恢复 | 反复 `systemctl restart`，观察 `NRestarts` 和日志 | 服务稳定，无持续启动竞态或权限错误 | 基础重启已验证 |
| REL-04 | 模式升级迁移 | 从旧 schema/旧 DEB 升级到当前版本 | 自动迁移成功，备份可回滚，旧数据可读 | 0.1.16→0.1.17 原地 DEB 升级已在 K3 执行（2026-09-22，见 `docs/GATEWAY_EMBEDDING_INTEGRATION.md` §8.2）：装包退出码 0、旧数据可读、配置收敛到网关端点、旧 unit 干净退场；本次 schema 未变（仍为 12），跨 schema 迁移验收仍需执行 |
| REL-05 | 回滚 | 安装上一版本 DEB，恢复数据库备份 | 回滚后数据完整且服务可用 | 需执行 |
| REL-06 | 真实断电 | `scripts/k3_power_loss_test.py --trigger`：`echo b > /proc/sysrq-trigger`（内核立即重启、不做 sync，等效硬掉电）→ 重启后自动比对 boot_id、schema、key package 可用性、标记记忆文本 sha256、命名空间行数、临时文件与 journal | 无损或达到规定恢复率，临时文件可清理 | 脚本已就位（2026-10-09 真断电跑通 PASS：20/20 标记记忆恢复、文本 sha256 一致、行数回到基线；驱动在重启后会先等库自检出结论再采 post）。真实市电拔插需现场受控操作；软件等效掉电会硬重启整台 K3，执行前须确认设备上没有其它在跑的任务 |
| REL-07 | 资源限制 | 观察 cgroup Memory/Tasks/CPU、磁盘配额和满盘行为 | 超限时可预测拒绝，服务不拖垮系统 | 需执行生产纳管验收 |
| REL-08 | SBOM/CVE | 生成发布 SBOM，扫描 Rust/Python/系统依赖 | 无阻断级漏洞，结果归档到发布记录 | 需执行 |
| REL-09 | 七天稳定性 | `scripts/k3_memory_soak.py start`（systemd 采样：60 s 一次 health/stats/RSS/FD/库与 WAL 体积；30 min 一次写入+检索+forget 扰动；24 h 一次计划内重启）→ `verdict` 出验收 JSON | 无内存泄漏、死锁、数据增长失控或错误风暴 | 工具已就位并自测通过（2026-09-23：扰动 put≈90–105 ms / search≈170–185 ms / forget≈7 ms，计划内重启记账正常）；7×24 窗口须等设备无其它任务占用后开始计时 |

## 8. 测试结果记录模板

每次测试建议保存以下信息：

```text
日期/操作者：
Memory commit/tag：
Runtime commit/tag：
K3 系统版本/内核：
DEB 文件及版本：
数据库 schema_version：
测试用例编号：
命令和参数：
实际结果：
日志/JSON 输出路径：
结论：PASS / FAIL / BLOCKED
失败原因和关联 issue：
```

汇总规则：

- 自动化测试通过只说明代码契约通过；
- K3 真实服务、目标包安装和规定指标全部满足，才可标记规格“已完成”；
- 外部 Security、BiBit、AI 模型、云服务、第二设备和长期稳定性缺失时，标记“进行中”；
- 任何 `backend_unavailable`、权限错误或服务未启动都必须记录为失败/阻塞，不能当作空结果。
