#!/usr/bin/env python3
"""Exercise the real Hermes loader/manager against an isolated installed memoryd.

Run with the Hermes Python interpreter; requires its dependencies and the Bianbu SDK.
No model requests, production service changes, or production data writes.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-source", required=True, type=Path)
    parser.add_argument("--memoryd", default="/usr/libexec/bianbu-agent-memoryd")
    parser.add_argument("--sdk-path", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--model-provider", default="custom")
    parser.add_argument("--model", default="integration-no-inference")
    parser.add_argument("--expected-model", help="Expected Hermes-normalized model; defaults to --model")
    parser.add_argument("--model-base-url", default="http://127.0.0.1:9/v1")
    args = parser.parse_args()
    source = args.hermes_source.resolve()
    revision = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    tag_revision = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "v0.21.7^{commit}"], text=True
    ).strip()
    assert revision == tag_revision, "Hermes must be pinned to v0.21.7"
    sys.path.insert(0, str(source))
    if args.sdk_path:
        sys.path.insert(0, str(args.sdk_path.resolve()))
    with tempfile.TemporaryDirectory(prefix="hermes-bianbu-acceptance-") as raw:
        root = Path(raw)
        home = root / "hermes"
        plugin = home / "plugins" / "bianbu"
        plugin.mkdir(parents=True)
        shutil.copyfile(
            Path(__file__).resolve().parents[1] / "adapters/hermes/__init__.py",
            plugin / "__init__.py",
        )
        (home / "config.yaml").write_text(
            f"memory:\n  provider: bianbu\nmodel:\n  default: {args.model}\n"
            f"  provider: {args.model_provider}\n  base_url: {args.model_base_url}\n",
            encoding="utf-8",
        )
        secret = root / "account-secret"
        secret.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")
        secret.chmod(0o600)
        socket = root / "memoryd.sock"
        os.environ["HERMES_HOME"] = str(home)
        os.environ["BIANBU_AGENT_MEMORY_SOCKET"] = str(socket)
        environment = dict(os.environ, AGENT_MEMORY_EMBEDDING_DISABLED="1")
        with (root / "memoryd.log").open("w+") as log:
            daemon = subprocess.Popen([
                args.memoryd, "--development-identity",
                "--development-agent-name", "hermes-v0217-acceptance",
                "--socket", str(socket), "--database", str(root / "memory.db"),
                "--key-package", str(root / "memory.keypkg"),
                "--account-secret-file", str(secret), "--initialize-key-package",
            ], env=environment, stdout=log, stderr=log)
            manager = None
            try:
                deadline = time.monotonic() + 15
                while not socket.exists():
                    if daemon.poll() is not None or time.monotonic() > deadline:
                        log.seek(0)
                        raise RuntimeError(log.read())
                    time.sleep(0.05)
                from plugins.memory import load_memory_provider, list_memory_provider_names
                from agent.memory_manager import MemoryManager
                from bianbu_agent_sdk.memory import MemoryClient

                assert "bianbu" in list_memory_provider_names()
                provider = load_memory_provider("bianbu")
                assert provider is not None and provider.is_available()
                from unittest.mock import patch
                failed = load_memory_provider("bianbu")
                with patch("bianbu_agent_sdk.memory.runtime.MemoryBridge", side_effect=RuntimeError("test startup failure")):
                    try:
                        failed.initialize("startup-failure", agent_context="primary")
                    except RuntimeError:
                        assert failed._startup_error is not None and failed._client is None
                        assert failed._worker is None
                    else:
                        raise AssertionError("worker startup failure was hidden")
                session = "hermes-v0217-session-a"
                provider.initialize(session, agent_context="primary", hermes_home=str(home))
                manager = MemoryManager()
                manager.add_provider(provider)

                def drain() -> None:
                    assert manager.flush_pending(timeout=10)
                    deadline = time.monotonic() + 10
                    while provider._jobs.unfinished_tasks:
                        assert time.monotonic() < deadline, "provider queue did not drain"
                        time.sleep(0.02)

                text = "hermes0217 preference: use Chinese responses and short paragraphs"
                manager.on_memory_write("add", "user", text, {"session_id": session})
                manager.sync_all("remember hermes0217 preference", "saved", session_id=session,
                                 messages=[{"role": "user"}, {"role": "assistant"}])
                drain()
                client = MemoryClient.connect(str(socket))
                try:
                    headers = client.long_term.search_headers("hermes0217", top_k=10)
                    records = client.long_term.get_many([h.memory_id for h in headers])
                    original = next(r for r in records if r.text == text)
                    assert "personal-agent" in original.tags and "user" in original.tags
                    assert text in manager.prefetch_all("解释编译器", session_id=session)
                    manager.on_memory_write("add", "user", text, {"session_id": session})
                    drain()
                    assert len([r for r in client.long_term.get_many([
                        h.memory_id for h in client.long_term.search_headers("hermes0217", top_k=20)
                    ]) if r.text == text]) == 1
                    manager.on_memory_write("add", "invalid-target", "must reject", {"session_id": session})
                    drain()
                    assert not client.long_term.search_headers("must reject", top_k=5)
                    from bianbu_agent_sdk.memory.temporal import now_local
                    from datetime import timedelta
                    date = (now_local().date() + timedelta(days=1)).isoformat()
                    manager.on_memory_write("add", "memory", "请记住明天早上9点日期验收518会议，晚上7点瑜伽", {"session_id":session})
                    drain()
                    dated = client.long_term.get_many([h.memory_id for h in client.long_term.search_headers("日期验收518",top_k=10)])
                    assert any(date in r.text and "早上9点" in r.text and "晚上7点" in r.text for r in dated)
                    # No prior queue_prefetch: current-query recall must work.
                    recalled = manager.prefetch_all("hermes0217 Chinese", session_id=session)
                    assert text in recalled and "untrusted-recalled-content" in recalled
                    assert text not in manager.build_system_prompt()
                    replacement = "hermes0217 preference: use Chinese responses and detailed paragraphs"
                    manager.on_memory_write("replace", "user", replacement, {
                        "old_text": "short paragraphs", "previous_content": text, "session_id": session,
                    })
                    drain()
                    record = client.long_term.get_many([original.memory_id])[0]
                    assert record.text == replacement and record.revision > original.revision
                    refreshed = manager.prefetch_all("hermes0217 Chinese", session_id=session)
                    assert replacement in refreshed and text not in refreshed
                    manager.on_memory_write("replace", "user", "must not replace", {
                        "old_text": replacement, "session_id": session,
                    })
                    drain()
                    assert client.long_term.get_many([original.memory_id])[0].text == replacement
                    manager.on_memory_write("remove", "user", "", {
                        "previous_content": replacement, "session_id": session,
                    })
                    drain()
                    assert client.long_term.get_many([original.memory_id])[0].text == replacement
                    manager.on_pre_compress([{"role": "user", "content": "remember preference"}])
                    drain()
                    context = client.sessions.get_context(session, include_events=True)
                    assert context["summary"]["revision"] >= 1
                    assert any(e["payload"].get("operation") == "curated_memory_remove_requires_confirm"
                               for e in context["events"])
                    manager.on_session_switch("hermes-v0217-session-b", reset=True)
                    manager.sync_all("new session", "ok", session_id="hermes-v0217-session-b",
                                     messages=[{"role": "user"}, {"role": "assistant"}])
                    drain()
                    switched = client.sessions.get_context("hermes-v0217-session-b", include_events=True)
                    assert [e["event_type"] for e in switched["events"]
                            if e["event_type"] in {"user_message", "assistant_message"}] == [
                                "user_message", "assistant_message"]
                    # A normal turn must use Runtime's automatic extraction,
                    # without any Hermes memory tool notification.
                    natural = "我是百岁太奶，年轻人的AI用词不太懂～记住和我说要通俗易懂."
                    manager.sync_all(natural, "好的", session_id="hermes-v0217-session-b",
                                     messages=[{"role": "user"}] * 4)
                    drain()
                    automatic = client.long_term.search_headers("通俗易懂", top_k=20)
                    automatic_records = client.long_term.get_many([h.memory_id for h in automatic])
                    assert any("通俗易懂" in r.text and "mem0-derived" in r.tags
                               for r in automatic_records)
                    before = {r.memory_id for r in automatic_records}
                    manager.sync_all(natural, "好的", session_id="hermes-v0217-session-b",
                                     messages=[{"role": "user"}] * 4)
                    drain()
                    assert before == {h.memory_id for h in
                                      client.long_term.search_headers("通俗易懂", top_k=20)}
                    # Communication preference must also accompany an unrelated query.
                    assert "通俗易懂" in manager.prefetch_all("解释什么是编译器", session_id=session)
                    # Provider recreation preserves long-term data across sessions.
                    # Shutdown must drain a genuinely slow queued write and
                    # must never close the client while the worker is using it.
                    slow_done = []
                    def slow_write() -> None:
                        time.sleep(5.2)
                        provider._client.long_term.search_headers("hermes0217", top_k=1)
                        slow_done.append(True)
                    provider._run_slow_test = slow_write
                    provider._enqueue("slow_test")
                    manager.shutdown_all()
                    assert slow_done and not provider._worker
                    manager = None
                    readonly = load_memory_provider("bianbu")
                    readonly.initialize("hermes-v0217-session-c", agent_context="subagent")
                    try:
                        assert replacement in readonly.prefetch("hermes0217 preference")
                        readonly.on_memory_write("add", "user", "hermes0217 forbidden subagent write")
                        assert readonly._jobs.unfinished_tasks == 0
                    finally:
                        readonly.shutdown()
                    # Check the actual Agent initialization path, not only the
                    # provider API. No inference call is made to this URL.
                    from run_agent import AIAgent
                    agent = AIAgent(
                        base_url=args.model_base_url, api_key="local-test",
                        provider=args.model_provider, model=args.model,
                        enabled_toolsets=["memory"], quiet_mode=True,
                    )
                    try:
                        assert agent.provider == args.model_provider
                        assert agent.model == (args.expected_model or args.model)
                        assert agent.base_url.rstrip("/") == args.model_base_url.rstrip("/")
                        assert agent._memory_manager is not None
                        assert agent._memory_manager.get_provider("bianbu") is not None
                    finally:
                        agent.shutdown_memory_provider()
                    report = {
                        "status": "passed", "hermes_tag": "v0.21.7", "hermes_commit": revision,
                        "python": sys.version.split()[0], "memoryd": args.memoryd,
                        "sdk": __import__("bianbu_agent_sdk.memory", fromlist=["memory"]).__file__,
                        "checks": ["user_plugin_discovery", "real_memory_manager", "long_term_write",
                                   "current_query_recall", "untrusted_context", "replace_stable_id",
                                   "remove_requires_confirmation", "session_events", "summary_revision",
                                   "session_switch", "provider_restart_recall", "subagent_write_disabled",
                                   "actual_agent_initialization", "recall_cache_invalidation",
                                   "substring_only_replace_rejected", "runtime_automatic_extraction",
                                   "automatic_turn_idempotency", "standing_preference_recall",
                                   "explicit_runtime_tags", "explicit_write_deduplication",
                                   "invalid_target_rejected", "slow_shutdown_drain",
                                   "worker_startup_failure_visible", "relative_date_saved_absolute"],
                        "model_provider": args.model_provider, "model": args.model,
                        "effective_model": agent.model,
                        "model_base_url": args.model_base_url,
                        "model_requests": 0, "embedding": "disabled (lexical integration gate)",
                    }
                    rendered = json.dumps(report, ensure_ascii=False, indent=2)
                    if args.report:
                        args.report.write_text(rendered + "\n", encoding="utf-8")
                    print(rendered)
                finally:
                    client.close()
            finally:
                if manager is not None:
                    manager.shutdown_all()
                daemon.terminate()
                try:
                    daemon.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=5)


if __name__ == "__main__":
    main()
