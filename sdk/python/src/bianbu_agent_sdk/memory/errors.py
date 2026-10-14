from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ErrorContext:
    code: str
    message: str
    request_id: str
    trace_id: str
    retryable: bool = False
    retry_after_ms: int | None = None


class MemoryError(RuntimeError):
    """Base error returned by the local memory service."""

    code = "MemoryError"

    def __init__(self, context: ErrorContext) -> None:
        super().__init__(f"{context.code}: {context.message}")
        self.context = context


class InvalidArgument(MemoryError):
    code = "InvalidArgument"


class Unauthenticated(MemoryError):
    code = "Unauthenticated"


class PermissionDenied(MemoryError):
    code = "PermissionDenied"


class NotFound(MemoryError):
    code = "NotFound"


class Conflict(MemoryError):
    code = "Conflict"


class ResourceExhausted(MemoryError):
    code = "ResourceExhausted"


class DeadlineExceeded(MemoryError):
    code = "DeadlineExceeded"


class ServerUnavailable(MemoryError):
    code = "ServerUnavailable"


class AccountOffline(MemoryError):
    code = "AccountOffline"


class SyncPending(MemoryError):
    code = "SyncPending"


class BackupUnavailable(MemoryError):
    code = "BackupUnavailable"


class ModelUnavailable(MemoryError):
    code = "ModelUnavailable"


class ProtocolMismatch(MemoryError):
    code = "ProtocolMismatch"


class MemoryDisabled(MemoryError):
    code = "MemoryDisabled"


_ERROR_TYPES: dict[str, type[MemoryError]] = {
    error_type.code: error_type
    for error_type in (
        InvalidArgument,
        Unauthenticated,
        PermissionDenied,
        NotFound,
        Conflict,
        ResourceExhausted,
        DeadlineExceeded,
        ServerUnavailable,
        AccountOffline,
        SyncPending,
        BackupUnavailable,
        ModelUnavailable,
        ProtocolMismatch,
        MemoryDisabled,
    )
}


def from_response(response: dict[str, Any]) -> MemoryError:
    body = response.get("error") or {}
    context = ErrorContext(
        code=str(body.get("code", "ServerUnavailable")),
        message=str(body.get("message", "memory service returned an error")),
        request_id=str(response.get("request_id", "unknown")),
        trace_id=str(response.get("trace_id", "unknown")),
        retryable=bool(body.get("retryable", False)),
        retry_after_ms=body.get("retry_after_ms"),
    )
    return _ERROR_TYPES.get(context.code, MemoryError)(context)


def failure_code(error: MemoryError) -> str:
    """Return the lowercase code the memory Tools report to the model."""

    code = str(getattr(error, "code", "") or "memory_error")
    return "".join(
        f"_{character.lower()}" if character.isupper() else character
        for character in code
    ).lstrip("_")


__all__ = [
    "ErrorContext",
    "MemoryError",
    "InvalidArgument",
    "Unauthenticated",
    "PermissionDenied",
    "NotFound",
    "Conflict",
    "ResourceExhausted",
    "DeadlineExceeded",
    "ServerUnavailable",
    "AccountOffline",
    "SyncPending",
    "BackupUnavailable",
    "ModelUnavailable",
    "ProtocolMismatch",
    "MemoryDisabled",
    "failure_code",
    "from_response",
]
