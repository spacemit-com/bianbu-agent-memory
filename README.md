# Bianbu Agent Memory

**让 Agent 记住对话、延续工作，并跨会话找回长期记忆。**

*A device-side, local-first memory service for AI agents — Rust daemon, Python SDK, and a Unix domain socket protocol.*

Bianbu Agent Memory 是面向 Agent 应用的**设备侧、本地优先记忆服务**。它将对话
历史、工作状态和长期知识组织为 L1–L3 三层记忆，帮助 Agent 在会话切换、上下文
压缩或服务重启后找回上下文，并持续使用用户偏好、项目事实和历史决定。

## 核心能力

- **分层记忆**：L1 保存会话事件与完整对话轮，L2 保留结构化工作摘要，L3 管理
  可跨会话使用的长期记忆及其证据和版本。
- **检索与召回**：内置本地全文检索与有界读取，可按需接入 Embedding 和 Mem0
  语义增强；召回内容始终以本地权威记录为准。
- **存储与访问控制**：支持 SQLCipher 加密、事务持久化、幂等写入和版本冲突检测，
  以及可信 Runtime 身份、Capability 校验和命名空间隔离。
- **记忆生命周期管理**：配置、保留期、导出、审计、删除、维护、检查点与
  本地加密备份。
- **Agent 集成**：Python 同步/异步 SDK、CLI、MCP 服务和 Runtime Bridge，并提供
  Hermes 与 DeepSeek Harness 适配器。

基础存储与全文检索不依赖模型服务；语义检索与模型摘要按需接入。

## 架构总览

```text
Agent / 适配器 / Runtime → Python SDK ─┐
                          CLI / MCP ─┤
                                     ▼
                         bianbu-agent-memoryd
                                     │
                           本地存储与检索
```

客户端通过 Unix domain socket 连接服务。Runtime 是一种集成方式，
SDK、CLI 和 MCP 无需经过 Runtime 才能连接。

## 从 Bianbu 软件源安装

适用于已配置相应 Bianbu 软件源、且软件源提供这两个包的设备。普通
Debian/Ubuntu 默认软件源不一定包含本项目；源码开发请使用下方快速开始。

```bash
sudo apt update
sudo apt install bianbu-agent-memory bianbu-agent-memoryd
```

`apt` 会下载并安装 DEB 包及其依赖，无需另行下载后执行 `dpkg -i`。
`bianbu-agent-memory` 提供 Python SDK 和 CLI，`bianbu-agent-memoryd` 提供
记忆守护进程。

安装完成后，生产使用仍需正确的服务配置和可信 Runtime 身份注册；
安装成功不等于 Agent 已接入自动记忆。服务配置与身份要求见
[配置指南](./docs/CONFIGURATION.md)。软件源配置请参考所用发行版的说明。

## 快速开始

从源码构建、启动隔离开发实例，再完成第一次读写。开发实例使用
临时数据库与测试身份，仅用于本地体验和调试，**不要写入真实个人数据**。

### 1. 环境与构建

开发平台为 Linux。已验证的构建与基础读写环境为 riscv64、Rust 1.93.1、
Python 3.14.4；最低版本要求以项目元数据为准。

要求 Linux、Rust **1.80+**、Python **3.10+**、C 编译工具链和 OpenSSL 开发依赖。
SQLite/SQLCipher 由 Cargo 的 bundled-sqlcipher 构建，无需独立数据库服务。

~~~bash
sudo apt install build-essential pkg-config libssl-dev python3-venv python3-dev libffi-dev  # Debian/Ubuntu

# 下载或克隆本仓库后，在仓库根目录执行
cargo build --workspace --release --locked

python3 -m venv .venv && . .venv/bin/activate
python -m pip install -e 'sdk/python'
~~~

**SDK 是客户端，memoryd 才是服务**：pip 安装不会安装或启动守护进程。

### 2. 启动开发服务

~~~bash
umask 077
export BIANBU_MEMORY_DEV_DIR="$(mktemp -d /tmp/bianbu-memory-dev.XXXXXX)"
printf '开发目录：%s\n' "$BIANBU_MEMORY_DEV_DIR"

target/release/bianbu-agent-memoryd \
  --development-identity \
  --allow-plaintext \
  --socket "$BIANBU_MEMORY_DEV_DIR/agent-memoryd.sock" \
  --database "$BIANBU_MEMORY_DEV_DIR/memory.db" \
  --background-device-id dev-local
~~~

保持终端运行。明文与开发身份选项仅限临时示例数据库，生产部署见
[配置指南](./docs/CONFIGURATION.md)。

### 3. 保存与检索第一条记忆

在第二个终端进入同一仓库目录，激活虚拟环境，并将下方路径替换为
第一个终端打印的开发目录，然后运行：

~~~bash
. .venv/bin/activate
export BIANBU_MEMORY_DEV_DIR=/tmp/bianbu-memory-dev.REPLACE_ME
~~~

~~~bash
python - <<'PY'
import os
from pathlib import Path

from bianbu_agent_sdk.memory import MemoryClient, MemoryKind

socket_path = Path(os.environ["BIANBU_MEMORY_DEV_DIR"]) / "agent-memoryd.sock"

with MemoryClient.connect(
    socket_path,
    timeout=2.0,
    account_id="default",
    agent_name="default-agent",
) as client:
    receipt = client.long_term.put(
        kind=MemoryKind.PREFERENCE,
        text="我喜欢深色终端主题。",
        summary="终端主题偏好：深色",
        idempotency_key="readme:terminal-theme:v1",
    )
    print("已保存：", receipt.memory_id, receipt.revision)

    headers = client.long_term.search_headers("终端", top_k=5)
    records = client.long_term.get_many([header.memory_id for header in headers])
    for record in records:
        print(record.memory_id, record.text)
PY
~~~

幂等键标识一次逻辑写入：重试时复用，写入新内容时换新键。

### 4. 验证与停止

~~~bash
bianbu-agent-memory --socket "$BIANBU_MEMORY_DEV_DIR/agent-memoryd.sock" health
~~~

成功返回健康状态 JSON 表示客户端已连通服务。启动后或刚写入时，数据库自检可能返回
`status: unknown`、`database_check.state: busy`；这不等于读写失败，
也不代表完整健康检查通过。首次读写以步骤 3 找回刚保存的记录为验证依据。
结束后在第一个终端 Ctrl+C 停止服务；
临时目录不会自动删除。

## 集成

| 集成方式 | 接入点与职责 |
| --- | --- |
| 自有 Agent | Python SDK / MemoryBridge，宿主管理可信身份与会话生命周期 |
| Bianbu Agent Runtime | memory.before_run 召回、memory.after_run 写入 |
| Hermes | [生命周期适配器](./adapters/hermes/README.md)：召回、自动写入、显式保存、会话切换与压缩 |
| DeepSeek Harness | [Cordis 插件](./adapters/deepseek-harness/README.md)：结构化搜索、保存、版本校验更新、用户确认删除、自动写入与压缩摘要 |
| Bibit 设置插件 | 前端通过 service:memory 调用可信 Adapter；见 [Bibit 契约](./docs/BIBIT_INTEGRATION_CONTRACT.md) |
| MCP | `sdk/python[mcp]`，实现位于 sdk/python/src/bianbu_agent_sdk/memory/mcp_server |

召回内容应作为**不可信背景信息**注入，不得覆盖当前用户指令和宿主安全规则。
模型与 API key 由宿主配置。

Memory 不包含聊天模型或聊天网页。Hermes 和 DeepSeek Harness 提供对话入口，
Bibit 插件提供记忆设置界面。自动记忆需要宿主接入召回、写入和会话生命周期；
仅安装 SDK 不会开启自动记忆。底层 `long_term.put` 是直接存储接口；需要规则
抽取、相对日期归一化和去重时，应使用 Runtime Bridge 或相应适配器。

当前适配器针对 Hermes `v0.21.7` 与 DeepSeek Harness `0.1.0-rc.6`。升级宿主
后应重新验证事件接口；DeepSeek Harness 是 Agent 宿主，不是 DeepSeek 官方聊天 App。

**可选增强**（先跑通核心服务，再按需配置）：

- **Embedding 检索**：词法与向量融合，服务不可用或超时回退词法结果。
  见 [Embedding 集成](./docs/GATEWAY_EMBEDDING_INTEGRATION.md)。
- **Mem0 / D+ 投影**：基于 Mem0、Qdrant 和 Embedding 的可重建语义索引，
  不替代权威存储。`python -m pip install -e 'sdk/python[dplus]'`；
  见 [Mem0 适配](./docs/MEM0_ADAPTATION.md)。
- **模型摘要**：见 [模型使用](./docs/MODEL_USAGE.md)。

## 开发与测试

~~~bash
python -m pip install -e 'sdk/python[test]'

cargo fmt --all -- --check
cargo test --workspace --locked
python -m pytest sdk/python/tests -q
~~~

更多测试场景见 [测试用例](./docs/TEST_CASES.md)。
可选 Python 依赖在缺少预编译包的平台上可能需要本地编译。

提交改动时请说明场景、行为变化和验证方式；协议及 SDK 变更应同步文档和
回归测试。不要提交凭据、私钥、用户数据库、导出记忆或现场日志。

## 文档

| 主题 | 文档 |
| --- | --- |
| SDK 与外部 Agent 接入 | [对接 API](./docs/INTEGRATION_API.md) |
| UDS 消息、方法与错误 | [协议说明](./docs/PROTOCOL.md) |
| 配置与运行参数 | [配置指南](./docs/CONFIGURATION.md) |
| 长期记忆字段和值约束 | [L3 字段字典](./docs/L3_FIELD_DICTIONARY.md) |
| 语义投影 | [Mem0 适配](./docs/MEM0_ADAPTATION.md) |
| 加密备份 | [Capsule](./docs/CAPSULE_BACKUP.md) |
| 已实现内容与边界 | [实现状态](./docs/IMPLEMENTATION_STATUS.md) |
| 完整应用示例 | [天气记忆示例](./docs/WEATHER_MEMORY_DEMO.md) |

设备部署、Debian 打包、D-Bus 控制面等主题见 `docs/` 目录。

## 边界

- **平台**：核心开发流程面向 Linux；已验证 riscv64 构建与基础读写，其他架构需
  自行构建验证。
- **不依赖 LLM 也能用**：基础存储和词法检索独立可用；语义检索与模型增强
  需另行配置服务。
- **SDK 不启动 daemon**：连接失败时先检查服务与 socket 路径。
- **删除与索引**：权威记录与 Tombstone 决定可见性，索引是派生数据，不能直接
  将向量库返回的文本注入上下文。

Rust workspace 与 Python SDK 元数据声明 Apache-2.0。
