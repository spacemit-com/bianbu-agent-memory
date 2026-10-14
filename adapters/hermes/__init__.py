"""Hermes external MemoryProvider backed by bianbu-agent-memoryd.

Copy or symlink this module to ``plugins/memory/bianbu/__init__.py`` in the
pinned Hermes source tree after installing the Bianbu Python SDK in the same
virtual environment.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import queue
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
        )

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # Stable-ID mutation stays in the Bianbu SDK instead of adding a second
        # model-facing tool vocabulary to Hermes.
        return []

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
                return ""
        return cached[1]

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
        self._enqueue("turn", active_session, turn, user_content, assistant_content)

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
        self._enqueue(
            "memory_write",
            action,
            target,
            content,
            # v0.21.7 supplies the complete selected entry under the native
            # store lock. old_text can be only a substring, not record identity.
            str(metadata.get("previous_content") or ""),
            str(metadata.get("session_id") or self._session_id),
        )

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
                    f"- [{record.kind.value}; revision={record.revision}; source={record.source}] {safe_text}"
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
    ) -> None:
        success = self._async_runner.run(
            self._bridge.record_turn(
                run_id=f"hermes:{session_id}:{turn}:{_digest(user_content + chr(0) + assistant_content)}",
                session_id=session_id,
                agent_id="hermes",
                user_text=user_content,
                assistant_text=assistant_content,
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
