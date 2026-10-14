from datetime import datetime, timedelta, timezone

import pytest

import bianbu_agent_sdk.memory.runtime as memory_runtime
from bianbu_agent_sdk.memory.runtime import MemoryBridge
from bianbu_agent_sdk.memory.types import MemoryKind


@pytest.mark.asyncio
async def test_reminder_recall_prioritizes_due_reminder_over_profiles(monkeypatch):
    fixed_now = datetime(
        2026, 8, 13, 12, 0, tzinfo=timezone(timedelta(hours=8), "Asia/Shanghai")
    )
    monkeypatch.setattr(memory_runtime, "now_local", lambda: fixed_now)

    class Header:
        def __init__(self, memory_id, score=1.0):
            self.memory_id = memory_id
            self.score = score

    class Memory:
        def __init__(self, memory_id, kind, text, tags=()):
            self.memory_id = memory_id
            self.kind = kind
            self.text = text
            self.tags = tags
            self.revision = 1
            self.source = "agent"

    reminder = Memory(
        "reminder-1",
        MemoryKind.EPISODIC,
        "提醒事项：计划时间 2026-08-13T09:00+08:00 (Asia/Shanghai)；原始内容：完成工程对接",
        ("temporal:reminder", "due-date:2026-08-13"),
    )
    profile = Memory(
        "profile-1", MemoryKind.PROFILE, "常用浏览器为 Firefox。", ("user",)
    )

    class LongTerm:
        async def search_headers(self, query, top_k=5):
            del top_k
            if query == "提醒事项":
                return [Header("reminder-1", 1.5)]
            if query == "":
                return [Header("profile-1", 1.0)]
            return [Header("reminder-1", 1.5)]

        async def get_many(self, memory_ids):
            return [item for item in (reminder, profile) if item.memory_id in memory_ids]

    monkeypatch.setenv("BIANBU_MEMORY_ENGINE", "scheme-a")
    bridge = MemoryBridge()
    bridge._client = type("Client", (), {"long_term": LongTerm()})()
    values = await bridge.recall("今天有什么事情需要提醒我吗", top_k=5)
    assert values
    assert "2026-08-13T09:00+08:00" in values[0]
    assert "2026-08-13" in values[0]
    assert "今天到期" in values[0]
    assert all("Firefox" not in item for item in values)
