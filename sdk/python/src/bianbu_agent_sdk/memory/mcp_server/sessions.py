"""Cross-session transcript search for the bianbu.memory MCP surface."""

from __future__ import annotations

import asyncio
import json
import logging

from ..client import DEFAULT_SOCKET, AsyncMemoryClient
from ..temporal import format_epoch_ms_display
from .context import HostContext, memory_socket_path

LOGGER = logging.getLogger("bianbu_agent_sdk.memory.mcp_server")

SEARCH_TIMEOUT_SECONDS = 8.0
STATUS_SESSION_LIMIT = 20
DISCOVERY_EVENT_LIMIT = 200
READ_EVENT_LIMIT = 5000


def _is_origin_query(query: str) -> bool:
    normalized = " ".join(query.casefold().split())
    return any(
        term in normalized
        for term in (
            "第一个会话",
            "第一个聊天",
            "最早的会话",
            "最早会话",
            "first session",
            "earliest session",
            "oldest session",
            "session history",
        )
    )


def _transcript_messages(events) -> list[dict[str, str]]:
    """Project AgentMemory session events into user/assistant messages.

    ``task_started``/``task_failed`` watermarks carry the request text of runs
    that never completed a turn, so in-progress and interrupted sessions stay
    readable; when the same run later has a completed ``user_message`` the
    watermark is dropped to avoid duplicating the request.
    """

    ordered = sorted(
        (event for event in events if isinstance(event, dict)),
        key=lambda event: int(event.get("event_sequence") or 0),
    )
    paired_runs = set()
    for event in ordered:
        if event.get("event_type") != "user_message":
            continue
        payload = event.get("payload")
        run_id = str(payload.get("run_id") or "") if isinstance(payload, dict) else ""
        if run_id:
            paired_runs.add(run_id)
    messages: list[dict[str, str]] = []
    for event in ordered:
        event_type = str(event.get("event_type") or "")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        text = str(payload.get("text") or "").strip()
        if not text:
            continue
        run_id = str(payload.get("run_id") or "")
        if event_type == "user_message":
            messages.append({"role": "user", "content": text})
        elif event_type == "assistant_message":
            messages.append({"role": "assistant", "content": text})
        elif event_type in ("task_started", "task_failed"):
            if run_id and run_id in paired_runs:
                continue
            messages.append({"role": "user", "content": text})
    return messages


def _stamp(value: object) -> dict[str, object]:
    """Return a persisted time as the machine stamp plus a printable form.

    ``last_active_at`` is bare epoch milliseconds, which reads as a corrupted
    timestamp when a consumer renders the number directly. Callers spread the
    result into their payload so the model always has a quotable alternative.
    """

    epoch_ms = int(value or 0)
    stamp: dict[str, object] = {"last_active_at": epoch_ms}
    if epoch_ms > 0:
        stamp["last_active_at_display"] = format_epoch_ms_display(epoch_ms)
    return stamp


def _status_entries(response) -> list[dict[str, object]]:
    if not isinstance(response, dict):
        return []
    entries = []
    for item in response.get("sessions") or ():
        if not isinstance(item, dict):
            continue
        session_id = str(item.get("session_id") or "").strip()
        if not session_id:
            continue
        entries.append(
            {
                "session_id": session_id,
                "title": str(item.get("title") or ""),
                **_stamp(item.get("last_active_at")),
                "completed_turn_count": int(item.get("completed_turn_count") or 0),
                "in_progress": bool(item.get("in_progress")),
                "interrupted": bool(item.get("interrupted")),
            }
        )
    return entries


def _clamp_int(value, *, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(number, high))


def _dumps(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _error(code: str, message: str) -> str:
    return _dumps({"success": False, "code": code, "error": message})


async def _fetch_status(client, current_session_id: str) -> list[dict[str, object]]:
    response = await client.sessions.get_recent_context(
        exclude_session_id=current_session_id or None,
        selection_mode="session_status",
        session_limit=STATUS_SESSION_LIMIT,
    )
    return _status_entries(response)


async def _search(client, current_session_id: str, arguments: dict) -> str:
    limit = _clamp_int(arguments.get("limit"), default=3, low=1, high=10)
    requested_session = str(arguments.get("session_id") or "").strip()
    if requested_session:
        history = await client.sessions.get_history(
            requested_session, limit=READ_EVENT_LIMIT
        )
        events = history.get("events") if isinstance(history, dict) else None
        messages = _transcript_messages(events or ())
        payload: dict[str, object] = {
            "success": True,
            "session_id": requested_session,
            "messages": messages,
        }
        if not messages:
            payload["note"] = (
                "no recorded turns for this session id (unknown id, or only "
                "unrecorded activity)"
            )
        return _dumps(payload)

    sessions = await _fetch_status(client, current_session_id)
    query = str(arguments.get("query") or "").strip()
    if query:
        origin = _is_origin_query(query)
        oldest_first = origin or arguments.get("sort") == "oldest"
        ordered = list(reversed(sessions)) if oldest_first else sessions
        if origin:
            selected = []
            for entry in ordered:
                history = await client.sessions.get_history(
                    str(entry["session_id"]), limit=READ_EVENT_LIMIT
                )
                events = history.get("events") if isinstance(history, dict) else None
                messages = _transcript_messages(events or ())
                if not messages:
                    continue
                selected.append(
                    {
                        "session_id": entry["session_id"],
                        "title": entry["title"],
                        **_stamp(entry["last_active_at"]),
                        "messages": messages,
                    }
                )
                if len(selected) >= limit:
                    break
            return _dumps(
                {
                    "success": True,
                    "query": query,
                    "selection_reason": "oldest_session",
                    "sessions": selected,
                }
            )
        folded = query.casefold()
        matches = []
        for entry in ordered:
            history = await client.sessions.get_history(
                str(entry["session_id"]), limit=DISCOVERY_EVENT_LIMIT
            )
            events = history.get("events") if isinstance(history, dict) else None
            for message in _transcript_messages(events or ()):
                if folded not in message["content"].casefold():
                    continue
                matches.append(
                    {
                        "session_id": entry["session_id"],
                        "title": entry["title"],
                        **_stamp(entry["last_active_at"]),
                        **message,
                    }
                )
                if len(matches) >= limit:
                    break
            if len(matches) >= limit:
                break
        return _dumps({"success": True, "query": query, "matches": matches})

    ordered = (
        list(reversed(sessions)) if arguments.get("sort") == "oldest" else sessions
    )
    return _dumps({"success": True, "sessions": ordered[:limit]})


async def session_search(
    host: HostContext,
    *,
    query: str | None = None,
    session_id: str | None = None,
    limit: int = 3,
    sort: str | None = None,
) -> str:
    arguments = {
        key: value
        for key, value in {
            "query": query,
            "session_id": session_id,
            "limit": limit,
            "sort": sort,
        }.items()
        if value is not None
    }
    try:
        client = await AsyncMemoryClient.connect(
            memory_socket_path() or DEFAULT_SOCKET,
            timeout=2.0,
            account_id=host.account_id or None,
            agent_name=host.agent_id or None,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("AgentMemory session search connect failed: %s", exc)
        return _error("memory_unavailable", f"AgentMemory is unavailable: {exc}")
    try:
        return await asyncio.wait_for(
            _search(client, host.session_id, arguments),
            timeout=SEARCH_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return _error(
            "memory_timeout",
            "AgentMemory session search timed out; retry with a narrower "
            "query, a session_id read, or a browse call",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("AgentMemory session search failed: %s", exc)
        return _error("memory_unavailable", f"Session search failed: {exc}")
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            LOGGER.debug("AgentMemory client close failed", exc_info=True)
