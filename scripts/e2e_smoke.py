#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid

from bianbu_agent_sdk.memory import (
    AsyncMemoryClient,
    InstructionScope,
    MemoryClient,
    MemoryKind,
    SessionSummaryPatch,
    SyncPolicy,
)
from bianbu_agent_sdk.memory.errors import Conflict


def main(socket_path: str) -> None:
    confirm_token = os.environ.get("BIANBU_MEMORY_DEV_CONFIRM_TOKEN")
    if not confirm_token:
        raise RuntimeError("the development confirm token is not configured")

    run_tag = uuid.uuid4().hex[:10]
    session_id = f"e2e-session-{run_tag}"
    topic = f"e2e 日程提醒 {run_tag}"
    with MemoryClient.connect(socket_path) as client:
        assert client.identity.agent_id.startswith("dev.uid.")
        assert client.health()["status"] == "ok"

        instruction = client.instructions.put(
            text="默认使用中文回答",
            scope=InstructionScope.ACCOUNT,
            idempotency_key=f"e2e:{run_tag}:instruction:language:v1",
        )
        repeated = client.instructions.put(
            text="默认使用中文回答",
            scope=InstructionScope.ACCOUNT,
            idempotency_key=f"e2e:{run_tag}:instruction:language:v1",
        )
        assert repeated.instruction_id == instruction.instruction_id

        event = client.sessions.append_event(
            session_id=session_id,
            event_type="user_message",
            payload={"text": "请记录日程偏好"},
            idempotency_key=f"e2e:{run_tag}:event:1",
        )
        summary = client.sessions.update_summary(
            session_id,
            patch=SessionSummaryPatch(
                current_status="记录偏好",
                key_decisions=("使用事务 outbox",),
                next_steps=("验证 Forget",),
            ),
            source_event_sequence=event["event_sequence"],
            expected_revision=0,
            idempotency_key=f"e2e:{run_tag}:summary:1",
        )
        assert summary["revision"] == 1

        receipt = client.long_term.put(
            kind=MemoryKind.PREFERENCE,
            text=f"用户希望 {topic} 提前十五分钟",
            summary=topic,
            sync_policy=SyncPolicy.ACCOUNT_AGENT,
            idempotency_key=f"e2e:{run_tag}:long-term:calendar:v1",
        )
        headers = client.long_term.search_headers(
            topic, kinds=[MemoryKind.PREFERENCE], top_k=5
        )
        # Search order is relevance-based; an existing local database may
        # contain another equally relevant preference, so assert inclusion
        # rather than assuming the newly written record is ranked first.
        assert any(item.memory_id == receipt.record_id for item in headers)
        assert client.long_term.get_many([receipt.record_id])[0].revision == 1

        revised = client.long_term.revise(
            receipt.record_id,
            text=f"用户希望 {topic} 提前三十分钟",
            expected_revision=1,
            idempotency_key=f"e2e:{run_tag}:long-term:calendar:v2",
        )
        assert revised.revision == 2
        try:
            client.long_term.revise(
                receipt.record_id,
                text="过期分支",
                expected_revision=1,
                idempotency_key=f"e2e:{run_tag}:long-term:calendar:stale",
            )
        except Conflict:
            pass
        else:
            raise AssertionError("stale expected_revision was accepted")

        latency = []
        for _ in range(30):
            start = time.perf_counter_ns()
            client.health()
            latency.append((time.perf_counter_ns() - start) / 1_000_000)

        job = client.long_term.forget(
            receipt.record_id,
            confirm_token=confirm_token,
            idempotency_key=f"e2e:{run_tag}:forget:calendar:v1",
        )
        status = client.long_term.wait_forget(job.job_id)
        assert status["local_hidden"] is True
        assert client.long_term.get_many([receipt.record_id]) == []
        sync_status = client.sync.status()
        # The built-in ``default`` account is deliberately local-only and must
        # never enqueue cloud backup work. Authenticated accounts retain the
        # historical outbox assertion so this smoke test also covers upload
        # scheduling when it is explicitly enabled.
        if client.identity.account_id == "default":
            assert sync_status.pending_upload == 0
        else:
            assert sync_status.pending_upload >= 4

        latency.sort()
        p95 = latency[max(0, int(len(latency) * 0.95) - 1)]
        print(f"SYNC_FLOW_OK p95_health_ms={p95:.3f}")

    asyncio.run(async_flow(socket_path))


async def async_flow(socket_path: str) -> None:
    async with await AsyncMemoryClient.connect(socket_path) as client:
        assert (await client.health())["status"] == "ok"
        instructions = await client.instructions.list()
        assert instructions
        print("ASYNC_FLOW_OK")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: e2e_smoke.py SOCKET_PATH")
    main(sys.argv[1])
