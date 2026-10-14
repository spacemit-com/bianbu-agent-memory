from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, Sequence

from .ontology import is_question_like, memory_slot, preference_slot, slot_residual
from .temporal import extract_reminder
from .types import EvidenceRef, LongTermMemory, MemoryKind, SyncPolicy


class MutationEvent(str, Enum):
    ADD = "ADD"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    NONE = "NONE"


@dataclass(frozen=True)
class MemoryCandidate:
    text: str
    kind: MemoryKind = MemoryKind.FACT
    confidence: float = 0.7


@dataclass(frozen=True)
class MemoryMutation:
    event: MutationEvent
    text: str
    kind: MemoryKind = MemoryKind.FACT
    memory_id: str | None = None
    old_memory: str | None = None
    confidence: float = 0.7


@dataclass(frozen=True)
class ApplyResult:
    added: int = 0
    updated: int = 0
    delete_proposals: int = 0
    unchanged: int = 0


class Mem0Planner(Protocol):
    async def extract(
        self, messages: Sequence[dict[str, str]]
    ) -> tuple[MemoryCandidate, ...]: ...

    async def merge(
        self,
        candidates: Sequence[MemoryCandidate],
        existing: Sequence[LongTermMemory],
    ) -> tuple[MemoryMutation, ...]: ...


_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)
_PREFERENCE_HINTS = (
    "喜欢",
    "偏好",
    "常用",
    "习惯",
    "默认",
    "以后",
    "请记住",
    "remember",
    "i prefer",
    "i like",
    "i usually",
)
_EXPLICIT_MEMORY_HINTS = (
    "请记住",
    "记住，",
    "记住:",
    "记住：",
    "我喜欢",
    "我偏好",
    "我习惯",
    "我常用",
    "我的偏好",
    "我的习惯",
    "默认使用",
    "默认用",
    "以后请",
    "我叫",
    "我是",
    "请叫我",
    "叫我",
    "remember",
    "i prefer",
    "i like",
    "i usually",
)
_INTERNAL_PROMPT_PREFIXES = (
    "generate a concise conversation title.",
)
_GREETING = re.compile(r"^(?:你好|您好|嗨|hello|hi)\s*[，,！!。.:：-]*\s*", re.IGNORECASE)
_REMEMBER = re.compile(r"^(?:(?:请)?记住|remember)\s*[，,:：]?\s*", re.IGNORECASE)
_PROFILE = re.compile(
    r"(?:^|[\s，,。；;！!])(?P<profile>(?:我叫|我是|请叫我|可以叫我|叫我)"
    r"[^\s，,。；;！？!?]{1,32})"
)
_DEVELOPMENT_HINTS = (
    "python",
    "rust",
    "typescript",
    "javascript",
    "uv",
    "pip",
    "poetry",
    "npm",
    "pnpm",
    "yarn",
    "依赖",
    "模板",
    "编程",
    "开发",
    "项目",
    "包管理",
    "版本",
    "代码风格",
    "缩进",
    "格式化",
    "静态检查",
    "测试框架",
    "常用命令",
    "脚手架",
    "编辑器",
)


# A clause that talks *to* the assistant instead of describing the user: the
# task half of "我是百岁太奶，帮我查询一下深圳天气".  Only these grammatical
# imperatives are dropped from a name declaration's residual; a communication
# style that merely looks similar ("你得用简洁清晰易懂的话来回答我的问题")
# stays, because dropping it is the bug this residual exists to fix.
_TASK_REQUEST_LEADS = (
    "帮我",
    "请帮",
    "请查询",
    "请查一下",
    "请查",
    "请搜",
    "请看",
    "请找",
    "请给我",
    "请告诉",
    "请列出",
    "请总结",
    "请翻译",
    "请计算",
    "请推荐",
    "麻烦",
    "给我",
    "你能",
    "你可以",
    "能不能",
    "你可否",
    "可否",
    "查一下",
    "查询一下",
    "查查",
    "搜一下",
    "找一下",
    "看一下",
    "看看",
    "告诉我",
)


def _json_value(value: str | dict[str, Any] | list[Any]) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    match = _FENCE.match(text)
    if match:
        text = match.group(1)
    return json.loads(text)


def _kind(value: object, text: str) -> MemoryKind:
    try:
        return MemoryKind(str(value))
    except ValueError:
        lowered = text.casefold()
        if any(hint in lowered for hint in _DEVELOPMENT_HINTS):
            return MemoryKind.DECISION
        if any(hint in lowered for hint in _PREFERENCE_HINTS):
            return MemoryKind.PREFERENCE
        return MemoryKind.FACT


def infer_memory_kind(text: str, value: object = None) -> MemoryKind:
    """Classify a provider-produced memory into the canonical SDK kind set."""

    return _kind(value, text)


def parse_candidates(
    value: str | dict[str, Any] | list[Any],
) -> tuple[MemoryCandidate, ...]:
    payload = _json_value(value)
    items = payload.get("facts", []) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ValueError("Mem0 extraction response must contain a facts list")
    candidates: list[MemoryCandidate] = []
    seen: set[str] = set()
    for item in items[:32]:
        if isinstance(item, str):
            text, raw_kind, confidence = item.strip(), None, 0.7
        elif isinstance(item, dict):
            text = str(item.get("text") or item.get("memory") or "").strip()
            raw_kind = item.get("kind")
            confidence = float(item.get("confidence", 0.7))
        else:
            continue
        normalized = " ".join(text.casefold().split())
        if (
            not text
            or len(text.encode("utf-8")) > 12_000
            or normalized in seen
            or is_question_like(text)
        ):
            continue
        seen.add(normalized)
        candidates.append(
            MemoryCandidate(
                text=text,
                kind=_kind(raw_kind, text),
                confidence=max(0.0, min(confidence, 1.0)),
            )
        )
    return tuple(candidates)


def parse_mutations(
    value: str | dict[str, Any] | list[Any],
    *,
    existing: Sequence[LongTermMemory],
) -> tuple[MemoryMutation, ...]:
    payload = _json_value(value)
    items = payload.get("memory", []) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ValueError("Mem0 merge response must contain a memory list")
    known = {item.memory_id: item for item in existing}
    mutations: list[MemoryMutation] = []
    for item in items[:64]:
        if not isinstance(item, dict):
            continue
        try:
            event = MutationEvent(str(item.get("event") or "NONE").upper())
        except ValueError as error:
            raise ValueError("unsupported Mem0 mutation event") from error
        memory_id = str(item.get("id") or item.get("memory_id") or "").strip() or None
        if event is not MutationEvent.ADD and memory_id not in known:
            raise ValueError(f"mutation {event.value} references an unknown memory id")
        current = known.get(memory_id or "")
        text = str(item.get("text") or (current.text if current else "")).strip()
        if event in {MutationEvent.ADD, MutationEvent.UPDATE} and not text:
            raise ValueError(f"mutation {event.value} requires text")
        confidence = float(item.get("confidence", 0.7))
        mutations.append(
            MemoryMutation(
                event=event,
                text=text,
                kind=_kind(item.get("kind"), text) if current is None else current.kind,
                memory_id=memory_id if event is not MutationEvent.ADD else None,
                old_memory=(
                    str(item.get("old_memory") or current.text) if current else None
                ),
                confidence=max(0.0, min(confidence, 1.0)),
            )
        )
    return tuple(mutations)


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def _durable_text(value: str) -> str:
    text = " ".join(value.strip().split())
    for _ in range(3):
        updated = _GREETING.sub("", text, count=1)
        updated = _REMEMBER.sub("", updated, count=1)
        if updated == text:
            break
        text = updated.strip()
    for marker in ("。只需", "。请简短", ". please briefly"):
        head, found, _ = text.casefold().partition(marker.casefold())
        if found:
            text = text[: len(head)].rstrip() + ("。" if marker.startswith("。") else ".")
            break
    return text


def _topic(value: str) -> str:
    lowered = _normalized(_durable_text(value))
    for separator in ("：", ":", "=", "是", "用", "使用", "prefer", "like"):
        head, found, _ = lowered.partition(separator)
        if found and head.strip():
            return head.strip(" ，,。.!！?？")[:80]
    return lowered.strip(" ，,。.!！?？")[:80]


def _profile_fact(value: str) -> str | None:
    match = _PROFILE.search(value)
    return match.group("profile").strip() if match else None


def _durable_kind(text: str) -> MemoryKind:
    """Development habits become decisions; other durable statements are preferences."""

    lowered = text.casefold()
    if any(hint in lowered for hint in _DEVELOPMENT_HINTS):
        return MemoryKind.DECISION
    return MemoryKind.PREFERENCE


def _is_task_request(text: str) -> bool:
    """Return whether a clause asks the assistant to do something for the user.

    Requests and style preferences can be shaped alike, so this deliberately
    matches only explicit imperative openers (``帮我查询深圳天气``) and leaves
    anything else -- ``你得用简洁清晰易懂的话来回答我的问题`` included -- to be
    kept.  Under-filtering is the safe direction: a missed request pollutes one
    record, while a dropped preference silently breaks the feature.
    """

    stripped = text.lstrip(" \t\n，,、；;。！!？?…")
    return stripped.startswith(_TASK_REQUEST_LEADS)


def _profile_residual(text: str) -> str | None:
    """Return the durable statement a name declaration would otherwise drop.

    A name slot is single-valued, so only the declared name is stored.  When the
    sentence that declared it keeps talking, whatever the slot cannot hold has to
    be kept as its own memory instead of being discarded before it is ever
    written: a slot *replace* already preserves such a residual through
    ``ontology.slot_residual``, and adding a slot record has to behave the same.
    Reminders belong to the temporal path (an expiring episodic memory) and a
    question is not a durable statement, so neither is kept here.
    The clause also has to be *about* the user: an imperative that talks to the
    assistant ("帮我查询一下深圳天气") is the user's current task, not a durable
    preference, and storing it is the pollution the K3 2026-09-29 "输出记忆"
    session showed.  ``tests/test_mem0.py`` pins the boundary: a question stores
    nothing at all, a task request adds no memory, while a transient state or a
    style preference is kept on purpose.
    """

    residual = slot_residual(text, "profile.user_name")
    if not residual:
        return None
    if is_question_like(residual) or extract_reminder(residual) is not None:
        return None
    if _is_task_request(residual):
        return None
    return residual


class RuleBasedMem0Planner:
    """Offline fallback that preserves Mem0's extract/merge action contract.

    It intentionally extracts only explicit preference or development-habit
    statements. A configured model planner can broaden extraction without
    changing canonical storage, revision, identity, or Forget semantics.
    """

    async def extract(
        self, messages: Sequence[dict[str, str]]
    ) -> tuple[MemoryCandidate, ...]:
        facts: list[dict[str, object]] = []
        for message in messages:
            if message.get("role") != "user":
                continue
            text = str(message.get("content") or "").strip()
            lowered = text.casefold()
            if (
                not text
                or lowered.startswith(_INTERNAL_PROMPT_PREFIXES)
                or is_question_like(text)
                or not any(hint in lowered for hint in _EXPLICIT_MEMORY_HINTS)
            ):
                continue
            durable_text = _durable_text(text)
            if not durable_text:
                continue
            profile = _profile_fact(durable_text)
            facts.append(
                {
                    "text": profile or durable_text,
                    "kind": (
                        MemoryKind.PROFILE.value
                        if profile
                        else _durable_kind(durable_text).value
                    ),
                    "confidence": 0.65,
                }
            )
            if profile:
                residual = _profile_residual(durable_text)
                if residual is not None:
                    facts.append(
                        {
                            "text": residual,
                            "kind": _durable_kind(residual).value,
                            "confidence": 0.65,
                        }
                    )
        return parse_candidates({"facts": facts})

    async def merge(
        self,
        candidates: Sequence[MemoryCandidate],
        existing: Sequence[LongTermMemory],
    ) -> tuple[MemoryMutation, ...]:
        mutations: list[MemoryMutation] = []
        for candidate in candidates:
            exact = next(
                (item for item in existing if _normalized(item.text) == _normalized(candidate.text)),
                None,
            )
            if exact is not None:
                mutations.append(
                    MemoryMutation(
                        MutationEvent.NONE,
                        exact.text,
                        exact.kind,
                        exact.memory_id,
                        exact.text,
                        candidate.confidence,
                    )
                )
                continue
            candidate_slot = preference_slot(candidate.text)
            same_topic = next(
                (
                    item
                    for item in existing
                    if (
                        candidate_slot
                        and memory_slot(item.tags, item.text) == candidate_slot
                    )
                    or (
                        item.kind == candidate.kind
                        and _topic(item.text) == _topic(candidate.text)
                        # A topic coincidence may not revise a record that holds
                        # a formal field this candidate does not claim.  The
                        # topic of "我是从梧州来的" degenerates to "我", which
                        # used to match the name record and replace it with a
                        # sentence that is not a name (K3 2026-09-30).
                        and memory_slot(item.tags, item.text)
                        in (None, candidate_slot)
                    )
                ),
                None,
            )
            if same_topic is not None:
                mutations.append(
                    MemoryMutation(
                        MutationEvent.UPDATE,
                        candidate.text,
                        same_topic.kind,
                        same_topic.memory_id,
                        same_topic.text,
                        candidate.confidence,
                    )
                )
            else:
                mutations.append(
                    MemoryMutation(
                        MutationEvent.ADD,
                        candidate.text,
                        candidate.kind,
                        confidence=candidate.confidence,
                    )
                )
        return tuple(mutations)


class OpenAICompatibleMem0Planner:
    """Two-stage Mem0-style planner using an existing OpenAI-compatible Gateway."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    @classmethod
    def from_environment(cls) -> "OpenAICompatibleMem0Planner | None":
        base_url = os.environ.get("BIANBU_MEMORY_LLM_BASE_URL", "").strip()
        model = os.environ.get("BIANBU_MEMORY_LLM_MODEL", "").strip()
        if not base_url or not model:
            return None
        return cls(
            base_url=base_url,
            model=model,
            api_key=os.environ.get("BIANBU_MEMORY_LLM_API_KEY", ""),
            timeout=float(os.environ.get("BIANBU_MEMORY_LLM_TIMEOUT", "15")),
        )

    async def extract(
        self, messages: Sequence[dict[str, str]]
    ) -> tuple[MemoryCandidate, ...]:
        prompt = (
            "Extract only durable user preferences, development habits, stable facts, and "
            "explicit remember requests. Ignore secrets and transient task details. Return "
            "JSON only: {\"facts\":[{\"text\":str,\"kind\":\"preference|decision|fact\","
            "\"confidence\":0..1}]}\nConversation:\n"
            + json.dumps(list(messages), ensure_ascii=False)
        )
        return parse_candidates(await self._complete(prompt))

    async def merge(
        self,
        candidates: Sequence[MemoryCandidate],
        existing: Sequence[LongTermMemory],
    ) -> tuple[MemoryMutation, ...]:
        prompt = (
            "Apply Mem0 memory-manager semantics. Compare facts with existing memories and "
            "return ADD, UPDATE, DELETE, or NONE. UPDATE/DELETE/NONE must reuse an existing "
            "id. DELETE is only a proposal and will require user confirmation. JSON only: "
            "{\"memory\":[{\"id\":str,\"text\":str,\"event\":str,"
            "\"old_memory\":str,\"kind\":str,\"confidence\":0..1}]}\nExisting:\n"
            + json.dumps(
                [
                    {"id": item.memory_id, "text": item.text, "kind": item.kind.value}
                    for item in existing
                ],
                ensure_ascii=False,
            )
            + "\nFacts:\n"
            + json.dumps(
                [
                    {
                        "text": item.text,
                        "kind": item.kind.value,
                        "confidence": item.confidence,
                    }
                    for item in candidates
                ],
                ensure_ascii=False,
            )
        )
        return parse_mutations(await self._complete(prompt), existing=existing)

    async def _complete(self, prompt: str) -> str:
        return await asyncio.to_thread(self._complete_blocking, prompt)

    def _complete_blocking(self, prompt: str) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body, headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.load(response)
        return str(payload["choices"][0]["message"]["content"])


class Mem0Coordinator:
    """Adapt Mem0's algorithm while leaving identity and canonical state in memoryd."""

    def __init__(self, planner: Mem0Planner | None = None) -> None:
        self.planner = planner or OpenAICompatibleMem0Planner.from_environment() or RuleBasedMem0Planner()

    async def ingest_turn(
        self,
        client: Any,
        *,
        session_id: str,
        run_id: str,
        user_text: str,
        assistant_text: str,
        sensitivity: str = "private",
    ) -> ApplyResult:
        candidates = await self.planner.extract(
            (
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": assistant_text},
            )
        )
        if not candidates:
            return ApplyResult()
        existing_by_id: dict[str, LongTermMemory] = {}
        candidate_slots: set[str] = set()
        for candidate in candidates:
            slot = preference_slot(candidate.text)
            if slot:
                candidate_slots.add(slot)
            headers = await client.long_term.search_headers(
                "" if slot else _topic(candidate.text),
                top_k=100 if slot else 5,
            )
            records = await client.long_term.get_many(
                [header.memory_id for header in headers]
            )
            if slot:
                records = [
                    item
                    for item in records
                    if memory_slot(item.tags, item.text) == slot
                ]
            existing_by_id.update({item.memory_id: item for item in records})
        existing = tuple(existing_by_id.values())
        mutations = await self.planner.merge(candidates, existing)
        counts = {event: 0 for event in MutationEvent}
        evidence = (EvidenceRef("session_turn", f"{session_id}:{run_id}"),)
        for index, mutation in enumerate(mutations):
            digest = hashlib.sha256(
                f"{run_id}:{index}:{mutation.event.value}:{mutation.text}".encode("utf-8")
            ).hexdigest()
            if mutation.event is MutationEvent.ADD:
                slot = preference_slot(mutation.text)
                await client.long_term.put(
                    kind=mutation.kind,
                    text=mutation.text,
                    tags=(
                        "mem0-derived",
                        *((f"slot:{slot}",) if slot else ()),
                    ),
                    evidence=evidence,
                    sync_policy=SyncPolicy.LOCAL_ONLY,
                    sensitivity=sensitivity,
                    confidence=mutation.confidence,
                    idempotency_key=f"mem0:{digest}:add",
                )
            elif mutation.event is MutationEvent.UPDATE:
                current = existing_by_id[mutation.memory_id or ""]
                slot = preference_slot(mutation.text)
                residual = slot_residual(current.text, memory_slot(current.tags, current.text))
                if residual is not None and not _is_task_request(residual):
                    # The revise below keeps only the slot value.  Whatever else the
                    # record said is a durable statement in its own right ("回答要
                    # 通俗易懂"), so it is written back as a plain memory instead of
                    # disappearing with the slot it happened to be stored in.
                    await client.long_term.put(
                        kind=MemoryKind.PREFERENCE,
                        text=residual,
                        tags=("mem0-derived", "slot-residual"),
                        evidence=evidence,
                        sync_policy=SyncPolicy.LOCAL_ONLY,
                        sensitivity=sensitivity,
                        confidence=mutation.confidence,
                        idempotency_key=f"mem0:{digest}:residual",
                    )
                await client.long_term.revise(
                    current.memory_id,
                    text=mutation.text,
                    expected_revision=current.revision,
                    tags=list(
                        dict.fromkeys(
                            (
                                *current.tags,
                                "mem0-derived",
                                *((f"slot:{slot}",) if slot else ()),
                            )
                        )
                    ),
                    evidence=evidence,
                    idempotency_key=f"mem0:{digest}:update",
                )
            elif mutation.event is MutationEvent.DELETE:
                await client.sessions.append_event(
                    session_id=session_id,
                    event_type="memory_delete_proposed",
                    payload={
                        "memory_id": mutation.memory_id,
                        "reason": "mem0_contradiction_or_delete_instruction",
                        "requires_confirm_token": True,
                    },
                    sync_policy=SyncPolicy.LOCAL_ONLY,
                    idempotency_key=f"mem0:{digest}:delete-proposal",
                )
            counts[mutation.event] += 1
        reconcile = getattr(client.long_term, "reconcile_slots", None)
        if candidate_slots and reconcile is not None:
            await reconcile(slots=sorted(candidate_slots), dry_run=False)
        return ApplyResult(
            added=counts[MutationEvent.ADD],
            updated=counts[MutationEvent.UPDATE],
            delete_proposals=counts[MutationEvent.DELETE],
            unchanged=counts[MutationEvent.NONE],
        )


__all__ = [
    "ApplyResult",
    "Mem0Coordinator",
    "Mem0Planner",
    "MemoryCandidate",
    "MemoryMutation",
    "MutationEvent",
    "OpenAICompatibleMem0Planner",
    "RuleBasedMem0Planner",
    "infer_memory_kind",
    "parse_candidates",
    "parse_mutations",
]
