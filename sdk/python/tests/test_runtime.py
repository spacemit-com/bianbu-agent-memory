from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from bianbu_agent_sdk.memory.errors import (
    ErrorContext,
    InvalidArgument,
    Unauthenticated,
)
from bianbu_agent_sdk.memory.runtime import (
    MemoryBridge,
    _contains_credential,
    _contains_pii,
    _usable_recall_text,
    session_evidence,
)
from bianbu_agent_sdk.memory.types import EvidenceRef, MemoryKind, SyncPolicy


def test_corrupted_semantic_recall_text_is_rejected() -> None:
    assert not _usable_recall_text("?????????")
    assert not _usable_recall_text("??????????? Rust")
    assert _usable_recall_text("终端颜色偏好为浅色")


class _LongTerm:
    def __init__(self) -> None:
        self.calls = []
        self.revise_calls = []
        self.records = []

    async def search_headers(self, query, top_k=100):
        del query, top_k
        return [
            SimpleNamespace(memory_id=record.memory_id)
            for record in self.records
        ]

    async def get_many(self, memory_ids):
        return [
            record for record in self.records if record.memory_id in memory_ids
        ]

    async def put(self, **arguments):
        self.calls.append(arguments)
        record = SimpleNamespace(
            memory_id=f"memory-{len(self.records) + 1}",
            kind=arguments["kind"],
            text=arguments["text"],
            summary=arguments.get("summary"),
            tags=tuple(arguments.get("tags", ())),
            revision=1,
            updated_at=len(self.records) + 1,
        )
        self.records.append(record)
        return SimpleNamespace(memory_id=record.memory_id, revision=1)

    async def revise(self, memory_id, **arguments):
        self.revise_calls.append((memory_id, arguments))
        record = next(item for item in self.records if item.memory_id == memory_id)
        record.text = arguments["text"]
        record.tags = tuple(arguments.get("tags") or record.tags)
        record.revision += 1
        record.updated_at += 1
        return SimpleNamespace(memory_id=memory_id, revision=record.revision)


class _Client:
    def __init__(self) -> None:
        self.long_term = _LongTerm()


@pytest.mark.parametrize(
    "value",
    (
        "remember my password is synthetic-secret",
        "请记住我的 API 密钥是 synthetic-secret",
        "SSH private key: synthetic-secret",
    ),
)
def test_sensitive_credential_detection(value: str) -> None:
    assert _contains_credential(value)


@pytest.mark.parametrize(
    "value",
    (
        "请记住我的手机号是 13800000000",
        "my email is synthetic@example.invalid",
        "家庭住址是测试路 1 号",
        "我叫测试用户",
        "my name is Test User",
    ),
)
def test_sensitive_pii_detection(value: str) -> None:
    assert _contains_pii(value)


@pytest.mark.asyncio
async def test_explicit_writes_in_one_run_have_content_scoped_idempotency_keys() -> None:
    bridge = MemoryBridge()
    client = _Client()

    async def client_or_none():
        return client

    bridge._client_or_none = client_or_none

    assert await bridge.put_explicit(
        content="用户偏好深色终端",
        target="user",
        run_id="run-1",
        session_id="session-1",
    )
    assert await bridge.put_explicit(
        content="用户习惯使用 Rust",
        target="user",
        run_id="run-1",
        session_id="session-1",
    )

    keys = [item["idempotency_key"] for item in client.long_term.calls]
    assert len(set(keys)) == 2
    assert all(key.startswith("personal-agent:") for key in keys)


@pytest.mark.parametrize(
    ("session_id", "expected"),
    (
        ("session-1", (EvidenceRef("session", "session-1"),)),
        ("  session_7b414fac  ", (EvidenceRef("session", "session_7b414fac"),)),
        ("01M2HJHWFP5VGQPDA0YWD99MJT", (EvidenceRef("session", "01M2HJHWFP5VGQPDA0YWD99MJT"),)),
        ("", ()),
        (None, ()),
        ("会话 一", ()),
        ("a" * 257, ()),
    ),
)
def test_session_evidence_requires_a_memoryd_identifier(session_id, expected) -> None:
    # memoryd answers ``invalid evidence references`` for anything outside its
    # identifier alphabet, so an unmanaged caller records without evidence
    # instead of failing the whole write.
    assert session_evidence(session_id) == expected


@pytest.mark.asyncio
async def test_explicit_write_without_a_session_skips_evidence() -> None:
    bridge = MemoryBridge()
    client = _Client()

    async def client_or_none():
        return client

    bridge._client_or_none = client_or_none

    result = await bridge.upsert_explicit(
        content="用户偏好深色终端",
        target="user",
        run_id="run-1",
        session_id="",
    )

    assert result["success"] is True
    assert client.long_term.calls[0]["evidence"] == ()


@pytest.mark.asyncio
async def test_explicit_write_reports_a_rejected_write_instead_of_hiding_it() -> None:
    class _Rejecting(_LongTerm):
        async def put(self, **arguments):
            raise InvalidArgument(
                ErrorContext(
                    code="InvalidArgument",
                    message="invalid evidence references",
                    request_id="request-1",
                    trace_id="trace-1",
                )
            )

    bridge = MemoryBridge()
    bridge._client = SimpleNamespace(long_term=_Rejecting())

    result = await bridge.upsert_explicit(
        content="工位号是 A-1234",
        target="user",
        run_id="run-1",
        session_id="session-1",
    )

    assert result["success"] is False
    assert result["code"] == "invalid_argument"
    assert "invalid evidence references" in str(result["error"])


@pytest.mark.asyncio
async def test_explicit_write_still_reports_an_unreachable_backend() -> None:
    class _Offline(_LongTerm):
        async def put(self, **arguments):
            raise ConnectionError("memory service socket is gone")

    bridge = MemoryBridge()
    bridge._client = SimpleNamespace(long_term=_Offline())

    result = await bridge.upsert_explicit(
        content="工位号是 A-1234",
        target="user",
        run_id="run-1",
        session_id="session-1",
    )

    assert result == {"success": False, "code": "backend_unavailable"}


@pytest.mark.asyncio
async def test_explicit_write_reports_a_refused_identity_instead_of_an_outage() -> None:
    """K3 2026-09-22: the add fallback dropped memoryd's own verdict."""

    bridge = MemoryBridge()

    async def client_or_none():
        return None

    bridge._client_or_none = client_or_none
    bridge._connect_error = Unauthenticated(
        ErrorContext(
            code="Unauthenticated",
            message=(
                "runtime account_id or agent_name does not match the trusted identity"
            ),
            request_id="request-1",
            trace_id="trace-1",
        )
    )

    result = await bridge.upsert_explicit(
        content="tea",
        target="user",
        run_id="run-1",
        session_id="session-1",
    )

    assert result["success"] is False
    assert result["code"] == "unauthenticated"
    assert "does not match the trusted identity" in result["error"]


@pytest.mark.asyncio
async def test_explicit_write_without_a_client_still_reports_backend_unavailable() -> None:
    bridge = MemoryBridge()

    async def client_or_none():
        return None

    bridge._client_or_none = client_or_none
    bridge._connect_error = FileNotFoundError("/run/bianbu/agent-memoryd.sock")

    result = await bridge.upsert_explicit(
        content="tea",
        target="user",
        run_id="run-1",
        session_id="session-1",
    )

    assert result == {"success": False, "code": "backend_unavailable"}


@pytest.mark.asyncio
async def test_exact_explicit_memory_is_not_duplicated_across_sessions() -> None:
    bridge = MemoryBridge()
    client = _Client()

    async def client_or_none():
        return client

    bridge._client_or_none = client_or_none

    for run_id, session_id in (("run-1", "session-1"), ("run-2", "session-2")):
        assert await bridge.put_explicit(
            content="常用浏览器为 Firefox。",
            target="user",
            run_id=run_id,
            session_id=session_id,
        )

    assert len(client.long_term.calls) == 1
    assert client.long_term.revise_calls == []


@pytest.mark.asyncio
async def test_explicit_browser_change_revises_stable_slot() -> None:
    bridge = MemoryBridge()
    client = _Client()

    async def client_or_none():
        return client

    bridge._client_or_none = client_or_none

    assert await bridge.put_explicit(
        content="常用浏览器为 Firefox。",
        target="user",
        run_id="run-1",
        session_id="session-1",
    )
    assert await bridge.put_explicit(
        content="常用浏览器为 Chromium（不是 Firefox）。",
        target="user",
        run_id="run-2",
        session_id="session-2",
    )

    assert len(client.long_term.calls) == 1
    assert len(client.long_term.revise_calls) == 1
    assert client.long_term.records[0].text == "常用浏览器为 Chromium（不是 Firefox）。"
    assert "slot:application.default_browser" in client.long_term.records[0].tags


@pytest.mark.asyncio
async def test_structured_recall_returns_safe_locator_and_dedupes_slot(
    monkeypatch,
) -> None:
    records = [
        SimpleNamespace(
            memory_id="terminal-dark-old",
            kind=MemoryKind.PROFILE,
            text="终端颜色方案偏好为深色。",
            tags=("slot:appearance.terminal_color_scheme",),
            revision=1,
            source="agent",
            updated_at=10,
        ),
        SimpleNamespace(
            memory_id="terminal-light-current",
            kind=MemoryKind.PREFERENCE,
            text="终端颜色方案偏好为浅色。",
            tags=("slot:appearance.terminal_color_scheme",),
            revision=2,
            source="agent",
            updated_at=20,
        ),
    ]

    class LongTerm:
        async def search_headers(self, query, top_k=5):
            del query, top_k
            return [
                SimpleNamespace(memory_id=record.memory_id, score=0.8)
                for record in records
            ]

        async def get_many(self, memory_ids):
            return [record for record in records if record.memory_id in memory_ids]

    monkeypatch.setenv("BIANBU_MEMORY_ENGINE", "scheme-a")
    bridge = MemoryBridge()
    bridge._client = SimpleNamespace(long_term=LongTerm())

    recalled = await bridge.recall_structured("终端颜色", top_k=5, max_chars=1000)

    assert len(recalled) == 1
    assert recalled[0].memory_id == "terminal-light-current"
    assert recalled[0].slot == "appearance.terminal_color_scheme"
    assert recalled[0].target == "user"
    assert recalled[0].to_dict()["kind"] == "preference"
    # Both the field slot and the important mark live only in the tags, so a
    # recall that drops them cannot be used to list what was marked.
    assert recalled[0].tags == ("slot:appearance.terminal_color_scheme",)
    assert recalled[0].to_dict()["tags"] == [
        "slot:appearance.terminal_color_scheme"
    ]


class _RefusingLongTerm(_LongTerm):
    """A backend that refuses this process' identity, as memoryd does."""

    async def search_headers(self, query, top_k=100):
        del query, top_k
        raise Unauthenticated(
            ErrorContext(
                code="Unauthenticated",
                message="runtime registration does not match the peer process",
                request_id="request-1",
                trace_id="trace-1",
            )
        )


@pytest.mark.asyncio
async def test_structured_recall_reports_a_refused_read_instead_of_no_match() -> None:
    bridge = MemoryBridge()
    bridge._client = SimpleNamespace(long_term=_RefusingLongTerm())

    outcome = await bridge.recall_structured_with_status("太奶")

    assert outcome["success"] is False
    assert outcome["code"] == "unauthenticated"
    assert "does not match the peer process" in str(outcome["error"])


@pytest.mark.asyncio
async def test_structured_recall_keeps_degrading_to_nothing_for_the_hooks() -> None:
    bridge = MemoryBridge()
    bridge._client = SimpleNamespace(long_term=_RefusingLongTerm())

    assert await bridge.recall_structured("太奶") == ()
    assert await bridge.recall("太奶") == ()


@pytest.mark.asyncio
async def test_structured_recall_reports_an_unreachable_service() -> None:
    bridge = MemoryBridge()

    async def client_or_none():
        return None

    bridge._client_or_none = client_or_none

    outcome = await bridge.recall_structured_with_status("太奶")

    assert outcome == {"success": False, "code": "backend_unavailable"}


@pytest.mark.asyncio
async def test_structured_recall_reports_no_match_only_when_answered() -> None:
    bridge = MemoryBridge()
    bridge._client = SimpleNamespace(long_term=_LongTerm())

    outcome = await bridge.recall_structured_with_status("太奶")

    assert outcome == {
        "success": True,
        "code": "no_match",
        "truncated": False,
        "items": (),
    }


def _profile_records(count: int, *, text_length: int = 12):
    return [
        SimpleNamespace(
            memory_id=f"memory-{index}",
            kind=MemoryKind.PROFILE,
            text="记" * text_length + f"-{index}",
            tags=(),
            revision=1,
            source="agent",
            updated_at=100 + index,
        )
        for index in range(count)
    ]


class _FixedLongTerm:
    """A backend that answers with every record it holds, whatever the window."""

    def __init__(self, records) -> None:
        self.records = list(records)

    async def search_headers(self, query, top_k=100):
        del query, top_k
        return [
            SimpleNamespace(memory_id=record.memory_id, score=0.8)
            for record in self.records
        ]

    async def get_many(self, memory_ids):
        return [record for record in self.records if record.memory_id in memory_ids]


def _fixed_bridge(records) -> MemoryBridge:
    bridge = MemoryBridge()
    bridge._client = SimpleNamespace(long_term=_FixedLongTerm(records))
    return bridge


@pytest.mark.asyncio
async def test_status_recall_reports_a_window_cut_at_the_record_ceiling() -> None:
    """The cut the Tool-level probe cannot see past, reported at its source.

    The probe asks for one entry past the window, so it goes blind once the
    window sits at the SDK ceiling (K3, 2026-09-28).  The bridge applies the
    ceiling itself and must still answer.
    """

    bridge = _fixed_bridge(_profile_records(21))

    outcome = await bridge.recall_structured_with_status(
        "太奶", top_k=20, max_chars=12000
    )

    assert len(outcome["items"]) == 20
    assert outcome["truncated"] is True


@pytest.mark.asyncio
async def test_status_recall_does_not_report_a_cut_for_a_full_store() -> None:
    """A window that exactly matches the store is not a truncated read."""

    bridge = _fixed_bridge(_profile_records(20))

    outcome = await bridge.recall_structured_with_status(
        "太奶", top_k=20, max_chars=12000
    )

    assert len(outcome["items"]) == 20
    assert outcome["truncated"] is False


@pytest.mark.asyncio
async def test_status_recall_reports_a_character_budget_cut() -> None:
    bridge = _fixed_bridge(_profile_records(4, text_length=200))

    outcome = await bridge.recall_structured_with_status(
        "太奶", top_k=20, max_chars=400
    )

    assert 0 < len(outcome["items"]) < 4
    assert outcome["truncated"] is True


@pytest.mark.asyncio
async def test_status_recall_ignores_a_duplicate_when_deciding_a_cut() -> None:
    """A record that dedupes away is not content the window left behind."""

    records = _profile_records(20)
    duplicate = SimpleNamespace(
        memory_id="memory-duplicate",
        kind=MemoryKind.PROFILE,
        text=records[0].text,
        tags=(),
        revision=1,
        source="agent",
        updated_at=1,
    )
    bridge = _fixed_bridge([*records, duplicate])

    outcome = await bridge.recall_structured_with_status(
        "太奶", top_k=20, max_chars=12000
    )

    assert len(outcome["items"]) == 20
    assert outcome["truncated"] is False


@pytest.mark.asyncio
async def test_slot_upsert_revises_newest_record_and_reconciles_duplicates() -> None:
    records = [
        SimpleNamespace(
            memory_id="terminal-dark-old",
            kind=MemoryKind.PROFILE,
            text="终端颜色方案偏好为深色。",
            summary=None,
            tags=("slot:appearance.terminal_color_scheme",),
            evidence=(),
            revision=1,
            updated_at=10,
        ),
        SimpleNamespace(
            memory_id="terminal-dark-newer",
            kind=MemoryKind.PREFERENCE,
            text="I prefer a dark terminal theme.",
            summary=None,
            tags=("slot:appearance.terminal_color_scheme",),
            evidence=(),
            revision=4,
            updated_at=20,
        ),
    ]

    class LongTerm:
        async def search_headers(self, query, top_k=100):
            del query, top_k
            return [SimpleNamespace(memory_id=record.memory_id) for record in records]

        async def get_many(self, memory_ids):
            return [record for record in records if record.memory_id in memory_ids]

        async def revise(self, memory_id, **arguments):
            record = next(item for item in records if item.memory_id == memory_id)
            record.text = arguments["text"]
            record.tags = tuple(arguments["tags"])
            record.revision += 1
            record.updated_at = 30
            return SimpleNamespace(memory_id=memory_id, revision=record.revision)

        async def reconcile_slots(self, *, slots, dry_run):
            assert slots == ["appearance.terminal_color_scheme"]
            assert dry_run is False
            winner = max(records, key=lambda item: (item.updated_at, item.revision))
            records[:] = [winner]
            return {"superseded_memories": 1}

        async def put(self, **arguments):
            raise AssertionError(f"slot upsert must revise, not insert: {arguments}")

    bridge = MemoryBridge()
    bridge._client = SimpleNamespace(long_term=LongTerm())

    result = await bridge.upsert_explicit(
        content="浅色",
        target="user",
        run_id="terminal-color-correction",
        session_id="session-color",
        slot="appearance.terminal_color_scheme",
    )

    assert result == {
        "success": True,
        "code": "slot_revised",
        "target": "user",
        "memory_id": "terminal-dark-newer",
        "revision": 5,
        "slot": "appearance.terminal_color_scheme",
        "duplicates_reconciled": 1,
    }
    assert len(records) == 1
    # MEM-L3-03: a declared field stores its canonical value, not the alias the
    # caller happened to type.
    assert records[0].text == "light"


@pytest.mark.asyncio
async def test_explicit_profile_pii_is_marked_sensitive() -> None:
    bridge = MemoryBridge()
    client = _Client()

    async def client_or_none():
        return client

    bridge._client_or_none = client_or_none

    assert await bridge.put_explicit(
        content="我叫测试用户",
        target="user",
        run_id="profile-run",
        session_id="profile-session",
    )

    assert client.long_term.calls[0]["sensitivity"] == "sensitive"


@pytest.mark.asyncio
async def test_explicit_user_memory_is_sensitive_even_without_pii_marker() -> None:
    bridge = MemoryBridge()
    client = _Client()

    async def client_or_none():
        return client

    bridge._client_or_none = client_or_none

    assert await bridge.put_explicit(
        content="用户长期资料",
        target="user",
        run_id="profile-generic-run",
        session_id="profile-generic-session",
    )

    assert client.long_term.calls[0]["sensitivity"] == "sensitive"


@pytest.mark.asyncio
async def test_recall_always_includes_explicit_profile_records(monkeypatch) -> None:
    record = SimpleNamespace(
        memory_id="profile-1",
        kind=MemoryKind.PROFILE,
        text="用户自称小圆",
        tags=("mem0-derived",),
        revision=1,
        source="agent",
    )

    class LongTerm:
        async def search_headers(self, query, top_k=5):
            del top_k
            return (
                [SimpleNamespace(memory_id="profile-1", score=0.0)]
                if not query
                else []
            )

        async def get_many(self, memory_ids):
            return [record] if "profile-1" in memory_ids else []

    monkeypatch.setenv("BIANBU_MEMORY_ENGINE", "scheme-a")
    bridge = MemoryBridge()
    bridge._client = SimpleNamespace(long_term=LongTerm())

    recalled = await bridge.recall("还记得我是谁吗", top_k=5, max_chars=1000)

    assert len(recalled) == 1
    assert "用户自称小圆" in recalled[0]


@pytest.mark.asyncio
async def test_an_unordered_read_carries_an_extracted_preference(monkeypatch) -> None:
    """91.67 (2026-09-30): "输出记忆" answered with one entry out of five.

    Automatic extraction tags its own writes "mem0-derived" and nothing else, and
    a read with no query is the only shape that can reach them, so keeping them
    out of that shape left a learned preference in the store and out of every
    listing the user could ask for.
    """

    records = [
        SimpleNamespace(
            memory_id="name-1",
            kind=MemoryKind.PROFILE,
            text="我是百岁太奶",
            tags=("personal-agent", "user", "slot:profile.user_name"),
            revision=6,
            source="agent",
            updated_at=20,
        ),
        SimpleNamespace(
            memory_id="style-1",
            kind=MemoryKind.PREFERENCE,
            text="请记住，以后回答我的问题要用简洁清晰易懂的话",
            tags=("mem0-derived",),
            revision=1,
            source="agent",
            updated_at=30,
        ),
    ]

    class LongTerm:
        async def search_headers(self, query, top_k=5):
            del query, top_k
            return [
                SimpleNamespace(memory_id=record.memory_id, score=0.0)
                for record in records
            ]

        async def get_many(self, memory_ids):
            return [record for record in records if record.memory_id in memory_ids]

    monkeypatch.setenv("BIANBU_MEMORY_ENGINE", "scheme-a")
    bridge = MemoryBridge()
    bridge._client = SimpleNamespace(long_term=LongTerm())

    outcome = await bridge.recall_structured_with_status(
        "", top_k=5, max_chars=1000, include_explicit=False
    )

    assert outcome["code"] == "recalled"
    assert [item.memory_id for item in outcome["items"]] == ["style-1", "name-1"]


@pytest.mark.asyncio
async def test_recent_context_returns_session_status_lines_without_turn_bodies() -> None:
    class Sessions:
        async def get_recent_context(self, **arguments):
            assert arguments == {
                "exclude_session_id": "session-new",
                "selection_mode": "session_status",
                "session_limit": 5,
            }
            return {
                "selection_mode": "session_status",
                "session_limit": 5,
                "sessions": [
                    {
                        "session_id": "session-chess",
                        "title": "帮我开发一个象棋游戏",
                        "last_active_at": int(time.time() * 1000) - 12 * 60 * 1000,
                        "completed_turn_count": 0,
                        "in_progress": True,
                        "interrupted": False,
                    },
                    {
                        "session_id": "session-travel",
                        "title": "规划下周旅游行程",
                        "last_active_at": int(time.time() * 1000) - 2 * 60 * 60 * 1000,
                        "completed_turn_count": 2,
                        "in_progress": False,
                        "interrupted": False,
                    },
                ],
            }

    bridge = MemoryBridge()
    bridge._client = SimpleNamespace(sessions=Sessions())

    context = await bridge.recent_context("session-new")

    assert context[0].startswith("[current_local_time=")
    status_lines = [item for item in context[1:]]
    assert any(
        "session-chess" in item and "帮我开发一个象棋游戏" in item and "进行中" in item
        for item in status_lines
    )
    assert any(
        "session-travel" in item and "规划下周旅游行程" in item and "2轮完成" in item
        for item in status_lines
    )
    assert all("分钟前活跃" in item or "小时前活跃" in item for item in status_lines)


@pytest.mark.asyncio
async def test_recent_context_degrades_to_header_only_on_legacy_daemon() -> None:
    class Sessions:
        async def get_recent_context(self, **arguments):
            # Old daemons do not know ``session_status`` and fall back to the
            # legacy response shape, which has no ``sessions`` key.
            return {
                "excluded_session_id": arguments.get("exclude_session_id"),
                "turn_limit": 5,
                "completed_turns": 0,
                "summaries": [],
                "messages": [
                    {
                        "session_id": "session-old",
                        "event_type": "user_message",
                        "payload": {"text": "旧 daemon 的轮次内容不应注入"},
                    }
                ],
            }

    bridge = MemoryBridge()
    bridge._client = SimpleNamespace(sessions=Sessions())

    context = await bridge.recent_context("session-new")

    assert len(context) == 1
    assert context[0].startswith("[current_local_time=")
    assert not any("旧 daemon 的轮次内容不应注入" in item for item in context)


@pytest.mark.asyncio
async def test_record_turn_delegates_summary_and_checkpoint_to_server() -> None:
    class Sessions:
        def __init__(self) -> None:
            self.events = []
            self.summary = None

        async def append_event(self, **arguments):
            self.events.append(arguments)
            return {"event_id": f"event-{len(self.events)}"}

        async def summarize(self, session_id, **arguments):
            self.summary = (session_id, arguments)

    class Runtime:
        def __init__(self) -> None:
            self.checkpoint = None

        async def prepare_checkpoint(self, session_id, **arguments):
            self.checkpoint = (session_id, arguments)

    class Coordinator:
        async def ingest_turn(self, *args, **kwargs):
            del args, kwargs

    class Privacy:
        def __init__(self) -> None:
            self.maintenance = []

        async def settings(self):
            return {"summary_interval_turns": 4, "recent_turn_limit": 8}

        async def run_maintenance(self, **arguments):
            self.maintenance.append(arguments)

    sessions = Sessions()
    runtime = Runtime()
    privacy = Privacy()
    bridge = MemoryBridge(coordinator=Coordinator())
    client = SimpleNamespace(sessions=sessions, runtime=runtime, privacy=privacy)

    async def client_or_none():
        return client

    bridge._client_or_none = client_or_none

    assert await bridge.record_turn(
        run_id="run-10",
        session_id="session-1",
        agent_id="agent-1",
        user_text="问题10",
        assistant_text="回答10",
    )

    assert sessions.summary is not None
    session_id, arguments = sessions.summary
    assert session_id == "session-1"
    assert arguments["interval_turns"] == 4
    assert arguments["keep_raw_turns"] == 8
    assert runtime.checkpoint is not None
    assert runtime.checkpoint[0] == "session-1"
    assert privacy.maintenance == [{"remove_expired": True}]
    assert [item["event_type"] for item in sessions.events] == [
        "user_message",
        "assistant_message",
    ]


@pytest.mark.asyncio
async def test_record_operation_event_writes_redacted_l2_event() -> None:
    class Sessions:
        def __init__(self) -> None:
            self.arguments = None

        async def append_event(self, **arguments):
            self.arguments = arguments
            return {"event_id": "operation-1"}

    sessions = Sessions()
    bridge = MemoryBridge()
    bridge._client = SimpleNamespace(sessions=sessions)

    async def client_or_none():
        return bridge._client

    bridge._client_or_none = client_or_none
    assert await bridge.record_operation_event(
        run_id="run-op",
        session_id="session-op",
        agent_id="agent-op",
        tool="terminal",
        command="echo hello",
        files=["/tmp/a.txt"],
    )
    assert sessions.arguments["event_type"] == "tool_result"
    assert sessions.arguments["payload"]["command"] == "echo hello"
    assert sessions.arguments["payload"]["files"] == ["/tmp/a.txt"]
    assert sessions.arguments["payload"]["eligible_for_context"] is True


@pytest.mark.asyncio
async def test_operation_event_idempotency_key_is_scoped_to_operation() -> None:
    class Sessions:
        def __init__(self) -> None:
            self.keys = []

        async def append_event(self, **arguments):
            self.keys.append(arguments["idempotency_key"])
            return {"event_id": str(len(self.keys))}

    sessions = Sessions()
    bridge = MemoryBridge()
    bridge._client = SimpleNamespace(sessions=sessions)

    async def client_or_none():
        return bridge._client

    bridge._client_or_none = client_or_none
    common = {
        "run_id": "run-op",
        "session_id": "session-op",
        "agent_id": "agent-op",
        "tool": "terminal",
    }
    assert await bridge.record_operation_event(**common, command="echo one", operation_id="call-1")
    assert await bridge.record_operation_event(**common, command="echo two", operation_id="call-2")
    assert sessions.keys[0] != sessions.keys[1]


@pytest.mark.asyncio
async def test_record_operation_event_omits_credential_assignments() -> None:
    class Sessions:
        def __init__(self) -> None:
            self.arguments = None

        async def append_event(self, **arguments):
            self.arguments = arguments
            return {"event_id": "operation-secret"}

    sessions = Sessions()
    bridge = MemoryBridge()
    bridge._client = SimpleNamespace(sessions=sessions)

    async def client_or_none():
        return bridge._client

    bridge._client_or_none = client_or_none
    assert await bridge.record_operation_event(
        run_id="run-secret",
        session_id="session-secret",
        agent_id="agent-secret",
        tool="terminal",
        command="TOKEN=do-not-store",
        files=["/tmp/API_KEY=do-not-store"],
        error="Authorization: do-not-store",
    )
    payload = sessions.arguments["payload"]
    assert "command" not in payload
    assert "files" not in payload
    assert "error" not in payload


@pytest.mark.asyncio
async def test_interrupted_turn_is_a_non_recallable_recovery_event() -> None:
    class Sessions:
        def __init__(self) -> None:
            self.event = None

        async def append_event(self, **arguments):
            self.event = arguments
            return {"event_id": "failed-event"}

    sessions = Sessions()
    class Runtime:
        def __init__(self) -> None:
            self.checkpoint = None

        async def prepare_checkpoint(self, session_id, **arguments):
            self.checkpoint = (session_id, arguments)

    runtime = Runtime()
    bridge = MemoryBridge()

    async def client_or_none():
        return SimpleNamespace(sessions=sessions, runtime=runtime)

    bridge._client_or_none = client_or_none

    assert await bridge.record_interrupted_turn(
        run_id="run-failed",
        session_id="session-1",
        agent_id="agent-1",
        user_text="continue the deployment",
        status="cancelled",
        error="user cancelled",
    )
    assert sessions.event["event_type"] == "task_failed"
    assert sessions.event["payload"] == {
        "run_id": "run-failed",
        "agent_id": "agent-1",
        "text": "continue the deployment",
        "status": "cancelled",
        "recovery_kind": "interrupted_turn",
        "eligible_for_context": False,
        "error": "user cancelled",
    }
    assert sessions.event["sync_policy"] is SyncPolicy.LOCAL_ONLY
    assert runtime.checkpoint is not None
    assert runtime.checkpoint[0] == "session-1"
    assert runtime.checkpoint[1]["idempotency_key"].startswith("personal-agent:")


@pytest.mark.asyncio
async def test_run_started_writes_non_recallable_watermark() -> None:
    class Sessions:
        def __init__(self) -> None:
            self.event = None

        async def append_event(self, **arguments):
            self.event = arguments
            return {"event_id": "started-event"}

    sessions = Sessions()
    bridge = MemoryBridge()
    bridge._client = SimpleNamespace(sessions=sessions)

    async def client_or_none():
        return bridge._client

    bridge._client_or_none = client_or_none
    assert await bridge.record_run_started(
        run_id="run-chess",
        session_id="session-chess",
        agent_id="agent-chess",
        user_text="帮我开发一个象棋游戏" * 500,
    )
    event = sessions.event
    assert event["session_id"] == "session-chess"
    assert event["event_type"] == "task_started"
    assert event["sync_policy"] is SyncPolicy.LOCAL_ONLY
    assert event["idempotency_key"].startswith("personal-agent:")
    assert event["idempotency_key"].endswith(":run-started")
    assert event["payload"]["run_id"] == "run-chess"
    assert event["payload"]["status"] == "started"
    assert event["payload"]["eligible_for_context"] is False
    assert len(event["payload"]["text"].encode("utf-8")) <= 2000
    assert event["payload"]["text"]


@pytest.mark.asyncio
async def test_pii_uses_encrypted_canonical_extraction_without_dplus_projection(
    monkeypatch,
) -> None:
    class Sessions:
        async def append_event(self, **arguments):
            return {"event_id": arguments["event_type"]}

        async def summarize(self, *args, **kwargs):
            return None

    class Runtime:
        async def prepare_checkpoint(self, *args, **kwargs):
            return None

    class Privacy:
        async def run_maintenance(self, **kwargs):
            return None

    class Projection:
        def __init__(self) -> None:
            self.calls = []

        async def enqueue_turn(self, **arguments):
            self.calls.append(arguments)

    class Coordinator:
        def __init__(self) -> None:
            self.calls = []

        async def ingest_turn(self, *args, **kwargs):
            self.calls.append(kwargs)

    monkeypatch.setenv("BIANBU_MEMORY_ENGINE", "dplus")
    coordinator = Coordinator()
    projection = Projection()
    bridge = MemoryBridge(coordinator=coordinator)
    bridge._client = SimpleNamespace(
        sessions=Sessions(), runtime=Runtime(), privacy=Privacy(), projection=projection
    )

    assert await bridge.record_turn(
        run_id="pii-run",
        session_id="pii-session",
        agent_id="agent-1",
        user_text="我叫测试用户",
        assistant_text="你好，测试用户",
    )

    assert projection.calls == []
    assert coordinator.calls[0]["sensitivity"] == "sensitive"


@pytest.mark.asyncio
async def test_credentials_are_not_extracted_or_projected(monkeypatch) -> None:
    class Sessions:
        async def append_event(self, **arguments):
            return {"event_id": arguments["event_type"]}

        async def summarize(self, *args, **kwargs):
            return None

    class Runtime:
        async def prepare_checkpoint(self, *args, **kwargs):
            return None

    class Privacy:
        async def run_maintenance(self, **kwargs):
            return None

    class Projection:
        def __init__(self) -> None:
            self.calls = []

        async def enqueue_turn(self, **arguments):
            self.calls.append(arguments)

    class Coordinator:
        def __init__(self) -> None:
            self.calls = []

        async def ingest_turn(self, *args, **kwargs):
            self.calls.append(kwargs)

    monkeypatch.setenv("BIANBU_MEMORY_ENGINE", "dplus")
    coordinator = Coordinator()
    projection = Projection()
    bridge = MemoryBridge(coordinator=coordinator)
    bridge._client = SimpleNamespace(
        sessions=Sessions(), runtime=Runtime(), privacy=Privacy(), projection=projection
    )

    assert await bridge.record_turn(
        run_id="secret-run",
        session_id="secret-session",
        agent_id="agent-1",
        user_text="remember my password is synthetic-secret",
        assistant_text="I will not retain credentials",
    )

    assert projection.calls == []
    assert coordinator.calls == []


@pytest.mark.asyncio
async def test_explicit_write_refuses_an_invented_slot() -> None:
    # MEM-L3-02: a self-made slot such as ``profile.explanation_style`` must be
    # refused, and the refusal must name the authoritative slots.
    bridge = MemoryBridge()
    client = _Client()
    bridge._client = SimpleNamespace(long_term=client.long_term)

    result = await bridge.upsert_explicit(
        content="以后跟我解释请用大白话",
        target="user",
        run_id="run-1",
        session_id="session-1",
        slot="profile.explanation_style",
    )

    assert result["success"] is False
    assert result["code"] == "invalid_slot"
    assert "profile.explanation_style" in result["error"]
    assert "profile.user_name" in result["known_slots"]
    assert client.long_term.calls == []


@pytest.mark.asyncio
async def test_explicit_write_resolves_a_slot_alias_to_the_authoritative_name() -> None:
    bridge = MemoryBridge()
    client = _Client()
    bridge._client = SimpleNamespace(long_term=client.long_term)

    result = await bridge.upsert_explicit(
        content="常用编辑器是 VSCode",
        target="user",
        run_id="run-1",
        session_id="session-1",
        slot="编辑器",
    )

    assert result["success"] is True
    assert result["slot"] == "development.editor"
    assert "slot:development.editor" in client.long_term.calls[0]["tags"]


@pytest.mark.asyncio
async def test_explicit_write_keeps_a_slot_less_memory_writable() -> None:
    # The recommendation keeps the "free memory" exit open, so a preference that
    # has no dictionary field is still storable without a slot.
    bridge = MemoryBridge()
    client = _Client()
    bridge._client = SimpleNamespace(long_term=client.long_term)

    result = await bridge.upsert_explicit(
        content="调试时先把问题稳定复现再改代码",
        target="memory",
        run_id="run-1",
        session_id="session-1",
    )

    assert result["success"] is True
    assert "slot" not in result
    assert not any(
        str(tag).startswith("slot:") for tag in client.long_term.calls[0]["tags"]
    )


@pytest.mark.asyncio
async def test_explicit_write_enforces_the_durable_text_budget() -> None:
    bridge = MemoryBridge()
    client = _Client()
    bridge._client = SimpleNamespace(long_term=client.long_term)

    bloated = await bridge.upsert_explicit(
        content="字" * 2201,
        target="memory",
        run_id="run-1",
        session_id="session-1",
    )
    profile_bloated = await bridge.upsert_explicit(
        content="字" * 1376,
        target="user",
        run_id="run-1",
        session_id="session-1",
    )

    assert bloated == {
        "success": False,
        "code": "content_too_long",
        "limit": 2200,
        "length": 2201,
    }
    assert profile_bloated["code"] == "content_too_long"
    assert profile_bloated["limit"] == 1375
    assert client.long_term.calls == []


@pytest.mark.asyncio
async def test_explicit_write_rejects_control_characters() -> None:
    bridge = MemoryBridge()
    client = _Client()
    bridge._client = SimpleNamespace(long_term=client.long_term)

    result = await bridge.upsert_explicit(
        content="用户偏好\u0000深色终端",
        target="user",
        run_id="run-1",
        session_id="session-1",
    )

    assert result["code"] == "invalid_content"
    assert client.long_term.calls == []


@pytest.mark.asyncio
async def test_explicit_slot_write_canonicalizes_a_declared_field_value() -> None:
    # MEM-L3-03: naming a slot asserts the text is that field's value, so a
    # registered alias is stored in its canonical form.
    bridge = MemoryBridge()
    client = _Client()
    bridge._client = SimpleNamespace(long_term=client.long_term)

    result = await bridge.upsert_explicit(
        content=" 深色 ",
        target="user",
        run_id="run-1",
        session_id="session-1",
        slot="终端配色",
    )

    assert result["success"] is True
    assert result["slot"] == "appearance.terminal_color_scheme"
    assert client.long_term.calls[0]["text"] == "dark"


@pytest.mark.asyncio
async def test_explicit_slot_write_refuses_prose_that_claims_a_declared_field() -> None:
    # MEM-L3-03: the dictionary's declarations are enforced once a slot is
    # named; the sentence itself stays storable without a slot.
    bridge = MemoryBridge()
    client = _Client()
    bridge._client = SimpleNamespace(long_term=client.long_term)

    result = await bridge.upsert_explicit(
        content="用户喜欢的终端颜色是浅色（light），不是深色。",
        target="user",
        run_id="run-1",
        session_id="session-1",
        slot="appearance.terminal_color_scheme",
    )

    assert result["success"] is False
    assert result["code"] == "invalid_value"
    assert result["slot"] == "appearance.terminal_color_scheme"
    assert result["allowed_values"] == ["light", "dark", "system"]
    assert result["max_length"] == 16
    assert "assistant_instruction" in result
    assert client.long_term.calls == []


@pytest.mark.asyncio
async def test_explicit_slot_write_refuses_a_value_outside_the_field_domain() -> None:
    bridge = MemoryBridge()
    client = _Client()
    bridge._client = SimpleNamespace(long_term=client.long_term)

    result = await bridge.upsert_explicit(
        content="unknown-manager",
        target="user",
        run_id="run-1",
        session_id="session-1",
        slot="development.package_manager",
    )

    assert result["code"] == "invalid_value"
    assert "uv" in result["allowed_values"]
    assert client.long_term.calls == []


@pytest.mark.asyncio
async def test_explicit_slot_write_enforces_the_declared_field_length() -> None:
    # `development.shell max_length=64` used to be inert documentation.
    bridge = MemoryBridge()
    client = _Client()
    bridge._client = SimpleNamespace(long_term=client.long_term)

    result = await bridge.upsert_explicit(
        content="z" * 65,
        target="user",
        run_id="run-1",
        session_id="session-1",
        slot="development.shell",
    )

    assert result["code"] == "invalid_value"
    assert result["max_length"] == 64
    assert client.long_term.calls == []


@pytest.mark.asyncio
async def test_non_name_pii_is_not_extracted_or_projected(monkeypatch) -> None:
    class Sessions:
        async def append_event(self, **arguments):
            return {"event_id": arguments["event_type"]}

        async def summarize(self, *args, **kwargs):
            return None

    class Runtime:
        async def prepare_checkpoint(self, *args, **kwargs):
            return None

    class Privacy:
        async def run_maintenance(self, **kwargs):
            return None

    class Projection:
        def __init__(self) -> None:
            self.calls = []

        async def enqueue_turn(self, **arguments):
            self.calls.append(arguments)

    class Coordinator:
        def __init__(self) -> None:
            self.calls = []

        async def ingest_turn(self, *args, **kwargs):
            self.calls.append(kwargs)

    monkeypatch.setenv("BIANBU_MEMORY_ENGINE", "dplus")
    coordinator = Coordinator()
    projection = Projection()
    bridge = MemoryBridge(coordinator=coordinator)
    bridge._client = SimpleNamespace(
        sessions=Sessions(), runtime=Runtime(), privacy=Privacy(), projection=projection
    )

    assert await bridge.record_turn(
        run_id="email-run",
        session_id="email-session",
        agent_id="agent-1",
        user_text="remember my email is synthetic@example.invalid",
        assistant_text="I will keep it out of durable extraction",
    )

    assert projection.calls == []
    assert coordinator.calls == []

@pytest.mark.asyncio
async def test_recall_drops_a_vector_only_hit_below_the_cosine_floor(monkeypatch) -> None:
    # B2: the store reports RRF in "score" and cosine in "relevance"; a low
    # cosine vector-only hit must not be injected, whatever its RRF rank.
    relevant = SimpleNamespace(
        memory_id="relevant-1",
        kind=MemoryKind.FACT,
        text="部署流水线使用 AZURE-7391",
        tags=(),
        revision=1,
        source="agent",
    )
    irrelevant = SimpleNamespace(
        memory_id="irrelevant-1",
        kind=MemoryKind.FACT,
        text="Alpha7413 的会议记录",
        tags=(),
        revision=1,
        source="agent",
    )

    class LongTerm:
        async def search_headers(self, query, top_k=5):
            del query, top_k
            return [
                SimpleNamespace(memory_id="relevant-1", score=0.021, relevance=0.68),
                SimpleNamespace(memory_id="irrelevant-1", score=0.015, relevance=0.02),
            ]

        async def get_many(self, memory_ids):
            return [
                item
                for item in (relevant, irrelevant)
                if item.memory_id in memory_ids
            ]

    monkeypatch.setenv("BIANBU_MEMORY_ENGINE", "scheme-a")
    bridge = MemoryBridge()
    bridge._client = SimpleNamespace(long_term=LongTerm())

    recalled = await bridge.recall("AZURE-7391", top_k=5, max_chars=1000)

    assert len(recalled) == 1
    assert "AZURE-7391" in recalled[0]


@pytest.mark.asyncio
async def test_recall_keeps_a_keyword_hit_whatever_its_rrf_score(monkeypatch) -> None:
    record = SimpleNamespace(
        memory_id="keyword-1",
        kind=MemoryKind.FACT,
        text="终端配色偏好为深色",
        tags=(),
        revision=1,
        source="agent",
    )

    class LongTerm:
        async def search_headers(self, query, top_k=5):
            del query, top_k
            # A keyword hit keeps relevance 1.0 although its RRF score is tiny.
            return [SimpleNamespace(memory_id="keyword-1", score=0.016, relevance=1.0)]

        async def get_many(self, memory_ids):
            return [record] if "keyword-1" in memory_ids else []

    monkeypatch.setenv("BIANBU_MEMORY_ENGINE", "scheme-a")
    bridge = MemoryBridge()
    bridge._client = SimpleNamespace(long_term=LongTerm())

    recalled = await bridge.recall("终端配色", top_k=5, max_chars=1000)

    assert len(recalled) == 1
    assert "深色" in recalled[0]
