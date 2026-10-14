"""Host context and Worker-attested identity for the MCP surface.

An MCP stdio child inherits almost nothing from the Worker that starts it, so
the tools read their authoritative execution context from two places:

* ``_meta`` on ``tools/call`` carries the run context the Worker attached to
  the request (session, run, agent, account).  It is a scoping hint, never an
  authorization input.
* ``BIANBU_MEMORY_REGISTRATION_SOCKET`` points at the Control-attested broker
  that publishes this process' memoryd registry entry.  memoryd authorizes the
  peer by SO_PEERCRED against that entry, and the entry lives exactly as long
  as the broker connection is held open.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from typing import Any, Mapping

REGISTRATION_SOCKET_ENV = "BIANBU_MEMORY_REGISTRATION_SOCKET"
MEMORY_SOCKET_ENV = "BIANBU_AGENT_MEMORY_SOCKET"
HOST_CONTEXT_KEY = "bianbu/host"
_FIELDS = ("session_id", "run_id", "agent_id", "account_id")


@dataclass(frozen=True, slots=True)
class HostContext:
    """Execution context the Runtime attached to one MCP tool call."""

    session_id: str = ""
    run_id: str = ""
    agent_id: str = ""
    account_id: str = ""

    @property
    def complete(self) -> bool:
        return bool(self.session_id and self.run_id)


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def memory_socket_path() -> str | None:
    """Return the configured memoryd socket, or None for the SDK default."""

    return _text(os.environ.get(MEMORY_SOCKET_ENV)) or None


def memory_timeout(default: float = 2.0) -> float:
    """Return the configured memoryd call budget in seconds."""

    try:
        return float(os.environ.get("BIANBU_MEMORY_TOOL_TIMEOUT", ""))
    except (TypeError, ValueError):
        return default


def _meta_namespace(meta: object) -> object:
    """Read the ``bianbu/host`` namespace out of an MCP ``_meta`` value.

    FastMCP hands the tools a pydantic ``Meta`` model whose extra fields land in
    ``model_extra``, while a raw protocol dict or a mapping-shaped stand-in
    keeps them top level.  A bare ``isinstance(meta, Mapping)`` check matches
    neither the model nor its extra namespace, so both shapes have to be tried:
    reading only the mapping branch silently dropped the whole host context and
    every session-scoped write fell back to an empty session ID.
    """

    if isinstance(meta, Mapping):
        direct = meta.get(HOST_CONTEXT_KEY)
        if isinstance(direct, Mapping):
            return direct
    extra = getattr(meta, "model_extra", None)
    if isinstance(extra, Mapping):
        return extra.get(HOST_CONTEXT_KEY)
    return None


def host_context(meta: object | None) -> HostContext:
    """Extract the Runtime host context from an MCP request ``_meta`` value."""

    payload = _meta_namespace(meta)
    if not isinstance(payload, Mapping):
        return HostContext()
    return HostContext(**{name: _text(payload.get(name)) for name in _FIELDS})


class WorkerRegistration:
    """Own the Worker-attested memoryd identity for one process lifetime.

    The socket path is injected by the Runtime.  Unmanaged development runs
    (no Worker, no registry) skip registration; memoryd then falls back to its
    development identity, exactly like the Hooks.
    """

    def __init__(self, socket_path: str | None = None) -> None:
        self._path = _text(socket_path) or _text(
            os.environ.get(REGISTRATION_SOCKET_ENV)
        )
        self._connection: socket.socket | None = None

    @property
    def active(self) -> bool:
        return self._connection is not None

    def open(self) -> bool:
        if not self._path or self._connection is not None:
            return False
        # The connection owns the attestation, so it must outlive this call:
        # create the socket outside a ``with`` block, which would close it (and
        # drop the registry entry with it) the moment ``open`` returned.
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(5)
        try:
            connection.connect(self._path)
            connection.sendall(b"register\n")
            reply = bytearray()
            while len(reply) < 16 and not reply.endswith(b"\n"):
                chunk = connection.recv(16 - len(reply))
                if not chunk:
                    break
                reply.extend(chunk)
        except OSError as error:
            connection.close()
            raise RuntimeError(
                "Worker memory registration is unavailable"
            ) from error
        if reply != b"ok\n":
            connection.close()
            raise RuntimeError("Worker memory registration was denied")
        connection.settimeout(None)
        self._connection = connection
        return True

    def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
