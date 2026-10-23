from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING, Any
import urllib.request

if TYPE_CHECKING:
    from .client import AsyncMemoryClient, MemoryClient


def _projection_action(action: str) -> dict[str, Any]:
    engine = os.environ.get("BIANBU_MEMORY_ENGINE", "scheme-a").strip().casefold()
    if engine not in {"dplus", "d+", "upstream-mem0", "mem0-oss"}:
        return {"state": "not_enabled", "deleted": 0}
    url = os.environ.get(
        "BIANBU_MEMORY_DPLUS_URL", "http://127.0.0.1:18920"
    ).rstrip("/")
    request = urllib.request.Request(
        f"{url}/v1/{action}",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10.0) as response:
            value = json.load(response)
        return value if isinstance(value, dict) else {"state": "invalid_response"}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"state": "cleanup_pending", "error": str(exc)[:500]}


def _merge_export_pages_pages(pages: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge a paginated export into one standard ``export.v1`` document.

    Collections are keyed by their original identity, so a cursor that overlaps
    by one millisecond can never produce a duplicate.  Transport-only fields
    (`has_more`, `next_cursor`) are dropped from the file; the snapshot and
    per-collection counts stay so an operator can prove the export is complete.
    """
    if not pages:
        raise ValueError("export returned no pages")
    keys = {
        "session_events": "event_id",
        "session_summaries": lambda row: (
            str(row.get("session_id")),
            row.get("revision", 1),
        ),
        "long_term_memories": "memory_id",
    }
    merged: dict[str, Any] = dict(pages[0])
    for collection, key in keys.items():
        seen: set[Any] = set()
        values: list[Any] = []
        for page in pages:
            for row in page.get(collection) or []:
                if not isinstance(row, dict):
                    continue
                identity = key(row) if callable(key) else row.get(key)
                if identity is None or identity in seen:
                    continue
                seen.add(identity)
                values.append(row)
        merged[collection] = values
    merged.pop("next_cursor", None)
    merged.pop("has_more_any", None)
    # A single-page export from an older daemon has no paging metadata at all;
    # it is complete by definition and should keep the legacy document shape.
    if any(page.get("has_more_any") is not None or page.get("page_count") for page in pages):
        merged["complete"] = True
    if merged.get("complete") is True:
        merged["page_count"] = len(pages)
    return merged


class AuditAPI:
    def __init__(self, client: "MemoryClient") -> None:
        self._client = client

    def status(self) -> dict[str, Any]:
        return self._client._call("audit.status", {})

    def list(self, *, limit: int = 100, before: int | None = None) -> dict[str, Any]:
        return self._client._call("audit.list", {"limit": limit, "before": before})

    def export(self, *, limit: int = 500, before: int | None = None) -> dict[str, Any]:
        return self._client._call("audit.export", {"limit": limit, "before": before})

    def record(
        self,
        action: str,
        *,
        result: str = "success",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._client._call(
            "audit.record",
            {"action": action, "result": result, "details": details or {}},
        )


class AsyncAuditAPI:
    def __init__(self, client: "AsyncMemoryClient") -> None:
        self._client = client

    async def status(self) -> dict[str, Any]:
        return await self._client._call("audit.status", {})

    async def list(self, *, limit: int = 100, before: int | None = None) -> dict[str, Any]:
        return await self._client._call("audit.list", {"limit": limit, "before": before})

    async def export(self, *, limit: int = 500, before: int | None = None) -> dict[str, Any]:
        return await self._client._call("audit.export", {"limit": limit, "before": before})

    async def record(
        self,
        action: str,
        *,
        result: str = "success",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._client._call(
            "audit.record",
            {"action": action, "result": result, "details": details or {}},
        )


class PrivacyAPI:
    def __init__(self, client: "MemoryClient") -> None:
        self._client = client

    def settings(self) -> dict[str, Any]:
        return self._client._call("privacy.settings.get", {})

    def update_settings(
        self,
        *,
        memory_enabled: bool | None = None,
        retention_days: int | None = None,
        audit_enabled: bool | None = None,
        local_device_id: str | None = None,
        timezone: str | None = None,
        sensitive_action_confirmation: str | None = None,
        preference_auto_apply_enabled: bool | None = None,
        development_habit_auto_apply_enabled: bool | None = None,
        recent_turn_limit: int | None = None,
        recent_session_limit: int | None = None,
        summary_interval_turns: int | None = None,
        summary_retention_days: int | None = None,
        summary_model: dict[str, Any] | None = None,
        embedding: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        params = {
            key: value
            for key, value in {
                "memory_enabled": memory_enabled,
                "retention_days": retention_days,
                "audit_enabled": audit_enabled,
                "local_device_id": local_device_id,
                "timezone": timezone,
                "sensitive_action_confirmation": sensitive_action_confirmation,
                "preference_auto_apply_enabled": preference_auto_apply_enabled,
                "development_habit_auto_apply_enabled": development_habit_auto_apply_enabled,
                "recent_turn_limit": recent_turn_limit,
                "recent_session_limit": recent_session_limit,
                "summary_interval_turns": summary_interval_turns,
                "summary_retention_days": summary_retention_days,
                "summary_model": summary_model,
                "embedding": embedding,
            }.items()
            if value is not None
        }
        return self._client._call(
            "privacy.settings.update", params, idempotency_key=idempotency_key
        )

    def export_data(
        self,
        *,
        limit_per_collection: int = 200,
        include_audit: bool = True,
        confirm_token: str | None = None,
        all_pages: bool = True,
    ) -> dict[str, Any]:
        first = self._export_page(
            limit_per_collection=limit_per_collection,
            include_audit=include_audit,
            confirm_token=confirm_token,
        )
        if not all_pages:
            return first
        pages = [first]
        seen_cursors: set[str] = set()
        while pages[-1].get("has_more_any"):
            cursor = pages[-1].get("next_cursor") or {}
            marker = json.dumps(cursor, sort_keys=True)
            if marker in seen_cursors:
                raise ValueError("export paging cursor did not advance")
            seen_cursors.add(marker)
            pages.append(self._export_page(
                limit_per_collection=limit_per_collection,
                include_audit=include_audit,
                confirm_token=confirm_token,
                snapshot_at=pages[-1].get("snapshot_at"),
                snapshot=pages[-1].get("snapshot"),
                cursor=cursor,
            ))
        return _merge_export_pages_pages(pages)

    def _export_page(
        self,
        *,
        limit_per_collection: int,
        include_audit: bool,
        confirm_token: str | None,
        snapshot_at: int | None = None,
        snapshot: dict[str, Any] | None = None,
        cursor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "limit_per_collection": limit_per_collection,
            "include_audit": include_audit,
        }
        if confirm_token is not None:
            params["confirm_token"] = confirm_token
        if snapshot_at is not None:
            params["snapshot_at"] = snapshot_at
        if snapshot:
            params["snapshot"] = snapshot
        if cursor:
            params["cursor"] = cursor
        return self._client._call("privacy.export_data", params)

    def import_data(
        self,
        payload: dict[str, Any],
        *,
        confirm_token: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Restore a ``privacy.export_data`` document into this namespace.

        The import merges: records whose original id is already present are
        skipped, so re-importing the same file is safe. ``confirm_token`` is
        mandatory because restoring memory is a data-boundary write.
        """
        return self._client._call(
            "privacy.import_data",
            {"payload": payload, "confirm_token": confirm_token},
            idempotency_key=idempotency_key,
        )

    def clear_history(
        self, *, confirm_token: str, reason_code: str = "user_request"
    ) -> dict[str, Any]:
        result = self._client._call(
            "privacy.clear_history",
            {"confirm_token": confirm_token, "reason_code": reason_code},
        )
        result["derived_projection"] = _projection_action("prune")
        return result

    def clear_all(
        self, *, confirm_token: str, reason_code: str = "user_request"
    ) -> dict[str, Any]:
        result = self._client._call(
            "privacy.clear_all",
            {"confirm_token": confirm_token, "reason_code": reason_code},
        )
        result["derived_projection"] = _projection_action("clear")
        return result

    def run_maintenance(self, *, remove_expired: bool = True) -> dict[str, Any]:
        result = self._client._call(
            "maintenance.run", {"remove_expired": remove_expired}
        )
        if remove_expired and any(
            int(result.get(key) or 0) > 0
            for key in (
                "expired_session_events",
                "expired_long_term_memories",
                "archived_session_summaries",
            )
        ):
            result["derived_projection"] = _projection_action("prune")
        return result


class AsyncPrivacyAPI:
    def __init__(self, client: "AsyncMemoryClient") -> None:
        self._client = client

    async def settings(self) -> dict[str, Any]:
        return await self._client._call("privacy.settings.get", {})

    async def update_settings(self, **kwargs: Any) -> dict[str, Any]:
        idempotency_key = kwargs.pop("idempotency_key", None)
        params = {key: value for key, value in kwargs.items() if value is not None}
        return await self._client._call(
            "privacy.settings.update", params, idempotency_key=idempotency_key
        )

    async def export_data(
        self,
        *,
        limit_per_collection: int = 200,
        include_audit: bool = True,
        confirm_token: str | None = None,
        all_pages: bool = True,
    ) -> dict[str, Any]:
        first = await self._export_page(
            limit_per_collection=limit_per_collection,
            include_audit=include_audit,
            confirm_token=confirm_token,
        )
        if not all_pages:
            return first
        pages = [first]
        seen_cursors: set[str] = set()
        while pages[-1].get("has_more_any"):
            cursor = pages[-1].get("next_cursor") or {}
            marker = json.dumps(cursor, sort_keys=True)
            if marker in seen_cursors:
                raise ValueError("export paging cursor did not advance")
            seen_cursors.add(marker)
            pages.append(await self._export_page(
                limit_per_collection=limit_per_collection,
                include_audit=include_audit,
                confirm_token=confirm_token,
                snapshot_at=pages[-1].get("snapshot_at"),
                snapshot=pages[-1].get("snapshot"),
                cursor=cursor,
            ))
        return _merge_export_pages_pages(pages)

    async def _export_page(
        self,
        *,
        limit_per_collection: int,
        include_audit: bool,
        confirm_token: str | None,
        snapshot_at: int | None = None,
        snapshot: dict[str, Any] | None = None,
        cursor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "limit_per_collection": limit_per_collection,
            "include_audit": include_audit,
        }
        if confirm_token is not None:
            params["confirm_token"] = confirm_token
        if snapshot_at is not None:
            params["snapshot_at"] = snapshot_at
        if snapshot:
            params["snapshot"] = snapshot
        if cursor:
            params["cursor"] = cursor
        return await self._client._call("privacy.export_data", params)

    async def import_data(
        self,
        payload: dict[str, Any],
        *,
        confirm_token: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """See :meth:`PrivacyAPI.import_data`."""
        return await self._client._call(
            "privacy.import_data",
            {"payload": payload, "confirm_token": confirm_token},
            idempotency_key=idempotency_key,
        )

    async def clear_history(
        self, *, confirm_token: str, reason_code: str = "user_request"
    ) -> dict[str, Any]:
        result = await self._client._call(
            "privacy.clear_history",
            {"confirm_token": confirm_token, "reason_code": reason_code},
        )
        result["derived_projection"] = await asyncio.to_thread(
            _projection_action, "prune"
        )
        return result

    async def clear_all(
        self, *, confirm_token: str, reason_code: str = "user_request"
    ) -> dict[str, Any]:
        result = await self._client._call(
            "privacy.clear_all",
            {"confirm_token": confirm_token, "reason_code": reason_code},
        )
        result["derived_projection"] = await asyncio.to_thread(
            _projection_action, "clear"
        )
        return result

    async def run_maintenance(self, *, remove_expired: bool = True) -> dict[str, Any]:
        result = await self._client._call(
            "maintenance.run", {"remove_expired": remove_expired}
        )
        if remove_expired and any(
            int(result.get(key) or 0) > 0
            for key in (
                "expired_session_events",
                "expired_long_term_memories",
                "archived_session_summaries",
            )
        ):
            result["derived_projection"] = await asyncio.to_thread(
                _projection_action, "prune"
            )
        return result


class RuntimeAPI:
    def __init__(self, client: "MemoryClient") -> None:
        self._client = client

    def prepare_checkpoint(
        self, session_id: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self._client._call(
            "runtime.prepare_checkpoint",
            {"session_id": session_id},
            idempotency_key=idempotency_key,
        )

    def list_checkpoints(self, *, limit: int = 10) -> list[dict[str, Any]]:
        return self._client._call("runtime.list_checkpoints", {"limit": limit})

    def restore_checkpoint(self, checkpoint_ref: str) -> dict[str, Any]:
        return self._client._call(
            "runtime.restore_checkpoint", {"checkpoint_ref": checkpoint_ref}
        )


class AsyncRuntimeAPI:
    def __init__(self, client: "AsyncMemoryClient") -> None:
        self._client = client

    async def prepare_checkpoint(
        self, session_id: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return await self._client._call(
            "runtime.prepare_checkpoint",
            {"session_id": session_id},
            idempotency_key=idempotency_key,
        )

    async def list_checkpoints(self, *, limit: int = 10) -> list[dict[str, Any]]:
        return await self._client._call("runtime.list_checkpoints", {"limit": limit})

    async def restore_checkpoint(self, checkpoint_ref: str) -> dict[str, Any]:
        return await self._client._call(
            "runtime.restore_checkpoint", {"checkpoint_ref": checkpoint_ref}
        )
