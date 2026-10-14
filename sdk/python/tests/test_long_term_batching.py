"""Regression coverage for the id batching inside ``long_term.get_many``.

``memoryd`` rejects a ``long_term.get_many`` carrying more than 100 ids, and a
single recall can union the query headers, the reminder headers and the explicit
records, so the ceiling has to be honoured per request inside the API instead of
at every call site. These tests pin the split, the aggregation order and the
end-to-end shape of both the sync and the async entry points.
"""

from __future__ import annotations

import asyncio

from bianbu_agent_sdk.memory.long_term import (
    AsyncLongTermAPI,
    LongTermAPI,
    _get_many_batches,
)
from bianbu_agent_sdk.memory.types import MemoryKind, SyncPolicy

#: Mirrors the ceiling in ``crates/memory-store/src/long_term.rs``
#: (``at most 100 memory_ids are allowed``).
MEMORYD_ID_LIMIT = 100


def _record(memory_id: str) -> dict:
    return {
        "memory_id": memory_id,
        "kind": MemoryKind.PROFILE.value,
        "text": f"text-{memory_id}",
        "summary": None,
        "tags": [],
        "evidence": [],
        "source": "agent",
        "confidence": None,
        "sensitivity": "private",
        "revision": 1,
        "sync_policy": SyncPolicy.ACCOUNT_AGENT.value,
        "created_at": 1,
        "updated_at": 2,
    }


def _ids(count: int) -> list[str]:
    return [f"m{index:04d}" for index in range(count)]


class _RecordingClient:
    """Sync client stub that serves reads from a fixed record table."""

    def __init__(self, table: dict[str, dict]) -> None:
        self._table = table
        self.calls: list[list[str]] = []

    def _call(self, method: str, payload: dict, **_: object) -> list[dict]:
        assert method == "long_term.get_many"
        memory_ids = list(payload["memory_ids"])
        self.calls.append(memory_ids)
        return [self._table[memory_id] for memory_id in memory_ids]


class _AsyncRecordingClient(_RecordingClient):
    async def _call(self, method: str, payload: dict, **_: object) -> list[dict]:
        return _RecordingClient._call(self, method, payload)


def test_batches_are_empty_for_no_ids() -> None:
    assert _get_many_batches([]) == []


def test_batches_keep_a_single_request_at_the_limit() -> None:
    ids = _ids(MEMORYD_ID_LIMIT)

    batches = _get_many_batches(ids)

    assert [len(batch) for batch in batches] == [MEMORYD_ID_LIMIT]
    assert batches[0] == ids


def test_batches_split_one_id_past_the_limit() -> None:
    batches = _get_many_batches(_ids(MEMORYD_ID_LIMIT + 1))

    assert [len(batch) for batch in batches] == [MEMORYD_ID_LIMIT, 1]


def test_batches_cover_several_windows_in_order() -> None:
    ids = _ids(201)

    batches = _get_many_batches(ids)

    assert [len(batch) for batch in batches] == [100, 100, 1]
    assert [item for batch in batches for item in batch] == ids


def test_batches_deduplicate_while_preserving_first_seen_order() -> None:
    batches = _get_many_batches(["b", "a", "b", "a", "c"])

    assert batches == [["b", "a", "c"]]


def test_sync_get_many_never_exceeds_the_limit_and_keeps_order() -> None:
    ids = _ids(150)
    table = {memory_id: _record(memory_id) for memory_id in ids}
    client = _RecordingClient(table)

    records = LongTermAPI(client).get_many(ids)

    assert all(len(batch) <= MEMORYD_ID_LIMIT for batch in client.calls)
    assert [len(batch) for batch in client.calls] == [100, 50]
    assert [record.memory_id for record in records] == ids
    assert [record.text for record in records] == [f"text-{item}" for item in ids]


def test_sync_get_many_reads_a_small_set_in_one_request() -> None:
    ids = _ids(3)
    client = _RecordingClient({memory_id: _record(memory_id) for memory_id in ids})

    records = LongTermAPI(client).get_many(ids)

    assert client.calls == [ids]
    assert [record.memory_id for record in records] == ids


def test_sync_get_many_skips_the_transport_for_no_ids() -> None:
    client = _RecordingClient({})

    assert LongTermAPI(client).get_many([]) == []
    assert client.calls == []


def test_async_get_many_batches_identically() -> None:
    ids = _ids(201)
    table = {memory_id: _record(memory_id) for memory_id in ids}
    client = _AsyncRecordingClient(table)

    records = asyncio.run(AsyncLongTermAPI(client).get_many(ids))

    assert [len(batch) for batch in client.calls] == [100, 100, 1]
    assert [record.memory_id for record in records] == ids


def test_async_get_many_skips_the_transport_for_no_ids() -> None:
    client = _AsyncRecordingClient({})

    assert asyncio.run(AsyncLongTermAPI(client).get_many([])) == []
    assert client.calls == []
