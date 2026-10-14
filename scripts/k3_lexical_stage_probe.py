#!/usr/bin/env python3
"""MEM-NFR-01-01 diagnostic: per-stage latency of the lexical cascade.

Seeds a throwaway daemon with the same corpus as
``k3_perf_acceptance.py`` (100k records by default), then issues each
benchmark query ONCE from a single client with ``SEMANTIC_CACHE_TRACE=1``
on the daemon, so the [search-trace] stderr lines attribute the latency
to the lexical (FTS) / semantic / overlap / like-backfill stages.

This isolates query cost from client concurrency: the acceptance bench
amplifies these numbers by clients/cores contention.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from k3_perf_acceptance import COMMON_QUERIES, seed  # noqa: E402

from bianbu_agent_sdk.memory import MemoryClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--memoryd", default="target/release/bianbu-agent-memoryd", type=Path)
    parser.add_argument("--records", default=100_000, type=int)
    parser.add_argument("--top-k", default=10, type=int)
    args = parser.parse_args()

    os.environ["SEMANTIC_CACHE_TRACE"] = "1"
    queries = list(COMMON_QUERIES)
    # one rare query (>=3-char run -> fuzzy FTS fast path) as control
    queries.append("docs-4231 存储层")
    # one mixed query with a 3-char run (partially indexable)
    queries.append("密钥管理 规范")

    report: dict[str, Any] = {"status": "K3_LEXICAL_STAGE_PROBE", "records": args.records}
    with tempfile.TemporaryDirectory(prefix="agent-memory-lexprobe-") as raw:
        root = Path(raw)
        trace_path = root / "trace.log"
        socket_path = root / "memoryd.sock"
        secret = root / "account-secret"
        if not secret.exists():
            import uuid

            secret.write_text(uuid.uuid4().hex + uuid.uuid4().hex + "\n", encoding="utf-8")
            secret.chmod(0o600)
        command = [
            str(args.memoryd), "--development-identity",
            "--socket", str(socket_path),
            "--database", str(root / "memory.db"),
            "--key-package", str(root / "memory.keypkg"),
            "--account-secret-file", str(secret), "--initialize-key-package",
        ]
        with trace_path.open("wb") as trace:
            proc = subprocess.Popen(
                command, stdout=subprocess.DEVNULL, stderr=trace
            )
            import socket as socket_module

            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                probe = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
                try:
                    probe.connect(str(socket_path))
                    probe.close()
                    break
                except OSError:
                    probe.close()
                    time.sleep(0.05)
            else:
                raise SystemExit("daemon did not start")

            try:
                print(f"seeding {args.records} records ...", file=sys.stderr)
                report["seed"] = seed(socket_path, args.records)
                per_query = []
                with MemoryClient.connect(socket_path, timeout=180.0) as client:
                    # warm one query so first-touch page decrypts do not skew
                    client.long_term.search_headers(queries[0], top_k=args.top_k)
                    for query in queries:
                        started = time.perf_counter_ns()
                        headers = client.long_term.search_headers(query, top_k=args.top_k)
                        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
                        per_query.append({
                            "query": query,
                            "elapsed_ms": round(elapsed_ms, 1),
                            "rows": len(headers),
                        })
                        print(f"{query!r}: {elapsed_ms:.0f} ms, {len(headers)} rows",
                              file=sys.stderr)
                report["single_client"] = per_query
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()

        trace_text = trace_path.read_text(encoding="utf-8", errors="replace")
        report["trace_tail"] = trace_text.splitlines()[-40:]
        print("\n== daemon trace (stage attribution) ==", file=sys.stderr)
        for line in report["trace_tail"]:
            print(line, file=sys.stderr)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
