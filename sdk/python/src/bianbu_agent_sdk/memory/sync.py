from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .types import SyncStatus

if TYPE_CHECKING:
    from .client import AsyncMemoryClient, MemoryClient


class SyncAPI:
    def __init__(self, client: "MemoryClient") -> None:
        self._client = client

    def status(self) -> SyncStatus:
        return SyncStatus.from_dict(self._client._call("sync.status", {}))

    def request(
        self, *, scopes: list[str] | tuple[str, ...], idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self._client._call(
            "sync.request", {"scopes": list(scopes)}, idempotency_key=idempotency_key
        )


class AsyncSyncAPI:
    def __init__(self, client: "AsyncMemoryClient") -> None:
        self._client = client

    async def status(self) -> SyncStatus:
        return SyncStatus.from_dict(await self._client._call("sync.status", {}))

    async def request(
        self, *, scopes: list[str] | tuple[str, ...], idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return await self._client._call(
            "sync.request", {"scopes": list(scopes)}, idempotency_key=idempotency_key
        )
