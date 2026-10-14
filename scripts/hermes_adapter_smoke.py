#!/usr/bin/env python3
from __future__ import annotations

import sys

from adapters.hermes import BianbuMemoryProvider


def main(socket_path: str) -> None:
    provider = BianbuMemoryProvider({"socket_path": socket_path})
    assert provider.is_available()
    provider.initialize("hermes-adapter-session", agent_context="primary")
    try:
        provider.on_memory_write(
            "add",
            "user",
            "Hermes adapter preference",
            {"session_id": "hermes-adapter-session"},
        )
        provider.sync_turn(
            "remember this",
            "remembered",
            session_id="hermes-adapter-session",
            messages=[{"role": "user"}, {"role": "assistant"}],
        )
        provider.on_pre_compress(
            [{"role": "user", "content": "remember this"}]
        )
        provider.queue_prefetch(
            "Hermes adapter preference", session_id="hermes-adapter-session"
        )
        provider._jobs.join()  # test-only deterministic drain
        recalled = provider.prefetch(
            "Hermes adapter preference", session_id="hermes-adapter-session"
        )
        assert "Hermes adapter preference" in recalled
        print("HERMES_PROVIDER_ADAPTER_OK")
    finally:
        provider.shutdown()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: hermes_adapter_smoke.py SOCKET_PATH")
    main(sys.argv[1])
