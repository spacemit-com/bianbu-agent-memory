#!/usr/bin/env python3
"""MEM-NFR-02 end-to-end: semantic recall through the real Runtime chain.

Runs against the K3 manual e2e stack on port 9898 (NOT the production service
on 9899): deb runtime venv + ``/home/example/e2e-runtime/config.yaml`` with
``security.required=false``, a manual memoryd on
``--development-identity --development-agent-name <agent-id>`` (so the worker
bridge and this script's SDK client share one dev namespace), and
``BIANBU_MEMORY_ENGINE=native`` so Runtime recall goes through memoryd's
native search_headers path where the vector branch lives. The runtime process
needs ``BIANBU_RUNTIME_ALLOW_UNMANAGED_WORKER=1 BIANBU_RUNTIME_SECURITY=optional
BIANBU_RUNTIME_MODE=development BIANBU_SECURITY=0`` in its environment
(development fallback worker; metering callbacks are dev-degraded).

Chat protocol (this runtime version): POST /api/v1/sessions/{sid}/prompt
{message_id, content} -> 202; completion via GET /api/v1/sessions/{sid}
(running/failed/pending_interaction); reply text from the run/completed
event. Tool calls trigger HITL approvals (agent.yaml capability confirmation
policy — data.memory is once_per_session), resolved automatically here via
POST /api/v1/sessions/{sid}/approvals/{approval_id} {decision: approve} —
equivalent to the user tapping 允许 in the production chat UI.

Flow:
  1. discover the on-device test agent (测试专家): --agent-id wins, then the
     runtime registry, then GET /api/v1/agents, then the default agent id;
  2. enable embedding on the daemon (Qwen3 llama-server at --embedding-url);
  3. session A (test agent) writes a unique preference via natural chat;
  4. A/B hard assert on memoryd directly:
       a. embedding disabled  -> reworded query must NOT retrieve the memory
          (proves the hit below is semantic, not lexical);
       b. embedding enabled   -> reworded query must rank the memory first;
  5. session B asks the reworded question through the Runtime; soft assert
     the assistant reply contains the marker;
  6. fallback: llama-server is stopped, session C asks a lexical question;
     the Runtime must still answer from FTS (silent degradation e2e).

The write marker is regenerated per run, so the script is repeatable.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from bianbu_agent_sdk.memory import MemoryClient

# The Gateway's model id, which memoryd stores verbatim in every vector row and
# compares to decide whether a stored vector is stale.
MODEL_ID = "qwen3-embedding-0.6b-q8_0"
EMBED_DIMS = 1024
# Base-URL fragment identifying the spacemit-ai-gateway Embed domain (as opposed
# to a bare llama-server), plus the start-up arguments the provisioning unit
# pins for this model.  A fallback test that restarts the model through the
# Gateway must reproduce those arguments rather than inherit the Gateway's
# embed.default_args, which belong to the intent module's bibit-embed entry.
EMBED_GATEWAY_MARKER = "/v1/embed"
EMBED_EXTRA_ARGS = [
    "--pooling", "last",
    "--embd-normalize", "2",
    "--ctx-size", "4096",
    "--parallel", "1",
]
DEFAULT_INSTRUCT = (
    "Instruct: Given a user query about a person's memories, retrieve the "
    "stored memory records that are semantically relevant to the query\nQuery: "
)
# Reworded query shares no lexical token with the write marker: the FTS
# cascade cannot see it, only the vector branch can.
MARKERS = ("夜航蓝", "霜叶红", "远山黛")
REWORDED_QUERY = "界面颜色主题喜欢哪种风格"
LEXICAL_QUERY = "终端配色偏好是什么颜色"


def post_json(base_url: str, path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_json(base_url: str, path: str, timeout: float) -> dict[str, Any]:
    with urllib.request.urlopen(f"{base_url.rstrip('/')}{path}", timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def discover_agent(base_url: str, timeout: float, registry: Path) -> tuple[str, list[str]]:
    """Find the on-device test agent (测试专家)."""
    candidates: list[str] = []
    try:
        for entry in sorted(registry.glob("*.json")):
            try:
                registration = json.loads(entry.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            agent_id = registration.get("agent_id")
            name = registration.get("agent_name") or ""
            if agent_id and re.search(r"test|测试", f"{agent_id} {name}", re.I):
                return str(agent_id), []
            if agent_id:
                candidates.append(str(agent_id))
    except OSError:
        pass
    try:
        listing = get_json(base_url, "/api/v1/agents", timeout)
        entries = listing.get("data") or []
        for entry in entries if isinstance(entries, list) else []:
            agent_id = entry.get("id") or entry.get("agent_id")
            name = entry.get("name") or ""
            if agent_id and re.search(r"test|测试", f"{agent_id} {name}", re.I):
                return str(agent_id), []
            if agent_id:
                candidates.append(str(agent_id))
    except OSError:
        pass
    return "personal-agent-default", candidates


def create_session(base_url: str, agent_id: str, title: str, timeout: float) -> str:
    response = post_json(
        base_url, "/api/v1/sessions", {"agent_id": agent_id, "title": title}, timeout
    )
    return str(response["data"]["id"])


def list_events(base_url: str, session_id: str, timeout: float) -> list[dict[str, Any]]:
    response = get_json(base_url, f"/api/v1/sessions/{session_id}/events", timeout)
    entries = response.get("data") or []
    return entries if isinstance(entries, list) else []


def pending_approval_id(events: list[dict[str, Any]]) -> str | None:
    """Latest approval/requested id with no matching approval/resolved."""
    unresolved: set[str] = set()
    for event in events:
        kind = str(event.get("type") or "")
        identifier = str((event.get("data") or {}).get("approval_id") or "")
        if not identifier:
            continue
        if kind == "approval/requested":
            unresolved.add(identifier)
        elif kind in {"approval/resolved", "approval/rejected"}:
            unresolved.discard(identifier)
    if not unresolved:
        return None
    return sorted(unresolved)[-1]


def run_chat(base_url: str, session_id: str, prompt: str, timeout: float) -> dict[str, Any]:
    """Send a prompt and drive the run to completion, auto-approving HITL
    tool approvals along the way (the production chat UI equivalent)."""
    response = post_json(
        base_url,
        f"/api/v1/sessions/{session_id}/prompt",
        {
            "message_id": f"k3-semantic-e2e-{uuid.uuid4().hex}",
            "content": prompt,
        },
        timeout,
    )
    accepted = bool((response.get("data") or {}).get("accepted"))
    if not accepted:
        raise AssertionError(f"Prompt not accepted: {response}")

    deadline = time.monotonic() + timeout
    approved: set[str] = set()
    while True:
        session = get_json(base_url, f"/api/v1/sessions/{session_id}", timeout).get("data") or {}
        if session.get("failed"):
            events = list_events(base_url, session_id, timeout)
            failure = next(
                (
                    event.get("data") or {}
                    for event in reversed(events)
                    if str(event.get("type") or "") == "run/failed"
                ),
                {},
            )
            raise AssertionError(f"Run failed: {failure.get('error') or session}")
        if session.get("running"):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Run did not finish: session={session_id}")
            time.sleep(0.5)
            continue
        if session.get("pending_interaction") == "approval":
            events = list_events(base_url, session_id, timeout)
            approval_id = pending_approval_id(events)
            if approval_id is None or approval_id in approved:
                # Already resolved (the approval/resolved event lands
                # asynchronously) — wait for the session state to clear.
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Approval state stuck: session={session_id}")
                time.sleep(0.5)
                continue
            try:
                post_json(
                    base_url,
                    f"/api/v1/sessions/{session_id}/approvals/{approval_id}",
                    {"decision": "approve", "reason": "k3 semantic e2e auto-approve"},
                    timeout,
                )
            except urllib.error.HTTPError as exc:
                # 409 = the approval was already resolved by an earlier POST
                # (benign race); anything else propagates.
                if exc.code != 409:
                    raise
            approved.add(approval_id)
            continue
        break

    events = list_events(base_url, session_id, timeout)
    completed = next(
        (
            event.get("data") or {}
            for event in reversed(events)
            if str(event.get("type") or "") == "run/completed"
        ),
        None,
    )
    if completed is None:
        raise AssertionError(f"No run/completed event: session={session_id}")
    return {
        "status": str(completed.get("status") or "completed"),
        "content": completed.get("content") or "",
        "usage": completed.get("usage") or {},
    }


def assistant_content(run: dict[str, Any]) -> str:
    for key in ("content", "response", "reply", "assistant_reply", "message"):
        value = run.get(key)
        if isinstance(value, str):
            return value
    output = run.get("output")
    if isinstance(output, str):
        return output
    return ""


def embedding_patch(base_url: str, enabled: bool) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "base_url": base_url,
        "model": MODEL_ID,
        "dims": EMBED_DIMS,
        "query_instruct": DEFAULT_INSTRUCT,
    }


def embedding_healthy(embedding_url: str) -> bool:
    try:
        if EMBED_GATEWAY_MARKER in embedding_url:
            probe = f"{embedding_url.rstrip('/')}/healthz"
        else:
            probe = f"{embedding_url.rsplit('/v1', 1)[0]}/health"
        with urllib.request.urlopen(probe, timeout=5) as response:
            return response.status == 200
    except OSError:
        return False


def search_rank(socket_path: Path, query: str, marker: str, top_k: int = 5) -> tuple[int, str]:
    """Return (rank of the marker memory, joined top texts); 0 = not found."""
    with MemoryClient.connect(socket_path, timeout=30.0) as client:
        headers = client.long_term.search_headers(query, top_k=top_k)
        if not headers:
            return 0, ""
        records = client.long_term.get_many([header.memory_id for header in headers])
    rank = 0
    for index, record in enumerate(records, start=1):
        if marker in (record.text or ""):
            rank = index
            break
    return rank, " | ".join(record.text or "" for record in records[:3])


class EmbeddingGuard:
    """Stops and restarts the embedding backend under test.

    Both shapes are supported.  A bare llama-server owned by the manual demo
    stack is identified by its pid file and signalled directly.  The
    spacemit-ai-gateway Embed domain is identified by its /v1/embed base URL and
    is toggled through the Gateway API: the equivalent of stopping the server is
    unloading the model, which is also what a device-wide outage looks like to
    the daemon.
    """

    def __init__(self, pid_file: Path, embedding_url: str) -> None:
        self.pid_file = pid_file
        self.embedding_url = embedding_url
        self.command: list[str] = []
        self.gateway_url = (
            embedding_url.rstrip("/")
            if EMBED_GATEWAY_MARKER in embedding_url
            else None
        )

    def _gateway_call(self, method: str, path: str, payload: dict | None = None) -> bool:
        assert self.gateway_url is not None
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.gateway_url}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return response.status == 200
        except OSError:
            return False

    def _gateway_model_loaded(self) -> bool | None:
        """None when the model's state cannot be read."""
        assert self.gateway_url is not None
        try:
            with urllib.request.urlopen(f"{self.gateway_url}/models", timeout=10) as response:
                models = json.loads(response.read())
        except (OSError, ValueError):
            return None
        for entry in models:
            if entry.get("id") == MODEL_ID:
                return entry.get("status") == "loaded"
        return None

    def stop(self) -> bool:
        if self.gateway_url:
            if not self._gateway_call("POST", "/models/unload", {"model": MODEL_ID}):
                return False
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if self._gateway_model_loaded() is False:
                    return True
                time.sleep(0.5)
            return self._gateway_model_loaded() is False
        if not self.pid_file.exists():
            return False
        try:
            pid = int(self.pid_file.read_text(encoding="utf-8").strip())
        except ValueError:
            return False
        try:
            self.command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            self.command = [
                part.decode("utf-8", "replace") for part in self.command if part
            ]
        except OSError:
            self.command = []
        try:
            subprocess.run(["kill", "-TERM", str(pid)], check=False)
            for _ in range(100):
                try:
                    Path(f"/proc/{pid}/stat").stat()
                    time.sleep(0.05)
                except OSError:
                    break
        except OSError:
            return False
        return not embedding_healthy(self.embedding_url)

    def restart(self) -> bool:
        if self.gateway_url:
            if not self._gateway_call(
                "POST",
                "/models/load",
                {"model": MODEL_ID, "extra_args": EMBED_EXTRA_ARGS},
            ):
                return False
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                if self._gateway_model_loaded() is True:
                    return True
                time.sleep(1.0)
            return False
        if not self.command:
            return False
        subprocess.Popen(
            self.command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            if embedding_healthy(self.embedding_url):
                return True
            time.sleep(1.0)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:9898")
    parser.add_argument(
        "--socket",
        default="/home/example/.local/state/bianbu-agent-memory-manual/agent-memoryd.sock",
        type=Path,
    )
    parser.add_argument("--embedding-url", default="http://127.0.0.1:18790/v1/embed")
    parser.add_argument(
        "--embedding-pid-file",
        default="/home/example/.local/state/bianbu-agent-memory-manual/embedding.pid",
        type=Path,
    )
    parser.add_argument(
        "--registry",
        default=Path("/home/example/e2e-runtime/data/runtime/memory-registry"),
        type=Path,
    )
    parser.add_argument("--agent-id", default=None,
                        help="skip discovery and use this Runtime agent id")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--skip-fallback", action="store_true",
                        help="do not stop the embedding backend (e.g. when shared)")
    args = parser.parse_args()

    if not embedding_healthy(args.embedding_url):
        print(f"embedding server is not healthy: {args.embedding_url}", file=sys.stderr)
        return 2

    agent_id, other_agents = (
        (args.agent_id, [])
        if args.agent_id
        else discover_agent(args.base_url, args.timeout, args.registry)
    )
    marker = MARKERS[int(time.time()) % len(MARKERS)]
    write_prompt = f"你好，请记住，我的终端和编辑器的配色偏好是{marker}。"
    report: dict[str, Any] = {
        "status": "K3_SEMANTIC_E2E_RUNTIME",
        "spec": "MEM-NFR-02",
        "agent_id": agent_id,
        "other_discovered_agents": other_agents,
        "marker": marker,
        "write_prompt": write_prompt,
        "reworded_query": REWORDED_QUERY,
    }
    print(f"agent: {agent_id} (others: {other_agents})", file=sys.stderr)

    # 1. Enable embedding on the daemon (dev stack shares one namespace per
    #    uid, so this script's SDK client sees the Runtime plugin's writes).
    with MemoryClient.connect(args.socket, timeout=30.0) as client:
        client.privacy.update_settings(
            embedding=embedding_patch(args.embedding_url, True)
        )

    # 2. Session A writes the preference through the Runtime.
    session_a = create_session(
        args.base_url, agent_id, f"semantic-e2e-write-{int(time.time())}", args.timeout
    )
    write_run = run_chat(args.base_url, session_a, write_prompt, args.timeout)
    report["write_run"] = {"session_id": session_a, "status": write_run["status"]}

    # 3. Wait until the memory (and its embedding) has landed.
    deadline = time.monotonic() + 60
    rank = 0
    while time.monotonic() < deadline:
        rank, _ = search_rank(args.socket, LEXICAL_QUERY, marker)
        if rank:
            break
        time.sleep(1.0)
    if not rank:
        print(f"write never landed: marker={marker}", file=sys.stderr)
        report["overall_pass"] = False
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    report["write_landed_lexical_rank"] = rank

    # 4a. Disable embedding: the reworded query must NOT retrieve the memory
    #     (lexical cannot bridge 配色偏好 -> 界面颜色主题).
    with MemoryClient.connect(args.socket, timeout=30.0) as client:
        client.privacy.update_settings(
            embedding=embedding_patch(args.embedding_url, False)
        )
    fts_rank, fts_top = search_rank(args.socket, REWORDED_QUERY, marker)
    report["fts_reworded_rank"] = fts_rank
    report["fts_reworded_top"] = fts_top
    if fts_rank != 0:
        print(
            f"FTS unexpectedly retrieved the memory at rank {fts_rank}; "
            "the reworded query is not lexically disjoint — pick new wording",
            file=sys.stderr,
        )

    # 4b. Re-enable embedding: the reworded query must rank it first.
    with MemoryClient.connect(args.socket, timeout=30.0) as client:
        client.privacy.update_settings(
            embedding=embedding_patch(args.embedding_url, True)
        )
    semantic_rank, semantic_top = search_rank(args.socket, REWORDED_QUERY, marker)
    report["semantic_reworded_rank"] = semantic_rank
    report["semantic_reworded_top"] = semantic_top
    semantic_pass = semantic_rank == 1
    report["semantic_pass"] = semantic_pass

    # 5. Session B asks the reworded question through the Runtime.
    session_b = create_session(
        args.base_url, agent_id, f"semantic-e2e-recall-{int(time.time())}", args.timeout
    )
    recall_run = run_chat(args.base_url, session_b, REWORDED_QUERY, args.timeout)
    recall_text = assistant_content(recall_run)
    report["runtime_recall"] = {
        "session_id": session_b,
        "reply": recall_text,
        "contains_marker": marker in recall_text,
    }
    runtime_soft_pass = marker in recall_text
    report["runtime_soft_pass"] = runtime_soft_pass

    # 6. Fallback: kill llama-server, ask a lexical question in session C.
    fallback_pass = True
    if not args.skip_fallback:
        guard = EmbeddingGuard(args.embedding_pid_file, args.embedding_url)
        stopped = guard.stop()
        if not stopped:
            print(
                "warning: could not stop llama-server (no managed pid file); "
                "skipping the fallback phase",
                file=sys.stderr,
            )
            report["fallback"] = {"skipped": "llama-server not owned by this stack"}
        else:
            session_c = create_session(
                args.base_url, agent_id, f"semantic-e2e-fallback-{int(time.time())}",
                args.timeout,
            )
            fallback_run = run_chat(args.base_url, session_c, LEXICAL_QUERY, args.timeout)
            fallback_text = assistant_content(fallback_run)
            fallback_pass = marker in fallback_text
            report["fallback"] = {
                "session_id": session_c,
                "reply": fallback_text,
                "contains_marker": fallback_pass,
                "restarted": guard.restart(),
            }
            report["fallback_pass"] = fallback_pass

    report["overall_pass"] = (
        semantic_pass and fallback_pass
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    print(payload)
    print(
        f"\n== MEM-NFR-02 e2e ==\n"
        f"semantic top-1 (reworded):  {'PASS' if semantic_pass else 'FAIL'} "
        f"(rank={semantic_rank}, fts rank={fts_rank})\n"
        f"Runtime reply contains marker: {'PASS' if runtime_soft_pass else 'SOFT-FAIL'}\n"
        f"FTS fallback after llama-server stop: "
        f"{'PASS' if fallback_pass else 'FAIL'}\n"
        f"overall: {'PASS' if report['overall_pass'] else 'FAIL'}",
        file=sys.stderr,
    )
    return 0 if report["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
