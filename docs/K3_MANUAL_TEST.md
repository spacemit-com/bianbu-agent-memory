# K3 AgentMemory 手动测试

本环境用于 D+ 工程验证，采用 development identity、SQLCipher 和独立持久化数据库，不是生产安全部署。云端备份/E2E Capsule 不在本轮测试范围。

## 访问地址

- Web：`http://192.0.2.10:9900/chat`
- memoryd socket：`/home/example/.local/state/bianbu-agent-memory-runtime-9900/agent-memoryd.sock`
- SQLite：`/home/example/.local/state/bianbu-agent-memory-runtime-9900/memory.db`（SQLCipher）
- D+：`http://127.0.0.1:18921`（仅 K3 回环地址）
- 日志：`/home/example/.local/state/bianbu-agent-memory-manual/memoryd.log`、`runtime.log`

## 建议手测流程

请在 Web 页面创建不同的新对话，避免 Runtime 自身的 Session 上下文影响判断。

1. 新对话 A 输入：`你好，请记住，终端配色偏好：深色。`
2. 在同一对话 A 输入：`请记住，终端配色偏好：浅色。`
3. 再创建新对话 B，输入自然问句：`你好，请问终端配色偏好是什么颜色`
4. 再创建新对话 C，改变词序输入：`你好，目前终端偏好颜色是什么？`
5. 两种问法的回答均应包含“浅色”；数据库中终端配色偏好只能有一条，前两次写入应使同一 memory ID 的 revision 连续增加两次，查询本身不得增加 revision。
6. Runtime 的自动标题生成不得增加 AgentMemory 的 Session Event 或长期记忆；标题文本也不得成为记忆记录。
7. 输入：`请记住，我习惯使用 uv 管理 Python 依赖。`
8. 新对话输入：`我习惯使用 uv 管理 Python 依赖`
9. 连续完成 5 轮对话后，可用状态和数据库命令确认 10 条 Session Event 与 1 条 Summary。

当前 Runtime 默认使用 D+：官方 Mem0 存储 API + 本地 Qdrant/Embedding 派生索引，规则模式负责安全的显式偏好/习惯抽取；召回结果必须经 AgentMemory canonical 映射。D+ 不可用时回退 FTS5/Scheme-A。profile、凭证和其它 PII 只保存在加密 canonical 库，不进入 D+。

## 跨日期提醒回归（2026-08-12 → 2026-08-13）

1. 在 8 月 12 日的新会话中输入：`明天上午 9 点提醒我完成 AgentMemory 和 Runtime 工程对接`。
2. 在 8 月 13 日新建会话，输入：`今天有什么事情需要提醒我吗`。
3. 预期召回结果包含绝对日期 `2026-08-13T09:00+08:00`，并标记 `due-date:2026-08-13` 或“今天到期（应在今天提醒）”。
4. 不得把原始文本中的“明天”重新解释为 8 月 14 日；不得优先返回 Firefox、Nebula 等无关 profile/fact。

SDK 验证命令：

```bash
PYTHONPATH=/home/example/bianbu-agent-memory/sdk/python/src \
/home/example/bianbu-agent-runtime-agent-memory/.venv/bin/python - <<'PY'
import asyncio
from bianbu_agent_sdk.memory.runtime import MemoryBridge

async def main():
    bridge = MemoryBridge(
        socket_path="/home/example/.local/state/bianbu-agent-memory-runtime-9900/agent-memoryd.sock"
    )
    values = await bridge.recall("今天有什么事情需要提醒我吗")
    print("\n".join(values))
    await bridge.close()

asyncio.run(main())
PY
```

## SSH 检查命令

```bash
cd /home/example/bianbu-agent-memory

scripts/k3_manual_demo_status.sh

python3.13 scripts/k3_runtime_memory_regression.py

PYTHONPATH="$PWD/sdk/python/src" python3.13 scripts/k3_manual_memory_dump.py \
  /home/example/.local/state/bianbu-agent-memory-manual/agent-memoryd.sock

PYTHONPATH="$PWD/sdk/python/src" python3.13 scripts/k3_manual_memory_dump.py \
  /home/example/.local/state/bianbu-agent-memory-manual/agent-memoryd.sock \
  --query '终端配色偏好'
```

## 启停

```bash
cd /home/example/bianbu-agent-memory
BIANBU_RUNTIME_ROOT=/home/example/personal-agent-runtime-agent-memory \
BIANBU_MEMORY_ROOT=/home/example/bianbu-agent-memory \
scripts/k3-agent-memory-stack-start.sh
scripts/k3-agent-memory-stack-start.sh  # 同一脚本可安全重启独立 9900 栈
```

停止脚本只停止手测 Runtime 和 memoryd，保留 SQLite。若需要清空测试记忆，应先停止服务并明确备份/删除数据库，不要在服务运行时直接修改 SQLite。
