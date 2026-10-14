from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    from .client import AsyncMemoryClient, MemoryClient


@dataclass(frozen=True)
class ProjectionMapping:
    provider_memory_id: str
    memory_id: str
    canonical_revision: int
    state: str = "active"
    last_score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_memory_id": self.provider_memory_id,
            "memory_id": self.memory_id,
            "canonical_revision": self.canonical_revision,
            "state": self.state,
            "last_score": self.last_score,
        }


class ProjectionAPI:
    def __init__(self, client: "MemoryClient") -> None:
        self._client = client

    def enqueue_turn(
        self,
        *,
        provider: str,
        run_id: str,
        session_id: str,
        user_event_id: str,
        assistant_event_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._client._call(
            "projection.enqueue_turn",
            {
                "provider": provider,
                "run_id": run_id,
                "session_id": session_id,
                "user_event_id": user_event_id,
                "assistant_event_id": assistant_event_id,
            },
            idempotency_key=idempotency_key,
        )

    def claim(
        self, *, provider: str, worker_id: str, lease_ms: int = 120_000
    ) -> dict[str, Any] | None:
        return self._client._call(
            "projection.claim",
            {"provider": provider, "worker_id": worker_id, "lease_ms": lease_ms},
        )

    def save_result(
        self,
        job_id: str,
        *,
        worker_id: str,
        provider_result: Any,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._client._call(
            "projection.save_result",
            {
                "job_id": job_id,
                "worker_id": worker_id,
                "provider_result": provider_result,
            },
            idempotency_key=idempotency_key,
        )

    def complete(
        self,
        job_id: str,
        *,
        worker_id: str,
        mappings: Sequence[ProjectionMapping] = (),
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._client._call(
            "projection.complete",
            {
                "job_id": job_id,
                "worker_id": worker_id,
                "mappings": [item.to_dict() for item in mappings],
            },
            idempotency_key=idempotency_key,
        )

    def fail(
        self,
        job_id: str,
        *,
        worker_id: str,
        error: str,
        retry_after_ms: int = 5_000,
        terminal: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._client._call(
            "projection.fail",
            {
                "job_id": job_id,
                "worker_id": worker_id,
                "error": error,
                "retry_after_ms": retry_after_ms,
                "terminal": terminal,
            },
            idempotency_key=idempotency_key,
        )

    def upsert_mapping(
        self,
        *,
        provider: str,
        mapping: ProjectionMapping,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._client._call(
            "projection.upsert_mapping",
            {"provider": provider, "mapping": mapping.to_dict()},
            idempotency_key=idempotency_key,
        )

    def resolve(
        self, *, provider: str, provider_memory_ids: Sequence[str]
    ) -> list[dict[str, Any]]:
        return self._client._call(
            "projection.resolve",
            {
                "provider": provider,
                "provider_memory_ids": list(provider_memory_ids),
            },
        )

    def status(self, *, provider: str | None = None) -> dict[str, Any]:
        return self._client._call("projection.status", {"provider": provider})

    def list_dead(
        self, *, provider: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        return self._client._call(
            "projection.list_dead", {"provider": provider, "limit": limit}
        )

    def requeue_dead(
        self,
        job_id: str,
        *,
        provider: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._client._call(
            "projection.requeue_dead",
            {"provider": provider, "job_id": job_id},
            idempotency_key=idempotency_key,
        )


class AsyncProjectionAPI:
    def __init__(self, client: "AsyncMemoryClient") -> None:
        self._client = client

    async def enqueue_turn(self, **kwargs: Any) -> dict[str, Any]:
        idempotency_key = kwargs.pop("idempotency_key", None)
        return await self._client._call(
            "projection.enqueue_turn", kwargs, idempotency_key=idempotency_key
        )

    async def claim(
        self, *, provider: str, worker_id: str, lease_ms: int = 120_000
    ) -> dict[str, Any] | None:
        return await self._client._call(
            "projection.claim",
            {"provider": provider, "worker_id": worker_id, "lease_ms": lease_ms},
        )

    async def save_result(
        self,
        job_id: str,
        *,
        worker_id: str,
        provider_result: Any,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client._call(
            "projection.save_result",
            {
                "job_id": job_id,
                "worker_id": worker_id,
                "provider_result": provider_result,
            },
            idempotency_key=idempotency_key,
        )

    async def complete(
        self,
        job_id: str,
        *,
        worker_id: str,
        mappings: Sequence[ProjectionMapping] = (),
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client._call(
            "projection.complete",
            {
                "job_id": job_id,
                "worker_id": worker_id,
                "mappings": [item.to_dict() for item in mappings],
            },
            idempotency_key=idempotency_key,
        )

    async def fail(
        self,
        job_id: str,
        *,
        worker_id: str,
        error: str,
        retry_after_ms: int = 5_000,
        terminal: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client._call(
            "projection.fail",
            {
                "job_id": job_id,
                "worker_id": worker_id,
                "error": error,
                "retry_after_ms": retry_after_ms,
                "terminal": terminal,
            },
            idempotency_key=idempotency_key,
        )

    async def upsert_mapping(
        self,
        *,
        provider: str,
        mapping: ProjectionMapping,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client._call(
            "projection.upsert_mapping",
            {"provider": provider, "mapping": mapping.to_dict()},
            idempotency_key=idempotency_key,
        )

    async def resolve(
        self, *, provider: str, provider_memory_ids: Sequence[str]
    ) -> list[dict[str, Any]]:
        return await self._client._call(
            "projection.resolve",
            {
                "provider": provider,
                "provider_memory_ids": list(provider_memory_ids),
            },
        )

    async def status(self, *, provider: str | None = None) -> dict[str, Any]:
        return await self._client._call("projection.status", {"provider": provider})

    async def list_dead(
        self, *, provider: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        return await self._client._call(
            "projection.list_dead", {"provider": provider, "limit": limit}
        )

    async def requeue_dead(
        self,
        job_id: str,
        *,
        provider: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client._call(
            "projection.requeue_dead",
            {"provider": provider, "job_id": job_id},
            idempotency_key=idempotency_key,
        )


__all__ = ["AsyncProjectionAPI", "ProjectionAPI", "ProjectionMapping"]
