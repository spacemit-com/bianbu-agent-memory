#!/usr/bin/env python3
"""Single-client service-time probe for MEM-NFR-01-01 (fts2 cascade).

Seeds --records rows (reusing k3_perf_acceptance's generator), then runs a
single-client battery over the bench query mix with SEMANTIC_CACHE_TRACE=1
so the daemon's [search-trace] stage lines can be compared against the
client-observed latency. Run from the repo root on K3:

    env PYTHONPATH=sdk/python/src python3 scripts/k3_lexical_service_probe.py \
        --root /home/example/probe-100k [--skip-seed]

The root directory persists, so a re-probe after a daemon rebuild skips the
~3.5-minute seed with --skip-seed.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from k3_perf_acceptance import (  # noqa: E402
    COMMON_QUERIES,
    MODULES,
    Daemon,
    seed,
)

from bianbu_agent_sdk.memory import MemoryClient  # noqa: E402


class TracedDaemon(Daemon):
    """Daemon variant that keeps stderr in a file for [search-trace] lines."""

    def start(self) -> Path:
        socket_path = self.root / "memoryd.sock"
        secret = self.root / "account-secret"
        if not secret.exists():
            secret.write_text(uuid.uuid4().hex + uuid.uuid4().hex + "\n", encoding="utf-8")
            secret.chmod(0o600)
        command = [
            str(self.memoryd), "--development-identity", "--socket", str(socket_path),
            "--database", str(self.root / "memory.db"),
            "--key-package", str(self.root / "memory.keypkg"),
            "--account-secret-file", str(secret), "--initialize-key-package",
        ]
        environment = dict(os.environ)
        environment["SEMANTIC_CACHE_TRACE"] = "1"
        self.trace_path = self.root / "daemon-trace.log"
        self._trace_file = open(self.trace_path, "a", encoding="utf-8")
        self.process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=self._trace_file,
            text=True, env=environment,
        )
        self._wait_for_socket(socket_path)
        return socket_path


def process_worker(
    socket_path: str, queries: list[str], worker_index: int, ops: int,
    start_at: float, out_path: str,
) -> None:
    """One real OS process per client: no GIL, the way 32 agents would load it."""
    from bianbu_agent_sdk.memory import MemoryClient

    while time.time() < start_at:
        time.sleep(0.005)
    samples: list[float] = []
    client = MemoryClient.connect(socket_path, timeout=120.0)
    try:
        for op in range(ops):
            query = queries[(worker_index * ops + op) % len(queries)]
            before = time.perf_counter_ns()
            client.long_term.search_headers(query, top_k=10)
            samples.append((time.perf_counter_ns() - before) / 1_000_000)
    finally:
        client.close()
        with open(out_path, "w", encoding="utf-8") as handle:
            json.dump(samples, handle)


def stage_percentiles(trace_text: str) -> dict[str, str]:
    """p50/p95/max of each [search-trace] stage over a trace segment."""
    import re

    buckets: dict[str, list[float]] = {}
    for line in trace_text.splitlines():
        if "search-trace" not in line:
            continue
        match = re.search(r"\] ([a-z-]+) ([0-9.]+)ms", line)
        if match:
            buckets.setdefault(match.group(1), []).append(float(match.group(2)))
    report = {}
    for stage, values in buckets.items():
        ordered = sorted(values)
        report[stage] = (
            f"n={len(ordered)} p50={ordered[len(ordered)//2]:.0f}ms "
            f"p95={ordered[max(0, int(len(ordered)*0.95)-1)]:.0f}ms max={ordered[-1]:.0f}ms"
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--memoryd", default="target/release/bianbu-agent-memoryd", type=Path)
    parser.add_argument("--records", default=100_000, type=int)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--skip-seed", action="store_true")
    parser.add_argument("--concurrent-clients", default=32, type=int)
    parser.add_argument("--concurrent-ops", default=50, type=int)
    args = parser.parse_args()

    args.root.mkdir(parents=True, exist_ok=True)
    daemon = TracedDaemon(args.memoryd, args.root)
    socket_path = daemon.start()
    try:
        if args.skip_seed:
            report_seed = {"skipped": True}
        else:
            report_seed = seed(socket_path, args.records)

        battery: list[tuple[str, str]] = []
        rare = 0
        for index, query in enumerate(COMMON_QUERIES * 5):
            battery.append((f"common-{index % len(COMMON_QUERIES)}", query))
        for index in range(5):
            battery.append((f"docs-{index}", f"docs-{4000 + index * 37} {MODULES[index % len(MODULES)]}"))
            rare += 1

        with MemoryClient.connect(socket_path, timeout=60.0) as client:
            for _, query in battery[:6]:  # warmup
                client.long_term.search_headers(query, top_k=10)
            samples: dict[str, list[float]] = {}
            trace_mark = daemon.trace_path.stat().st_size
            for label, query in battery:
                before = time.perf_counter_ns()
                headers = client.long_term.search_headers(query, top_k=10)
                elapsed = (time.perf_counter_ns() - before) / 1_000_000
                samples.setdefault(label, []).append(elapsed)
                if len(headers) < 10 and label.startswith("common"):
                    print(f"note: common query {label!r} returned {len(headers)} rows", file=sys.stderr)

            get_many_ids: list[str] = []
            for _, query in battery[:2]:
                for header in client.long_term.search_headers(query, top_k=10):
                    get_many_ids.append(header.memory_id)
            read_samples: list[float] = []
            for _ in range(20):
                before = time.perf_counter_ns()
                client.long_term.get_many(get_many_ids[:10])
                read_samples.append((time.perf_counter_ns() - before) / 1_000_000)

        # Concurrent phase: same mix, N clients, to see queueing amplification.
        def worker(index: int, sink: list[float]) -> None:
            client = MemoryClient.connect(socket_path, timeout=120.0)
            try:
                for op in range(args.concurrent_ops):
                    query = battery[(index * args.concurrent_ops + op) % len(battery)][1]
                    before = time.perf_counter_ns()
                    client.long_term.search_headers(query, top_k=10)
                    sink.append((time.perf_counter_ns() - before) / 1_000_000)
            finally:
                client.close()

        queries_only = [query for _, query in battery]
        thread_mark = daemon.trace_path.stat().st_size
        concurrent: list[float] = []
        threads = [
            threading.Thread(target=worker, args=(index, concurrent))
            for index in range(args.concurrent_clients)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # Same load again, one OS process per client: no CPython GIL, the
        # way 32 real agent processes would drive the daemon.
        proc_mark = daemon.trace_path.stat().st_size
        proc_concurrent: list[float] = []
        proc_dir = tempfile.mkdtemp(prefix="probe-procs-")
        start_at = time.time() + 3.0
        procs = []
        for index in range(args.concurrent_clients):
            out_path = os.path.join(proc_dir, f"w{index}.json")
            proc = multiprocessing.get_context("fork").Process(
                target=process_worker,
                args=(str(socket_path), queries_only, index,
                      args.concurrent_ops, start_at, out_path),
            )
            proc.start()
            procs.append((proc, out_path))
        for proc, _ in procs:
            proc.join()
        for _, out_path in procs:
            with open(out_path, encoding="utf-8") as handle:
                proc_concurrent.extend(json.load(handle))

        trace = daemon.trace_path.read_text(encoding="utf-8")
        single_trace = trace[trace_mark:thread_mark]
        thread_trace = trace[thread_mark:proc_mark]
        proc_trace = trace[proc_mark:]
        stage_lines = [line for line in single_trace.splitlines() if "search-trace" in line]

    finally:
        daemon.stop()

    print(json.dumps({"seed": report_seed}, ensure_ascii=False))
    print("== single client ==")
    for label, values in samples.items():
        ordered = sorted(values)
        print(
            f"{label}: n={len(values)} p50={ordered[len(ordered)//2]:.1f}ms "
            f"max={ordered[-1]:.1f}ms"
        )
    ordered = sorted(read_samples)
    print(f"get_many(10): p50={ordered[len(ordered)//2]:.1f}ms max={ordered[-1]:.1f}ms")
    for title, values in (
        (f"{args.concurrent_clients} threads x {args.concurrent_ops} ops", concurrent),
        (f"{args.concurrent_clients} processes x {args.concurrent_ops} ops", proc_concurrent),
    ):
        ordered = sorted(values)
        print(f"== {title} ==")
        print(
            f"search: n={len(ordered)} p50={ordered[len(ordered)//2]:.1f}ms "
            f"p95={ordered[max(0, int(len(ordered)*0.95)-1)]:.1f}ms max={ordered[-1]:.1f}ms"
        )
    print("== daemon stage percentiles ==")
    for title, segment in (("threads", thread_trace), ("processes", proc_trace)):
        for stage, summary in stage_percentiles(segment).items():
            print(f"{title}/{stage}: {summary}")
    print("== daemon stage trace (single-client battery) ==")
    for line in stage_lines[:40]:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
