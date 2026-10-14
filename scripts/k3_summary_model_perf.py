#!/usr/bin/env python3
"""Summary-model performance acceptance for MEM-L2 on K3.

Checks the summary path against the product requirements:

  R1  every 5 completed turns a summary is generated automatically
      (需求清单 MEM-L2 / PRD V0.91 存储设计表);
  R2  local write ACK <=100ms and does not wait for summary work
      (统一需求 §9: 不等待摘要、向量派生或云端) — measured on the
      shipped default (rules-only, model disabled);
  R3  optional model enhancement: latency versus the configured budget
      (max_latency_ms) and its impact on the triggering append —
      informational, the PRD sets no summary latency figure;
  R4  model unavailable -> degrade to the rules summary, source events
      preserved, degraded_reason=model_unavailable (统一需求 §降级表).

Method: isolated daemon over a temp DB (production stack untouched).
Phase A rules-only; phase B a real local OpenAI-compatible endpoint
(llama-server); phase C broken endpoints (fast refuse + slow blackhole).

Run from the repo root on K3:

    env PYTHONPATH=sdk/python/src python3 scripts/k3_summary_model_perf.py \
        --endpoint http://127.0.0.1:18081/v1
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from k3_perf_acceptance import Daemon, describe  # noqa: E402

from bianbu_agent_sdk.memory import MemoryClient  # noqa: E402

TURN_TEXTS = [
    "帮我把终端主题换成深色，我晚上写代码比较多",
    "好，已将终端主题设置为深色，下次打开保持生效",
    "决定采用 uv 管理这个项目的依赖，不用 pip 了",
    "已完成：pyproject.toml 迁移到 uv，锁定文件已生成",
    "待办：还要补充 K3 上的回归测试，稍后提醒我",
    "请记住，我喜欢简洁的 commit message，不要加多余前缀",
    "我习惯用 Neovim 写代码，配置文件在 ~/.config/nvim",
    "刚才那个构建报错是缺 openssl 头文件，装上就好了",
    "需要完成 CI 里的 riscv64 交叉编译配置",
    "第一个方案更合适，就用它吧，性能差距明显",
]


def append(client: MemoryClient, session: str, event_type: str, payload: dict) -> float:
    before = time.perf_counter_ns()
    client.sessions.append_event(session_id=session, event_type=event_type, payload=payload)
    return (time.perf_counter_ns() - before) / 1_000_000


def add_pair(client: MemoryClient, session: str, index: int) -> tuple[float, float]:
    # completed_turns() pairs a user_message with the assistant_message that
    # shares its turn_id; payloads without a turn/run id never form a turn.
    turn_id = f"turn-{index}"
    user_ms = append(client, session, "user_message", {
        "turn_id": turn_id, "text": TURN_TEXTS[index % len(TURN_TEXTS)],
    })
    reply = TURN_TEXTS[(index + 3) % len(TURN_TEXTS)]
    assistant_ms = append(client, session, "assistant_message", {
        "turn_id": turn_id, "text": reply,
    })
    return user_ms, assistant_ms


def tool_event(client: MemoryClient, session: str) -> float:
    return append(client, session, "tool_call", {
        "turn_id": "turn-tool", "status": "completed",
        "tool": "bash", "command": "cargo test --workspace",
        "files": ["crates/memory-store/src/lib.rs"],
    })


def enable_model(client: MemoryClient, base_url: str, model: str, max_latency_ms: int) -> None:
    client.privacy.update_settings(summary_model={
        "enabled": True,
        "base_url": base_url,
        "model": model,
        "auth": "none",
        "max_output_tokens": 512,
        "max_latency_ms": max_latency_ms,
    })


def disable_model(client: MemoryClient) -> None:
    client.privacy.update_settings(summary_model={"enabled": False})


class Blackhole:
    """Accepts TCP connections and never responds (drives the read timeout)."""

    def __init__(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self.port = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        sockets = [self._listener]
        while not self._stop.is_set():
            try:
                readable, _, _ = select_sockets(sockets, timeout=0.2)
            except OSError:
                return
            for ready in readable:
                try:
                    ready.accept()
                except OSError:
                    return

    def close(self) -> None:
        self._stop.set()
        self._listener.close()


def select_sockets(sockets: list, timeout: float):
    import select

    return select.select(sockets, [], [], timeout)


def endpoint_alive(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/models", timeout=3) as response:
            return response.status == 200
    except OSError:
        return False


def phase_a(client: MemoryClient, report: dict) -> None:
    print("== Phase A: rules-only (model disabled, shipped default)")
    session = "perf-rules-a"
    non_trigger: list[float] = []
    trigger: list[float] = []
    for index in range(20):
        user_ms, assistant_ms = add_pair(client, session, index)
        non_trigger.append(user_ms)
        if (index + 1) % 5 == 0:
            trigger.append(assistant_ms)
        else:
            non_trigger.append(assistant_ms)
    report["A_non_trigger_ms"] = describe(non_trigger)
    report["A_trigger_ms"] = describe(trigger)
    print(f"non-trigger appends (n={len(non_trigger)}): {report['A_non_trigger_ms']}")
    print(f"5th-turn trigger appends (n={len(trigger)}): {report['A_trigger_ms']}")

    print("-- rules summary cost on one big 50-turn batch")
    client.privacy.update_settings(summary_interval_turns=50)
    big = "perf-rules-b"
    for index in range(49):
        add_pair(client, big, index)
    before = time.perf_counter_ns()
    _, assistant_ms = add_pair(client, big, 49)
    batch_ms = (time.perf_counter_ns() - before) / 1_000_000
    client.privacy.update_settings(summary_interval_turns=5)
    report["A_rules_50turn_batch_ms"] = round(assistant_ms, 1)
    print(f"50th-turn append incl. full 50-turn rules summary: {assistant_ms:.1f} ms "
          f"(pair total {batch_ms:.1f} ms)")

    context = client.sessions.get_context(session)
    summary = context.get("summary") or {}
    report["A_summary_producer"] = summary.get("producer")
    report["A_summary_revision"] = summary.get("revision")
    print(f"stored summary producer={summary.get('producer')} revision={summary.get('revision')}")
    report["A_gate_write_ack_100ms"] = report["A_trigger_ms"]["p95_ms"] <= 100.0
    print(f"R2 gate (trigger append p95 <=100ms, rules-only): "
          f"{'PASS' if report['A_gate_write_ack_100ms'] else 'FAIL'}")


def phase_b(client: MemoryClient, report: dict, endpoint: str, model: str) -> None:
    print("== Phase B: model enabled against a real local endpoint")
    if not endpoint_alive(endpoint):
        print(f"endpoint {endpoint} unreachable — phase B skipped")
        report["B_skipped"] = True
        return
    enable_model(client, endpoint, model, max_latency_ms=15_000)

    samples: list[float] = []
    outcomes: list[str] = []
    for index in range(6):
        session = f"perf-model-{index}"
        add_pair(client, session, index)
        before = time.perf_counter_ns()
        result = client.sessions.summarize(session, interval_turns=1)
        elapsed = (time.perf_counter_ns() - before) / 1_000_000
        samples.append(elapsed)
        state = result.get("state")
        producer = result.get("producer")
        degraded = result.get("degraded_reason")
        outcomes.append(f"state={state} producer={producer} degraded={degraded}")
        print(f"sample {index}: {elapsed:.0f} ms — state={state} producer={producer} "
              f"degraded={degraded}")
    report["B_direct_summarize_ms"] = describe(samples)
    report["B_outcomes"] = outcomes
    print(f"direct summarize latency: {report['B_direct_summarize_ms']}")

    print("-- triggering append with model enabled (write-ack impact)")
    session = "perf-model-append"
    for index in range(4):
        add_pair(client, session, index)
    _, trigger_ms = add_pair(client, session, 4)
    context = client.sessions.get_context(session)
    summary = context.get("summary") or {}
    report["B_trigger_append_ms"] = round(trigger_ms, 1)
    report["B_trigger_summary_producer"] = summary.get("producer")
    report["B_trigger_summary_model_ref"] = summary.get("model_ref")
    print(f"5th assistant append incl. rules+model: {trigger_ms:.0f} ms; stored "
          f"producer={summary.get('producer')} model_ref={summary.get('model_ref')}")
    disable_model(client)


def phase_c(client: MemoryClient, report: dict) -> None:
    print("== Phase C: model unavailable -> rules summary intact")
    # Fast refusal: closed local port (connection refused, well under budget).
    enable_model(client, "http://127.0.0.1:59999/v1", "summary", max_latency_ms=2_000)
    session = "perf-degrade-refused"
    add_pair(client, session, 0)
    before = time.perf_counter_ns()
    refused = client.sessions.summarize(session, interval_turns=1)
    refused_ms = (time.perf_counter_ns() - before) / 1_000_000
    report["C_refused_ms"] = round(refused_ms, 1)
    report["C_refused_state"] = refused.get("state")
    report["C_refused_producer"] = refused.get("producer")
    report["C_refused_degraded"] = refused.get("degraded_reason")
    print(f"refused endpoint: {refused_ms:.0f} ms — state={refused.get('state')} "
          f"producer={refused.get('producer')} degraded_reason={refused.get('degraded_reason')}")

    # Slow timeout: blackhole listener + 2s budget.
    hole = Blackhole()
    try:
        enable_model(client, f"http://127.0.0.1:{hole.port}/v1", "summary", max_latency_ms=2_000)
        session = "perf-degrade-timeout"
        add_pair(client, session, 1)
        before = time.perf_counter_ns()
        timed_out = client.sessions.summarize(session, interval_turns=1)
        timeout_ms = (time.perf_counter_ns() - before) / 1_000_000
    finally:
        hole.close()
    report["C_timeout_ms"] = round(timeout_ms, 1)
    report["C_timeout_state"] = timed_out.get("state")
    report["C_timeout_producer"] = timed_out.get("producer")
    report["C_timeout_degraded"] = timed_out.get("degraded_reason")
    print(f"blackhole endpoint: {timeout_ms:.0f} ms — state={timed_out.get('state')} "
          f"producer={timed_out.get('producer')} degraded_reason={timed_out.get('degraded_reason')}")

    history = client.sessions.get_history(session)
    events = history.get("events", history.get("items", []))
    report["C_events_preserved"] = len(events)
    context = client.sessions.get_context(session)
    summary = context.get("summary") or {}
    report["C_rules_summary_revision"] = summary.get("revision")
    print(f"after degrade: events={len(events)} summary revision={summary.get('revision')} "
          f"producer={summary.get('producer')}")
    disable_model(client)
    report["C_gate"] = (
        refused.get("degraded_reason") == "model_unavailable"
        and refused.get("producer") == "memoryd.rules"
        and timed_out.get("degraded_reason") == "model_unavailable"
        and timed_out.get("producer") == "memoryd.rules"
        and len(events) >= 2
    )
    print(f"R4 gate (degrade keeps rules summary + events): "
          f"{'PASS' if report['C_gate'] else 'FAIL'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--memoryd", default="target/release/bianbu-agent-memoryd", type=Path)
    parser.add_argument("--root", default=Path("/tmp/k3-summary-perf"), type=Path)
    parser.add_argument("--endpoint", default="http://127.0.0.1:18081/v1")
    parser.add_argument("--model", default="qwen2.5-1.5b-instruct-q4_k_m")
    args = parser.parse_args()

    args.root.mkdir(parents=True, exist_ok=True)
    daemon = Daemon(args.memoryd, args.root)
    socket_path = daemon.start()
    report: dict = {"endpoint": args.endpoint, "model": args.model}
    try:
        with MemoryClient.connect(socket_path, timeout=180.0) as client:
            phase_a(client, report)
            phase_b(client, report, args.endpoint, args.model)
            phase_c(client, report)
    finally:
        daemon.stop()

    out = args.root / "summary_perf_report.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"report written to {out}")
    gates = {
        "R1_rules_auto_summary": report.get("A_summary_producer") == "memoryd.rules"
        and report.get("A_summary_revision", 0) >= 1,
        "R2_write_ack_100ms_rules_only": report.get("A_gate_write_ack_100ms"),
        "R4_degrade": report.get("C_gate"),
    }
    print("== gates:", json.dumps(gates, ensure_ascii=False))
    return 0 if all(gates.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
