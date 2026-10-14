#!/usr/bin/env python3
"""Null-request floor probe for MEM-NFR-01-01 (attribution of the p95 tail).

Formal benches (v5, formal7/formal8) show search p50 ~3ms but client-observed
p95 ~130ms with 32 concurrent clients. The probe's 32-process cache-hit arm
reproduces the same p95 with near-zero service work, so the tail is not the
lexical pipeline. This script separates two remaining hypotheses:

  H1: the floor lives in the generic request path (unix socket wakeup,
      daemon thread scheduling, tiny response) -> any method shows it;
  H2: the floor is specific to the search/get_many response shape
      (larger payloads, client-side parse) -> only those methods show it.

Method: spawn an isolated daemon over the existing probe root (no reseed),
then run 32 forked worker processes x N ops of (a) privacy.settings.get
(tiny response, no memory gate) and (b) long_term.search_headers on a
repeated query (cache hit). Report client-observed percentiles for both.

Run from the repo root on K3:

    env PYTHONPATH=sdk/python/src python3 scripts/k3_null_request_floor.py \
        --root /home/example/probe-100k
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from k3_perf_acceptance import COMMON_QUERIES, Daemon  # noqa: E402

from bianbu_agent_sdk.memory import MemoryClient  # noqa: E402


def worker(
    socket_path: str,
    mode: str,
    worker_index: int,
    ops: int,
    start_at: float,
    out_path: str,
) -> None:
    """One OS process per client; mode picks the request shape."""
    from bianbu_agent_sdk.memory import MemoryClient

    while time.time() < start_at:
        time.sleep(0.005)
    samples: list[float] = []
    client = MemoryClient.connect(socket_path, timeout=120.0)
    query = COMMON_QUERIES[worker_index % len(COMMON_QUERIES)]
    try:
        client.privacy.settings()  # warm the per-process connection
        client.long_term.search_headers(query, top_k=10)  # warm the cache
        for _ in range(ops):
            before = time.perf_counter_ns()
            if mode == "settings":
                client.privacy.settings()
            else:
                client.long_term.search_headers(query, top_k=10)
            samples.append((time.perf_counter_ns() - before) / 1_000_000)
    finally:
        client.close()
        with open(out_path, "w", encoding="utf-8") as handle:
            json.dump(samples, handle)


def describe(values: list[float]) -> str:
    ordered = sorted(values)
    n = len(ordered)

    def pct(p: float) -> float:
        return ordered[max(0, int(n * p) - 1)]

    over = sum(1 for v in ordered if v > 100.0)
    return (
        f"n={n} p50={pct(0.5):.1f}ms p90={pct(0.9):.1f}ms p95={pct(0.95):.1f}ms "
        f"p99={pct(0.99):.1f}ms max={ordered[-1]:.1f}ms "
        f">100ms={100.0 * over / n:.1f}%"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--memoryd", default="target/release/bianbu-agent-memoryd", type=Path)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--clients", default=32, type=int)
    parser.add_argument("--ops", default=50, type=int)
    args = parser.parse_args()

    args.root.mkdir(parents=True, exist_ok=True)
    daemon = Daemon(args.memoryd, args.root)
    socket_path = daemon.start()
    try:
        results = {}
        for mode in ("settings", "search"):
            start_at = time.time() + 3.0
            proc_dir = tempfile.mkdtemp(prefix=f"null-floor-{mode}-")
            procs = []
            for index in range(args.clients):
                out_path = os.path.join(proc_dir, f"w{index}.json")
                proc = multiprocessing.get_context("fork").Process(
                    target=worker,
                    args=(str(socket_path), mode, index, args.ops, start_at, out_path),
                )
                proc.start()
                procs.append((proc, out_path))
            for proc, _ in procs:
                proc.join()
            samples: list[float] = []
            for _, out_path in procs:
                with open(out_path, encoding="utf-8") as handle:
                    samples.extend(json.load(handle))
            results[mode] = samples
            print(f"== {args.clients} processes x {args.ops} ops: {mode} ==")
            print(f"latency: {describe(samples)}")
    finally:
        daemon.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
