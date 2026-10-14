from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .client import AsyncMemoryClient, MemoryClient


class BackupAPI:
    def __init__(self, client: "MemoryClient") -> None:
        self._client = client

    def create(
        self, *, scopes: list[str] | tuple[str, ...], idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self._client._call(
            "backup.create", {"scopes": list(scopes)}, idempotency_key=idempotency_key
        )

    def list(self, *, limit: int = 100, device_id: str | None = None) -> list[dict[str, Any]]:
        return self._client._call("backup.list", {"limit": limit, "device_id": device_id})

    def run_scheduled(self, *, force: bool = False) -> dict[str, Any]:
        return self._client._call("backup.run_scheduled", {"force": force})

    def preview_restore(self, backup_id: str) -> dict[str, Any]:
        return self._client._call("backup.preview_restore", {"backup_id": backup_id})

    def restore(
        self, backup_id: str, *, confirm_token: str, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self._client._call(
            "backup.restore",
            {"backup_id": backup_id, "confirm_token": confirm_token},
            idempotency_key=idempotency_key,
        )


class AsyncBackupAPI:
    def __init__(self, client: "AsyncMemoryClient") -> None:
        self._client = client

    async def create(
        self, *, scopes: list[str] | tuple[str, ...], idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return await self._client._call(
            "backup.create", {"scopes": list(scopes)}, idempotency_key=idempotency_key
        )

    async def list(self, *, limit: int = 100, device_id: str | None = None) -> list[dict[str, Any]]:
        return await self._client._call("backup.list", {"limit": limit, "device_id": device_id})

    async def run_scheduled(self, *, force: bool = False) -> dict[str, Any]:
        return await self._client._call("backup.run_scheduled", {"force": force})

    async def preview_restore(self, backup_id: str) -> dict[str, Any]:
        return await self._client._call("backup.preview_restore", {"backup_id": backup_id})

    async def restore(
        self, backup_id: str, *, confirm_token: str, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return await self._client._call(
            "backup.restore",
            {"backup_id": backup_id, "confirm_token": confirm_token},
            idempotency_key=idempotency_key,
        )
