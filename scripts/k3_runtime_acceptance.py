#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
import urllib.request
import uuid


TERMINAL = {"completed", "failed", "cancelled"}


def call(base_url: str, method: str, path: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base_url + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.load(response)


def create_session(base_url: str, title: str) -> str:
    return str(call(base_url, "POST", "/sessions", {"title": title})["data"]["id"])


def run(base_url: str, session_id: str, prompt: str, label: str) -> dict:
    created = call(
        base_url,
        "POST",
        "/runs",
        {
            "session_id": session_id,
            "prompt": prompt,
            "idempotency_key": f"memory-acceptance:{label}:{uuid.uuid4().hex}",
        },
    )["data"]
    run_id = str(created["id"])
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        snapshot = call(base_url, "GET", f"/runs/{run_id}")["data"]
        status = str(snapshot["status"])
        if status in TERMINAL:
            if status != "completed":
                raise RuntimeError(json.dumps(snapshot, ensure_ascii=False))
            return snapshot
        if status == "waiting":
            raise RuntimeError("acceptance Run unexpectedly requires approval")
        time.sleep(0.5)
    raise TimeoutError(run_id)


def require(answer: str, *needles: str) -> None:
    lowered = answer.casefold()
    missing = [needle for needle in needles if needle.casefold() not in lowered]
    if missing:
        raise AssertionError(f"answer is missing {missing}: {answer}")


def require_any(answer: str, *alternatives: str) -> None:
    lowered = answer.casefold()
    if not any(item.casefold() in lowered for item in alternatives):
        raise AssertionError(
            f"answer is missing one of {alternatives}: {answer}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:9900/api/v1")
    parser.add_argument("--mode", choices=("seed-and-recall", "restart-recall"), required=True)
    args = parser.parse_args()

    marker = "Nebula-742"
    if args.mode == "seed-and-recall":
        source_session = create_session(args.base_url, "AgentMemory final UTF-8 seed")
        prompts = (
            f'Remember that project code is "{marker}" and its acceptance report is due Friday. Reply OK only.',
            "I prefer a dark terminal theme. Reply OK only.",
            "I prefer uv instead of pip as the Python package manager. Reply OK only.",
            "My usual browser is Firefox. Reply OK only.",
            "My name is Acceptance User 742. Reply OK only.",
        )
        for index, prompt in enumerate(prompts, 1):
            run(args.base_url, source_session, prompt, f"seed-{index}")

        recall_session = create_session(args.base_url, "AgentMemory final UTF-8 recall")
        questions = (
            ("context", "What project code and deadline did I just give you?", (marker,)),
            ("theme", "What is my terminal theme preference?", ("dark",)),
            (
                "habit",
                "Give the command to add requests to a new Python project, following my package manager habit.",
                ("uv", "requests"),
            ),
            ("browser", "What browser do I usually use?", ("Firefox",)),
            ("profile", "What is my name?", ("Acceptance User 742",)),
        )
        results = []
        for label, prompt, expected in questions:
            snapshot = run(args.base_url, recall_session, prompt, f"recall-{label}")
            answer = str(snapshot["content"])
            require(answer, *expected)
            if label == "context":
                require_any(answer, "Friday", "本周五", "周五", "this Friday")
            results.append({"label": label, "answer": answer})
        output = {
            "status": "K3_RUNTIME_SEED_AND_RECALL_OK",
            "source_session": source_session,
            "recall_session": recall_session,
            "results": results,
        }
    else:
        recall_session = create_session(args.base_url, "AgentMemory restart recall")
        snapshot = run(
            args.base_url,
            recall_session,
            "After the service restart, state my name, terminal theme, and Python package manager preference.",
            "restart-recall",
        )
        answer = str(snapshot["content"])
        require(answer, "Acceptance User 742", "dark", "uv")
        output = {
            "status": "K3_RUNTIME_RESTART_RECALL_OK",
            "recall_session": recall_session,
            "answer": answer,
        }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
