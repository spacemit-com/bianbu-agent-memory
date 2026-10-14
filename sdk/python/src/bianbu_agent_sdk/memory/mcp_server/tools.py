"""Tool bodies for the bianbu.memory MCP surface.

The payloads match what the Runtime Tools returned before the MCP move so the
model sees the same JSON contract; only the transport changed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from ..errors import MemoryError, ServerUnavailable, failure_code
from ..ontology import (
    CONTENT_LIMITS,
    content_limit,
    known_slots,
    memory_slot,
    preference_slot,
    resolve_slot,
    validate_memory_text,
)
from ..runtime import get_memory_bridge
from ..types import MemoryKind
from .context import HostContext, memory_socket_path, memory_timeout

LOGGER = logging.getLogger("bianbu_agent_sdk.memory.mcp_server")

DEFAULT_CONFIRMATION_SECRET = "/etc/bianbu-agent-memory/adapter-confirmation.secret"
CONFIRM_TOKEN_ENV = "BIANBU_MEMORY_CONFIRM_TOKEN"
CONFIRM_TOKEN_FILE_ENV = "BIANBU_MEMORY_CONFIRM_TOKEN_FILE"
CONFIRMATION_TTL_SECONDS = 60
FORGET_METHOD = "long_term.forget"

_SESSION_HISTORY_ANSWER = (
    "这是会话历史问题，请使用 session_search 查询 Runtime 会话记录；"
    "memory_retrieve 只查询长期记忆，不能提供会话原文。"
)
_SESSION_HISTORY_TERMS = (
    "第一个会话", "第一个聊天", "最早的会话", "最早会话", "历史会话",
    "历史记录", "会话记录", "之前的会话", "上一个会话", "first session",
    "earliest session", "oldest session", "session history",
)
_NEGATIONS = (
    "不是", "并非", "而非", "不要", "不使用",
    "not", "isn't", "is not", "don't use", "do not use",
)


def dumps(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def error(code: str, message: str, **extra: object) -> str:
    return dumps({"success": False, "code": code, "error": message, **extra})


def _int_env(name: str, default: int, *, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return max(low, min(value, high))


async def bridge(host: HostContext, timeout: float | None = None):
    return await get_memory_bridge(
        socket_path=memory_socket_path(),
        timeout=memory_timeout() if timeout is None else timeout,
        account_id=host.account_id or None,
        agent_name=host.agent_id or None,
    )


def _confirmation_token() -> str:
    """Mint the one-shot token memoryd requires for destructive operations.

    memoryd only accepts tokens signed with the adapter secret, so an MCP
    child that runs as the service user mints its own instead of asking the
    Runtime for one.
    """

    direct = os.environ.get(CONFIRM_TOKEN_ENV, "").strip()
    if direct:
        return direct
    path = os.environ.get(CONFIRM_TOKEN_FILE_ENV, "").strip()
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    try:
        secret = Path(DEFAULT_CONFIRMATION_SECRET).read_bytes()
    except OSError:
        return ""
    if not secret:
        return ""
    expiry = int(time.time() * 1000) + CONFIRMATION_TTL_SECONDS * 1000
    nonce = secrets.token_urlsafe(24)
    payload = f"v1:{expiry}:{nonce}:{FORGET_METHOD}".encode("utf-8")
    return f"v1:{expiry}:{nonce}:{hashlib.sha256(secret + payload).hexdigest()}"


def _idempotency_key(run_id: str, operation: dict) -> str:
    encoded = json.dumps(operation, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return f"runtime:{run_id}:memory-tool:{hashlib.sha256(encoded).hexdigest()}"


def _one_operation(arguments: dict) -> tuple[dict | None, str | None]:
    operations = arguments.get("operations")
    if operations is None:
        return {
            key: arguments.get(key)
            for key in ("action", "content", "old_text", "memory_id", "slot")
        }, None
    if not isinstance(operations, list) or not operations:
        return None, error(
            "invalid_request", "an action or operations list is required"
        )
    if len(operations) != 1:
        return None, error(
            "batch_not_supported",
            "AgentMemory does not support atomic multi-operation batches",
        )
    if not isinstance(operations[0], dict):
        return None, error("invalid_operation", "each operation must be an object")
    return dict(operations[0]), None


def _text_matches(record_text: str, old_text: str) -> bool:
    """Match a locator without treating a negated value as the value to delete."""

    haystack = " ".join(record_text.casefold().split())
    needle = " ".join(old_text.casefold().split())
    if not needle or needle not in haystack:
        return False
    if haystack == needle:
        return True
    if any(marker in needle for marker in _NEGATIONS):
        return True
    offset = 0
    while True:
        index = haystack.find(needle, offset)
        if index < 0:
            return False
        prefix = haystack[max(0, index - 24):index].rstrip(" \t\r\n([{（【:：,，")
        if not any(prefix.endswith(marker) for marker in _NEGATIONS):
            return True
        offset = index + len(needle)


def _record_target(record) -> str:
    return (
        "user"
        if record.kind in {MemoryKind.PROFILE, MemoryKind.PREFERENCE}
        else "memory"
    )


def _record_order(record) -> tuple[int, int, str]:
    return (
        int(getattr(record, "updated_at", 0) or 0),
        int(getattr(record, "revision", 0) or 0),
        str(getattr(record, "memory_id", "")),
    )


def _candidate(record) -> dict[str, object]:
    return {
        "memory_id": record.memory_id,
        "kind": record.kind.value,
        "target": _record_target(record),
        "text": record.text,
        "revision": record.revision,
        "slot": memory_slot(getattr(record, "tags", ()), record.text),
    }


async def _manage(host: HostContext, target: str, arguments: dict) -> dict:
    if target not in CONTENT_LIMITS:
        return {
            "success": False,
            "code": "invalid_target",
            "error": "target must be memory or user",
        }
    operation, failure = _one_operation(arguments)
    if failure is not None:
        return json.loads(failure)
    assert operation is not None
    action = operation.get("action")
    content = operation.get("content")
    old_text = operation.get("old_text")
    memory_id = operation.get("memory_id")
    slot = operation.get("slot")
    if action not in {
        "add",
        "replace",
        "remove",
        "mark_important",
        "unmark_important",
    }:
        return {
            "success": False,
            "code": "invalid_operation",
            "error": (
                "action must be add, replace, remove, mark_important, or "
                "unmark_important"
            ),
        }
    if action in {"add", "replace"} and (
        not isinstance(content, str) or not content.strip()
    ):
        return {
            "success": False,
            "code": "invalid_operation",
            "error": "content is required for add/replace",
        }
    if slot is not None:
        if not isinstance(slot, str):
            return {
                "success": False,
                "code": "invalid_slot",
                "error": "slot must be a string",
            }
        slot = slot.strip()
        if not slot or len(slot) > 128 or not re.fullmatch(r"[A-Za-z0-9._-]+", slot):
            return {
                "success": False,
                "code": "invalid_slot",
                "error": "slot must contain only ASCII letters, digits, '.', '_' or '-'",
            }
        resolved = resolve_slot(slot)
        if resolved is None:
            return {
                "success": False,
                "code": "invalid_slot",
                "error": "slot must be one of the documented L3 slots",
                "slot": slot,
                "known_slots": list(known_slots()),
                "assistant_instruction": (
                    "Do not claim success. Retry without slot to store a free "
                    "memory, or use one of known_slots."
                ),
            }
        slot = resolved
    if action in {"add", "replace"} and isinstance(content, str):
        failure = validate_memory_text(target, content.strip())
        if failure is not None:
            return {
                "success": False,
                "code": failure,
                "error": (
                    "content exceeds the durable memory budget"
                    if failure == "content_too_long"
                    else "content contains unsupported control characters"
                ),
                "limit": content_limit(target),
                "length": len(content.strip()),
            }
    run_id = host.run_id
    session_id = host.session_id
    if action == "add" or (action == "replace" and slot):
        bridge_value = await bridge(host)
        upsert_explicit = getattr(bridge_value, "upsert_explicit", None)
        if callable(upsert_explicit):
            return await upsert_explicit(
                content=content.strip(),
                target=target,
                run_id=run_id,
                session_id=session_id,
                slot=slot,
            )
        if action == "add":
            recorded = await bridge_value.put_explicit(
                content=content.strip(),
                target=target,
                run_id=run_id,
                session_id=session_id,
            )
            return {
                "success": bool(recorded),
                "code": "recorded" if recorded else "backend_unavailable",
                "target": target,
            }
    if not isinstance(memory_id, str) or not memory_id.strip():
        memory_id = None
    if (
        memory_id is None
        and not slot
        and (not isinstance(old_text, str) or not old_text.strip())
    ):
        return {
            "success": False,
            "code": "invalid_operation",
            "error": "memory_id, slot, or old_text is required for replace/remove",
        }

    from ..client import DEFAULT_SOCKET, AsyncMemoryClient

    client = await AsyncMemoryClient.connect(
        memory_socket_path() or DEFAULT_SOCKET,
        timeout=memory_timeout(),
        account_id=host.account_id or None,
        agent_name=host.agent_id or None,
    )
    try:
        # The important mark is orthogonal to the content category, so a mark
        # action accepts a record of any kind instead of filtering to the
        # kinds the requested `target` normally owns.
        mark_action = action in {"mark_important", "unmark_important"}
        expected_kinds = (
            None
            if mark_action
            else (
                {MemoryKind.PROFILE, MemoryKind.PREFERENCE}
                if target == "user"
                else {
                    MemoryKind.FACT,
                    MemoryKind.PROJECT,
                    MemoryKind.EPISODIC,
                    MemoryKind.DECISION,
                    MemoryKind.REFERENCE,
                }
            )
        )
        if memory_id is not None:
            records = await client.long_term.get_many([memory_id.strip()])
            if not records:
                return {
                    "success": False,
                    "code": "not_found",
                    "error": "memory_id was not found",
                    "memory_id": memory_id.strip(),
                }
            if expected_kinds is not None and records[0].kind not in expected_kinds:
                return {
                    "success": False,
                    "code": "target_mismatch",
                    "error": "memory_id does not belong to the requested target",
                    "candidate": _candidate(records[0]),
                    "recommended_target": _record_target(records[0]),
                }
            matches = records
        else:
            query = "" if slot else str(old_text or "")
            headers = await client.long_term.search_headers(
                query, top_k=100 if slot else 20
            )
            records = await client.long_term.get_many(
                [header.memory_id for header in headers]
            )
            located = [
                record
                for record in records
                if (
                    memory_slot(getattr(record, "tags", ()), record.text) == slot
                    if slot
                    else _text_matches(record.text, str(old_text))
                )
            ]
            matches = [
                record
                for record in located
                if expected_kinds is None or record.kind in expected_kinds
            ]
            if not matches and located:
                recommended = _record_target(max(located, key=_record_order))
                return {
                    "success": False,
                    "code": "target_mismatch",
                    "error": "matching memories belong to a different target",
                    "recommended_target": recommended,
                    "candidates": [_candidate(record) for record in located[:5]],
                    "assistant_instruction": (
                        "Do not claim success. Retry with recommended_target and "
                        "prefer memory_id or slot."
                    ),
                }
        if not matches:
            return {
                "success": False,
                "code": "not_found",
                "error": "no matching memory was found",
                "candidates": [],
                "assistant_instruction": (
                    "Do not claim the memory was changed because success is false."
                ),
            }
        if len(matches) > 1:
            matched_slots = {
                memory_slot(getattr(record, "tags", ()), record.text)
                for record in matches
            }
            matched_slots.discard(None)
            if action != "replace" or len(matched_slots) != 1:
                return {
                    "success": False,
                    "code": "ambiguous_match",
                    "error": (
                        f"locator matches {len(matches)} memories; retry with memory_id"
                    ),
                    "candidates": [_candidate(record) for record in matches],
                    "assistant_instruction": (
                        "Do not claim success. Select one returned memory_id."
                    ),
                }
        record = max(matches, key=_record_order)
        if mark_action:
            receipt = await client.long_term.mark_important(
                record, important=action == "mark_important"
            )
            return {
                "success": True,
                "code": (
                    "marked_important"
                    if action == "mark_important"
                    else "unmarked_important"
                ),
                "target": target,
                "memory_id": record.memory_id,
                "revision": receipt.revision,
                "important": action == "mark_important",
                "unchanged": bool(receipt.raw.get("unchanged")),
            }
        if action == "remove":
            confirm_token = _confirmation_token()
            if not confirm_token:
                return {
                    "success": False,
                    "code": "confirmation_unavailable",
                    "error": "AgentMemory confirmation token is not configured",
                }
            job = await client.long_term.forget(
                record.memory_id,
                confirm_token=confirm_token,
                reason_code="user_request",
                idempotency_key=_idempotency_key(run_id, operation),
            )
            status = await client.long_term.wait_forget(job.job_id)
            return {
                "success": bool(status.get("local_hidden")),
                "code": "forgotten" if status.get("local_hidden") else "forget_pending",
                "target": target,
                "memory_id": record.memory_id,
                "job_id": job.job_id,
                "state": status.get("state"),
                "local_hidden": bool(status.get("local_hidden")),
                "physical_cleanup_pending": bool(
                    status.get("physical_cleanup_pending")
                ),
            }
        revision_slot = (
            slot
            or preference_slot(content.strip())
            or memory_slot(getattr(record, "tags", ()), record.text)
        )
        tags = list(getattr(record, "tags", ()) or ())
        if revision_slot:
            tags = list(
                dict.fromkeys(
                    (
                        *(tag for tag in tags if not str(tag).startswith("slot:")),
                        f"slot:{revision_slot}",
                    )
                )
            )
        receipt = await client.long_term.revise(
            record.memory_id,
            text=content.strip(),
            expected_revision=record.revision,
            summary=getattr(record, "summary", None),
            tags=tags,
            evidence=getattr(record, "evidence", ()),
            idempotency_key=_idempotency_key(run_id, operation),
        )
        convergence: dict[str, object] = {}
        reconcile = getattr(client.long_term, "reconcile_slots", None)
        if revision_slot and reconcile is not None:
            value = await reconcile(slots=[revision_slot], dry_run=False)
            if isinstance(value, dict):
                convergence = value
        return {
            "success": True,
            "code": "slot_revised" if revision_slot else "revised",
            "target": target,
            "memory_id": receipt.memory_id,
            "revision": receipt.revision,
            **({"slot": revision_slot} if revision_slot else {}),
            "duplicates_reconciled": int(
                convergence.get("superseded_memories") or 0
            ),
        }
    finally:
        await client.close()


def _mutation_failure(error: BaseException) -> dict[str, object]:
    """Report a failed mutation the way a failed recall already is.

    A service that cannot be reached is ``backend_unavailable``; anything
    memoryd itself refused keeps its own code, so a rejected write can never be
    read as a dead service. Folding every exception into ``backend_unavailable``
    is how an ``Unauthenticated`` write looked like an outage for a whole
    debugging session (K3, 2026-09-22).
    """

    if isinstance(error, MemoryError) and not isinstance(error, ServerUnavailable):
        return {
            "success": False,
            "code": failure_code(error),
            "error": str(error),
        }
    reason = str(error).strip()
    detail = f"{type(error).__name__}: {reason}" if reason else type(error).__name__
    return {
        "success": False,
        "code": "backend_unavailable",
        "error": f"AgentMemory is unavailable ({detail})",
    }


async def memory(
    host: HostContext,
    *,
    target: str,
    action: str | None = None,
    content: str | None = None,
    old_text: str | None = None,
    memory_id: str | None = None,
    slot: str | None = None,
    operations: list[Any] | None = None,
) -> str:
    arguments = {
        key: value
        for key, value in {
            "target": target,
            "action": action,
            "content": content,
            "old_text": old_text,
            "memory_id": memory_id,
            "slot": slot,
            "operations": operations,
        }.items()
        if value is not None
    }
    try:
        payload = await asyncio.wait_for(
            _manage(host, target, arguments), timeout=memory_timeout() + 0.5
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — the Tool contract is JSON, never a raise
        LOGGER.warning("AgentMemory mutation failed: %s", exc)
        payload = _mutation_failure(exc)
    return dumps(payload)


async def memory_record(host: HostContext, *, content: str) -> str:
    text = str(content or "").strip()
    if not text:
        return "memory content is empty"
    outcome: object = None
    try:
        bridge_value = await bridge(host)
        upsert = getattr(bridge_value, "upsert_explicit", None)
        if callable(upsert):
            outcome = await asyncio.wait_for(
                upsert(
                    content=text,
                    target="memory",
                    run_id=host.run_id,
                    session_id=host.session_id,
                ),
                timeout=memory_timeout(),
            )
        else:
            recorded = await asyncio.wait_for(
                bridge_value.put_explicit(
                    content=text,
                    target="memory",
                    run_id=host.run_id,
                    session_id=host.session_id,
                ),
                timeout=memory_timeout(),
            )
            outcome = {"success": bool(recorded)}
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("AgentMemory explicit record unavailable: %s", exc)
        return "AgentMemory unavailable; memory was not recorded"
    if isinstance(outcome, dict) and outcome.get("success") is True:
        return "memory recorded"
    # A bare "unavailable" made a rejected write indistinguishable from a dead
    # backend, which is how an InvalidArgument stayed hidden for a whole
    # debugging session. Say what the backend actually returned.
    reason = ""
    if isinstance(outcome, dict):
        reason = str(outcome.get("error") or outcome.get("code") or "")
    if not reason:
        return "AgentMemory unavailable; memory was not recorded"
    return f"AgentMemory could not record the memory ({reason})"


def _recall_item(value: object) -> dict[str, object]:
    serializer = getattr(value, "to_dict", None)
    if callable(serializer):
        result = serializer()
        if isinstance(result, dict):
            return result
    if isinstance(value, dict):
        return dict(value)
    return {"text": str(value), "legacy": True}


def _asks_for_session_history(query: str) -> bool:
    normalized = " ".join(str(query or "").casefold().split())
    return any(term in normalized for term in _SESSION_HISTORY_TERMS)


async def _recall_outcome(
    bridge_value: object, text: str, *, top_k: int, max_chars: int
) -> dict[str, object]:
    """Return the recalled records plus whatever the memory service reported.

    ``recall_structured_with_status`` is preferred because it keeps an empty
    memory apart from a refused read. A bridge without it is still driven, but
    its failures stay invisible, which is what this Tool used to report.
    """

    status = getattr(bridge_value, "recall_structured_with_status", None)
    if callable(status):
        outcome = await status(
            text, top_k=top_k, max_chars=max_chars, include_explicit=False
        )
        if isinstance(outcome, dict):
            return outcome
    structured = getattr(bridge_value, "recall_structured", None)
    recalled = (
        await structured(
            text, top_k=top_k, max_chars=max_chars, include_explicit=False
        )
        if callable(structured)
        else await bridge_value.recall(text, top_k=top_k, max_chars=max_chars)
    )
    return {"success": True, "items": tuple(recalled)}


async def memory_retrieve(host: HostContext, *, query: str | None = None) -> str:
    text = str(query or "")
    if _asks_for_session_history(text):
        return _SESSION_HISTORY_ANSWER
    # 20 is the effective ceiling: the SDK clamps the window to 20 records, so a
    # larger value here would only promise entries that cannot be served.
    top_k = _int_env("BIANBU_MEMORY_RECALL_TOP_K", 5, low=1, high=20)
    max_chars = _int_env("BIANBU_MEMORY_RECALL_MAX_CHARS", 4000, low=1, high=100000)
    # The recall window is a bounded read, so the answer has to say whether it
    # was cut.  `truncated` from the bridge is the authoritative verdict, and the
    # probe below is the fallback for a bridge that predates the field: asking
    # for one entry past the window is evidence that the store holds more.
    # `count` alone cannot tell a full window from a complete store (K3,
    # 2026-09-28: a window that happened to be exactly five entries long reached
    # the user as all five memories).
    probe_k = top_k + 1
    try:
        bridge_value = await bridge(host)
        outcome = await asyncio.wait_for(
            _recall_outcome(bridge_value, text, top_k=probe_k, max_chars=max_chars),
            timeout=memory_timeout(1.5),
        )
    except asyncio.CancelledError:
        raise
    except MemoryError as refused:
        # A refused read is not an empty memory. Reporting no_match here is how
        # an expired Runtime registration looked like an empty memory for days.
        LOGGER.warning("AgentMemory retrieval was refused: %s", refused)
        return error(failure_code(refused), str(refused), query=text)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("AgentMemory retrieval unavailable: %s", exc)
        return error(
            "backend_unavailable",
            f"AgentMemory retrieval failed: {exc}",
            query=text,
        )
    if outcome.get("success") is not True:
        return dumps({**outcome, "query": text})
    memories = [_recall_item(item) for item in outcome.get("items") or ()]
    # The bridge sees the caps it applied (window and character budget); the
    # probe cannot see either once the window sits at the 20-record ceiling.
    has_more = bool(outcome.get("truncated")) or len(memories) > top_k
    del memories[top_k:]
    return dumps(
        {
            "success": True,
            "code": "recalled" if memories else "no_match",
            "query": text,
            "count": len(memories),
            "has_more": has_more,
            "memories": memories,
            "mutation_hint": (
                "Use memory_id for replace/remove. For a stable preference "
                "slot, replace by slot and use the returned target."
            ),
        }
    )
