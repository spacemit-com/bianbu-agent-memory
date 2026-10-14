#!/usr/bin/env python3
"""Three-session e2e for the Hermes-aligned injection redesign (2026-09-11).

Reproduces the original failure scenario end to end against an isolated
daemon, driving the real SDK bridge exactly the way the Runtime does:

  session 1 (travel) — two completed turns via ``record_turn``;
  session 2 (chess)  — a first run in execution: ``record_run_started``
                       watermark only, no completed turn;
  session 3 (hello)  — a fresh session that asks something unrelated.

Assertions (the three rules of the redesign):

  * rule 2 (no completed-turn gate): the chess session is visible in the
    default injection as ``进行中`` even though it has zero completed turns;
  * rule 1 (status, not content): the injection for session 3 is status
    lines only — the travel session appears with its turn count, and no
    travel transcript body leaks into the injection.  A coreference-style
    prompt in a fourth session changes nothing: referent resolution depends
    on the session_search pull path (runtime-side guidance), never on an
    unconditional push;
  * MEM-L1-01 service side is unchanged: the ``recent_sessions`` service
    mode still returns the full transcript, which is what a pull recovers.

The runtime-side half of the scenario (boundary instructions, policy text,
session_search guidance, fire-and-forget watermark) is covered by
``bianbu-agent-runtime`` tests/sdk/plugins/test_memory_hooks.py.

Usage (K3, from the repository root after ``cargo build --release``):

    PYTHONPATH=sdk/python/src python3 scripts/k3_three_session_e2e.py \
        --memoryd target/release/bianbu-agent-memoryd
"""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from bianbu_agent_sdk.memory import AsyncMemoryClient
from bianbu_agent_sdk.memory.runtime import MemoryBridge

TRAVEL_TURNS = [
    (
        "帮我规划一个三天的杭州旅游行程",
        "第一天西湖环线，第二天灵隐寺和飞来峰，第三天西溪湿地。",
    ),
    (
        "第二天的行程改成博物馆可以吗",
        "可以，第二天改为浙江省博物馆加河坊街。",
    ),
]
CHESS_USER_TEXT = "帮我开发一个象棋游戏，要求能人机对战"
TRAVEL_BODY_MARKERS = ("灵隐寺", "西溪湿地", "浙江省博物馆", "河坊街")


def wait_for_socket(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr else "memoryd exited"
            raise RuntimeError(f"memoryd exited during e2e setup: {stderr[-2000:]}")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(path))
            return
        except OSError:
            time.sleep(0.05)
        finally:
            probe.close()
    raise TimeoutError(f"memoryd socket was not ready: {path}")


async def run(args: argparse.Namespace) -> dict[str, object]:
    memoryd = args.memoryd.resolve()
    if not memoryd.is_file():
        raise FileNotFoundError(memoryd)

    with tempfile.TemporaryDirectory(prefix="agent-memory-three-session-") as raw:
        root = Path(raw)
        socket_path = root / "memoryd.sock"
        secret = root / "account-secret"
        secret.write_text(uuid.uuid4().hex * 2 + "\n", encoding="utf-8")
        secret.chmod(0o600)
        process = subprocess.Popen(
            [
                str(memoryd),
                "--development-identity",
                "--socket",
                str(socket_path),
                "--database",
                str(root / "memory.db"),
                "--key-package",
                str(root / "memory.keypkg"),
                "--account-secret-file",
                str(secret),
                "--initialize-key-package",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            wait_for_socket(socket_path, process)
            client = await AsyncMemoryClient.connect(socket_path, timeout=30.0)
            bridge = MemoryBridge(str(socket_path))
            bridge._client = client
            try:
                # --- session 1: travel, two completed turns -----------------
                for index, (user, assistant) in enumerate(TRAVEL_TURNS, 1):
                    recorded = await bridge.record_turn(
                        run_id=f"e2e-travel-turn-{index}",
                        session_id="e2e-travel-session",
                        agent_id="e2e-agent",
                        user_text=user,
                        assistant_text=assistant,
                    )
                    if not recorded:
                        raise AssertionError(f"travel turn {index} was not persisted")

                # --- session 2: chess, first run in execution ---------------
                started = await bridge.record_run_started(
                    run_id="e2e-chess-run-1",
                    session_id="e2e-chess-session",
                    agent_id="e2e-agent",
                    user_text=CHESS_USER_TEXT,
                )
                if not started:
                    raise AssertionError("run-start watermark was not written")

                # --- session 3: hello (unrelated fresh session) -------------
                context = await bridge.recent_context("e2e-hello-session")

                # --- session 4: coreference-style prompt --------------------
                coref_context = await bridge.recent_context("e2e-coref-session")

                # --- MEM-L1-01 recovery shape (service side) ----------------
                recovery = await client.sessions.get_recent_context(
                    exclude_session_id="e2e-hello-session",
                    selection_mode="recent_sessions",
                    turn_limit=50,
                    summary_limit=20,
                )
            finally:
                # bridge.close() closes the injected client as well.
                await bridge.close()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()

    def body_leak(lines: list[str]) -> list[str]:
        return [marker for marker in TRAVEL_BODY_MARKERS if any(marker in l for l in lines)]

    report: dict[str, object] = {"status": "THREE_SESSION_E2E_OK", "sessions": {}}

    # Rule 2: the in-progress chess session must be visible with zero
    # completed turns.
    chess_lines = [l for l in context if "象棋" in l]
    if not chess_lines or not any("进行中" in l for l in chess_lines):
        raise AssertionError(f"chess session not visible as in progress: {context}")
    report["sessions"]["chess_status_line"] = chess_lines[0]

    # Rule 1: travel visible as a status line with its turn count, but its
    # transcript bodies must not be injected.
    travel_lines = [l for l in context if "杭州旅游" in l]
    if not travel_lines or not any("2轮完成" in l for l in travel_lines):
        raise AssertionError(f"travel session status line missing turn count: {context}")
    leaked = body_leak(context)
    if leaked:
        raise AssertionError(f"travel transcript bodies leaked into injection: {leaked}")
    report["sessions"]["travel_status_line"] = travel_lines[0]

    # The time header survives (it anchors relative-activity rendering).
    if not any(l.startswith("[current_local_time=") for l in context):
        raise AssertionError(f"local time header missing: {context}")

    # Coreference prompt must not change the shape: still no bodies pushed.
    coref_leaked = body_leak(coref_context)
    if coref_leaked:
        raise AssertionError(f"coreference prompt resurrected turn bodies: {coref_leaked}")
    if not any("进行中" in l for l in coref_context):
        raise AssertionError(f"chess in-progress line missing in coreference context: {coref_context}")
    report["sessions"]["coref_context_lines"] = len(coref_context)

    # MEM-L1-01: the service-side recovery API still carries the transcript.
    recovered_text = json.dumps(recovery, ensure_ascii=False)
    missing = [m for m in TRAVEL_BODY_MARKERS if m not in recovered_text]
    if missing:
        raise AssertionError(f"recovery API lost transcript content: {missing}")
    report["recovery_api_transcript_present"] = True

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--memoryd", type=Path, default=Path("target/release/bianbu-agent-memoryd"))
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
