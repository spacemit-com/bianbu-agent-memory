#!/usr/bin/env python3
"""MEM-NFR-02 quality evaluation: Qwen3 query-instruct prefix A/B test.

Seeds the ~300-record Chinese retrieval corpus (tests/data/
memory_retrieval_corpus.jsonl) into a throwaway daemon through the REAL
Qwen3 embedding model (seeding is small enough that inline embed with the
real model is fine, unlike the 100k perf benchmark), then scores the ~100
gold queries (tests/data/memory_retrieval_goldset.jsonl) in three modes:

* instruct    — default query_instruct prefix (the shipped default);
* noinstruct  — empty prefix (A/B baseline for the prefix decision);
* fts         — embedding disabled (lexical BM25 baseline).

Metrics: Recall@1/3/5 + MRR overall and per category (paraphrase, negation,
word_order, mixed_language, short_query, long_query, distractor), plus a
failure-case dump for qualitative review.

Gates (deliverable #2 formal conclusion):
* recall@5(instruct) >= recall@5(fts) + 0.15   (semantic gain);
* recall@5(instruct) >= recall@5(noinstruct)   (prefix does no harm).

If the second gate fails, the default query_instruct in product.rs must be
revised and this evaluation re-run — that outcome is itself the deliverable.

Usage (K3, from the repository root after ``cargo build --release``):

    python3 scripts/k3_embedding_quality_eval.py \
        --memoryd target/release/bianbu-agent-memoryd
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from bianbu_agent_sdk.memory import MemoryClient, MemoryKind, SyncPolicy

CORPUS_PATH = Path("tests/data/memory_retrieval_corpus.jsonl")
GOLDSET_PATH = Path("tests/data/memory_retrieval_goldset.jsonl")

MODEL_ID = "qwen3-embedding-0.6b"
EMBED_DIMS = 1024
DEFAULT_INSTRUCT = (
    "Instruct: Given a user query about a person's memories, retrieve the "
    "stored memory records that are semantically relevant to the query\nQuery: "
)
MODES = ("instruct", "noinstruct", "fts")
TOP_K = 5


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


class LlamaServer:
    def __init__(self, binary: Path, model: Path, port: int, threads: int) -> None:
        self.port = port
        self.process: subprocess.Popen[str] | None = None
        # --parallel 1 matches the deployed embedding unit (§7.5): more
        # slots split --ctx-size (2048/8=256 tokens) and silently truncate
        # long corpus records, skewing the quality numbers. The eval is
        # sequential anyway — extra slots buy nothing.
        self.command = [
            str(binary), "--model", str(model), "--embedding", "--pooling", "last",
            "--host", "127.0.0.1", "--port", str(port), "--ctx-size", "2048",
            "--threads", str(threads), "--parallel", "1", "--no-webui",
        ]

    def start(self, timeout_s: float = 240.0) -> None:
        self.process = subprocess.Popen(
            self.command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True
        )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"llama-server exited with {self.process.returncode}")
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/health", timeout=5
                ) as response:
                    if response.status == 200:
                        return
            except OSError:
                time.sleep(1.0)
        raise TimeoutError("llama-server did not become healthy")

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)


class Daemon:
    def __init__(self, memoryd: Path, root: Path) -> None:
        self.memoryd = memoryd
        self.root = root
        self.process: subprocess.Popen[str] | None = None

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
        self.process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError(
                    self.process.stderr.read() if self.process.stderr else "memoryd exited"
                )
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(socket_path))
                return socket_path
            except OSError:
                time.sleep(0.05)
            finally:
                probe.close()
        raise TimeoutError(socket_path)

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()


def embedding_patch(base_url: str, enabled: bool, query_instruct: str) -> dict[str, Any]:
    # The daemon replaces the whole embedding section on update; always send
    # the complete value so model/dims stay stable across mode switches.
    return {
        "enabled": enabled,
        "base_url": base_url,
        "model": MODEL_ID,
        "dims": EMBED_DIMS,
        "query_instruct": query_instruct,
    }


def seed(
    socket_path: Path, corpus: list[dict[str, Any]]
) -> tuple[dict[str, str], dict[str, Any]]:
    """Seed the corpus; returns corpus_id -> daemon memory_id mapping.

    The daemon mints its own UUIDs on put, while the goldset addresses
    records by corpus id — every search result has to be translated back.
    """
    mapping: dict[str, str] = {}
    started = time.monotonic()
    with MemoryClient.connect(socket_path, timeout=60.0) as client:
        for record in corpus:
            receipt = client.long_term.put(
                kind=MemoryKind(record["kind"]),
                text=record["text"],
                tags=("quality-corpus", record["memory_id"]),
                sync_policy=SyncPolicy.LOCAL_ONLY,
                idempotency_key=f"quality:{record['memory_id']}",
            )
            mapping[record["memory_id"]] = receipt.memory_id
        embedded = client.stats().get("memory_embeddings", 0)
    return mapping, {
        "records": len(corpus),
        "seed_elapsed_s": round(time.monotonic() - started, 1),
        "memory_embeddings": embedded,
    }


def run_mode(
    socket_path: Path,
    goldset: list[dict[str, Any]],
    mapping: dict[str, str],
    mode: str,
) -> dict[str, Any]:
    per_category: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"hits_at_1": 0, "hits_at_3": 0, "hits_at_5": 0, "rr": 0.0, "count": 0}
    )
    failures: list[dict[str, Any]] = []
    reverse = {daemon_id: corpus_id for corpus_id, daemon_id in mapping.items()}
    with MemoryClient.connect(socket_path, timeout=30.0) as client:
        for case in goldset:
            headers = client.long_term.search_headers(case["query"], top_k=TOP_K)
            ranked = [reverse.get(header.memory_id, header.memory_id) for header in headers]
            expected = case["expected_memory_id"]
            rank = ranked.index(expected) + 1 if expected in ranked else 0
            bucket = per_category[case["category"]]
            bucket["count"] += 1
            if rank >= 1:
                bucket["hits_at_1"] += 1
            if rank >= 1 and rank <= 3:
                bucket["hits_at_3"] += 1
            if rank >= 1:
                bucket["hits_at_5"] += 1
                bucket["rr"] += 1.0 / rank
            if rank == 0:
                failures.append({
                    "case_id": case["case_id"],
                    "category": case["category"],
                    "query": case["query"],
                    "expected": expected,
                    "returned": ranked,
                    "rank": rank,
                })
    total = len(goldset)

    def rollup(bucket: dict[str, Any]) -> dict[str, float]:
        count = bucket["count"] or 1
        return {
            "count": bucket["count"],
            "recall_at_1": round(bucket["hits_at_1"] / count, 4),
            "recall_at_3": round(bucket["hits_at_3"] / count, 4),
            "recall_at_5": round(bucket["hits_at_5"] / count, 4),
            "mrr": round(bucket["rr"] / count, 4),
        }

    overall = rollup({
        "count": total,
        "hits_at_1": sum(b["hits_at_1"] for b in per_category.values()),
        "hits_at_3": sum(b["hits_at_3"] for b in per_category.values()),
        "hits_at_5": sum(b["hits_at_5"] for b in per_category.values()),
        "rr": sum(b["rr"] for b in per_category.values()),
    })
    return {
        "mode": mode,
        "overall": overall,
        "per_category": {key: rollup(value) for key, value in sorted(per_category.items())},
        "failure_count": len(failures),
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--memoryd", default="target/release/bianbu-agent-memoryd", type=Path)
    parser.add_argument("--llama-server", default=Path("/usr/bin/llama-server"), type=Path)
    # Outside the spacemit-ai-gateway Embed pool (18800-18900): this harness
    # starts its own llama-server to control the parameters it measures, and a
    # port inside that range can be handed to a Gateway-spawned model at any
    # time.
    parser.add_argument("--embedding-port", default=18901, type=int)
    parser.add_argument("--threads", default=8, type=int)
    parser.add_argument("--embedding-model", type=Path, default=None,
                        help="gguf path; default: first match under the packaged "
                             "models dir and ~/.cache/models/embedding/")
    parser.add_argument("--corpus", type=Path, default=CORPUS_PATH)
    parser.add_argument("--goldset", type=Path, default=GOLDSET_PATH)
    parser.add_argument("--instruct", default=DEFAULT_INSTRUCT,
                        help="query prefix under evaluation (the A arm)")
    parser.add_argument("--gain-gate", default=0.15, type=float,
                        help="required recall@5 gain of instruct over fts")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    corpus = load_jsonl(args.corpus)
    goldset = load_jsonl(args.goldset)
    expected_ids = {case["expected_memory_id"] for case in goldset}
    corpus_ids = {record["memory_id"] for record in corpus}
    missing = expected_ids - corpus_ids
    if missing:
        print(f"goldset references unknown corpus ids: {sorted(missing)}", file=sys.stderr)
        return 2

    model = args.embedding_model
    if model is None:
        candidates = [
            Path("/var/lib/bianbu-agent-memory/models/Qwen3-Embedding-0.6B-Q8_0.gguf"),
            Path.home() / ".cache/models/embedding/Qwen3-Embedding-0.6B-Q8_0.gguf",
        ]
        model = next((path for path in candidates if path.exists()), candidates[0])
    if not model.exists():
        print(f"embedding model not found: {model}", file=sys.stderr)
        return 2

    report: dict[str, Any] = {
        "status": "K3_EMBEDDING_QUALITY_EVAL",
        "spec": "MEM-NFR-02",
        "corpus_records": len(corpus),
        "goldset_cases": len(goldset),
        "embedding_model": str(model),
        "model_id": MODEL_ID,
        "top_k": TOP_K,
        "instruct_prefix": args.instruct,
    }

    llama = LlamaServer(args.llama_server, model, args.embedding_port, args.threads)
    llama.start()
    try:
        with tempfile.TemporaryDirectory(prefix="agent-memory-quality-") as raw:
            daemon = Daemon(args.memoryd, Path(raw))
            socket_path = daemon.start()
            try:
                base_url = f"http://127.0.0.1:{args.embedding_port}/v1"
                with MemoryClient.connect(socket_path, timeout=60.0) as client:
                    client._call(
                        "privacy.settings.update",
                        {"max_database_bytes": 256 * 1024 * 1024, "min_free_bytes": 0},
                    )
                    client.privacy.update_settings(embedding=embedding_patch(
                        base_url, True, args.instruct
                    ))
                print(f"seeding {len(corpus)} records via real model ...", file=sys.stderr)
                mapping, seed_report = seed(socket_path, corpus)
                report["seed"] = seed_report
                if report["seed"]["memory_embeddings"] != len(corpus):
                    print(
                        f"warning: embedded {report['seed']['memory_embeddings']} "
                        f"of {len(corpus)} records",
                        file=sys.stderr,
                    )

                for mode in MODES:
                    with MemoryClient.connect(socket_path, timeout=60.0) as client:
                        if mode == "instruct":
                            patch = embedding_patch(base_url, True, args.instruct)
                        elif mode == "noinstruct":
                            patch = embedding_patch(base_url, True, "")
                        else:  # fts
                            patch = embedding_patch(base_url, False, args.instruct)
                        client.privacy.update_settings(embedding=patch)
                    report[mode] = run_mode(socket_path, goldset, mapping, mode)
                    print(
                        f"{mode}: recall@5={report[mode]['overall']['recall_at_5']} "
                        f"mrr={report[mode]['overall']['mrr']}",
                        file=sys.stderr,
                    )
            finally:
                daemon.stop()
    finally:
        llama.stop()

    instruct_r5 = report["instruct"]["overall"]["recall_at_5"]
    noinstruct_r5 = report["noinstruct"]["overall"]["recall_at_5"]
    fts_r5 = report["fts"]["overall"]["recall_at_5"]
    report["semantic_gain"] = round(instruct_r5 - fts_r5, 4)
    report["prefix_delta"] = round(instruct_r5 - noinstruct_r5, 4)
    report["gain_gate_pass"] = instruct_r5 >= fts_r5 + args.gain_gate
    report["prefix_gate_pass"] = instruct_r5 >= noinstruct_r5
    report["overall_pass"] = report["gain_gate_pass"] and report["prefix_gate_pass"]

    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload)
    print(
        f"\n== MEM-NFR-02 quality gates ==\n"
        f"recall@5 instruct:    {instruct_r5}\n"
        f"recall@5 noinstruct:  {noinstruct_r5}  (prefix delta {report['prefix_delta']:+.4f} "
        f"-> {'PASS' if report['prefix_gate_pass'] else 'FAIL'})\n"
        f"recall@5 fts:         {fts_r5}  (semantic gain {report['semantic_gain']:+.4f}, "
        f"gate >= +{args.gain_gate:g} -> {'PASS' if report['gain_gate_pass'] else 'FAIL'})\n"
        f"overall: {'PASS' if report['overall_pass'] else 'FAIL'}",
        file=sys.stderr,
    )
    return 0 if report["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
