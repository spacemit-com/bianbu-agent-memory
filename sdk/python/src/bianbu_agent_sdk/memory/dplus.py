from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .client import AsyncMemoryClient, MemoryClient
from .errors import Conflict, MemoryError
from .mem0 import Mem0Coordinator, RuleBasedMem0Planner, infer_memory_kind
from .ontology import is_question_like, memory_slot, preference_slot
from .projection import ProjectionMapping
from .types import EvidenceRef, LongTermMemory, SyncPolicy

PROVIDER_NAME = "mem0-oss-v2"


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _clip(value: object, limit: int = 2_000) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit]


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def scope_key(identity: Any) -> str:
    payload = "\0".join(
        (identity.account_id, identity.agent_id, identity.memory_space_id)
    ).encode("utf-8")
    return "bianbu-" + hashlib.sha256(payload).hexdigest()


def _is_projection_safe(memory: LongTermMemory) -> bool:
    return (
        memory.sensitivity in {"public", "private"}
        and memory.kind.value != "profile"
        and not any(tag.startswith("slot:profile.") for tag in memory.tags)
    )


@dataclass(frozen=True)
class DPlusConfig:
    state_dir: Path
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    embedder_base_url: str
    embedder_model: str
    embedder_api_key: str
    embedding_dims: int = 1024
    collection_name: str = "bianbu_agent_memory_dplus_v2"
    search_threshold: float = 0.25
    convergence_threshold: float = 0.80
    infer_mode: str = "upstream"

    @classmethod
    def from_environment(cls) -> "DPlusConfig":
        state_dir = Path(
            os.environ.get(
                "BIANBU_MEMORY_DPLUS_STATE_DIR",
                str(Path.home() / ".local/state/bianbu-agent-memory-dplus"),
            )
        ).expanduser()
        llm_base = (
            os.environ.get("BIANBU_MEMORY_DPLUS_LLM_BASE_URL")
            or os.environ.get("BIANBU_MEMORY_LLM_BASE_URL")
            or "http://127.0.0.1:18790/v1"
        )
        llm_model = (
            os.environ.get("BIANBU_MEMORY_DPLUS_LLM_MODEL")
            or os.environ.get("BIANBU_MEMORY_LLM_MODEL")
            or "qwen3-k3-local"
        )
        return cls(
            state_dir=state_dir,
            llm_base_url=llm_base.rstrip("/"),
            llm_model=llm_model,
            llm_api_key=os.environ.get("BIANBU_MEMORY_DPLUS_LLM_API_KEY", "local"),
            embedder_base_url=os.environ.get(
                "BIANBU_MEMORY_DPLUS_EMBEDDER_BASE_URL",
                "http://127.0.0.1:18790/v1/embed",
            ).rstrip("/"),
            embedder_model=os.environ.get(
                "BIANBU_MEMORY_DPLUS_EMBEDDER_MODEL", "qwen3-embedding-0.6b-q8_0"
            ),
            embedder_api_key=os.environ.get(
                "BIANBU_MEMORY_DPLUS_EMBEDDER_API_KEY", "local"
            ),
            embedding_dims=_int_env("BIANBU_MEMORY_DPLUS_EMBEDDING_DIMS", 1024),
            collection_name=os.environ.get(
                "BIANBU_MEMORY_DPLUS_COLLECTION", "bianbu_agent_memory_dplus_v2"
            ),
            search_threshold=_float_env(
                "BIANBU_MEMORY_DPLUS_SEARCH_THRESHOLD", 0.25
            ),
            convergence_threshold=_float_env(
                "BIANBU_MEMORY_DPLUS_CONVERGENCE_THRESHOLD", 0.80
            ),
            infer_mode=os.environ.get(
                "BIANBU_MEMORY_DPLUS_INFER_MODE", "upstream"
            ).strip().casefold(),
        )

    def mem0_config(self) -> dict[str, Any]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        return {
            "llm": {
                "provider": "openai",
                "config": {
                    "model": self.llm_model,
                    "openai_base_url": self.llm_base_url,
                    "api_key": self.llm_api_key,
                    "temperature": 0.0,
                    "top_p": 0.1,
                    "max_tokens": 1200,
                    "is_reasoning_model": False,
                },
            },
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": self.embedder_model,
                    "openai_base_url": self.embedder_base_url,
                    "api_key": self.embedder_api_key,
                    "embedding_dims": self.embedding_dims,
                },
            },
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": self.collection_name,
                    "path": str(self.state_dir / "qdrant"),
                    "embedding_model_dims": self.embedding_dims,
                    "on_disk": True,
                },
            },
            "history_db_path": str(self.state_dir / "history.db"),
            "version": "v1.1",
            "custom_instructions": (
                "Only retain durable facts explicitly stated by the user: preferences, "
                "stable personal facts, and repeatable development habits. Ignore greetings, "
                "questions, transient task state, secrets, and claims made only by the assistant. "
                "When the user changes a preference, emit the latest current value as one concise "
                "memory and do not preserve the obsolete value in the new memory text."
            ),
        }


class CircuitOpen(RuntimeError):
    pass


class CircuitBreaker:
    def __init__(self, threshold: int = 3, reset_seconds: float = 30.0) -> None:
        self.threshold = threshold
        self.reset_seconds = reset_seconds
        self.failures = 0
        self.opened_at: float | None = None
        self._lock = threading.Lock()

    def before_call(self) -> None:
        with self._lock:
            if self.opened_at is None:
                return
            if time.monotonic() - self.opened_at >= self.reset_seconds:
                self.opened_at = None
                self.failures = 0
                return
            raise CircuitOpen("Mem0 provider circuit is open")

    def success(self) -> None:
        with self._lock:
            self.failures = 0
            self.opened_at = None

    def failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.failures >= self.threshold:
                self.opened_at = time.monotonic()

    @property
    def state(self) -> str:
        with self._lock:
            return "open" if self.opened_at is not None else "closed"


class UpstreamMem0Provider:
    """Official mem0ai public API adapter; all stored vectors are derived state."""

    def __init__(self, config: DPlusConfig | None = None) -> None:
        self.config = config or DPlusConfig.from_environment()
        os.environ.setdefault("MEM0_TELEMETRY", "false")
        os.environ.setdefault("MEM0_DIR", str(self.config.state_dir / "mem0-home"))
        from mem0 import Memory  # type: ignore[import-not-found]

        config_path = os.environ.get("BIANBU_MEMORY_DPLUS_CONFIG", "").strip()
        if config_path:
            payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
        else:
            payload = self.config.mem0_config()
        self.memory = Memory.from_config(payload)
        self.breaker = CircuitBreaker(
            threshold=_int_env("BIANBU_MEMORY_DPLUS_CIRCUIT_FAILURES", 3),
            reset_seconds=_float_env("BIANBU_MEMORY_DPLUS_CIRCUIT_RESET", 30.0),
        )
        self._lock = threading.RLock()

    def _call(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        self.breaker.before_call()
        try:
            with self._lock:
                result = getattr(self.memory, operation)(*args, **kwargs)
        except Exception:
            self.breaker.failure()
            raise
        self.breaker.success()
        return result

    def add_turn(self, identity: Any, job: dict[str, Any]) -> dict[str, Any]:
        scope = scope_key(identity)
        filters = {"user_id": scope, "projection_job_id": job["job_id"]}
        existing = self._call("get_all", filters=filters, top_k=32).get("results", [])
        if existing:
            return {
                "results": [
                    {
                        "id": item["id"],
                        "memory": item.get("memory", ""),
                        "event": "ADD",
                        "recovered_by": "projection_job_id",
                    }
                    for item in existing
                ]
            }
        metadata = {
            "projection_job_id": job["job_id"],
            "source": "bianbu-agent-memory",
            "session_hash": hashlib.sha256(
                str(job.get("session_id", "")).encode("utf-8")
            ).hexdigest(),
        }
        if self.config.infer_mode == "rules":
            candidates = asyncio.run(
                RuleBasedMem0Planner().extract(
                    (
                        {"role": "user", "content": str(job.get("user_text") or "")},
                        {
                            "role": "assistant",
                            "content": str(job.get("assistant_text") or ""),
                        },
                    )
                )
            )
            results: list[dict[str, Any]] = []
            for candidate in candidates:
                payload = self._call(
                    "add",
                    candidate.text,
                    user_id=scope,
                    metadata={**metadata, "canonical_kind": candidate.kind.value},
                    infer=False,
                )
                results.extend(payload.get("results", ()))
            return {"results": results, "infer_mode": "rules"}
        if self.config.infer_mode != "upstream":
            raise ValueError("BIANBU_MEMORY_DPLUS_INFER_MODE must be upstream or rules")
        payload = self._call(
            "add",
            [
                {"role": "user", "content": job.get("user_text", "")},
                {"role": "assistant", "content": job.get("assistant_text", "")},
            ],
            user_id=scope,
            metadata=metadata,
            infer=True,
        )
        return {"results": self._user_attributed_results(payload.get("results", ())) }

    def _user_attributed_results(
        self, values: Iterable[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for item in values:
            provider_id = str(item.get("id") or "")
            if not provider_id:
                continue
            try:
                stored = self._call("get", provider_id)
            except Exception:
                stored = {}
            attributed = str(
                stored.get("attributed_to")
                or stored.get("role")
                or (stored.get("metadata") or {}).get("attributed_to")
                or ""
            ).casefold()
            if attributed == "assistant":
                try:
                    self.delete(provider_id)
                except Exception:
                    pass
                continue
            results.append(item)
        return results

    def search(self, identity: Any, query: str, top_k: int = 8) -> list[dict[str, Any]]:
        payload = self._call(
            "search",
            query,
            top_k=top_k,
            filters={"user_id": scope_key(identity)},
            threshold=self.config.search_threshold,
        )
        return list(payload.get("results", ()))

    def add_canonical(self, identity: Any, memory: LongTermMemory) -> str:
        payload = self._call(
            "add",
            memory.text,
            user_id=scope_key(identity),
            metadata={
                "canonical_id": memory.memory_id,
                "canonical_revision": memory.revision,
                "canonical_kind": memory.kind.value,
                "source": "bianbu-agent-memory-rebuild",
            },
            infer=False,
        )
        results = payload.get("results", ())
        if not results:
            raise RuntimeError("Mem0 did not return an id for canonical rebuild")
        return str(results[0]["id"])

    def delete(self, provider_memory_id: str) -> None:
        self._call("delete", provider_memory_id)

    def delete_scope(self, identity: Any) -> int:
        deleted = 0
        scope = scope_key(identity)
        for _ in range(100):
            values = self._call(
                "get_all", filters={"user_id": scope}, top_k=100
            ).get("results", ())
            provider_ids = [str(item.get("id") or "") for item in values]
            provider_ids = [item for item in provider_ids if item]
            if not provider_ids:
                return deleted
            for provider_id in provider_ids:
                self.delete(provider_id)
                deleted += 1
        raise RuntimeError("D+ scope deletion did not converge")

    def list_scope(self, identity: Any) -> list[dict[str, Any]]:
        return list(
            self._call(
                "get_all",
                filters={"user_id": scope_key(identity)},
                top_k=100,
            ).get("results", ())
        )


class DPlusProjector:
    def __init__(
        self,
        client: MemoryClient,
        provider: UpstreamMem0Provider,
        *,
        provider_name: str = PROVIDER_NAME,
        worker_id: str | None = None,
    ) -> None:
        self.client = client
        self.provider = provider
        self.provider_name = provider_name
        self.worker_id = worker_id or f"dplus-{uuid.uuid4()}"

    def process_one(self) -> bool:
        job = self.client.projection.claim(
            provider=self.provider_name, worker_id=self.worker_id
        )
        if not job:
            return False
        job_id = str(job["job_id"])
        try:
            provider_result = job.get("provider_result")
            if provider_result is None:
                provider_result = self.provider.add_turn(self.client.identity, job)
                self.client.projection.save_result(
                    job_id,
                    worker_id=self.worker_id,
                    provider_result=provider_result,
                    idempotency_key=f"dplus:{job_id}:provider-result",
                )
            mappings = self._reconcile(job, provider_result)
            self.client.projection.complete(
                job_id,
                worker_id=self.worker_id,
                mappings=mappings,
                idempotency_key=f"dplus:{job_id}:complete",
            )
        except Exception as error:
            self._apply_scheme_a_fallback(job)
            terminal = int(job.get("attempt_count") or 1) >= _int_env(
                "BIANBU_MEMORY_DPLUS_MAX_ATTEMPTS", 8
            )
            try:
                self.client.projection.fail(
                    job_id,
                    worker_id=self.worker_id,
                    error=_clip(f"{type(error).__name__}: {error}"),
                    retry_after_ms=min(
                        300_000, 5_000 * 2 ** min(int(job.get("attempt_count") or 1) - 1, 6)
                    ),
                    terminal=terminal,
                    idempotency_key=f"dplus:{job_id}:fail:{job.get('attempt_count', 1)}",
                )
            except MemoryError:
                pass
        return True

    def _reconcile(
        self, job: dict[str, Any], provider_result: Any
    ) -> tuple[ProjectionMapping, ...]:
        items = provider_result.get("results", ()) if isinstance(provider_result, dict) else ()
        mappings: list[ProjectionMapping] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            provider_id = str(item.get("id") or "").strip()
            text = str(item.get("memory") or item.get("text") or "").strip()
            event = str(item.get("event") or "ADD").upper()
            if not provider_id:
                continue
            known = self.client.projection.resolve(
                provider=self.provider_name, provider_memory_ids=[provider_id]
            )
            if event == "DELETE":
                if known:
                    self.client.sessions.append_event(
                        session_id=str(job["session_id"]),
                        event_type="memory_delete_proposed",
                        payload={
                            "memory_id": known[0]["memory_id"],
                            "reason": "upstream_mem0_delete_proposal",
                            "requires_confirm_token": True,
                        },
                        sync_policy=SyncPolicy.LOCAL_ONLY,
                        idempotency_key=f"dplus:{job['job_id']}:{index}:delete-proposal",
                    )
                continue
            if event == "NONE" or not text:
                continue
            if is_question_like(text):
                try:
                    self.provider.delete(provider_id)
                except Exception:
                    pass
                continue
            mapping = self._upsert_canonical_for_result(
                job=job,
                provider_id=provider_id,
                text=text,
                known=known[0] if known else None,
            )
            mappings.append(mapping)
        return tuple(mappings)

    def _upsert_canonical_for_result(
        self,
        *,
        job: dict[str, Any],
        provider_id: str,
        text: str,
        known: dict[str, Any] | None,
    ) -> ProjectionMapping:
        evidence = (EvidenceRef("projection_job", str(job["job_id"])),)
        incoming_slot = preference_slot(text)
        reconcile = getattr(self.client.long_term, "reconcile_slots", None)
        if incoming_slot and reconcile is not None:
            reconcile(
                slots=[incoming_slot], dry_run=False
            )
        if known:
            records = self.client.long_term.get_many([known["memory_id"]])
            if records:
                record = records[0]
                if _normalized(record.text) != _normalized(text):
                    slot = preference_slot(text)
                    receipt = self.client.long_term.revise(
                        record.memory_id,
                        text=text,
                        expected_revision=record.revision,
                        tags=list(
                            dict.fromkeys(
                                (
                                    *record.tags,
                                    "mem0-dplus",
                                    *((f"slot:{slot}",) if slot else ()),
                                )
                            )
                        ),
                        evidence=evidence,
                        idempotency_key=f"dplus:{provider_id}:revise:{hashlib.sha256(text.encode()).hexdigest()}",
                    )
                    revision = receipt.revision
                else:
                    revision = record.revision
                return ProjectionMapping(provider_id, record.memory_id, revision)

        semantic = self.provider.search(self.client.identity, text, top_k=10)
        semantic_ids = [
            str(item.get("id") or "")
            for item in semantic
            if item.get("id") and str(item.get("id")) != provider_id
        ]
        resolved = self.client.projection.resolve(
            provider=self.provider_name, provider_memory_ids=semantic_ids
        )
        resolution_by_id = {item["provider_memory_id"]: item for item in resolved}
        candidates: list[tuple[float, str, LongTermMemory]] = []
        expected_kind = infer_memory_kind(text)
        expected_slot = preference_slot(text)
        candidate_memory_ids: set[str] = set()
        for item in semantic:
            other_id = str(item.get("id") or "")
            mapping = resolution_by_id.get(other_id)
            if not mapping or mapping.get("state") != "active":
                continue
            score = float(item.get("score") or 0.0)
            records = self.client.long_term.get_many([mapping["memory_id"]])
            same_slot = bool(
                expected_slot
                and records
                and memory_slot(records[0].tags, records[0].text) == expected_slot
            )
            if (
                records
                and records[0].kind == expected_kind
                and (same_slot or score >= self.provider.config.convergence_threshold)
            ):
                candidates.append((score, other_id, records[0]))
                candidate_memory_ids.add(records[0].memory_id)
        if expected_slot:
            slot_headers = self.client.long_term.search_headers(
                "", top_k=100
            )
            slot_records = self.client.long_term.get_many(
                [item.memory_id for item in slot_headers]
            )
            for record in slot_records:
                if (
                    record.memory_id not in candidate_memory_ids
                    and memory_slot(record.tags, record.text) == expected_slot
                ):
                    candidates.append((-1.0, "", record))
                    candidate_memory_ids.add(record.memory_id)
        candidates.sort(key=lambda item: (item[0], item[2].updated_at), reverse=True)

        if candidates:
            target = candidates[0][2]
            target_revision = target.revision
            for score, old_provider_id, record in candidates:
                revision = record.revision
                if _normalized(record.text) != _normalized(text):
                    slot = preference_slot(text)
                    try:
                        receipt = self.client.long_term.revise(
                            record.memory_id,
                            text=text,
                            expected_revision=record.revision,
                            tags=list(
                                dict.fromkeys(
                                    (
                                        *record.tags,
                                        "mem0-dplus",
                                        *((f"slot:{slot}",) if slot else ()),
                                    )
                                )
                            ),
                            evidence=evidence,
                            idempotency_key=(
                                f"dplus:{job['job_id']}:{record.memory_id}:converge:"
                                f"{hashlib.sha256(text.encode()).hexdigest()}"
                            ),
                        )
                        revision = receipt.revision
                    except Conflict:
                        current = self.client.long_term.get_many([record.memory_id])
                        if current:
                            revision = current[0].revision
                if old_provider_id:
                    self.client.projection.upsert_mapping(
                        provider=self.provider_name,
                        mapping=ProjectionMapping(
                            old_provider_id,
                            record.memory_id,
                            revision,
                            state="superseded",
                            last_score=score,
                        ),
                        idempotency_key=f"dplus:{job['job_id']}:{old_provider_id}:supersede",
                    )
                    try:
                        self.provider.delete(old_provider_id)
                    except Exception:
                        pass
                if record.memory_id == target.memory_id:
                    target_revision = revision
            return ProjectionMapping(
                provider_id,
                target.memory_id,
                target_revision,
                last_score=candidates[0][0],
            )

        slot = preference_slot(text)
        receipt = self.client.long_term.put(
            kind=expected_kind,
            text=text,
            tags=("mem0-dplus", *((f"slot:{slot}",) if slot else ())),
            evidence=evidence,
            sync_policy=SyncPolicy.LOCAL_ONLY,
            confidence=0.85,
            idempotency_key=f"dplus:{self.provider_name}:{provider_id}:canonical",
        )
        return ProjectionMapping(provider_id, receipt.memory_id, receipt.revision)

    def _apply_scheme_a_fallback(self, job: dict[str, Any]) -> None:
        async def run() -> None:
            client = await AsyncMemoryClient.connect(
                self.client._transport.socket_path, self.client._transport.timeout
            )
            try:
                await Mem0Coordinator(RuleBasedMem0Planner()).ingest_turn(
                    client,
                    session_id=str(job["session_id"]),
                    run_id=str(job["run_id"]),
                    user_text=str(job.get("user_text") or ""),
                    assistant_text=str(job.get("assistant_text") or ""),
                )
            finally:
                await client.close()

        try:
            asyncio.run(run())
        except Exception:
            pass

    def rebuild(self, limit: int = 100) -> dict[str, int]:
        headers = self.client.long_term.search_headers("", top_k=min(limit, 100))
        records = self.client.long_term.get_many([item.memory_id for item in headers])
        records = [item for item in records if _is_projection_safe(item)]
        projected = 0
        failed = 0
        provider_by_memory: dict[str, str] = {}
        for record in records:
            try:
                existing = self.provider._call(
                    "get_all",
                    filters={
                        "user_id": scope_key(self.client.identity),
                        "canonical_id": record.memory_id,
                    },
                    top_k=8,
                ).get("results", ())
                provider_id = (
                    str(existing[0]["id"])
                    if existing
                    else self.provider.add_canonical(self.client.identity, record)
                )
                self.client.projection.upsert_mapping(
                    provider=self.provider_name,
                    mapping=ProjectionMapping(
                        provider_id, record.memory_id, record.revision
                    ),
                    idempotency_key=f"dplus:rebuild:{provider_id}:{record.revision}",
                )
                provider_by_memory[record.memory_id] = provider_id
                projected += 1
            except Exception:
                failed += 1
        converged = 0
        slots: dict[str, list[LongTermMemory]] = {}
        for record in records:
            if record.kind.value != "preference":
                continue
            slot = memory_slot(record.tags, record.text)
            if slot:
                slots.setdefault(slot, []).append(record)
        for slot, group in slots.items():
            if len(group) < 2:
                continue
            winner = max(group, key=lambda item: (item.updated_at, item.revision))
            for record in group:
                if record.memory_id == winner.memory_id:
                    continue
                provider_id = provider_by_memory.get(record.memory_id)
                if not provider_id:
                    continue
                revision = record.revision
                if _normalized(record.text) != _normalized(winner.text):
                    receipt = self.client.long_term.revise(
                        record.memory_id,
                        text=winner.text,
                        expected_revision=record.revision,
                        tags=list(
                            dict.fromkeys(
                                (
                                    *record.tags,
                                    "mem0-dplus",
                                    "dplus-rebuild-converged",
                                    f"slot:{slot}",
                                )
                            )
                        ),
                        evidence=(EvidenceRef("projection_rebuild", provider_id),),
                        idempotency_key=(
                            f"dplus:rebuild:converge:{record.memory_id}:"
                            f"{hashlib.sha256(winner.text.encode()).hexdigest()}"
                        ),
                    )
                    revision = receipt.revision
                self.client.projection.upsert_mapping(
                    provider=self.provider_name,
                    mapping=ProjectionMapping(
                        provider_id,
                        record.memory_id,
                        revision,
                        state="superseded",
                    ),
                    idempotency_key=f"dplus:rebuild:supersede:{provider_id}:{revision}",
                )
                try:
                    self.provider.delete(provider_id)
                except Exception:
                    pass
                converged += 1
        return {
            "projected": projected,
            "failed": failed,
            "converged": converged,
            "total": len(records),
        }

    def clear_scope(self) -> dict[str, int]:
        return {"deleted": self.provider.delete_scope(self.client.identity)}

    def prune_orphans(self) -> dict[str, int]:
        values = self.provider.list_scope(self.client.identity)
        provider_ids = [str(item.get("id") or "") for item in values]
        provider_ids = [item for item in provider_ids if item]
        resolved = self.client.projection.resolve(
            provider=self.provider_name, provider_memory_ids=provider_ids
        )
        active = {
            str(item.get("provider_memory_id") or "")
            for item in resolved
            if item.get("state") == "active"
        }
        deleted = 0
        for provider_id in provider_ids:
            if provider_id in active:
                continue
            self.provider.delete(provider_id)
            deleted += 1
        return {"examined": len(provider_ids), "deleted": deleted}

    def search_canonical(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        semantic = self.provider.search(
            self.client.identity, query, top_k=max(top_k * 3, 10)
        )
        provider_ids = [str(item.get("id")) for item in semantic if item.get("id")]
        resolved = self.client.projection.resolve(
            provider=self.provider_name, provider_memory_ids=provider_ids
        )
        by_provider = {
            item["provider_memory_id"]: item
            for item in resolved
            if item.get("state") == "active"
        }
        memory_ids = list(
            dict.fromkeys(
                by_provider[item_id]["memory_id"]
                for item_id in provider_ids
                if item_id in by_provider
            )
        )
        records = self.client.long_term.get_many(memory_ids)
        by_memory = {item.memory_id: item for item in records}
        output: list[dict[str, Any]] = []
        seen_text: set[str] = set()
        for semantic_item in semantic:
            provider_id = str(semantic_item.get("id") or "")
            mapping = by_provider.get(provider_id)
            if not mapping:
                continue
            record = by_memory.get(mapping["memory_id"])
            if (
                record is None
                or not _is_projection_safe(record)
                or _normalized(record.text) in seen_text
            ):
                continue
            seen_text.add(_normalized(record.text))
            output.append(
                {
                    "memory_id": record.memory_id,
                    "kind": record.kind.value,
                    "text": record.text,
                    "source": record.source,
                    "revision": record.revision,
                    "updated_at": record.updated_at,
                    "score": float(semantic_item.get("score") or 0.0),
                    "slot": next(
                        (
                            tag.removeprefix("slot:")
                            for tag in record.tags
                            if tag.startswith("slot:")
                        ),
                        preference_slot(record.text),
                    ),
                    "tags": list(record.tags),
                    "provider": self.provider_name,
                }
            )
            if len(output) >= top_k:
                break
        return output


__all__ = [
    "CircuitBreaker",
    "CircuitOpen",
    "DPlusConfig",
    "DPlusProjector",
    "PROVIDER_NAME",
    "UpstreamMem0Provider",
    "scope_key",
    "preference_slot",
]
