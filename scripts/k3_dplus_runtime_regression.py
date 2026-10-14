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


LIGHT_PROMPT = "请记住：终端配色偏好是浅色。"
DARK_PROMPT = "请记住：系统偏好颜色改为深色。"
RECALL_PROMPT = "请问终端配色偏好是什么？"
SYSTEM_RECALL_PROMPT = "请问目前系统偏好颜色是什么？"
SLOT_TAG = "slot:appearance.terminal_color_scheme"


def request_json(
    url: str, *, payload: dict[str, Any] | None = None, timeout: float = 300.0
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def create_session(base_url: str, title: str, timeout: float) -> str:
    result = request_json(
        f"{base_url}/api/v1/sessions",
        payload={"agent_id": "personal-agent-default", "title": title},
        timeout=timeout,
    )
    return str(result["data"]["id"])


def run(base_url: str, session_id: str, prompt: str, timeout: float) -> dict[str, Any]:
    result = request_json(
        f"{base_url}/api/v1/runs",
        payload={
            "session_id": session_id,
            "prompt": prompt,
            "idempotency_key": f"dplus-runtime:{uuid.uuid4()}",
            "metadata": {},
        },
        timeout=timeout,
    )["data"]
    run_id = result["id"]
    deadline = time.monotonic() + timeout
    while result["status"] not in {"completed", "failed", "cancelled"}:
        if time.monotonic() >= deadline:
            raise TimeoutError(run_id)
        time.sleep(0.2)
        result = request_json(
            f"{base_url}/api/v1/runs/{run_id}", timeout=timeout
        )["data"]
    if result["status"] != "completed":
        raise AssertionError(result)
    return result


def assistant_text(run_result: dict[str, Any]) -> str:
    for key in ("content", "response", "reply", "assistant_reply", "message"):
        if isinstance(run_result.get(key), str):
            return str(run_result[key])
    return ""


def projection_status(dplus_url: str, timeout: float) -> dict[str, Any]:
    return request_json(f"{dplus_url}/health", timeout=timeout)["projection"]


def wait_projection(
    dplus_url: str, *, minimum_completed: int, timeout: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = projection_status(dplus_url, timeout)
        if (
            int(status["completed"]) >= minimum_completed
            and int(status["pending"]) == 0
            and int(status["leased"]) == 0
        ):
            return status
        time.sleep(0.1)
    raise TimeoutError("D+ projection did not drain")


def slot_snapshot(database: Path) -> dict[str, Any]:
    with sqlite3.connect(database) as connection:
        records = connection.execute(
            """
            SELECT memory_id, content, current_revision, tags_json
            FROM long_term_memories
            WHERE tags_json LIKE ?
               OR content LIKE '%终端%配色%'
               OR content LIKE '%系统偏好%色%'
            ORDER BY updated_at DESC
            """,
            (f"%{SLOT_TAG}%",),
        ).fetchall()
        active_mappings = connection.execute(
            """
            SELECT COUNT(*) FROM semantic_projection_mappings
            WHERE provider='mem0-oss-v2' AND state='active'
            """
        ).fetchone()[0]
    return {"records": records, "active_mappings": active_mappings}


def validate(args: argparse.Namespace) -> dict[str, Any]:
    base_url = args.base_url.rstrip("/")
    dplus_url = args.dplus_url.rstrip("/")
    database = Path(args.database).resolve()
    prefix = f"dplus-runtime-{int(time.time())}"
    baseline = projection_status(dplus_url, args.timeout)
    completed = int(baseline["completed"])

    write_light = create_session(base_url, f"{prefix}-light", args.timeout)
    light_result = run(base_url, write_light, LIGHT_PROMPT, args.timeout)
    light_status = wait_projection(
        dplus_url, minimum_completed=completed + 1, timeout=args.timeout
    )
    completed = int(light_status["completed"])
    light_snapshot = slot_snapshot(database)
    assert light_snapshot["records"], light_snapshot
    assert all("浅色" in row[1] for row in light_snapshot["records"]), light_snapshot

    write_dark = create_session(base_url, f"{prefix}-dark", args.timeout)
    dark_result = run(base_url, write_dark, DARK_PROMPT, args.timeout)
    dark_status = wait_projection(
        dplus_url, minimum_completed=completed + 1, timeout=args.timeout
    )
    completed = int(dark_status["completed"])
    dark_snapshot = slot_snapshot(database)
    assert dark_snapshot["records"], dark_snapshot
    assert all("深色" in row[1] for row in dark_snapshot["records"]), dark_snapshot
    assert dark_snapshot["active_mappings"] == 1, dark_snapshot

    recall_session = create_session(base_url, f"{prefix}-recall", args.timeout)
    recall_result = run(base_url, recall_session, RECALL_PROMPT, args.timeout)
    recall_text = assistant_text(recall_result)
    assert "深色" in recall_text and "浅色" not in recall_text, recall_result

    system_session = create_session(base_url, f"{prefix}-system", args.timeout)
    system_result = run(base_url, system_session, SYSTEM_RECALL_PROMPT, args.timeout)
    system_text = assistant_text(system_result)
    assert "深色" in system_text and "浅色" not in system_text, system_result

    final_status = projection_status(dplus_url, args.timeout)
    assert int(final_status["dead"]) == 0, final_status
    assert int(final_status["retry"]) == 0, final_status
    return {
        "status": "K3_DPLUS_RUNTIME_REGRESSION_OK",
        "sessions": {
            "write_light": write_light,
            "write_dark": write_dark,
            "recall": recall_session,
            "system_recall": system_session,
        },
        "write_responses": {"light": light_result, "dark": dark_result},
        "recall_responses": {
            "terminal_wording": recall_result,
            "system_wording": system_result,
        },
        "canonical": dark_snapshot,
        "projection": final_status,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:9899")
    parser.add_argument("--dplus-url", default="http://127.0.0.1:18920")
    parser.add_argument(
        "--database",
        default="/home/example/.local/state/bianbu-agent-memory-manual/memory.db",
    )
    parser.add_argument("--timeout", type=float, default=300.0)
    print(json.dumps(validate(parser.parse_args()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
