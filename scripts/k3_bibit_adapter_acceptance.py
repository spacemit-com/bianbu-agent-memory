#!/usr/bin/env python3
"""MEM-INT-02-01 / MEM-INT-02-02 acceptance: BiBit settings-page adapter.

Unlike ``k3_requirements_acceptance.py`` (development identity), this script
exercises the *production* topology introduced by the uncommitted BiBit
settings-page work, in an isolated namespace:

  - memoryd runs WITHOUT ``--development-identity``: peer identity comes from
    the runtime registry, exactly like the installed service;
  - memoryd loads ``--adapter-confirmation-secret-file``: destructive and
    strict-export confirmations use the one-shot ``v1:expiry:nonce:HMAC``
    tokens instead of the development token;
  - the real ``bibit_adapter`` gateway serves the five privacy operations on a
    loopback HTTP port, registering its own least-privilege identity;
  - a separate seeded test identity shares the adapter's account/agent
    namespace so clear/export results are observable.

Covers: settings round-trip over HTTP, envelope validation (serviceId,
protocolVersion, unknown operationId, malformed/oversized body), CORS
preflight, destructive operations with real minted tokens, one-shot/expired/
method-bound token rejection, loose/standard/strict confirmation levels,
memory switch fail-closed, memoryd restart recovery, fail-closed adapter
without a secret, and the adapter's minimal registry capabilities.

Usage (K3, from the repository root after ``cargo build --release``):

    python3 scripts/k3_bibit_adapter_acceptance.py \
        --memoryd target/release/bianbu-agent-memoryd
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets as pysecrets
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from bianbu_agent_sdk.memory import MemoryKind, MemoryClient, SyncPolicy
    from bianbu_agent_sdk.memory.bibit_adapter import _device_id, _start_time_ticks
    from bianbu_agent_sdk.memory.errors import MemoryDisabled, PermissionDenied
except ModuleNotFoundError:  # allow running straight from a fresh checkout
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sdk" / "python" / "src"))
    from bianbu_agent_sdk.memory import MemoryKind, MemoryClient, SyncPolicy
    from bianbu_agent_sdk.memory.bibit_adapter import _device_id, _start_time_ticks
    from bianbu_agent_sdk.memory.errors import MemoryDisabled, PermissionDenied

ACCOUNT_ID = "bianbu"
AGENT_ID = "com.bianbu.runtime"
AGENT_NAME = "com.bianbu.0091ef44d6cb"
# BiBit 网关固定 POST <baseUrl>/bibit/v1/invoke；/invoke 仅为兼容入口。
INVOKE_PATH = "/bibit/v1/invoke"
ADAPTER_CAPABILITIES = [
    "memory.privacy.read",
    "memory.privacy.write",
    "memory.forget",
]
SEEDER_CAPABILITIES = [
    "memory.instruction.read", "memory.instruction.write",
    "memory.short_term.read", "memory.short_term.write",
    "memory.long_term.read", "memory.long_term.write",
    "memory.audit.read", "memory.privacy.read", "memory.privacy.write",
    "memory.forget", "memory.account.read",
]


def wait_for_socket(path: Path, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr else ""
            raise RuntimeError(f"daemon exited during startup: {stderr.strip()[-2000:]}")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(path))
            return
        except OSError:
            time.sleep(0.05)
        finally:
            probe.close()
    raise TimeoutError(str(path))


def wait_for_tcp(port: int, process: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr else ""
            raise RuntimeError(f"adapter exited during startup: {stderr.strip()[-2000:]}")
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.connect(("127.0.0.1", port))
            return
        except OSError:
            time.sleep(0.05)
        finally:
            probe.close()
    raise TimeoutError(f"127.0.0.1:{port}")


def free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


def register_identity(registry_dir: Path, capabilities: list[str]) -> Path:
    """Register this process in the runtime registry (same shape as the
    adapter's own registration, but with a caller-chosen capability set)."""
    pid = os.getpid()
    start = _start_time_ticks(pid)
    device = _device_id()
    executable = str(Path(f"/proc/{pid}/exe").resolve())
    record = {
        "uid": os.getuid(),
        "gid": os.getgid(),
        "pid_start_time_ticks": start,
        "account_id": ACCOUNT_ID,
        "device_id": device,
        "agent_id": AGENT_ID,
        "agent_name": AGENT_NAME,
        "agent_instance_id": f"k3-acceptance.{pid}.{start}",
        "memory_space_id": f"{ACCOUNT_ID}.{AGENT_ID}.{device}",
        "capabilities": capabilities,
        "executable_path": executable,
        "expires_at_ms": int(time.time() * 1000) + 24 * 60 * 60 * 1000,
    }
    target = registry_dir / f"{pid}.json"
    temporary = registry_dir / f".{pid}.json.tmp"
    temporary.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
    os.chmod(temporary, 0o640)
    os.replace(temporary, target)
    os.chmod(target, 0o640)
    return target


def mint_token(secret: bytes, method: str, *, ttl_seconds: int = 60, expiry_ms: int | None = None) -> str:
    expiry = expiry_ms if expiry_ms is not None else int(time.time() * 1000) + ttl_seconds * 1000
    nonce = pysecrets.token_urlsafe(24)
    payload = f"v1:{expiry}:{nonce}:{method}".encode("utf-8")
    signature = hashlib.sha256(secret + payload).hexdigest()
    return f"v1:{expiry}:{nonce}:{signature}"


class HttpResult:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def http_post(port: int, payload: bytes | str, path: str = "/invoke", method: str = "POST") -> HttpResult:
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    request = Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=15.0) as response:
            return HttpResult(response.status, response.read())
    except HTTPError as error:
        return HttpResult(error.code, error.read())


def http_get(port: int, path: str = "/") -> HttpResult:
    request = Request(f"http://127.0.0.1:{port}{path}", method="GET")
    try:
        with urlopen(request, timeout=15.0) as response:
            return HttpResult(response.status, response.read())
    except HTTPError as error:
        return HttpResult(error.code, error.read())


def http_options(port: int, path: str = "/invoke") -> HttpResult:
    request = Request(f"http://127.0.0.1:{port}{path}", data=b"", method="OPTIONS")
    try:
        with urlopen(request, timeout=15.0) as response:
            return HttpResult(response.status, response.read())
    except HTTPError as error:
        return HttpResult(error.code, error.read())


def invoke(port: int, operation: str, params: dict[str, Any] | None = None, request_id: str | None = None,
           service_id: str = "memory", protocol_version: int = 1,
           path: str = INVOKE_PATH) -> HttpResult:
    envelope = {
        "protocolVersion": protocol_version,
        "requestId": request_id or str(uuid.uuid4()),
        "serviceId": service_id,
        "operationId": operation,
        "params": params or {},
    }
    return http_post(port, json.dumps(envelope, ensure_ascii=False), path=path)


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def run(args: argparse.Namespace) -> dict[str, Any]:
    memoryd = args.memoryd.resolve()
    if not memoryd.is_file():
        raise FileNotFoundError(memoryd)

    tests: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "") -> bool:
        tests.append({"name": name, "ok": bool(ok), "detail": detail[:500]})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail[:200]}" if (detail and not ok) else ""),
              file=sys.stderr)
        return bool(ok)

    with tempfile.TemporaryDirectory(prefix="bibit-adapter-acceptance-") as raw:
        root = Path(raw)
        registry = root / "registry"
        registry.mkdir(mode=0o755)
        account_secret = root / "account-secret"
        account_secret.write_text(uuid.uuid4().hex + uuid.uuid4().hex + "\n", encoding="utf-8")
        account_secret.chmod(0o600)
        shared_secret_path = root / "adapter-confirmation.secret"
        shared_secret = pysecrets.token_hex(32).encode("utf-8")
        shared_secret_path.write_bytes(shared_secret)
        shared_secret_path.chmod(0o600)
        socket_path = root / "memoryd.sock"

        def daemon_command(initialize: bool) -> list[str]:
            command = [
                str(memoryd),
                "--socket", str(socket_path),
                "--database", str(root / "memory.db"),
                "--runtime-registry-dir", str(registry),
                "--key-package", str(root / "memory.keypkg"),
                "--account-secret-file", str(account_secret),
                "--adapter-confirmation-secret-file", str(shared_secret_path),
                "--background-interval-seconds", "3600",
            ]
            if initialize:
                command.append("--initialize-key-package")
            return command

        daemon = subprocess.Popen(daemon_command(True), stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, text=True)
        try:
            wait_for_socket(socket_path, daemon)
            check("daemon-start-production-mode", True, "memoryd started without development identity")

            port = free_port()
            adapter_port = args.port or port
            adapter = subprocess.Popen(
                [sys.executable, "-m", "bianbu_agent_sdk.memory.bibit_adapter",
                 "--socket", str(socket_path),
                 "--registry-dir", str(registry),
                 "--secret-file", str(shared_secret_path),
                 "--host", "127.0.0.1", "--port", str(adapter_port),
                 "--account-id", ACCOUNT_ID, "--agent-id", AGENT_ID, "--agent-name", AGENT_NAME],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            try:
                wait_for_tcp(adapter_port, adapter)
                check("adapter-start", True, f"adapter listening on 127.0.0.1:{adapter_port}")

                root_response = http_get(adapter_port)
                root_body = root_response.json()
                check("root-status-get",
                      root_response.status == 200 and root_body.get("status") == "ok"
                      and root_body.get("serviceId") == "memory"
                      and root_body.get("invokePath") == INVOKE_PATH,
                      f"status={root_response.status} body={root_response.body[:200]!r}")

                health = http_get(adapter_port, "/health")
                check("health-status-get",
                      health.status == 200 and health.json() == root_body,
                      f"status={health.status} body={health.body[:200]!r}")

                unknown_get = http_get(adapter_port, "/missing")
                check("unknown-get-path-rejected",
                      unknown_get.status == 404,
                      f"status={unknown_get.status} body={unknown_get.body[:200]!r}")

                adapter_record = registry / f"{adapter.pid}.json"
                record_ok = adapter_record.is_file()
                capabilities: list[str] = []
                if record_ok:
                    record = json.loads(adapter_record.read_text(encoding="utf-8"))
                    capabilities = list(record.get("capabilities", []))
                check("adapter-registry-record", record_ok and capabilities == ADAPTER_CAPABILITIES,
                      f"capabilities={capabilities}")

                registration = register_identity(registry, SEEDER_CAPABILITIES)
                client = MemoryClient.connect(socket_path, timeout=15.0,
                                              account_id=ACCOUNT_ID, agent_name=AGENT_NAME)
                try:
                    # ---- read path over HTTP ----
                    response = invoke(adapter_port, "privacy.settings.get")
                    body = response.json()
                    settings = (body.get("result") or {}) if isinstance(body, dict) else {}
                    check("settings-get-http",
                          response.status == 200 and body.get("ok") is True
                          and settings.get("memory_enabled") is True
                          and body.get("protocolVersion") == 1,
                          f"status={response.status} body={response.body[:200]!r}")

                    legacy = invoke(adapter_port, "privacy.settings.get", path="/invoke")
                    check("settings-get-legacy-invoke-path",
                          legacy.status == 200 and legacy.json().get("ok") is True,
                          f"status={legacy.status} body={legacy.body[:200]!r}")

                    # ---- settings round-trip over HTTP ----
                    response = invoke(adapter_port, "privacy.settings.update", {"retention_days": 7})
                    updated = (response.json().get("result") or {})
                    current = client.privacy.settings()
                    check("settings-update-retention-http",
                          response.json().get("ok") is True and updated.get("retention_days") == 7
                          and current.get("retention_days") == 7,
                          f"response={updated} settings={current.get('retention_days')}")

                    response = invoke(adapter_port, "privacy.settings.update", {"retention_days": 0})
                    check("settings-update-retention-revert",
                          response.json().get("ok") is True
                          and (response.json().get("result") or {}).get("retention_days") == 0,
                          f"response={response.body[:200]!r}")

                    response = invoke(adapter_port, "privacy.settings.update", {"retention_days": 45})
                    body = response.json()
                    check("settings-update-invalid-rejected",
                          response.status == 200 and body.get("ok") is False,
                          f"status={response.status} body={response.body[:200]!r}")

                    # Cloud backup is retired: a 0.1.28-style client may still send
                    # `backup_frequency`. It must be accepted (so the rest of the
                    # settings write is not rejected) but must not resurface in the
                    # response.
                    response = invoke(adapter_port, "privacy.settings.update",
                                      {"backup_frequency": "monthly", "retention_days": 7})
                    payload = response.json()
                    check("settings-update-retired-backup-key-ignored",
                          payload.get("ok") is True
                          and "backup_frequency" not in (payload.get("result") or {}),
                          f"body={response.body[:200]!r}")
                    response = invoke(adapter_port, "privacy.settings.update", {"retention_days": 0})
                    check("settings-update-retention-restored",
                          response.json().get("ok") is True,
                          f"body={response.body[:200]!r}")

                    # ---- confirmation levels (new `loose` value needs the new binary) ----
                    response = invoke(adapter_port, "privacy.settings.update",
                                      {"sensitive_action_confirmation": "loose"})
                    loose_ok = response.json().get("ok") is True and \
                        (response.json().get("result") or {}).get("sensitive_action_confirmation") == "loose"
                    response = invoke(adapter_port, "privacy.settings.update",
                                      {"sensitive_action_confirmation": "standard"})
                    check("confirmation-level-loose-accepted",
                          loose_ok and (response.json().get("result") or {})
                          .get("sensitive_action_confirmation") == "standard",
                          f"body={response.body[:200]!r}")

                    # ---- envelope validation ----
                    response = invoke(adapter_port, "long_term.put",
                                      {"kind": "fact", "text": "must not be exposed"})
                    check("unknown-operation-rejected",
                          response.status == 400 and b"not exposed" in response.body,
                          f"status={response.status} body={response.body[:200]!r}")

                    response = invoke(adapter_port, "privacy.settings.get", service_id="filesystem")
                    check("wrong-service-id-rejected", response.status == 400,
                          f"status={response.status} body={response.body[:200]!r}")

                    response = invoke(adapter_port, "privacy.settings.get", protocol_version=2)
                    check("wrong-protocol-version-rejected", response.status == 400,
                          f"status={response.status} body={response.body[:200]!r}")

                    response = http_post(adapter_port, "{not json")
                    check("malformed-json-rejected", response.status == 400,
                          f"status={response.status}")

                    response = http_post(adapter_port, "x" * (128 * 1024 + 1))
                    check("oversized-body-rejected", response.status == 400,
                          f"status={response.status}")

                    response = http_post(adapter_port, json.dumps({"protocolVersion": 1}),
                                         path="/other")
                    check("unknown-path-rejected", response.status == 404,
                          f"status={response.status}")

                    preflight = http_options(adapter_port)
                    check("cors-preflight",
                          preflight.status == 204,
                          f"status={preflight.status}")

                    # ---- L3 recall through the production identity path ----
                    preference = client.long_term.put(
                        kind=MemoryKind.PREFERENCE,
                        text="terminal color preference: dark",
                        tags=("personal-agent", "slot:appearance.terminal_color_scheme"),
                        sync_policy=SyncPolicy.LOCAL_ONLY,
                        idempotency_key=f"adapter-acceptance-{uuid.uuid4().hex}",
                    )
                    preference = client.long_term.revise(
                        preference.memory_id,
                        text="terminal color preference: light",
                        expected_revision=preference.revision,
                        tags=("personal-agent", "slot:appearance.terminal_color_scheme"),
                        idempotency_key=f"adapter-acceptance-revise-{uuid.uuid4().hex}",
                    )
                    headers = client.long_term.search_headers("terminal color", top_k=10)
                    records = client.long_term.get_many([h.memory_id for h in headers]) if headers else []
                    authoritative = [r for r in records if "slot:appearance.terminal_color_scheme" in r.tags]
                    check("l3-recall-authoritative-slot",
                          len(authoritative) == 1 and authoritative[0].text.endswith("light"),
                          f"matched={len(authoritative)}")

                    # ---- destructive operations through the adapter ----
                    def seed_session_events(tag: str) -> None:
                        run_id = f"adapter-acceptance-{tag}-{uuid.uuid4().hex[:8]}"
                        client.sessions.append_event(
                            session_id=f"adapter-acceptance-session-{tag}",
                            event_type="user_message",
                            payload={"run_id": run_id, "status": "completed",
                                     "text": f"acceptance seed {tag}"},
                            sync_policy=SyncPolicy.LOCAL_ONLY,
                            idempotency_key=f"{run_id}-user",
                        )
                        client.sessions.append_event(
                            session_id=f"adapter-acceptance-session-{tag}",
                            event_type="assistant_message",
                            payload={"run_id": run_id, "status": "completed",
                                     "text": f"acknowledged {tag}"},
                            sync_policy=SyncPolicy.LOCAL_ONLY,
                            idempotency_key=f"{run_id}-assistant",
                        )

                    seed_session_events("clear-history")
                    response = invoke(adapter_port, "privacy.clear_history",
                                      {"reason_code": "user_request"})
                    body = response.json()
                    result = body.get("result") or {}
                    check("clear-history-http",
                          body.get("ok") is True
                          and result.get("long_term_preferences_retained") is True
                          and client.stats().get("session_events") == 0,
                          f"body={response.body[:300]!r} stats={client.stats().get('session_events')}")

                    retained = client.long_term.get_many([preference.memory_id])
                    check("clear-history-retains-l3",
                          len(retained) == 1 and retained[0].text.endswith("light"),
                          f"retained={len(retained)}")

                    response = invoke(adapter_port, "privacy.clear_all",
                                      {"reason_code": "user_request"})
                    body = response.json()
                    deleted = (body.get("result") or {}).get("deleted", {})
                    check("clear-all-http",
                          body.get("ok") is True and deleted.get("long_term_memories", 0) >= 1
                          and client.stats().get("long_term_memories") == 0,
                          f"deleted={deleted} stats={client.stats().get('long_term_memories')}")

                    # ---- confirmation token semantics against the daemon ----
                    seed_session_events("one-shot")
                    token = mint_token(shared_secret, "privacy.clear_history")
                    first = client.privacy.clear_history(confirm_token=token, reason_code="one-shot-test")
                    one_shot_first_ok = first.get("reason_code") == "one-shot-test"
                    try:
                        client.privacy.clear_history(confirm_token=token, reason_code="one-shot-replay")
                        replay_rejected = False
                        replay_detail = "replay was accepted"
                    except PermissionDenied:
                        replay_rejected = True
                        replay_detail = "replay rejected"
                    check("confirm-token-one-shot",
                          one_shot_first_ok and replay_rejected,
                          f"first_ok={one_shot_first_ok} {replay_detail}")

                    expired = mint_token(shared_secret, "privacy.clear_history",
                                         expiry_ms=int(time.time() * 1000) - 60_000)
                    try:
                        client.privacy.clear_history(confirm_token=expired, reason_code="expired-test")
                        expired_rejected = False
                    except PermissionDenied:
                        expired_rejected = True
                    check("confirm-token-expired-rejected", expired_rejected)

                    cross_method = mint_token(shared_secret, "privacy.clear_all")
                    try:
                        client.privacy.clear_history(confirm_token=cross_method, reason_code="cross-method")
                        cross_rejected = False
                    except PermissionDenied:
                        cross_rejected = True
                    check("confirm-token-method-bound", cross_rejected)

                    try:
                        # The SDK makes confirm_token mandatory; an empty token
                        # exercises the same server-side rejection path.
                        client.privacy.clear_history(confirm_token="", reason_code="missing-token")
                        missing_rejected = False
                    except PermissionDenied:
                        missing_rejected = True
                    check("confirm-token-required", missing_rejected)

                    # ---- strict export boundary ----
                    invoke(adapter_port, "privacy.settings.update",
                           {"sensitive_action_confirmation": "strict"})
                    try:
                        client.privacy.export_data(limit_per_collection=50, include_audit=False)
                        strict_sdk_rejected = False
                    except PermissionDenied:
                        strict_sdk_rejected = True
                    check("strict-export-requires-token-sdk", strict_sdk_rejected)

                    response = invoke(adapter_port, "privacy.export_data",
                                      {"limit_per_collection": 50, "include_audit": False})
                    body = response.json()
                    export = body.get("result") or {}
                    check("strict-export-via-adapter",
                          body.get("ok") is True
                          and export.get("format") == "bianbu-agent-memory.export.v1",
                          f"body={response.body[:300]!r}")

                    invoke(adapter_port, "privacy.settings.update",
                           {"sensitive_action_confirmation": "standard"})
                    response = invoke(adapter_port, "privacy.export_data",
                                      {"limit_per_collection": 50, "include_audit": False})
                    check("standard-export-via-adapter",
                          response.json().get("ok") is True,
                          f"body={response.body[:200]!r}")

                    # ---- memory switch fail-closed ----
                    invoke(adapter_port, "privacy.settings.update", {"memory_enabled": False})
                    try:
                        client.long_term.search_headers("terminal color")
                        disabled_blocked = False
                    except MemoryDisabled:
                        disabled_blocked = True
                    response = invoke(adapter_port, "privacy.settings.update", {"memory_enabled": True})
                    check("memory-switch-fail-closed",
                          disabled_blocked and response.json().get("ok") is True,
                          f"blocked={disabled_blocked}")

                    # ---- memoryd restart: the adapter must recover ----
                    stop_process(daemon)
                    daemon = subprocess.Popen(daemon_command(False), stdout=subprocess.DEVNULL,
                                              stderr=subprocess.PIPE, text=True)
                    wait_for_socket(socket_path, daemon)
                    response = invoke(adapter_port, "privacy.settings.get")
                    check("adapter-recovers-after-daemon-restart",
                          response.status == 200 and response.json().get("ok") is True,
                          f"status={response.status} body={response.body[:300]!r}")

                    # ---- adapter without a secret is fail-closed ----
                    second_port = free_port()
                    second_adapter = subprocess.Popen(
                        [sys.executable, "-m", "bianbu_agent_sdk.memory.bibit_adapter",
                         "--socket", str(socket_path),
                         "--registry-dir", str(registry),
                         "--secret-file", str(root / "missing.secret"),
                         "--host", "127.0.0.1", "--port", str(second_port),
                         "--account-id", ACCOUNT_ID, "--agent-id", AGENT_ID,
                         "--agent-name", AGENT_NAME],
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                    try:
                        wait_for_tcp(second_port, second_adapter)
                        response = invoke(second_port, "privacy.clear_history",
                                          {"reason_code": "user_request"})
                        body = response.json()
                        check("adapter-without-secret-fail-closed",
                              response.status == 200 and body.get("ok") is False
                              and "not configured" in str(body.get("error", {}).get("message", "")),
                              f"status={response.status} body={response.body[:300]!r}")
                    finally:
                        stop_process(second_adapter)
                finally:
                    client.close()
                    registration.unlink(missing_ok=True)
            finally:
                stop_process(adapter)
                time.sleep(0.2)
                adapter_record = registry / f"{adapter.pid}.json"
                check("adapter-registry-cleaned", not adapter_record.exists(),
                      f"record still present: {adapter_record}")
                if adapter.poll() is not None and adapter.stderr:
                    stderr_tail = adapter.stderr.read().strip()[-500:]
                    if stderr_tail:
                        print(f"adapter stderr: {stderr_tail}", file=sys.stderr)
        finally:
            stop_process(daemon)

    passed = sum(1 for t in tests if t["ok"])
    failed = len(tests) - passed
    return {
        "status": "K3_BIBIT_ADAPTER_ACCEPTANCE_OK" if failed == 0 else "K3_BIBIT_ADAPTER_ACCEPTANCE_FAILED",
        "spec": ["MEM-INT-02-01", "MEM-INT-02-02"],
        "passed": passed,
        "failed": failed,
        "tests": tests,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--memoryd", default="target/release/bianbu-agent-memoryd", type=Path)
    parser.add_argument("--port", type=int, default=None, help="fixed adapter port (default: ephemeral)")
    parser.add_argument("--output", type=Path, default=None, help="write the JSON report here")
    args = parser.parse_args()

    report = run(args)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload)
    print(f"\n== BiBit adapter acceptance: {report['passed']} passed, {report['failed']} failed ==",
          file=sys.stderr)
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
