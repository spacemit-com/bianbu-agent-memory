#!/usr/bin/env python3
"""How long must `system.health` be quiet before `PRAGMA quick_check` can be trusted?

On K3 the shipped SQLCipher build reports

    malformed inverted index for FTS5 table main.long_term_fts[2]

for an FTS5 index that is fine, as long as the database was written to
recently; the same check returns ``ok`` once the database has been left alone
(docs/GATEWAY_EMBEDDING_INTEGRATION.md §8.2).  Excluding the writer from the
check is not enough -- the sample has to come from a *quiet* database -- so the
default ``AGENT_MEMORY_HEALTH_SETTLE_MS`` is chosen from this curve.

This script measures the curve: it bursts writes, then samples health once at a
known delay after the last write, cycling through delays.  Run it against a
daemon started with the gate and the verdict cache disabled, and with the
background embedding worker off so that the only recent write is the burst:

    sudo mkdir -p /etc/systemd/system/bianbu-agent-memoryd.service.d
    sudo tee /etc/systemd/system/bianbu-agent-memoryd.service.d/zz-health-calibration.conf <<'EOF'
    [Service]
    Environment=AGENT_MEMORY_HEALTH_SETTLE_MS=0
    Environment=AGENT_MEMORY_HEALTH_CHECK_INTERVAL_MS=0
    Environment=AGENT_MEMORY_EMBEDDING_DISABLED=1
    EOF
    sudo systemctl daemon-reload && sudo systemctl restart bianbu-agent-memoryd
    echo bianbu | sudo -S python3 scripts/k3_health_settle_calibration.py

Remove the drop-in afterwards.  Every probe row lives in its own agent
namespace and is forgotten through the daemon's own confirmation token.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path

from bianbu_agent_sdk.memory import MemoryClient, MemoryKind, SyncPolicy

REGISTRY = Path("/run/bianbu/agent-runtime/memory-registry")
SOCKET = os.environ.get("MEMORY_SOCKET", "/run/bianbu/agent-memoryd.sock")
SECRET_PATH = Path("/etc/bianbu-agent-memory/adapter-confirmation.secret")
DEVICE_ID = os.environ.get("MEMORY_DEVICE_ID", "fe:fe:fe:4e:f8:26")
ACCOUNT_ID = os.environ.get("MEMORY_ACCOUNT_ID", "bianbu")
AGENT = "com.bianbu.gateway.healthcal"
CAPABILITIES = [
    "memory.long_term.read",
    "memory.long_term.write",
    "memory.forget",
]
BURST = int(os.environ.get("BURST", "12"))
# Writes from several connections at once is the condition that reproduces the
# phantom mismatch; a single writer does not reproduce it at any delay.
WRITER_THREADS = int(os.environ.get("WRITER_THREADS", "1"))
ROUNDS = int(os.environ.get("ROUNDS", "3"))
DELAYS = [float(value) for value in os.environ.get("DELAYS", "0.1,0.6,1.5,3.0").split(",")]
COOLDOWN = float(os.environ.get("COOLDOWN", "6"))


def confirmation_token(secret: bytes, method: str, ttl_ms: int = 120_000) -> str:
    expiry = int(time.time() * 1000) + ttl_ms
    nonce = secrets.token_urlsafe(24)
    payload = f"v1:{expiry}:{nonce}:{method}".encode("utf-8")
    return f"v1:{expiry}:{nonce}:{hashlib.sha256(secret + payload).hexdigest()}"


def register() -> Path:
    pid = os.getpid()
    fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1]
    start = int(fields.split()[19])
    path = REGISTRY / f"{pid}.json"
    path.write_text(json.dumps({
        "uid": os.getuid(), "pid_start_time_ticks": start, "account_id": ACCOUNT_ID,
        "device_id": DEVICE_ID, "agent_id": AGENT, "agent_name": AGENT,
        "agent_instance_id": f"healthcal.{pid}.{start}",
        "memory_space_id": f"{ACCOUNT_ID}.{AGENT}.{DEVICE_ID}",
        "capabilities": CAPABILITIES,
        "expires_at_ms": int(time.time() * 1000) + 900_000,
        "allow_runtime_context": False}, ensure_ascii=False), encoding="utf-8")
    os.chmod(path, 0o640)
    return path


def main() -> int:
    secret = SECRET_PATH.read_bytes() if os.access(SECRET_PATH, os.R_OK) else None
    registration = register()
    written: list[str] = []
    samples: list[dict[str, object]] = []
    try:
        with MemoryClient.connect(
            SOCKET, timeout=60.0, account_id=ACCOUNT_ID, agent_name=AGENT
        ) as client:
            stamp = secrets.token_hex(4)
            for cycle in range(ROUNDS * len(DELAYS)):
                delay = DELAYS[cycle % len(DELAYS)]
                finished = [0.0] * WRITER_THREADS

                def burst_thread(number: int) -> None:
                    with MemoryClient.connect(
                        SOCKET, timeout=60.0, account_id=ACCOUNT_ID, agent_name=AGENT
                    ) as writer:
                        for index in range(BURST):
                            receipt = writer.long_term.put(
                                kind=MemoryKind.PREFERENCE,
                                text=f"标定 {cycle}-{number}-{index}：把屏幕亮度调低一点 zzcal{stamp}",
                                tags=("gateway-health",), sync_policy=SyncPolicy.LOCAL_ONLY,
                                expires_at=int(time.time() * 1000) + 900_000,
                                idempotency_key=f"healthcal:{os.getpid()}:{cycle}:{number}:{index}")
                            written.append(receipt.memory_id)
                    finished[number] = time.monotonic()

                threads = [
                    threading.Thread(target=burst_thread, args=(number,))
                    for number in range(WRITER_THREADS)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                last_write = max(finished)
                wait = delay - (time.monotonic() - last_write)
                if wait > 0:
                    time.sleep(wait)
                started = time.monotonic()
                health = client.health()
                check = health.get("database_check") or {}
                samples.append({
                    "cycle": cycle,
                    "writer_threads": WRITER_THREADS,
                    "target_delay_s": delay,
                    "actual_delay_s": round(started - last_write, 3),
                    "status": health["status"],
                    "database": health["database"],
                    "state": check.get("state"),
                    "reason": check.get("reason"),
                    "check_ms": round((time.monotonic() - started) * 1000, 1),
                })
                time.sleep(COOLDOWN)
            if secret is not None:
                for memory_id in written:
                    job = client.long_term.forget(
                        memory_id,
                        confirm_token=confirmation_token(secret, "long_term.forget"))
                    client.long_term.wait_forget(job.job_id)
                print("=== cleanup ===", client.stats().get("long_term_memories"),
                      client.stats().get("memory_embeddings"))
    finally:
        registration.unlink(missing_ok=True)

    buckets: dict[str, dict[str, int]] = {}
    for sample in samples:
        bucket = f"{sample['actual_delay_s']:.1f}s"
        entry = buckets.setdefault(bucket, {"samples": 0, "ok": 0, "degraded": 0, "busy": 0})
        entry["samples"] += 1
        if sample["status"] == "ok":
            entry["ok"] += 1
        elif sample["status"] == "degraded":
            entry["degraded"] += 1
        else:
            entry["busy"] += 1
    print("=== per-delay ===")
    print(json.dumps(buckets, ensure_ascii=False, indent=2, sort_keys=True))
    print("=== samples ===")
    print(json.dumps(samples, ensure_ascii=False, indent=2))
    return 0 if all(entry["degraded"] == 0 for entry in buckets.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
