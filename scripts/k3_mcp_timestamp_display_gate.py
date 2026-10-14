#!/usr/bin/env python3
"""K3 gate: the MCP surface must hand the model a time a user can read.

`updated_at` is bare epoch milliseconds, a machine shape that a model told to
"quote the timestamp" restyles on its own: the same record reached users as
`2026-09-24T11:40:27+08:00` in one run and as `2026-09-24 11:31` in another.
The recall payload therefore carries exactly one displayable time,
`updated_at_display` (`YYYY-MM-DD HH:MM`), and the Tool description tells the
model to copy it verbatim.

This gate drives the **installed** daemon and the **installed** SDK through one
real MCP round trip, so it proves what the device actually delivers.  It does
not care how the SDK was built: the isolated daemon gets its own socket and
database, and the memoryd already running on the device is left alone.

Usage (on K3, from any directory; no PYTHONPATH, so the installed SDK is used):

    python3 scripts/k3_mcp_timestamp_display_gate.py
    python3 scripts/k3_mcp_timestamp_display_gate.py --json /tmp/mcp-display-gate.json

Exit code: 0 = every check matched the fixed behaviour, 1 = at least one did not.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_MEMORYD = "/usr/libexec/bianbu-agent-memoryd"
DEFAULT_PYTHON = "/usr/lib/bianbu-agent-runtime/venv/bin/python"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DISPLAY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
RECORD_CONTENT = f"闸门标记：MCP 时间展示格式 {uuid.uuid4().hex[:8]}"
START_TIMEOUT = 30.0
CALL_TIMEOUT = 60.0

RESULTS: list[dict[str, object]] = []


def record(check_id: str, title: str, observed: object, expected: object, ok: bool) -> None:
    RESULTS.append(
        {
            "id": check_id,
            "title": title,
            "observed": observed,
            "expected": expected,
            "ok": bool(ok),
        }
    )


class IsolatedDaemon:
    """A memoryd on its own socket and database, so the live one is untouched."""

    def __init__(self, binary: Path, root: Path) -> None:
        self.binary = binary
        self.root = root
        self.socket = root / "memoryd.sock"
        self.database = root / "memory.db"
        self.key_package = root / "memory.keypkg"
        self.secret = root / "account-secret"
        self.log = root / "memoryd.log"
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.secret.write_text(uuid.uuid4().hex + uuid.uuid4().hex + "\n", encoding="utf-8")
        self.secret.chmod(0o600)
        environment = dict(os.environ)
        environment["BIANBU_MEMORY_DEV_CONFIRM_TOKEN"] = uuid.uuid4().hex
        environment["AGENT_MEMORY_EMBEDDING_DISABLED"] = "1"
        arguments = [
            str(self.binary),
            "--development-identity",
            "--socket",
            str(self.socket),
            "--database",
            str(self.database),
            "--key-package",
            str(self.key_package),
            "--account-secret-file",
            str(self.secret),
            "--initialize-key-package",
        ]
        with self.log.open("wb") as handle:
            self.process = subprocess.Popen(
                arguments, env=environment, stdout=handle, stderr=subprocess.STDOUT
            )
        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            if self.socket.exists():
                return
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"memoryd exited with {self.process.returncode}: {self._tail()}"
                )
            time.sleep(0.2)
        raise RuntimeError(f"memoryd socket never appeared: {self._tail()}")

    def _tail(self) -> str:
        try:
            return self.log.read_text(encoding="utf-8", errors="replace")[-600:]
        except OSError:
            return ""

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()


class McpStdio:
    """The installed MCP server, driven the way the Runtime drives it."""

    def __init__(self, python: Path, socket_path: Path, log_path: Path) -> None:
        self.python = python
        self.socket_path = socket_path
        self.log_path = log_path
        self.process: subprocess.Popen | None = None
        self._log = None
        self._next_id = 0

    def start(self) -> None:
        environment = dict(os.environ)
        # Pin the transport the Runtime pins, and make sure a source checkout on
        # the search path cannot shadow the installed package under test.
        environment["BIANBU_AGENT_MEMORY_SOCKET"] = str(self.socket_path)
        environment.pop("PYTHONPATH", None)
        self._log = self.log_path.open("wb")
        self.process = subprocess.Popen(
            [str(self.python), "-m", "bianbu_agent_sdk.memory.mcp_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._log,
            text=True,
            bufsize=1,
            env=environment,
        )

    def _send(self, payload: dict[str, object]) -> None:
        assert self.process is not None and self.process.stdin is not None
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()

    def _await(self, request_id: int) -> dict[str, object]:
        assert self.process is not None and self.process.stdout is not None
        deadline = time.time() + CALL_TIMEOUT
        while time.time() < deadline:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError(f"MCP server closed stdout: {self._tail()}")
            message = json.loads(line)
            if message.get("id") == request_id:
                return message
        raise RuntimeError("MCP server did not answer in time")

    def call(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        self._next_id += 1
        self._send(
            {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": method,
                "params": params or {},
            }
        )
        return self._await(self._next_id)

    def notify(self, method: str, params: dict[str, object] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def handshake(self) -> dict[str, object]:
        reply = self.call(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "k3-mcp-timestamp-gate", "version": "1.0"},
            },
        )
        self.notify("notifications/initialized")
        return reply

    def _tail(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")[-600:]
        except OSError:
            return ""

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self._log is not None:
            self._log.close()


def tool_text(reply: dict[str, object]) -> str:
    result = reply.get("result") or {}
    blocks = result.get("content") or [] if isinstance(result, dict) else []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            return str(block.get("text", ""))
    return json.dumps(result, ensure_ascii=False)


def tool_description(reply: dict[str, object], name: str) -> str:
    result = reply.get("result") or {}
    tools = result.get("tools") or [] if isinstance(result, dict) else []
    for tool in tools:
        if isinstance(tool, dict) and tool.get("name") == name:
            return str(tool.get("description", ""))
    return ""


def evaluate(memoryd: Path, python: Path, root: Path) -> None:
    daemon = IsolatedDaemon(memoryd, root / "daemon")
    server = McpStdio(python, daemon.socket, root / "mcp-stdio.log")
    try:
        daemon.start()
        record("daemon", "隔离 memoryd 已就绪", str(daemon.socket), "socket 存在", True)
        server.start()
        server.handshake()

        listing = server.call("tools/list")
        retrieve_description = tool_description(listing, "memory_retrieve")
        search_description = tool_description(listing, "session_search")
        record(
            "description-retrieve",
            "memory_retrieve 的工具说明要求逐字复制展示值",
            "updated_at_display" in retrieve_description
            and "verbatim" in retrieve_description,
            True,
            "updated_at_display" in retrieve_description
            and "verbatim" in retrieve_description,
        )
        record(
            "description-search",
            "session_search 的工具说明要求逐字复制展示值",
            "last_active_at_display" in search_description,
            True,
            "last_active_at_display" in search_description,
        )

        written = json.loads(
            tool_text(
                server.call(
                    "tools/call",
                    {
                        "name": "memory",
                        "arguments": {
                            "target": "memory",
                            "action": "add",
                            "content": RECORD_CONTENT,
                        },
                    },
                )
            )
        )
        record("write", "写入一条记忆成功", written.get("success"), True, bool(written.get("success")))

        recalled = json.loads(
            tool_text(
                server.call(
                    "tools/call", {"name": "memory_retrieve", "arguments": {"query": ""}}
                )
            )
        )
        memories = recalled.get("memories") or []
        stamped = [item for item in memories if int(item.get("updated_at") or 0) > 0]
        record("recall", "检索返回带时间戳的记录", len(stamped), ">0", bool(stamped))
        if not stamped:
            return
        item = stamped[0]
        raw_ms = int(item["updated_at"])
        display = item.get("updated_at_display")
        with_tz = datetime.fromtimestamp(raw_ms / 1000, ZoneInfo(DEFAULT_TIMEZONE))
        expected_display = with_tz.strftime("%Y-%m-%d %H:%M")

        record(
            "display-present",
            "记录带 updated_at_display 字段",
            display,
            DEFAULT_TIMEZONE + " 本地 YYYY-MM-DD HH:MM",
            isinstance(display, str) and bool(DISPLAY_PATTERN.match(display)),
        )
        record(
            "display-value",
            "展示值与 epoch 是同一时刻的本地分钟",
            display,
            expected_display,
            display == expected_display,
        )
        record(
            "iso-absent",
            "载荷不再带机器口径的时间串（只剩毫秒与展示值）",
            sorted(
                key
                for key in item
                if "iso" in key.lower() or "timestamp" in key.lower()
            ),
            [],
            not any(
                "iso" in key.lower() or "timestamp" in key.lower() for key in item
            ),
        )
        record(
            "display-shape",
            "展示值不含时钟格式以外的内容（无 T、无秒、无偏移）",
            display,
            "YYYY-MM-DD HH:MM",
            isinstance(display, str)
            and "T" not in display
            and ":" not in display[14:]
            and "+" not in display,
        )
    finally:
        server.stop()
        daemon.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="K3 MCP 时间展示格式闸门")
    parser.add_argument("--memoryd", default=DEFAULT_MEMORYD, help="已安装的 memoryd 二进制")
    parser.add_argument("--python", default=DEFAULT_PYTHON, help="Runtime venv 的解释器")
    parser.add_argument("--json", help="证据 JSON 输出路径")
    arguments = parser.parse_args()

    memoryd = Path(arguments.memoryd)
    python = Path(arguments.python)
    for path in (memoryd, python):
        if not path.is_file():
            print(f"missing required file: {path}", file=sys.stderr)
            return 2

    failure: str | None = None
    with tempfile.TemporaryDirectory(prefix="mcp-display-gate-") as raw:
        try:
            evaluate(memoryd, python, Path(raw))
        except Exception as error:  # noqa: BLE001 - reported as a failed gate
            failure = f"{type(error).__name__}: {error}"

    evidence = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "memoryd": str(memoryd),
        "python": str(python),
        "timezone": DEFAULT_TIMEZONE,
        "checks": RESULTS,
        "failure": failure,
        "passed": failure is None and all(item["ok"] for item in RESULTS),
    }
    if arguments.json:
        Path(arguments.json).write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    print("VERDICT:", "PASS" if evidence["passed"] else "FAIL")
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
