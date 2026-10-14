#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import time
from typing import Any
import urllib.request
import uuid


PREFERENCE_TOPIC = "终端配色偏好"
DARK_PROMPT = "你好，请记住，终端配色偏好：深色。"
LIGHT_PROMPT = "请记住，终端配色偏好：浅色。"
RECALL_PROMPT = "你好，请问终端配色偏好是什么颜色"
REORDERED_RECALL_PROMPT = "你好，目前终端偏好颜色是什么？"
SYSTEM_RECALL_PROMPT = "请问目前系统偏好颜色是什么？"
MISSING_PREFERENCE_PROMPT = "请问我目前的编辑器字体偏好是什么？"
MISSING_PREFERENCE_REPLY = "尚未记录该偏好。你可以告诉我，我会记住。"


def post_json(
    base_url: str, path: str, payload: dict[str, Any], timeout: float
) -> tuple[dict[str, Any], float]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    return result, time.perf_counter() - started


def get_json(base_url: str, path: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def create_session(
    base_url: str, *, title: str, timeout: float
) -> str:
    response, _ = post_json(
        base_url,
        "/api/v1/sessions",
        {"agent_id": "personal-agent-default", "title": title},
        timeout,
    )
    return str(response["data"]["id"])


def run_chat(
    base_url: str,
    *,
    session_id: str,
    prompt: str,
    timeout: float,
    metadata: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    response, _ = post_json(
        base_url,
        "/api/v1/runs",
        {
            "session_id": session_id,
            "prompt": prompt,
            "idempotency_key": f"k3-memory-regression:{uuid.uuid4().hex}",
            "metadata": metadata or {},
        },
        timeout,
    )
    run = response["data"]
    run_id = str(run["id"])
    deadline = time.monotonic() + timeout
    while str(run["status"]) not in {"completed", "failed", "cancelled"}:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Run did not finish: {run_id}")
        time.sleep(0.2)
        run = get_json(base_url, f"/api/v1/runs/{run_id}", timeout)["data"]
    if run["status"] != "completed":
        raise AssertionError(f"Run failed: {run}")
    return run, time.perf_counter() - started


def snapshot(database: Path, session_a: str, session_b: str) -> dict[str, Any]:
    with sqlite3.connect(database) as connection:
        matching = connection.execute(
            """
            SELECT memory_id, content, current_revision
            FROM long_term_memories
            WHERE content LIKE ?
            ORDER BY created_at
            """,
            (f"%{PREFERENCE_TOPIC}%",),
        ).fetchall()
        events = {
            session_id: connection.execute(
                "SELECT COUNT(*) FROM session_events WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            for session_id in (session_a, session_b)
        }
        total_events = connection.execute(
            "SELECT COUNT(*) FROM session_events"
        ).fetchone()[0]
        total_memories = connection.execute(
            "SELECT COUNT(*) FROM long_term_memories"
        ).fetchone()[0]
    return {
        "matching_memories": matching,
        "session_events": events,
        "total_events": total_events,
        "total_memories": total_memories,
    }


def assistant_content(response: dict[str, Any]) -> str:
    for key in ("content", "response", "reply", "assistant_reply", "message"):
        value = response.get(key)
        if isinstance(value, str):
            return value
    return ""


def validate(args: argparse.Namespace) -> dict[str, Any]:
    database = Path(args.database).expanduser().resolve()
    if not database.is_file():
        raise FileNotFoundError(database)

    prefix = args.session_prefix or f"memory-regression-{int(time.time())}"
    session_a = create_session(
        args.base_url, title=f"{prefix}-write", timeout=args.timeout
    )
    session_b = create_session(
        args.base_url, title=f"{prefix}-recall", timeout=args.timeout
    )
    baseline = snapshot(database, session_a, session_b)
    if len(baseline["matching_memories"]) > 1:
        raise AssertionError(
            f"duplicate preference records exist before test: "
            f"{baseline['matching_memories']}"
        )

    base_revision = (
        baseline["matching_memories"][0][2]
        if baseline["matching_memories"]
        else 0
    )
    base_memory_id = (
        baseline["matching_memories"][0][0]
        if baseline["matching_memories"]
        else None
    )

    dark_response, dark_seconds = run_chat(
        args.base_url,
        session_id=session_a,
        prompt=DARK_PROMPT,
        timeout=args.timeout,
    )
    after_dark = snapshot(database, session_a, session_b)
    assert len(after_dark["matching_memories"]) == 1, after_dark
    dark_memory = after_dark["matching_memories"][0]
    assert "深色" in dark_memory[1], dark_memory
    assert dark_memory[2] == base_revision + 1, (baseline, after_dark)
    if base_memory_id is not None:
        assert dark_memory[0] == base_memory_id, (base_memory_id, dark_memory)

    light_response, light_seconds = run_chat(
        args.base_url,
        session_id=session_a,
        prompt=LIGHT_PROMPT,
        timeout=args.timeout,
    )
    after_light = snapshot(database, session_a, session_b)
    assert len(after_light["matching_memories"]) == 1, after_light
    light_memory = after_light["matching_memories"][0]
    assert light_memory[0] == dark_memory[0], (dark_memory, light_memory)
    assert "浅色" in light_memory[1], light_memory
    assert light_memory[2] == base_revision + 2, (baseline, after_light)
    assert after_light["session_events"][session_a] == 4, after_light

    before_title = snapshot(database, session_a, session_b)
    title_response, title_seconds = run_chat(
        args.base_url,
        session_id=session_a,
        prompt=f"Generate a concise conversation title for: {LIGHT_PROMPT}",
        timeout=args.timeout,
        metadata={"console_operation": "generate_title"},
    )
    after_title = snapshot(database, session_a, session_b)
    assert after_title == before_title, (before_title, after_title)

    recall_response, recall_seconds = run_chat(
        args.base_url,
        session_id=session_b,
        prompt=RECALL_PROMPT,
        timeout=args.timeout,
    )
    after_recall = snapshot(database, session_a, session_b)
    assert "浅色" in assistant_content(recall_response), recall_response
    assert after_recall["matching_memories"] == after_light["matching_memories"], (
        after_light,
        after_recall,
    )
    assert after_recall["session_events"][session_b] == 2, after_recall

    reordered_session = create_session(
        args.base_url, title=f"{prefix}-reordered", timeout=args.timeout
    )
    reordered_response, reordered_seconds = run_chat(
        args.base_url,
        session_id=reordered_session,
        prompt=REORDERED_RECALL_PROMPT,
        timeout=args.timeout,
    )
    after_reordered = snapshot(database, session_a, session_b)
    assert "浅色" in assistant_content(reordered_response), reordered_response
    assert after_reordered["matching_memories"] == after_light["matching_memories"], (
        after_light,
        after_reordered,
    )

    system_session = create_session(
        args.base_url, title=f"{prefix}-system-wording", timeout=args.timeout
    )
    system_response, system_seconds = run_chat(
        args.base_url,
        session_id=system_session,
        prompt=SYSTEM_RECALL_PROMPT,
        timeout=args.timeout,
    )
    after_system = snapshot(database, session_a, session_b)
    assert "浅色" in assistant_content(system_response), system_response
    assert after_system["matching_memories"] == after_light["matching_memories"], (
        after_light,
        after_system,
    )

    missing_session = create_session(
        args.base_url, title=f"{prefix}-missing-preference", timeout=args.timeout
    )
    missing_response, missing_seconds = run_chat(
        args.base_url,
        session_id=missing_session,
        prompt=MISSING_PREFERENCE_PROMPT,
        timeout=args.timeout,
    )
    after_missing = snapshot(database, session_a, session_b)
    assert assistant_content(missing_response) == MISSING_PREFERENCE_REPLY, (
        missing_response
    )
    assert after_missing["matching_memories"] == after_light["matching_memories"], (
        after_light,
        after_missing,
    )

    return {
        "status": "K3_RUNTIME_MEMORY_REGRESSION_OK",
        "sessions": {
            "write": session_a,
            "recall": session_b,
            "system_wording": system_session,
            "missing_preference": missing_session,
        },
        "memory": {
            "memory_id": light_memory[0],
            "content": light_memory[1],
            "revision_before": base_revision,
            "revision_after": light_memory[2],
            "matching_records": len(after_recall["matching_memories"]),
        },
        "title_isolation": {
            "events_before": before_title["total_events"],
            "events_after": after_title["total_events"],
            "response": title_response,
        },
        "recall": {
            "original_word_order": recall_response,
            "reordered_terms": reordered_response,
            "system_wording": system_response,
            "missing_preference": missing_response,
        },
        "responses": {"dark": dark_response, "light": light_response},
        "timings_seconds": {
            "dark": round(dark_seconds, 3),
            "light": round(light_seconds, 3),
            "title": round(title_seconds, 3),
            "recall": round(recall_seconds, 3),
            "reordered_recall": round(reordered_seconds, 3),
            "system_recall": round(system_seconds, 3),
            "missing_preference": round(missing_seconds, 3),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the two-session Runtime/AgentMemory regression on K3."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:9899")
    parser.add_argument(
        "--database",
        default=(
            "/home/example/.local/state/bianbu-agent-memory-manual/memory.db"
        ),
    )
    parser.add_argument("--session-prefix")
    parser.add_argument("--timeout", type=float, default=300.0)
    arguments = parser.parse_args()
    print(json.dumps(validate(arguments), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
