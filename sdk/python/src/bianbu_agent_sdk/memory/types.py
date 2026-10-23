from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .temporal import format_epoch_ms_display


class SyncPolicy(str, Enum):
    LOCAL_ONLY = "local_only"
    SUMMARY_ONLY = "summary_only"
    ACCOUNT_AGENT = "account_agent"
    FULL = "full"


class InstructionScope(str, Enum):
    MANAGED = "managed"
    ACCOUNT = "account"
    AGENT = "agent"
    WORKSPACE = "workspace"
    DEVICE = "device"


class MemoryKind(str, Enum):
    PROFILE = "profile"
    PREFERENCE = "preference"
    FACT = "fact"
    PROJECT = "project"
    EPISODIC = "episodic"
    DECISION = "decision"
    REFERENCE = "reference"


@dataclass(frozen=True)
class MemoryIdentity:
    account_id: str
    device_id: str
    agent_id: str
    agent_instance_id: str
    memory_space_id: str
    agent_name: str = ""
    capabilities: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MemoryIdentity":
        return cls(
            account_id=value["account_id"],
            device_id=value["device_id"],
            agent_id=value["agent_id"],
            agent_instance_id=value["agent_instance_id"],
            memory_space_id=value["memory_space_id"],
            agent_name=value.get("agent_name") or value["agent_id"],
            capabilities=frozenset(value.get("capabilities", ())),
        )


@dataclass(frozen=True)
class InstructionMemory:
    instruction_id: str
    scope: InstructionScope
    text: str
    rationale: str | None
    applicability: str | None
    priority: int
    revision: int
    sync_policy: SyncPolicy
    source: str
    sensitivity: str = "private"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InstructionMemory":
        return cls(
            instruction_id=value["instruction_id"],
            scope=InstructionScope(value["scope"]),
            text=value["text"],
            rationale=value.get("rationale"),
            applicability=value.get("applicability"),
            priority=int(value["priority"]),
            revision=int(value["revision"]),
            sync_policy=SyncPolicy(value["sync_policy"]),
            source=value["source"],
            sensitivity=value.get("sensitivity", "private"),
        )


@dataclass(frozen=True)
class EvidenceRef:
    evidence_type: str
    evidence_id: str
    evidence_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_type": self.evidence_type,
            "evidence_id": self.evidence_id,
            "evidence_hash": self.evidence_hash,
        }


@dataclass(frozen=True)
class SessionSummaryPatch:
    current_status: str | None = None
    key_decisions: tuple[str, ...] | None = None
    open_questions: tuple[str, ...] | None = None
    next_steps: tuple[str, ...] | None = None
    files_resources_touched: tuple[str, ...] | None = None
    commands_executed: tuple[str, ...] | None = None
    operations_completed: tuple[str, ...] | None = None
    errors_encountered: tuple[str, ...] | None = None
    user_preferences: tuple[str, ...] | None = None
    context_pointers: tuple[str, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in self.__dict__.items():
            if value is not None:
                result[key] = list(value) if isinstance(value, tuple) else value
        return result


@dataclass(frozen=True)
class MemoryHeader:
    memory_id: str
    kind: MemoryKind
    summary: str | None
    revision: int
    sync_policy: SyncPolicy
    source: str
    sensitivity: str
    updated_at: int
    score: float
    relevance: float = 1.0

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MemoryHeader":
        return cls(
            memory_id=value["memory_id"],
            kind=MemoryKind(value["kind"]),
            summary=value.get("summary"),
            revision=int(value["revision"]),
            sync_policy=SyncPolicy(value["sync_policy"]),
            source=value["source"],
            sensitivity=value["sensitivity"],
            updated_at=int(value["updated_at"]),
            score=float(value.get("score", 0.0)),
            # Older daemons answer without a relevance field; the default keeps
            # such a header recallable instead of silently gating it out.
            relevance=float(value.get("relevance", 1.0)),
        )


@dataclass(frozen=True)
class LongTermMemory:
    memory_id: str
    kind: MemoryKind
    text: str
    summary: str | None
    tags: tuple[str, ...]
    evidence: tuple[EvidenceRef, ...]
    source: str
    confidence: float | None
    sensitivity: str
    revision: int
    sync_policy: SyncPolicy
    created_at: int
    updated_at: int

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "LongTermMemory":
        return cls(
            memory_id=value["memory_id"],
            kind=MemoryKind(value["kind"]),
            text=value["text"],
            summary=value.get("summary"),
            tags=tuple(value.get("tags", ())),
            evidence=tuple(EvidenceRef(**item) for item in value.get("evidence", ())),
            source=value["source"],
            confidence=value.get("confidence"),
            sensitivity=value["sensitivity"],
            revision=int(value["revision"]),
            sync_policy=SyncPolicy(value["sync_policy"]),
            created_at=int(value["created_at"]),
            updated_at=int(value["updated_at"]),
        )


@dataclass(frozen=True)
class MemoryRecallItem:
    """Structured recall result suitable for safe follow-up mutations."""

    memory_id: str
    kind: MemoryKind
    text: str
    revision: int
    source: str
    score: float = 0.0
    relevance: float = 1.0
    slot: str | None = None
    provider: str | None = None
    updated_at: int | None = None
    # Stored tags, verbatim.  A caller that has to answer "which of these did I
    # mark important" cannot recover the mark from text: the reserved tag is
    # the only place it lives, so dropping it here made the mark invisible.
    tags: tuple[str, ...] = ()
    text_truncated: bool = False

    @property
    def target(self) -> str:
        return (
            "user"
            if self.kind in {MemoryKind.PROFILE, MemoryKind.PREFERENCE}
            else "memory"
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "memory_id": self.memory_id,
            "kind": self.kind.value,
            "target": self.target,
            "text": self.text,
            "revision": self.revision,
            "source": self.source,
            "score": self.score,
            "relevance": self.relevance,
            "text_truncated": self.text_truncated,
        }
        if self.slot:
            result["slot"] = self.slot
        if self.provider:
            result["provider"] = self.provider
        if self.updated_at is not None:
            result["updated_at"] = self.updated_at
            if self.updated_at > 0:
                result["updated_at_display"] = format_epoch_ms_display(
                    self.updated_at
                )
        if self.tags:
            result["tags"] = list(self.tags)
        return result


@dataclass(frozen=True)
class WriteReceipt:
    record_id: str
    revision: int
    raw: dict[str, Any]

    @property
    def memory_id(self) -> str:
        return self.record_id


@dataclass(frozen=True)
class ForgetJob:
    job_id: str
    state: str
    receipt: dict[str, Any]


@dataclass(frozen=True)
class SyncStatus:
    account_id: str
    device_id: str
    agent_id: str
    online: bool
    state: str
    last_success_at: int | None
    local_cursor: str | None
    server_cursor: str | None
    pending_upload: int
    pending_download: int
    conflict_count: int
    last_error: str | None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SyncStatus":
        return cls(**value)
