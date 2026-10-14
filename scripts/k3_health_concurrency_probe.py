#!/usr/bin/env python3
"""Does `health` still call a database "degraded" just because it is being written?

Before 0.1.18 health ran ``PRAGMA quick_check`` on a pooled connection with no
writer exclusion, so on K3 the verdict flipped to "malformed inverted index for
FTS5 table main.long_term_fts[2]" whenever anything was writing and returned to
``ok`` once the daemon was quiesced.  0.1.18 serializes that check against
writers, reports an inconclusive sample as ``database_check.state == "busy"``
(with ``status`` left at the previous conclusive verdict, or ``unknown``), and
reserves ``degraded`` for a conclusive defect.

This probe is the regression gate for that behaviour.  It samples health while
writers run and requires that no sample is ``degraded`` and that no two adjacent
samples disagree, with three further signals, all inside a dedicated probe
namespace (the user's Runtime space is never touched):

1. paired health samples taken back to back on one connection while writers run:
   a verdict that flips between two adjacent calls cannot be a property of the
   stored index;
2. a functional invariant during the same window -- two marker memories must
   stay retrievable, and a third marker forgotten before the load must never
   come back (an orphaned index entry is exactly what would revive it);
3. the verdict sampled repeatedly (``AFTER_LOAD_SAMPLES`` every
   ``AFTER_LOAD_INTERVAL``) after the writers stop, because the complaint is at
   its worst just then - on K3 it kept reproducing for ~100 s after a burst and
   then cleared by itself. A ``degraded`` there fails the gate even if every
   sample during the load was clean.

Reading the confirmation secret needs root (or the ``bianbu-agent`` group);
without it the probe still runs but leaves the marker rows to their expiry.

Usage (K3, repo root):

    echo bianbu | sudo -S python3 scripts/k3_health_concurrency_probe.py
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
AGENT = "com.bianbu.gateway.healthprobe"
CAPABILITIES = [
    "memory.long_term.read",
    "memory.long_term.write",
    "memory.forget",
    "memory.privacy.read",
]
LOAD_SECONDS = float(os.environ.get("LOAD_SECONDS", "12"))
WRITERS = int(os.environ.get("WRITERS", "2"))
# How long to keep sampling health once the load threads have stopped.  A
# complaint that a single sample produced and the next ones do not is the WAL
# artifact this probe exists for; a complaint that every sample reproduces is a
# defect.  The last sample is the "resting verdict".
AFTER_LOAD_SAMPLES = int(os.environ.get("AFTER_LOAD_SAMPLES", "10"))
AFTER_LOAD_INTERVAL = float(os.environ.get("AFTER_LOAD_INTERVAL", "2.0"))
# writers | readers | both -- readers alone is the discriminating case: a
# concurrent writer is excluded from the check by design, a concurrent reader
# is not, and with no writer at all the WAL stays empty.
PROBE_MODE = os.environ.get("PROBE_MODE", "both")
READERS = int(os.environ.get("READERS", "4"))

LOCK = threading.Lock()
SAMPLES: list[dict[str, object]] = []
WRITTEN: list[str] = []
STOP = threading.Event()


def confirmation_token(secret: bytes, method: str, ttl_ms: int = 120_000) -> str:
    expiry = int(time.time() * 1000) + ttl_ms
    nonce = secrets.token_urlsafe(24)
    payload = f"v1:{expiry}:{nonce}:{method}".encode("utf-8")
    return f"v1:{expiry}:{nonce}:{hashlib.sha256(secret + payload).hexdigest()}"


def register() -> Path:
    pid = os.getpid()
    # Field 22 of /proc/<pid>/stat; the daemon binds the registration to it.
    fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1]
    start = int(fields.split()[19])
    path = REGISTRY / f"{pid}.json"
    path.write_text(json.dumps({
        "uid": os.getuid(), "pid_start_time_ticks": start, "account_id": ACCOUNT_ID,
        "device_id": DEVICE_ID, "agent_id": AGENT, "agent_name": AGENT,
        "agent_instance_id": f"healthprobe.{pid}.{start}",
        "memory_space_id": f"{ACCOUNT_ID}.{AGENT}.{DEVICE_ID}",
        "capabilities": CAPABILITIES,
        "expires_at_ms": int(time.time() * 1000) + 900_000,
        "allow_runtime_context": False}, ensure_ascii=False), encoding="utf-8")
    os.chmod(path, 0o640)
    return path


def connect() -> MemoryClient:
    return MemoryClient.connect(SOCKET, timeout=60.0, account_id=ACCOUNT_ID, agent_name=AGENT)


def verdict(client: MemoryClient) -> dict[str, str]:
    health = client.health()
    check = health.get("database_check") or {}
    return {
        "status": health["status"],
        "database": health["database"],
        "check_state": check.get("state"),
        "verdict": check.get("verdict"),
        "reason": check.get("reason"),
        "complaint": check.get("complaint"),
        "attempts": check.get("attempts"),
    }


def writer_loop(number: int) -> None:
    try:
        with connect() as client:
            index = 0
            while not STOP.is_set():
                receipt = client.long_term.put(
                    kind=MemoryKind.PREFERENCE,
                    text=f"并发写入 {number}-{index}：让屏幕晚上不那么刺眼",
                    tags=("gateway-health",), sync_policy=SyncPolicy.LOCAL_ONLY,
                    expires_at=int(time.time() * 1000) + 900_000,
                    idempotency_key=f"gateway-health:{os.getpid()}:{number}:{index}")
                with LOCK:
                    WRITTEN.append(receipt.memory_id)
                index += 1
                time.sleep(0.05)
    except Exception as error:  # noqa: BLE001
        with LOCK:
            SAMPLES.append({"status": "writer-error", "database": f"{type(error).__name__}: {error}"})


def reader_loop(number: int, needle: str) -> None:
    try:
        with connect() as client:
            while not STOP.is_set():
                client.long_term.search_headers(needle, top_k=20)
                client.long_term.search_headers("让屏幕晚上不那么刺眼", top_k=20)
    except Exception as error:  # noqa: BLE001
        with LOCK:
            SAMPLES.append({"status": "reader-error", "database": f"{type(error).__name__}: {error}"})


def main() -> int:
    secret = SECRET_PATH.read_bytes() if os.access(SECRET_PATH, os.R_OK) else None
    registration = register()
    kept_markers: list[str] = []
    forgotten_marker: str | None = None
    quiesced: dict[str, dict[str, str]] = {}
    after_load: list[dict[str, object]] = []
    try:
        with connect() as client:
            stamp = secrets.token_hex(4)
            markers = []
            for index in range(3):
                receipt = client.long_term.put(
                    kind=MemoryKind.PREFERENCE,
                    text=f"标记 {index} 记录 zzmarker{stamp}",
                    tags=("gateway-health",), sync_policy=SyncPolicy.LOCAL_ONLY,
                    expires_at=int(time.time() * 1000) + 900_000,
                    idempotency_key=f"gateway-health-marker:{os.getpid()}:{index}")
                markers.append(receipt.memory_id)
            kept_markers = markers[:2]
            if secret is not None:
                forgotten_marker = markers[2]
                job = client.long_term.forget(
                    forgotten_marker,
                    confirm_token=confirmation_token(secret, "long_term.forget"))
                client.long_term.wait_forget(job.job_id)
            time.sleep(4)
            quiesced["before_load"] = verdict(client)

            threads = []
            if PROBE_MODE in {"writers", "both"}:
                threads += [threading.Thread(target=writer_loop, args=(i,)) for i in range(WRITERS)]
            if PROBE_MODE in {"readers", "both"}:
                threads += [
                    threading.Thread(target=reader_loop, args=(i, f"zzmarker{stamp}"))
                    for i in range(READERS)
                ]
            for thread in threads:
                thread.start()
            end = time.time() + LOAD_SECONDS
            while time.time() < end:
                first = client.health()
                second = client.health()
                first_check = first.get("database_check") or {}
                hits = {h.memory_id for h in client.long_term.search_headers(f"zzmarker{stamp}", top_k=20)}
                with LOCK:
                    SAMPLES.append({
                        "status": first["status"],
                        "database": first["database"],
                        "check_state": first_check.get("state"),
                        "verdict": first_check.get("verdict"),
                        "pair_flip": first["database"] != second["database"],
                        "missing_kept_markers": [m for m in kept_markers if m not in hits],
                        "forgotten_marker_resurfaced": bool(forgotten_marker and forgotten_marker in hits),
                    })
            STOP.set()
            for thread in threads:
                thread.join(timeout=60)
            # Keep looking after the writers stop: a verdict that only shows up
            # in one sample, or only while the WAL is still warm, is the
            # artifact; the resting verdict is what the daemon settles on.
            started = time.time()
            for index in range(AFTER_LOAD_SAMPLES):
                sample = verdict(client)
                sample["elapsed_ms"] = int((time.time() - started) * 1000)
                after_load.append(sample)
                if index + 1 < AFTER_LOAD_SAMPLES:
                    time.sleep(AFTER_LOAD_INTERVAL)
            quiesced["after_load"] = after_load[-1]
    finally:
        try:
            with connect() as client:
                if secret is not None:
                    for memory_id in list(WRITTEN) + kept_markers:
                        job = client.long_term.forget(
                            memory_id,
                            confirm_token=confirmation_token(secret, "long_term.forget"))
                        client.long_term.wait_forget(job.job_id)
                else:
                    print("WARNING: confirmation secret unreadable; probe rows left to expire")
                stats = client.stats()
                print("=== cleanup ===", stats.get("long_term_memories"), stats.get("memory_embeddings"))
        finally:
            registration.unlink(missing_ok=True)

    with LOCK:
        samples = [
            s for s in SAMPLES if s["status"] not in {"writer-error", "reader-error"}
        ]
        load_errors = [s for s in SAMPLES if s["status"] in {"writer-error", "reader-error"}]
    conclusive = [s for s in samples if s["status"] in {"ok", "degraded"}]
    degraded = [s for s in conclusive if s["status"] != "ok"]
    inconclusive = [s for s in samples if s["status"] not in {"ok", "degraded"}]
    flips = [s for s in samples if s["pair_flip"]]
    missing = [s for s in samples if s["missing_kept_markers"]]
    resurrected = [s for s in samples if s["forgotten_marker_resurfaced"]]
    # The artifact is at its worst right after the writers stop, so the gate is
    # not satisfied by the load phase alone: a `degraded` in the post-load
    # series is a failure even when every sample during the load was clean.
    after_load_degraded = [s for s in after_load if s["status"] == "degraded"]
    after_load_flips = [
        (after_load[i - 1], after_load[i])
        for i in range(1, len(after_load))
        if after_load[i - 1]["database"] != after_load[i]["database"]
    ]
    passed = (
        not degraded
        and not flips
        and not missing
        and not resurrected
        and not after_load_degraded
        and not after_load_flips
    )
    print("=== quiesced ===", json.dumps(quiesced, ensure_ascii=False))
    print("=== after load ===")
    for sample in after_load:
        print(json.dumps(sample, ensure_ascii=False))
    print("=== summary ===")
    print(json.dumps({
        "verdict": "K3_HEALTH_CONCURRENCY_PROBE_OK" if passed else "K3_HEALTH_CONCURRENCY_PROBE_FAIL",
        "mode": PROBE_MODE,
        "samples": len(samples),
        "load_errors": len(load_errors),
        "conclusive": len(conclusive),
        "degraded": len(degraded),
        "inconclusive": len(inconclusive),
        "pair_flips": len(flips),
        "after_load_samples": len(after_load),
        "after_load_degraded": len(after_load_degraded),
        "after_load_flips": len(after_load_flips),
        "after_load_resting": quiesced.get("after_load"),
        "samples_missing_kept_marker": len(missing),
        "samples_forgotten_marker_resurfaced": len(resurrected),
        "check_states": sorted({str(s["check_state"]) for s in samples}),
        "distinct_verdicts": sorted({str(s["verdict"]) for s in samples}),
        "distinct_database": sorted({str(s["database"]) for s in samples}),
    }, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
