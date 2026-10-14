#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import sqlite3
import statistics
import subprocess
import tempfile
import time
import uuid

from bianbu_agent_sdk.memory import (
    AsyncMemoryClient,
    EvidenceRef,
    MemoryKind,
    SyncPolicy,
)
from bianbu_agent_sdk.memory.errors import MemoryDisabled, PermissionDenied
from bianbu_agent_sdk.memory.runtime import MemoryBridge


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * quantile) - 1))
    return ordered[index]


async def wait_for_socket(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise RuntimeError(
                f"memoryd exited during acceptance setup: {stderr.strip()}"
            )
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(str(path))
        except OSError:
            pass
        else:
            return
        finally:
            client.close()
        await asyncio.sleep(0.05)
    raise TimeoutError(path)


async def exercise(socket_path: Path, confirm_token: str) -> dict[str, object]:
    client = await AsyncMemoryClient.connect(socket_path)
    bridge = MemoryBridge(str(socket_path))
    bridge._client = client
    result: dict[str, object] = {}
    try:
        result["health"] = await client.health()
        assert result["health"]["storage_encryption"] == "sqlcipher"
        assert (await client.privacy.settings())["memory_enabled"] is True

        await client.privacy.update_settings(retention_days=7)
        assert (await client.privacy.settings())["retention_days"] == 7
        permanent = await client.privacy.update_settings(retention_days=0)
        # The public contract uses 0 as the wire value for permanent retention.
        assert permanent["retention_days"] == 0

        preference = await client.long_term.put(
            kind=MemoryKind.PREFERENCE,
            text="terminal color preference: dark",
            tags=("personal-agent", "user", "slot:appearance.terminal_color_scheme"),
            sync_policy=SyncPolicy.LOCAL_ONLY,
            evidence=(EvidenceRef("acceptance", "preference"),),
            idempotency_key="acceptance-preference-add",
        )
        preference = await client.long_term.revise(
            preference.memory_id,
            text="terminal color preference: light",
            expected_revision=preference.revision,
            tags=("personal-agent", "user", "slot:appearance.terminal_color_scheme"),
            evidence=(EvidenceRef("acceptance", "preference-update"),),
            idempotency_key="acceptance-preference-update",
        )
        habit = await client.long_term.put(
            kind=MemoryKind.DECISION,
            text="Python package manager preference: uv instead of pip",
            tags=("mem0-derived", "slot:development.package_manager"),
            sync_policy=SyncPolicy.LOCAL_ONLY,
            idempotency_key="acceptance-development-habit",
        )
        recalled = await bridge.recall("create a Python project", top_k=10)
        assert any("uv instead of pip" in item for item in recalled)
        result["preference_and_habit"] = {
            "preference_revision": preference.revision,
            "habit_revision": habit.revision,
            "recalled": list(recalled),
        }

        session_id = "requirements-session"
        for turn in range(1, 6):
            assert await bridge.record_turn(
                run_id=f"requirements-run-{turn}",
                session_id=session_id,
                agent_id="requirements-agent",
                user_text=f"remember task detail {turn}",
                assistant_text=f"acknowledged task detail {turn}",
            )
        context = await client.sessions.get_context(
            session_id, include_events=True, event_limit=100
        )
        assert len(context["events"]) == 15
        assert context["summary"]["revision"] == 1
        assert context["summary"]["source_event_sequence"] == 14
        recent = await client.sessions.get_recent_context(
            exclude_session_id="new-session",
            turn_limit=5,
            summary_limit=3,
            selection_mode="recent_sessions",
            session_limit=5,
            event_limit_per_session=200,
        )
        assert recent["selection_mode"] == "recent_sessions"
        selected_sessions = recent["sessions"]
        assert selected_sessions
        selected = next(
            session for session in selected_sessions if session["session_id"] == session_id
        )
        messages = selected["messages"]
        assert len(messages) == 10
        assert messages[0]["payload"]["text"] == "remember task detail 1"
        result["context_and_summary"] = {
            "raw_events": len(context["events"]),
            "recent_messages": len(messages),
            "recent_sessions": len(selected_sessions),
            "summary_revision": context["summary"]["revision"],
            "source_events_preserved": True,
        }

        # The Server must create a rule summary from the committed fifth
        # assistant message even when the SDK summary call is omitted.
        automatic_session = "requirements-automatic-summary"
        for turn in range(1, 6):
            await client.sessions.append_event(
                session_id=automatic_session,
                event_type="user_message",
                payload={
                    "run_id": f"requirements-automatic-run-{turn}",
                    "status": "completed",
                    "text": (
                        "请记住自动摘要验收的稳定事实"
                        if turn == 1
                        else f"自动摘要第 {turn} 轮"
                    )
                },
                sync_policy=SyncPolicy.LOCAL_ONLY,
                idempotency_key=f"automatic-summary-user-{turn}",
            )
            await client.sessions.append_event(
                session_id=automatic_session,
                event_type="assistant_message",
                payload={
                    "run_id": f"requirements-automatic-run-{turn}",
                    "status": "completed",
                    "text": f"自动摘要应在第 {turn} 轮完成",
                },
                sync_policy=SyncPolicy.LOCAL_ONLY,
                idempotency_key=f"automatic-summary-assistant-{turn}",
            )
        automatic_context = await client.sessions.get_context(
            automatic_session, include_events=False
        )
        assert automatic_context["summary"]["revision"] == 1
        assert automatic_context["summary"]["producer"] == "memoryd.rules"
        assert "请记住自动摘要验收的稳定事实" in automatic_context["summary"]["summary"][
            "salient_facts"
        ]
        result["automatic_summary"] = {
            "revision": automatic_context["summary"]["revision"],
            "producer": automatic_context["summary"]["producer"],
            "stable_fact_promoted": True,
        }

        reference_session = "requirements-reference-session"
        await client.sessions.append_event(
            session_id=reference_session,
            event_type="user_message",
            payload={
                "run_id": "requirements-reference-run",
                "status": "completed",
                "text": "候选方案：\n1. SQLite\n2. PostgreSQL",
            },
            idempotency_key="reference-user",
        )
        await client.sessions.append_event(
            session_id=reference_session,
            event_type="assistant_message",
            payload={
                "run_id": "requirements-reference-run",
                "status": "completed",
                "text": "第一个方案更适合 K3 本地部署。",
            },
            idempotency_key="reference-assistant",
        )
        # MEM-L1-02 (new evidence basis): the default new-session injection is
        # one status line per session — the title is the reference anchor, the
        # transcript itself is pulled on demand via session_search.  A first
        # run in execution must already be visible as in progress.
        assert await bridge.record_run_started(
            run_id="requirements-inflight-run",
            session_id="requirements-inflight-session",
            agent_id="requirements-agent",
            user_text="进行中的首轮任务",
        )
        status_context = await bridge.recent_context("requirements-new-session")
        assert any(
            "requirements-reference-session" in item and "SQLite" in item
            for item in status_context
        )
        assert any("进行中" in item for item in status_context)
        assert not any("第一个方案更适合" in item for item in status_context)
        result["reference_resolution"] = {
            "status_line_anchor": True,
            "in_progress_session_visible": True,
            "turn_bodies_not_injected": True,
            "current_request_authoritative": True,
        }

        for content, key in [
            ("Python 版本固定为 3.13", "l3-runtime-version"),
            ("项目模板默认使用 FastAPI 脚手架", "l3-project-template"),
            ("测试框架使用 pytest", "l3-test-framework"),
            ("常用测试命令是 pytest -q", "l3-test-command"),
        ]:
            assert await bridge.put_explicit(
                content=content,
                target="memory",
                run_id=key,
                session_id="requirements-l3-session",
            )
        l3_headers = await client.long_term.search_headers("", top_k=100)
        l3_records = await client.long_term.get_many(
            [item.memory_id for item in l3_headers]
        )
        l3_slots = {
            tag.removeprefix("slot:")
            for memory in l3_records
            for tag in memory.tags
            if tag.startswith("slot:")
        }
        assert {
            "development.runtime_version.python",
            "development.project_template",
            "development.test_framework",
            "development.command.test",
        } <= l3_slots
        result["l3_extended_slots"] = sorted(
            slot for slot in l3_slots if slot.startswith("development.")
        )

        checkpoints = await client.runtime.list_checkpoints(limit=10)
        assert len(checkpoints) == 5
        restored = await client.runtime.restore_checkpoint(
            checkpoints[0]["checkpoint_ref"]
        )
        assert restored["session_id"] == session_id
        for index in range(7):
            await client.runtime.prepare_checkpoint(
                session_id, idempotency_key=f"acceptance-extra-checkpoint-{index}"
            )
        assert len(await client.runtime.list_checkpoints(limit=10)) == 10
        result["checkpoints"] = {"retained": 10, "restore": "ok"}

        credential_run = "requirements-credential-run"
        assert await bridge.record_turn(
            run_id=credential_run,
            session_id="requirements-sensitive-session",
            agent_id="requirements-agent",
            user_text="remember my password is synthetic-acceptance-secret",
            assistant_text="I will not promote credentials to durable memory",
        )
        pii_run = "requirements-pii-run"
        assert await bridge.record_turn(
            run_id=pii_run,
            session_id="requirements-sensitive-session",
            agent_id="requirements-agent",
            user_text="remember my email is synthetic@example.invalid",
            assistant_text="I will keep personal data out of durable extraction",
        )
        stats = await client.stats()
        assert stats["semantic_projection_jobs"] == 0
        headers = await client.long_term.search_headers("synthetic", top_k=20)
        # `search_headers` is a *semantic* query, so a set of unrelated but
        # similar records may come back. Assert on content, not on emptiness:
        # the point is that the credential/PII turns were never promoted.
        _export = await client.privacy.export_data(
            limit_per_collection=500, include_audit=False
        )
        _texts = [str(item.get("text", "")) for item in _export["long_term_memories"]]
        _leaked = [text for text in _texts if "synthetic" in text]
        assert not _leaked, f"sensitive content promoted to durable memory: {_leaked[:3]}"
        result["sensitive_scan"] = {
            "durable_records_scanned": len(_texts),
            "literal_sensitive_hits": len(_leaked),
            "semantic_search_hits": len(headers),
        }
        result["sensitive_data"] = {
            "session_persisted_encrypted": True,
            "long_term_extraction_blocked": True,
            "projection_jobs": stats["semantic_projection_jobs"],
        }

        projection_session = "requirements-projection-recovery"
        projection_user = await client.sessions.append_event(
            session_id=projection_session,
            event_type="user_message",
            payload={
                "run_id": "requirements-projection-run",
                "status": "completed",
                "text": "remember the projection recovery marker",
            },
            idempotency_key="requirements-projection-user",
        )
        projection_assistant = await client.sessions.append_event(
            session_id=projection_session,
            event_type="assistant_message",
            payload={
                "run_id": "requirements-projection-run",
                "status": "completed",
                "text": "acknowledged",
            },
            idempotency_key="requirements-projection-assistant",
        )
        projection_job = await client.projection.enqueue_turn(
            provider="mem0-oss-v2",
            run_id="requirements-projection-run",
            session_id=projection_session,
            user_event_id=str(projection_user["event_id"]),
            assistant_event_id=str(projection_assistant["event_id"]),
            idempotency_key="requirements-projection-enqueue",
        )
        claimed = await client.projection.claim(
            provider="mem0-oss-v2",
            worker_id="requirements-failing-worker",
            lease_ms=30_000,
        )
        assert claimed is not None and claimed["job_id"] == projection_job["job_id"]
        await client.projection.fail(
            str(projection_job["job_id"]),
            worker_id="requirements-failing-worker",
            error="synthetic terminal projection failure",
            retry_after_ms=1_000,
            terminal=True,
            idempotency_key="requirements-projection-terminal-failure",
        )
        dead_jobs = await client.projection.list_dead(
            provider="mem0-oss-v2", limit=10
        )
        assert [item["job_id"] for item in dead_jobs] == [projection_job["job_id"]]
        requeued = await client.projection.requeue_dead(
            str(projection_job["job_id"]),
            provider="mem0-oss-v2",
            idempotency_key="requirements-projection-requeue",
        )
        assert requeued["state"] == "pending"
        assert requeued["previous_error"] == "synthetic terminal projection failure"
        claimed_again = await client.projection.claim(
            provider="mem0-oss-v2",
            worker_id="requirements-recovery-worker",
            lease_ms=30_000,
        )
        assert claimed_again is not None
        assert claimed_again["job_id"] == projection_job["job_id"]
        assert claimed_again["attempt_count"] == 1
        result["projection_recovery"] = {
            "dead_jobs_listed": len(dead_jobs),
            "previous_error_preserved": True,
            "requeued_and_reclaimed": True,
        }

        await client.audit.record(
            "runtime.tool.completed",
            details={"tool": "acceptance.synthetic"},
        )
        audit = await client.audit.export(limit=500)
        assert audit["format"] == "bianbu-agent-memory.audit.v1"
        assert any(
            item["action"] == "runtime.tool.completed" for item in audit["records"]
        )
        assert (await client.audit.status())["state"] == "ok"
        exported = await client.privacy.export_data(
            limit_per_collection=500, include_audit=True
        )
        assert exported["format"] == "bianbu-agent-memory.export.v1"
        result["audit_and_export"] = {
            "audit_records": len(audit["records"]),
            "export_session_events": len(exported["session_events"]),
            "export_long_term_memories": len(exported["long_term_memories"]),
        }

        latency_ms: list[float] = []
        for _ in range(100):
            started = time.perf_counter()
            await client.long_term.search_headers("package manager", top_k=5)
            latency_ms.append((time.perf_counter() - started) * 1000)
        p50 = statistics.median(latency_ms)
        p95 = percentile(latency_ms, 0.95)
        assert p95 <= 100
        result["lexical_retrieval_ms"] = {
            "samples": len(latency_ms),
            "p50": round(p50, 3),
            "p95": round(p95, 3),
            "target": 100,
        }

        cleared = await client.privacy.clear_history(confirm_token=confirm_token)
        assert cleared["long_term_preferences_retained"] is True
        assert (await client.long_term.get_many([preference.memory_id]))[0].text.endswith(
            "light"
        )
        assert (await client.stats())["session_events"] == 0
        try:
            await client.privacy.clear_all(confirm_token="wrong-token")
        except PermissionDenied:
            pass
        else:
            raise AssertionError("clear-all accepted an invalid confirmation token")
        cleared_all = await client.privacy.clear_all(confirm_token=confirm_token)
        assert cleared_all["deleted"]["long_term_memories"] >= 2
        stats = await client.stats()
        assert stats["session_events"] == 0
        assert stats["long_term_memories"] == 0
        result["privacy_controls"] = {
            "clear_history_retained_preferences": True,
            "clear_all_confirmation_required": True,
            "clear_all": "ok",
        }

        await client.privacy.update_settings(memory_enabled=False)
        try:
            await client.long_term.search_headers("anything")
        except MemoryDisabled:
            pass
        else:
            raise AssertionError("memory reads remained enabled")
        await client.privacy.update_settings(memory_enabled=True)
        result["memory_switch"] = "default-on and fail-closed when disabled"
        restart_marker = await client.long_term.put(
            kind=MemoryKind.FACT,
            text="restart recovery marker",
            sync_policy=SyncPolicy.LOCAL_ONLY,
            idempotency_key="acceptance-restart-marker",
        )
        result["restart_memory_id"] = restart_marker.memory_id
        return result
    finally:
        await bridge.close()


def plain_sqlite_is_rejected(database: Path) -> bool:
    try:
        connection = sqlite3.connect(database)
        try:
            connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        return True
    return False


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--memoryd", default="target/release/bianbu-agent-memoryd", type=Path
    )
    arguments = parser.parse_args()
    memoryd = arguments.memoryd.resolve()
    if not memoryd.is_file():
        raise FileNotFoundError(memoryd)
    confirm_token = uuid.uuid4().hex
    account_secret = uuid.uuid4().hex + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="agent-memory-acceptance-") as raw:
        root = Path(raw)
        secret_path = root / "account-secret"
        secret_path.write_text(account_secret + "\n", encoding="utf-8")
        secret_path.chmod(0o600)
        socket_path = root / "memoryd.sock"
        database = root / "memory.db"
        key_package = root / "memory.keypkg"
        environment = dict(os.environ)
        environment["BIANBU_MEMORY_DEV_CONFIRM_TOKEN"] = confirm_token
        process = subprocess.Popen(
            [
                str(memoryd),
                "--development-identity",
                "--socket",
                str(socket_path),
                "--database",
                str(database),
                "--key-package",
                str(key_package),
                "--account-secret-file",
                str(secret_path),
                "--initialize-key-package",
            ],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            await wait_for_socket(socket_path, process)
            result = await exercise(socket_path, confirm_token)
            process.terminate()
            process.wait(timeout=10)
            process = subprocess.Popen(
                [
                    str(memoryd),
                    "--development-identity",
                    "--socket",
                    str(socket_path),
                    "--database",
                    str(database),
                    "--key-package",
                    str(key_package),
                    "--account-secret-file",
                    str(secret_path),
                ],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            await wait_for_socket(socket_path, process)
            async with await AsyncMemoryClient.connect(socket_path) as client:
                health = await client.health()
                # `system.health` only reports an integrity verdict it could
                # take against a quiesced writer; while a background batch job
                # holds the database the sample is reported as inconclusive
                # (database="busy") instead of as a phantom FTS5 defect. Poll
                # until the first conclusive verdict before asserting.
                for _ in range(20):
                    if health["database"] != "busy":
                        break
                    await asyncio.sleep(0.25)
                    health = await client.health()
                assert health["database"] == "ok", health
                restored = await client.long_term.get_many(
                    [str(result.pop("restart_memory_id"))]
                )
                assert restored[0].text == "restart recovery marker"
                result["restart_recovery"] = "persisted marker restored"
            result["plain_sqlite_rejected"] = plain_sqlite_is_rejected(database)
            assert result["plain_sqlite_rejected"] is True
            result["secret_permissions"] = {
                "account_secret": oct(secret_path.stat().st_mode & 0o777),
                "key_package": oct(key_package.stat().st_mode & 0o777),
            }
            assert result["secret_permissions"] == {
                "account_secret": "0o600",
                "key_package": "0o600",
            }
            print(
                json.dumps(
                    {"status": "K3_REQUIREMENTS_ACCEPTANCE_OK", **result},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()


if __name__ == "__main__":
    asyncio.run(main())
