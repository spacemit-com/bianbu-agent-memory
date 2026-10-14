from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .core import PromptError, WeatherMemoryApplication, WeatherServiceError


_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bianbu 天气与记忆演示</title>
  <style>
    :root { color-scheme: light; font-family: Inter, "Noto Sans SC", system-ui, sans-serif; }
    * { box-sizing: border-box; }
    body { margin: 0; background: #f4f6fb; color: #172033; }
    main { width: min(980px, calc(100% - 32px)); margin: 48px auto; }
    header { margin-bottom: 24px; }
    h1 { margin: 0 0 8px; font-size: clamp(28px, 5vw, 42px); }
    .lead { margin: 0; color: #5d6980; }
    .panel { background: white; border: 1px solid #dfe4ef; border-radius: 16px; padding: 20px; box-shadow: 0 8px 30px rgba(25, 42, 75, .07); }
    form { display: flex; gap: 12px; }
    input { flex: 1; min-width: 0; border: 1px solid #bac4d6; border-radius: 10px; padding: 13px 14px; font-size: 16px; }
    input:focus { outline: 3px solid #dce8ff; border-color: #356fd6; }
    button { border: 0; border-radius: 10px; padding: 0 22px; color: white; background: #245fc7; font-size: 16px; cursor: pointer; }
    button:disabled { opacity: .65; cursor: wait; }
    #status { min-height: 24px; margin: 14px 0 0; color: #5d6980; }
    #status.error { color: #b42318; }
    #result { display: none; margin-top: 22px; }
    .answer { margin: 0; font-size: 18px; line-height: 1.8; white-space: pre-wrap; }
    .meta { margin-top: 14px; display: flex; flex-wrap: wrap; gap: 8px; }
    .chip { border-radius: 999px; padding: 5px 10px; background: #eef4ff; color: #244e91; font-size: 13px; }
    h2 { margin: 30px 0 12px; font-size: 22px; }
    #memories { display: grid; gap: 12px; }
    .memory { border: 1px solid #dfe4ef; border-radius: 13px; padding: 16px; background: #fbfcff; }
    .memory h3 { margin: 0 0 8px; font-size: 16px; }
    .memory p { margin: 7px 0; line-height: 1.6; }
    .memory dl { display: grid; grid-template-columns: 110px 1fr; gap: 7px 12px; margin: 12px 0 0; font-size: 13px; }
    .memory dt { color: #65728a; }
    .memory dd { margin: 0; overflow-wrap: anywhere; }
    code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; background: #edf0f6; border-radius: 4px; padding: 2px 5px; }
    ul.paths { margin: 4px 0 0; padding-left: 20px; }
    footer { margin-top: 18px; color: #7a8497; font-size: 13px; }
    @media (max-width: 640px) { form { flex-direction: column; } button { height: 46px; } .memory dl { grid-template-columns: 1fr; gap: 2px; } }
  </style>
</head>
<body>
<main>
  <header>
    <h1>天气查询 + Agent Memory</h1>
    <p class="lead">输入自然语言提示词，返回天气，并展示本轮召回的记忆正文、版本、来源和文件路径。</p>
  </header>
  <section class="panel">
    <form id="ask-form">
      <input id="prompt" name="prompt" value="帮我查询珠海明天的天气？" maxlength="1000" aria-label="天气查询提示词">
      <button id="submit" type="submit">查询</button>
    </form>
    <p id="status"></p>
    <div id="result">
      <p id="answer" class="answer"></p>
      <div id="weather-meta" class="meta"></div>
      <h2>相关记忆</h2>
      <div id="memories"></div>
    </div>
  </section>
  <footer>天气数据：Open-Meteo；记忆数据：bianbu-agent-memoryd（search_headers → get_many）。</footer>
</main>
<script>
const form = document.getElementById('ask-form');
const promptInput = document.getElementById('prompt');
const submit = document.getElementById('submit');
const statusNode = document.getElementById('status');
const resultNode = document.getElementById('result');
const answerNode = document.getElementById('answer');
const weatherMeta = document.getElementById('weather-meta');
const memoriesNode = document.getElementById('memories');

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function addDefinition(dl, label, value, asCode=false) {
  dl.appendChild(element('dt', '', label));
  const dd = element('dd');
  dd.appendChild(element(asCode ? 'code' : 'span', '', String(value ?? '')));
  dl.appendChild(dd);
}

function render(data) {
  answerNode.textContent = data.answer;
  weatherMeta.replaceChildren();
  const weather = data.weather;
  [weather.condition, `${weather.temperature_min_c}–${weather.temperature_max_c}°C`, `降水概率 ${weather.precipitation_probability_percent}%`, `会话 ${data.session_id}`]
    .forEach(text => weatherMeta.appendChild(element('span', 'chip', text)));

  memoriesNode.replaceChildren();
  if (!data.memories.length) {
    memoriesNode.appendChild(element('p', '', '未召回到相关长期记忆。'));
  }
  data.memories.forEach((memory, index) => {
    const card = element('article', 'memory');
    card.appendChild(element('h3', '', `${index + 1}. ${memory.summary || memory.kind}`));
    card.appendChild(element('p', '', memory.content));
    const dl = element('dl');
    addDefinition(dl, 'memory_id', memory.memory_id, true);
    addDefinition(dl, '类型 / 版本', `${memory.kind} / r${memory.revision}`);
    addDefinition(dl, '来源', memory.source);
    addDefinition(dl, '同步策略', memory.sync_policy);
    addDefinition(dl, '标签', memory.tags.join(', '));
    dl.appendChild(element('dt', '', '文件路径'));
    const pathCell = element('dd');
    if (memory.file_paths.length) {
      const list = element('ul', 'paths');
      memory.file_paths.forEach(path => {
        const li = element('li');
        li.appendChild(element('code', '', path));
        list.appendChild(li);
      });
      pathCell.appendChild(list);
    } else {
      pathCell.textContent = '无文件路径证据';
    }
    dl.appendChild(pathCell);
    card.appendChild(dl);
    memoriesNode.appendChild(card);
  });
  resultNode.style.display = 'block';
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  submit.disabled = true;
  statusNode.className = '';
  statusNode.textContent = '正在查询天气并召回相关记忆…';
  try {
    const response = await fetch('/api/ask', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({prompt: promptInput.value})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    render(data);
    statusNode.textContent = data.warnings.length ? data.warnings.join('；') : '查询完成';
  } catch (error) {
    statusNode.className = 'error';
    statusNode.textContent = `查询失败：${error.message}`;
  } finally {
    submit.disabled = false;
  }
});
</script>
</body>
</html>
"""


class WeatherDemoServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        application: WeatherMemoryApplication,
    ) -> None:
        self.application = application
        super().__init__(server_address, WeatherDemoHandler)


class WeatherDemoHandler(BaseHTTPRequestHandler):
    server: WeatherDemoServer
    server_version = "BianbuWeatherMemoryDemo/0.1"

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send_bytes(HTTPStatus.OK, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/health":
            try:
                health = self.server.application.memory_client.health()
                self._send_json(
                    HTTPStatus.OK,
                    {"status": "ok", "memory_service": health},
                )
            except Exception as error:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"status": "error", "error": f"记忆服务不可用：{type(error).__name__}"},
                )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/api/ask":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 64 * 1024:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "请求体为空或过大"})
            return
        try:
            body = json.loads(self.rfile.read(length))
            prompt = body.get("prompt") if isinstance(body, dict) else None
            if not isinstance(prompt, str):
                raise PromptError("prompt 必须是字符串")
            result = self.server.application.ask(prompt)
            self._send_json(HTTPStatus.OK, result.to_dict())
        except (json.JSONDecodeError, PromptError, ValueError) as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except WeatherServiceError as error:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
        except Exception as error:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": f"记忆服务或应用执行失败：{type(error).__name__}"},
            )

    def log_message(self, format: str, *args: Any) -> None:
        # Keep ordinary logs metadata-only; prompt and recalled memory stay out of logs.
        super().log_message(format, *args)

    def _send_json(self, status: HTTPStatus, value: dict[str, Any]) -> None:
        self._send_bytes(
            status,
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _send_bytes(self, status: HTTPStatus, payload: bytes, content_type: str) -> None:
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(payload)


def serve(
    application: WeatherMemoryApplication,
    host: str = "127.0.0.1",
    port: int = 8080,
) -> None:
    server = WeatherDemoServer((host, port), application)
    print(f"Bianbu 天气与记忆演示已启动：http://{host}:{server.server_port}")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
