#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
from threading import Thread
import time
from typing import Any, Iterator
import urllib.request
import uuid

from bianbu_agent_sdk.memory import (
    AsyncMemoryClient,
    MemoryKind,
    OpenAICompatibleMem0Planner,
    SyncPolicy,
    field_dictionary,
    normalize_field_value,
)
from bianbu_agent_sdk.memory.errors import Unauthenticated
from bianbu_agent_sdk.memory.mem0 import MutationEvent
from bianbu_agent_sdk.memory.runtime import MemoryBridge


PASS = "PASS"
SKIP_EXTERNAL_RESOURCE = "SKIP_EXTERNAL_RESOURCE"


class _MockOpenAIHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    expected_token = "contract-demo-token"

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        if self.headers.get("Authorization") != f"Bearer {self.expected_token}":
            self.send_error(401)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.requests.append(payload)
        prompt = str(payload["messages"][0]["content"])
        if prompt.startswith("Extract only durable"):
            content = json.dumps(
                {
                    "facts": [
                        {
                            "text": "Python package manager preference: uv",
                            "kind": "decision",
                            "confidence": 0.9,
                        }
                    ]
                }
            )
        else:
            content = json.dumps(
                {
                    "memory": [
                        {
                            "text": "Python package manager preference: uv",
                            "kind": "decision",
                            "event": "ADD",
                            "confidence": 0.9,
                        }
                    ]
                }
            )
        encoded = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": content}}]}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        return None


@contextmanager
def mock_openai_server() -> Iterator[str]:
    _MockOpenAIHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockOpenAIHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


async def exercise_model_api(
    *, base_url: str | None = None, model: str = "contract-demo-model", api_key: str = ""
) -> dict[str, object]:
    using_mock = base_url is None
    manager = mock_openai_server() if using_mock else _passthrough(base_url)
    with manager as selected_url:
        selected_key = _MockOpenAIHandler.expected_token if using_mock else api_key
        planner = OpenAICompatibleMem0Planner(
            base_url=selected_url,
            model=model,
            api_key=selected_key,
            timeout=30,
        )
        candidates = await planner.extract(
            ({"role": "user", "content": "Please remember that I use uv for Python."},)
        )
        if not candidates:
            raise AssertionError("model API returned no durable candidate")
        mutations = await planner.merge(candidates, ())
        if not mutations or mutations[0].event is not MutationEvent.ADD:
            raise AssertionError("model API did not return an ADD mutation")
        result: dict[str, object] = {
            "status": PASS,
            "evidence_level": "mock_contract" if using_mock else "live_external",
            "base_url": selected_url,
            "model": model,
            "candidate_count": len(candidates),
            "mutation": mutations[0].event.value,
        }
        if using_mock:
            result["requests"] = len(_MockOpenAIHandler.requests)
            result["authorization_header_verified"] = True
            result["response_format_verified"] = all(
                request.get("response_format") == {"type": "json_object"}
                and request.get("temperature") == 0
                for request in _MockOpenAIHandler.requests
            )
        return result


@contextmanager
def _passthrough(value: str | None) -> Iterator[str]:
    if not value:
        raise ValueError("model base URL is required")
    yield value.rstrip("/")


async def wait_for_socket(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise RuntimeError(f"memoryd exited during setup: {stderr[-2000:]}")
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(str(path))
        except OSError:
            await asyncio.sleep(0.05)
        else:
            return
        finally:
            client.close()
    raise TimeoutError(f"memoryd socket was not ready: {path}")


async def exercise_external_callers(socket_path: Path) -> dict[str, object]:
    client = await AsyncMemoryClient.connect(socket_path)
    bridge = MemoryBridge(str(socket_path))
    bridge._client = client
    try:
        if client.identity.account_id != "default":
            raise AssertionError("development identity must use default account")
        health = await client.health()
        settings = await client.privacy.settings()
        if settings["identity"]["cloud_upload_allowed"] is not False:
            raise AssertionError("default account unexpectedly allows cloud upload")

        updated = await client.privacy.update_settings(
            timezone="UTC",
            local_device_id="contract-demo-k3",
            recent_turn_limit=5,
            summary_interval_turns=5,
            preference_auto_apply_enabled=True,
            development_habit_auto_apply_enabled=True,
            idempotency_key="external-contract-settings",
        )
        bridge_settings = await bridge.settings()
        if bridge_settings.get("identity", {}).get("account_id") != "default":
            raise AssertionError("Runtime bridge received a mismatched account")

        fields = field_dictionary()
        if normalize_field_value("development.package_manager", "PNPM") != "pnpm":
            raise AssertionError("L3 field normalization is inconsistent")
        memory = await client.long_term.put(
            kind=MemoryKind.DECISION,
            text="Python package manager preference: uv",
            tags=("slot:development.package_manager",),
            sync_policy=SyncPolicy.LOCAL_ONLY,
            idempotency_key="external-contract-l3",
        )
        recalled = await bridge.recall("Python package manager", top_k=5)
        if not any("uv" in item for item in recalled):
            raise AssertionError("Runtime bridge did not recall the L3 memory")
        recorded = await bridge.record_turn(
            run_id="external-contract-run",
            session_id="external-contract-session",
            agent_id="external-contract-agent",
            user_text="Compare SQLite and PostgreSQL for this local service.",
            assistant_text="SQLite is the first option for this local deployment.",
        )
        if not recorded:
            raise AssertionError("Runtime bridge did not persist the turn")
        recent = await bridge.recent_context("external-contract-next-session")
        if not any("SQLite" in item for item in recent):
            raise AssertionError("Runtime bridge did not surface recent session status")

        for operation in (
            client.sync.request(
                scopes=("preferences",), idempotency_key="external-contract-sync"
            ),
            client.backup.create(
                scopes=("preferences",), idempotency_key="external-contract-backup"
            ),
        ):
            try:
                await operation
            except Unauthenticated:
                continue
            raise AssertionError("default account external operation did not fail closed")

        return {
            "status": PASS,
            "evidence_level": "isolated_real_sdk_and_daemon",
            "health": {
                "schema_version": health["schema_version"],
                "storage_encryption": health["storage_encryption"],
            },
            "identity": {
                "account_id": client.identity.account_id,
                "device_id": updated["identity"]["configured_device_id"],
                "cloud_upload_allowed": updated["identity"]["cloud_upload_allowed"],
            },
            "settings_round_trip": True,
            "l3_dictionary_fields": len(fields),
            "l3_memory_id": memory.memory_id,
            "runtime_recall": True,
            "runtime_turn_persistence": True,
            "default_sync_and_backup_rejected": True,
        }
    finally:
        await bridge.close()


def check_runtime_http(base_url: str) -> dict[str, object]:
    request = urllib.request.Request(f"{base_url.rstrip('/')}/chat", method="GET")
    with urllib.request.urlopen(request, timeout=15) as response:
        status = response.status
    if status != 200:
        raise AssertionError(f"Runtime /chat returned HTTP {status}")
    return {"status": PASS, "evidence_level": "live_external", "chat_http": status}


def external_resource_gaps(*, live_model: bool, runtime_checked: bool) -> list[dict[str, object]]:
    gaps = [
        {
            "specs": ["MEM-INT-02"],
            "status": SKIP_EXTERNAL_RESOURCE,
            "resource": "BiBit UI and real project/code-generation entry",
        },
        {
            "specs": ["MEM-INT-03", "MEM-L4-05"],
            "status": SKIP_EXTERNAL_RESOURCE,
            "resource": "trusted account identity, login/logout and token lifecycle",
        },
        {
            "specs": ["MEM-INT-04"],
            "status": SKIP_EXTERNAL_RESOURCE,
            "resource": "Secret Broker, production signature, Confirm Token and SEC-06",
        },
        {
            "specs": ["MEM-L4-06", "MEM-NFR-03"],
            "status": SKIP_EXTERNAL_RESOURCE,
            "resource": "cloud Backup Adapter, test token and second device",
        },
    ]
    if not live_model:
        gaps.append(
            {
                "specs": ["MEM-INT-05", "MEM-INT-08"],
                "status": SKIP_EXTERNAL_RESOURCE,
                "resource": "frozen production model endpoint and model budget",
            }
        )
    if not runtime_checked:
        gaps.append(
            {
                "specs": ["MEM-INT-02", "MEM-INT-08"],
                "status": SKIP_EXTERNAL_RESOURCE,
                "resource": "live Runtime HTTP endpoint",
            }
        )
    return gaps


async def run(arguments: argparse.Namespace) -> dict[str, object]:
    memoryd = arguments.memoryd.resolve()
    if not memoryd.is_file():
        raise FileNotFoundError(memoryd)
    with tempfile.TemporaryDirectory(prefix="agent-memory-external-contract-") as raw:
        root = Path(raw)
        socket_path = root / "memoryd.sock"
        secret_path = root / "account-secret"
        secret_path.write_text(uuid.uuid4().hex * 2 + "\n", encoding="utf-8")
        secret_path.chmod(0o600)
        environment = dict(os.environ)
        environment["BIANBU_MEMORY_DEV_CONFIRM_TOKEN"] = uuid.uuid4().hex
        process = subprocess.Popen(
            [
                str(memoryd),
                "--development-identity",
                "--socket",
                str(socket_path),
                "--database",
                str(root / "memory.db"),
                "--key-package",
                str(root / "memory.keypkg"),
                "--account-secret-file",
                str(secret_path),
                "--initialize-key-package",
            ],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            await wait_for_socket(socket_path, process)
            callers = await exercise_external_callers(socket_path)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()

    api_key = (
        os.environ.get(arguments.model_api_key_env, "")
        if arguments.model_api_key_env
        else ""
    )
    model = await exercise_model_api(
        base_url=arguments.model_base_url,
        model=arguments.model_name,
        api_key=api_key,
    )
    runtime = check_runtime_http(arguments.runtime_url) if arguments.runtime_url else None
    report = {
        "status": "EXTERNAL_API_CONTRACT_DEMO_OK",
        "contract_checks": {
            "bibit_and_runtime_callers": callers,
            "openai_compatible_model": model,
            "runtime_http": runtime
            or {
                "status": SKIP_EXTERNAL_RESOURCE,
                "resource": "live Runtime HTTP endpoint",
            },
        },
        "external_resources_pending": external_resource_gaps(
            live_model=arguments.model_base_url is not None,
            runtime_checked=runtime is not None,
        ),
        "closure_rule": (
            "PASS with mock_contract closes only the AgentMemory adapter contract; "
            "an external specification closes only with live_external evidence."
        ),
    }
    if arguments.require_live_external and report["external_resources_pending"]:
        raise RuntimeError("live external resources are still missing")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exercise AgentMemory APIs from external caller boundaries."
    )
    parser.add_argument(
        "--memoryd", type=Path, default=Path("target/release/bianbu-agent-memoryd")
    )
    parser.add_argument("--runtime-url")
    parser.add_argument("--model-base-url")
    parser.add_argument("--model-name", default="contract-demo-model")
    parser.add_argument("--model-api-key-env")
    parser.add_argument("--require-live-external", action="store_true")
    return parser.parse_args()


def main() -> None:
    print(json.dumps(asyncio.run(run(parse_args())), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
