from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .client import AsyncMemoryClient, MemoryClient


class AccountAPI:
    def __init__(self, client: "MemoryClient") -> None:
        self._client = client

    def status(self) -> dict[str, Any]:
        return self._client._call("account.status", {})

    def begin_login(self, *, mode: str = "pkce") -> dict[str, Any]:
        return self._client._call("account.begin_login", {"mode": mode})

    def complete_login(self, flow_id: str, redirect_result: str) -> dict[str, Any]:
        return self._client._call(
            "account.complete_login",
            {"flow_id": flow_id, "redirect_result": redirect_result},
        )

    def logout(self, *, confirm_token: str) -> dict[str, Any]:
        return self._client._call("account.logout", {"confirm_token": confirm_token})


class AsyncAccountAPI:
    def __init__(self, client: "AsyncMemoryClient") -> None:
        self._client = client

    async def status(self) -> dict[str, Any]:
        return await self._client._call("account.status", {})

    async def begin_login(self, *, mode: str = "pkce") -> dict[str, Any]:
        return await self._client._call("account.begin_login", {"mode": mode})

    async def complete_login(self, flow_id: str, redirect_result: str) -> dict[str, Any]:
        return await self._client._call(
            "account.complete_login",
            {"flow_id": flow_id, "redirect_result": redirect_result},
        )

    async def logout(self, *, confirm_token: str) -> dict[str, Any]:
        return await self._client._call("account.logout", {"confirm_token": confirm_token})
