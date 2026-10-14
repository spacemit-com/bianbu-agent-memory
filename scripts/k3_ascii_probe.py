#!/usr/bin/env python3
"""Isolate which L1 pass is slow for rare ASCII runs (MEM-NFR-01-01 tail).

Reuses the persisted 100k probe database; every query runs 3 times after a
warmup so the per-label timings and the daemon [search-trace] lexical lines
can be compared. Run from the repo root on K3:

    env PYTHONPATH=sdk/python/src python3 scripts/k3_ascii_probe.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from k3_lexical_service_probe import TracedDaemon  # noqa: E402

from bianbu_agent_sdk.memory import MemoryClient  # noqa: E402

QUERIES: list[tuple[str, str]] = [
    ("cjk-common", "浏览器 默认"),
    ("ascii-rare-4074", "docs-4074"),
    ("ascii-rare-4037", "docs-4037"),
    ("ascii-rare-4000", "docs-4000"),
    ("ascii-cjk-4074", "docs-4074 浏览器"),
    ("ascii-dense", "docs"),
    ("ascii-nomatch", "zzzq-9999"),
    ("numeric-only", "4074"),
    ("alnum-mixed", "q7x-4074"),
    ("long-word", "configuration"),
]


def main() -> int:
    root = Path("/home/example/probe-100k")
    daemon = TracedDaemon(Path("target/release/bianbu-agent-memoryd"), root)
    socket_path = daemon.start()
    try:
        with MemoryClient.connect(socket_path, timeout=60.0) as client:
            # Recall check: does a rare+common mix actually return the rare
            # match, or just the newest rows that share the common term?
            for query in ("docs-4074", "docs-4074 浏览器", "4074 浏览器"):
                rows = [client.long_term.get_many([h.memory_id]) for h in
                        client.long_term.search_headers(query, top_k=10)]
                texts = [rec[0].text if rec else "" for rec in rows]
                hits = sum(1 for text in texts if "4074" in text)
                print(f"recall {query!r}: {hits}/10 rows contain '4074'")
                for text in texts[:3]:
                    print(f"  row: {text[:60]}")
            for _, query in QUERIES:  # warmup
                client.long_term.search_headers(query, top_k=10)
            mark = daemon.trace_path.stat().st_size
            for label, query in QUERIES:
                for _ in range(3):
                    before = time.perf_counter_ns()
                    rows = client.long_term.search_headers(query, top_k=10)
                    elapsed = (time.perf_counter_ns() - before) / 1_000_000
                    print(f"{label}: {elapsed:.1f}ms ({len(rows)} rows)")
    finally:
        daemon.stop()
    trace = daemon.trace_path.read_text(encoding="utf-8")[mark:]
    for line in trace.splitlines():
        if "search-trace" in line:
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
