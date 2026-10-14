#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import signal
import socket
import statistics
import subprocess
import tempfile
import time
import uuid

from bianbu_agent_sdk.memory import MemoryClient, MemoryKind, SyncPolicy


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * quantile) - 1))
    return ordered[index]


def wait_for_socket(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise RuntimeError(f"memoryd exited during startup: {stderr.strip()}")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(path))
        except OSError:
            time.sleep(0.05)
        else:
            return
        finally:
            probe.close()
    raise TimeoutError(path)


def read_rss_kib(pid: int) -> int:
    status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return 0


def file_bytes(database: Path) -> int:
    return sum(
        path.stat().st_size
        for path in (
            database,
            Path(f"{database}-wal"),
            Path(f"{database}-shm"),
        )
        if path.exists()
    )


def start_memoryd(
    memoryd: Path,
    socket_path: Path,
    database: Path,
    key_package: Path,
    secret_path: Path,
    *,
    initialize: bool,
) -> subprocess.Popen[str]:
    arguments = [
        str(memoryd),
        "--development-identity",
        "--socket",
        str(socket_path),
        "--database",
        str(database),
        "--key-package",
        str(key_package),
        "--account-secret-file",
        str(secret_path),
        "--background-interval-seconds",
        "3600",
    ]
    if initialize:
        arguments.append("--initialize-key-package")
    process = subprocess.Popen(
        arguments,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    wait_for_socket(socket_path, process)
    return process


def populate_worker(
    socket_path: Path,
    worker: int,
    start: int,
    stop: int,
) -> tuple[int, list[str]]:
    client = MemoryClient.connect(socket_path, timeout=30)
    retained: list[str] = []
    try:
        for index in range(start, stop):
            receipt = client.long_term.put(
                kind=MemoryKind.FACT,
                text=(
                    f"capacity marker record {index:06d}; worker={worker}; "
                    f"lookup-token=scale-{index % 1000:03d}"
                ),
                tags=("scale-acceptance", f"worker:{worker}"),
                sync_policy=SyncPolicy.LOCAL_ONLY,
                idempotency_key=f"scale:{worker}:{index}",
            )
            if index in {start, stop - 1}:
                retained.append(receipt.memory_id)
        return stop - start, retained
    finally:
        client.close()


def query_worker(socket_path: Path, worker: int, samples: int) -> list[float]:
    client = MemoryClient.connect(socket_path, timeout=30)
    latencies: list[float] = []
    try:
        for sample in range(samples):
            query = f"scale-{(worker * samples + sample) % 1000:03d}"
            started = time.perf_counter()
            headers = client.long_term.search_headers(query, top_k=10)
            records = client.long_term.get_many(
                [header.memory_id for header in headers]
            )
            latency = (time.perf_counter() - started) * 1000
            if not headers or not records:
                raise AssertionError(f"query returned no canonical records: {query}")
            latencies.append(latency)
        return latencies
    finally:
        client.close()


def query_once(client: MemoryClient, query: str) -> float:
    started = time.perf_counter()
    headers = client.long_term.search_headers(query, top_k=10)
    records = client.long_term.get_many([header.memory_id for header in headers])
    latency = (time.perf_counter() - started) * 1000
    if not headers or not records:
        raise AssertionError(f"query returned no canonical records: {query}")
    return latency


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--memoryd", default="target/release/bianbu-agent-memoryd", type=Path
    )
    parser.add_argument("--records", default=100_000, type=int)
    parser.add_argument("--clients", default=32, type=int)
    parser.add_argument("--write-clients", type=int)
    parser.add_argument("--query-samples-per-client", default=10, type=int)
    arguments = parser.parse_args()
    write_clients = arguments.write_clients or arguments.clients
    if (
        arguments.records < max(arguments.clients, write_clients)
        or arguments.clients < 1
        or write_clients < 1
    ):
        raise ValueError(
            "records must be at least client counts, and client counts must be positive"
        )
    memoryd = arguments.memoryd.resolve()
    if not memoryd.is_file():
        raise FileNotFoundError(memoryd)

    with tempfile.TemporaryDirectory(prefix="agent-memory-scale-") as raw:
        root = Path(raw)
        socket_path = root / "memoryd.sock"
        database = root / "memory.db"
        key_package = root / "memory.keypkg"
        secret_path = root / "account-secret"
        secret_path.write_text(uuid.uuid4().hex + uuid.uuid4().hex + "\n", encoding="utf-8")
        secret_path.chmod(0o600)
        process = start_memoryd(
            memoryd,
            socket_path,
            database,
            key_package,
            secret_path,
            initialize=True,
        )
        retained_ids: list[str] = []
        try:
            ranges = []
            for worker in range(write_clients):
                start = arguments.records * worker // write_clients
                stop = arguments.records * (worker + 1) // write_clients
                ranges.append((worker, start, stop))
            write_started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=write_clients) as executor:
                futures = [
                    executor.submit(
                        populate_worker, socket_path, worker, start, stop
                    )
                    for worker, start, stop in ranges
                ]
                written = 0
                for future in as_completed(futures):
                    count, ids = future.result()
                    written += count
                    retained_ids.extend(ids)
            write_seconds = time.perf_counter() - write_started
            if written != arguments.records:
                raise AssertionError(f"wrote {written}, expected {arguments.records}")

            with MemoryClient.connect(socket_path, timeout=30) as client:
                stats = client.stats()
            if int(stats["long_term_memories"]) != arguments.records:
                raise AssertionError(stats)

            query_started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=arguments.clients) as executor:
                futures = [
                    executor.submit(
                        query_worker,
                        socket_path,
                        worker,
                        arguments.query_samples_per_client,
                    )
                    for worker in range(arguments.clients)
                ]
                latencies = [
                    latency
                    for future in as_completed(futures)
                    for latency in future.result()
                ]
            query_seconds = time.perf_counter() - query_started
            p50 = statistics.median(latencies)
            p95 = percentile(latencies, 0.95)
            before_kill = {
                "rss_kib": read_rss_kib(process.pid),
                "database_bytes": file_bytes(database),
                "stats": stats,
            }
            os.kill(process.pid, signal.SIGKILL)
            process.wait(timeout=30)
            process = start_memoryd(
                memoryd,
                socket_path,
                database,
                key_package,
                secret_path,
                initialize=False,
            )
            with MemoryClient.connect(socket_path, timeout=30) as client:
                recovered_stats = client.stats()
                recovered = client.long_term.get_many(retained_ids)
                health = client.health()
                cold_latency = query_once(client, "scale-000")
                warm_latencies = [
                    query_once(client, f"scale-{sample % 1000:03d}")
                    for sample in range(100)
                ]
            if int(recovered_stats["long_term_memories"]) != arguments.records:
                raise AssertionError(recovered_stats)
            if len(recovered) != len(retained_ids):
                raise AssertionError(
                    f"recovered {len(recovered)} retained records, expected {len(retained_ids)}"
                )

            warm_p95 = percentile(warm_latencies, 0.95)
            performance_passed = p95 <= 100 and warm_p95 <= 100
            result = {
                "status": (
                    "K3_SCALE_RECOVERY_ACCEPTANCE_OK"
                    if performance_passed
                    else "K3_SCALE_RECOVERY_ACCEPTANCE_FAILED"
                ),
                "records": arguments.records,
                "write_clients": write_clients,
                "query_clients": arguments.clients,
                "write": {
                    "seconds": round(write_seconds, 3),
                    "records_per_second": round(
                        arguments.records / write_seconds, 3
                    ),
                    "errors": 0,
                },
                "query_plus_canonical_read_ms": {
                    "samples": len(latencies),
                    "concurrent_clients": arguments.clients,
                    "seconds": round(query_seconds, 3),
                    "p50": round(p50, 3),
                    "p95": round(p95, 3),
                    "target": 100,
                    "passed": p95 <= 100,
                },
                "before_kill": before_kill,
                "restart_recovery": {
                    "signal": "SIGKILL",
                    "health": health,
                    "records": recovered_stats["long_term_memories"],
                    "sample_records_recovered": len(recovered),
                    "cold_query_plus_canonical_read_ms": round(cold_latency, 3),
                    "warm_query_plus_canonical_read_ms": {
                        "samples": len(warm_latencies),
                        "p50": round(statistics.median(warm_latencies), 3),
                        "p95": round(warm_p95, 3),
                        "target": 100,
                        "passed": warm_p95 <= 100,
                    },
                },
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if not performance_passed:
                raise SystemExit(2)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()


if __name__ == "__main__":
    main()
