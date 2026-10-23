from types import SimpleNamespace
import asyncio

import pytest
from bianbu_agent_sdk.memory import context
from bianbu_agent_sdk.memory.runtime import MemoryBridge


class Bridge(MemoryBridge):
    def __init__(self, *, disabled=False):
        super().__init__(account_id="trusted-account", agent_name="trusted-agent")
        self.disabled = disabled
        self.queries = []

    async def settings(self):
        return {"memory_enabled": not self.disabled}

    async def recall(self, query, **kwargs):
        self.queries.append((query, kwargs))
        return (
            "[fact rev=1 source=agent] crash test residue",
            "[preference rev=1 source=agent slot=development.editor] Prefer vim",
        )

    async def _client_or_none(self):
        class Sessions:
            async def get_context(self, sid, **kwargs):
                assert sid == "current"
                assert kwargs == {"include_summary":True, "include_events":False}
                return {"summary": {"revision": 2, "summary": {
                    "current_status": "Continue the current task", "key_decisions": ["Keep raw events"],
                    "next_steps": ["Run regression"], "open_questions": ["Confirm release"]}}}
        class Instructions:
            async def list(self, **kwargs):
                return [SimpleNamespace(text="Use concise replies", priority=2,
                         revision=1, scope=SimpleNamespace(value="agent"))]
        return SimpleNamespace(sessions=Sessions(), instructions=Instructions())


@pytest.mark.asyncio
async def test_sdk_assembles_guarded_memory_instructions_and_current_l2():
    bridge = Bridge()
    patch = await bridge.prepare_context(query="editor", session_id="current",
                                         config={"recall_top_k": 2, "recall_max_chars": 300})
    text = "\n".join(patch["context_sections"])
    assert "Prefer vim" in text and "crash test residue" not in text
    assert "Use concise replies" in text
    assert "[L2 session summary revision=2]" in text and "Keep raw events" in text
    assert "Run regression" in text and "Confirm release" in text
    assert patch["metadata"]["memory_recall_withheld"] == 1
    assert bridge.queries[0][1] == {"top_k": 2, "max_chars": 300, "include_explicit": False}


@pytest.mark.asyncio
async def test_disabled_memory_suppresses_l2_instructions_and_history():
    bridge = Bridge(disabled=True)
    assert await bridge.prepare_context(query="first session", session_id="current") is None
    assert bridge.queries == []


@pytest.mark.asyncio
async def test_history_guidance_does_not_recall_durable_memory():
    bridge = Bridge()
    patch = await bridge.prepare_context(query="first session", session_id="current")
    assert patch["metadata"]["session_history_request"] is True
    assert bridge.queries == []


@pytest.mark.asyncio
async def test_sdk_context_timeout_degrades_but_cancellation_propagates():
    bridge = Bridge()
    async def slow():
        await asyncio.sleep(10)
    bridge.settings = slow
    assert await bridge.prepare_context(query="hi", session_id="current",
                                         config={"timeout_seconds": 0.001}) is None
    async def cancelled():
        raise asyncio.CancelledError()
    bridge.settings = cancelled
    with pytest.raises(asyncio.CancelledError):
        await bridge.prepare_context(query="hi", session_id="current")


@pytest.mark.asyncio
async def test_sdk_lifecycle_forwards_explicit_close_and_interruption():
    bridge = Bridge()
    calls = []
    async def record(**kwargs):
        calls.append(kwargs)
        return True
    bridge.record_turn = record
    bridge.record_interrupted_turn = record
    assert await bridge.record_runtime_result(run_id="r", session_id="s", agent_id="a",
        user_text="hi", assistant_text="hello", status="completed", metadata={"session_end": True})
    assert calls[-1]["force_summary"] is True
    assert await bridge.record_runtime_result(run_id="r2", session_id="s", agent_id="a",
        user_text="task", assistant_text="", status="cancelled")
    assert calls[-1]["status"] == "cancelled"


@pytest.mark.parametrize(("item", "reason"), [
    ("[fact rev=1 source=agent] CRASH-test", "not_a_durable_preference"),
    ("[profile slot=workflow.bio] my name", "unknown_slot"),
    ("[preference] What editor do I use?", "question_like"),
    ("[profile] ", "empty"),
    ("[profile] bad\x00text", "control_characters"),
    ("[profile] " + "x" * 2201, "oversized"),
    ("[decision] Use SQLite locally", None),
    ("[profile slot=development.editor] Prefer vim", None),
])
def test_sdk_keeps_the_injection_guard(item, reason):
    assert context.injection_pollution_reason(item) == reason


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_failure", [False, True])
async def test_compression_summary_survives_interruption(summary_failure):
    bridge = Bridge()
    calls = []
    class Sessions:
        async def get_context(self, sid):
            return {"last_event_sequence":3, "summary":{"revision":1}}
        async def update_summary(self, sid, **kwargs):
            assert kwargs["patch"].current_status == "summary preserved"
            assert kwargs["expected_revision"] == 1
            calls.append("summary")
            if summary_failure:
                raise RuntimeError("summary write failed")
    class Runtime:
        async def prepare_checkpoint(self, sid, **kwargs):
            calls.append("checkpoint")
    async def client():
        return SimpleNamespace(sessions=Sessions(), runtime=Runtime())
    async def interrupted(**kwargs):
        calls.append("interrupted")
        return True
    bridge._client_or_none = client
    bridge.record_interrupted_turn = interrupted
    assert await bridge.record_runtime_result(run_id="r", session_id="s", agent_id="a",
        user_text="task", assistant_text="", status="cancelled", metadata={
            "context_compressions":[{"compression_id":"c1", "summary":"summary preserved"}]})
    assert calls == (["summary", "interrupted"] if summary_failure
                     else ["summary", "checkpoint", "interrupted"])


def test_sdk_owns_legacy_file_backend(tmp_path):
    from bianbu_agent_sdk.memory.lifecycle import record_legacy_result
    (tmp_path / "bianbu-memory.json").write_text('{"user":["calls me Ada"]}', encoding="utf-8")
    record_legacy_result(str(tmp_path), {"metadata":{"memory_to_record":"finished onboarding"}})
    patch = context.legacy_context(str(tmp_path))
    assert "calls me Ada" in "\n".join(patch["context_sections"])
    assert "finished onboarding" in "\n".join(patch["context_sections"])
