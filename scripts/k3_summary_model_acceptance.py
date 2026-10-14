#!/usr/bin/env python3
"""Exercise Memory's real summary-model path against a K3 model service.

The script starts an isolated memoryd instance, so it does not change the
device's production database or product configuration.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from bianbu_agent_sdk.memory import MemoryClient


ACCOUNT_ID = "bianbu"
AGENT_ID = "com.bianbu.memory.summary.acceptance"
AGENT_NAME = "summary-acceptance"
CAPABILITIES = [
    "memory.short_term.read",
    "memory.short_term.write",
    "memory.privacy.read",
    "memory.privacy.write",
]


def device_id() -> str:
    for candidate in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        try:
            value = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return "k3-summary-acceptance"


def start_time_ticks(pid: int) -> int:
    fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    return int(fields[21])


def register_identity(registry_dir: Path) -> Path:
    pid = os.getpid()
    started = start_time_ticks(pid)
    device = device_id()
    record = {
        "uid": os.getuid(),
        "gid": os.getgid(),
        "pid_start_time_ticks": started,
        "account_id": ACCOUNT_ID,
        "device_id": device,
        "agent_id": AGENT_ID,
        "agent_name": AGENT_NAME,
        "agent_instance_id": f"summary-acceptance.{pid}.{started}",
        "memory_space_id": f"{ACCOUNT_ID}.{AGENT_ID}.{device}",
        "capabilities": CAPABILITIES,
        "executable_path": str(Path(f"/proc/{pid}/exe").resolve()),
        "expires_at_ms": int(time.time() * 1000) + 60 * 60 * 1000,
    }
    target = registry_dir / f"{pid}.json"
    temporary = registry_dir / f".{pid}.json.tmp"
    temporary.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
    temporary.chmod(0o640)
    temporary.replace(target)
    target.chmod(0o640)
    return target


def wait_for_socket(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr else ""
            raise RuntimeError(f"memoryd exited during startup: {stderr[-2000:]}")
        if path.exists():
            return
        time.sleep(0.05)
    raise TimeoutError(f"memoryd socket was not created: {path}")


def stop_process(process: subprocess.Popen[str] | None) -> str:
    if process is None:
        return ""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    return process.stderr.read() if process.stderr else ""


def discover_model(base_url: str) -> str:
    request = Request(f"{base_url.rstrip('/')}/models", method="GET")
    with urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    models = payload.get("data") or payload.get("models") or []
    if not models:
        raise RuntimeError("the model service returned no models")
    first = models[0]
    model = first.get("id") or first.get("model") or first.get("name")
    if not isinstance(model, str) or not model:
        raise RuntimeError("the model service returned an invalid model id")
    return model


def append_turn(client: MemoryClient, session_id: str, turn: int, user: str, assistant: str) -> float:
    turn_id = f"turn-{turn}"
    client.sessions.append_event(
        session_id=session_id,
        event_type="user_message",
        payload={"text": user, "turn_id": turn_id, "status": "completed"},
        idempotency_key=f"{session_id}-{turn_id}-user",
    )
    started = time.monotonic()
    client.sessions.append_event(
        session_id=session_id,
        event_type="assistant_message",
        payload={"text": assistant, "turn_id": turn_id, "status": "completed"},
        idempotency_key=f"{session_id}-{turn_id}-assistant",
    )
    return (time.monotonic() - started) * 1000


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run(args: argparse.Namespace) -> dict[str, Any]:
    memoryd = Path(args.memoryd).resolve()
    check(memoryd.is_file(), f"memoryd does not exist: {memoryd}")
    model = args.model or discover_model(args.base_url)
    report: dict[str, Any] = {
        "status": "FAIL",
        "memoryd": str(memoryd),
        "model_base_url": args.base_url,
        "model": model,
        "checks": [],
    }

    def record(name: str, condition: bool, detail: Any = None) -> None:
        report["checks"].append({"name": name, "passed": bool(condition), "detail": detail})
        print(f"[{'PASS' if condition else 'FAIL'}] {name}", file=sys.stderr)
        check(condition, f"{name}: {detail}")

    daemon: subprocess.Popen[str] | None = None
    client: MemoryClient | None = None
    with tempfile.TemporaryDirectory(prefix="memory-summary-model-") as raw:
        root = Path(raw)
        registry = root / "registry"
        registry.mkdir(mode=0o755)
        account_secret = root / "account-secret"
        account_secret.write_text(uuid.uuid4().hex + uuid.uuid4().hex + "\n", encoding="utf-8")
        account_secret.chmod(0o600)
        socket_path = root / "memoryd.sock"
        command = [
            str(memoryd),
            "--socket",
            str(socket_path),
            "--database",
            str(root / "memory.db"),
            "--runtime-registry-dir",
            str(registry),
            "--key-package",
            str(root / "memory.keypkg"),
            "--account-secret-file",
            str(account_secret),
            "--initialize-key-package",
            "--background-interval-seconds",
            "3600",
        ]
        daemon = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            wait_for_socket(socket_path, daemon)
            record("isolated-memoryd-started", True)
            register_identity(registry)
            client = MemoryClient.connect(
                socket_path,
                timeout=max(30.0, args.max_latency_ms / 1000 + 10),
                account_id=ACCOUNT_ID,
                agent_name=AGENT_NAME,
            )
            identity_detail = {
                "account_id": client.identity.account_id,
                "agent_id": client.identity.agent_id,
                "agent_name": client.identity.agent_name,
                "agent_instance_id": client.identity.agent_instance_id,
                "memory_space_id": client.identity.memory_space_id,
            }
            record("sdk-identity-bound", client.identity.agent_id == AGENT_ID, identity_detail)

            model_config = {
                "enabled": True,
                "base_url": args.base_url,
                "model": model,
                "auth": "none",
                "credential_env": None,
                "max_context_tokens": 8192,
                "max_output_tokens": 512,
                "cpu_threads": 4,
                "ram_budget_mb": 4096,
                "max_latency_ms": args.max_latency_ms,
            }
            configured = client.privacy.update_settings(
                summary_interval_turns=5,
                summary_retention_days=7,
                summary_model=model_config,
                idempotency_key=f"summary-model-config-{uuid.uuid4()}",
            )
            record(
                "summary-model-enabled",
                configured["summary_model"]["enabled"] is True
                and configured["summary_model"]["base_url"] == args.base_url,
                configured["summary_model"],
            )

            session_id = f"summary-model-{uuid.uuid4().hex[:12]}"
            turns = [
                ("请记住，Memory HTTP Adapter 固定使用 5168 端口。", "已记录 Adapter 端口为 5168。"),
                ("项目决定每五个完整对话轮次自动生成摘要。", "已记录五轮自动摘要决策。"),
                ("待办是补充 K3 摘要质量回归测试。", "已将 K3 摘要质量回归列为待办。"),
                ("云端备份当前保持关闭。", "已记录云端备份当前关闭。"),
                ("摘要失败时必须保留原始 L1 事件。", "已记录失败时保留原始事件。"),
            ]
            latencies = [
                append_turn(client, session_id, index, user, assistant)
                for index, (user, assistant) in enumerate(turns, 1)
            ]
            context = client.sessions.get_context(
                session_id, include_events=True, include_summary=True, event_limit=20
            )
            summary_row = context.get("summary") or {}
            summary = summary_row.get("summary") or {}
            encoded_summary = json.dumps(summary, ensure_ascii=False)
            record("five-turn-auto-summary", len(context.get("events") or []) == 10, {
                "event_count": len(context.get("events") or []),
                "assistant_append_latency_ms": [round(value, 2) for value in latencies],
            })
            record("model-summary-persisted", summary_row.get("producer") == "memoryd.model", summary_row)
            record(
                "model-summary-versioned",
                summary_row.get("revision") == 2 and summary_row.get("source_event_sequence") == 10,
                {"revision": summary_row.get("revision"), "source_event_sequence": summary_row.get("source_event_sequence")},
            )
            expected_facts = ["5168", "K3", "原始"]
            record(
                "key-facts-preserved",
                all(value in encoded_summary for value in expected_facts),
                {"expected": expected_facts, "summary": summary},
            )
            report["model_path"] = {
                "session_id": session_id,
                "producer": summary_row.get("producer"),
                "model_ref": summary_row.get("model_ref"),
                "revision": summary_row.get("revision"),
                "source_event_sequence": summary_row.get("source_event_sequence"),
                "assistant_append_latency_ms": [round(value, 2) for value in latencies],
                "summary": summary,
            }

            unavailable_config = dict(model_config)
            unavailable_config["base_url"] = "http://127.0.0.1:1/v1"
            client.privacy.update_settings(
                summary_interval_turns=50,
                summary_model=unavailable_config,
                idempotency_key=f"summary-model-fallback-config-{uuid.uuid4()}",
            )
            fallback_session = f"summary-fallback-{uuid.uuid4().hex[:12]}"
            append_turn(
                client,
                fallback_session,
                1,
                "请记住，降级测试必须保留这条原始事件。",
                "已记录降级测试要求。",
            )
            fallback = client._call(
                "sessions.summarize",
                {
                    "session_id": fallback_session,
                    "interval_turns": 1,
                    "keep_raw_turns": 5,
                    "force": True,
                },
                idempotency_key=f"summary-fallback-{uuid.uuid4()}",
            )
            fallback_context = client.sessions.get_context(
                fallback_session, include_events=True, include_summary=True, event_limit=10
            )
            fallback_row = fallback_context.get("summary") or {}
            record(
                "unavailable-model-degrades-to-rules",
                fallback.get("degraded_reason") == "model_unavailable"
                and fallback_row.get("producer") == "memoryd.rules",
                {"summarize_result": fallback, "producer": fallback_row.get("producer")},
            )
            record(
                "fallback-preserves-l1-events",
                len(fallback_context.get("events") or []) == 2,
                {"event_count": len(fallback_context.get("events") or [])},
            )
            report["fallback_path"] = {
                "session_id": fallback_session,
                "degraded_reason": fallback.get("degraded_reason"),
                "producer": fallback_row.get("producer"),
                "event_count": len(fallback_context.get("events") or []),
            }
            report["status"] = "PASS"
        finally:
            if client is not None:
                client.close()
            daemon_stderr = stop_process(daemon)
            if daemon_stderr.strip():
                report["memoryd_stderr_tail"] = daemon_stderr[-2000:]
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memoryd", default="/usr/libexec/bianbu-agent-memoryd")
    parser.add_argument("--base-url", default="http://127.0.0.1:8081/v1")
    parser.add_argument("--model", default="")
    parser.add_argument("--max-latency-ms", type=int, default=15000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = run(args)
    except Exception as error:
        report = {
            "status": "FAIL",
            "error_type": type(error).__name__,
            "error": str(error),
        }
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    print(encoded)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
