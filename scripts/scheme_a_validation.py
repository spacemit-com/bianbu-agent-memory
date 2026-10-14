#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections.abc import Sequence

from bianbu_agent_sdk.memory import (
    AsyncMemoryClient,
    Mem0Coordinator,
    MemoryBridge,
    MemoryCandidate,
    MemoryKind,
    MemoryMutation,
    MutationEvent,
)
from bianbu_agent_sdk.memory.types import LongTermMemory


class ActionPlanner:
    def __init__(self, action: MutationEvent, text: str) -> None:
        self.action = action
        self.candidate = MemoryCandidate(text, MemoryKind.PREFERENCE, 0.9)

    async def extract(
        self, messages: Sequence[dict[str, str]]
    ) -> tuple[MemoryCandidate, ...]:
        return (self.candidate,)

    async def merge(
        self,
        candidates: Sequence[MemoryCandidate],
        existing: Sequence[LongTermMemory],
    ) -> tuple[MemoryMutation, ...]:
        if self.action is MutationEvent.ADD:
            return (
                MemoryMutation(
                    event=self.action,
                    text=self.candidate.text,
                    kind=self.candidate.kind,
                    confidence=self.candidate.confidence,
                ),
            )
        if not existing:
            raise AssertionError(f"{self.action.value} did not recall an existing memory")
        current = existing[0]
        return (
            MemoryMutation(
                event=self.action,
                text=self.candidate.text,
                kind=current.kind,
                memory_id=current.memory_id,
                old_memory=current.text,
                confidence=self.candidate.confidence,
            ),
        )


class EmptyPlanner:
    async def extract(
        self, messages: Sequence[dict[str, str]]
    ) -> tuple[MemoryCandidate, ...]:
        return ()

    async def merge(
        self,
        candidates: Sequence[MemoryCandidate],
        existing: Sequence[LongTermMemory],
    ) -> tuple[MemoryMutation, ...]:
        return ()


async def ingest(
    client: AsyncMemoryClient,
    *,
    session_id: str,
    run_id: str,
    action: MutationEvent,
    text: str,
):
    coordinator = Mem0Coordinator(ActionPlanner(action, text))
    return await coordinator.ingest_turn(
        client,
        session_id=session_id,
        run_id=run_id,
        user_text=text,
        assistant_text="已记录",
    )


async def validate(socket_path: str, prefix: str) -> dict[str, object]:
    mutation_session = f"{prefix}-mutations"
    summary_session = f"{prefix}-summary"
    async with await AsyncMemoryClient.connect(socket_path, timeout=5.0) as client:
        health = await client.health()

        added = await ingest(
            client,
            session_id=mutation_session,
            run_id=f"{prefix}-add",
            action=MutationEvent.ADD,
            text="编辑器主题偏好：深色",
        )
        assert added.added == 1
        headers = await client.long_term.search_headers("编辑器主题偏好", top_k=5)
        assert headers
        memory_id = headers[0].memory_id

        updated = await ingest(
            client,
            session_id=mutation_session,
            run_id=f"{prefix}-update",
            action=MutationEvent.UPDATE,
            text="编辑器主题偏好：浅色",
        )
        assert updated.updated == 1
        current = (await client.long_term.get_many([memory_id]))[0]
        assert current.text == "编辑器主题偏好：浅色"
        assert current.revision == 2

        unchanged = await ingest(
            client,
            session_id=mutation_session,
            run_id=f"{prefix}-none",
            action=MutationEvent.NONE,
            text="编辑器主题偏好：浅色",
        )
        assert unchanged.unchanged == 1
        assert (await client.long_term.get_many([memory_id]))[0].revision == 2

        proposed = await ingest(
            client,
            session_id=mutation_session,
            run_id=f"{prefix}-delete",
            action=MutationEvent.DELETE,
            text="编辑器主题偏好：不再保留",
        )
        assert proposed.delete_proposals == 1
        assert (await client.long_term.get_many([memory_id]))[0].revision == 2
        mutation_context = await client.sessions.get_context(
            mutation_session, include_events=True, include_summary=False
        )
        assert mutation_context["events"][-1]["event_type"] == "memory_delete_proposed"

        recall_bridge = MemoryBridge(
            socket_path,
            timeout=5.0,
            coordinator=Mem0Coordinator(EmptyPlanner()),
        )
        try:
            natural_recall = await recall_bridge.recall(
                "你好，请问编辑器主题偏好是什么颜色"
            )
        finally:
            await recall_bridge.close()
        assert natural_recall and "浅色" in natural_recall[0]

        bridge = MemoryBridge(
            socket_path,
            timeout=5.0,
            coordinator=Mem0Coordinator(EmptyPlanner()),
        )
        try:
            for turn in range(1, 6):
                recorded = await bridge.record_turn(
                    run_id=f"{prefix}-turn-{turn}",
                    session_id=summary_session,
                    agent_id="personal-agent-default",
                    user_text=f"第 {turn} 轮方案 A 验证问题",
                    assistant_text=f"第 {turn} 轮方案 A 验证回答",
                )
                assert recorded
        finally:
            await bridge.close()

        summary_context = await client.sessions.get_context(
            summary_session,
            include_events=True,
            include_summary=True,
            event_limit=20,
        )
        assert len(summary_context["events"]) == 10
        assert summary_context["summary"]["revision"] == 1
        assert summary_context["summary"]["source_event_sequence"] == 10
        compacted = await client.sessions.compact(
            summary_session,
            level="auto",
            target_tokens=2000,
            idempotency_key=f"{prefix}-compact",
        )
        assert compacted["completed_level"] == "summary"
        assert compacted["source_events_preserved"] is True
        stats = await client.stats()

    unavailable = MemoryBridge(f"{socket_path}.missing", timeout=0.2)
    try:
        degraded_recall = await unavailable.recall("编辑器主题偏好")
        degraded_write = await unavailable.record_turn(
            run_id=f"{prefix}-degraded",
            session_id=f"{prefix}-degraded",
            agent_id="personal-agent-default",
            user_text="memoryd 不可用时仍应降级",
            assistant_text="Runtime 不应被记忆服务阻塞",
        )
    finally:
        await unavailable.close()
    assert degraded_recall == ()
    assert degraded_write is False

    return {
        "status": "SCHEME_A_FUNCTIONAL_OK",
        "timestamp_ms": int(time.time() * 1000),
        "health": health,
        "mutations": {
            "add": added.added,
            "update": updated.updated,
            "none": unchanged.unchanged,
            "delete_proposal": proposed.delete_proposals,
            "delete_did_not_forget": True,
            "memory_id": memory_id,
            "revision": 2,
            "natural_language_recall": list(natural_recall),
        },
        "summary": {
            "turns": 5,
            "events": len(summary_context["events"]),
            "revision": summary_context["summary"]["revision"],
            "producer": summary_context["summary"]["producer"],
            "compaction": compacted,
        },
        "sdk_degradation": {
            "recall": list(degraded_recall),
            "record_turn": degraded_write,
        },
        "stats": stats,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("socket_path")
    parser.add_argument(
        "--prefix", default=f"scheme-a-{int(time.time())}", help="unique test namespace"
    )
    arguments = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(validate(arguments.socket_path, arguments.prefix)),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
