"""The bianbu.memory MCP surface keeps the Tool contract of the retired plugin.

The Runtime supplies no identity to an MCP child, so these tests pin both
sources of context: the ``_meta`` host context attached to every call and the
Worker-attested registration that memoryd authenticates by SO_PEERCRED.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import threading
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")

from bianbu_agent_sdk.memory.errors import (
    ErrorContext,
    ServerUnavailable,
    Unauthenticated,
)
from bianbu_agent_sdk.memory.mcp_server import sessions as session_tools
from bianbu_agent_sdk.memory.mcp_server import tools as memory_tools
from bianbu_agent_sdk.memory.mcp_server.context import (
    HostContext,
    WorkerRegistration,
    host_context,
)
from bianbu_agent_sdk.memory.mcp_server.server import create_server
from bianbu_agent_sdk.memory.runtime import MemoryBridge
from bianbu_agent_sdk.memory.types import MemoryKind

HOST = HostContext(
    session_id="session-1", run_id="run-1", agent_id="agent-1", account_id="account-1"
)


def test_server_exposes_the_memory_tools_with_runtime_annotations() -> None:
    server = create_server()
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}

    assert set(tools) == {"memory", "memory_record", "memory_retrieve", "session_search"}
    assert tools["memory_retrieve"].annotations.readOnlyHint is True
    assert tools["session_search"].annotations.readOnlyHint is True
    # A blanket destructiveHint would make the Runtime require approval for
    # every save; only the remove action is gated, and the Runtime owns it.
    assert tools["memory"].annotations.destructiveHint is False
    assert tools["memory_record"].annotations.readOnlyHint is False
    schema = tools["memory"].inputSchema
    assert "target" in schema["properties"]
    assert "operations" in schema["properties"]


def test_memory_retrieve_documents_the_scoped_read() -> None:
    """91.67 (2026-09-30): "输出所有记忆" arrived as a query-scoped search.

    Nothing in the contract said that a non-empty query narrows the read, so the
    model invented one and the answer was reported as the whole store while it
    was one entry out of five.
    """

    tools = {tool.name: tool for tool in asyncio.run(create_server().list_tools())}
    description = tools["memory_retrieve"].description or ""
    parameter = tools["memory_retrieve"].inputSchema["properties"]["query"]

    assert "SCOPED READ" in description
    assert "no `query`" in description
    assert "narrows the read to the entries that match it" in parameter["description"]


def test_host_context_is_read_from_the_bianbu_meta_namespace() -> None:
    meta = {"bianbu/host": {
        "session_id": "session-1",
        "run_id": "run-1",
        "agent_id": "agent-1",
        "account_id": "account-1",
        "ignored": "value",
    }}
    assert host_context(meta) == HOST
    assert host_context({}) == HostContext()
    assert host_context(None) == HostContext()


def test_host_context_is_read_from_the_fastmcp_meta_model() -> None:
    """FastMCP hands the tools a pydantic ``Meta``, not the raw mapping.

    The extra ``bianbu/host`` namespace lives in ``model_extra`` while the model
    itself is not a ``Mapping``, so a mapping-only reader drops every field and
    session-scoped writes lose their session ID.
    """

    class _Meta:
        def __init__(self, **extra) -> None:
            self.model_extra = dict(extra)

    meta = _Meta(
        progressToken="3",
        **{
            "bianbu/host": {
                "session_id": "session-1",
                "run_id": "run-1",
                "agent_id": "agent-1",
                "account_id": "account-1",
            }
        },
    )

    assert host_context(meta) == HOST
    assert host_context(_Meta(progressToken="3")) == HostContext()


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="holding a broker registration needs a Unix-domain socket",
)
def test_worker_registration_holds_the_broker_connection(tmp_path) -> None:
    socket_path = tmp_path / "worker.sock"
    accepted: list[str] = []
    dropped: list[bool] = []
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(1)

    def serve() -> None:
        connection, _ = server.accept()
        with connection:
            accepted.append(connection.recv(32).decode())
            connection.sendall(b"ok\n")
            connection.settimeout(2)
            try:
                # The broker deletes the registry entry on EOF, so an early
                # close silently unregisters the child that just registered.
                dropped.append(connection.recv(32) == b"")
            except TimeoutError:
                dropped.append(False)

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    registration = WorkerRegistration(str(socket_path))
    try:
        assert registration.open() is True
        assert registration.active is True
        assert accepted == ["register\n"]
        worker.join(timeout=5)
        assert dropped == [False]
    finally:
        registration.close()
        server.close()
    assert registration.active is False


def test_registration_is_skipped_without_a_broker(monkeypatch) -> None:
    monkeypatch.delenv("BIANBU_MEMORY_REGISTRATION_SOCKET", raising=False)
    registration = WorkerRegistration()
    assert registration.open() is False
    registration.close()


class _FakeBridge:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def upsert_explicit(self, **kwargs):
        self.calls.append(kwargs)
        return {"success": True, "code": "recorded", "target": kwargs["target"]}

    async def recall_structured(self, query, **kwargs):
        self.calls.append({"query": query, **kwargs})
        return ({"memory_id": "m-1", "kind": "fact", "target": "memory", "text": "tea"},)


@pytest.mark.asyncio
async def test_memory_add_uses_the_host_context_for_evidence(monkeypatch) -> None:
    bridge = _FakeBridge()

    async def fake_bridge(host, timeout=None):
        assert host == HOST
        return bridge

    monkeypatch.setattr(memory_tools, "bridge", fake_bridge)
    result = json.loads(
        await memory_tools.memory(HOST, target="user", action="add", content="tea")
    )

    assert result == {"success": True, "code": "recorded", "target": "user"}
    assert bridge.calls == [
        {
            "content": "tea",
            "target": "user",
            "run_id": "run-1",
            "session_id": "session-1",
            "slot": None,
        }
    ]


def _refusal() -> Unauthenticated:
    return Unauthenticated(
        ErrorContext(
            code="Unauthenticated",
            message=(
                "runtime account_id or agent_name does not match the trusted identity"
            ),
            request_id="request-1",
            trace_id="trace-1",
        )
    )


@pytest.mark.asyncio
async def test_memory_mutation_reports_a_refused_write(monkeypatch) -> None:
    """A refused write keeps memoryd's code instead of looking like an outage."""

    async def refusing_manage(host, target, arguments):
        del host, target, arguments
        raise _refusal()

    monkeypatch.setattr(memory_tools, "_manage", refusing_manage)
    payload = json.loads(
        await memory_tools.memory(
            HOST, target="user", action="replace", content="tea", memory_id="m-1"
        )
    )

    assert payload["success"] is False
    assert payload["code"] == "unauthenticated"
    assert "does not match the trusted identity" in payload["error"]


@pytest.mark.asyncio
async def test_memory_mutation_keeps_backend_unavailable_for_a_dead_service(
    monkeypatch,
) -> None:
    async def unavailable_manage(host, target, arguments):
        del host, target, arguments
        raise ServerUnavailable(
            ErrorContext(
                code="ServerUnavailable",
                message="memoryd is restarting",
                request_id="request-1",
                trace_id="trace-1",
                retryable=True,
            )
        )

    monkeypatch.setattr(memory_tools, "_manage", unavailable_manage)
    payload = json.loads(
        await memory_tools.memory(
            HOST, target="user", action="replace", content="tea", memory_id="m-1"
        )
    )

    assert payload["success"] is False
    assert payload["code"] == "backend_unavailable"
    assert payload["error"].startswith("AgentMemory is unavailable")


@pytest.mark.asyncio
async def test_memory_mutation_names_the_transport_error(monkeypatch) -> None:
    async def broken_manage(host, target, arguments):
        del host, target, arguments
        raise FileNotFoundError("/run/bianbu/agent-memoryd.sock")

    monkeypatch.setattr(memory_tools, "_manage", broken_manage)
    payload = json.loads(
        await memory_tools.memory(HOST, target="user", action="add", content="tea")
    )

    assert payload["success"] is False
    assert payload["code"] == "backend_unavailable"
    assert "FileNotFoundError" in payload["error"]


@pytest.mark.asyncio
async def test_memory_record_reports_the_backend_verdict(monkeypatch) -> None:
    class _RejectingBridge:
        async def upsert_explicit(self, **kwargs):
            del kwargs
            return {
                "success": False,
                "code": "invalid_argument",
                "error": "invalid evidence references",
            }

    async def fake_bridge(host, timeout=None):
        return _RejectingBridge()

    monkeypatch.setattr(memory_tools, "bridge", fake_bridge)

    assert (
        await memory_tools.memory_record(HOST, content="工位号是 A-1234")
        == "AgentMemory could not record the memory (invalid evidence references)"
    )


@pytest.mark.asyncio
async def test_memory_retrieve_returns_structured_recall(monkeypatch) -> None:
    bridge = _FakeBridge()

    async def fake_bridge(host, timeout=None):
        return bridge

    monkeypatch.setattr(memory_tools, "bridge", fake_bridge)
    payload = json.loads(await memory_tools.memory_retrieve(HOST, query="tea"))

    assert payload["code"] == "recalled"
    assert payload["count"] == 1
    assert payload["has_more"] is False
    assert payload["memories"] == [
        {"memory_id": "m-1", "kind": "fact", "target": "memory", "text": "tea"}
    ]
    assert bridge.calls == [
        {"query": "tea", "top_k": 6, "max_chars": 4000, "include_explicit": False}
    ]


class _WindowBridge:
    """Hand back at most the caller's window, out of a store of a fixed size."""

    def __init__(self, stored: int) -> None:
        self.stored = stored
        self.windows: list[int] = []

    async def recall_structured(self, query, **kwargs):
        del query
        window = int(kwargs["top_k"])
        self.windows.append(window)
        return tuple(
            {
                "memory_id": f"m-{index}",
                "kind": "profile",
                "target": "user",
                "text": f"note {index}",
            }
            for index in range(min(self.stored, window))
        )


async def _windowed_payload(monkeypatch, bridge: _WindowBridge) -> dict:
    async def fake_bridge(host, timeout=None):
        return bridge

    monkeypatch.setattr(memory_tools, "bridge", fake_bridge)
    return json.loads(await memory_tools.memory_retrieve(HOST, query=""))


@pytest.mark.asyncio
async def test_memory_retrieve_says_when_the_window_was_cut(monkeypatch) -> None:
    bridge = _WindowBridge(stored=9)
    payload = await _windowed_payload(monkeypatch, bridge)

    # One entry past the window is the probe that proves the store holds more.
    assert bridge.windows == [6]
    assert payload["count"] == 5
    assert payload["has_more"] is True
    assert [item["memory_id"] for item in payload["memories"]] == [
        f"m-{index}" for index in range(5)
    ]


@pytest.mark.asyncio
async def test_memory_retrieve_does_not_invent_entries_beyond_the_store(
    monkeypatch,
) -> None:
    bridge = _WindowBridge(stored=5)
    payload = await _windowed_payload(monkeypatch, bridge)

    # A store that happens to be exactly as long as the window holds nothing more.
    assert bridge.windows == [6]
    assert payload["count"] == 5
    assert payload["has_more"] is False


@pytest.mark.asyncio
async def test_memory_retrieve_probes_past_a_configured_window(monkeypatch) -> None:
    monkeypatch.setenv("BIANBU_MEMORY_RECALL_TOP_K", "3")
    bridge = _WindowBridge(stored=9)
    payload = await _windowed_payload(monkeypatch, bridge)

    assert bridge.windows == [4]
    assert payload["count"] == 3
    assert payload["has_more"] is True


@pytest.mark.asyncio
async def test_memory_retrieve_reports_the_cut_through_the_status_bridge(
    monkeypatch,
) -> None:
    """The production bridge answers with status, not with a bare tuple."""

    class _StatusBridge:
        def __init__(self) -> None:
            self.windows: list[int] = []

        async def recall_structured_with_status(self, query, **kwargs):
            del query
            window = int(kwargs["top_k"])
            self.windows.append(window)
            return {
                "success": True,
                "code": "recalled",
                "items": tuple(
                    {
                        "memory_id": f"m-{index}",
                        "kind": "profile",
                        "target": "user",
                        "text": f"note {index}",
                    }
                    for index in range(min(9, window))
                ),
            }

    bridge = _StatusBridge()

    async def fake_bridge(host, timeout=None):
        return bridge

    monkeypatch.setattr(memory_tools, "bridge", fake_bridge)
    payload = json.loads(await memory_tools.memory_retrieve(HOST, query=""))

    assert bridge.windows == [6]
    assert payload["code"] == "recalled"
    assert payload["count"] == 5
    assert payload["has_more"] is True


@pytest.mark.asyncio
async def test_memory_retrieve_trusts_the_bridge_when_the_probe_cannot_see_the_cut(
    monkeypatch,
) -> None:
    """A short window over a longer store: the probe alone would say "that is all".

    The probe works by asking for one entry past the window, so it goes blind
    once the window sits at the SDK ceiling.  The bridge applies that ceiling
    itself, so its verdict has to win (K3, 2026-09-28).
    """

    class _TruncatedBridge:
        async def recall_structured_with_status(self, query, **kwargs):
            del query, kwargs
            return {
                "success": True,
                "code": "recalled",
                "truncated": True,
                "items": tuple(
                    {
                        "memory_id": f"m-{index}",
                        "kind": "profile",
                        "target": "user",
                        "text": f"note {index}",
                    }
                    for index in range(3)
                ),
            }

    async def fake_bridge(host, timeout=None):
        return _TruncatedBridge()

    monkeypatch.setattr(memory_tools, "bridge", fake_bridge)
    payload = json.loads(await memory_tools.memory_retrieve(HOST, query="太奶"))

    assert payload["count"] == 3
    assert payload["has_more"] is True


@pytest.mark.asyncio
async def test_memory_retrieve_caps_the_configured_window(monkeypatch) -> None:
    """100 would promise records the SDK ceiling cannot serve."""

    monkeypatch.setenv("BIANBU_MEMORY_RECALL_TOP_K", "100")
    bridge = _WindowBridge(stored=9)
    payload = await _windowed_payload(monkeypatch, bridge)

    assert bridge.windows == [21]
    assert payload["count"] == 9
    assert payload["has_more"] is False


@pytest.mark.asyncio
async def test_memory_retrieve_redirects_session_history_questions(monkeypatch) -> None:
    async def unexpected(host, timeout=None):  # pragma: no cover - must not run
        raise AssertionError("session history questions must not hit AgentMemory")

    monkeypatch.setattr(memory_tools, "bridge", unexpected)
    result = await memory_tools.memory_retrieve(HOST, query="第一个会话聊了什么")

    assert "session_search" in result


@pytest.mark.asyncio
async def test_memory_retrieve_reports_a_refused_read_instead_of_no_match(
    monkeypatch,
) -> None:
    class _RefusingBridge:
        async def recall_structured_with_status(self, query, **kwargs):
            del query, kwargs
            return {
                "success": False,
                "code": "unauthenticated",
                "error": (
                    "Unauthenticated: runtime registration does not match "
                    "the peer process"
                ),
            }

    async def fake_bridge(host, timeout=None):
        return _RefusingBridge()

    monkeypatch.setattr(memory_tools, "bridge", fake_bridge)
    payload = json.loads(await memory_tools.memory_retrieve(HOST, query="太奶"))

    assert payload["success"] is False
    assert payload["code"] == "unauthenticated"
    assert "does not match the peer process" in payload["error"]
    assert payload["query"] == "太奶"


@pytest.mark.asyncio
async def test_memory_retrieve_keeps_no_match_for_a_bridge_without_status(
    monkeypatch,
) -> None:
    class _EmptyBridge:
        async def recall_structured(self, query, **kwargs):
            del query, kwargs
            return ()

    async def fake_bridge(host, timeout=None):
        return _EmptyBridge()

    monkeypatch.setattr(memory_tools, "bridge", fake_bridge)
    payload = json.loads(await memory_tools.memory_retrieve(HOST, query="太奶"))

    assert payload["success"] is True
    assert payload["code"] == "no_match"
    assert payload["count"] == 0


class _SlotLongTerm:
    """One slot-tagged record, enough to drive the MemoryBridge write path."""

    def __init__(self) -> None:
        self.put_calls: list[dict[str, object]] = []
        self.revise_calls: list[tuple[str, dict[str, object]]] = []
        self.records = [
            SimpleNamespace(
                memory_id="terminal-dark",
                kind=MemoryKind.PREFERENCE,
                text="终端颜色方案偏好为深色。",
                summary=None,
                tags=("slot:appearance.terminal_color_scheme",),
                evidence=(),
                revision=1,
                updated_at=1,
            )
        ]

    async def search_headers(self, query, top_k=100):
        del query, top_k
        return [SimpleNamespace(memory_id=record.memory_id) for record in self.records]

    async def get_many(self, memory_ids):
        return [record for record in self.records if record.memory_id in memory_ids]

    async def revise(self, memory_id, **arguments):
        self.revise_calls.append((memory_id, arguments))
        return SimpleNamespace(memory_id=memory_id, revision=2)

    async def put(self, **arguments):
        self.put_calls.append(arguments)
        return SimpleNamespace(memory_id="memory-new", revision=1)

    async def reconcile_slots(self, *, slots, dry_run):
        del slots, dry_run
        return {"superseded_memories": 0}


def _use_stub_bridge(monkeypatch) -> _SlotLongTerm:
    """Drive the real MemoryBridge (and its MEM-L3-03 gate) from the Tool.

    The Tool path for add/replace-with-slot is the bridge, not the low-level
    client, so the stub is injected as the bridge's connected client.
    """

    long_term = _SlotLongTerm()
    bridge = MemoryBridge()
    bridge._client = SimpleNamespace(long_term=long_term)

    async def fake_bridge(host, timeout=None):
        del host, timeout
        return bridge

    monkeypatch.setattr(memory_tools, "bridge", fake_bridge)
    return long_term


@pytest.mark.asyncio
async def test_memory_replace_refuses_prose_for_a_declared_field(monkeypatch) -> None:
    """MEM-L3-03: a value written under a slot must satisfy that field."""

    long_term = _use_stub_bridge(monkeypatch)
    payload = json.loads(
        await memory_tools.memory(
            HOST,
            target="user",
            action="replace",
            content="用户喜欢的终端颜色是浅色（light），不是深色。",
            slot="appearance.terminal_color_scheme",
        )
    )

    assert payload["success"] is False
    assert payload["code"] == "invalid_value"
    assert payload["slot"] == "appearance.terminal_color_scheme"
    assert "assistant_instruction" in payload
    assert long_term.put_calls == []
    assert long_term.revise_calls == []


@pytest.mark.asyncio
async def test_memory_replace_stores_the_canonical_field_value(monkeypatch) -> None:
    long_term = _use_stub_bridge(monkeypatch)
    payload = json.loads(
        await memory_tools.memory(
            HOST,
            target="user",
            action="replace",
            content="浅色",
            slot="appearance.terminal_color_scheme",
        )
    )

    assert payload["success"] is True
    assert payload["code"] == "slot_revised"
    assert long_term.revise_calls[0][1]["text"] == "light"
    assert "slot:appearance.terminal_color_scheme" in long_term.revise_calls[0][1]["tags"]


class _FakeSessions:
    def __init__(self) -> None:
        self.history_requests: list[tuple[str, int]] = []
        self.recent_context: list[dict[str, object]] = []

    async def get_history(self, session_id, *, limit=200):
        self.history_requests.append((session_id, limit))
        return {
            "events": [
                {"event_type": "user_message", "payload": {"text": "你好", "run_id": "r1"}, "event_sequence": 1},
                {"event_type": "assistant_message", "payload": {"text": "在", "run_id": "r1"}, "event_sequence": 2},
            ]
        }

    async def get_recent_context(self, **kwargs):
        self.recent_context.append(kwargs)
        return {
            "sessions": [
                {"session_id": "session-2", "title": "象棋", "last_active_at": 2000},
                {"session_id": "session-1", "title": "当前", "last_active_at": 1000},
            ]
        }


class _FakeClient:
    def __init__(self) -> None:
        self.sessions = _FakeSessions()
        self.closed = False

    @classmethod
    async def connect(cls, socket_path, timeout=5.0, *, account_id=None, agent_name=None):
        return cls.connections.pop(0)

    async def close(self) -> None:
        self.closed = True


def _use_fake_client(monkeypatch) -> _FakeClient:
    client = _FakeClient()
    _FakeClient.connections = [client]
    monkeypatch.setattr(session_tools, "AsyncMemoryClient", _FakeClient)
    return client


@pytest.mark.asyncio
async def test_session_search_excludes_the_current_session(monkeypatch) -> None:
    client = _use_fake_client(monkeypatch)

    payload = json.loads(await session_tools.session_search(HOST))

    assert client.closed is True
    assert payload["sessions"][0]["session_id"] == "session-2"
    assert client.sessions.recent_context == [
        {
            "exclude_session_id": "session-1",
            "selection_mode": "session_status",
            "session_limit": 20,
        }
    ]


@pytest.mark.asyncio
async def test_session_search_reads_one_transcript(monkeypatch) -> None:
    client = _use_fake_client(monkeypatch)

    payload = json.loads(
        await session_tools.session_search(HOST, session_id="session-9", limit=5)
    )

    assert payload["session_id"] == "session-9"
    assert payload["messages"] == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "在"},
    ]
    assert client.sessions.history_requests == [("session-9", 5000)]


def test_confirmation_token_matches_the_adapter_signature(monkeypatch, tmp_path) -> None:
    secret = b"unit-test-secret"
    secret_file = tmp_path / "adapter-confirmation.secret"
    secret_file.write_bytes(secret)
    monkeypatch.delenv(memory_tools.CONFIRM_TOKEN_ENV, raising=False)
    monkeypatch.delenv(memory_tools.CONFIRM_TOKEN_FILE_ENV, raising=False)
    monkeypatch.setattr(
        memory_tools, "DEFAULT_CONFIRMATION_SECRET", str(secret_file)
    )

    token = memory_tools._confirmation_token()

    prefix, expiry, nonce, signature = token.split(":")
    assert prefix == "v1"
    assert int(expiry) > 0
    assert nonce
    expected = hashlib.sha256(
        secret + f"v1:{expiry}:{nonce}:{memory_tools.FORGET_METHOD}".encode()
    ).hexdigest()
    assert signature == expected


def test_confirmation_token_is_empty_without_a_secret(monkeypatch) -> None:
    monkeypatch.delenv(memory_tools.CONFIRM_TOKEN_ENV, raising=False)
    monkeypatch.delenv(memory_tools.CONFIRM_TOKEN_FILE_ENV, raising=False)
    monkeypatch.setattr(
        memory_tools, "DEFAULT_CONFIRMATION_SECRET", "/nonexistent/secret"
    )
    assert memory_tools._confirmation_token() == ""
