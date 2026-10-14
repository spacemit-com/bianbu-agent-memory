#!/usr/bin/env python3
"""K3 on-device embedding model benchmark (MEM-NFR-02 evidence).

Starts the Spacemit llama-server for each candidate GGUF embedding model and
measures, per model:

  - model load time (process start -> /health ready) and resident memory (RSS)
  - single-query embedding latency P50/P95/max at several Chinese text lengths
    (the memory-search critical path; MEM-NFR-02 budget is 2s per query)
  - batch embedding throughput (the memory-indexing path)
  - concurrent query latency with 8 parallel clients
  - a small semantic-relevance sanity check (cosine similarity) so the report
    pairs latency with a minimal quality signal

Only the Python standard library is used. Output: JSON report + stdout table.

Usage:
  python3 k3_embedding_benchmark.py \
    --models qwen3-0.6b-q8=/path/Qwen3-Embedding-0.6B-Q8_0.gguf:last,bge-small-zh-q8=/path/bge-small-zh-v1.5-q8_0.gguf:cls \
    --output embedding-benchmark.json
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import threading
import time
import urllib.request

BASE_TEXT = (
    "用户偏好使用深色主题的终端界面，常用 Python 和 Rust 进行开发，"
    "习惯使用 uv 管理依赖，回答问题时偏好简洁的中文说明并附代码示例，"
    "项目构建常用 cargo build --release，编辑器是 VSCode，使用等宽字体，"
    "每周五下午会整理一次项目文档，测试习惯是先写回归用例再动手改代码。"
)

RELEVANCE_CHECKS = [
    (
        "如何让界面进入夜间模式？",
        "用户偏好：终端与编辑器使用深色主题配色",
        "项目构建命令是 cargo build --release",
    ),
    (
        "我平时用什么工具管理 Python 依赖？",
        "开发习惯：使用 uv 管理 Python 项目的依赖",
        "用户所在时区是 Asia/Shanghai",
    ),
]


def text_of(n_chars: int) -> str:
    return (BASE_TEXT * 16)[:n_chars]


def http_json(url: str, payload: dict | None = None, timeout: float = 180.0) -> dict:
    if payload is None:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.load(response)
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def embed(port: int, texts: list[str]) -> list[list[float]]:
    response = http_json(
        f"http://127.0.0.1:{port}/v1/embeddings", {"input": texts, "model": "bench"}
    )
    return [item["embedding"] for item in response["data"]]


def wait_health(port: int, deadline_s: float) -> bool:
    deadline = time.perf_counter() + deadline_s
    while time.perf_counter() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=2
            ) as response:
                if response.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def rss_mb(pid: int) -> float | None:
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    return None


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((p / 100) * (len(ordered) - 1))))
    return ordered[index]


def stats(values: list[float]) -> dict:
    return {
        "p50_ms": round(percentile(values, 50) * 1000, 1),
        "p95_ms": round(percentile(values, 95) * 1000, 1),
        "max_ms": round(max(values) * 1000, 1),
        "mean_ms": round(sum(values) / len(values) * 1000, 1),
        "n": len(values),
    }


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def bench_one(name: str, path: str, pooling: str, args: argparse.Namespace) -> dict:
    print(f"[{name}] starting llama-server ({path}, pooling={pooling}) ...", flush=True)
    log_path = f"/tmp/k3-embed-bench-{name}.log"
    log = open(log_path, "wb")
    started = time.perf_counter()
    proc = subprocess.Popen(
        [
            "llama-server",
            "-m", path,
            "--host", "127.0.0.1",
            "--port", str(args.port),
            "--embedding",
            "--pooling", pooling,
            "--ctx-size", str(args.ctx_size),
            "--threads", str(args.threads),
            "--no-webui",
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    result: dict = {"name": name, "model_path": path, "pooling": pooling}
    try:
        if not wait_health(args.port, 600):
            raise RuntimeError(f"llama-server not ready in 600s; see {log_path}")
        result["load_s"] = round(time.perf_counter() - started, 2)

        # Warm up and capture the embedding dimensionality.
        vectors = embed(args.port, [text_of(32)])
        result["dims"] = len(vectors[0])
        result["rss_after_load_mb"] = rss_mb(proc.pid)
        print(
            f"[{name}] ready: load={result['load_s']}s dims={result['dims']} "
            f"rss={result['rss_after_load_mb']}MB",
            flush=True,
        )

        # Single-query latency at several text lengths (search critical path).
        latency: dict[str, dict] = {}
        for n_chars in (32, 64, 128, 256):
            text = text_of(n_chars)
            for _ in range(3):
                embed(args.port, [text])
            times = []
            for _ in range(args.samples):
                t0 = time.perf_counter()
                embed(args.port, [text])
                times.append(time.perf_counter() - t0)
            latency[f"{n_chars}chars"] = stats(times)
            print(
                f"[{name}] latency@{n_chars}chars: "
                f"p50={latency[f'{n_chars}chars']['p50_ms']}ms "
                f"p95={latency[f'{n_chars}chars']['p95_ms']}ms",
                flush=True,
            )
        result["single_query_latency"] = latency

        # Batch throughput (indexing path): 64 distinct short texts per request.
        batches = []
        for _ in range(3):
            texts = [text_of(48) + f"记录编号{i}。" for i in range(64)]
            t0 = time.perf_counter()
            embed(args.port, texts)
            batches.append(time.perf_counter() - t0)
        result["batch_64"] = {
            **stats(batches),
            "texts_per_s": round(64 / (sum(batches) / len(batches)), 2),
        }
        print(
            f"[{name}] batch64: {result['batch_64']['texts_per_s']} texts/s "
            f"(p95={result['batch_64']['p95_ms']}ms)",
            flush=True,
        )

        # Concurrent queries: 8 workers x N requests.
        latencies: list[float] = []
        lock = threading.Lock()

        def worker() -> None:
            local: list[float] = []
            for _ in range(args.samples):
                t0 = time.perf_counter()
                embed(args.port, [text_of(64)])
                local.append(time.perf_counter() - t0)
            with lock:
                latencies.extend(local)

        threads = [threading.Thread(target=worker) for _ in range(args.workers)]
        t0 = time.perf_counter()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        result["concurrent"] = {
            **stats(latencies),
            "workers": args.workers,
            "wall_s": round(time.perf_counter() - t0, 2),
            "queries_per_s": round(len(latencies) / (time.perf_counter() - t0), 2),
        }
        print(
            f"[{name}] concurrent({args.workers}): "
            f"p50={result['concurrent']['p50_ms']}ms "
            f"p95={result['concurrent']['p95_ms']}ms "
            f"qps={result['concurrent']['queries_per_s']}",
            flush=True,
        )

        # Semantic relevance sanity check (minimal quality signal).
        checks = []
        for query, related, unrelated in RELEVANCE_CHECKS:
            q, r, u = embed(args.port, [query, related, unrelated])
            sim_related = cosine(q, r)
            sim_unrelated = cosine(q, u)
            checks.append(
                {
                    "query": query,
                    "sim_related": round(sim_related, 4),
                    "sim_unrelated": round(sim_unrelated, 4),
                    "correct": sim_related > sim_unrelated,
                }
            )
        result["relevance_checks"] = checks
        result["rss_end_mb"] = rss_mb(proc.pid)
        print(
            f"[{name}] relevance: "
            + "; ".join(
                f"{'OK' if c['correct'] else 'FAIL'} "
                f"({c['sim_related']} vs {c['sim_unrelated']})"
                for c in checks
            ),
            flush=True,
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--models",
        required=True,
        help="comma list of name=/path/model.gguf:pooling (pooling: last|cls|mean)",
    )
    parser.add_argument("--port", type=int, default=18890)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--ctx-size", type=int, default=2048)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--output", default="k3_embedding_benchmark.json")
    args = parser.parse_args()

    results = []
    for spec in args.models.split(","):
        name, rest = spec.split("=", 1)
        path, pooling = rest.rsplit(":", 1)
        try:
            results.append(bench_one(name.strip(), path.strip(), pooling.strip(), args))
        except Exception as error:  # keep benchmarking the remaining models
            results.append({"name": name, "error": str(error)})
            print(f"[{name}] FAILED: {error}", file=sys.stderr, flush=True)
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(results, handle, ensure_ascii=False, indent=2)

    print("\n=== SUMMARY ===")
    for result in results:
        if "error" in result:
            print(f"{result['name']}: ERROR {result['error']}")
            continue
        p95_64 = result["single_query_latency"]["64chars"]["p95_ms"]
        print(
            f"{result['name']}: dims={result['dims']} load={result['load_s']}s "
            f"rss={result['rss_end_mb']}MB "
            f"p95@64chars={p95_64}ms "
            f"batch={result['batch_64']['texts_per_s']}/s "
            f"conc_p95={result['concurrent']['p95_ms']}ms "
            f"relevance={'all OK' if all(c['correct'] for c in result['relevance_checks']) else 'CHECK FAILED'}"
        )
    print(f"report: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
