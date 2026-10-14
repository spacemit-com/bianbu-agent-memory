"""session_search shapes, ported from the retired Runtime Tool tests.

The Runtime Tool read the current session from its call context; the MCP
server reads it from the ``bianbu/host`` meta the Worker attaches.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from bianbu_agent_sdk.memory.mcp_server import sessions as session_tools
from bianbu_agent_sdk.memory.mcp_server.context import HostContext

CALL = HostContext(
    session_id="session-current",
    run_id="run-1",
    agent_id="memory-agent",
    account_id="acct-1",
)


class _FakeSessions:
    def __init__(self, status, histories):
        self.status = status
        self.histories = histories
        self.recent_calls: list[dict] = []
        self.history_calls: list[tuple[str, int]] = []

    async def get_recent_context(self, **kwargs):
        self.recent_calls.append(kwargs)
        return self.status

    async def get_history(self, session_id, *, before_sequence=None, limit=200):
        self.history_calls.append((session_id, limit))
        return {
            "session_id": session_id,
            "events": self.histories.get(session_id, []),
            "next_before_sequence": None,
        }


class _FakeClient:
    def __init__(self, sessions):
        self.sessions = sessions
        self.closed = False

    async def close(self):
        self.closed = True


class _FakeFactory:
    def __init__(self, client, error=None):
        self.client = client
        self.error = error
        self.connect_kwargs = None

    async def connect(self, socket_path, timeout=5.0, *, account_id=None, agent_name=None):
        if self.error is not None:
            raise self.error
        self.connect_kwargs = {
            "socket_path": socket_path,
            "timeout": timeout,
            "account_id": account_id,
            "agent_name": agent_name,
        }
        return self.client


def _event(seq, event_type, text, *, run_id=""):
    return {
        "event_id": f"event-{seq}",
        "event_sequence": seq,
        "event_type": event_type,
        "payload": {
            "role": "user" if event_type == "user_message" else "assistant",
            "text": text,
            "run_id": run_id,
            "agent_id": "memory-agent",
            "status": "completed",
        },
    }


def _status_entry(session_id, title, last_active_at, **flags):
    return {
        "session_id": session_id,
        "title": title,
        "last_active_at": last_active_at,
        "completed_turn_count": flags.get("turns", 1),
        "in_progress": flags.get("in_progress", False),
        "interrupted": flags.get("interrupted", False),
    }


def _fixtures():
    status = {
        "selection_mode": "session_status",
        "session_limit": 20,
        "sessions": [
            _status_entry("chess", "象棋游戏开发", 3000, turns=0, in_progress=True),
            _status_entry("trip", "旅游计划", 2000, turns=2),
            _status_entry("old", "最早的会话", 1000, turns=1),
            _status_entry("empty", "没有消息的会话", 500, turns=0),
        ],
    }
    histories = {
        "chess": [
            _event(0, "task_started", "帮我开发一个象棋游戏", run_id="run-3"),
        ],
        "trip": [
            _event(0, "task_started", "帮我制定旅游计划", run_id="run-1"),
            _event(1, "user_message", "帮我制定旅游计划", run_id="run-1"),
            _event(2, "assistant_message", "好的，先看三天的行程", run_id="run-1"),
            _event(3, "user_message", "预算改成5000", run_id="run-2"),
            _event(4, "assistant_message", "已按预算调整", run_id="run-2"),
        ],
        "old": [
            _event(0, "user_message", "最早的问题：帮我", run_id="run-4"),
            _event(1, "assistant_message", "最早的回答", run_id="run-4"),
        ],
        "failed": [
            _event(0, "task_failed", "中断的请求：帮我", run_id="run-9"),
        ],
    }
    return status, histories


def _install(monkeypatch, *, connect_error=None):
    status, histories = _fixtures()
    client = _FakeClient(_FakeSessions(status, histories))
    factory = _FakeFactory(client, connect_error)
    monkeypatch.setattr(session_tools, "AsyncMemoryClient", factory)
    return factory, client


async def _call(**arguments) -> dict:
    return json.loads(await session_tools.session_search(CALL, **arguments))


@pytest.mark.asyncio
async def test_browse_lists_recent_sessions_newest_first(monkeypatch) -> None:
    factory, client = _install(monkeypatch)

    payload = await _call(limit=10)

    assert payload["success"] is True
    assert [item["session_id"] for item in payload["sessions"]] == [
        "chess",
        "trip",
        "old",
        "empty",
    ]
    assert payload["sessions"][0]["in_progress"] is True
    assert payload["sessions"][0]["last_active_at"] == 3000
    assert payload["sessions"][0]["last_active_at_display"] == "1970-01-01 08:00"
    kwargs = client.sessions.recent_calls[0]
    assert kwargs["selection_mode"] == "session_status"
    assert kwargs["exclude_session_id"] == "session-current"
    assert kwargs["session_limit"] == 20
    # BROWSE must not fetch any transcript.
    assert client.sessions.history_calls == []
    assert client.closed is True
    assert factory.connect_kwargs["account_id"] == "acct-1"

    default_limit = await _call()
    assert len(default_limit["sessions"]) == 3


@pytest.mark.asyncio
async def test_browse_entries_never_force_the_model_to_print_an_epoch(monkeypatch) -> None:
    """Every listed entry carries a quotable time next to the machine stamp."""

    _install(monkeypatch)

    payload = await _call(limit=10)

    for entry in payload["sessions"]:
        assert entry["last_active_at"] > 0
        expected = datetime.fromtimestamp(
            entry["last_active_at"] / 1000, timezone(timedelta(hours=8))
        ).strftime("%Y-%m-%d %H:%M")
        assert entry["last_active_at_display"] == expected


@pytest.mark.asyncio
async def test_browse_oldest_sort_and_limit_clamp(monkeypatch) -> None:
    _install(monkeypatch)

    payload = await _call(sort="oldest", limit=10)
    assert [item["session_id"] for item in payload["sessions"]] == [
        "empty",
        "old",
        "trip",
        "chess",
    ]

    assert len((await _call(limit=0))["sessions"]) == 1
    assert len((await _call(limit=99))["sessions"]) == 4


@pytest.mark.asyncio
async def test_discovery_matches_messages_newest_session_first(monkeypatch) -> None:
    _install(monkeypatch)

    payload = await _call(query="旅游")

    assert payload["matches"] == [
        {
            "session_id": "trip",
            "title": "旅游计划",
            "last_active_at": 2000,
            "last_active_at_display": "1970-01-01 08:00",
            "role": "user",
            "content": "帮我制定旅游计划",
        }
    ]


@pytest.mark.asyncio
async def test_discovery_includes_watermarks_and_honors_limit(monkeypatch) -> None:
    _install(monkeypatch)

    payload = await _call(query="帮我", limit=2)

    assert [item["session_id"] for item in payload["matches"]] == ["chess", "trip"]


@pytest.mark.asyncio
async def test_origin_query_returns_oldest_sessions_with_transcripts(monkeypatch) -> None:
    _install(monkeypatch)

    payload = await _call(query="最早的会话聊了什么", limit=2)

    assert payload["selection_reason"] == "oldest_session"
    assert [item["session_id"] for item in payload["sessions"]] == ["old", "trip"]


@pytest.mark.asyncio
async def test_read_returns_full_transcript_and_dedupes_watermark(monkeypatch) -> None:
    _install(monkeypatch)

    payload = await _call(session_id="trip")

    assert payload["messages"] == [
        {"role": "user", "content": "帮我制定旅游计划"},
        {"role": "assistant", "content": "好的，先看三天的行程"},
        {"role": "user", "content": "预算改成5000"},
        {"role": "assistant", "content": "已按预算调整"},
    ]


@pytest.mark.asyncio
async def test_read_keeps_unpaired_watermarks(monkeypatch) -> None:
    _, client = _install(monkeypatch)

    assert (await _call(session_id="chess"))["messages"] == [
        {"role": "user", "content": "帮我开发一个象棋游戏"}
    ]
    assert (await _call(session_id="failed"))["messages"] == [
        {"role": "user", "content": "中断的请求：帮我"}
    ]
    # READ must not fetch the status list.
    assert client.sessions.recent_calls == []
    assert client.sessions.history_calls[-1] == ("failed", 5000)


@pytest.mark.asyncio
async def test_read_unknown_session_returns_note(monkeypatch) -> None:
    _install(monkeypatch)

    payload = await _call(session_id="ghost")

    assert payload["success"] is True
    assert payload["messages"] == []
    assert "no recorded turns" in payload["note"]


@pytest.mark.asyncio
async def test_session_id_takes_precedence_over_query(monkeypatch) -> None:
    _, client = _install(monkeypatch)

    payload = await _call(session_id="old", query="旅游")

    assert payload["session_id"] == "old"
    assert client.sessions.recent_calls == []


@pytest.mark.asyncio
async def test_daemon_connect_failure_reports_unavailable(monkeypatch) -> None:
    _install(monkeypatch, connect_error=OSError("daemon down"))

    payload = await _call()

    assert payload["code"] == "memory_unavailable"
    assert "daemon down" in payload["error"]


@pytest.mark.asyncio
async def test_search_failure_reports_unavailable_and_closes_client(monkeypatch) -> None:
    _, client = _install(monkeypatch)

    async def _boom(**_kwargs):
        raise ValueError("corrupt response")

    client.sessions.get_recent_context = _boom  # type: ignore[method-assign]

    payload = await _call()

    assert payload["code"] == "memory_unavailable"
    assert client.closed is True
