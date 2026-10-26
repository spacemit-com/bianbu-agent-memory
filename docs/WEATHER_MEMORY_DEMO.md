# 天气查询与相关记忆展示 Demo

该 Demo 提供两个用户入口：

- CLI：输入一句自然语言天气提示词，直接输出天气回复和相关记忆；
- Web：在浏览器输入提示词，以卡片形式展示天气、记忆正文、版本、来源、证据和文件路径。

示例提示词：

```text
帮我查询珠海明天的天气？
```

## 1. 处理链路

```text
CLI / Web prompt
  -> 解析城市与今天/明天/后天
  -> long_term.search_headers（有界候选）
  -> long_term.get_many（二次鉴权读取正文和 evidence）
  -> Open-Meteo 城市解析与逐日天气预报
  -> 根据召回偏好生成中文天气回复
  -> 展示 memory_id/kind/revision/source/tags/file_paths/evidence
  -> sessions.append_event 保存本轮 user/assistant 事件
```

记忆读取遵循需求文档规定的 `search_headers -> get_many` 两阶段路径。文件路径不伪装成 Server 的权威 `source` 字段，而是作为 `EvidenceRef("file_path", absolute_path, sha256)` 保存和展示。

## 2. 启动开发记忆服务

以下方式只用于开发验证。生产环境应使用 systemd 服务、Runtime registry 和默认 UDS。

下面按两个终端操作。终端 1 中的 `workdir`、`socket` 等 shell 变量不会自动传递到终端 2，因此终端 1 会把本次实际 socket 路径写入 `/tmp/bianbu-weather-demo.socket`，终端 2 再读取它。

```bash
## 终端 1
cd /home/example/bianbu-agent-memory
workdir=$(mktemp -d /tmp/bianbu-weather-demo.XXXXXX)
socket="$workdir/agent-memoryd.sock"
database="$workdir/memory.db"
printf '%s\n' "$socket" > /tmp/bianbu-weather-demo.socket
printf '记忆服务 socket: %s\n' "$socket"
export BIANBU_MEMORY_DEV_CONFIRM_TOKEN="$(python3.13 -c 'import secrets; print(secrets.token_urlsafe(32))')"
target/release/bianbu-agent-memoryd \
  --development-identity \
  --socket "$socket" \
  --database "$database"
```

看到类似下面的日志后，保持终端 1 不要退出：

```text
bianbu-agent-memoryd: listening on /tmp/bianbu-weather-demo.xxxxxx/agent-memoryd.sock (protocol agent-memory.v1)
```

## 3. CLI 使用

在终端 2 执行。这里的 `socket` 必须是终端 1 实际输出的路径，不能把 `/path/to/...`、`/实际的/...` 或 `实际目录` 当作路径原样输入。

```bash
## 终端 2
cd /home/example/bianbu-agent-memory
export PYTHONPATH="$PWD:$PWD/sdk/python/src"
socket=$(cat /tmp/bianbu-weather-demo.socket)
test -S "$socket" || { echo "记忆服务 socket 不存在：$socket" >&2; exit 1; }

python3.13 -m weather_memory_demo \
  --socket "$socket" \
  ask '帮我查询珠海明天的天气？'
```

输出包含：

- 天气状况、最低/最高温度、降水概率、降水量和最大风速；
- 本轮实际应用的偏好，例如摄氏度、降雨时携带雨具；
- 相关长期记忆正文；
- `memory_id`、kind、revision、source、tags；
- `file_path` evidence 的绝对文件路径和 SHA-256；
- 本轮写入的 Session ID。

需要机器可读结果时：

```bash
python3.13 -m weather_memory_demo \
  --socket "$socket" \
  ask --json '帮我查询珠海明天的天气？'
```

如果不希望自动准备随项目提供的两条示例偏好，增加 `--no-seed`。也可单独执行：

```bash
python3.13 -m weather_memory_demo \
  --socket "$socket" \
  seed
```

## 4. Web 使用

启动 Web 前先确认端口没有被占用：

```bash
ss -ltn | grep -E ':8081([[:space:]]|$)' || echo PORT_8081_FREE
```

K3 设备上的 `8080` 已被 Nginx/SpacemiT 管理页面占用，K3 请使用 `8081`。仅本机访问时，如果 `8080` 确认空闲，也可以改回 `8080`。

仅本机访问：

```bash
python3.13 -m weather_memory_demo \
  --socket "$socket" \
  web --host 127.0.0.1 --port 8081
```

浏览器打开 `http://127.0.0.1:8081`。

需要从同一可信局域网访问 K3 时，可显式使用：

```bash
python3.13 -m weather_memory_demo \
  --socket "$socket" \
  web --host 0.0.0.0 --port 8081
```

然后打开 `http://192.0.2.13:8081`。该页面是开发 Demo，没有账户登录层；不要暴露到不可信网络。生产 UI 应通过受信网关、鉴权和正式 Runtime 身份注册接入。

Web API：

```http
POST /api/ask
Content-Type: application/json

{"prompt":"帮我查询珠海明天的天气？"}
```

健康检查：`GET /api/health`。

## 5. 示例记忆文件

随项目提供的文件：

```text
weather_memory_demo/weather_memories.json
```

默认包含两条 `local_only` 偏好：

1. 天气回复使用中文和摄氏度；
2. 有雨或降水概率较高时提醒携带雨具。

启动时使用稳定幂等键写入，因此重复启动不会重复插入。实际产品可加 `--no-seed`，只召回 Agent 已有的长期记忆。

## 6. 边界

- 当前自然语言解析覆盖中文/英文天气查询，以及今天、明天、后天；不是通用 LLM Agent；
- 天气数据来自 Open-Meteo，设备需要可访问公网 HTTPS；
- 中文城市优先使用内置常见城市英文别名解决 Open-Meteo 中文地名检索限制，其他城市可直接输入英文名；
- 记忆召回使用当前 M0 的 SQLite FTS5/LIKE，不依赖常驻 Embedding 服务；
- secret 记忆不会由 Demo 展示；召回正文限制为每条 2000 字符、最多 5 条；
- Web 普通日志只记录 HTTP 元数据，不记录提示词或召回正文。

## 7. 常见问题

### `No module named weather_memory_demo`

从项目根目录执行，或显式设置模块和 SDK 路径：

```bash
cd /home/example/bianbu-agent-memory
export PYTHONPATH="$PWD:$PWD/sdk/python/src"
```

### `FileNotFoundError: ... agent-memoryd.sock`

确认终端 1 的 daemon 仍在运行，并检查终端 2 读取到的路径：

```bash
socket=$(cat /tmp/bianbu-weather-demo.socket)
printf '%s\n' "$socket"
test -S "$socket" && echo SOCKET_OK || echo SOCKET_MISSING
```

不要使用文档中的示例占位符 `/path/to/agent-memoryd.sock`、`/实际的/agent-memoryd.sock` 或 `实际目录`。每次重新启动 daemon 生成的随机 `workdir` 可能不同，必须使用本次启动日志输出的真实路径。

### `OSError: [Errno 98] Address already in use`

这是 Web TCP 端口被其他进程占用，不是记忆服务 UDS 故障。换用空闲端口，例如：

```bash
python3.13 -m weather_memory_demo \
  --socket "$socket" \
  web --host 0.0.0.0 --port 8081
```

### 页面打不开或 API 无响应

先在 K3 本机检查：

```bash
curl -fsS http://127.0.0.1:8081/api/health
```

应返回 `"status":"ok"`。远程浏览器访问时，请确认 K3 网络策略允许访问 `8081`。

## 8. 验证

本地：

```bash
python -m unittest discover -s tests -p 'test_weather_memory_demo.py' -v
```

K3 已验证：

- CLI 真实连接 UDS，写入 2 条带文件路径证据的长期记忆；
- 输入“帮我查询珠海明天的天气？”返回 Open-Meteo 天气；
- 召回并展示 2 条相关记忆和绝对文件路径；
- 降雨预报触发“建议携带雨具”的记忆应用；
- Web `/api/health` 和 `/api/ask` 返回成功；
- CLI + Web 共落库 4 条 Session Event。
