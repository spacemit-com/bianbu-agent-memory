#!/usr/bin/env python3
"""K3 acceptance for the local database quota write guard."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import uuid

from bianbu_agent_sdk.memory import MemoryClient, MemoryKind, SyncPolicy


def wait_for_socket(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(process.stderr.read() if process.stderr else "memoryd exited")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(path))
            return
        except OSError:
            time.sleep(0.05)
        finally:
            probe.close()
    raise TimeoutError(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--memoryd", default="target/release/bianbu-agent-memoryd", type=Path)
    parser.add_argument("--limit-bytes", default=16 * 1024 * 1024, type=int)
    args = parser.parse_args()
    memoryd = args.memoryd.resolve()
    with tempfile.TemporaryDirectory(prefix="agent-memory-quota-") as raw:
        root = Path(raw)
        socket_path, database = root / "memoryd.sock", root / "memory.db"
        key_package, secret = root / "memory.keypkg", root / "account-secret"
        secret.write_text(uuid.uuid4().hex + uuid.uuid4().hex + "\n", encoding="utf-8")
        secret.chmod(0o600)
        command = [
            str(memoryd), "--development-identity", "--socket", str(socket_path),
            "--database", str(database), "--key-package", str(key_package),
            "--account-secret-file", str(secret), "--initialize-key-package",
        ]
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            wait_for_socket(socket_path, process)
            with MemoryClient.connect(socket_path, timeout=30) as client:
                client._call("privacy.settings.update", {
                    "max_database_bytes": args.limit_bytes,
                    "min_free_bytes": 0,
                })
                rejected = False
                writes = 0
                # The write-side content gate (MEM-L3-02/03, shipped after this
                # script was written) caps one record at 2200 characters for the
                # "memory" target, so a 48 KB payload is now rejected with
                # InvalidArgument before the store ever sees the size limit this
                # script exists to prove.  Fill the database with records that
                # pass the gate instead; the quota is reached after a few
                # thousand of them.
                payload = "quota marker " + ("x" * 2_000)
                for index in range(20_000):
                    try:
                        client.long_term.put(
                            kind=MemoryKind.FACT, text=f"{payload} {index}",
                            sync_policy=SyncPolicy.LOCAL_ONLY,
                            idempotency_key=f"quota:{index}",
                        )
                        writes += 1
                    except Exception as error:  # SDK maps wire ResourceExhausted to a typed error.
                        rejected = "ResourceExhausted" in str(error) or "quota" in str(error).lower()
                        if not rejected:
                            raise
                        break
                if not rejected:
                    raise AssertionError("quota did not reject a write")
                health = client.health()
                assert health["storage"]["within_limits"] is False
                maintenance = client._call("maintenance.run", {"remove_expired": True})
            print(json.dumps({
                "status": "K3_QUOTA_ACCEPTANCE_OK",
                "limit_bytes": args.limit_bytes,
                "writes_before_rejection": writes,
                "rejection": "ResourceExhausted",
                "maintenance": maintenance,
                "within_limits_after_rejection": health["storage"]["within_limits"],
            }, ensure_ascii=False, indent=2))
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


if __name__ == "__main__":
    main()
