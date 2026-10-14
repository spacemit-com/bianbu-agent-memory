from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from .administration import _projection_action

from .types import (
    EvidenceRef,
    ForgetJob,
    LongTermMemory,
    MemoryHeader,
    MemoryKind,
    SyncPolicy,
    WriteReceipt,
)

if TYPE_CHECKING:
    from .client import AsyncMemoryClient, MemoryClient


#: memoryd refuses a ``long_term.get_many`` carrying more than 100 ids
#: (``at most 100 memory_ids are allowed``). That ceiling belongs to the
#: transport, so it is honoured here rather than at every call site: a recall
#: over a large store unions the query headers, the reminder headers and the
#: explicit records, and each of those can reach 100 on its own.
_GET_MANY_ID_LIMIT = 100


def _get_many_batches(memory_ids: list[str] | tuple[str, ...]) -> list[list[str]]:
    """Split one logical read into the requests memoryd will accept."""

    unique = list(dict.fromkeys(str(item) for item in memory_ids))
    return [
        unique[start : start + _GET_MANY_ID_LIMIT]
        for start in range(0, len(unique), _GET_MANY_ID_LIMIT)
    ]

#: Reserved tag carrying the user-designated important mark on a MEM-L3 record.
#: Mirrors ``IMPORTANT_MEMORY_TAG`` in ``crates/memory-store/src/product.rs``;
#: the cloud backup scope of the same name matches exactly this tag. It is
#: deliberately a plain tag: ``validate_tags`` allows at most one ``slot:`` tag
#: per record, so a slot would collide with the record's own field slot.
IMPORTANT_MARK_TAG = "important"


def _with_important_tag(
    tags: list[str] | tuple[str, ...], important: bool
) -> list[str]:
    """Return ``tags`` with the reserved important mark set or cleared.

    Position is preserved when the mark is already in the requested state, so
    the caller can compare the result with the input to detect a no-op. Other
    tags are never dropped.
    """

    current = [str(tag) for tag in tags]
    present = IMPORTANT_MARK_TAG in current
    if present == important:
        return current
    if important:
        return [*current, IMPORTANT_MARK_TAG]
    return [tag for tag in current if tag != IMPORTANT_MARK_TAG]


class LongTermAPI:
    def __init__(self, client: "MemoryClient") -> None:
        self._client = client

    def put(
        self,
        *,
        kind: MemoryKind,
        text: str,
        summary: str | None = None,
        tags: list[str] | tuple[str, ...] = (),
        evidence: list[EvidenceRef] | tuple[EvidenceRef, ...] = (),
        sync_policy: SyncPolicy = SyncPolicy.ACCOUNT_AGENT,
        sensitivity: str = "private",
        confidence: float | None = None,
        expires_at: int | None = None,
        idempotency_key: str | None = None,
    ) -> WriteReceipt:
        value = self._client._call(
            "long_term.put",
            {
                "kind": kind.value,
                "text": text,
                "summary": summary,
                "tags": list(tags),
                "evidence": [item.to_dict() for item in evidence],
                "sync_policy": sync_policy.value,
                "sensitivity": sensitivity,
                "confidence": confidence,
                "expires_at": expires_at,
            },
            idempotency_key=idempotency_key,
        )
        return WriteReceipt(value["memory_id"], int(value["revision"]), value)

    def search_headers(
        self,
        query: str,
        *,
        kinds: list[MemoryKind] | tuple[MemoryKind, ...] = (),
        top_k: int = 10,
    ) -> list[MemoryHeader]:
        values = self._client._call(
            "long_term.search_headers",
            {"query": query, "kinds": [kind.value for kind in kinds], "top_k": top_k},
        )
        return [MemoryHeader.from_dict(value) for value in values]

    def get_many(self, memory_ids: list[str] | tuple[str, ...]) -> list[LongTermMemory]:
        records: list[LongTermMemory] = []
        for batch in _get_many_batches(memory_ids):
            values = self._client._call("long_term.get_many", {"memory_ids": batch})
            records.extend(LongTermMemory.from_dict(value) for value in values)
        return records

    def revise(
        self,
        memory_id: str,
        *,
        text: str,
        expected_revision: int,
        summary: str | None = None,
        tags: list[str] | None = None,
        evidence: list[EvidenceRef] | tuple[EvidenceRef, ...] = (),
        idempotency_key: str | None = None,
    ) -> WriteReceipt:
        value = self._client._call(
            "long_term.revise",
            {
                "memory_id": memory_id,
                "text": text,
                "expected_revision": expected_revision,
                "summary": summary,
                "tags": tags,
                "evidence": [item.to_dict() for item in evidence],
            },
            idempotency_key=idempotency_key,
        )
        return WriteReceipt(value["memory_id"], int(value["revision"]), value)

    def mark_important(
        self,
        record: LongTermMemory,
        *,
        important: bool = True,
        idempotency_key: str | None = None,
    ) -> WriteReceipt:
        """Set or clear the user-designated important mark on a record.

        Only the reserved tag is toggled: text, summary, evidence and every
        other tag are carried through unchanged, so the record keeps its field
        slot. A record already in the requested state is returned untouched
        instead of burning a revision the cloud would have to carry.
        """
        tags = _with_important_tag(record.tags, important)
        if list(record.tags) == tags:
            return WriteReceipt(
                record.memory_id,
                int(record.revision),
                {
                    "memory_id": record.memory_id,
                    "revision": int(record.revision),
                    "tags": tags,
                    "unchanged": True,
                },
            )
        return self.revise(
            record.memory_id,
            text=record.text,
            expected_revision=int(record.revision),
            summary=record.summary,
            tags=tags,
            evidence=record.evidence,
            idempotency_key=idempotency_key,
        )

    def unmark_important(
        self,
        record: LongTermMemory,
        *,
        idempotency_key: str | None = None,
    ) -> WriteReceipt:
        return self.mark_important(
            record, important=False, idempotency_key=idempotency_key
        )

    def reconcile_slots(
        self,
        *,
        slots: list[str] | tuple[str, ...] = (),
        dry_run: bool = True,
    ) -> dict[str, object]:
        return self._client._call(
            "long_term.reconcile_slots",
            {"slots": list(slots), "dry_run": dry_run},
        )

    def forget(
        self,
        memory_id: str,
        *,
        confirm_token: str,
        reason_code: str = "user_request",
        idempotency_key: str | None = None,
    ) -> ForgetJob:
        value = self._client._call(
            "long_term.forget",
            {
                "memory_id": memory_id,
                "confirm_token": confirm_token,
                "reason_code": reason_code,
            },
            idempotency_key=idempotency_key,
        )
        receipt = dict(value["receipt"])
        receipt["derived_projection"] = _projection_action("prune")
        return ForgetJob(value["job_id"], value["state"], receipt)

    def wait_forget(self, job_id: str) -> dict[str, object]:
        return self._client._call("long_term.wait_forget", {"job_id": job_id})


class AsyncLongTermAPI:
    def __init__(self, client: "AsyncMemoryClient") -> None:
        self._client = client

    async def put(
        self,
        *,
        kind: MemoryKind,
        text: str,
        summary: str | None = None,
        tags: list[str] | tuple[str, ...] = (),
        evidence: list[EvidenceRef] | tuple[EvidenceRef, ...] = (),
        sync_policy: SyncPolicy = SyncPolicy.ACCOUNT_AGENT,
        sensitivity: str = "private",
        confidence: float | None = None,
        expires_at: int | None = None,
        idempotency_key: str | None = None,
    ) -> WriteReceipt:
        value = await self._client._call(
            "long_term.put",
            {
                "kind": kind.value,
                "text": text,
                "summary": summary,
                "tags": list(tags),
                "evidence": [item.to_dict() for item in evidence],
                "sync_policy": sync_policy.value,
                "sensitivity": sensitivity,
                "confidence": confidence,
                "expires_at": expires_at,
            },
            idempotency_key=idempotency_key,
        )
        return WriteReceipt(value["memory_id"], int(value["revision"]), value)

    async def search_headers(
        self,
        query: str,
        *,
        kinds: list[MemoryKind] | tuple[MemoryKind, ...] = (),
        top_k: int = 10,
    ) -> list[MemoryHeader]:
        values = await self._client._call(
            "long_term.search_headers",
            {"query": query, "kinds": [kind.value for kind in kinds], "top_k": top_k},
        )
        return [MemoryHeader.from_dict(value) for value in values]

    async def get_many(self, memory_ids: list[str] | tuple[str, ...]) -> list[LongTermMemory]:
        records: list[LongTermMemory] = []
        for batch in _get_many_batches(memory_ids):
            values = await self._client._call(
                "long_term.get_many", {"memory_ids": batch}
            )
            records.extend(LongTermMemory.from_dict(value) for value in values)
        return records

    async def revise(
        self,
        memory_id: str,
        *,
        text: str,
        expected_revision: int,
        summary: str | None = None,
        tags: list[str] | None = None,
        evidence: list[EvidenceRef] | tuple[EvidenceRef, ...] = (),
        idempotency_key: str | None = None,
    ) -> WriteReceipt:
        value = await self._client._call(
            "long_term.revise",
            {
                "memory_id": memory_id,
                "text": text,
                "expected_revision": expected_revision,
                "summary": summary,
                "tags": tags,
                "evidence": [item.to_dict() for item in evidence],
            },
            idempotency_key=idempotency_key,
        )
        return WriteReceipt(value["memory_id"], int(value["revision"]), value)

    async def mark_important(
        self,
        record: LongTermMemory,
        *,
        important: bool = True,
        idempotency_key: str | None = None,
    ) -> WriteReceipt:
        """Set or clear the user-designated important mark on a record."""
        tags = _with_important_tag(record.tags, important)
        if list(record.tags) == tags:
            return WriteReceipt(
                record.memory_id,
                int(record.revision),
                {
                    "memory_id": record.memory_id,
                    "revision": int(record.revision),
                    "tags": tags,
                    "unchanged": True,
                },
            )
        return await self.revise(
            record.memory_id,
            text=record.text,
            expected_revision=int(record.revision),
            summary=record.summary,
            tags=tags,
            evidence=record.evidence,
            idempotency_key=idempotency_key,
        )

    async def unmark_important(
        self,
        record: LongTermMemory,
        *,
        idempotency_key: str | None = None,
    ) -> WriteReceipt:
        return await self.mark_important(
            record, important=False, idempotency_key=idempotency_key
        )

    async def reconcile_slots(
        self,
        *,
        slots: list[str] | tuple[str, ...] = (),
        dry_run: bool = True,
    ) -> dict[str, object]:
        return await self._client._call(
            "long_term.reconcile_slots",
            {"slots": list(slots), "dry_run": dry_run},
        )

    async def forget(
        self,
        memory_id: str,
        *,
        confirm_token: str,
        reason_code: str = "user_request",
        idempotency_key: str | None = None,
    ) -> ForgetJob:
        value = await self._client._call(
            "long_term.forget",
            {"memory_id": memory_id, "confirm_token": confirm_token, "reason_code": reason_code},
            idempotency_key=idempotency_key,
        )
        receipt = dict(value["receipt"])
        receipt["derived_projection"] = await asyncio.to_thread(
            _projection_action, "prune"
        )
        return ForgetJob(value["job_id"], value["state"], receipt)

    async def wait_forget(self, job_id: str) -> dict[str, object]:
        return await self._client._call("long_term.wait_forget", {"job_id": job_id})
