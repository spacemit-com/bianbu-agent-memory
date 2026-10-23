#!/usr/bin/env python3
"""K3 现场验收：MEM-L3-03 字段值闸门与 MEM-L2-01 摘要触发条件。

用隔离的 development-identity memoryd 在 K3 上跑真实 daemon：

1) MEM-L3-03：显式带 `slot` 的写入按 L3 词典校验——值形态归一为 canonical 值落库，
   域外值/超长值/叙述文本返回 `invalid_value` 且一条都不落库；同一叙述文本不带 slot
   仍然可写，且原文保留。
2) MEM-L2-01：只有完整轮次触发 L2 摘要——`task_started`/`tool_call`/`tool_result`
   既不是触发源，也不再构成旁路。

用法（K3）：
    cd ~/mem-gateway-build
    PYTHONPATH=sdk/python/src python3 scripts/k3_field_value_gate.py \
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
from bianbu_agent_sdk.memory.runtime import MemoryBridge

SLOT = "appearance.terminal_color_scheme"
TOOL_EVENT_TYPES = ("task_started", "tool_call", "tool_result")
PROSE = "用户喜欢的终端颜色是浅色（light），不是深色。"

CHECKS: list[dict[str, object]] = []


def check(name: str, condition: bool, detail: object = None) -> None:
    CHECKS.append({"name": name, "ok": bool(condition), "detail": detail})
    if not condition:
        raise AssertionError(f"{name}: {json.dumps(detail, ensure_ascii=False)}")


async def wait_for_socket(path: Path, process: subprocess.Popen[str]) -> None:
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


async def l3_records(client: AsyncMemoryClient) -> list[object]:
    headers = await client.long_term.search_headers("", top_k=100)
    return await client.long_term.get_many([header.memory_id for header in headers])


async def exercise_value_gate(
    client: AsyncMemoryClient, bridge: MemoryBridge
) -> dict[str, object]:
    accepted = await bridge.upsert_explicit(
        content="深色",
        target="user",
        run_id="value-gate",
        session_id="value-gate-session",
        slot="终端配色",
    )
    check("value-alias-accepted", accepted.get("success") is True, accepted)
    records = await l3_records(client)
    canonical = [record for record in records if record.text == "dark"]
    check(
        "value-stored-canonical",
        len(canonical) == 1 and f"slot:{SLOT}" in canonical[0].tags,
        [record.text for record in records],
    )

    # Recall dedupes by slot, so the canonical value is asserted before a
    # prose memory can infer the same slot and win the dedupe.
    recalled = await bridge.recall("终端配色", top_k=5)
    check(
        "canonical-value-recallable",
        any(f"slot={SLOT}" in item for item in recalled)
        and any("dark" in item for item in recalled),
        list(recalled),
    )

    before = len(records)
    refused_domain = await bridge.upsert_explicit(
        content="unknown-manager",
        target="user",
        run_id="value-gate",
        session_id="value-gate-session",
        slot="development.package_manager",
    )
    check(
        "value-outside-domain-refused",
        refused_domain.get("code") == "invalid_value"
        and "uv" in (refused_domain.get("allowed_values") or []),
        refused_domain,
    )
    refused_length = await bridge.upsert_explicit(
        content="z" * 65,
        target="user",
        run_id="value-gate",
        session_id="value-gate-session",
        slot="development.shell",
    )
    check(
        "value-too-long-refused",
        refused_length.get("code") == "invalid_value"
        and refused_length.get("max_length") == 64,
        refused_length,
    )
    refused_prose = await bridge.upsert_explicit(
        content=PROSE,
        target="user",
        run_id="value-gate",
        session_id="value-gate-session",
        slot=SLOT,
    )
    check(
        "prose-under-a-slot-refused",
        refused_prose.get("code") == "invalid_value"
        and bool(refused_prose.get("assistant_instruction")),
        refused_prose,
    )
    after = await l3_records(client)
    check(
        "refusals-write-nothing",
        len(after) == before,
        {"before": before, "after": len(after)},
    )

    plain = await bridge.upsert_explicit(
        content=PROSE,
        target="memory",
        run_id="value-gate",
        session_id="value-gate-session",
    )
    check("prose-without-a-slot-accepted", plain.get("success") is True, plain)
    stored_texts = [record.text for record in await l3_records(client)]
    check("prose-kept-verbatim", PROSE in stored_texts, stored_texts)

    return {
        "records": stored_texts,
        "refused": {
            "domain": refused_domain.get("code"),
            "length": refused_length.get("code"),
            "prose": refused_prose.get("code"),
        },
        "recalled": list(recalled),
    }


async def exercise_summary_trigger(client: AsyncMemoryClient) -> dict[str, object]:
    session_id = f"trigger-{uuid.uuid4().hex[:12]}"

    async def append(event_type: str, payload: dict[str, object], key: str) -> None:
        await client.sessions.append_event(
            session_id=session_id,
            event_type=event_type,
            payload=payload,
            idempotency_key=f"{session_id}-{key}",
        )

    async def append_turn(turn: int) -> None:
        turn_id = f"turn-{turn}"
        await append(
            "user_message",
            {"text": f"第 {turn} 轮问题", "turn_id": turn_id, "status": "completed"},
            f"{turn_id}-user",
        )
        await append(
            "assistant_message",
            {"text": f"第 {turn} 轮回答", "turn_id": turn_id, "status": "completed"},
            f"{turn_id}-assistant",
        )
        for event_type in TOOL_EVENT_TYPES:
            await append(
                event_type,
                {"turn_id": turn_id, "status": "completed", "name": event_type},
                f"{turn_id}-{event_type}",
            )

    for turn in range(1, 5):
        await append_turn(turn)
    context = await client.sessions.get_context(
        session_id, include_events=True, include_summary=True, event_limit=50
    )
    summary = context.get("summary") or {}
    check(
        "tool-events-are-retained",
        len(context.get("events") or []) == 20,
        len(context.get("events") or []),
    )
    check(
        "four-turns-and-tool-events-do-not-summarize",
        int(summary.get("revision") or 0) == 0,
        {"revision": summary.get("revision"), "producer": summary.get("producer")},
    )

    await append_turn(5)
    context = await client.sessions.get_context(
        session_id, include_events=True, include_summary=True, event_limit=60
    )
    summary = context.get("summary") or {}
    check(
        "fifth-complete-turn-summarizes",
        int(summary.get("revision") or 0) == 1
        and summary.get("producer") == "memoryd.rules",
        summary,
    )

    await append(
        "tool_call", {"turn_id": "turn-5", "status": "completed", "name": "late"}, "late-tool"
    )
    await append(
        "task_failed", {"turn_id": "turn-5", "status": "failed", "name": "late"}, "late-task"
    )
    context = await client.sessions.get_context(session_id, include_summary=True)
    summary = context.get("summary") or {}
    check(
        "late-tool-events-do-not-re-summarize",
        int(summary.get("revision") or 0) == 1,
        {"revision": summary.get("revision")},
    )
    return {
        "session_id": session_id,
        "events_after_four_turns": 20,
        "revision_after_four_turns": 0,
        "revision_after_fifth_turn": 1,
        "producer": summary.get("producer"),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--memoryd", default="target/release/bianbu-agent-memoryd", type=Path
    )
    arguments = parser.parse_args()
    memoryd = arguments.memoryd.resolve()
    if not memoryd.is_file():
        raise FileNotFoundError(memoryd)
    confirm_token = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="agent-memory-value-gate-") as raw:
        root = Path(raw)
        secret_path = root / "account-secret"
        secret_path.write_text(uuid.uuid4().hex + uuid.uuid4().hex + "\n", encoding="utf-8")
        secret_path.chmod(0o600)
        socket_path = root / "memoryd.sock"
        environment = dict(os.environ)
        # Hermetic: the semantic path is not what this gate measures.
        environment["AGENT_MEMORY_EMBEDDING_DISABLED"] = "1"
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
                report = {
                    "memoryd": str(memoryd),
                    "value_gate": await exercise_value_gate(client, bridge),
                    "summary_trigger": await exercise_summary_trigger(client),
                }
            finally:
                await client.close()
            report["checks"] = CHECKS
            report["status"] = "K3_FIELD_VALUE_GATE_OK"
            print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()


if __name__ == "__main__":
    asyncio.run(main())