from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest

from bianbu_agent_sdk.memory.client import MemoryClient

# Every test here drives the real Unix-socket transport, and Windows builds of
# CPython do not expose `socket.AF_UNIX` at all (not even as a stub), so the
# module cannot run there. Skipping keeps a local Windows run honest instead of
# failing three tests that no code change could pass.
pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="the Unix-socket transport needs socket.AF_UNIX",
)

IDENTITY = {
    "account_id": "local:test",
    "device_id": "k3",
    "agent_id": "io.bianbu.test",
    "agent_instance_id": "instance",
    "memory_space_id": "space",
}


def _serve(socket_path: Path, advertised_limit: int, blob: str) -> threading.Thread:
    """Minimal newline-framed daemon: answers system.hello, then one call."""

    def reply(conn: socket.socket, request: dict[str, object], result: dict[str, object]) -> None:
        payload = {"status": "ok", "request_id": request["request_id"], "result": result}
        conn.sendall(json.dumps(payload).encode("utf-8") + b"\n")

    # Bind and listen synchronously: connecting between bind() and listen()
    # fails with ECONNREFUSED instead of queueing.
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(4)

    def run() -> None:
        # The transport drops the connection and retries once after it
        # refuses a frame, so keep accepting connections for the lifetime
        # of the test and survive the peer closing mid-send.
        while True:
            conn, _ = listener.accept()
            try:
                reader = conn.makefile("rb")
                while True:
                    line = reader.readline()
                    if not line:
                        break
                    request = json.loads(line)
                    if request["method"] == "system.hello":
                        reply(
                            conn,
                            request,
                            {
                                "identity": IDENTITY,
                                "protocol": request["protocol"],
                                "max_frame_bytes": advertised_limit,
                            },
                        )
                    else:
                        reply(conn, request, {"blob": blob})
            except OSError:
                pass
            finally:
                conn.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_reads_frames_beyond_the_legacy_256_kib_limit(tmp_path: Path) -> None:
    socket_path = tmp_path / "memoryd.sock"
    blob = "x" * (300 * 1024)
    _serve(socket_path, advertised_limit=4 * 1024 * 1024, blob=blob)

    with MemoryClient.connect(socket_path, timeout=5.0) as client:
        assert client._transport._max_frame_bytes == 4 * 1024 * 1024
        result = client._call("system.stats", {})

    assert result == {"blob": blob}


def test_adopts_the_advertised_frame_limit_of_older_daemons(tmp_path: Path) -> None:
    socket_path = tmp_path / "memoryd.sock"
    blob = "x" * (300 * 1024)
    # An older 256 KiB daemon must keep its own limit: the oversized
    # response is refused client-side instead of silently truncated.
    _serve(socket_path, advertised_limit=256 * 1024, blob=blob)

    client = MemoryClient.connect(socket_path, timeout=5.0)
    try:
        assert client._transport._max_frame_bytes == 256 * 1024
        try:
            client._call("system.stats", {})
        except ConnectionError as error:
            assert "oversized" in str(error)
        else:
            raise AssertionError("expected the advertised limit to reject the frame")
    finally:
        client.close()
