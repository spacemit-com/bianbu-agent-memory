#!/usr/bin/env python3
"""MEM-NFR-02 acceptance benchmark: 100k vectors, cosine retrieval, 8 clients.

Runs against a throwaway daemon + database (same isolation pattern as
``k3_perf_acceptance.py``) so nothing touches production state.

Methodology (see docs/K3_EMBEDDING_BENCHMARK.md and the MEM-NFR-02 plan):

* Seed phase drives the daemon's real inline embedding path, but against a
  local fake embedding server that answers instantly with deterministic
  SHA-256 vectors. Seeding 100k memories through the real Qwen3 model would
  take ~1 hour (12.5 items/s); scan cost is independent of vector content,
  so the perf numbers stay valid while seed time drops to minutes.
* The embedding ``model`` identifier stays constant across the whole run
  (``bench-embed``); only ``base_url`` is hot-swapped to the real
  llama-server after seeding. The scan filters on ``e.model = config.model``,
  so changing the identifier would mark every vector stale.
* Hot phase: 8 concurrent clients run paraphrase queries whose embeddings hit
  the real Qwen3 llama-server, exercising embed + 100k-row cosine scan + RRF
  fusion + authoritative re-read under load.
* Cold phase restarts the daemon on the same database (optionally dropping
  the page cache with root) and reruns the workload immediately.
* FTS baseline reruns the same queries with embedding disabled.
* Fallback phase kills llama-server and asserts queries still succeed via
  the silent FTS degradation path (zero errors).

Usage (K3, from the repository root after ``cargo build --release``):

    python3 scripts/k3_vector_perf_acceptance.py \
        --memoryd target/release/bianbu-agent-memoryd \
        --records 100000 --clients 8 --ops 400

Smoke run: ``--records 2000 --clients 4 --ops 20``.
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import math
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from bianbu_agent_sdk.memory import MemoryClient, MemoryKind, SyncPolicy

TOOLS = ("终端", "浏览器", "编辑器", "输入法", "主题", "字体", "下载目录", "通知")
VALUES = ("深色", "浅色", "system", "等宽字体", "无衬线", "垂直标签", "自动", "关闭")
PROJECTS = ("memory", "runtime", "bibit", "adapter", "gateway", "settings", "backup")
# "密钥管理" would trip looks_like_credential ("密钥") — the daemon then
# classifies those rows secret and never embeds them (by design), which broke
# the exact seed-coverage gate (first probe: 90137/100000). Non-credential
# module wording keeps the corpus 100% embeddable.
MODULES = ("存储层", "检索层", "备份链路", "会话层", "配置中心", "审计链路")
ACTIONS = ("构建命令", "测试命令", "部署步骤", "回滚步骤", "代码评审", "发布流程")

TEMPLATES = (
    "用户偏好：{tool}使用{value}，已在 {n} 号设备验证",
    "项目 {project} 的{action}是 make {project}-{n}，入口在 crates 目录",
    "事实：{module}由 {n} 号负责人维护，评审周期为每周",
    "决策：{module}采用 {value} 方案，替代了旧的第 {n} 版实现",
    "参考：{project} 文档索引保存在 docs-{n}，包含 {module} 说明",
)

# Paraphrase-style queries: none of these share literal tokens with half the
# corpus, which is exactly the workload the vector branch exists for.
QUERIES = (
    "夜间模式怎么设置",
    "怎么切换深色主题",
    "编辑器字体有什么偏好",
    "如何管理 Python 依赖",
    "浏览器默认下载位置在哪",
    "项目构建命令是什么",
    "部署流程谁负责",
    "密钥轮换周期多长",
    "回滚步骤记在哪里",
    "文档索引怎么查",
    "配色方案用哪套",
    "评审周期是多久",
    "输入法切换快捷键",
    "通知提醒怎么关",
    "网关配置入口",
    "备份链路在哪条路径",
    "会话摘要怎么生成",
    "存储层用什么引擎",
    "发布流程有几个阶段",
    "测试命令怎么跑",
)

MODEL_ID = "bench-embed"
EMBED_DIMS = 1024
DEFAULT_DB_LIMIT_BYTES = 2 * 1024 * 1024 * 1024


def percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def describe(samples: list[float]) -> dict[str, float | int]:
    if not samples:
        return {"count": 0}
    return {
        "count": len(samples),
        "p50_ms": round(percentile(samples, 0.50), 3),
        "p95_ms": round(percentile(samples, 0.95), 3),
        "p99_ms": round(percentile(samples, 0.99), 3),
        "max_ms": round(max(samples), 3),
    }


def make_text(index: int, rng: random.Random) -> str:
    template = TEMPLATES[index % len(TEMPLATES)]
    return template.format(
        tool=rng.choice(TOOLS), value=rng.choice(VALUES), project=rng.choice(PROJECTS),
        module=rng.choice(MODULES), action=rng.choice(ACTIONS), n=rng.randrange(1, 9999),
    )


def deterministic_vector(text: str, dims: int) -> list[float]:
    """Deterministic unit vector from a SHA-256 chain; same text, same vector."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    values: list[float] = []
    while len(values) < dims:
        values.extend((byte / 127.5) - 1.0 for byte in digest)
        digest = hashlib.sha256(digest).digest()
    values = values[:dims]
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


class FakeEmbeddingServer:
    """Instant OpenAI-format /embeddings responder for the seed phase."""

    def __init__(self, dims: int) -> None:
        self.dims = dims
        self.requests = 0
        self._lock = threading.Lock()
        handler = self._handler

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                handler(self)

            def log_message(self, *args: object) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _handler(self, request: http.server.BaseHTTPRequestHandler) -> None:
        length = int(request.headers.get("Content-Length") or 0)
        body = json.loads(request.rfile.read(length) or b"{}")
        texts = body.get("input") or []
        if isinstance(texts, str):
            texts = [texts]
        data = [
            {
                "object": "embedding",
                "index": index,
                "embedding": deterministic_vector(str(text), self.dims),
            }
            for index, text in enumerate(texts)
        ]
        payload = json.dumps(
            {"object": "list", "data": data, "model": body.get("model") or MODEL_ID,
             "usage": {"prompt_tokens": 0, "total_tokens": 0}}
        ).encode("utf-8")
        request.send_response(200)
        request.send_header("Content-Type", "application/json")
        request.send_header("Content-Length", str(len(payload)))
        request.end_headers()
        request.wfile.write(payload)
        with self._lock:
            self.requests += 1

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class LlamaServer:
    """Real Qwen3 embedding inference, managed as a child process."""

    def __init__(
        self, binary: Path, model: Path, port: int, threads: int, parallel: int = 1
    ) -> None:
        self.port = port
        self.process: subprocess.Popen[str] | None = None
        self.command = [
            str(binary),
            "--model", str(model),
            "--embedding", "--pooling", "last",
            "--host", "127.0.0.1", "--port", str(port),
            "--ctx-size", "2048",
            "--threads", str(threads),
            "--parallel", str(parallel),
            "--no-webui",
        ]

    def start(self, timeout_s: float = 240.0) -> None:
        self.process = subprocess.Popen(
            self.command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True
        )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"llama-server exited with {self.process.returncode}")
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.connect(("127.0.0.1", self.port))
            except OSError:
                time.sleep(1.0)
            else:
                probe.close()
                # Port open is not enough; wait for the model to answer.
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/health", timeout=5
                    ) as response:
                        if response.status == 200:
                            return
                except OSError:
                    pass
            finally:
                probe.close()
        raise TimeoutError("llama-server did not become healthy")

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)

    def embed_latency(self, rounds: int, concurrency: int, instruct: str) -> dict[str, Any]:
        """Direct /v1/embeddings latency, measured to isolate scan cost later."""
        samples: list[float] = []
        errors: list[str] = []
        lock = threading.Lock()

        def worker(offset: int) -> None:
            local: list[float] = []
            for index in range(offset, rounds, concurrency):
                query = QUERIES[index % len(QUERIES)]
                body = json.dumps(
                    {"model": MODEL_ID, "input": [instruct + query]}
                ).encode("utf-8")
                request = urllib.request.Request(
                    f"http://127.0.0.1:{self.port}/v1/embeddings",
                    data=body, headers={"Content-Type": "application/json"},
                )
                started = time.perf_counter_ns()
                try:
                    with urllib.request.urlopen(request, timeout=30) as response:
                        response.read()
                    local.append((time.perf_counter_ns() - started) / 1_000_000)
                except OSError as error:
                    with lock:
                        errors.append(str(error))
            with lock:
                samples.extend(local)

        threads = [
            threading.Thread(target=worker, args=(index,)) for index in range(concurrency)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return {"direct_embed": describe(samples), "errors": errors[:5]}


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
        self._wait_for_socket(socket_path)
        return socket_path

    def _wait_for_socket(self, path: Path) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError(self.process.stderr.read() if self.process.stderr else "memoryd exited")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(path))
                return
            except OSError:
                time.sleep(0.05)
            finally:
                probe.close()
        raise TimeoutError(path)

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()


def embedding_patch(base_url: str, enabled: bool) -> dict[str, Any]:
    """Full embedding config for privacy.settings.update.

    The daemon replaces the whole embedding section on update (serde
    defaults fill missing fields), so partial dicts would silently reset
    enabled/model. Always send the complete value.
    """
    return {
        "enabled": enabled,
        "base_url": base_url,
        "model": MODEL_ID,
        "dims": EMBED_DIMS,
    }


def configure(
    client: MemoryClient, base_url: str, enabled: bool, db_limit_bytes: int
) -> None:
    client._call(
        "privacy.settings.update",
        {"max_database_bytes": db_limit_bytes, "min_free_bytes": 0},
    )
    client.privacy.update_settings(embedding=embedding_patch(base_url, enabled))


def seed(socket_path: Path, records: int) -> dict[str, Any]:
    rng = random.Random(20260908)
    kinds = [MemoryKind.PREFERENCE, MemoryKind.FACT, MemoryKind.PROJECT, MemoryKind.DECISION, MemoryKind.REFERENCE]
    write_samples: list[float] = []
    started = time.monotonic()
    with MemoryClient.connect(socket_path, timeout=30.0) as client:
        for index in range(records):
            text = make_text(index, rng)
            before = time.perf_counter_ns()
            client.long_term.put(
                kind=kinds[index % len(kinds)],
                text=text,
                tags=("vector-seed",),
                sync_policy=SyncPolicy.LOCAL_ONLY,
                idempotency_key=f"vec:seed:{index}",
            )
            if index % 10 == 0:
                write_samples.append((time.perf_counter_ns() - before) / 1_000_000)
            if (index + 1) % 10_000 == 0:
                elapsed = time.monotonic() - started
                print(f"seed {index + 1}/{records} elapsed {elapsed:.0f}s", file=sys.stderr)
        embedded = client.stats().get("memory_embeddings", 0)
    return {
        "records": records,
        "seed_elapsed_s": round(time.monotonic() - started, 1),
        "write_ack_with_inline_embed": describe(write_samples),
        "memory_embeddings": embedded,
    }


def run_hot(
    socket_path: Path, clients: int, ops: int, top_k: int, queries: list[str]
) -> dict[str, Any]:
    search_samples: list[float] = []
    get_many_samples: list[float] = []
    total_samples: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(clients)

    def worker(client_index: int) -> None:
        try:
            client = MemoryClient.connect(socket_path, timeout=30.0)
        except Exception as error:  # noqa: BLE001 - recorded into the report
            with lock:
                errors.append(f"connect:{client_index}:{error}")
            barrier.abort()
            return
        local_search: list[float] = []
        local_get: list[float] = []
        local_total: list[float] = []
        try:
            client.health()
            barrier.wait()
            for op in range(ops):
                query = queries[(client_index * ops + op) % len(queries)]
                t0 = time.perf_counter_ns()
                headers = client.long_term.search_headers(query, top_k=top_k)
                t1 = time.perf_counter_ns()
                if headers:
                    client.long_term.get_many([h.memory_id for h in headers])
                t2 = time.perf_counter_ns()
                local_search.append((t1 - t0) / 1_000_000)
                local_get.append((t2 - t1) / 1_000_000)
                local_total.append((t2 - t0) / 1_000_000)
        except Exception as error:  # noqa: BLE001
            with lock:
                errors.append(f"client{client_index}:{error}")
        finally:
            client.close()
            with lock:
                search_samples.extend(local_search)
                get_many_samples.extend(local_get)
                total_samples.extend(local_total)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(clients)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return {
        "clients": clients,
        "ops_per_client": ops,
        "top_k": top_k,
        "elapsed_s": round(time.monotonic() - started, 1),
        "search_headers": describe(search_samples),
        "get_many": describe(get_many_samples),
        "search_plus_read": describe(total_samples),
        "errors": errors[:10],
        "error_count": len(errors),
    }


def build_queries(count: int) -> list[str]:
    rng = random.Random(20260908)
    queries: list[str] = []
    while len(queries) < count:
        queries.append(QUERIES[len(queries) % len(QUERIES)])
        if len(queries) < count:
            queries.append(f"怎么调整{rng.choice(TOOLS)}的{rng.choice(VALUES)}设置")
    return queries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--memoryd", default="target/release/bianbu-agent-memoryd", type=Path)
    parser.add_argument("--records", default=100_000, type=int)
    parser.add_argument("--clients", default=8, type=int)
    parser.add_argument("--ops", default=400, type=int)
    parser.add_argument("--top-k", default=10, type=int)
    parser.add_argument("--gate-ms", default=2000.0, type=float, help="P95 gate from MEM-NFR-02")
    parser.add_argument("--db-limit-bytes", default=DEFAULT_DB_LIMIT_BYTES, type=int,
                        help="raised max_database_bytes for the throwaway database")
    parser.add_argument("--llama-server", default=Path("/usr/bin/llama-server"), type=Path)
    # Outside the spacemit-ai-gateway Embed pool (18800-18900): this harness
    # starts its own llama-server to control the parameters it measures.
    parser.add_argument("--embedding-port", default=18901, type=int)
    parser.add_argument("--threads", default=8, type=int)
    parser.add_argument("--parallel", default=1, type=int,
                        help="llama-server slots; 1 keeps the full ctx-size per "
                             "request (parallel>1 splits it) and matches the "
                             "deployed embedding unit")
    parser.add_argument("--embedding-model", type=Path, default=None,
                        help="gguf path; default: first match under the packaged "
                             "models dir and ~/.cache/models/embedding/")
    parser.add_argument("--instruct", default="Instruct: Given a user query about a person's memories, retrieve the stored memory records that are semantically relevant to the query\nQuery: ")
    parser.add_argument("--skip-cold", action="store_true")
    parser.add_argument("--drop-caches", action="store_true",
                        help="drop OS page cache before the cold phase (root)")
    parser.add_argument("--skip-real", action="store_true",
                        help="skip the real llama-server phases (smoke only)")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    model = args.embedding_model
    if model is None:
        candidates = [
            Path("/var/lib/bianbu-agent-memory/models/Qwen3-Embedding-0.6B-Q8_0.gguf"),
            Path.home() / ".cache/models/embedding/Qwen3-Embedding-0.6B-Q8_0.gguf",
        ]
        model = next((path for path in candidates if path.exists()), candidates[0])
    if not args.skip_real and not model.exists():
        print(f"embedding model not found: {model}", file=sys.stderr)
        return 2

    queries = build_queries(max(64, args.clients * args.ops))
    report: dict[str, Any] = {
        "status": "K3_VECTOR_PERF_ACCEPTANCE",
        "spec": "MEM-NFR-02",
        "gate_ms_p95": args.gate_ms,
        "records": args.records,
        "embedding_model": str(model),
        "embedding_dims": EMBED_DIMS,
        "model_id": MODEL_ID,
    }

    def flush_report() -> None:
        # Persist after every phase: a multi-hour run must not lose the
        # completed phases if a later one is interrupted.
        if args.output:
            args.output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    fake = FakeEmbeddingServer(EMBED_DIMS)
    llama = None if args.skip_real else LlamaServer(
        args.llama_server, model, args.embedding_port, args.threads, args.parallel
    )
    try:
        if llama is not None:
            print(f"starting llama-server on :{args.embedding_port} ...", file=sys.stderr)
            llama.start()
        with tempfile.TemporaryDirectory(prefix="agent-memory-vector-") as raw:
            daemon = Daemon(args.memoryd, Path(raw))
            socket_path = daemon.start()
            try:
                with MemoryClient.connect(socket_path, timeout=30.0) as client:
                    configure(client, f"http://127.0.0.1:{fake.port}", True,
                              args.db_limit_bytes)
                print(f"seeding {args.records} records via inline embed ...",
                      file=sys.stderr)
                report["seed"] = seed(socket_path, args.records)
                flush_report()

                if llama is not None:
                    # Hot-swap only the endpoint; the model identifier must
                    # stay stable or every stored vector turns stale.
                    with MemoryClient.connect(socket_path, timeout=30.0) as client:
                        client.privacy.update_settings(embedding=embedding_patch(
                            f"http://127.0.0.1:{args.embedding_port}/v1", True
                        ))
                    report["query_embed"] = llama.embed_latency(
                        rounds=200, concurrency=args.clients, instruct=args.instruct
                    )
                    flush_report()
                    report["hot"] = run_hot(
                        socket_path, args.clients, args.ops, args.top_k, queries
                    )
                    flush_report()
                    if not args.skip_cold:
                        daemon.stop()
                        if args.drop_caches:
                            # Root drops directly; an unprivileged run falls
                            # back to the passwordless sudoers helper
                            # /usr/local/bin/drop-page-caches. The benchmark
                            # itself must stay on the NPU-owner uid —
                            # llama-server cannot run as root (K3 NPU
                            # arbiter is single-uid, see
                            # docs/K3_EMBEDDING_BENCHMARK.md §7.5).
                            drop = subprocess.run(
                                ["sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches"],
                                check=False,
                            )
                            if drop.returncode != 0:
                                subprocess.run(
                                    ["sudo", "-n", "/usr/local/bin/drop-page-caches"],
                                    check=False,
                                )
                        socket_path = daemon.start()
                        report["cold"] = run_hot(
                            socket_path, args.clients, args.ops, args.top_k, queries
                        )
                        flush_report()
                    with MemoryClient.connect(socket_path, timeout=30.0) as client:
                        client.privacy.update_settings(embedding=embedding_patch(
                            f"http://127.0.0.1:{args.embedding_port}/v1", False
                        ))
                    report["fts_baseline"] = run_hot(
                        socket_path, args.clients, min(args.ops, 100), args.top_k, queries
                    )
                    flush_report()

                    # Fallback: embedding re-enabled but the inference process
                    # is gone. Every query must still succeed through FTS
                    # (silent degradation, MEM-NFR-02).
                    llama.stop()
                    with MemoryClient.connect(socket_path, timeout=30.0) as client:
                        client.privacy.update_settings(embedding=embedding_patch(
                            f"http://127.0.0.1:{args.embedding_port}/v1", True
                        ))
                    report["fallback"] = run_hot(
                        socket_path, args.clients, 50, args.top_k, queries
                    )
            finally:
                daemon.stop()
    finally:
        fake.stop()
        if llama is not None:
            llama.stop()

    def gate(phase: dict[str, Any]) -> bool:
        return phase["search_headers"]["p95_ms"] <= args.gate_ms and phase["error_count"] == 0

    if "hot" in report:
        report["hot_pass"] = gate(report["hot"])
        if "cold" in report:
            report["cold_pass"] = gate(report["cold"])
        report["fallback_pass"] = (
            report["fallback"]["error_count"] == 0
            and report["fallback"]["search_headers"]["count"] > 0
        )
        report["seed_coverage_pass"] = (
            report["seed"]["memory_embeddings"] == report["seed"]["records"]
        )
        report["overall_pass"] = (
            report["hot_pass"]
            and report.get("cold_pass", True)
            and report["fallback_pass"]
            and report["seed_coverage_pass"]
        )
    else:
        report["seed_coverage_pass"] = (
            report["seed"]["memory_embeddings"] == report["seed"]["records"]
        )
        report["overall_pass"] = report["seed_coverage_pass"]

    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload)
    print(
        f"\n== MEM-NFR-02 gate (P95 <= {args.gate_ms:g} ms) ==\n"
        f"hot   search P95: {report.get('hot', {}).get('search_headers', {}).get('p95_ms', 'n/a')} ms\n"
        f"cold  search P95: {report.get('cold', {}).get('search_headers', {}).get('p95_ms', 'n/a')} ms\n"
        f"fts   search P95: {report.get('fts_baseline', {}).get('search_headers', {}).get('p95_ms', 'n/a')} ms (baseline)\n"
        f"fallback errors: {report.get('fallback', {}).get('error_count', 'n/a')}\n"
        f"embed coverage:  {report['seed'].get('memory_embeddings')}/{report['seed'].get('records')}\n"
        f"overall: {'PASS' if report['overall_pass'] else 'FAIL'}",
        file=sys.stderr,
    )
    return 0 if report["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
