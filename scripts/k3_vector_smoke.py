#!/usr/bin/env python3
"""M2/M3 K3 冒烟：内联嵌入、语义召回、forget 清理、FTS 静默回退。

在 K3 上对 release 二进制做 30 秒级端到端验证，跑 10 万条压测
（``k3_vector_perf_acceptance.py``）之前先确认整条部署链路活着：
迁移 0011 建表、put 内联嵌入、search_headers 向量分支 + RRF、
forget 删除派生向量、embedding 服务不可达时静默降级回 FTS。

存储↔查询对按 trigram 分词刻意零重叠：语义命中只能来自向量分支；
禁用 embedding 后同查询必然落空，构成 A/B 对照。

用法（K3，仓库根目录，需 release 构建完成、spacemit-ai-gateway 的
Embed 域已 provision 出 qwen3-embedding-0.6b-q8_0）：

    PYTHONPATH=sdk/python/src python3 scripts/k3_vector_smoke.py \
        --memoryd target/release/bianbu-agent-memoryd
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from bianbu_agent_sdk.memory import MemoryClient, MemoryKind, SyncPolicy

# (存储文本, 改写查询) —— trigram 层面互不重叠，FTS 必然检索不到。
PAIRS = [
    ("终端使用深色主题", "怎么让屏幕晚上不那么刺眼"),
    ("项目构建常用 cargo build --release", "怎么把源码编译成可执行程序"),
    ("海鲜过敏，点菜要避开虾和蟹", "聚餐有什么饮食禁忌"),
]
EMBED_DIMS = 1024
# The Gateway's model id, stored verbatim in every vector row and compared to
# decide whether a stored vector is stale.
MODEL_ID = "qwen3-embedding-0.6b-q8_0"


def embedding_patch(base_url: str, enabled: bool) -> dict[str, Any]:
    """整段 embedding 配置（daemon 是整结构替换，局部字典会重置其余字段）。"""
    return {
        "enabled": enabled,
        "base_url": base_url,
        "model": MODEL_ID,
        "dims": EMBED_DIMS,
    }


def wait_for_socket(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                process.stderr.read() if process.stderr else "memoryd exited"
            )
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(path))
            return
        except OSError:
            time.sleep(0.05)
        finally:
            probe.close()
    raise TimeoutError(path)


def wait_embeddings(client: MemoryClient, expected: int, timeout_s: float = 10.0) -> int:
    deadline = time.monotonic() + timeout_s
    value = client.stats().get("memory_embeddings", -1)
    while value != expected and time.monotonic() < deadline:
        time.sleep(0.2)
        value = client.stats().get("memory_embeddings", -1)
    return value


def run(memoryd: Path, embedding_url: str, dead_url: str) -> tuple[dict[str, Any], bool]:
    gates: dict[str, bool] = {}
    report: dict[str, Any] = {"embedding_url": embedding_url, "gates": gates}
    confirm_token = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="agent-memory-smoke-") as raw:
        root = Path(raw)
        secret = root / "account-secret"
        secret.write_text(uuid.uuid4().hex + uuid.uuid4().hex + "\n", encoding="utf-8")
        secret.chmod(0o600)
        socket_path = root / "memoryd.sock"
        environment = dict(os.environ)
        environment["BIANBU_MEMORY_DEV_CONFIRM_TOKEN"] = confirm_token
        process = subprocess.Popen(
            [
                str(memoryd),
                "--development-identity",
                "--socket",
                str(socket_path),
                "--database",
                str(root / "memory.db"),
                "--key-package",
                str(root / "memory.keypkg"),
                "--account-secret-file",
                str(secret),
                "--initialize-key-package",
                "--background-device-id",
                "k3-smoke",
            ],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            wait_for_socket(socket_path, process)
            with MemoryClient.connect(socket_path, timeout=30.0) as client:
                client.health()
                client._call(
                    "privacy.settings.update",
                    {"max_database_bytes": 256 * 1024 * 1024, "min_free_bytes": 0},
                )
                baseline = client.stats().get("memory_embeddings", 0)

                # --- M2: 启用 embedding，put 内联嵌入 ---
                client.privacy.update_settings(
                    embedding=embedding_patch(embedding_url, True)
                )
                receipts = []
                for index, (text, _) in enumerate(PAIRS):
                    receipts.append(
                        client.long_term.put(
                            kind=MemoryKind.PREFERENCE,
                            text=text,
                            tags=("vector-smoke",),
                            sync_policy=SyncPolicy.LOCAL_ONLY,
                            idempotency_key=f"smoke:{index}",
                        )
                    )
                embedded = wait_embeddings(client, baseline + len(PAIRS))
                gates["inline_embed_writes_vectors"] = embedded == baseline + len(PAIRS)
                report["memory_embeddings_after_put"] = embedded

                # --- M3: 改写查询语义命中（trigram 零重叠，FTS 不可能命中） ---
                semantic_hits = []
                for receipt, (_, query) in zip(receipts, PAIRS):
                    started = time.perf_counter()
                    headers = client.long_term.search_headers(query, top_k=5)
                    elapsed = (time.perf_counter() - started) * 1000
                    hit = any(h.memory_id == receipt.memory_id for h in headers)
                    semantic_hits.append(
                        {
                            "query": query,
                            "hit": hit,
                            "rank": next(
                                (
                                    index
                                    for index, h in enumerate(headers, 1)
                                    if h.memory_id == receipt.memory_id
                                ),
                                None,
                            ),
                            "results": len(headers),
                            "search_ms": round(elapsed, 1),
                        }
                    )
                gates["semantic_recall"] = all(item["hit"] for item in semantic_hits)
                report["semantic_search"] = semantic_hits

                # --- 对照：禁用 embedding 后同查询落空（证明命中来自向量分支） ---
                client.privacy.update_settings(
                    embedding=embedding_patch(embedding_url, False)
                )
                contrast_hits = []
                for receipt, (_, query) in zip(receipts, PAIRS):
                    headers = client.long_term.search_headers(query, top_k=5)
                    contrast_hits.append(
                        any(h.memory_id == receipt.memory_id for h in headers)
                    )
                gates["fts_contrast_no_false_positive"] = not any(contrast_hits)
                report["fts_contrast_hits"] = contrast_hits

                # --- M2: forget 删除派生向量 ---
                client.privacy.update_settings(
                    embedding=embedding_patch(embedding_url, True)
                )
                job = client.long_term.forget(
                    receipts[2].memory_id, confirm_token=confirm_token
                )
                client.long_term.wait_forget(job.job_id)
                after_forget = wait_embeddings(client, baseline + len(PAIRS) - 1)
                gates["forget_cleans_vectors"] = (
                    after_forget == baseline + len(PAIRS) - 1
                )
                report["memory_embeddings_after_forget"] = after_forget
                headers = client.long_term.search_headers(PAIRS[2][1], top_k=5)
                gates["forgotten_not_recalled"] = not any(
                    h.memory_id == receipts[2].memory_id for h in headers
                )

                # --- M3 降级：embedding 服务不可达 → 静默回退 FTS，零错误 ---
                client.privacy.update_settings(
                    embedding=embedding_patch(dead_url, True)
                )
                started = time.perf_counter()
                headers = client.long_term.search_headers("深色主题", top_k=5)
                elapsed = (time.perf_counter() - started) * 1000
                fallback_hit = any(
                    h.memory_id == receipts[0].memory_id for h in headers
                )
                # "深色主题" 与存储文本 trigram 重叠，FTS 本身可命中；
                # 静默降级的硬门控是"不抛错"，命中是 FTS 正常工作的佐证。
                gates["silent_fallback_no_error"] = True
                report["fallback_search"] = {
                    "query": "深色主题",
                    "fts_hit": fallback_hit,
                    "search_ms": round(elapsed, 1),
                }
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
    return report, all(gates.values())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--memoryd", default="target/release/bianbu-agent-memoryd", type=Path
    )
    parser.add_argument("--embedding-url", default="http://127.0.0.1:18790/v1/embed")
    parser.add_argument("--dead-url", default="http://127.0.0.1:18999/v1")
    parser.add_argument("--json", type=Path, help="额外把报告写入该 JSON 文件")
    arguments = parser.parse_args()
    memoryd = arguments.memoryd.resolve()
    if not memoryd.is_file():
        raise FileNotFoundError(memoryd)
    report, passed = run(memoryd, arguments.embedding_url, arguments.dead_url)
    report["passed"] = passed
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if arguments.json:
        arguments.json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print("K3_VECTOR_SMOKE_" + ("PASS" if passed else "FAIL"))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
