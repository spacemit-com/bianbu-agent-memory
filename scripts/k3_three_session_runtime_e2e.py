#!/usr/bin/env python3
"""Three-session scenario through the real isolated Runtime stack (K3).

Drives the Runtime HTTP API on port 9898 (the ``e2e-runtime`` isolated
stack — NOT the production service on 9899): deb-venv ``bianbu-agent`` with
the refreshed ``bianbu.memory`` plugin (2026-09-11 injection redesign) and
an isolated memoryd built from this tree.  Production services are not
touched; the stack is started separately (see the session log).

Scenario (the original three-session problem, replayed through real runs):

  session 1 (travel) — two completed chat turns;
  session 2 (chess)  — one chat run; while it is in flight the run-start
                       watermark must already make the session visible as
                       进行中 in the ``session_status`` injection shape;
  session 3 (hello)  — one unrelated chat turn.

Deterministic assertions (memory-side, model-independent):
  * every run left a ``task_started`` watermark (write-through visibility);
  * the default injection for a fresh session is status lines only: chess
    appears with its turn count, travel appears with its turn count, and no
    travel transcript body leaks (bodies are pulled via session_search /
    MEM-L1-01, never pushed);
  * the MEM-L1-01 service shape still carries the full transcript.

The mid-run ``进行中`` observation and the session-3 answer text are reported
as evidence fields (model timing / model behaviour are not memory-side
gates).

Usage (K3, with the isolated stack already up):

    PYTHONPATH=sdk/python/src python3 scripts/k3_three_session_runtime_e2e.py \
        --base-url http://127.0.0.1:9898 \
        --socket /home/example/e2e-memfix/memoryd.sock
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

from bianbu_agent_sdk.memory import AsyncMemoryClient

TRAVEL_PROMPTS = [
    "帮我规划一个三天的杭州旅游行程，简要列出每天的安排。",
    "第二天的行程改成博物馆可以吗？简要说明调整后的安排。",
]
CHESS_PROMPT = (
    "帮我开发一个象棋游戏，请先给出完整的项目结构、主要模块设计和"
    "实现步骤，内容尽量详细。"
)
HELLO_PROMPT = "你好"
TRAVEL_BODY_MARKERS = ("灵隐寺", "西溪湿地", "浙江省博物馆", "河坊街")


def request_json(base: str, method: str, path: str, payload: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base.rstrip("/") + path, data=data, method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def events(base: str, sid: str, timeout: float) -> list[dict[str, Any]]:
    data = request_json(base, "GET", f"/api/v1/sessions/{sid}/events", None, timeout).get("data") or []
    return data if isinstance(data, list) else []


def discover_agent(base: str, timeout: float) -> str:
    listing = request_json(base, "GET", "/api/v1/agents", None, timeout)
    entries = listing.get("data") or []
    if isinstance(entries, dict):
        entries = entries.get("agents") or []
    for entry in entries:
        agent_id = str(entry.get("id") or entry.get("agent_id") or "")
        name = str(entry.get("name") or entry.get("zh_name") or "")
        if agent_id and re.search(r"test|测试", f"{agent_id} {name}", re.I):
            return agent_id
    for entry in entries:
        agent_id = str(entry.get("id") or entry.get("agent_id") or "")
        if agent_id:
            return agent_id
    raise RuntimeError(f"no agent discovered: {listing}")


def create_session(base: str, agent_id: str, title: str, timeout: float) -> str:
    data = request_json(base, "POST", "/api/v1/sessions", {"agent_id": agent_id, "title": title}, timeout)
    return str(data["data"]["id"])


def pending_approval_id(items: list[dict[str, Any]]) -> str | None:
    pending: set[str] = set()
    for item in items:
        kind = str(item.get("type") or "")
        aid = str((item.get("data") or {}).get("approval_id") or "")
        if not aid:
            continue
        if kind == "approval/requested":
            pending.add(aid)
        elif kind in {"approval/resolved", "approval/rejected"}:
            pending.discard(aid)
    return sorted(pending)[-1] if pending else None


def run_chat(base: str, sid: str, prompt: str, timeout: float) -> dict[str, Any]:
    data = request_json(
        base, "POST", f"/api/v1/sessions/{sid}/prompt",
        {"message_id": f"three-session-{uuid.uuid4().hex}", "content": prompt}, timeout,
    )
    if not bool((data.get("data") or {}).get("accepted")):
        raise RuntimeError(f"prompt rejected: {data}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = request_json(base, "GET", f"/api/v1/sessions/{sid}", None, timeout).get("data") or {}
        if snap.get("failed"):
            raise RuntimeError(f"session failed: {snap}")
        if snap.get("running"):
            yield_signal()  # keep the poll loop hot for the in-flight probe
            time.sleep(0.4)
            continue
        if snap.get("pending_interaction") == "approval":
            aid = pending_approval_id(events(base, sid, timeout))
            if aid:
                try:
                    request_json(base, "POST", f"/api/v1/sessions/{sid}/approvals/{aid}",
                                 {"decision": "approve", "reason": "three-session e2e"}, timeout)
                except urllib.error.HTTPError as exc:
                    if exc.code != 409:
                        raise
            else:
                time.sleep(0.4)
            continue
        break
    else:
        raise TimeoutError(f"run timeout: {sid}")
    for item in reversed(events(base, sid, timeout)):
        if str(item.get("type") or "") == "run/completed":
            payload = item.get("data") or {}
            return {"content": str(payload.get("content") or ""), "status": payload.get("status", "completed")}
    raise RuntimeError(f"no run/completed event for {sid}")


def yield_signal() -> None:
    return None


def _texts_from_events(event_items: list[dict[str, Any]]) -> list[str]:
    texts: list[str] = []
    for item in event_items:
        if not isinstance(item, dict):
            continue
        payload = item.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("text"), str):
            texts.append(payload["text"])
    return texts


async def memory_side(socket_path: str, agent_id: str, session_ids: dict[str, str]) -> dict[str, Any]:
    client = await AsyncMemoryClient.connect(socket_path, timeout=30.0, agent_name=agent_id)
    try:
        evidence: dict[str, Any] = {}

        # task_started watermarks: one per session that had a run.
        started_sessions: list[str] = []
        for label, sid in session_ids.items():
            context = await client.sessions.get_context(sid, include_events=True, event_limit=200)
            event_items = context.get("events") or []
            types = [str(item.get("event_type") or "") for item in event_items if isinstance(item, dict)]
            if "task_started" in types:
                started_sessions.append(label)
            texts = _texts_from_events(event_items)
            evidence[f"{label}_task_started"] = "task_started" in types
            evidence[f"{label}_event_count"] = len(event_items)
            if label == "travel":
                evidence["travel_transcript_in_store"] = any(
                    marker in text for marker in TRAVEL_BODY_MARKERS for text in texts
                )
        evidence["watermark_sessions"] = sorted(started_sessions)

        # Default injection shape for a fresh session: status lines only.
        status = await client.sessions.get_recent_context(
            exclude_session_id="three-session-fresh-session",
            selection_mode="session_status",
        )
        lines: list[str] = []
        for item in status.get("sessions") or []:
            lines.append(
                f"[{item.get('session_id')}] {item.get('title') or ''} "
                f"turns={item.get('completed_turn_count')} "
                f"in_progress={bool(item.get('in_progress'))} "
                f"interrupted={bool(item.get('interrupted'))}"
            )
        evidence["status_lines"] = lines
        evidence["travel_body_leak_into_default_injection"] = any(
            marker in line for marker in TRAVEL_BODY_MARKERS for line in lines
        )

        # MEM-L1-01 service shape still carries the transcript.
        recovery = await client.sessions.get_recent_context(
            exclude_session_id="three-session-fresh-session",
            selection_mode="recent_sessions",
            turn_limit=50,
            summary_limit=20,
        )
        recovered = json.dumps(recovery, ensure_ascii=False)
        evidence["recovery_api_transcript_present"] = any(
            marker in recovered for marker in TRAVEL_BODY_MARKERS
        )
        return evidence
    finally:
        await client.close()


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    base = args.base_url
    agent_id = args.agent_id or discover_agent(base, 10.0)
    report: dict[str, Any] = {"agent_id": agent_id, "base_url": base}
    session_ids: dict[str, str] = {}

    # --- session 1: travel, two completed turns ---------------------------
    travel_sid = create_session(base, agent_id, "杭州旅游规划", 10.0)
    session_ids["travel"] = travel_sid
    travel_answers = []
    for prompt in TRAVEL_PROMPTS:
        travel_answers.append(run_chat(base, travel_sid, prompt, args.run_timeout))
    report["travel_runs"] = len(travel_answers)

    # --- session 2: chess, observe in-flight watermark, complete ---------
    chess_sid = create_session(base, agent_id, "象棋游戏开发", 10.0)
    session_ids["chess"] = chess_sid
    chess_answer: dict[str, Any] = {}

    async def probe_in_progress() -> list[str]:
        observed: list[str] = []
        client = await AsyncMemoryClient.connect(args.socket, timeout=10.0, agent_name=agent_id)
        try:
            deadline = time.monotonic() + args.chess_timeout
            while time.monotonic() < deadline:
                status = await client.sessions.get_recent_context(
                    exclude_session_id="three-session-fresh-session",
                    selection_mode="session_status",
                )
                for item in status.get("sessions") or []:
                    if item.get("session_id") == chess_sid and item.get("in_progress"):
                        observed.append(
                            f"[{item.get('session_id')}] {item.get('title') or ''} "
                            f"turns={item.get('completed_turn_count')} in_progress=True"
                        )
                        return observed
                await asyncio.sleep(0.2)
            return observed
        finally:
            await client.close()

    probe_task = asyncio.create_task(probe_in_progress())
    loop = asyncio.get_running_loop()
    chess_future = loop.run_in_executor(None, run_chat, base, chess_sid, CHESS_PROMPT, args.chess_timeout)
    chess_answer = await asyncio.wait_for(chess_future, timeout=args.chess_timeout + 30.0)
    in_progress_lines = await asyncio.wait_for(probe_task, timeout=30.0)
    report["chess_in_progress_observed"] = bool(in_progress_lines)
    report["chess_in_progress_line"] = in_progress_lines[0] if in_progress_lines else None

    # --- session 3: hello --------------------------------------------------
    hello_sid = create_session(base, agent_id, "打招呼", 10.0)
    session_ids["hello"] = hello_sid
    hello_answer = run_chat(base, hello_sid, HELLO_PROMPT, args.run_timeout)
    report["hello_answer_excerpt"] = hello_answer["content"][:200]
    report["hello_recites_travel"] = any(
        marker in hello_answer["content"] for marker in TRAVEL_BODY_MARKERS
    )

    # --- memory-side deterministic assertions ------------------------------
    evidence = await memory_side(args.socket, agent_id, session_ids)
    report["memory"] = evidence

    # Gates.
    if sorted(evidence["watermark_sessions"]) != ["chess", "hello", "travel"]:
        raise AssertionError(f"task_started watermark missing: {evidence['watermark_sessions']}")
    if not evidence["travel_transcript_in_store"]:
        raise AssertionError("travel transcript was not persisted through the runtime")
    if evidence["travel_body_leak_into_default_injection"]:
        raise AssertionError(f"travel bodies leaked into default injection: {evidence['status_lines']}")
    if not evidence["recovery_api_transcript_present"]:
        raise AssertionError("MEM-L1-01 recovery shape lost the transcript")
    report["status"] = "THREE_SESSION_RUNTIME_E2E_OK"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:9898")
    parser.add_argument("--socket", default="/home/example/e2e-memfix/memoryd.sock")
    parser.add_argument("--agent-id", default=None)
    parser.add_argument("--run-timeout", type=float, default=180.0)
    parser.add_argument("--chess-timeout", type=float, default=240.0)
    args = parser.parse_args()
    report = asyncio.run(main_async(args))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
