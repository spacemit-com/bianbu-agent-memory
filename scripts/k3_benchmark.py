#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import time
from collections.abc import Callable
from typing import Any

from bianbu_agent_sdk.memory import MemoryClient, MemoryKind, SyncPolicy


def percentile(samples: list[float], value: float) -> float:
    ordered = sorted(samples)
    index = max(0, math.ceil(len(ordered) * value) - 1)
    return ordered[index]


def measure(count: int, operation: Callable[[int], Any]) -> tuple[list[float], float]:
    samples: list[float] = []
    started = time.perf_counter()
    for index in range(count):
        before = time.perf_counter_ns()
        operation(index)
        samples.append((time.perf_counter_ns() - before) / 1_000_000)
    elapsed = time.perf_counter() - started
    return samples, elapsed


def describe(samples: list[float], elapsed: float) -> dict[str, float | int]:
    return {
        "count": len(samples),
        "p50_ms": round(percentile(samples, 0.50), 3),
        "p95_ms": round(percentile(samples, 0.95), 3),
        "p99_ms": round(percentile(samples, 0.99), 3),
        "max_ms": round(max(samples), 3),
        "throughput_ops_s": round(len(samples) / elapsed, 2),
    }


def benchmark(socket_path: str, count: int, prefix: str) -> dict[str, object]:
    with MemoryClient.connect(socket_path, timeout=10.0) as client:
        for _ in range(20):
            client.health()

        health_samples, health_elapsed = measure(count, lambda _: client.health())

        memory_ids: list[str] = []

        def write(index: int) -> None:
            receipt = client.long_term.put(
                kind=MemoryKind.PREFERENCE,
                text=f"{prefix} benchmark preference {index}: use deterministic local storage",
                tags=("scheme-a-benchmark",),
                sync_policy=SyncPolicy.LOCAL_ONLY,
                confidence=0.9,
                idempotency_key=f"{prefix}:write:{index}",
            )
            memory_ids.append(receipt.memory_id)

        write_samples, write_elapsed = measure(count, write)
        search_samples, search_elapsed = measure(
            count,
            lambda _: client.long_term.search_headers(
                f"{prefix} benchmark preference", top_k=10
            ),
        )
        selected = memory_ids[-10:]
        get_samples, get_elapsed = measure(
            count, lambda _: client.long_term.get_many(selected)
        )
        stats = client.stats()

    return {
        "status": "K3_BENCHMARK_OK",
        "architecture": "riscv64",
        "mode": "single_process_single_client_sequential",
        "operations": {
            "health": describe(health_samples, health_elapsed),
            "long_term_put": describe(write_samples, write_elapsed),
            "search_headers": describe(search_samples, search_elapsed),
            "get_many_10": describe(get_samples, get_elapsed),
        },
        "stats": stats,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("socket_path")
    parser.add_argument("--count", type=int, default=300)
    parser.add_argument("--prefix", default=f"bench-{int(time.time())}")
    arguments = parser.parse_args()
    if arguments.count < 10:
        raise SystemExit("--count must be at least 10")
    print(
        json.dumps(
            benchmark(arguments.socket_path, arguments.count, arguments.prefix),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
