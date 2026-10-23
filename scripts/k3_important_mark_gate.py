#!/usr/bin/env python3
"""K3 现场手动验收：重要标记到底有没有存进记忆？

每一步都打印真实读回的 `tags` 与 `revision`，而不是只报成功与否，
这样"存进去了"是可以被直接看见的。全程跑在隔离的 development-identity
栈上，不碰生产库。

用法（K3）：
    cd ~/mem-gateway-build
    PYTHONPATH=sdk/python/src python3 scripts/k3_important_mark_gate.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import uuid

from bianbu_agent_sdk.memory import AsyncMemoryClient, MemoryKind, SyncPolicy

MARK = "important"
FAILURES: list[str] = []


def step(name: str, ok: bool, detail: object = None) -> None:
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail is not None:
        line += " -> " + json.dumps(detail, ensure_ascii=False)
    print(line, flush=True)
    if not ok:
        FAILURES.append(name)


def start(memoryd: Path, root: Path, initialize: bool):
    secret = root / "account-secret"
    if not secret.exists():
        secret.write_text(uuid.uuid4().hex + uuid.uuid4().hex + "\n", encoding="utf-8")
        secret.chmod(0o600)
    sock = root / "memoryd.sock"
    if sock.exists():
        sock.unlink()
    environment = dict(os.environ)
    environment["AGENT_MEMORY_EMBEDDING_DISABLED"] = "1"
    argv = [
        str(memoryd),
        "--development-identity",
        "--socket", str(sock),
        "--database", str(root / "memory.db"),
        "--key-package", str(root / "memory.keypkg"),
        "--account-secret-file", str(secret),
    ]
    if initialize:
        argv.append("--initialize-key-package")
    process = subprocess.Popen(
        argv, env=environment, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, text=True,
    )
    return process, sock


async def wait_socket(sock: Path, process: subprocess.Popen, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError((process.stderr.read() or "").strip()[:500])
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(sock))
            return
        except OSError:
            pass
        finally:
            probe.close()
        await asyncio.sleep(0.05)
    raise TimeoutError(str(sock))


async def tags_of(client: AsyncMemoryClient, memory_id: str) -> list[str]:
    records = await client.long_term.get_many([memory_id])
    if not records:
        return []
    return list(records[0].tags)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--memoryd", default="/usr/libexec/bianbu-agent-memoryd", type=Path)
    arguments = parser.parse_args()
    memoryd = arguments.memoryd.resolve()
    if not memoryd.is_file():
        raise FileNotFoundError(memoryd)

    with tempfile.TemporaryDirectory(prefix="important-mark-manual-") as raw:
        root = Path(raw)
        process, sock = start(memoryd, root, initialize=True)
        try:
            await wait_socket(sock, process)
            client = await AsyncMemoryClient.connect(str(sock))
            try:
                receipt = await client.long_term.put(
                    kind=MemoryKind.FACT,
                    text="重要标记手测：机房断电演练固定在每周三凌晨",
                    tags=["probe:manual"],
                    sync_policy=SyncPolicy.ACCOUNT_AGENT,
                )
                memory_id = receipt.memory_id
                before = await tags_of(client, memory_id)
                step("1. 新写入的记录默认不带 important", MARK not in before,
                     {"memory_id": memory_id, "revision": receipt.revision, "tags": before})

                record = (await client.long_term.get_many([memory_id]))[0]
                marked = await client.long_term.mark_important(record)
                after = await tags_of(client, memory_id)
                step("2. 标记后读回，tags 出现 important", MARK in after,
                     {"revision": marked.revision, "tags": after})

                step("3. 其它 tag 与原文都没被动过",
                     "probe:manual" in after and len(after) == len(before) + 1,
                     {"tags": after})

                same = await client.long_term.mark_important((await client.long_term.get_many([memory_id]))[0])
                step("4. 重复标记是 no-op，不空耗 revision",
                     bool(same.raw.get("unchanged")) and same.revision == marked.revision,
                     {"revision": same.revision, "unchanged": same.raw.get("unchanged")})

                settings = await client.privacy.settings()
                step("5. 云备份退役后设置页不再暴露备份字段",
                     "backup_enabled" not in settings and "backup_scopes" not in settings,
                     {"keys": sorted(key for key in settings if key.startswith("backup"))})
            finally:
                await client.close()

            process.terminate()
            process.wait(timeout=20)
            process, sock = start(memoryd, root, initialize=False)
            await wait_socket(sock, process)
            client = await AsyncMemoryClient.connect(str(sock))
            try:
                survived = await tags_of(client, memory_id)
                step("6. 守护进程重启后标记仍在（真的落库了）", MARK in survived,
                     {"tags": survived})
                record = (await client.long_term.get_many([memory_id]))[0]
                cleared = await client.long_term.unmark_important(record)
                final = await tags_of(client, memory_id)
                step("7. 取消标记后 important 消失，其它 tag 保留",
                     MARK not in final and "probe:manual" in final,
                     {"revision": cleared.revision, "tags": final})
            finally:
                await client.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()

    print("VERDICT:", "K3_IMPORTANT_MARK_GATE_OK" if not FAILURES else f"FAIL ({', '.join(FAILURES)})")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
