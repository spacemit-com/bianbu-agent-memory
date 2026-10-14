from __future__ import annotations

import asyncio
import json
import socket
import threading
import uuid
from pathlib import Path
from typing import Any

from .errors import ServerUnavailable, from_response
from .types import MemoryIdentity

PROTOCOL = "agent-memory.v1"
DEFAULT_SOCKET = "/run/bianbu/agent-memoryd.sock"
# Default frame budget for the newline-framed UDS protocol. Bulk operations
# (privacy.export_data) against real namespaces exceed the old 256 KiB
# budget, so it matches the daemon's 4 MiB ceiling. The daemon advertises
# its effective limit in the system.hello response and _Transport adopts
# the advertised value, so a newer SDK talking to an older 256 KiB daemon
# keeps that daemon's limit instead of truncating oversized responses.
MAX_FRAME_BYTES = 4 * 1024 * 1024


class _Transport:
    def __init__(
        self,
        socket_path: str | Path,
        timeout: float,
        *,
        account_id: str | None = "default",
        agent_name: str | None = None,
    ) -> None:
        self.socket_path = str(socket_path)
        self.timeout = timeout
        self.account_id = account_id
        self.agent_name = agent_name
        self._socket: socket.socket | None = None
        self._reader: Any = None
        self._lock = threading.Lock()
        self._max_frame_bytes = MAX_FRAME_BYTES
        self.identity: MemoryIdentity | None = None

    def connect(self) -> MemoryIdentity:
        with self._lock:
            self._close_unlocked()
            self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._socket.settimeout(self.timeout)
            try:
                self._socket.connect(self.socket_path)
                self._reader = self._socket.makefile("rb")
                result = self._exchange_unlocked(
                    method="system.hello",
                    params=self._hello_params(),
                    idempotency_key=None,
                    request_id=str(uuid.uuid4()),
                    trace_id=str(uuid.uuid4()),
                )
                self.identity = MemoryIdentity.from_dict(result["identity"])
                self._adopt_frame_limit(result)
                return self.identity
            except Exception:
                self._close_unlocked()
                raise

    def call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        trace_id: str | None = None,
    ) -> Any:
        request_id = str(uuid.uuid4())
        trace_id = trace_id or str(uuid.uuid4())
        with self._lock:
            if self._socket is None:
                self.connect_without_lock()
            try:
                return self._exchange_unlocked(
                    method=method,
                    params=params,
                    idempotency_key=idempotency_key,
                    request_id=request_id,
                    trace_id=trace_id,
                )
            except (BrokenPipeError, ConnectionError, OSError):
                self._close_unlocked()
                self.connect_without_lock()
                return self._exchange_unlocked(
                    method=method,
                    params=params,
                    idempotency_key=idempotency_key,
                    request_id=request_id,
                    trace_id=trace_id,
                )

    def connect_without_lock(self) -> MemoryIdentity:
        self._close_unlocked()
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.settimeout(self.timeout)
        self._socket.connect(self.socket_path)
        self._reader = self._socket.makefile("rb")
        result = self._exchange_unlocked(
            method="system.hello",
            params=self._hello_params(),
            idempotency_key=None,
            request_id=str(uuid.uuid4()),
            trace_id=str(uuid.uuid4()),
        )
        self.identity = MemoryIdentity.from_dict(result["identity"])
        self._adopt_frame_limit(result)
        return self.identity

    def _adopt_frame_limit(self, hello_result: dict[str, Any]) -> None:
        advertised = hello_result.get("max_frame_bytes")
        if isinstance(advertised, int) and not isinstance(advertised, bool) and advertised > 0:
            self._max_frame_bytes = advertised

    def _hello_params(self) -> dict[str, str]:
        params = {"sdk_language": "python", "sdk_version": "0.1.6"}
        if self.account_id is not None:
            params["account_id"] = self.account_id
        if self.agent_name is not None:
            params["agent_name"] = self.agent_name
        return params

    def close(self) -> None:
        with self._lock:
            self._close_unlocked()

    def _close_unlocked(self) -> None:
        if self._reader is not None:
            try:
                self._reader.close()
            except OSError:
                pass
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
        self._reader = None
        self._socket = None

    def _exchange_unlocked(
        self,
        *,
        method: str,
        params: dict[str, Any],
        idempotency_key: str | None,
        request_id: str,
        trace_id: str,
    ) -> Any:
        assert self._socket is not None
        request = {
            "protocol": PROTOCOL,
            "request_id": request_id,
            "trace_id": trace_id,
            "method": method,
            "params": params,
            "idempotency_key": idempotency_key,
            "deadline_ms": int(self.timeout * 1000),
        }
        encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        limit = self._max_frame_bytes
        if len(encoded) + 1 > limit:
            raise ValueError(f"request exceeds the {limit // 1024} KiB protocol limit")
        self._socket.sendall(encoded + b"\n")
        response_bytes = self._reader.readline(limit + 1)
        if not response_bytes or len(response_bytes) > limit or not response_bytes.endswith(b"\n"):
            raise ConnectionError("memory service returned an empty or oversized frame")
        response = json.loads(response_bytes)
        if response.get("request_id") != request_id:
            raise ConnectionError("memory service returned a mismatched request_id")
        if response.get("status") != "ok":
            raise from_response(response)
        return response.get("result")


class MemoryClient:
    """Thread-safe synchronous SDK client bound to a server-provided identity."""

    def __init__(self, transport: _Transport, identity: MemoryIdentity) -> None:
        self._transport = transport
        self.identity = identity
        from .account import AccountAPI
        from .administration import AuditAPI, PrivacyAPI, RuntimeAPI
        from .backup import BackupAPI
        from .instruction import InstructionAPI
        from .long_term import LongTermAPI
        from .projection import ProjectionAPI
        from .session import SessionAPI
        from .sync import SyncAPI

        self.account = AccountAPI(self)
        self.audit = AuditAPI(self)
        self.backup = BackupAPI(self)
        self.instructions = InstructionAPI(self)
        self.long_term = LongTermAPI(self)
        self.projection = ProjectionAPI(self)
        self.privacy = PrivacyAPI(self)
        self.runtime = RuntimeAPI(self)
        self.sessions = SessionAPI(self)
        self.sync = SyncAPI(self)

    @classmethod
    def connect(
        cls,
        socket_path: str | Path = DEFAULT_SOCKET,
        timeout: float = 5.0,
        *,
        account_id: str | None = "default",
        agent_name: str | None = None,
    ) -> "MemoryClient":
        transport = _Transport(
            socket_path, timeout, account_id=account_id, agent_name=agent_name
        )
        identity = transport.connect()
        return cls(transport, identity)

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> "MemoryClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        trace_id: str | None = None,
    ) -> Any:
        return self._transport.call(
            method,
            params,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )

    def health(self) -> dict[str, Any]:
        return self._call("system.health", {})

    def stats(self) -> dict[str, int]:
        return self._call("system.stats", {})


class AsyncMemoryClient:
    """Async facade; blocking UDS work is isolated with asyncio.to_thread."""

    def __init__(self, client: MemoryClient) -> None:
        self._sync = client
        self.identity = client.identity
        from .account import AsyncAccountAPI
        from .administration import AsyncAuditAPI, AsyncPrivacyAPI, AsyncRuntimeAPI
        from .backup import AsyncBackupAPI
        from .instruction import AsyncInstructionAPI
        from .long_term import AsyncLongTermAPI
        from .projection import AsyncProjectionAPI
        from .session import AsyncSessionAPI
        from .sync import AsyncSyncAPI

        self.account = AsyncAccountAPI(self)
        self.audit = AsyncAuditAPI(self)
        self.backup = AsyncBackupAPI(self)
        self.instructions = AsyncInstructionAPI(self)
        self.long_term = AsyncLongTermAPI(self)
        self.projection = AsyncProjectionAPI(self)
        self.privacy = AsyncPrivacyAPI(self)
        self.runtime = AsyncRuntimeAPI(self)
        self.sessions = AsyncSessionAPI(self)
        self.sync = AsyncSyncAPI(self)

    @classmethod
    async def connect(
        cls,
        socket_path: str | Path = DEFAULT_SOCKET,
        timeout: float = 5.0,
        *,
        account_id: str | None = "default",
        agent_name: str | None = None,
    ) -> "AsyncMemoryClient":
        client = await asyncio.to_thread(
            MemoryClient.connect,
            socket_path,
            timeout,
            account_id=account_id,
            agent_name=agent_name,
        )
        return cls(client)

    async def close(self) -> None:
        await asyncio.to_thread(self._sync.close)

    async def __aenter__(self) -> "AsyncMemoryClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def _call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        trace_id: str | None = None,
    ) -> Any:
        return await asyncio.to_thread(
            self._sync._call,
            method,
            params,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )

    async def health(self) -> dict[str, Any]:
        return await self._call("system.health", {})

    async def stats(self) -> dict[str, int]:
        return await self._call("system.stats", {})
