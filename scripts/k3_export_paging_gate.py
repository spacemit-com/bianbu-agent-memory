#!/usr/bin/env python3
"""K3 field gate: >500 L1 events survive one-click export and re-import."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sdk" / "python" / "src"))
from bianbu_agent_sdk.memory import MemoryClient, MemoryKind, SyncPolicy  # noqa: E402
from bianbu_agent_sdk.memory.bibit_adapter import _device_id, _start_time_ticks  # noqa: E402

ACCOUNT_ID = "bianbu"
AGENT_ID = "com.bianbu.runtime"
AGENT_NAME = "com.bianbu.0091ef44d6cb"
EVENTS = 600


def wait_socket(path: Path, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError((process.stderr.read() or "")[-2000:])
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(path))
            return
        except OSError:
            time.sleep(0.05)
        finally:
            probe.close()
    raise TimeoutError(str(path))


def wait_tcp(port: int, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError((process.stderr.read() or "")[-2000:])
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.connect(("127.0.0.1", port))
            return
        except OSError:
            time.sleep(0.05)
        finally:
            probe.close()
    raise TimeoutError(str(port))


def free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


def register(registry: Path, capabilities: list[str]) -> Path:
    pid = os.getpid()
    start = _start_time_ticks(pid)
    device = _device_id()
    record = {
        "uid": os.getuid(),
        "gid": os.getgid(),
        "pid_start_time_ticks": start,
        "account_id": ACCOUNT_ID,
        "device_id": device,
        "agent_id": AGENT_ID,
        "agent_name": AGENT_NAME,
        "agent_instance_id": f"export-gate.{pid}.{start}",
        "memory_space_id": f"{ACCOUNT_ID}.{AGENT_ID}.{device}",
        "capabilities": capabilities,
        "executable_path": str(Path(f"/proc/{pid}/exe").resolve()),
        "expires_at_ms": int(time.time() * 1000) + 86_400_000,
    }
    target = registry / f"{pid}.json"
    temporary = registry / f".{pid}.json.tmp"
    temporary.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
    os.chmod(temporary, 0o640)
    os.replace(temporary, target)
    os.chmod(target, 0o640)
    return target


def invoke(port: int, operation: str, params: dict) -> dict:
    body = json.dumps({
        "protocolVersion": 1,
        "requestId": str(uuid.uuid4()),
        "serviceId": "memory",
        "operationId": operation,
        "params": params,
    }, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"http://127.0.0.1:{port}/bibit/v1/invoke", data=body, method="POST"
    )
    request.add_header("Content-Type", "application/json")
    with urlopen(request, timeout=120) as response:
        payload = json.loads(response.read())
    if payload.get("ok") is not True:
        raise RuntimeError(json.dumps(payload, ensure_ascii=False))
    return payload["result"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--memoryd", type=Path, default=Path("target/release/bianbu-agent-memoryd"))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="export-gate-") as raw:
        root = Path(raw)
        registry = root / "registry"
        registry.mkdir(mode=0o755)
        account_secret = root / "account-secret"
        account_secret.write_text(uuid.uuid4().hex + uuid.uuid4().hex + "\n", encoding="utf-8")
        account_secret.chmod(0o600)
        confirmation = root / "adapter-confirmation.secret"
        confirmation.write_bytes(secrets.token_hex(32).encode())
        confirmation.chmod(0o600)
        socket_path = root / "memoryd.sock"
        daemon = subprocess.Popen([
            str(args.memoryd), "--socket", str(socket_path),
            "--database", str(root / "memory.db"),
            "--runtime-registry-dir", str(registry),
            "--key-package", str(root / "memory.keypkg"),
            "--account-secret-file", str(account_secret),
            "--adapter-confirmation-secret-file", str(confirmation),
            "--background-interval-seconds", "3600",
            "--initialize-key-package",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        adapter = None
        try:
            wait_socket(socket_path, daemon)
            port = free_port()
            adapter = subprocess.Popen([
                sys.executable, "-m", "bianbu_agent_sdk.memory.bibit_adapter",
                "--socket", str(socket_path), "--registry-dir", str(registry),
                "--secret-file", str(confirmation), "--host", "127.0.0.1",
                "--port", str(port), "--account-id", ACCOUNT_ID,
                "--agent-id", AGENT_ID, "--agent-name", AGENT_NAME,
            ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            wait_tcp(port, adapter)
            registration = register(registry, [
                "memory.short_term.write", "memory.short_term.read",
                "memory.long_term.write", "memory.long_term.read",
                "memory.privacy.read", "memory.privacy.write",
                "memory.audit.read", "memory.forget",
            ])
            client = MemoryClient.connect(
                socket_path, timeout=120.0, account_id=ACCOUNT_ID,
                agent_name=AGENT_NAME,
            )
            try:
                for index in range(EVENTS):
                    client.sessions.append_event(
                        session_id="export-gate-session",
                        event_type="user_message",
                        payload={"text": f"bulk export event {index}"},
                        idempotency_key=f"export-gate-{index}",
                    )
                client.long_term.put(
                    kind=MemoryKind.PREFERENCE, text="bulk export keeps this preference",
                    sync_policy=SyncPolicy.LOCAL_ONLY, idempotency_key="export-gate-memory",
                )
                before = client.stats()
                exported = invoke(port, "privacy.export_data", {
                    "limit_per_collection": 500, "include_audit": False,
                })
                events = len(exported.get("session_events") or [])
                memories = len(exported.get("long_term_memories") or [])
                pages = int(exported.get("page_count") or 0)
                assert exported.get("complete") is True, "export was not marked complete"
                assert events == before["session_events"], (events, before["session_events"])
                assert events >= EVENTS, events
                assert pages >= 2, pages
                assert memories >= 1, memories
                # Destructive operations go through the same trusted Adapter
                # path as the settings page; the Adapter mints the one-shot
                # token after checking that its confirmation secret is loaded.
                invoke(port, "privacy.clear_all", {"reason_code": "user_request"})
                cleared = client.stats()
                assert cleared["session_events"] == 0
                imported = invoke(port, "privacy.import_data", {"payload": exported})
                assert imported["imported"]["session_events"] == events
                assert imported["imported"]["long_term_memories"] == memories
                after = client.stats()
                assert after["session_events"] == events
                print(json.dumps({
                    "status": "EXPORT_GATE_OK",
                    "events_written": EVENTS,
                    "events_exported": events,
                    "memories_exported": memories,
                    "pages": pages,
                    "snapshot": exported.get("snapshot_at"),
                    "reimported": imported["imported"],
                    "stats_after": {
                        "session_events": after["session_events"],
                        "long_term_memories": after["long_term_memories"],
                    },
                }, ensure_ascii=False, indent=2))
            finally:
                client.close()
                registration.unlink(missing_ok=True)
        finally:
            for process in (adapter, daemon):
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
