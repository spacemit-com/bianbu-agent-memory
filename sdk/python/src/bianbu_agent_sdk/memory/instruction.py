from __future__ import annotations

from typing import TYPE_CHECKING

from .types import InstructionMemory, InstructionScope, SyncPolicy

if TYPE_CHECKING:
    from .client import AsyncMemoryClient, MemoryClient


class InstructionAPI:
    def __init__(self, client: "MemoryClient") -> None:
        self._client = client

    def put(
        self,
        *,
        text: str,
        scope: InstructionScope,
        rationale: str | None = None,
        applicability: str | None = None,
        priority: int = 100,
        sync_policy: SyncPolicy = SyncPolicy.ACCOUNT_AGENT,
        sensitivity: str = "private",
        expires_at: int | None = None,
        idempotency_key: str | None = None,
    ) -> InstructionMemory:
        value = self._client._call(
            "instructions.put",
            {
                "text": text,
                "scope": scope.value,
                "rationale": rationale,
                "applicability": applicability,
                "priority": priority,
                "sync_policy": sync_policy.value,
                "sensitivity": sensitivity,
                "expires_at": expires_at,
            },
            idempotency_key=idempotency_key,
        )
        return InstructionMemory.from_dict(value)

    def list(
        self, *, workspace_id: str | None = None, effective_only: bool = True
    ) -> list[InstructionMemory]:
        values = self._client._call(
            "instructions.list",
            {"workspace_id": workspace_id, "effective_only": effective_only},
        )
        return [InstructionMemory.from_dict(value) for value in values]

    def revise(
        self,
        instruction_id: str,
        *,
        text: str,
        expected_revision: int,
        rationale: str | None = None,
        applicability: str | None = None,
        idempotency_key: str | None = None,
    ) -> InstructionMemory:
        value = self._client._call(
            "instructions.revise",
            {
                "instruction_id": instruction_id,
                "text": text,
                "expected_revision": expected_revision,
                "rationale": rationale,
                "applicability": applicability,
            },
            idempotency_key=idempotency_key,
        )
        return InstructionMemory.from_dict(value)


class AsyncInstructionAPI:
    def __init__(self, client: "AsyncMemoryClient") -> None:
        self._client = client

    async def put(
        self,
        *,
        text: str,
        scope: InstructionScope,
        rationale: str | None = None,
        applicability: str | None = None,
        priority: int = 100,
        sync_policy: SyncPolicy = SyncPolicy.ACCOUNT_AGENT,
        sensitivity: str = "private",
        expires_at: int | None = None,
        idempotency_key: str | None = None,
    ) -> InstructionMemory:
        value = await self._client._call(
            "instructions.put",
            {
                "text": text,
                "scope": scope.value,
                "rationale": rationale,
                "applicability": applicability,
                "priority": priority,
                "sync_policy": sync_policy.value,
                "sensitivity": sensitivity,
                "expires_at": expires_at,
            },
            idempotency_key=idempotency_key,
        )
        return InstructionMemory.from_dict(value)

    async def list(
        self, *, workspace_id: str | None = None, effective_only: bool = True
    ) -> list[InstructionMemory]:
        values = await self._client._call(
            "instructions.list",
            {"workspace_id": workspace_id, "effective_only": effective_only},
        )
        return [InstructionMemory.from_dict(value) for value in values]

    async def revise(
        self,
        instruction_id: str,
        *,
        text: str,
        expected_revision: int,
        rationale: str | None = None,
        applicability: str | None = None,
        idempotency_key: str | None = None,
    ) -> InstructionMemory:
        value = await self._client._call(
            "instructions.revise",
            {
                "instruction_id": instruction_id,
                "text": text,
                "expected_revision": expected_revision,
                "rationale": rationale,
                "applicability": applicability,
            },
            idempotency_key=idempotency_key,
        )
        return InstructionMemory.from_dict(value)
