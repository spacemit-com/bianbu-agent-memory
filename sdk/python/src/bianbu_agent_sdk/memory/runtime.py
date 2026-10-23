from __future__ import annotations

import asyncio
import hashlib
import json
import os
import urllib.request
import re
from datetime import datetime

from .client import AsyncMemoryClient, DEFAULT_SOCKET
from .errors import ErrorContext, MemoryError, ServerUnavailable, failure_code
from .mem0 import Mem0Coordinator
from .ontology import (
    content_limit,
    field_value_failure,
    known_slots,
    memory_slot,
    preference_slot,
    resolve_field_value,
    resolve_slot,
    validate_memory_text,
)
from .types import EvidenceRef, MemoryKind, MemoryRecallItem, SyncPolicy
from .temporal import (
    current_time_context,
    extract_reminder,
    from_epoch_ms,
    now_local,
    normalize_relative_dates,
)


def _configured_socket(socket_path: str | None) -> str:
    return socket_path or os.environ.get("BIANBU_AGENT_MEMORY_SOCKET") or DEFAULT_SOCKET


def _key(run_id: str, suffix: str) -> str:
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    return f"personal-agent:{digest}:{suffix}"


def _failure_payload(error: BaseException) -> dict[str, object]:
    """Report a memoryd failure the way every Tool path must.

    An unreachable service is ``backend_unavailable``; anything memoryd itself
    rejected keeps its own code, so a refused identity can never be read as
    "this user has no memories" (reads) or as a dead service (writes).
    """

    if isinstance(error, MemoryError) and not isinstance(error, ServerUnavailable):
        return {
            "success": False,
            "code": failure_code(error),
            "error": str(error),
        }
    return {"success": False, "code": "backend_unavailable"}


def _unavailable_error(message: str) -> ServerUnavailable:
    return ServerUnavailable(
        ErrorContext(
            code="ServerUnavailable",
            message=message,
            request_id="unknown",
            trace_id="unknown",
            retryable=True,
        )
    )


def _operation_key(run_id: str, event_type: str, operation_id: str | None) -> str:
    suffix = f"operation:{event_type}"
    if operation_id:
        suffix = f"{suffix}:{hashlib.sha256(operation_id.encode('utf-8')).hexdigest()[:24]}"
    return _key(run_id, suffix)


_EVIDENCE_ID_MAX_BYTES = 256
_EVIDENCE_ID_EXTRA = frozenset("-_.:/")


def session_evidence(session_id: object) -> tuple[EvidenceRef, ...]:
    """Return the session evidence memoryd will accept, or nothing.

    memoryd rejects a write whose evidence ID is empty or outside its
    identifier alphabet, so an unmanaged caller that has no Runtime session
    (or a malformed one) records the memory without attestation instead of
    turning the whole write into an ``InvalidArgument``.
    """

    value = str(session_id or "").strip()
    encoded = value.encode("utf-8")
    if not value or len(encoded) > _EVIDENCE_ID_MAX_BYTES:
        return ()
    if not all(
        character.isascii()
        and (character.isalnum() or character in _EVIDENCE_ID_EXTRA)
        for character in value
    ):
        return ()
    return (EvidenceRef("session", value),)


def _clip(value: object, limit: int) -> str:
    text = str(value or "")
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def _render_memory_text(memory: object, *, current_date: object | None = None) -> str:
    """Render temporal memories with an unambiguous absolute-date status.

    The original reminder sentence is retained for auditability, but must not
    be allowed to make the model reinterpret ``明天`` relative to a later
    request.  The due-date tag is the authoritative value written at ingest.
    """

    text = str(getattr(memory, "text", "") or "")
    tags = tuple(getattr(memory, "tags", ()) or ())
    due_tag = next((tag for tag in tags if str(tag).startswith("due-date:")), "")
    if "temporal:reminder" not in tags or not due_tag:
        return text
    due_date = due_tag.removeprefix("due-date:")
    today = (current_date or now_local().date())
    today_iso = getattr(today, "isoformat", lambda: str(today))()
    if due_date == today_iso:
        status = "今天到期（应在今天提醒）"
    elif due_date < today_iso:
        status = "已到期（原定日期已过）"
    else:
        status = f"未来日期（{due_date}）"
    return (
        f"{text}\n"
        f"【绝对日期结论】计划日期={due_date}；当前日期={today_iso}；状态={status}。"
        "原始内容中的相对日期仅供审计，禁止按当前提问日期重新解释。"
    )


def _dplus_enabled() -> bool:
    return os.environ.get("BIANBU_MEMORY_ENGINE", "scheme-a").strip().casefold() in {
        "dplus",
        "d+",
        "upstream-mem0",
        "mem0-oss",
    }


def _recall_min_relevance() -> float:
    """Return the cosine floor applied to semantic-only recall (B2 / MEM-L3-02).

    Keyword hits are exempt because the store already reports them with
    relevance 1.0: only vector-only evidence has to clear this floor. The value
    is calibrated on K3 and overridable for tuning.
    """

    try:
        value = float(os.environ.get("BIANBU_MEMORY_RECALL_MIN_RELEVANCE", "0.35"))
    except ValueError:
        value = 0.35
    return max(0.0, min(value, 1.0))


def _contains_credential(value: str) -> bool:
    lowered = value.casefold()
    return any(
        marker in lowered
        for marker in (
            "password",
            "passwd",
            "api key",
            "api_key",
            "access token",
            "refresh token",
            "token=",
            "token:",
            "secret=",
            "secret:",
            "authorization:",
            "x-api-key",
            "--token ",
            "--api-key ",
            "bearer ",
            "private key",
            "ssh key",
            "密码",
            "口令",
            "令牌",
            "密钥",
        )
    )


def _contains_pii(value: str) -> bool:
    lowered = value.casefold()
    return any(
        marker in lowered
        for marker in (
            "phone",
            "telephone",
            "mobile number",
            "email",
            "e-mail",
            "address",
            "location",
            "my name is",
            "call me",
            "手机号",
            "电话号码",
            "邮箱",
            "电子邮件",
            "家庭住址",
            "地址",
            "位置",
            "我叫",
            "我的名字",
            "我的姓名",
            "请叫我",
            "身份证",
            "人脸",
        )
    )


def _contains_profile_name(value: str) -> bool:
    lowered = value.casefold()
    return any(
        marker in lowered
        for marker in (
            "my name is",
            "call me",
            "我叫",
            "我的名字",
            "我的姓名",
            "请叫我",
        )
    )


def _query_terms(value: str) -> set[str]:
    lowered = value.casefold()
    terms = set(re.findall(r"[a-z0-9][a-z0-9_.+-]{1,}", lowered))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
    terms.update(chinese[index : index + 2] for index in range(max(0, len(chinese) - 1)))
    return {
        item
        for item in terms
        if item not in {"今天", "什么", "事情", "需要", "有什", "么事", "我吗"}
    }


def _usable_recall_text(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    question_marks = text.count("?") + text.count("？")
    if question_marks > 3 and question_marks * 3 >= len(text):
        return False
    return bool(re.search(r"[A-Za-z0-9\u4e00-\u9fff]", text))


def _relative_activity(last_active_at: int, current: datetime) -> str:
    """Render a compact ``N分钟前活跃`` annotation for a status line."""

    if last_active_at <= 0:
        return "last_active=unknown"
    delta_seconds = max(0.0, (current - from_epoch_ms(last_active_at)).total_seconds())
    if delta_seconds < 60:
        return "刚刚活跃"
    if delta_seconds < 3600:
        return f"{int(delta_seconds // 60)}分钟前活跃"
    if delta_seconds < 24 * 3600:
        return f"{int(delta_seconds // 3600)}小时前活跃"
    return f"{int(delta_seconds // (24 * 3600))}天前活跃"


class MemoryBridge:
    """Best-effort Personal Agent lifecycle bridge to the local memory daemon."""

    async def prepare_context(
        self, *, query: str, session_id: str, config: dict | None = None
    ) -> dict | None:
        """Build bounded prompt context; the host only injects the result.

        Identity belongs to this bridge, never to model-provided text/config.
        This method performs reads only; summary compensation belongs to memoryd.
        """
        from .context import prepare_context

        try:
            return await asyncio.wait_for(
                prepare_context(self, query=query, session_id=session_id, config=config),
                timeout=(config or {}).get("timeout_seconds", self.timeout),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return None

    async def record_runtime_result(
        self, *, run_id: str, session_id: str, agent_id: str,
        user_text: str, assistant_text: str, status: str,
        metadata: dict | None = None, config: dict | None = None,
    ) -> bool:
        """Record a host result; lifecycle flags must come from trusted host metadata."""
        from .lifecycle import record_runtime_result

        return await record_runtime_result(self, {
            "run_id": run_id, "session_id": session_id, "agent_id": agent_id,
            "request": user_text, "response": assistant_text, "status": status,
            "metadata": metadata or {}, "config": config or {},
        })

    def __init__(
        self,
        socket_path: str | None = None,
        timeout: float = 2.0,
        account_id: str | None = None,
        agent_name: str | None = None,
        coordinator: Mem0Coordinator | None = None,
    ) -> None:
        self.socket_path = _configured_socket(socket_path)
        self.timeout = timeout
        self.account_id = account_id or os.environ.get(
            "BIANBU_AGENT_MEMORY_ACCOUNT_ID", "default"
        )
        self.agent_name = agent_name or os.environ.get(
            "BIANBU_AGENT_MEMORY_AGENT_NAME"
        )
        self.coordinator = coordinator or Mem0Coordinator()
        self._client: AsyncMemoryClient | None = None
        self._lock = asyncio.Lock()
        self._connect_error: BaseException | None = None

    async def _recall_dplus(
        self, query: str, *, top_k: int, max_chars: int
    ) -> tuple[MemoryRecallItem, ...]:
        url = os.environ.get(
            "BIANBU_MEMORY_DPLUS_URL", "http://127.0.0.1:18920"
        ).rstrip("/")
        timeout = float(os.environ.get("BIANBU_MEMORY_DPLUS_SEARCH_TIMEOUT", "1.2"))

        def request() -> list[dict[str, object]]:
            body = json.dumps(
                {"query": query, "top_k": top_k}, ensure_ascii=False
            ).encode("utf-8")
            http_request = urllib.request.Request(
                f"{url}/v1/search",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(http_request, timeout=timeout) as response:
                payload = json.load(response)
            values = payload.get("results", ()) if isinstance(payload, dict) else ()
            return [item for item in values if isinstance(item, dict)]

        try:
            values = await asyncio.to_thread(request)
        except (OSError, TimeoutError, ValueError, json.JSONDecodeError):
            return ()
        result: list[MemoryRecallItem] = []
        used = 0
        for item in values:
            text = _clip(item.get("text"), max_chars - used)
            memory_id = str(item.get("memory_id") or "").strip()
            if not text or not memory_id:
                continue
            slot = str(item.get("slot") or "").strip()
            try:
                kind = MemoryKind(str(item.get("kind") or MemoryKind.FACT.value))
            except ValueError:
                kind = MemoryKind.FACT
            score = float(item.get("score") or 0.0)
            result.append(
                MemoryRecallItem(
                    memory_id=memory_id,
                    kind=kind,
                    text=text,
                    revision=int(item.get("revision") or 1),
                    source="mem0-dplus",
                    text_truncated=text != str(item.get("text") or ""),
                    score=score,
                    # mem0 answers similarities, so the same floor applies.
                    relevance=score,
                    slot=slot or None,
                    provider=str(item.get("provider") or "mem0-dplus"),
                    tags=tuple(str(tag) for tag in (item.get("tags") or ())),
                    updated_at=(
                        int(item["updated_at"])
                        if item.get("updated_at") is not None
                        else None
                    ),
                )
            )
            used += len(text.encode("utf-8"))
            if used >= max_chars:
                break
        return tuple(result)

    async def _client_or_none(self) -> AsyncMemoryClient | None:
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is not None:
                return self._client
            try:
                self._client = await AsyncMemoryClient.connect(
                    self.socket_path,
                    self.timeout,
                    account_id=self.account_id,
                    agent_name=self.agent_name,
                )
            except (OSError, ConnectionError, ServerUnavailable, MemoryError) as error:
                # Keep the reason. A refused identity and an empty memory look
                # the same once the failure is dropped here.
                self._connect_error = error
                return None
            self._connect_error = None
            return self._client

    async def close(self) -> None:
        async with self._lock:
            client, self._client = self._client, None
        if client is not None:
            await client.close()

    async def settings(self) -> dict[str, object]:
        """Return AgentMemory-owned Runtime/BiBit settings."""

        client = await self._client_or_none()
        if client is None:
            return {}
        try:
            result = await client.privacy.settings()
        except (OSError, ConnectionError, ServerUnavailable, MemoryError):
            return {}
        return result if isinstance(result, dict) else {}

    async def recall(
        self,
        query: str,
        *,
        top_k: int = 5,
        max_chars: int = 6000,
        include_explicit: bool = True,
    ) -> tuple[str, ...]:
        items = await self.recall_structured(
            query,
            top_k=top_k,
            max_chars=max_chars,
            include_explicit=include_explicit,
        )
        return tuple(
            f"[{item.kind.value} rev={item.revision} source={item.source} "
            f"score={item.score:.3f}"
            f"{' slot=' + item.slot if item.slot else ''}] {item.text}"
            for item in items
        )

    async def recall_structured(
        self,
        query: str,
        *,
        top_k: int = 5,
        max_chars: int = 4000,
        include_explicit: bool = True,
    ) -> tuple[MemoryRecallItem, ...]:
        """Return bounded canonical records, degrading to nothing on failure.

        Best effort by contract: a before_run Hook injects whatever memory is
        readable and must never fail a run because memoryd is unreachable or
        refused this process. Callers that have to tell "nothing matched" from
        "the backend said no" use :meth:`recall_structured_with_status`.
        """

        items, _, _ = await self._recall_once(
            query,
            top_k=top_k,
            max_chars=max_chars,
            include_explicit=include_explicit,
        )
        return items

    async def recall_structured_with_status(
        self,
        query: str,
        *,
        top_k: int = 5,
        max_chars: int = 4000,
        include_explicit: bool = True,
    ) -> dict[str, object]:
        """Return the recalled records together with the service' own verdict.

        An empty result is reported as ``no_match`` only when memoryd actually
        answered. A rejected identity or an unreachable socket is reported as
        ``success: false`` with the backend code, because collapsing both into
        an empty list is what made an expired Runtime registration look like an
        empty memory for days.

        ``truncated`` says whether the window was cut. Callers that answer
        "these are your memories" have to repeat it, because the applied caps
        are invisible in ``items``.
        """

        items, error, truncated = await self._recall_once(
            query,
            top_k=top_k,
            max_chars=max_chars,
            include_explicit=include_explicit,
        )
        if error is not None:
            return _failure_payload(error)
        return {
            "success": True,
            "code": "recalled" if items else "no_match",
            "truncated": truncated,
            "items": items,
        }

    async def _recall_once(
        self,
        query: str,
        *,
        top_k: int = 5,
        max_chars: int = 4000,
        include_explicit: bool = True,
    ) -> tuple[tuple[MemoryRecallItem, ...], BaseException | None, bool]:
        """Recall once, returning the records, the backend failure, and truncation.

        ``truncated`` is the store's own verdict that the window was cut: at
        least one further record qualified but did not fit ``top_k`` or
        ``max_chars``.  A caller that answers "these are your memories" needs
        it, because the caps applied here are invisible in the records
        themselves, and a window that is exactly as long as the cap looks
        identical to a complete store.
        """

        top_k = max(1, min(int(top_k), 20))
        max_chars = max(256, min(int(max_chars), 12000))
        semantic: tuple[MemoryRecallItem, ...] = ()
        if _dplus_enabled() and query.strip():
            semantic = await self._recall_dplus(
                query, top_k=top_k, max_chars=max_chars
            )
        client = await self._client_or_none()
        if client is None:
            return semantic, (
                self._connect_error
                or _unavailable_error("memory service is unreachable")
            ), False
        try:
            reminder_query = bool(
                re.search(r"(?:提醒|待办|日程|remind|todo|schedule)", query, re.I)
            )
            if reminder_query:
                await self._materialize_historical_reminders(client)
            headers = (
                await client.long_term.search_headers(query, top_k=top_k)
                if query.strip()
                else []
            )
            temporal_headers = (
                await client.long_term.search_headers("提醒事项", top_k=100)
                if reminder_query
                else []
            )
            # Automatic prompt injection must not fill a query's empty result
            # with unrelated profile records. Unordered/manual reads retain
            # the canonical snapshot for backwards-compatible listing.
            explicit_headers = (
                await client.long_term.search_headers(
                    "", top_k=min(100, max(20, top_k * 4))
                )
                if include_explicit or not query.strip()
                else []
            )
            header_ids = list(
                dict.fromkeys(
                    header.memory_id
                    for header in (*headers, *temporal_headers)
                )
            )
            all_records = await client.long_term.get_many(
                list(dict.fromkeys((*header_ids, *(item.memory_id for item in explicit_headers))))
            )
            # A read that lists memories has to carry everything this pipeline
            # wrote for the user.  Automatic extraction tags its own writes
            # "mem0-derived" and nothing else, so leaving that tag out of this
            # set hid every learned preference from an unordered read: the
            # planner stored "回答要简洁清晰易懂", the record was in the store,
            # and "输出记忆" still answered with one entry out of five
            # (91.67, 2026-09-30).
            explicit_records = [
                memory
                for memory in all_records
                if memory.kind is MemoryKind.PROFILE
                or (
                    "personal-agent" in memory.tags
                    and any(tag in {"user", "memory"} for tag in memory.tags)
                )
                or "mem0-derived" in memory.tags
            ]
            if not include_explicit and query.strip():
                # Only backend query hits (with their real relevance) qualify.
                # Token overlap on a snapshot is not a relevance judgement.
                explicit_records = []
            by_id = {memory.memory_id: memory for memory in all_records}
            query_records = [
                by_id[memory_id]
                for memory_id in header_ids
                if memory_id in by_id
            ]
            if reminder_query:
                # FTS tokenization for Chinese is not stable across SQLite
                # builds. The temporal tag is the authoritative reminder
                # index, so include it from the bounded canonical snapshot.
                query_records.extend(
                    memory
                    for memory in all_records
                    if "temporal:reminder" in memory.tags
                    and memory.memory_id not in {item.memory_id for item in query_records}
                )
                today = now_local().date().isoformat()
                dated = [
                    memory
                    for memory in query_records
                    if any(
                        tag == f"due-date:{today}" for tag in memory.tags
                    )
                ]
                future = sorted(
                    (
                        memory
                        for memory in query_records
                        if any(tag.startswith("due-date:") for tag in memory.tags)
                    ),
                    key=lambda memory: next(
                        (
                            tag.removeprefix("due-date:")
                            for tag in memory.tags
                            if tag.startswith("due-date:")
                        ),
                        "9999-12-31",
                    ),
                )
                query_records = dated or future[:3] or query_records
                # Profile preferences are useful for generic questions, but
                # must not precede an actionable reminder or cause the model
                # to answer a different remembered topic.
                explicit_records = []
        except (OSError, ConnectionError, ServerUnavailable, MemoryError) as error:
            return semantic, error, False
        result: list[MemoryRecallItem] = []
        used = 0
        full = False
        truncated = False
        seen_ids: set[str] = set()
        seen_slots: set[str] = set()
        seen_text: set[str] = set()
        score_by_id = {header.memory_id: float(header.score) for header in headers}
        relevance_by_id = {
            header.memory_id: float(getattr(header, "relevance", 1.0))
            for header in headers
        }
        query_ids = {memory.memory_id for memory in query_records}
        canonical_records = list(
            {
                memory.memory_id: memory
                for memory in (*query_records, *explicit_records)
            }.values()
        )
        canonical_records.sort(
            key=lambda memory: (
                memory.memory_id in query_ids,
                score_by_id.get(memory.memory_id, 0.0),
                int(getattr(memory, "updated_at", 0) or 0),
                int(getattr(memory, "revision", 0) or 0),
            ),
            reverse=True,
        )
        for memory in canonical_records:
            normalized = " ".join(memory.text.casefold().split())
            slot = memory_slot(memory.tags, memory.text)
            if (
                not normalized
                or memory.memory_id in seen_ids
                or normalized in seen_text
                or bool(slot and slot in seen_slots)
            ):
                continue
            relevance = relevance_by_id.get(memory.memory_id, 1.0)
            if query.strip() and relevance < _recall_min_relevance():
                # B2: a vector-only hit below the cosine floor is not recalled.
                # Keyword hits carry relevance 1.0 and keep passing, and the
                # explicit profile records never enter this branch.
                continue
            if full:
                # The window closed earlier in this loop and this record still
                # qualifies, so the read was cut.  Only a qualifying record
                # counts: duplicates and sub-floor hits are not missing content.
                truncated = True
                break
            rendered = _render_memory_text(memory)
            text = _clip(rendered, max_chars - used)
            if not text:
                # The record qualifies but the remaining budget cannot hold one
                # more character of it: the window was cut by `max_chars`.
                truncated = True
                continue
            result.append(
                MemoryRecallItem(
                    memory_id=memory.memory_id,
                    kind=memory.kind,
                    text=text,
                    text_truncated=text != rendered,
                    revision=memory.revision,
                    source=memory.source,
                    score=score_by_id.get(memory.memory_id, 0.0),
                    relevance=relevance,
                    slot=slot,
                    tags=tuple(getattr(memory, "tags", ()) or ()),
                    updated_at=int(getattr(memory, "updated_at", 0) or 0),
                )
            )
            seen_ids.add(memory.memory_id)
            seen_text.add(normalized)
            if slot:
                seen_slots.add(slot)
            used += len(text.encode("utf-8"))
            if used >= max_chars or len(result) >= top_k:
                full = True
        for item in semantic:
            semantic_text = item.text
            if reminder_query and not (
                "temporal:reminder" in item.text
                or "提醒事项" in semantic_text
                or "due-date:" in item.text
            ):
                continue
            if not _usable_recall_text(item.text):
                continue
            if query.strip() and item.relevance < _recall_min_relevance():
                continue
            normalized = " ".join(semantic_text.casefold().split())
            if (
                not normalized
                or item.memory_id in seen_ids
                or normalized in seen_text
                or bool(item.slot and item.slot in seen_slots)
            ):
                continue
            if full:
                truncated = True
                break
            clipped = _clip(item.text, max_chars - used)
            if not clipped:
                truncated = True
                break
            result.append(
                MemoryRecallItem(
                    memory_id=item.memory_id,
                    kind=item.kind,
                    text=clipped,
                    text_truncated=item.text_truncated or clipped != item.text,
                    revision=item.revision,
                    source=item.source,
                    score=item.score,
                    relevance=item.relevance,
                    slot=item.slot,
                    provider=item.provider,
                    tags=item.tags,
                    updated_at=item.updated_at,
                )
            )
            seen_ids.add(item.memory_id)
            seen_text.add(normalized)
            if item.slot:
                seen_slots.add(item.slot)
            used += len(clipped.encode("utf-8"))
            if used >= max_chars or len(result) >= top_k:
                full = True
        return tuple(result), None, truncated

    async def _materialize_historical_reminders(
        self, client: AsyncMemoryClient
    ) -> None:
        """Backfill reminders written before absolute-time normalization shipped."""

        try:
            if not hasattr(client, "sessions"):
                return
            context = await client.sessions.get_recent_context(
                exclude_session_id=None,
                turn_limit=50,
                summary_limit=20,
            )
            # ``recent_sessions`` (the wire default) groups paired messages
            # per session; older daemons answered with a flat ``messages``
            # list.  Accept both so the backfill keeps working either way.
            history_messages = [
                item for item in context.get("messages", ()) if isinstance(item, dict)
            ]
            for group in context.get("sessions", ()):
                if not isinstance(group, dict):
                    continue
                for item in group.get("messages", ()):
                    if isinstance(item, dict):
                        history_messages.append(item)
            for item in history_messages:
                if not isinstance(item, dict) or item.get("event_type") != "user_message":
                    continue
                payload = item.get("payload")
                if not isinstance(payload, dict):
                    continue
                text = str(payload.get("text") or "").strip()
                created_at = int(item.get("created_at") or 0)
                event_id = str(item.get("event_id") or "")
                if not text or created_at <= 0 or not event_id:
                    continue
                reminder = extract_reminder(
                    text, anchor=from_epoch_ms(created_at)
                )
                if reminder is None:
                    continue
                await client.long_term.put(
                    kind=MemoryKind.EPISODIC,
                    text=reminder.canonical_text,
                    tags=(
                        "personal-agent",
                        "temporal:reminder",
                        f"due-date:{reminder.due_at.date().isoformat()}",
                    ),
                    evidence=(EvidenceRef("session_event", event_id),),
                    sync_policy=SyncPolicy.LOCAL_ONLY,
                    sensitivity="private",
                    expires_at=int(
                        (reminder.due_at.timestamp() + 7 * 24 * 60 * 60) * 1000
                    ),
                    idempotency_key=f"temporal-backfill:{event_id}",
                )
        except (OSError, ConnectionError, ServerUnavailable, MemoryError, ValueError):
            return

    async def recent_context(
        self,
        session_id: str,
        *,
        session_limit: int = 5,
        max_chars: int = 6000,
    ) -> tuple[str, ...]:
        """Return one-line status metadata for other Runtime sessions.

        Only distilled metadata crosses into a new session: recent-ness is a
        property of the status line, not an admission criterion for message
        bodies.  Conversational content stays behind ``session_search`` and the
        query-keyed ``recall`` path, so a session without any completed turn
        still shows up as ``进行中`` instead of being invisible.
        """

        client = await self._client_or_none()
        if client is None:
            return ()
        try:
            context = await client.sessions.get_recent_context(
                exclude_session_id=session_id,
                selection_mode="session_status",
                session_limit=session_limit,
            )
        except (OSError, ConnectionError, ServerUnavailable, MemoryError):
            return ()

        result: list[str] = []
        used = 0
        current = now_local()
        header = current_time_context(current)
        result.append(header)
        used += len(header.encode("utf-8"))
        # Older daemons do not know ``session_status`` and answer with the
        # legacy shape; degrade to an empty status list instead of failing.
        for item in context.get("sessions", ()):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            if item.get("in_progress"):
                state = "进行中"
            elif item.get("interrupted"):
                state = "已中断"
            else:
                state = "空闲"
            turns = int(item.get("completed_turn_count") or 0)
            activity = _relative_activity(int(item.get("last_active_at") or 0), current)
            line = _clip(
                f"[session-status id={item.get('session_id', '')} {activity}] "
                f"{title} · {turns}轮完成 · {state}",
                max_chars - used,
            )
            if not line:
                break
            result.append(line)
            used += len(line.encode("utf-8"))
            if used >= max_chars:
                break
        return tuple(result)

    async def upsert_explicit(
        self,
        *,
        content: str,
        target: str,
        run_id: str,
        session_id: str,
        slot: str | None = None,
    ) -> dict[str, object]:
        """Insert or revise one explicit memory and converge a stable slot."""

        client = await self._client_or_none()
        normalized_content = normalize_relative_dates(content.strip())
        if client is None:
            # ``_client_or_none`` already kept the connect-time verdict; dropping
            # it here is what made a refused write look like an outage
            # (K3 2026-09-22: the add fallback reported backend_unavailable
            # alone, with no reason attached).
            return _failure_payload(
                self._connect_error
                or _unavailable_error("memory service is unreachable")
            )
        if not normalized_content:
            return {"success": False, "code": "invalid_content"}
        if target not in {"user", "memory"}:
            return {"success": False, "code": "invalid_target"}
        failure = validate_memory_text(target, normalized_content)
        if failure is not None:
            return {
                "success": False,
                "code": failure,
                "limit": content_limit(target),
                "length": len(normalized_content),
            }
        if slot is not None:
            # MEM-L3-02: an explicitly requested slot must exist in the L3 field
            # dictionary (or be one of its aliases). A model that invents
            # ``profile.explanation_style`` is refused instead of writing a
            # self-made slot; the same text is still storable without a slot.
            canonical_slot = resolve_slot(slot) or ""
            if not canonical_slot:
                return {
                    "success": False,
                    "code": "invalid_slot",
                    "error": f"unknown slot: {slot}",
                    "known_slots": list(known_slots()),
                }
            # MEM-L3-03: naming a slot asserts that the text *is* that field's
            # value, so the dictionary's value declarations are enforced here
            # and a registered alias is stored in its canonical form. Prose that
            # has no field simply omits the slot (the invalid_value payload says
            # so) instead of being rewritten behind the caller's back.
            canonical_value, value_failure = resolve_field_value(
                canonical_slot, normalized_content
            )
            if value_failure is not None:
                return field_value_failure(canonical_slot, value_failure)
            normalized_content = canonical_value
        else:
            canonical_slot = (preference_slot(normalized_content) or "").strip()
        kind = MemoryKind.PROFILE if target == "user" else MemoryKind.FACT
        sensitivity = (
            "secret"
            if _contains_credential(normalized_content)
            else "sensitive"
            if target == "user" or _contains_pii(normalized_content)
            else "private"
        )
        content_digest = hashlib.sha256(
            normalized_content.encode("utf-8")
        ).hexdigest()
        expected_kinds = (
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

        async def reconcile() -> dict[str, object]:
            method = getattr(client.long_term, "reconcile_slots", None)
            if not canonical_slot or method is None:
                return {}
            result = await method(slots=[canonical_slot], dry_run=False)
            return result if isinstance(result, dict) else {}

        try:
            headers = await client.long_term.search_headers("", top_k=100)
            records = await client.long_term.get_many(
                [header.memory_id for header in headers]
            )
            normalized_key = " ".join(normalized_content.casefold().split())
            candidates = [
                record
                for record in records
                if record.kind in expected_kinds
                and (
                    " ".join(record.text.casefold().split()) == normalized_key
                    or bool(
                        canonical_slot
                        and memory_slot(record.tags, record.text) == canonical_slot
                    )
                )
            ]
            if candidates:
                target_record = max(
                    candidates,
                    key=lambda item: (
                        int(getattr(item, "updated_at", 0) or 0),
                        int(getattr(item, "revision", 0) or 0),
                        str(getattr(item, "memory_id", "")),
                    ),
                )
                if " ".join(target_record.text.casefold().split()) == normalized_key:
                    convergence = await reconcile()
                    return {
                        "success": True,
                        "code": "unchanged",
                        "target": target,
                        "memory_id": target_record.memory_id,
                        "revision": target_record.revision,
                        **({"slot": canonical_slot} if canonical_slot else {}),
                        "duplicates_reconciled": int(
                            convergence.get("superseded_memories") or 0
                        ),
                    }
                tags = list(
                    dict.fromkeys(
                        (
                            *target_record.tags,
                            "personal-agent",
                            target,
                            *((f"slot:{canonical_slot}",) if canonical_slot else ()),
                        )
                    )
                )
                receipt = await client.long_term.revise(
                    target_record.memory_id,
                    text=_clip(normalized_content, 12000),
                    expected_revision=target_record.revision,
                    summary=target_record.summary,
                    tags=tags,
                    evidence=session_evidence(session_id),
                    idempotency_key=_key(
                        run_id,
                        f"explicit-revise:{target_record.memory_id}:{content_digest}",
                    ),
                )
                convergence = await reconcile()
                return {
                    "success": True,
                    "code": "slot_revised" if canonical_slot else "revised",
                    "target": target,
                    "memory_id": receipt.memory_id,
                    "revision": receipt.revision,
                    **({"slot": canonical_slot} if canonical_slot else {}),
                    "duplicates_reconciled": int(
                        convergence.get("superseded_memories") or 0
                    ),
                }
            receipt = await client.long_term.put(
                kind=kind,
                text=_clip(normalized_content, 12000),
                tags=(
                    "personal-agent",
                    target,
                    *((f"slot:{canonical_slot}",) if canonical_slot else ()),
                ),
                evidence=session_evidence(session_id),
                sync_policy=SyncPolicy.LOCAL_ONLY,
                sensitivity=sensitivity,
                idempotency_key=_key(
                    run_id, f"explicit:{target}:{content_digest}"
                ),
            )
            convergence = await reconcile()
            return {
                "success": True,
                "code": "recorded",
                "target": target,
                "memory_id": receipt.memory_id,
                "revision": receipt.revision,
                **({"slot": canonical_slot} if canonical_slot else {}),
                "duplicates_reconciled": int(
                    convergence.get("superseded_memories") or 0
                ),
            }
        except ServerUnavailable:
            return {"success": False, "code": "backend_unavailable"}
        except MemoryError as error:
            # A rejected write is not an unavailable backend: collapsing the two
            # hid a real InvalidArgument behind ``backend_unavailable`` for the
            # whole debugging session. Report what memoryd actually said.
            return {
                "success": False,
                "code": failure_code(error),
                "error": str(error),
            }
        except (OSError, ConnectionError):
            return {"success": False, "code": "backend_unavailable"}

    async def put_explicit(
        self,
        *,
        content: str,
        target: str,
        run_id: str,
        session_id: str,
    ) -> bool:
        result = await self.upsert_explicit(
            content=content,
            target=target,
            run_id=run_id,
            session_id=session_id,
        )
        return result.get("success") is True

    async def record_turn(
        self,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
        user_text: str,
        assistant_text: str,
        extraction_text: str | None = None,
        force_summary: bool = False,
    ) -> bool:
        client = await self._client_or_none()
        if client is None:
            return False
        user_text = _clip(user_text, 64000)
        assistant_text = _clip(assistant_text, 64000)
        try:
            user_event = await client.sessions.append_event(
                session_id=session_id,
                event_type="user_message",
                payload={
                    "role": "user",
                    "text": user_text,
                    "run_id": run_id,
                    "agent_id": agent_id,
                    "status": "completed",
                },
                sync_policy=SyncPolicy.LOCAL_ONLY,
                idempotency_key=_key(run_id, "user-event"),
            )
            assistant_event = await client.sessions.append_event(
                session_id=session_id,
                event_type="assistant_message",
                payload={
                    "role": "assistant",
                    "session_end": force_summary,
                    "text": assistant_text,
                    "run_id": run_id,
                    "agent_id": agent_id,
                    "status": "completed",
                },
                sync_policy=SyncPolicy.LOCAL_ONLY,
                idempotency_key=_key(run_id, "assistant-event"),
            )
            await client.runtime.prepare_checkpoint(
                session_id,
                idempotency_key=_key(run_id, "checkpoint"),
            )
            # Keep original L2 events; only extraction uses the uncovered text.
            if extraction_text is not None:
                user_text = _clip(extraction_text, 64000)
            reminder = extract_reminder(user_text)
            if reminder is not None:
                await client.long_term.put(
                    kind=MemoryKind.EPISODIC,
                    text=reminder.canonical_text,
                    tags=(
                        "personal-agent",
                        "temporal:reminder",
                        f"due-date:{reminder.due_at.date().isoformat()}",
                    ),
                    evidence=session_evidence(session_id),
                    sync_policy=SyncPolicy.LOCAL_ONLY,
                    sensitivity="private",
                    expires_at=int(
                        (reminder.due_at.timestamp() + 7 * 24 * 60 * 60) * 1000
                    ),
                    idempotency_key=_key(run_id, "temporal-reminder"),
                )
            contains_credential = _contains_credential(user_text)
            contains_pii = _contains_pii(user_text)
            contains_profile_name = _contains_profile_name(user_text)
            contains_sensitive_data = contains_credential or contains_pii
            if _dplus_enabled() and not contains_sensitive_data and extraction_text is None:
                try:
                    await client.projection.enqueue_turn(
                        provider="mem0-oss-v2",
                        run_id=run_id,
                        session_id=session_id,
                        user_event_id=str(user_event["event_id"]),
                        assistant_event_id=str(assistant_event["event_id"]),
                        idempotency_key=_key(run_id, "dplus-projection"),
                    )
                except (OSError, ConnectionError, ServerUnavailable, MemoryError):
                    await self.coordinator.ingest_turn(
                        client,
                        session_id=session_id,
                        run_id=run_id,
                        user_text=normalize_relative_dates(user_text),
                        assistant_text=assistant_text,
                    )
            elif not contains_credential and (not contains_pii or contains_profile_name):
                await self.coordinator.ingest_turn(
                    client,
                    session_id=session_id,
                    run_id=run_id,
                    user_text=normalize_relative_dates(user_text),
                    assistant_text=assistant_text,
                    sensitivity="sensitive" if contains_pii else "private",
                )
        except (OSError, ConnectionError, ServerUnavailable, MemoryError, ValueError):
            return False
        return True

    async def record_operation_event(
        self,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
        event_type: str = "tool_result",
        tool: str = "",
        status: str = "completed",
        command: str | None = None,
        files: list[str] | tuple[str, ...] | None = None,
        error: str | None = None,
        operation_id: str | None = None,
    ) -> bool:
        """Write a redacted Runtime operation into the L2 session stream.

        Operation events are intentionally separate from conversational turns;
        memoryd gives them a seven-day TTL and includes them when producing an
        L2 summary. Secrets and oversized values are never persisted.
        """
        client = await self._client_or_none()
        if client is None or event_type not in {"tool_call", "tool_result", "task_started", "task_completed", "task_failed"}:
            return False
        payload: dict[str, object] = {
            "run_id": run_id,
            "agent_id": agent_id,
            "tool": _clip(tool, 256),
            "status": _clip(status, 64),
            "eligible_for_context": True,
        }
        # Commands and error text can contain credentials.  Omit them rather
        # than attempting lossy tokenisation; the audit channel still retains
        # the event type and status without leaking the secret into L2.
        if command and not _contains_credential(command):
            payload["command"] = _clip(command, 1000)
        if files:
            safe_files = [
                _clip(item, 500)
                for item in files[:50]
                if str(item or "").strip() and not _contains_credential(str(item))
            ]
            if safe_files:
                payload["files"] = safe_files
        if error and not _contains_credential(error):
            payload["error"] = _clip(error, 2000)
        operation_identity = operation_id or json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        try:
            await client.sessions.append_event(
                session_id=session_id,
                event_type=event_type,
                payload=payload,
                sync_policy=SyncPolicy.LOCAL_ONLY,
                idempotency_key=_operation_key(run_id, event_type, operation_identity),
            )
            return True
        except (OSError, ConnectionError, ServerUnavailable, MemoryError, ValueError):
            return False

    async def record_interrupted_turn(
        self,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
        user_text: str,
        status: str,
        error: str | None = None,
    ) -> bool:
        """Persist recovery metadata without creating a recallable turn.

        The daemon retains ``task_failed`` events for a short audit window, but
        L1/L2/L3 projections only consume paired user/assistant messages.
        """

        client = await self._client_or_none()
        if client is None:
            return False
        payload = {
            "run_id": run_id,
            "agent_id": agent_id,
            "text": _clip(user_text, 64000),
            "status": _clip(status, 64),
            "recovery_kind": "interrupted_turn",
            "eligible_for_context": False,
        }
        if error:
            payload["error"] = _clip(error, 2000)
        try:
            await client.sessions.append_event(
                session_id=session_id,
                event_type="task_failed",
                payload=payload,
                sync_policy=SyncPolicy.LOCAL_ONLY,
                idempotency_key=_key(run_id, "interrupted-turn"),
            )
            # Keep a Memory-side recovery anchor even when the user-visible
            # turn is intentionally excluded from L1/L2/L3 recall. Runtime
            # owns the process checkpoint; Memory owns this event watermark.
            runtime = getattr(client, "runtime", None)
            if runtime is not None:
                try:
                    await runtime.prepare_checkpoint(
                        session_id,
                        idempotency_key=_key(run_id, "interrupted-checkpoint"),
                    )
                except (OSError, ConnectionError, ServerUnavailable, MemoryError, ValueError):
                    pass
        except (OSError, ConnectionError, ServerUnavailable, MemoryError, ValueError):
            return False
        return True

    async def record_run_started(
        self,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
        user_text: str,
    ) -> bool:
        """Persist a run-start watermark so an in-execution first run is visible.

        ``record_turn`` only writes after a run completes, which leaves a
        session with zero events while its first run is executing.  This audit
        event is the write-through equivalent: memoryd gives ``task_started``
        a seven-day TTL, and L1/L2/L3 projections ignore it because it never
        pairs into a turn.
        """

        client = await self._client_or_none()
        if client is None:
            return False
        payload = {
            "run_id": run_id,
            "agent_id": agent_id,
            "text": _clip(user_text, 2000),
            "status": "started",
            "eligible_for_context": False,
        }
        try:
            await client.sessions.append_event(
                session_id=session_id,
                event_type="task_started",
                payload=payload,
                sync_policy=SyncPolicy.LOCAL_ONLY,
                idempotency_key=_key(run_id, "run-started"),
            )
        except (OSError, ConnectionError, ServerUnavailable, MemoryError, ValueError):
            return False
        return True

_bridges: dict[tuple[str, float, str, str], MemoryBridge] = {}
_bridges_lock = asyncio.Lock()


async def get_memory_bridge(
    socket_path: str | None = None,
    timeout: float = 2.0,
    account_id: str | None = None,
    agent_name: str | None = None,
) -> MemoryBridge:
    resolved_socket = _configured_socket(socket_path)
    resolved_account = account_id or os.environ.get(
        "BIANBU_AGENT_MEMORY_ACCOUNT_ID", "default"
    )
    resolved_agent = agent_name or os.environ.get(
        "BIANBU_AGENT_MEMORY_AGENT_NAME", ""
    )
    key = (resolved_socket, float(timeout), resolved_account, resolved_agent)
    async with _bridges_lock:
        bridge = _bridges.get(key)
        if bridge is None:
            bridge = MemoryBridge(
                resolved_socket,
                timeout,
                account_id=resolved_account,
                agent_name=resolved_agent or None,
            )
            _bridges[key] = bridge
        return bridge


__all__ = ["MemoryBridge", "get_memory_bridge"]
