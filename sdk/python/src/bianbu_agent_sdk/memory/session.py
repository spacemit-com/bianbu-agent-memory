from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .types import SessionSummaryPatch, SyncPolicy

if TYPE_CHECKING:
    from .client import AsyncMemoryClient, MemoryClient


class SessionAPI:
    def __init__(self, client: "MemoryClient") -> None:
        self._client = client

    def append_event(
        self,
        *,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        sync_policy: SyncPolicy = SyncPolicy.LOCAL_ONLY,
        expires_at: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._client._call(
            "sessions.append_event",
            {
                "session_id": session_id,
                "event_type": event_type,
                "payload": payload,
                "sync_policy": sync_policy.value,
                "expires_at": expires_at,
            },
            idempotency_key=idempotency_key,
        )

    def update_summary(
        self,
        session_id: str,
        *,
        patch: SessionSummaryPatch,
        source_event_sequence: int,
        expected_revision: int,
        sync_policy: SyncPolicy = SyncPolicy.SUMMARY_ONLY,
        producer: str = "agent",
        model_ref: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._client._call(
            "sessions.update_summary",
            {
                "session_id": session_id,
                "patch": patch.to_dict(),
                "source_event_sequence": source_event_sequence,
                "expected_revision": expected_revision,
                "sync_policy": sync_policy.value,
                "producer": producer,
                "model_ref": model_ref,
            },
            idempotency_key=idempotency_key,
        )

    def get_context(
        self,
        session_id: str,
        *,
        include_events: bool = False,
        include_summary: bool = True,
        event_limit: int = 100,
    ) -> dict[str, Any]:
        return self._client._call(
            "sessions.get_context",
            {
                "session_id": session_id,
                "include_events": include_events,
                "include_summary": include_summary,
                "event_limit": event_limit,
            },
        )

    def get_recent_context(
        self,
        *,
        exclude_session_id: str | None = None,
        turn_limit: int = 5,
        summary_limit: int = 3,
        selection_mode: str = "recent_sessions",
        session_limit: int = 5,
        event_limit_per_session: int = 200,
    ) -> dict[str, Any]:
        return self._client._call(
            "sessions.get_recent_context",
            {
                "exclude_session_id": exclude_session_id,
                "turn_limit": turn_limit,
                "summary_limit": summary_limit,
                "selection_mode": selection_mode,
                "session_limit": session_limit,
                "event_limit_per_session": event_limit_per_session,
            },
        )

    def get_history(
        self,
        session_id: str,
        *,
        before_sequence: int | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        return self._client._call(
            "sessions.get_history",
            {"session_id": session_id, "before_sequence": before_sequence, "limit": limit},
        )

    def archive_old(
        self, *, keep_sessions: int = 5, dry_run: bool = False, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self._client._call(
            "sessions.archive_old",
            {"keep_sessions": keep_sessions, "dry_run": dry_run},
            idempotency_key=idempotency_key,
        )

    def compact(
        self,
        session_id: str,
        *,
        level: str = "auto",
        target_tokens: int = 2000,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._client._call(
            "sessions.compact",
            {"session_id": session_id, "level": level, "target_tokens": target_tokens},
            idempotency_key=idempotency_key,
        )

    def summarize(
        self,
        session_id: str,
        *,
        interval_turns: int = 5,
        keep_raw_turns: int = 5,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._client._call(
            "sessions.summarize",
            {
                "session_id": session_id,
                "interval_turns": interval_turns,
                "keep_raw_turns": keep_raw_turns,
            },
            idempotency_key=idempotency_key,
        )


class AsyncSessionAPI:
    def __init__(self, client: "AsyncMemoryClient") -> None:
        self._client = client

    async def append_event(
        self,
        *,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        sync_policy: SyncPolicy = SyncPolicy.LOCAL_ONLY,
        expires_at: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client._call(
            "sessions.append_event",
            {
                "session_id": session_id,
                "event_type": event_type,
                "payload": payload,
                "sync_policy": sync_policy.value,
                "expires_at": expires_at,
            },
            idempotency_key=idempotency_key,
        )

    async def update_summary(
        self,
        session_id: str,
        *,
        patch: SessionSummaryPatch,
        source_event_sequence: int,
        expected_revision: int,
        sync_policy: SyncPolicy = SyncPolicy.SUMMARY_ONLY,
        producer: str = "agent",
        model_ref: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client._call(
            "sessions.update_summary",
            {
                "session_id": session_id,
                "patch": patch.to_dict(),
                "source_event_sequence": source_event_sequence,
                "expected_revision": expected_revision,
                "sync_policy": sync_policy.value,
                "producer": producer,
                "model_ref": model_ref,
            },
            idempotency_key=idempotency_key,
        )

    async def get_context(
        self,
        session_id: str,
        *,
        include_events: bool = False,
        include_summary: bool = True,
        event_limit: int = 100,
    ) -> dict[str, Any]:
        return await self._client._call(
            "sessions.get_context",
            {
                "session_id": session_id,
                "include_events": include_events,
                "include_summary": include_summary,
                "event_limit": event_limit,
            },
        )

    async def get_recent_context(
        self,
        *,
        exclude_session_id: str | None = None,
        turn_limit: int = 5,
        summary_limit: int = 3,
        selection_mode: str = "recent_sessions",
        session_limit: int = 5,
        event_limit_per_session: int = 200,
    ) -> dict[str, Any]:
        return await self._client._call(
            "sessions.get_recent_context",
            {
                "exclude_session_id": exclude_session_id,
                "turn_limit": turn_limit,
                "summary_limit": summary_limit,
                "selection_mode": selection_mode,
                "session_limit": session_limit,
                "event_limit_per_session": event_limit_per_session,
            },
        )

    async def get_history(
        self,
        session_id: str,
        *,
        before_sequence: int | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        return await self._client._call(
            "sessions.get_history",
            {"session_id": session_id, "before_sequence": before_sequence, "limit": limit},
        )

    async def archive_old(
        self, *, keep_sessions: int = 5, dry_run: bool = False, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return await self._client._call(
            "sessions.archive_old",
            {"keep_sessions": keep_sessions, "dry_run": dry_run},
            idempotency_key=idempotency_key,
        )

    async def compact(
        self,
        session_id: str,
        *,
        level: str = "auto",
        target_tokens: int = 2000,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client._call(
            "sessions.compact",
            {"session_id": session_id, "level": level, "target_tokens": target_tokens},
            idempotency_key=idempotency_key,
        )

    async def summarize(
        self,
        session_id: str,
        *,
        interval_turns: int = 5,
        keep_raw_turns: int = 5,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client._call(
            "sessions.summarize",
            {
                "session_id": session_id,
                "interval_turns": interval_turns,
                "keep_raw_turns": keep_raw_turns,
            },
            idempotency_key=idempotency_key,
        )
