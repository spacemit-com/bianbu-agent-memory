"""Coverage for the user-designated important mark (``long_term`` SDK).

The mark is a reserved plain tag, not a slot: ``validate_tags`` allows at most
one ``slot:`` tag per record, so a slot would collide with the record own field
slot. It is also a cloud backup scope of its own (``important``), because the
mark is orthogonal to the record content category. These tests pin the tag
arithmetic, the no-op short circuit and both the sync and async entry points.
"""

from __future__ import annotations

import asyncio

from bianbu_agent_sdk.memory.long_term import (
    IMPORTANT_MARK_TAG,
    AsyncLongTermAPI,
    LongTermAPI,
    _with_important_tag,
)
from bianbu_agent_sdk.memory.types import LongTermMemory, MemoryKind, SyncPolicy


def _record(memory_id: str = "m-1", tags=(), revision: int = 3) -> LongTermMemory:
    return LongTermMemory.from_dict(
        {
            "memory_id": memory_id,
            "kind": MemoryKind.PREFERENCE.value,
            "text": "terminal color: dark",
            "summary": "terminal color",
            "tags": list(tags),
            "evidence": [],
            "source": "agent",
            "confidence": None,
            "sensitivity": "private",
            "revision": revision,
            "sync_policy": SyncPolicy.ACCOUNT_AGENT.value,
            "created_at": 1,
            "updated_at": 2,
        }
    )


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def _call(self, method: str, payload: dict, **_: object) -> dict:
        assert method == "long_term.revise", method
        self.calls.append(payload)
        return {"memory_id": payload["memory_id"], "revision": 4, "tags": payload["tags"]}


class _AsyncRecordingClient(_RecordingClient):
    async def _call(self, method: str, payload: dict, **_: object) -> dict:
        return _RecordingClient._call(self, method, payload)


def test_tag_is_a_plain_tag_and_matches_the_daemon_reserved_name() -> None:
    # Mirrors IMPORTANT_MEMORY_TAG in crates/memory-store/src/product.rs.
    assert IMPORTANT_MARK_TAG == "important"


def test_helper_appends_the_mark_and_keeps_other_tags() -> None:
    tags = _with_important_tag(["slot:appearance.terminal_color_scheme"], True)

    assert tags == ["slot:appearance.terminal_color_scheme", IMPORTANT_MARK_TAG]


def test_helper_is_a_fixed_point_in_both_directions() -> None:
    marked = ["slot:appearance.terminal_color_scheme", IMPORTANT_MARK_TAG]

    assert _with_important_tag(marked, True) == marked
    assert _with_important_tag(["slot:x"], False) == ["slot:x"]


def test_helper_removes_only_the_mark() -> None:
    marked = ["slot:appearance.terminal_color_scheme", IMPORTANT_MARK_TAG]

    assert _with_important_tag(marked, False) == ["slot:appearance.terminal_color_scheme"]


def test_mark_important_revises_at_the_expected_revision() -> None:
    client = _RecordingClient()

    receipt = LongTermAPI(client).mark_important(_record())

    assert receipt.revision == 4
    assert len(client.calls) == 1
    payload = client.calls[0]
    assert payload["expected_revision"] == 3
    assert payload["text"] == "terminal color: dark"
    assert payload["summary"] == "terminal color"
    assert payload["tags"] == [IMPORTANT_MARK_TAG]


def test_mark_important_keeps_the_field_slot() -> None:
    client = _RecordingClient()
    record = _record(tags=["slot:appearance.terminal_color_scheme"])

    LongTermAPI(client).mark_important(record)

    assert client.calls[0]["tags"] == [
        "slot:appearance.terminal_color_scheme",
        IMPORTANT_MARK_TAG,
    ]


def test_marking_an_already_marked_record_costs_no_revision() -> None:
    client = _RecordingClient()
    record = _record(tags=[IMPORTANT_MARK_TAG])

    receipt = LongTermAPI(client).mark_important(record)

    assert client.calls == []
    assert receipt.revision == 3
    assert receipt.raw["unchanged"] is True


def test_unmark_important_removes_only_the_mark() -> None:
    client = _RecordingClient()
    record = _record(tags=["slot:appearance.terminal_color_scheme", IMPORTANT_MARK_TAG])

    LongTermAPI(client).unmark_important(record)

    assert client.calls[0]["tags"] == ["slot:appearance.terminal_color_scheme"]


def test_async_entry_points_toggle_identically() -> None:
    client = _AsyncRecordingClient()
    api = AsyncLongTermAPI(client)

    marked = asyncio.run(api.mark_important(_record()))
    cleared = asyncio.run(api.unmark_important(_record(tags=[IMPORTANT_MARK_TAG])))

    assert marked.revision == 4
    assert cleared.revision == 4
    assert client.calls[0]["tags"] == [IMPORTANT_MARK_TAG]
    assert client.calls[1]["tags"] == []
