# Bianbu Agent Memory

**让 Agent 记住对话、接上工作，不再每次都从零开始。**

[English](README.md) | [简体中文](README_CN.md)

嗨，这里是 Bianbu Agent Memory，也就是 Bianbu Agent OS 的“记忆模块”。

你可以把它想成 Agent 随身携带的三个抽屉：

- **L1 会话抽屉**：刚刚聊了什么、这一轮做完了什么；
- **L2 工作抽屉**：把一段对话压缩成结构化摘要，方便下次快速接上；
- **L3 长期抽屉**：用户偏好、开发习惯、重要标记，以及支撑它们的证据和版本。

它住在设备本地，权威数据也在本地。基础存储和全文检索不需要任何模型服务；
想要更聪明的语义召回或模型摘要，再按需接上 Embedding / Mem0。

## 先看架构：谁在干活？

```text
┌─────────────────────────────────────────────┐
│ Agent / 宿主应用                             │
│ Hermes · DeepSeek Harness · BiBit · 你的应用 │
└──────────────────┬──────────────────────────┘
                   │ SDK / CLI / MCP / Runtime Bridge / Adapter
                   ▼
        ┌──────────────────────────┐
        │ bianbu-agent-memoryd     │
        │ Rust 守护进程（UDS/D-Bus）│
        └────────────┬─────────────┘
                     │
      ┌──────────────┼───────────────┐
      ▼              ▼               ▼
 SQLite/SQLCipher  FTS5 全文索引   可选：Embedding / Mem0
 权威记忆 L1/L2/L3  （随写随查）     语义投影（可重建）
```

一句话分工：

- **客户端**负责“说”：SDK、CLI、MCP、Runtime Bridge 和宿主适配器；
- **memoryd** 负责“记”和“找”：落盘、检索、保留期、导出导入、审计；
- **索引**负责“猜你想找什么”：FTS5 是基本盘，Embedding / Mem0 是加分项。

> 名词说明：Runtime 指 Bianbu Agent Runtime（设备上的 Agent 运行时）；
> Runtime Bridge 是它在进程内调用记忆服务的桥接层。

索引永远只是派生数据。语义召回命中后，还要回到权威记录取当前版本，
所以向量库里返回的文本不会被直接塞进模型上下文。

要自己写 Agent 或接入现有宿主，从[对接 API](docs/INTEGRATION_API.md)开始。

## 我能做什么？

| 能力 | 说人话 |
| --- | --- |
| **分层记忆** | L1 记对话事件与完整轮次，L2 记工作摘要，L3 记长期偏好、习惯和重要标记 |
| **找得回来** | 本地 FTS5 全文检索 + 有界读取；可选语义召回，失败自动回退词法 |
| **记得安全** | SQLCipher 加密、事务写入、幂等回执、WAL、存储配额 |
| **分得清楚** | 可信 Runtime 身份、能力令牌（Capability）校验、账号 / Agent / 记忆空间隔离 |
| **管得住** | 保留期、隐私设置、清除历史 / 全部、JSON 导出导入、审计、遗忘（Forget）、检查点 |
| **接得上** | Python 同步/异步 SDK、CLI、MCP、Runtime Bridge，以及 Hermes、DeepSeek Harness、BiBit 适配器 |

几个容易混淆的点，先划重点：

- **SDK 不是服务**：`pip install` 只装客户端，真正干活的是 `bianbu-agent-memoryd`。
- **导入是合并，不是覆盖**：同一份文件重复导入会按原 id 跳过，不会复制一份。
- **导出是全量**：单页最多 500 条，SDK 会自动翻页合并，导出保留期内全部数据。
- **删除靠 Tombstone**：权威记录和墓碑决定可见性，索引删不删都不影响最终结果。

## 代码都在哪？

| 目录 | 它是干什么的 |
| --- | --- |
| `crates/memory-core` | 记忆的领域模型和核心规则，先从这里理解“记忆长什么样” |
| `crates/memory-store` | SQLite/SQLCipher 落盘、检索、保留期、导出导入 |
| `crates/memory-protocol` | 请求、响应和错误码，客户端与服务端的共同语言 |
| `crates/memoryd` | `bianbu-agent-memoryd` 守护进程，UDS 服务与 D-Bus 控制面 |
| `sdk/python` | Python SDK、CLI、MCP 服务、Runtime Bridge、BiBit Adapter |
| `adapters/hermes` | Hermes v0.21.7 的外部记忆 Provider |
| `adapters/deepseek-harness` | DeepSeek Harness 0.1.0-rc.6 的 Cordis 插件（宿主生命周期框架）与 Python Bridge |
| `packaging` | systemd 单元、启动脚本、D-Bus 策略 |
| `debian` | Debian 打包元数据 |
| `migrations` | 数据库 Schema 迁移 |
| `scripts` | 现场验收和诊断脚本 |
| `docs` | 协议、配置、集成、测试等文档 |

第一次读代码，建议按这个顺序：`memory-core` → `memory-store` → `memoryd` →
`sdk/python`。想先跑起来，直接跳到下一节。

## 编译安装

### 方式一：从 Bianbu 软件源安装

设备已经配置好 Bianbu 软件源时，一行搞定：

```bash
sudo apt update
sudo apt install bianbu-agent-memory bianbu-agent-memoryd
```

- `bianbu-agent-memory`：Python SDK + CLI；
- `bianbu-agent-memoryd`：记忆守护进程。

安装会自动创建服务账号、密钥包、默认配置和 systemd 服务，并把守护进程拉起来。
如果设备使用 Bianbu Runtime / BiBit 的内置记忆集成，Runtime 会完成可信身份注册，
通常不需要手工改配置。自定义程序直接用 SDK/CLI 连接时，才需要开发身份或由 Runtime
注册进程，否则会被拒绝。只有要修改保留期、摘要模型、Embedding、配额等非默认策略时，
才需要翻[配置指南](docs/CONFIGURATION.md)。

### 方式二：从源码编译

面向 Linux。已验证 riscv64 + Rust 1.93.1 + Python 3.14.4。
`Cargo.lock` 锁定的依赖实际要求 **Rust 1.87+**（zbus 5.19 使用 edition 2024），
工作区自身声明的 `rust-version = 1.80` 不足以完成构建。另需 Python 3.10+、
C 工具链、pkg-config 和 OpenSSL 开发依赖（bundled SQLCipher 会链接 OpenSSL）。

以下命令都在仓库根目录执行；K3 上首次 release 构建需要数分钟。

```bash
sudo apt install build-essential pkg-config libssl-dev \
  python3-venv python3-dev libffi-dev

cargo build --workspace --release --locked

python3 -m venv .venv && . .venv/bin/activate
python -m pip install -e 'sdk/python'
```

## 五分钟跑起来

开发实例用临时数据库和开发身份，**别写真实个人数据**。

### 第一步：把服务拉起来

```bash
umask 077
export BIANBU_MEMORY_DEV_DIR="$(mktemp -d /tmp/bianbu-memory-dev.XXXXXX)"
echo "$BIANBU_MEMORY_DEV_DIR"

target/release/bianbu-agent-memoryd \
  --development-identity \
  --allow-plaintext \
  --socket "$BIANBU_MEMORY_DEV_DIR/agent-memoryd.sock" \
  --database "$BIANBU_MEMORY_DEV_DIR/memory.db" \
  --background-device-id dev-local
```

### 第二步：写一条、再找回来

另开一个终端，激活虚拟环境，并 export 第一步打印的目录（例如
`export BIANBU_MEMORY_DEV_DIR=/tmp/bianbu-memory-dev.abcd1234`）：

```bash
python - <<'PY'
import os
from pathlib import Path
from bianbu_agent_sdk.memory import MemoryClient, MemoryKind

sock = Path(os.environ["BIANBU_MEMORY_DEV_DIR"]) / "agent-memoryd.sock"
with MemoryClient.connect(sock, timeout=2.0,
                          account_id="default", agent_name="default-agent") as client:
    receipt = client.long_term.put(
        kind=MemoryKind.PREFERENCE,
        text="我喜欢深色终端主题。",
        summary="终端主题偏好：深色",
        idempotency_key="readme:terminal-theme:v1",
    )
    print("已保存：", receipt.memory_id, receipt.revision)

    for header in client.long_term.search_headers("终端", top_k=5):
        record = client.long_term.get_many([header.memory_id])[0]
        print(record.memory_id, record.text)
PY
```

幂等键是“这次逻辑写入”的身份证：重试复用，换内容就换新键。

### 第三步：看一眼健康状态

```bash
bianbu-agent-memory --socket "$BIANBU_MEMORY_DEV_DIR/agent-memoryd.sock" health
```

刚启动时可能看到 `status: unknown` 或 `database_check.state: busy`，
这通常只是数据库自检还没跑完，不代表读写失败。最靠谱的验证还是第二步：
刚写进去的记忆能不能找回来。按 Ctrl+C 停止服务即可。

## 对接 Hermes

Hermes 的接入方式很“规矩”：记忆是一个**外部 Provider**，不改 Hermes 源码。
当前适配 Hermes `v0.21.7`（验证提交 `71d34bad1045610252eda02ee79549436896e784`）。

### 三步装上

```bash
# 1. 安装 SDK
python -m pip install /path/to/bianbu-agent-memory/sdk/python

# 2. 放插件
mkdir -p "$HERMES_HOME/plugins/bianbu"
cp /path/to/bianbu-agent-memory/adapters/hermes/__init__.py \
  "$HERMES_HOME/plugins/bianbu/__init__.py"

# 3. 在 Hermes 的 Provider 配置里选择 bianbu
```

注意两点：

- `bianbu-agent-memoryd` 必须先跑起来，并且 Hermes 进程能被 Runtime 注册表解析到；
- 只是本地开发时，可以把 `BIANBU_AGENT_MEMORY_SOCKET` 指向临时 daemon 的 socket。

### 它会在什么时候记？

| Hermes 的时机 | 记忆模块的动作 |
| --- | --- |
| `initialize` | 握手、确认不可变身份 |
| `system_prompt_block` | 提供当前生效的指令记忆（Instruction Memory） |
| `prefetch` | 召回相关记忆；当前查询缓存未命中最多等 2 秒，超时返回空 |
| `sync_turn` | 一轮完成后自动写入事件、抽取、摘要/检查点 |
| `on_memory_write(add/replace/remove)` | 显式保存、版本校验更新、待确认删除 |
| `on_pre_compress` | 压缩前保存结构化摘要和上下文指针 |
| `on_session_switch` | 切换会话并让旧缓存失效 |
| `shutdown` | 停止接收、排空队列、关闭连接 |

它用的是有界进程内队列，不是持久化重试队列；进程退出时未完成的工作可能丢失。

## 对接 DeepSeek Harness

DeepSeek Harness 这边是一个 **Cordis 插件 + 一个小型 Python Bridge**，
使用宿主公开的插件 Hook 和工具注册表，同样不改宿主源码。
当前适配 `0.1.0-rc.6`。注意：它是 Agent 宿主，不是 DeepSeek 官方聊天应用。

### 两个文件，各司其职

- `adapters/deepseek-harness/index.mjs`：插件本体，挂生命周期、注册工具；
- `adapters/deepseek-harness/bridge.py`：JSON-lines 桥，替插件连接记忆服务；
- `adapters/deepseek-harness/acceptance.mjs`：验收脚本。

在宿主配置（host profile）里配置插件的 `python`、`bridge`、`state`，需要指定 SDK 时再加
`sdk`。状态目录放在仓库外面，别把数据库、密钥或日志提交进来。

### 模型能用到哪些工具？

| 工具 | 用途 |
| --- | --- |
| `memory_search` | 结构化搜索，返回 id、版本、来源、标签和日期 |
| `memory_save` | 显式保存，走校验、去重和相对日期归一化 |
| `memory_update` | 按 `memory_id` + `expected_revision` 更新，版本过期会被拒绝 |
| `memory_remove` | 先给预览，再由**用户本人**发确认消息后才删除 |

安全边界也顺手记一下：召回内容按“不可信上下文”注入；插件自己产生的消息
不参与写入；子代理只读。

## 可选增强

- **Embedding 检索**：词法 + 向量融合，服务挂了或超时自动回退词法，
  按需启用，不需要改基础存储；
- **Mem0 语义投影（`dplus` 可选依赖）**：用官方 Mem0 + 本地 Qdrant 维护的
  可重建语义索引，不替代权威存储；安装命令为
  `python -m pip install -e 'sdk/python[dplus]'`；
- **模型摘要**：按需接入，不影响基础记忆与检索。

## 开发与测试

```bash
python -m pip install -e 'sdk/python[test]'

cargo fmt --all -- --check
cargo test --workspace --locked
python -m pytest sdk/python/tests -q
```

提交改动时请说明场景、行为变化和验证方式；协议和 SDK 变更要同步文档与
回归测试。凭据、私钥、用户数据库、导出记忆和现场日志都不要提交。

## 最后，几条边界

- **平台**：核心流程面向 Linux，已验证 riscv64；其他架构请自行构建验证。
- **不依赖 LLM 也能用**：基础存储和词法检索独立可用，语义与摘要按需增强。
- **SDK 不启动 daemon**：连不上先看服务和 socket 路径。
- **索引不是真相**：权威记录与 Tombstone 决定可见性，索引只是可重建的派生数据。

Rust workspace 与 Python SDK 元数据声明 Apache-2.0。

好了，介绍完了。接下来，去写一条属于你的记忆吧。
