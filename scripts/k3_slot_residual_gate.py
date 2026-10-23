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

#: The 2026-10-15 rename from defect 7136281755: one sentence that declares a
#: name *and* an address.  It used to be stored as a single unslotted record, so
#: the later rename moved the name while the address stayed on the old value.
RENAME_SENTENCE = (
    "\u6211\u7684\u540d\u5b57\u53eb\u738b\u6e90\uff0c\u4ee5\u540e\u53eb\u6211\u5c0f\u5143"
)
RENAME_NAME = "\u6211\u7684\u540d\u5b57\u53eb\u738b\u6e90"
RENAME_ADDRESS = "\u4ee5\u540e\u53eb\u6211\u5c0f\u5143"

#: The second rename: it must converge both single-valued slots, not stack up a
#: second identity beside the first.
RENAME_AGAIN_SENTENCE = (
    "\u6211\u7684\u540d\u5b57\u53eb\u674e\u96f7\uff0c\u4ee5\u540e\u53eb\u6211\u5c0f\u674e"
)
RENAME_AGAIN_NAME = "\u6211\u7684\u540d\u5b57\u53eb\u674e\u96f7"
RENAME_AGAIN_ADDRESS = "\u4ee5\u540e\u53eb\u6211\u5c0f\u674e"

#: The fused record the defect left in the store: a name and an address in one
#: unslotted entry, so neither rename reached the half it was about.
RENAME_FUSED = (
    "\u7528\u6237\u540d\u5b57\u662f Dong\uff0c\u5e0c\u671b\u88ab\u79f0\u547c\u4e3a"
    "\u300c\u592a\u5976\u300d\u3002"
)

#: The two halves the fused sentence splits into on the way in.
RENAME_FUSED_NAME = "\u7528\u6237\u540d\u5b57\u662f Dong"
RENAME_FUSED_ADDRESS = "\u5e0c\u671b\u88ab\u79f0\u547c\u4e3a\u300c\u592a\u5976\u300d"

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
    # Defect 7136281755: an address owns its own single-valued field, so the
    # sentence that declares a name and an address belongs to neither field on
    # its own (extraction splits it), while a bare address claims the new one.
    check(
        "classify-rename-sentence-not-a-slot",
        preference_slot(RENAME_SENTENCE) is None,
        preference_slot(RENAME_SENTENCE),
    )
    check(
        "classify-bare-address-slots",
        preference_slot(RENAME_ADDRESS) == "profile.preferred_address",
        preference_slot(RENAME_ADDRESS),
    )
    check(
        "classify-fused-legacy-record-not-a-name-slot",
        preference_slot(RENAME_FUSED) is None,
        preference_slot(RENAME_FUSED),
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


async def check_rename_slots(client: AsyncMemoryClient, bridge: MemoryBridge) -> dict[str, object]:
    """Defect 7136281755: a rename must reach the address field, not just the name.

    The defect-era store held the identity as one unslotted sentence, so the
    rename moved the name slot while the address kept answering from the old
    value.  Two slotted fields change that: the fused sentence splits on the way
    in, and each later rename converges both fields instead of stacking a second
    identity beside the first.
    """

    # Single-valued is a property of the whole store, so these read it globally:
    # a rename may *revise* the field record a previous turn wrote, and exactly
    # one record per field is what "no second identity" means.
    async def slot_records(slot: str) -> list[object]:
        return [
            record
            for record in await all_records(client)
            if any(str(tag) == f"slot:{slot}" for tag in record.tags)
        ]

    async def rename(session_id: str, turn: str, label: str) -> None:
        ok = await bridge.record_turn(
            run_id=f"{session_id}-{label}",
            session_id=session_id,
            agent_id="k3-slot-residual-gate",
            user_text=turn,
            assistant_text="\u6536\u5230\u3002",
        )
        check(f"rename-{label}-turn-recorded", ok is True, ok)

    session_id = f"slot-rename-{uuid.uuid4().hex[:8]}"

    # Turn 1: the fused sentence the defect left behind ("用户名字是 Dong，希望被称呼
    # 为「太奶」") is split into the two single-valued fields it declares.
    await rename(session_id, RENAME_FUSED, "seed")
    names = await slot_records("profile.user_name")
    addresses = await slot_records("profile.preferred_address")
    check(
        "fused-sentence-writes-the-name-field",
        len(names) == 1 and names[0].text == RENAME_FUSED_NAME,
        [(record.text, record.revision) for record in names],
    )
    check(
        "fused-sentence-writes-the-address-field",
        len(addresses) == 1 and addresses[0].text == RENAME_FUSED_ADDRESS,
        [(record.text, record.revision) for record in addresses],
    )

    # Turn 2: the 2026-10-15 rename.  Both fields move; neither is re-added.
    await rename(session_id, RENAME_SENTENCE, "rename")
    names_after = await slot_records("profile.user_name")
    addresses_after = await slot_records("profile.preferred_address")
    check(
        "rename-moves-the-name-field",
        len(names_after) == 1 and names_after[0].text == RENAME_NAME,
        [(record.text, record.revision) for record in names_after],
    )
    check(
        "rename-moves-the-address-field",
        len(addresses_after) == 1 and addresses_after[0].text == RENAME_ADDRESS,
        [(record.text, record.revision) for record in addresses_after],
    )
    check(
        "rename-does-not-stack-a-second-identity",
        len(names_after) == 1 and len(addresses_after) == 1,
        (len(names_after), len(addresses_after)),
    )

    # Turn 3: a second rename converges the same two fields again.
    await rename(session_id, RENAME_AGAIN_SENTENCE, "rename-again")
    names_final = await slot_records("profile.user_name")
    addresses_final = await slot_records("profile.preferred_address")
    check(
        "second-rename-converges-the-name-field",
        len(names_final) == 1 and names_final[0].text == RENAME_AGAIN_NAME,
        [(record.text, record.revision) for record in names_final],
    )
    check(
        "second-rename-converges-the-address-field",
        len(addresses_final) == 1 and addresses_final[0].text == RENAME_AGAIN_ADDRESS,
        [(record.text, record.revision) for record in addresses_final],
    )

    recalled = await bridge.recall(RENAME_AGAIN_NAME, top_k=5)
    check(
        "renamed-identity-is-recallable",
        any(RENAME_AGAIN_ADDRESS in item for item in recalled),
        list(recalled)[:5],
    )

    return {
        "session_id": session_id,
        "name_slot": names_final[0].text if names_final else None,
        "name_revision": names_final[0].revision if names_final else None,
        "address_slot": addresses_final[0].text if addresses_final else None,
        "address_revision": addresses_final[0].revision if addresses_final else None,
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
                    ("rename_slots", lambda: check_rename_slots(client, bridge)),
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
