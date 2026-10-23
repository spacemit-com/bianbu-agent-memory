"""Hermes external MemoryProvider backed by bianbu-agent-memoryd.

Copy or symlink this module to ``plugins/memory/bianbu/__init__.py`` in the
pinned Hermes source tree after installing the Bianbu Python SDK in the same
virtual environment.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
import logging
import os
import queue
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)


class BianbuMemoryProvider(MemoryProvider):
    """Bounded Hermes lifecycle adapter; the daemon remains the truth source."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self._socket_path = str(
            self._config.get("socket_path")
            or os.environ.get("BIANBU_AGENT_MEMORY_SOCKET")
            or "/run/bianbu/agent-memoryd.sock"
        )
        self._client: Any = None
        self._session_id = ""
        self._write_enabled = False
        self._jobs: queue.Queue[tuple[str, tuple[Any, ...]] | None] = queue.Queue(maxsize=256)
        self._worker: threading.Thread | None = None
        self._cache_lock = threading.Lock()
        self._prefetch_cache: dict[str, tuple[str, str]] = {}
        self._turns: dict[str, int] = {}
        self._instruction_block = ""
        self._pending_deletes: dict[str, tuple[Any, ...]] = {}
        self._turn_notice = ""
        self._author_id = None
        self._explicit_contents: list[str] = []
        self._bridge: Any = None
        self._async_runner: Any = None
        self._accepting = True
        self._startup_error: BaseException | None = None
        self._started = threading.Event()
        self._stopping = threading.Event()

    @property
    def name(self) -> str:
        return "bianbu"

    def is_available(self) -> bool:
        if not Path(self._socket_path).is_socket():
            return False
        try:
            import bianbu_agent_sdk.memory  # noqa: F401
        except ImportError:
            return False
        return True

    def unavailable_reason(self) -> str:
        if not Path(self._socket_path).is_socket():
            return f"Bianbu memory socket is missing: {self._socket_path}"
        return "Install the Bianbu Python SDK in the Hermes interpreter environment."

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        from bianbu_agent_sdk.memory import MemoryClient

        self._client = MemoryClient.connect(self._socket_path)
        self._session_id = session_id
        self._write_enabled = kwargs.get("agent_context", "primary") == "primary"
        self._refresh_instruction_block()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="bianbu-memory-provider",
            daemon=True,
        )
        self._worker.start()
        if not self._started.wait(5) or self._startup_error is not None:
            self.shutdown()
            raise RuntimeError("Bianbu memory worker failed to initialize") from self._startup_error

    def system_prompt_block(self) -> str:
        from bianbu_agent_sdk.memory.temporal import current_time_context
        return self._instruction_block + "\n" + current_time_context() + (
            "\nMemory resolves relative dates at recording time. Use the current local date "
            "for 明天/tomorrow; do not request an absolute date unless the user's meaning is ambiguous."
            "\nFor requests to remember/save (记住/保存), call bianbu_memory_save with target=user for personal "
            "preferences or target=memory for environment facts. Use bianbu_memory_search/update/remove for "
            "canonical memory. When the user explicitly asks to save an environment note, save the full "
            "note with target=memory immediately, preserving the user wording verbatim (do not paraphrase); do not ask them to choose a storage target or reminder "
            "mode. Saving a dated note does not create an alarm. Preserve identifiers supplied by the user. "
            "Only claim success after a successful tool result. Plain text/XML is NOT a tool call. "
            "Do not use the native memory file tool for canonical writes/deletion. Deletion requires the user "
            "to send the exact confirmation message as a new turn; tools cannot confirm deletion. "
            "On a delete request, FIRST call bianbu_memory_remove for every selected record to obtain "
            "its real confirmation_message. This is a read-only preview and needs no prior consent. "
            "Never invent a confirmation phrase or ask for confirmation before calling the preview tool."

        )

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        specs = [
            ("search", "Search canonical long-term memory; returns IDs, text and revisions.",
             {"query": {"type": "string"}}, ["query"]),
            ("save", "Save explicit user-requested long-term memory. Relative dates are resolved automatically. Report saved ONLY if success=true.",
             {"content": {"type": "string"}, "target": {"type": "string", "enum": ["user", "memory"]}}, ["content", "target"]),
            ("update", "Update a searched record by ID and expected revision.",
             {"memory_id": {"type": "string"}, "expected_revision": {"type": "integer"}, "content": {"type": "string"}}, ["memory_id", "expected_revision", "content"]),
            ("remove", "Preview ONE record for deletion. Does not delete. Ask user to send the exact confirmation returned, as a new message.",
             {"memory_id": {"type": "string"}}, ["memory_id"]),
        ]
        return [{"name": "bianbu_memory_" + name, "description": description,
                 "parameters": {"type": "object", "properties": props, "required": required,
                                "additionalProperties": False}}
                for name, description, props, required in specs]

    def _call_worker(self, callback: Any) -> dict[str, Any]:
        ready = threading.Event()
        result: dict[str, Any] = {}
        def run() -> None:
            try:
                result.update(callback())
            except Exception as error:
                from bianbu_agent_sdk.memory.runtime import _failure_payload
                result.update(_failure_payload(error))
            finally:
                ready.set()
        if not self._accepting:
            return {"success": False, "code": "provider_closed"}
        try:
            self._jobs.put_nowait(("call", (run,)))
        except queue.Full:
            return {"success": False, "code": "queue_full"}
        if not ready.wait(15):
            return {"success": False, "code": "outcome_unknown", "message": "Check memory before retrying."}
        return result

    def _run_call(self, callback: Any) -> None:
        callback()

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        if tool_name not in {s["name"] for s in self.get_tool_schemas()}:
            return json.dumps({"success": False, "code": "unknown_tool"})
        if tool_name != "bianbu_memory_search" and not self._write_enabled:
            return json.dumps({"success": False, "code": "read_only"})
        return json.dumps(self._call_worker(lambda: self._tool_operation(tool_name, args)), ensure_ascii=False)

    def _tool_operation(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "bianbu_memory_search":
            result = self._async_runner.run(self._bridge.recall_structured_with_status(
                str(args["query"]), top_k=10, max_chars=10000))
            result["records"] = [{"memory_id": r.memory_id, "text": r.text,
                                  "revision": r.revision, "kind": r.kind.value,
                                  "tags": list(r.tags)} for r in result.pop("items", [])]
            return result
        if name == "bianbu_memory_save":
            result = self._async_runner.run(self._bridge.upsert_explicit(
                content=args["content"], target=args["target"],
                run_id=f"hermes-tool:{self._session_id}:{_digest(args['content'])}", session_id=self._session_id))
        else:
            records = self._client.long_term.get_many([args["memory_id"]])
            if not records:
                return {"success": False, "code": "memory_not_found"}
            record = records[0]
            if not any(t in record.tags for t in ("personal-agent", "hermes-curated", "mem0-derived")):
                return {"success": False, "code": "not_adapter_owned"}
            if name == "bianbu_memory_update":
                from bianbu_agent_sdk.memory.temporal import normalize_relative_dates
                from bianbu_agent_sdk.memory.ontology import validate_memory_text
                target = "user" if "user" in record.tags or record.kind.value in {"profile", "preference"} else "memory"
                content = normalize_relative_dates(args["content"].strip())
                if validate_memory_text(target, content) is not None:
                    return {"success": False, "code": "invalid_content"}
                receipt = self._client.long_term.revise(record.memory_id, text=content,
                    expected_revision=args["expected_revision"],
                    idempotency_key=f"hermes-tool-update:{record.memory_id}:{args['expected_revision']}:{_digest(content)}")
                result = {"success": True, "memory_id": receipt.memory_id, "revision": receipt.revision}
            else:
                if not self._confirmation_token():
                    return {"success": False, "code": "trusted_confirmation_unavailable"}
                challenge = "确认删除 " + secrets.token_hex(12)
                self._pending_deletes = {k: v for k, v in self._pending_deletes.items()
                                         if v[4] > time.monotonic() and v[0] == self._session_id
                                         and v[2] != record.memory_id}
                if len(self._pending_deletes) >= 32:
                    return {"success": False, "code": "too_many_pending_confirmations"}
                self._pending_deletes[challenge] = (self._session_id, self._author_id, record.memory_id,
                                        record.revision, time.monotonic() + 300, challenge)
                return {"success": False, "code": "confirmation_required", "memory_id": record.memory_id,
                        "text": record.text, "revision": record.revision, "confirmation_message": challenge}
        if result.get("success"):
            self._explicit_contents.append(str(args["content"]))
        with self._cache_lock:
            self._prefetch_cache.clear()
        return result

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
        self._explicit_contents = []
        self._author_id = kwargs.get("author_id")
        self._turn_notice = ""
        self._pending_deletes = {k: v for k, v in self._pending_deletes.items()
                                 if v[4] > time.monotonic() and v[0] == self._session_id}
        pending = self._pending_deletes.get(message.strip())
        if pending is None or not self._write_enabled:
            return
        session, author, memory_id, revision, expiry, challenge = pending
        if time.monotonic() > expiry or session != self._session_id:
            self._pending_deletes.pop(challenge, None)
            return
        if kwargs.get("author_is_bot") or author != self._author_id or message.strip() != challenge:
            return
        self._pending_deletes.pop(challenge, None)
        def remove() -> dict[str, Any]:
            records = self._client.long_term.get_many([memory_id])
            if not records or records[0].revision != revision:
                return {"success": False, "code": "confirmation_stale"}
            token = self._confirmation_token()
            if not token:
                return {"success": False, "code": "trusted_confirmation_unavailable"}
            job = self._client.long_term.forget(memory_id, confirm_token=token, reason_code="user_request",
                                               idempotency_key="hermes-confirm:" + _digest(challenge))
            status = self._client.long_term.wait_forget(job.job_id)
            with self._cache_lock:
                self._prefetch_cache.clear()
            hidden = bool(status.get("local_hidden"))
            return {"success": hidden,
                    "code": "removed" if hidden else "forget_pending",
                    "memory_id": memory_id, "job_id": job.job_id}
        self._turn_notice = "Memory deletion result: " + json.dumps(self._call_worker(remove), ensure_ascii=False)

    def _confirmation_token(self) -> str | None:
        # Production provisioning must grant this host access to the same secret
        # configured in memoryd. Never accept tokens supplied by model arguments.
        secret_file = self._config.get("confirmation_secret_file")
        if secret_file:
            from bianbu_agent_sdk.memory.bibit_adapter import _confirmation_token
            try:
                secret = Path(secret_file).read_bytes()
            except OSError:
                return None
            return _confirmation_token(secret, "long_term.forget") if secret else None
        return os.environ.get("BIANBU_MEMORY_DEV_CONFIRM_TOKEN")

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not query or self._client is None:
            return ""
        key = session_id or self._session_id
        fingerprint = _digest(query)
        with self._cache_lock:
            cached = self._prefetch_cache.get(key)
        if cached is None or cached[0] != fingerprint:
            # Hermes prefetch_all asks about the current message, whereas its
            # post-turn queue_prefetch uses the previous one. Cache-only recall
            # therefore misses almost every normal conversation turn.
            ready = threading.Event()
            self._enqueue("recall", key, query, ready)
            ready.wait(2.0)
            with self._cache_lock:
                cached = self._prefetch_cache.get(key)
            if cached is None or cached[0] != fingerprint:
                return self._turn_notice
        return self._turn_notice + "\n" + cached[1]

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if query:
            self._enqueue("prefetch", session_id or self._session_id, query)

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
        turn_author: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self._write_enabled:
            return
        active_session = session_id or self._session_id
        if messages is not None:
            turn = len(messages)
        else:
            turn = self._turns.get(active_session, 0) + 1
            self._turns[active_session] = turn
        self._enqueue("turn", active_session, turn, user_content, assistant_content,
                      tuple(self._explicit_contents))

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self._write_enabled:
            return
        metadata = dict(metadata or {})
        # Wait for the queued canonical write before snapshotting covered content
        # in sync_turn. Failed writes must remain eligible for automatic ingestion.
        def write() -> dict[str, Any]:
            self._run_memory_write(action, target, content,
                str(metadata.get("previous_content") or ""),
                str(metadata.get("session_id") or self._session_id))
            return {"success": True}
        result = self._call_worker(write)
        if action == "add" and result.get("success"):
            self._explicit_contents.append(content)

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        if self._write_enabled and messages:
            pointers = [
                f"hermes-message:sha256:{_digest(str(message.get('content') or ''))}"
                for message in messages[-32:]
            ]
            self._enqueue("pre_compress", self._session_id, pointers)
        return "Bianbu Memory preserves canonical session events and structured summary revisions."

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs: Any,
    ) -> None:
        self._pending_deletes.clear()
        self._turn_notice = ""
        self._session_id = new_session_id
        if reset or rewound:
            self._turns.pop(new_session_id, None)
            with self._cache_lock:
                self._prefetch_cache.pop(new_session_id, None)

    def shutdown(self) -> None:
        self._accepting = False
        self._stopping.set()
        worker = self._worker
        if worker is not None:
            try:
                worker.join(timeout=15.0)
            except RuntimeError:
                pass
            if worker.is_alive():
                # The worker owns both clients; never close them underneath
                # an outstanding SDK operation. It will finish and clean up.
                logger.warning("Bianbu memory worker is still draining at shutdown")
                return
        self._worker = None

    def _enqueue(self, operation: str, *args: Any) -> None:
        if not self._accepting:
            return
        try:
            self._jobs.put_nowait((operation, args))
        except queue.Full:
            # The provider may degrade, but it must not stall the Agent turn.
            logger.warning("Bianbu memory provider queue is full; operation=%s", operation)

    def _worker_loop(self) -> None:
        try:
            from bianbu_agent_sdk.memory.runtime import MemoryBridge
            # new_event_loop also supports Python 3.10 (Runner needs 3.11).
            self._async_runner = _WorkerLoop()
            self._bridge = MemoryBridge(socket_path=self._socket_path)
        except BaseException as error:
            self._startup_error = error
            if self._async_runner is not None:
                self._async_runner.close()
            if self._client is not None:
                self._client.close()
                self._client = None
            self._started.set()
            return
        self._started.set()
        while True:
            try:
                item = self._jobs.get(timeout=0.1)
            except queue.Empty:
                if not self._stopping.is_set():
                    continue
                try:
                    self._async_runner.run(self._bridge.close())
                finally:
                    self._async_runner.close()
                    if self._client is not None:
                        self._client.close()
                        self._client = None
                return
            try:
                operation, args = item
                handler = getattr(self, f"_run_{operation}")
                handler(*args)
            except Exception as error:
                logger.warning(
                    "Bianbu memory provider background operation failed: %s (%s)",
                    item[0] if item else "shutdown",
                    type(error).__name__,
                )
            finally:
                self._jobs.task_done()

    def _run_recall(self, session_id: str, query: str, ready: threading.Event) -> None:
        try:
            self._run_prefetch(session_id, query)
        finally:
            ready.set()

    def _run_prefetch(self, session_id: str, query: str) -> None:
        records = self._async_runner.run(
            self._bridge.recall_structured(query, top_k=5, max_chars=6000)
        )
        if not records:
            block = ""
        else:
            lines = []
            for record in records:
                safe_text = record.text[:1000].replace("</memory-context>", "&lt;/memory-context&gt;")
                lines.append(
                    f"- [id={record.memory_id}; {record.kind.value}; revision={record.revision}; source={record.source}] {safe_text}"
                )
            block = (
                "<memory-context trust=\"untrusted-recalled-content\">\n"
                + "\n".join(lines)
                + "\n</memory-context>"
            )
        with self._cache_lock:
            self._prefetch_cache[session_id] = (_digest(query), block)

    def _run_turn(
        self,
        session_id: str,
        turn: int,
        user_content: str,
        assistant_content: str,
        explicit_contents: tuple[str, ...] = (),
    ) -> None:
        success = self._async_runner.run(
            self._bridge.record_turn(
                run_id=f"hermes:{session_id}:{turn}:{_digest(user_content + chr(0) + assistant_content)}",
                session_id=session_id,
                agent_id="hermes",
                user_text=user_content,
                assistant_text=assistant_content,
                extraction_text=_unhandled_text(user_content, explicit_contents),
            )
        )
        if not success:
            raise RuntimeError("Runtime memory turn ingestion failed")
        with self._cache_lock:
            self._prefetch_cache.clear()

    def _append_text_event(
        self,
        session_id: str,
        turn: int,
        event_type: str,
        content: str,
        role: str,
    ) -> None:
        encoded = content.encode("utf-8")
        clipped = encoded[:64 * 1024].decode("utf-8", "ignore")
        self._client.sessions.append_event(
            session_id=session_id,
            event_type=event_type,
            payload={
                "role": role,
                "text": clipped,
                "content_sha256": hashlib.sha256(encoded).hexdigest(),
                "truncated": len(encoded) > len(clipped.encode("utf-8")),
            },
            idempotency_key=f"hermes:{session_id}:turn:{turn}:{role}:{_digest(content)}",
        )

    def _run_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        old_text: str,
        session_id: str,
    ) -> None:
        if action == "add":
            result = self._async_runner.run(
                self._bridge.upsert_explicit(
                    content=content,
                    target=target,
                    run_id=f"hermes-curated:{target}:{_digest(content)}",
                    session_id=session_id,
                )
            )
            if not result.get("success"):
                raise RuntimeError(f"explicit memory write failed: {result.get('code')}")
            with self._cache_lock:
                self._prefetch_cache.clear()
            return
        if action == "replace" and old_text:
            from bianbu_agent_sdk.memory.temporal import normalize_relative_dates
            from bianbu_agent_sdk.memory.ontology import validate_memory_text
            content = normalize_relative_dates(content.strip())
            headers = self._client.long_term.search_headers(old_text, top_k=10)
            records = self._client.long_term.get_many([header.memory_id for header in headers])
            exact = next((record for record in records if record.text == old_text
                          and any(t in record.tags for t in ("hermes-curated", "personal-agent"))
                          and target in record.tags), None)
            if exact is not None:
                if validate_memory_text(target, content) is not None:
                    raise ValueError("replacement rejected by memory validation")
                self._client.long_term.revise(
                    exact.memory_id,
                    text=content,
                    expected_revision=exact.revision,
                    idempotency_key=(
                        f"hermes:curated:replace:{exact.memory_id}:{exact.revision}:{_digest(content)}"
                    ),
                )
                with self._cache_lock:
                    self._prefetch_cache.clear()
            return
        if action == "remove":
            # A successful Hermes file removal is not an Agent OS confirm token.
            # Preserve an auditable pointer and let trusted UI perform Forget.
            self._client.sessions.append_event(
                session_id=session_id,
                event_type="summary_updated",
                payload={
                    "operation": "curated_memory_remove_requires_confirm",
                    "target": target,
                    "content_sha256": _digest(old_text or content),
                },
                idempotency_key=f"hermes:curated:remove-pending:{target}:{_digest(old_text or content)}",
            )

    def _run_pre_compress(self, session_id: str, pointers: list[str]) -> None:
        context = self._client.sessions.get_context(session_id)
        current = context.get("summary") or {}
        revision = int(current.get("revision", 0))
        self._client.sessions.update_summary(
            session_id,
            patch=_summary_patch(pointers),
            source_event_sequence=int(context.get("last_event_sequence", 0)),
            expected_revision=revision,
            idempotency_key=f"hermes:{session_id}:pre-compress:{revision + 1}",
        )

    def _refresh_instruction_block(self) -> None:
        instructions = self._client.instructions.list(effective_only=True)
        if not instructions:
            self._instruction_block = ""
            return
        lines = [
            f"- [scope={item.scope.value}; priority={item.priority}; revision={item.revision}] {item.text}"
            for item in instructions[:32]
        ]
        self._instruction_block = "# Bianbu Memory Instructions\n" + "\n".join(lines)


def _unhandled_text(user_text: str, explicit_contents: tuple[str, ...]) -> str:
    """Subtract only literal tool-covered clauses; never discard the whole turn.

    Normalize dates before matching, and ignore punctuation/spacing differences.
    Paraphrases cannot prove coverage and deliberately remain extractable.
    """
    from bianbu_agent_sdk.memory.temporal import normalize_relative_dates
    if not explicit_contents:
        return user_text
    text = normalize_relative_dates(user_text)
    def compact(value: str) -> str:
        # Models may expand tomorrow into an equivalent explicit date.
        value = re.sub(r"(\d{4}-\d{2}-\d{2})\s*[（(]\s*\1\s*[）)]", r"\1", value)
        value = re.sub(r"(?:早上|上午|凌晨)?\s*(\d{1,2})\s*点(?:整)?", lambda m: f" {int(m[1])}:00", value)
        value = re.sub(r"(?<!\d)0?(\d{1,2}):00(?!\d)", lambda m: f"{int(m[1])}:00", value)
        # Drop a datetime annotation only when BOTH date and clock agree.
        # A different time/date remains visible and cannot prove coverage.
        value = re.sub(
            r"(\d{4}-\d{2}-\d{2})\s*(\d{1,2}):00\s*[（(]\s*\1\s+\2:00(?:\s+CST)?\s*[）)]",
            r"\1 \2:00", value)
        # Only reorder a strictly delimited event label, never arbitrary words
        # (subject/object order can change the meaning of a saved fact).
        value = re.sub(r"(\d{4}-\d{2}-\d{2}\s*\d{1,2}:00\s*会议)\s*[：:]\s*([A-Za-z][A-Za-z0-9_-]*)\s*(?=[。；;\n]|$)",
                       r"\2，\1", value)
        # A leading event label and the same trailing noun are equivalent.
        value = re.sub(r"^\s*会议\s*[：:]\s*(.*)$", r"\1会议", value)
        value = re.sub(r"(?<!\d)0?(\d{1,2}):00(?!\d)", lambda m: f"{int(m[1])}时", value)
        return "".join(c.casefold() for c in value if c.isalnum())
    covered = [compact(normalize_relative_dates(c)) for c in explicit_contents]
    # Tool-routing instructions are not memory content. Keep them separate from
    # the factual clause so they cannot prevent an otherwise exact coverage match.
    text = re.sub(r"[，,]?\s*(?:必须|请)?调用\s*bianbu_memory_\w+", "。", text)
    # The tool may append metadata: still recognize a complete original clause
    # contained in its payload, without deleting neighboring preference clauses.
    # Hermes frequently joins a saved fact and a preference with “另外/此外”
    # without punctuation. Treat those conjunctions as clause boundaries too.
    text = re.sub(r"\s+(?=(?:另外|此外|同时|以后)\s*)", "。", text)
    parts = re.split(r"([。；;\n])", text)
    for index in range(0, len(parts), 2):
        clause = re.sub(r"^\s*(?:请记住|记住|请保存|保存)\s*[：:,，]?", "", parts[index])
        key = compact(clause)
        if len(key) >= 8 and any(key in item for item in covered):
            parts[index] = ""
    text = "".join(parts)
    for content in explicit_contents:
        content = normalize_relative_dates(content)
        chars = [c for c in content if c.isalnum()]
        if not chars:
            continue
        pattern = r"[\W_]*".join(re.escape(c) for c in chars)
        text = re.sub(pattern, "", text, flags=re.I)
    # Empty imperative wrappers carry no fact; preserve every other clause.
    clauses = re.split(r"[。；;\n]", text)
    wrappers = {"请记住", "记住", "请保存", "保存到长期环境记忆", "保存到长期记忆",
                "这是独立临时测试不是个人信息", "不要创建文件或闹钟", "不要创建闹钟或其他文件"}
    return "。".join(c for c in clauses if re.sub(r"[\W_]", "", c) not in wrappers
                    and re.search(r"\w", c))


def _summary_patch(pointers: list[str]) -> Any:
    from bianbu_agent_sdk.memory import SessionSummaryPatch

    return SessionSummaryPatch(
        current_status="Hermes context compression boundary",
        context_pointers=tuple(pointers),
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _WorkerLoop:
    """One asyncio loop owned exclusively by the provider worker."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()

    def run(self, coroutine: Any) -> Any:
        return self.loop.run_until_complete(coroutine)

    def close(self) -> None:
        self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        self.loop.run_until_complete(self.loop.shutdown_default_executor())
        self.loop.close()


__all__ = ["BianbuMemoryProvider"]

