#!/usr/bin/env python3
"""K3 现网安装验收：确认设备上正在运行的 memoryd 就是"经 gateway 做语义检索"的这一版。

与 ``k3_vector_smoke.py``、``k3_semantic_e2e_runtime.py`` 不同，本脚本不启动
任何测试实例，直接打设备上现网的守护进程（``/run/bianbu/agent-memoryd.sock``），
因此它回答的是"装到设备上的包是否真的按新配置在跑"：

阶段 A（只读）：把本进程注册成生产 Runtime 身份，读 *生效* 的产品配置与
``system.health``。只有这份 RPC 返回值代表真实运行配置：``memory.config.json`` 是
运维能直接看到的副本，0.1.17 起守护进程在启动时会把收敛结果写回它，但它仍可能被
外部改成别的值。``health.embedding`` 把两半都报出来 —— ``base_url``/``model`` 是生效
值，``config_file_matches`` 表示磁盘副本是否已与进程一致。

阶段 B（独立命名空间）：注册到一个专用探针 memory space（绝不碰用户 Runtime
空间），写入三条与改写查询 trigram 零重叠的记忆——FTS 不可能命中，命中只能来自
向量分支——等后台 embedding 任务补齐向量，校验召回，再用守护进程自己的 adapter
确认令牌把探针记录逐条 forget，确认派生向量被清理。令牌方案与
``bianbu_agent_sdk.memory.bibit_adapter._confirmation_token`` 一致：
``sha256(secret || "v1:<expiry_ms>:<nonce>:<method>")``，secret 取
``/etc/bianbu-agent-memory/adapter-confirmation.secret``。

读 secret 需要 root（或 ``bianbu-agent`` 组）；读不到时跳过 forget，探针记录靠
10 分钟 ``expires_at`` 自行过期。

用法（K3，仓库根目录）：

    echo bianbu | sudo -S python3 scripts/k3_production_install_verify.py
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import time
from pathlib import Path

from bianbu_agent_sdk.memory import MemoryClient, MemoryKind, SyncPolicy

# 设备上 0.1.17 的生效配置应当收敛到 gateway Embed 域与网关模型 id。
GATEWAY_BASE_URL = "http://127.0.0.1:18790/v1/embed"
MODEL_ID = "qwen3-embedding-0.6b-q8_0"

REGISTRY = Path("/run/bianbu/agent-runtime/memory-registry")
SOCKET = os.environ.get("MEMORY_SOCKET", "/run/bianbu/agent-memoryd.sock")
SECRET_PATH = Path("/etc/bianbu-agent-memory/adapter-confirmation.secret")
DEVICE_ID = os.environ.get("MEMORY_DEVICE_ID", "fe:fe:fe:4e:f8:26")
ACCOUNT_ID = os.environ.get("MEMORY_ACCOUNT_ID", "bianbu")
RUNTIME_AGENT = "com.bianbu.runtime"
PROBE_AGENT = "com.bianbu.gateway.probe"
CAPABILITIES = [
    "memory.instruction.read",
    "memory.instruction.write",
    "memory.short_term.read",
    "memory.short_term.write",
    "memory.long_term.read",
    "memory.long_term.write",
    "memory.forget",
    "memory.account.read",
    "memory.audit.status",
    "memory.audit.read",
    "memory.audit.write",
    "memory.privacy.read",
    "memory.privacy.write",
]
# (存储文本, 改写查询) —— trigram 层面互不重叠，FTS 必然检索不到。
PAIRS = [
    ("终端使用深色主题", "怎么让屏幕晚上不那么刺眼"),
    ("项目构建常用 cargo build --release", "怎么把源码编译成可执行程序"),
    ("海鲜过敏，点菜要避开虾和蟹", "聚餐有什么饮食禁忌"),
]


def start_time_ticks(pid: int) -> int:
    fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1]
    return int(fields.split()[19])


def register(agent_id: str, space: str) -> Path:
    """写一份属于本进程的 runtime registration（memoryd 只信任这种身份）。"""

    pid = os.getpid()
    start = start_time_ticks(pid)
    record = {
        "uid": os.getuid(),
        "pid_start_time_ticks": start,
        "account_id": ACCOUNT_ID,
        "device_id": DEVICE_ID,
        "agent_id": agent_id,
        "agent_name": agent_id,
        "agent_instance_id": f"gateway-verify.{pid}.{start}",
        "memory_space_id": space,
        "capabilities": CAPABILITIES,
        "expires_at_ms": int(time.time() * 1000) + 900_000,
        "allow_runtime_context": False,
    }
    path = REGISTRY / f"{pid}.json"
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    os.chmod(path, 0o640)
    return path


def connect_client(agent_id: str) -> MemoryClient:
    # system.hello 的 account_id/agent_name 必须与 registration 一致。
    return MemoryClient.connect(
        SOCKET, timeout=30.0, account_id=ACCOUNT_ID, agent_name=agent_id
    )


def confirmation_token(secret: bytes, method: str, ttl_ms: int = 120_000) -> str:
    expiry = int(time.time() * 1000) + ttl_ms
    nonce = secrets.token_urlsafe(24)
    payload = f"v1:{expiry}:{nonce}:{method}".encode("utf-8")
    signature = hashlib.sha256(secret + payload).hexdigest()
    return f"v1:{expiry}:{nonce}:{signature}"


def wait_embeddings(client: MemoryClient, expected: int, timeout_s: float = 90.0) -> int:
    deadline = time.monotonic() + timeout_s
    value = client.stats().get("memory_embeddings", 0)
    while value != expected and time.monotonic() < deadline:
        time.sleep(0.5)
        value = client.stats().get("memory_embeddings", 0)
    return value


def effective_config_phase(report: dict[str, object]) -> None:
    registration = register(RUNTIME_AGENT, f"{ACCOUNT_ID}.{RUNTIME_AGENT}.{DEVICE_ID}")
    try:
        with connect_client(RUNTIME_AGENT) as client:
            report["identity"] = {
                "account_id": client.identity.account_id,
                "agent_id": client.identity.agent_id,
                "memory_space_id": client.identity.memory_space_id,
            }
            report["health"] = client.health()
            settings = client.privacy.settings()
            report["effective_config_path"] = settings.get("config_path")
            report["effective_embedding"] = settings.get("embedding")
            report["runtime_space_stats"] = client.stats()
    finally:
        registration.unlink(missing_ok=True)


def probe_phase(report: dict[str, object]) -> None:
    secret = SECRET_PATH.read_bytes() if os.access(SECRET_PATH, os.R_OK) else None
    report["confirmation_secret_available"] = secret is not None
    registration = register(PROBE_AGENT, f"{ACCOUNT_ID}.{PROBE_AGENT}.{DEVICE_ID}")
    try:
        with connect_client(PROBE_AGENT) as client:
            baseline = client.stats().get("memory_embeddings", 0)
            stamp = int(time.time())
            receipts = [
                client.long_term.put(
                    kind=MemoryKind.PREFERENCE,
                    text=text,
                    tags=("gateway-probe",),
                    sync_policy=SyncPolicy.LOCAL_ONLY,
                    expires_at=int(time.time() * 1000) + 600_000,
                    idempotency_key=f"gateway-probe:{stamp}:{index}",
                )
                for index, (text, _) in enumerate(PAIRS)
            ]
            embedded = wait_embeddings(client, baseline + len(PAIRS))
            report["probe_space_baseline"] = baseline
            report["embeddings_after_put"] = embedded
            report["vector_created_per_memory"] = embedded == baseline + len(PAIRS)

            hits = []
            for receipt, (_, query) in zip(receipts, PAIRS):
                started = time.perf_counter()
                headers = client.long_term.search_headers(query, top_k=5)
                elapsed = (time.perf_counter() - started) * 1000
                hits.append(
                    {
                        "query": query,
                        "hit": any(h.memory_id == receipt.memory_id for h in headers),
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
            report["semantic_search"] = hits
            report["semantic_recall_all_hit"] = all(item["hit"] for item in hits)

            if secret is not None:
                for receipt in receipts:
                    job = client.long_term.forget(
                        receipt.memory_id,
                        confirm_token=confirmation_token(secret, "long_term.forget"),
                    )
                    client.long_term.wait_forget(job.job_id)
                after_forget = wait_embeddings(client, baseline)
                report["embeddings_after_forget"] = after_forget
                report["forget_prunes_vectors"] = after_forget == baseline
            report["probe_space_final"] = client.stats()
    finally:
        registration.unlink(missing_ok=True)


def main() -> int:
    report: dict[str, object] = {}
    try:
        effective_config_phase(report)
        probe_phase(report)
    except Exception as error:  # noqa: BLE001 - 验收脚本只报告，不隐藏
        report["error"] = f"{type(error).__name__}: {error}"

    embedding = report.get("effective_embedding") or {}
    health = report.get("health") or {}
    check = health.get("database_check") or {}
    gates = {
        "effective_endpoint_is_gateway": embedding.get("base_url") == GATEWAY_BASE_URL
        and embedding.get("model") == MODEL_ID,
        "vector_created_per_memory": report.get("vector_created_per_memory") is True,
        "semantic_recall_all_hit": report.get("semantic_recall_all_hit") is True,
        # 0.1.18：health 不再把 FTS5 inverted-index 那条判词当缺陷。写者占库
        # （state=busy）或该判词在案（state=complaint，原文见
        # database_check.complaint）时 status 必须还是 ok/unknown。
        "health_reports_no_phantom_degradation": health.get("status") in {"ok", "unknown"}
        and check.get("state") in {"checked", "cached", "busy", "complaint"},
        # 开发期编号 0.1.20 引入：health.embedding 必须报出生效值，并且磁盘上的 memory.config.json
        # 已经和进程一致（启动时写回收敛结果，见 docs/CONFIGURATION.md §5/§9）。
        "health_publishes_effective_embedding": (
            health.get("embedding") or {}
        ).get("base_url")
        == GATEWAY_BASE_URL
        and (health.get("embedding") or {}).get("model") == MODEL_ID
        and (health.get("embedding") or {}).get("config_file_matches") is True,
    }
    if report.get("confirmation_secret_available"):
        gates["forget_prunes_vectors"] = report.get("forget_prunes_vectors") is True
    report["gates"] = gates
    report["passed"] = "error" not in report and all(gates.values())

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("K3_PRODUCTION_VERIFY_PASS" if report["passed"] else "K3_PRODUCTION_VERIFY_FAIL")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
