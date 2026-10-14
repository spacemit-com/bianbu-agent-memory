"""BiBit ``service:memory`` HTTP adapter.

The BiBit renderer cannot access the memoryd Unix socket directly.  This small
loopback gateway is the only process that translates the External Service
Gateway v1 envelope into the Python SDK and therefore into memoryd.  It
deliberately exposes only the operations needed by the Memory settings page.
The adapter registers its own short-lived, least-privilege identity so memoryd
keeps the normal production peer/registry checks enabled.

The HTTP endpoint authenticates nobody: any peer that can reach it can read,
export or erase the local memory store.  It therefore binds loopback only.
Remote development must tunnel to the device
(``ssh -L 5168:127.0.0.1:5168 <device>``) instead of widening ``--host``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .client import DEFAULT_SOCKET, MemoryClient

PROTOCOL_VERSION = 1
SERVICE_ID = "memory"
# BiBit 的 External Service Gateway 固定 POST ``<baseUrl>/bibit/v1/invoke``；
# 裸 ``/invoke`` 保留作为兼容入口，避免旧验收脚本和已部署探针失效。
INVOKE_PATH = "/bibit/v1/invoke"
INVOKE_PATHS = frozenset({INVOKE_PATH, "/invoke"})
MAX_BODY_BYTES = 128 * 1024
DEFAULT_REGISTRY = "/run/bianbu/agent-runtime/memory-registry"
DEFAULT_SECRET_FILE = "/etc/bianbu-agent-memory/adapter-confirmation.secret"
ALLOWED_OPERATIONS = frozenset(
    {
        "privacy.settings.get",
        "privacy.settings.update",
        "privacy.clear_history",
        "privacy.clear_all",
        "privacy.export_data",
    }
)
CAPABILITIES = [
    "memory.privacy.read",
    "memory.privacy.write",
    "memory.forget",
]


def _start_time_ticks(pid: int) -> int:
    fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1]
    return int(fields.split()[19])


def _device_id() -> str:
    configured = os.environ.get("BIANBU_AGENT_MEMORY_DEVICE_ID", "").strip()
    if configured:
        return configured
    for address in sorted(Path("/sys/class/net").glob("*/address")):
        name = address.parent.name
        if name == "lo" or name.startswith(("docker", "veth", "br-", "virbr", "tun", "tap")):
            continue
        value = address.read_text(encoding="ascii").strip().lower()
        octets = value.split(":")
        if len(octets) == 6:
            try:
                raw = bytes(int(part, 16) for part in octets)
            except ValueError:
                continue
            if any(raw) and not raw[0] & 1:
                return ":".join(f"{part:02x}" for part in raw)
    return "unknown-device"


def register_adapter(
    registry_dir: str,
    account_id: str,
    agent_id: str,
    agent_name: str,
) -> Path:
    """Create a registry record owned by this process' UID."""

    root = Path(registry_dir)
    if not root.is_dir():
        raise RuntimeError(f"memory registry is unavailable: {root}")
    pid = os.getpid()
    start = _start_time_ticks(pid)
    device = _device_id()
    executable = str((Path(f"/proc/{pid}/exe")).resolve())
    record = {
        "uid": os.getuid(),
        "gid": os.getgid(),
        "pid_start_time_ticks": start,
        "account_id": account_id,
        "device_id": device,
        "agent_id": agent_id,
        "agent_name": agent_name,
        "agent_instance_id": f"bibit-memory-adapter.{pid}.{start}",
        "memory_space_id": f"{account_id}.{agent_id}.{device}",
        "capabilities": CAPABILITIES,
        "executable_path": executable,
        "expires_at_ms": int(time.time() * 1000) + 24 * 60 * 60 * 1000,
    }
    target = root / f"{pid}.json"
    temporary = root / f".{pid}.json.tmp"
    temporary.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
    os.chmod(temporary, 0o640)
    os.replace(temporary, target)
    os.chmod(target, 0o640)
    return target


def _confirmation_token(secret: bytes, method: str, ttl_seconds: int = 60) -> str:
    expiry = int(time.time() * 1000) + ttl_seconds * 1000
    nonce = secrets.token_urlsafe(24)
    payload = f"v1:{expiry}:{nonce}:{method}".encode("utf-8")
    signature = hashlib.sha256(secret + payload).hexdigest()
    return f"v1:{expiry}:{nonce}:{signature}"


def _error_code(error: Exception) -> str:
    context = getattr(error, "context", None)
    code = str(getattr(context, "code", "ServerUnavailable"))
    return {
        "Unauthenticated": "unauthorized",
        "PermissionDenied": "unauthorized",
        "InvalidArgument": "unknown",
        "ResourceExhausted": "resource_exhausted",
        "DeadlineExceeded": "timeout",
        "BackupUnavailable": "unavailable",
        "ServerUnavailable": "unavailable",
    }.get(code, "unknown")


class AdapterState:
    def __init__(self, client: MemoryClient, confirmation_secret: bytes | None) -> None:
        self.client = client
        self.confirmation_secret = confirmation_secret

    def invoke(self, operation: str, params: dict[str, Any]) -> Any:
        if operation not in ALLOWED_OPERATIONS:
            raise ValueError(f"operation is not exposed by the BiBit Memory adapter: {operation}")
        if operation == "privacy.settings.get":
            return self.client.privacy.settings()
        if operation == "privacy.settings.update":
            return self.client.privacy.update_settings(**params)
        if operation == "privacy.export_data":
            # Strict Memory privacy policy treats export as a sensitive data
            # boundary.  Mint a one-shot token through the same trusted local
            # adapter path used by destructive operations.
            settings = self.client.privacy.settings()
            confirmation = None
            if settings.get("sensitive_action_confirmation") == "strict":
                if self.confirmation_secret is None:
                    raise RuntimeError("strict export confirmation is not configured for this adapter")
                confirmation = _confirmation_token(self.confirmation_secret, operation)
            return self.client.privacy.export_data(
                limit_per_collection=max(1, min(int(params.get("limit_per_collection", 500)), 500)),
                include_audit=bool(params.get("include_audit", True)),
                confirm_token=confirmation,
            )
        if self.confirmation_secret is None:
            raise RuntimeError("destructive Memory operations are not configured for this adapter")
        token = _confirmation_token(self.confirmation_secret, operation)
        reason = str(params.get("reason_code") or "user_request")
        if operation == "privacy.clear_history":
            return self.client.privacy.clear_history(confirm_token=token, reason_code=reason)
        return self.client.privacy.clear_all(confirm_token=token, reason_code=reason)


class Handler(BaseHTTPRequestHandler):
    server: "AdapterServer"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _cors(self) -> None:
        # Production is same-origin over loopback and must not advertise itself
        # to arbitrary web pages; only local development sets --allow-origin.
        if self.server.allow_origin:
            self.send_header("Access-Control-Allow-Origin", self.server.allow_origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path not in {"/", "/health"}:
            self._write(404, {"error": "not_found"})
            return
        self._write(200, {
            "status": "ok",
            "serviceId": SERVICE_ID,
            "protocolVersion": PROTOCOL_VERSION,
            "invokePath": INVOKE_PATH,
        })

    def do_OPTIONS(self) -> None:
        if self.path not in INVOKE_PATHS:
            self.send_error(404)
            return
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self) -> None:
        if self.path not in INVOKE_PATHS:
            self._write(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("request body is missing or too large")
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            response = self._invoke(request)
            self._write(200, response)
        except ValueError as error:
            self._write(400, {"error": str(error)})
        except Exception as error:  # gateway responses must remain valid JSON
            request_id = "unknown"
            try:
                request_id = str(request.get("requestId", "unknown"))
            except UnboundLocalError:
                pass
            self._write(200, {
                "protocolVersion": PROTOCOL_VERSION,
                "requestId": request_id,
                "ok": False,
                "error": {"code": _error_code(error), "message": str(error), "retryable": False},
            })

    def _invoke(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("protocolVersion") != PROTOCOL_VERSION:
            raise ValueError("unsupported External Service Gateway protocol")
        request_id = str(request.get("requestId") or "")
        operation = str(request.get("operationId") or "")
        if not request_id or request.get("serviceId") != SERVICE_ID:
            raise ValueError("invalid service request")
        params = request.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        result = self.server.state.invoke(operation, params)
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "requestId": request_id,
            "ok": True,
            "result": result,
        }

    def _write(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)


class AdapterServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], state: AdapterState, allow_origin: str) -> None:
        super().__init__(address, Handler)
        self.state = state
        self.allow_origin = allow_origin


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BiBit service:memory HTTP adapter")
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--registry-dir", default=DEFAULT_REGISTRY)
    parser.add_argument("--account-id", default=os.environ.get("BIANBU_AGENT_MEMORY_ACCOUNT_ID", "bianbu"))
    parser.add_argument("--agent-id", default=os.environ.get("BIANBU_AGENT_MEMORY_AGENT_ID", "com.bianbu.runtime"))
    parser.add_argument("--agent-name", default=os.environ.get("BIANBU_AGENT_MEMORY_AGENT_NAME", "com.bianbu.0091ef44d6cb"))
    parser.add_argument("--secret-file", default=DEFAULT_SECRET_FILE)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5168)
    parser.add_argument("--allow-origin", default="")
    args = parser.parse_args(argv)
    secret_path = Path(args.secret_file)
    secret = secret_path.read_bytes() if secret_path.is_file() else None
    registration = register_adapter(
        args.registry_dir, args.account_id, args.agent_id, args.agent_name
    )
    client = MemoryClient.connect(
        args.socket, timeout=10.0, account_id=args.account_id, agent_name=args.agent_name
    )
    server = AdapterServer((args.host, args.port), AdapterState(client, secret), args.allow_origin)

    def stop(*_: object) -> None:
        # ``BaseServer.shutdown`` waits for ``serve_forever`` to return and
        # therefore deadlocks when called from the signal handler running on
        # that same thread.  Keep signal handling non-blocking so systemd can
        # stop/restart the adapter deterministically.
        threading.Thread(target=server.shutdown, name="memory-adapter-shutdown", daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        client.close()
        try:
            registration.unlink()
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
