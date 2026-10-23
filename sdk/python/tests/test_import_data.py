"""Contract coverage for the local export/import round trip."""

from __future__ import annotations

import asyncio
import json

from bianbu_agent_sdk.memory.administration import (
    AsyncPrivacyAPI,
    PrivacyAPI,
    _merge_export_pages_pages,
)

DOCUMENT = {
    "format": "bianbu-agent-memory.export.v1",
    "scope": {"account_id": "local:test", "agent_id": "bibit", "memory_space_id": "space"},
    "session_events": [],
    "session_summaries": [],
    "long_term_memories": [],
}


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict]] = []

    def _call(self, method: str, payload: dict, **kwargs: object) -> dict:
        self.calls.append((method, payload, kwargs))
        return {"imported_at": 1, "format": DOCUMENT["format"]}


class _AsyncRecordingClient(_RecordingClient):
    async def _call(self, method: str, payload: dict, **kwargs: object) -> dict:
        return _RecordingClient._call(self, method, payload, **kwargs)


def test_import_data_sends_the_document_and_confirmation_token() -> None:
    client = _RecordingClient()

    PrivacyAPI(client).import_data(DOCUMENT, confirm_token="token", idempotency_key="k1")

    method, payload, kwargs = client.calls[0]
    assert method == "privacy.import_data"
    assert payload == {"payload": DOCUMENT, "confirm_token": "token"}
    assert kwargs["idempotency_key"] == "k1"


def test_async_import_data_matches_the_sync_payload() -> None:
    client = _AsyncRecordingClient()

    asyncio.run(AsyncPrivacyAPI(client).import_data(DOCUMENT, confirm_token="token"))

    method, payload, _ = client.calls[0]
    assert method == "privacy.import_data"
    assert payload["confirm_token"] == "token"
    assert payload["payload"] is DOCUMENT


class _PagedClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def _call(self, method: str, payload: dict, **kwargs: object) -> dict:
        assert method == "privacy.export_data"
        self.calls.append(payload)
        if "cursor" not in payload:
            return {
                "format": DOCUMENT["format"],
                "snapshot_at": 7,
                "snapshot": {
                    "snapshot_at": 7,
                    "session_events": 2,
                    "session_summaries": 0,
                    "long_term_memories": 1,
                },
                "session_events": [{"event_id": "e1"}, {"event_id": "e2"}],
                "session_summaries": [],
                "long_term_memories": [{"memory_id": "m1"}],
                "has_more_any": True,
                "next_cursor": {"session_events": {"at": 10, "rowid": 2}},
            }
        assert payload["snapshot_at"] == 7
        assert payload["snapshot"]["session_events"] == 2
        return {
            "format": DOCUMENT["format"],
            "snapshot_at": 7,
            "session_events": [{"event_id": "e2"}, {"event_id": "e3"}],
            "session_summaries": [{"session_id": "s1", "revision": 1}],
            "long_term_memories": [],
            "has_more_any": False,
            "next_cursor": {},
        }


def test_export_data_follows_the_cursor_and_merges_without_duplicates() -> None:
    client = _PagedClient()

    document = PrivacyAPI(client).export_data(
        limit_per_collection=2, include_audit=False
    )

    assert len(client.calls) == 2
    assert client.calls[1]["cursor"] == {"session_events": {"at": 10, "rowid": 2}}
    assert client.calls[1]["snapshot"]["session_events"] == 2
    assert [row["event_id"] for row in document["session_events"]] == ["e1", "e2", "e3"]
    assert [row["memory_id"] for row in document["long_term_memories"]] == ["m1"]
    assert document["complete"] is True
    assert document["page_count"] == 2
    assert "next_cursor" not in document


def test_export_data_can_return_exactly_one_page() -> None:
    client = _PagedClient()

    page = PrivacyAPI(client).export_data(limit_per_collection=2, all_pages=False)

    assert len(client.calls) == 1
    assert page["has_more_any"] is True


def test_merge_keeps_the_standard_document_shape() -> None:
    merged = _merge_export_pages_pages([
        {
            "format": DOCUMENT["format"],
            "scope": DOCUMENT["scope"],
            "session_events": [],
            "session_summaries": [],
            "long_term_memories": [],
            "snapshot_at": 7,
            "has_more_any": False,
            "next_cursor": None,
        }
    ])

    json.dumps(merged)
    assert merged["format"] == DOCUMENT["format"]
    assert merged["scope"] == DOCUMENT["scope"]


def test_legacy_single_page_export_keeps_the_old_shape() -> None:
    legacy = {
        "format": DOCUMENT["format"],
        "scope": DOCUMENT["scope"],
        "session_events": [],
        "session_summaries": [],
        "long_term_memories": [],
    }

    merged = _merge_export_pages_pages([legacy])

    assert "complete" not in merged
    assert "page_count" not in merged
