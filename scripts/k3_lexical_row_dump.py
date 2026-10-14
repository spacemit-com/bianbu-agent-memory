#!/usr/bin/env python3
"""MEM-NFR-01-01 diagnostic: dump actual stored rows + discriminating queries.

Seeds a throwaway daemon with the perf corpus, then answers:
  1. what content/summary/tags the daemon ACTUALLY stores for corpus rows
     (search a distinctive substring, get_many the hits, print raw fields);
  2. stats() long-term count (slot collapse would show < records);
  3. discriminating two-2-char-term queries to find why "密钥 管理"
     returns 0 rows on K3 while the local replica returns 10.
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
from k3_perf_acceptance import seed  # noqa: E402

from bianbu_agent_sdk.memory import MemoryClient  # noqa: E402


def wait_socket(path: Path, process: subprocess.Popen) -> None:
    import socket

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit("daemon exited early")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(path))
            return
        except OSError:
            time.sleep(0.05)
        finally:
            probe.close()
    raise SystemExit("daemon socket timeout")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--memoryd", default="target/release/bianbu-agent-memoryd", type=Path)
    parser.add_argument("--records", default=20_000, type=int)
    args = parser.parse_args()

    queries = [
        "密钥 管理",       # K3: 0 rows (mystery)   replica: 10
        "密钥管理 规范",    # K3: 0 rows (mystery)   replica: 10
        "深色 主题",       # K3: 50 rows            replica: 6
        "偏好 终端",       # both words in T1 rows: expected hit
        "事实 负责人",     # both words in T2 rows: expected hit
        "密钥管理",        # single 4-char run: lexical phrase path
        "存储层 检索层",    # two module names, co-occurrence ~0
    ]

    report: dict[str, Any] = {"status": "K3_LEXICAL_ROW_DUMP", "records": args.records}
    with tempfile.TemporaryDirectory(prefix="agent-memory-rowdump-") as raw:
        root = Path(raw)
        socket_path = root / "memoryd.sock"
        secret = root / "account-secret"
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
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_socket(socket_path, process)
            report["seed"] = seed(socket_path, args.records)
            with MemoryClient.connect(socket_path, timeout=180.0) as client:
                stats = client.stats()
                report["stats_long_term"] = stats.get("long_term") or stats

                per_query = []
                for query in queries:
                    started = time.perf_counter_ns()
                    headers = client.long_term.search_headers(query, top_k=10)
                    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
                    per_query.append({
                        "query": query,
                        "elapsed_ms": round(elapsed_ms, 1),
                        "rows": len(headers),
                        "top3": [
                            {
                                "memory_id": h.memory_id,
                                "kind": getattr(h, "kind", None),
                                "summary": getattr(h, "summary", None),
                                "score": getattr(h, "score", None),
                            }
                            for h in headers[:3]
                        ],
                    })
                    print(f"{query!r}: {elapsed_ms:.0f} ms, {len(headers)} rows", file=sys.stderr)
                report["queries"] = per_query

                # full-row dump of rows containing 密钥管理 (phrase path)
                headers = client.long_term.search_headers("密钥管理", top_k=3)
                if headers:
                    records = client.long_term.get_many([h.memory_id for h in headers])
                    report["dump"] = [
                        {
                            "memory_id": r.memory_id,
                            "kind": str(getattr(r, "kind", "")),
                            "content": getattr(r, "content", None),
                            "summary": getattr(r, "summary", None),
                            "tags": list(getattr(r, "tags", ()) or ()),
                            "revision": getattr(r, "revision", None),
                        }
                        for r in records
                    ]
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
