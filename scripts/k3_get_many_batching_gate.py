#!/usr/bin/env python3
"""K3 gate: one recall never puts more than 100 ids on a single wire request.

``memoryd`` rejects a ``long_term.get_many`` carrying more than 100 ids
(``at most 100 memory_ids are allowed``). One recall unions the query headers,
the reminder headers and the explicit records, so the union can pass 100 on a
large store, and the rejection used to surface as a silent degradation: the
SDK's ``InvalidArgument`` is a ``MemoryError``, which the recall path swallows.

This gate drives the **installed** daemon on its own socket and database
(``--development-identity``), so the memoryd already running on the device is
left alone. It proves three things:

1. the ceiling is real: one request with 120 ids is refused by the daemon;
2. the installed SDK no longer hits it: ``get_many`` with the same 120 ids
   returns every record, in order;
3. the small and empty cases still take the cheap single request.

Usage (on K3, from any directory; no PYTHONPATH, so the installed SDK is used):

    python3 scripts/k3_get_many_batching_gate.py
    python3 scripts/k3_get_many_batching_gate.py --json /tmp/get-many-batching.json

Exit code: 0 = every check matched the fixed behaviour, 1 = at least one did not.
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

DEFAULT_MEMORYD = "/usr/libexec/bianbu-agent-memoryd"
DEFAULT_PYTHON = "/usr/lib/bianbu-agent-runtime/venv/bin/python"
ID_LIMIT = 100
RECORD_COUNT = 120
START_TIMEOUT = 30.0

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
        self.secret.write_text(
            uuid.uuid4().hex + uuid.uuid4().hex + "\n", encoding="utf-8"
        )
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


def evaluate(memoryd: Path, root: Path) -> None:
    from bianbu_agent_sdk.memory.client import MemoryClient
    from bianbu_agent_sdk.memory.types import MemoryKind

    daemon = IsolatedDaemon(memoryd, root / "daemon")
    client = None
    try:
        daemon.start()
        record("daemon", "隔离 memoryd 已就绪", str(daemon.socket), "socket 存在", True)
        client = MemoryClient.connect(daemon.socket, timeout=30.0)
        record(
            "connect",
            "已安装 SDK 连上隔离 memoryd",
            client.identity.memory_space_id,
            "非空 memory_space_id",
            bool(client.identity.memory_space_id),
        )

        ids: list[str] = []
        for index in range(RECORD_COUNT):
            receipt = client.long_term.put(
                kind=MemoryKind.FACT,
                text=f"分批闸门：第 {index:03d} 条记录 {uuid.uuid4().hex[:8]}",
            )
            ids.append(receipt.memory_id)
        record(
            "seed",
            f"隔离库写入 {RECORD_COUNT} 条记录",
            len(ids),
            RECORD_COUNT,
            len(ids) == RECORD_COUNT and len(set(ids)) == RECORD_COUNT,
        )

        refusal = None
        try:
            client._call("long_term.get_many", {"memory_ids": ids})
        except Exception as error:  # noqa: BLE001 - the refusal is the observation
            refusal = f"{type(error).__name__}: {error}"
        record(
            "ceiling-real",
            f"单请求携带 {RECORD_COUNT} 个 id 被 daemon 拒绝（这正是分批要绕开的）",
            refusal,
            f"包含 'at most {ID_LIMIT}' 的拒绝",
            bool(refusal) and "at most" in refusal and str(ID_LIMIT) in refusal,
        )

        records = client.long_term.get_many(ids)
        returned = [item.memory_id for item in records]
        record(
            "batched-read-complete",
            f"SDK 的 get_many 取回全部 {RECORD_COUNT} 条",
            len(returned),
            RECORD_COUNT,
            len(returned) == RECORD_COUNT,
        )
        record(
            "batched-read-order",
            "返回顺序与请求顺序一致，且无重复无遗漏",
            "identical" if returned == ids else "differs",
            "identical",
            returned == ids,
        )
        record(
            "batched-read-content",
            "每条记录都带回了正文",
            sum(1 for item in records if item.text.startswith("分批闸门：")),
            RECORD_COUNT,
            all(item.text.startswith("分批闸门：") for item in records),
        )

        small = client.long_term.get_many(ids[:3])
        record(
            "small-read",
            "小批量仍走单请求并完整返回",
            [item.memory_id for item in small] == ids[:3],
            True,
            [item.memory_id for item in small] == ids[:3],
        )
        empty = client.long_term.get_many([])
        record("empty-read", "空请求不发网络往返，返回空列表", empty, [], empty == [])

        duplicated = client.long_term.get_many(ids + ids)
        record(
            "duplicate-read",
            "重复 id 先去重再分批，不会重复返回记录",
            len(duplicated),
            RECORD_COUNT,
            len(duplicated) == RECORD_COUNT,
        )
    finally:
        if client is not None:
            client.close()
        daemon.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="K3 get_many 分批闸门")
    parser.add_argument("--memoryd", default=DEFAULT_MEMORYD, help="已安装的 memoryd 二进制")
    parser.add_argument("--json", help="证据 JSON 输出路径")
    arguments = parser.parse_args()

    memoryd = Path(arguments.memoryd)
    if not memoryd.is_file():
        print(f"missing required file: {memoryd}", file=sys.stderr)
        return 2

    failure: str | None = None
    with tempfile.TemporaryDirectory(prefix="get-many-batching-gate-") as raw:
        try:
            evaluate(memoryd, Path(raw))
        except Exception as error:  # noqa: BLE001 - reported as a failed gate
            failure = f"{type(error).__name__}: {error}"

    evidence = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "memoryd": str(memoryd),
        "id_limit": ID_LIMIT,
        "record_count": RECORD_COUNT,
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
