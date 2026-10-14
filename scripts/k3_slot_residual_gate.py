#!/usr/bin/env python3
"""K3 现场验证：单值槽 profile.user_name 不得吞掉同句里的其它持久内容。

复现 10.0.91.67 / 2026-09-24 09:57 的缺陷：
  * 显式写入「用户自称"百岁太奶"，不熟悉科技产品。希望回答简洁、清晰、通俗易懂，避免专业术语和复杂步骤。」
    被贴上 slot:profile.user_name（rev1）；
  * 4 秒后 after_run 自动抽取把同一槽位改成「我是百岁太奶」（rev2），整段偏好被静默覆盖。

修复后应有两条性质：
  1) 分类侧：复合句不再独占单值槽（退回无槽位散文），纯姓名声明的归位不受影响；
  2) 覆盖侧：老库里那句复合文本被同槽位覆盖时，槽位外的持久内容另存为一条无槽位记忆。

用法（K3，设备自带 python3 即可）：
    cd ~/mem-gateway-build
    PYTHONPATH=sdk/python/src python3 scripts/k3_slot_residual_gate.py \
        --memoryd target/release/bianbu-agent-memoryd
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import uuid

from bianbu_agent_sdk.memory import AsyncMemoryClient
from bianbu_agent_sdk.memory.ontology import preference_slot
from bianbu_agent_sdk.memory.runtime import MemoryBridge
from bianbu_agent_sdk.memory.types import EvidenceRef, MemoryKind, SyncPolicy

INCIDENT = (
    "\u7528\u6237\u81ea\u79f0\u201c\u767e\u5c81\u592a\u5976\u201d\uff0c\u4e0d\u719f\u6089\u79d1\u6280\u4ea7\u54c1\u3002"
    "\u5e0c\u671b\u56de\u7b54\u7b80\u6d01\u3001\u6e05\u6670\u3001\u901a\u4fd7\u6613\u61c2\uff0c\u907f\u514d\u4e13\u4e1a\u672f\u8bed\u548c\u590d\u6742\u6b65\u9aa4\u3002"
)
USER_TURN = (
    "\u6211\u662f\u767e\u5c81\u592a\u5976\uff0c\u5e74\u8f7b\u4eba\u7684\u4e1c\u897f\u6211\u90fd\u4e0d\u61c2\uff0c"
    "\u5f97\u7528\u7b80\u6d01\u6e05\u6670\u6613\u61c2\u7684\u8bdd\u6765\u56de\u7b54\u6211\u7684\u95ee\u9898"
)
EXPECT_NAME = "\u6211\u662f\u767e\u5c81\u592a\u5976"
EXPECT_RESIDUAL = (
    "\u4e0d\u719f\u6089\u79d1\u6280\u4ea7\u54c1\u3002\u5e0c\u671b\u56de\u7b54\u7b80\u6d01\u3001\u6e05\u6670\u3001"
    "\u901a\u4fd7\u6613\u61c2\uff0c\u907f\u514d\u4e13\u4e1a\u672f\u8bed\u548c\u590d\u6742\u6b65\u9aa4"
)

CHECKS: list[dict[str, object]] = []


def check(name: str, condition: bool, detail: object = None) -> None:
    """Record one verdict and keep going: the whole table is the evidence."""

    CHECKS.append({"name": name, "ok": bool(condition), "detail": detail})


async def wait_for_socket(path: Path, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise RuntimeError(f"memoryd exited during setup: {stderr.strip()}")
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(str(path))
        except OSError:
            pass
        else:
            return
        finally:
            client.close()
        await asyncio.sleep(0.05)
    raise TimeoutError(path)


async def all_records(client: AsyncMemoryClient) -> list[object]:
    headers = await client.long_term.search_headers("", top_k=100)
    return await client.long_term.get_many([header.memory_id for header in headers])


def check_classification() -> None:
    check("classify-incident-compound-not-a-slot", preference_slot(INCIDENT) is None, preference_slot(INCIDENT))
    check("classify-user-turn-compound-not-a-slot", preference_slot(USER_TURN) is None, preference_slot(USER_TURN))
    for text, label in (
        ("\u6211\u662f\u5c0f\u5706", "classify-pure-name-still-slots"),
        ("\u6211\u53eb\u5c0f\u5706", "classify-call-me-still-slots"),
        ('\u7528\u6237\u81ea\u79f0"\u5c0f\u5706"\uff08\u53ef\u80fd\u4e3a\u6635\u79f0\uff09', "classify-nickname-note-still-slots"),
        ("\u7528\u6237\u540d\u79f0\u4e3a Acceptance User 742\u3002", "classify-long-name-still-slots"),
        ("my name is Alice", "classify-english-name-still-slots"),
    ):
        check(label, preference_slot(text) == "profile.user_name", preference_slot(text))
    check(
        "classify-english-compound-not-a-slot",
        preference_slot("my name is Alice and I like tea") is None,
        preference_slot("my name is Alice and I like tea"),
    )


async def check_write_path(client: AsyncMemoryClient, bridge: MemoryBridge) -> dict[str, object]:
    session_id = f"slot-residual-write-{uuid.uuid4().hex[:8]}"
    result = await bridge.upsert_explicit(
        content=INCIDENT,
        target="user",
        run_id="slot-residual-write",
        session_id=session_id,
    )
    check("write-path-accepts-prose", result.get("success") is True, result)
    stored = [record for record in await all_records(client) if record.text == INCIDENT]
    check("write-path-keeps-text-verbatim", len(stored) == 1, [record.text for record in stored])
    check(
        "write-path-applies-no-name-slot",
        not any(str(tag).startswith("slot:") for tag in stored[0].tags),
        list(stored[0].tags),
    )
    return {"session_id": session_id, "tags": list(stored[0].tags)}


async def check_legacy_overwrite(client: AsyncMemoryClient, bridge: MemoryBridge) -> dict[str, object]:
    session_id = f"slot-residual-legacy-{uuid.uuid4().hex[:8]}"
    run_id = f"slot-residual-legacy-{uuid.uuid4().hex[:8]}"
    # The device's rev1: a slot record whose text is a name *plus* a preference,
    # written through the low-level path (that is how the slot tag got there).
    slotted = await client.long_term.put(
        kind=MemoryKind.PROFILE,
        text=INCIDENT,
        tags=("slot:profile.user_name",),
        evidence=(EvidenceRef("session_turn", f"{session_id}:seed"),),
        sync_policy=SyncPolicy.LOCAL_ONLY,
        sensitivity="private",
        idempotency_key=f"{session_id}-seed",
    )
    check("legacy-slot-record-seeded", slotted.revision == 1, slotted.revision)

    ok = await bridge.record_turn(
        run_id=run_id,
        session_id=session_id,
        agent_id="k3-slot-residual-gate",
        user_text=USER_TURN,
        assistant_text="\u597d\u5627\uff0c\u592a\u5976\u3002",
    )
    check("after-run-turn-recorded", ok is True, ok)

    records = await all_records(client)
    slot_records = [
        record
        for record in records
        if any(str(tag) == "slot:profile.user_name" for tag in record.tags)
    ]
    slotted_now = slot_records[0] if slot_records else None
    check(
        "legacy-slot-now-holds-the-name",
        slotted_now is not None and slotted_now.text == EXPECT_NAME,
        [(record.text, record.revision) for record in slot_records],
    )
    check(
        "legacy-slot-advanced-one-revision",
        slotted_now is not None and slotted_now.revision == 2,
        slotted_now.revision if slotted_now else None,
    )

    preserved = [record for record in records if record.text == EXPECT_RESIDUAL]
    residual = preserved[0] if preserved else None
    check("residual-written-back", residual is not None, [record.text for record in records])
    check(
        "residual-is-a-plain-memory",
        residual is not None
        and not any(str(tag).startswith("slot:") for tag in residual.tags),
        list(residual.tags) if residual else None,
    )
    check(
        "residual-carries-its-tag",
        residual is not None and "slot-residual" in residual.tags,
        list(residual.tags) if residual else None,
    )

    recalled = await bridge.recall("\u901a\u4fd7\u6613\u61c2", top_k=5)
    check(
        "residual-is-recallable",
        any(EXPECT_RESIDUAL in item for item in recalled),
        list(recalled),
    )

    # A second identical automatic pass must not pile up copies of the preference.
    second_run = f"{run_id}-again"
    await bridge.record_turn(
        run_id=second_run,
        session_id=session_id,
        agent_id="k3-slot-residual-gate",
        user_text=USER_TURN,
        assistant_text="\u597d\u5627\uff0c\u592a\u5976\u3002",
    )
    again = await all_records(client)
    residuals = [record for record in again if "slot-residual" in record.tags]
    check("second-pass-does-not-duplicate", len(residuals) == 1, len(residuals))

    return {
        "session_id": session_id,
        "slot_text_after": slotted_now.text if slotted_now else None,
        "slot_revision_after": slotted_now.revision if slotted_now else None,
        "residual_tags": list(residual.tags) if residual else None,
        "residual_records_after_second_pass": len(residuals),
        "recalled": list(recalled)[:5],
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--memoryd", default="target/release/bianbu-agent-memoryd", type=Path)
    arguments = parser.parse_args()
    memoryd = arguments.memoryd.resolve()
    if not memoryd.is_file():
        raise FileNotFoundError(memoryd)

    report: dict[str, object] = {"memoryd": str(memoryd)}
    exit_code = 1
    with tempfile.TemporaryDirectory(prefix="agent-memory-slot-residual-") as raw:
        root = Path(raw)
        secret_path = root / "account-secret"
        secret_path.write_text(uuid.uuid4().hex + uuid.uuid4().hex + "\n", encoding="utf-8")
        secret_path.chmod(0o600)
        socket_path = root / "memoryd.sock"
        environment = dict(os.environ)
        environment["AGENT_MEMORY_EMBEDDING_DISABLED"] = "1"
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
                str(secret_path),
                "--initialize-key-package",
            ],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            await wait_for_socket(socket_path, process)
            client = await AsyncMemoryClient.connect(socket_path)
            bridge = MemoryBridge(str(socket_path))
            bridge._client = client
            try:
                # Each phase is guarded on its own: a precondition that fails on the
                # old code must not hide what the later phases would have shown.
                for phase, runner in (
                    ("classification", check_classification),
                    ("write_path", lambda: check_write_path(client, bridge)),
                    ("legacy_overwrite", lambda: check_legacy_overwrite(client, bridge)),
                ):
                    try:
                        produced = runner()
                        if asyncio.iscoroutine(produced):
                            report[phase] = await produced
                    except Exception as error:  # noqa: BLE001 - the report is the deliverable
                        report[phase + "_error"] = f"{type(error).__name__}: {error}"
                exit_code = 0 if all(item["ok"] for item in CHECKS) else 1
            finally:
                await client.close()
        except Exception as error:  # noqa: BLE001 - the report is the deliverable
            report["error"] = f"{type(error).__name__}: {error}"
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()

    report["checks"] = CHECKS
    report["passed"] = sum(1 for item in CHECKS if item["ok"])
    report["total"] = len(CHECKS)
    report["status"] = "K3_SLOT_RESIDUAL_GATE_OK" if exit_code == 0 else "K3_SLOT_RESIDUAL_GATE_FAIL"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
