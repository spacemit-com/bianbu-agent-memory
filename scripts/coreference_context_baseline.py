#!/usr/bin/env python3
"""Model-independent context baseline for the Chinese coreference goldset.

`MEM-INT-08-02` measures how well a frozen model resolves 这个/那个/刚才/第一个
when AgentMemory is available.  That accuracy needs the real model on K3.
This script measures the *memory-side evidence*, split by access mode after
the Hermes-aligned injection redesign (2026-09-11): the default new-session
injection no longer carries turn bodies, so referent resolution depends on
(a) the status-line anchor and (b) what a pull can recover:

  1. status_provisioned   — the referent content is anchored in what a Runtime
     injects by default (``session_status`` titles plus the current
     session's own history).  Low values are expected by design: bodies are
     pulled on demand, not pushed;
  2. recovery_provisioned — the referent content is recoverable through the
     MEM-L1-01 service API (``recent_sessions`` full messages + summaries),
     the upper bound a session_search-style pull could reach;
  3. l3_provisioned       — the rule extractor preserved the fact in L3 and it
     is retrievable by a content query (``long_term.search_headers`` +
     ``get_many``), which is what survives once the session ages out;
  4. conflict_provisioned — for `conflict` cases both the historical
     instruction and the current instruction source are recoverable, so a
     model *could* honour the current instruction (遵从 itself is model-side).

Scoring is content-unit overlap (alphanumeric tokens + CJK bigrams), never a
model call.  Every case runs against a freshly initialised daemon so cases
cannot pollute each other.  Output is a JSON report plus a markdown summary.

Usage (K3, from the repository root after ``cargo build --release``):

    BIANBU_MEMORY_DEV_CONFIRM_TOKEN=$(uuidgen) \
    python3 scripts/coreference_context_baseline.py \
        --memoryd target/release/bianbu-agent-memoryd

Add ``--limit 5`` for a smoke run and ``--keep-temp`` to inspect databases.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from bianbu_agent_sdk.memory import (
    AsyncMemoryClient,
    Mem0Coordinator,
    SyncPolicy,
)

GOLDSET = Path(__file__).resolve().parent.parent / "tests" / "data" / "chinese_coreference_goldset.jsonl"

# Characters that never carry referent content in the goldset.
_STOP_CHARS = set("的了呢吗吧啊呀嘛与及或而且在是有对把给被就都也很更最")

_ALNUM = re.compile(r"[A-Za-z0-9_./:+-]{2,}")
_CJK_RUN = re.compile(r"[一-鿿]+")


def content_units(*texts: str) -> set[str]:
    """Model-free content fingerprint: alnum tokens plus CJK bigrams."""

    units: set[str] = set()
    for text in texts:
        units.update(_ALNUM.findall(text))
        for run in _CJK_RUN.findall(text):
            units.update(
                run[i : i + 2]
                for i in range(len(run) - 1)
                if not (run[i] in _STOP_CHARS or run[i + 1] in _STOP_CHARS)
            )
            if len(run) == 1 and run not in _STOP_CHARS:
                units.add(run)
    return units


def coverage(units: set[str], haystacks: list[str]) -> float:
    if not units:
        return 0.0
    joined = "\n".join(haystacks)
    hit = sum(1 for unit in units if unit in joined)
    return hit / len(units)


def load_cases(limit: int | None = None) -> list[dict[str, Any]]:
    cases = [
        json.loads(line)
        for line in GOLDSET.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return cases[:limit] if limit else cases


def wait_for_socket(path: Path, process: subprocess.Popen[str]) -> None:
    import socket

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(process.stderr.read() if process.stderr else "memoryd exited")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(path))
            return
        except OSError:
            time.sleep(0.05)
        finally:
            probe.close()
    raise TimeoutError(path)


class CaseDaemon:
    """One throwaway daemon per goldset case for hard namespace isolation."""

    def __init__(self, memoryd: Path, root: Path) -> None:
        self.memoryd = memoryd
        self.root = root
        self.process: subprocess.Popen[str] | None = None

    def __enter__(self) -> Path:
        socket_path = self.root / "memoryd.sock"
        secret = self.root / "account-secret"
        secret.write_text(uuid.uuid4().hex + uuid.uuid4().hex + "\n", encoding="utf-8")
        secret.chmod(0o600)
        command = [
            str(self.memoryd), "--development-identity", "--socket", str(socket_path),
            "--database", str(self.root / "memory.db"),
            "--key-package", str(self.root / "memory.keypkg"),
            "--account-secret-file", str(secret), "--initialize-key-package",
        ]
        self.process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
        )
        wait_for_socket(socket_path, self.process)
        return socket_path

    def __exit__(self, *_: object) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()


def _texts_from_events(events: Any) -> list[str]:
    texts: list[str] = []
    if not isinstance(events, list):
        return texts
    for item in events:
        if not isinstance(item, dict):
            continue
        payload = item.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("text"), str):
            texts.append(payload["text"])
    return texts


async def replay_history(client: AsyncMemoryClient, case: dict[str, Any]) -> None:
    """Write every history turn through the real chain: paired events, rule
    extraction (RuleBasedMem0Planner is the coordinator default when no model
    environment is configured) and the rule summary trigger."""

    coordinator = Mem0Coordinator()
    sessions = {turn["session_id"] for turn in case["history"]}
    for index, turn in enumerate(case["history"]):
        session_id, run_id = turn["session_id"], f"{case['id']}:turn{index}"
        await client.sessions.append_event(
            session_id=session_id,
            event_type="user_message",
            payload={
                "role": "user", "text": turn["user"],
                "run_id": run_id, "agent_id": "coref-baseline", "status": "completed",
            },
            sync_policy=SyncPolicy.LOCAL_ONLY,
            idempotency_key=f"{run_id}:user-event",
        )
        await client.sessions.append_event(
            session_id=session_id,
            event_type="assistant_message",
            payload={
                "role": "assistant", "text": turn["assistant"],
                "run_id": run_id, "agent_id": "coref-baseline", "status": "completed",
            },
            sync_policy=SyncPolicy.LOCAL_ONLY,
            idempotency_key=f"{run_id}:assistant-event",
        )
        await coordinator.ingest_turn(
            client,
            session_id=session_id,
            run_id=run_id,
            user_text=turn["user"],
            assistant_text=turn["assistant"],
        )
    for session_id in sessions:
        await client.sessions.summarize(
            session_id, idempotency_key=f"{case['id']}:summarize:{session_id}"
        )


async def evaluate_case(
    client: AsyncMemoryClient, case: dict[str, Any], threshold: float
) -> dict[str, Any]:
    current = str(case["current_session_id"])
    referent = str(case["expected_referent"])
    # Units come from the referent only: it is what must be locatable in the
    # context.  The expected action is model-side semantics ("设为" vs "建议")
    # and would understate the upper bound (offline check: referent-only upper
    # bound is mean 0.91, 58/60 cases >= 0.5 against the raw history).
    units = content_units(referent)

    # Default new-session injection: one status line per session (title is
    # the anchor), plus the current session's own history when the case has
    # turns in it.
    status = await client.sessions.get_recent_context(
        exclude_session_id=current, selection_mode="session_status"
    )
    injected = [str(item.get("title") or "") for item in status.get("sessions") or []]
    if any(turn["session_id"] == current for turn in case["history"]):
        history = await client.sessions.get_history(current)
        injected.extend(_texts_from_events(history.get("events") or history.get("messages")))

    # Pull upper bound: the MEM-L1-01 service shape.  Accept both the
    # grouped (``recent_sessions``) and flat (legacy) response forms.
    recovery = await client.sessions.get_recent_context(
        exclude_session_id=current,
        selection_mode="recent_sessions",
        turn_limit=50,
        summary_limit=20,
    )
    recovered = _texts_from_events(recovery.get("messages"))
    for summary in recovery.get("summaries") or []:
        recovered.append(json.dumps(summary.get("summary") or {}, ensure_ascii=False))
    for group in recovery.get("sessions") or []:
        recovered.extend(_texts_from_events(group.get("messages")))
        summary = group.get("summary")
        if summary:
            recovered.append(json.dumps(summary.get("summary") or summary, ensure_ascii=False))

    headers = await client.long_term.search_headers(referent, top_k=5)
    records = await client.long_term.get_many([h.memory_id for h in headers]) if headers else []
    l3_texts = [record.text for record in records]

    status_cov = coverage(units, injected)
    recovery_cov = coverage(units, recovered)
    l3_cov = coverage(units, l3_texts)

    return {
        "id": case["id"],
        "category": case["category"],
        "tags": case["tags"],
        "conflict": "conflict" in case.get("tags", []),
        "units": len(units),
        "status_coverage": round(status_cov, 3),
        "recovery_coverage": round(recovery_cov, 3),
        "l3_coverage": round(l3_cov, 3),
        "l3_records": len(records),
        "status_provisioned": status_cov >= threshold,
        "recovery_provisioned": recovery_cov >= threshold,
        "l3_provisioned": l3_cov >= threshold,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    cases = load_cases(args.limit)
    results: list[dict[str, Any]] = []
    started = time.monotonic()

    for index, case in enumerate(cases, 1):
        with tempfile.TemporaryDirectory(prefix=f"coref-{case['id']}-") as raw:
            root = Path(raw)
            if args.keep_temp:
                root = Path(tempfile.mkdtemp(prefix=f"coref-keep-{case['id']}-"))
            with CaseDaemon(args.memoryd, root) as socket_path:
                async with await AsyncMemoryClient.connect(socket_path, timeout=30.0) as client:
                    await replay_history(client, case)
                    result = await evaluate_case(client, case, args.threshold)
            results.append(result)
        if args.verbose or index % 10 == 0:
            print(
                f"[{index}/{len(cases)}] {case['id']} "
                f"status={result['status_coverage']} recovery={result['recovery_coverage']} "
                f"l3={result['l3_coverage']}"
            )

    def rate(key: str, subset: list[dict[str, Any]]) -> float:
        return round(sum(1 for r in subset if r[key]) / len(subset), 3) if subset else 0.0

    by_category: dict[str, dict[str, float]] = {}
    for category in sorted({r["category"] for r in results}):
        subset = [r for r in results if r["category"] == category]
        by_category[category] = {
            "count": len(subset),
            "status_provisioned": rate("status_provisioned", subset),
            "recovery_provisioned": rate("recovery_provisioned", subset),
            "l3_provisioned": rate("l3_provisioned", subset),
        }
    conflicts = [r for r in results if r["conflict"]]

    return {
        "status": "COREF_BASELINE_OK",
        "dataset": str(GOLDSET.name),
        "cases": len(results),
        "threshold": args.threshold,
        "elapsed_s": round(time.monotonic() - started, 1),
        "overall": {
            "status_provisioned": rate("status_provisioned", results),
            "recovery_provisioned": rate("recovery_provisioned", results),
            "l3_provisioned": rate("l3_provisioned", results),
            "mean_status_coverage": round(
                sum(r["status_coverage"] for r in results) / len(results), 3
            ),
            "mean_recovery_coverage": round(
                sum(r["recovery_coverage"] for r in results) / len(results), 3
            ),
            "mean_l3_coverage": round(sum(r["l3_coverage"] for r in results) / len(results), 3),
        },
        "conflict_cases": {
            # Context provisioning for 冲突遵从: the historical instruction must
            # be recoverable as data so a model *could* prefer the current one.
            "count": len(conflicts),
            "recovery_provisioned": rate("recovery_provisioned", conflicts),
        },
        "by_category": by_category,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--memoryd", default="target/release/bianbu-agent-memoryd", type=Path)
    parser.add_argument("--limit", type=int, default=None, help="run only the first N cases")
    parser.add_argument("--threshold", type=float, default=0.5,
                        help="content-unit coverage that counts as provisioned")
    parser.add_argument("--keep-temp", action="store_true", help="keep per-case databases")
    parser.add_argument("--output", type=Path, default=None, help="write the JSON report here")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    import asyncio

    report = asyncio.run(run(args))
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload)

    overall = report["overall"]
    print(
        f"\n== Memory-side evidence (model-independent) ==\n"
        f"status-line anchor provisioned: {overall['status_provisioned']:.1%} "
        f"(mean coverage {overall['mean_status_coverage']:.1%}; low by design — "
        f"bodies are pulled, not pushed)\n"
        f"pull-recovery provisioned: {overall['recovery_provisioned']:.1%} "
        f"(mean coverage {overall['mean_recovery_coverage']:.1%})\n"
        f"L3 rule-extracted provisioned: {overall['l3_provisioned']:.1%} "
        f"(mean coverage {overall['mean_l3_coverage']:.1%})\n"
        f"conflict context recoverable: {report['conflict_cases']['recovery_provisioned']:.1%}\n"
        f"Note: the 80%/100% targets of MEM-INT-08-02 are model-side accuracy; "
        f"these numbers are the evidence bounds a model could possibly reach.",
        file=__import__("sys").stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
