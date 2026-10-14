#!/usr/bin/env python3
"""K3 现场运维共用件：以指定 memory space 的身份注册、导出与清理。

设备上的 memoryd 不接受匿名请求：连接身份来自
``/run/bianbu/agent-runtime/memory-registry/<pid>.json`` 里由可信进程写下的注册
（``crates/memoryd/src/registry.rs`` 校验 peer uid/gid、进程启动时刻、注册文件
权限与 capability 白名单）。现场脚本若要读或清某个 ``(account_id, agent_id,
memory_space_id)`` 的数据，就必须先按同一机制注册，并且要以 root（或
``bianbu-agent`` 组）执行。

本模块只提供共用实现，不决定任何清理策略：

* :func:`runtime_registration`：为当前进程写一份临时注册，退出即删除；
* :func:`memory_session`：注册 + 连接现网守护进程的组合；
* :func:`confirmation_token`：按 adapter 的 ``v1:<expiry>:<nonce>:<sha256>`` 规则
  签发确认令牌，供 ``long_term.forget`` 等危险操作使用；
* :func:`declared_slots` / :func:`out_of_dictionary_slots`：读取一条记忆声明的
  ``slot:`` 标签并对照 L3 词典 —— 这是 MEM-L3-02 的判定口径。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import time
from collections.abc import Iterator
from pathlib import Path

from bianbu_agent_sdk.memory import MemoryClient
from bianbu_agent_sdk.memory.ontology import resolve_slot

REGISTRY = Path(
    os.environ.get("MEMORY_REGISTRY_DIR", "/run/bianbu/agent-runtime/memory-registry")
)
SOCKET = os.environ.get("MEMORY_SOCKET", "/run/bianbu/agent-memoryd.sock")
CONFIRMATION_SECRET = Path(
    os.environ.get(
        "MEMORY_CONFIRMATION_SECRET",
        "/etc/bianbu-agent-memory/adapter-confirmation.secret",
    )
)
DEVICE_ID = os.environ.get("MEMORY_DEVICE_ID", "fe:fe:fe:4e:f8:26")
ACCOUNT_ID = os.environ.get("MEMORY_ACCOUNT_ID", "bianbu")

# 只读身份：够看命名空间、导出数据、读审计；不带任何写能力。
READ_CAPABILITIES = (
    "memory.instruction.read",
    "memory.short_term.read",
    "memory.long_term.read",
    "memory.account.read",
    "memory.audit.status",
    "memory.audit.read",
    "memory.privacy.read",
)
# 清理身份：在只读之上加写入与 forget（``long_term.forget`` 需要 memory.forget）。
CLEANUP_CAPABILITIES = READ_CAPABILITIES + (
    "memory.instruction.write",
    "memory.short_term.write",
    "memory.long_term.write",
    "memory.forget",
    "memory.audit.write",
    "memory.privacy.write",
)


def process_start_time_ticks(pid: int) -> int:
    """读取 ``/proc/<pid>/stat`` 的 starttime，memoryd 用它绑定注册与进程。"""

    fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1]
    return int(fields.split()[19])


def registration_record(
    *,
    account_id: str,
    agent_id: str,
    space_id: str,
    capabilities: tuple[str, ...],
    device_id: str = DEVICE_ID,
    instance_prefix: str = "k3-admin",
    ttl_ms: int = 900_000,
) -> tuple[Path, dict[str, object]]:
    """为当前进程构造一份注册记录（不落盘）。"""

    pid = os.getpid()
    start = process_start_time_ticks(pid)
    record: dict[str, object] = {
        "uid": os.getuid(),
        "pid_start_time_ticks": start,
        "account_id": account_id,
        "device_id": device_id,
        "agent_id": agent_id,
        "agent_name": agent_id,
        "agent_instance_id": f"{instance_prefix}.{pid}.{start}",
        "memory_space_id": space_id,
        "capabilities": list(capabilities),
        "expires_at_ms": int(time.time() * 1000) + ttl_ms,
        # 本次会话的目标身份就是注册身份，不需要 runtime-context 再作用域一次。
        "allow_runtime_context": False,
    }
    return REGISTRY / f"{pid}.json", record


@contextlib.contextmanager
def runtime_registration(
    *,
    account_id: str,
    agent_id: str,
    space_id: str,
    capabilities: tuple[str, ...],
    device_id: str = DEVICE_ID,
    instance_prefix: str = "k3-admin",
) -> Iterator[Path]:
    """写一份属于本进程的注册；退出时删除，不给设备留垃圾。"""

    path, record = registration_record(
        account_id=account_id,
        agent_id=agent_id,
        space_id=space_id,
        capabilities=capabilities,
        device_id=device_id,
        instance_prefix=instance_prefix,
    )
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    os.chmod(path, 0o640)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


@contextlib.contextmanager
def memory_session(
    *,
    account_id: str,
    agent_id: str,
    space_id: str,
    capabilities: tuple[str, ...] = READ_CAPABILITIES,
    device_id: str = DEVICE_ID,
    timeout: float = 60.0,
    instance_prefix: str = "k3-admin",
) -> Iterator[MemoryClient]:
    """以 ``(account, agent, space)`` 身份连上现网守护进程。"""

    with runtime_registration(
        account_id=account_id,
        agent_id=agent_id,
        space_id=space_id,
        capabilities=capabilities,
        device_id=device_id,
        instance_prefix=instance_prefix,
    ):
        client = MemoryClient.connect(
            SOCKET, timeout=timeout, account_id=account_id, agent_name=agent_id
        )
        try:
            yield client
        finally:
            client.close()


def confirmation_secret() -> bytes | None:
    """读 adapter 确认令牌密钥；读不到就返回 None（调用方自己决定怎么报错）。"""

    try:
        return CONFIRMATION_SECRET.read_bytes()
    except OSError:
        return None


def confirmation_token(secret: bytes, method: str, ttl_ms: int = 120_000) -> str:
    """按 adapter 的规则签发一次确认令牌。"""

    expiry = int(time.time() * 1000) + ttl_ms
    nonce = secrets.token_urlsafe(24)
    payload = f"v1:{expiry}:{nonce}:{method}".encode()
    signature = hashlib.sha256(secret + payload).hexdigest()
    return f"v1:{expiry}:{nonce}:{signature}"


def declared_slots(item: dict) -> list[str]:
    """返回一条记忆声明的槽位（``slot:`` 标签去掉前缀）。"""

    return [
        str(tag)[len("slot:") :]
        for tag in (item.get("tags") or [])
        if str(tag).startswith("slot:")
    ]


def out_of_dictionary_slots(item: dict) -> list[str]:
    """返回一条记忆里不在 L3 词典内的槽位（MEM-L3-02 的违规集合）。"""

    seen: list[str] = []
    for slot in declared_slots(item):
        if resolve_slot(slot) is None and slot not in seen:
            seen.append(slot)
    return seen


def export_records(client: MemoryClient, limit_per_collection: int = 500) -> dict:
    """导出当前身份能看到的全部记忆数据（单集合上限 500）。"""

    return client.privacy.export_data(
        limit_per_collection=limit_per_collection, include_audit=False
    )


def forget_record(
    client: MemoryClient,
    memory_id: str,
    secret: bytes,
    *,
    reason_code: str = "user_request",
) -> dict[str, object]:
    """用确认令牌 forget 一条记忆并等待收敛。"""

    token = confirmation_token(secret, "long_term.forget")
    job = client.long_term.forget(
        memory_id, confirm_token=token, reason_code=reason_code
    )
    settled = client.long_term.wait_forget(job.job_id)
    return {"memory_id": memory_id, "job_id": job.job_id, "result": settled}
