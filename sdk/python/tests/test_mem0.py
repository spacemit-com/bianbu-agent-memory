import asyncio

import pytest

from bianbu_agent_sdk.memory.mem0 import (
    Mem0Coordinator,
    MemoryCandidate,
    MutationEvent,
    RuleBasedMem0Planner,
    parse_candidates,
    parse_mutations,
    infer_memory_kind,
)
from bianbu_agent_sdk.memory.runtime import get_memory_bridge
from bianbu_agent_sdk.memory.types import (
    LongTermMemory,
    MemoryKind,
    SyncPolicy,
    WriteReceipt,
)


def test_rule_planner_extracts_name_but_keeps_reminder_in_session_memory() -> None:
    planner = RuleBasedMem0Planner()
    candidates = asyncio.run(
        planner.extract(
            (
                {"role": "user", "content": "我是小圆"},
                {
                    "role": "user",
                    "content": "记得明天上午9点提醒我完成 AgentMemory 和 Runtime 工程对接",
                },
            )
        )
    )

    assert [(item.text, item.kind) for item in candidates] == [
        ("我是小圆", MemoryKind.PROFILE),
    ]


def test_rule_planner_does_not_attach_same_message_reminder_to_profile() -> None:
    planner = RuleBasedMem0Planner()
    candidates = asyncio.run(
        planner.extract(
            (
                {
                    "role": "user",
                    "content": (
                        "我叫小圆。明天上午9点提醒我完成 AgentMemory 和 Runtime 工程对接"
                    ),
                },
            )
        )
    )

    assert candidates == (
        MemoryCandidate("我叫小圆", MemoryKind.PROFILE, 0.65),
    )


def test_rule_planner_keeps_the_preference_a_name_declaration_bundles() -> None:
    """K3 2026-09-29: "I am Grandma, young people's stuff is beyond me, you have

    to answer me in plain words" stored only the declared name, because a name
    declaration short-circuited the whole sentence.  A slot *replace* already
    preserved the residual (see ontology.slot_residual); adding the slot record
    has to keep it too, or the communication-style half is dropped before it can
    ever be written -- which is exactly what the user saw on 10.0.91.67.
    """

    planner = RuleBasedMem0Planner()
    candidates = asyncio.run(
        planner.extract(
            (
                {
                    "role": "user",
                    "content": (
                        "\u6211\u662f\u767e\u5c81\u592a\u5976\uff0c\u5e74\u8f7b\u4eba\u7684"
                        "\u4e1c\u897f\u6211\u90fd\u4e0d\u61c2\uff0c\u4f60\u5f97\u7528\u7b80"
                        "\u6d01\u6e05\u6670\u6613\u61c2\u7684\u8bdd\u6765\u56de\u7b54\u6211"
                        "\u7684\u95ee\u9898"
                    ),
                },
            )
        )
    )

    assert [(item.text, item.kind) for item in candidates] == [
        ("\u6211\u662f\u767e\u5c81\u592a\u5976", MemoryKind.PROFILE),
        (
            "\u5e74\u8f7b\u4eba\u7684\u4e1c\u897f\u6211\u90fd\u4e0d\u61c2\uff0c\u4f60"
            "\u5f97\u7528\u7b80\u6d01\u6e05\u6670\u6613\u61c2\u7684\u8bdd\u6765\u56de"
            "\u7b54\u6211\u7684\u95ee\u9898",
            MemoryKind.PREFERENCE,
        ),
    ]


def test_rule_planner_keeps_a_development_habit_bundled_with_a_name() -> None:
    """The residual keeps its own kind instead of being flattened into a name."""

    planner = RuleBasedMem0Planner()
    candidates = asyncio.run(
        planner.extract(
            (
                {
                    "role": "user",
                    "content": (
                        "\u6211\u662f\u5c0f\u660e\uff0c\u6211\u4e60\u60ef\u7528 uv "
                        "\u7ba1\u7406\u4f9d\u8d56"
                    ),
                },
            )
        )
    )

    assert [(item.text, item.kind) for item in candidates] == [
        ("\u6211\u662f\u5c0f\u660e", MemoryKind.PROFILE),
        ("\u6211\u4e60\u60ef\u7528 uv \u7ba1\u7406\u4f9d\u8d56", MemoryKind.DECISION),
    ]


@pytest.mark.parametrize(
    "content",
    (
        "\u6211\u662f\u767e\u5c81\u592a\u5976\uff0c\u4f60\u80fd\u5e2e\u6211"
        "\u67e5\u8be2\u4e00\u4e0b\u73e0\u6d77\u7684\u5929\u6c14\u5417",
        "\u6211\u662f\u767e\u5c81\u592a\u5976\uff0c\u4f60\u80fd\u5e2e\u6211"
        "\u67e5\u8be2\u4e00\u4e0b\u73e0\u6d77\u7684\u5929\u6c14\u5417\uff1f",
        "\u6211\u662f\u767e\u5c81\u592a\u5976\uff0c\u73e0\u6d77\u7684\u5929"
        "\u6c14\u600e\u4e48\u6837\uff1f",
        "\u6211\u662f\u767e\u5c81\u592a\u5976\uff0c\u73b0\u5728\u51e0\u70b9\u4e86\uff1f",
    ),
)
def test_rule_planner_stores_nothing_when_the_name_declaration_asks_a_question(
    content: str,
) -> None:
    """A question takes the whole message, name declaration included.

    ``extract`` rejects an interrogative before it ever looks for a slot, so
    there is no name-only fallback either: the run produces no candidate at all.
    """

    planner = RuleBasedMem0Planner()

    assert asyncio.run(planner.extract(({"role": "user", "content": content},))) == ()


@pytest.mark.parametrize(
    "content",
    (
        "\u6211\u662f\u767e\u5c81\u592a\u5976\uff0c\u6211\u73b0\u5728\u6709\u70b9\u5e73\u9759",
    ),
)
def test_rule_planner_keeps_a_transient_state_bundled_with_a_name(
    content: str,
) -> None:
    """A transient state is about the user, so it rides along with the name.

    It is not a durable preference, but it is also not a request to the
    assistant, so there is no keyword that separates it safely -- keeping it is
    the deliberate direction (see the task-request test for the other half).
    """

    planner = RuleBasedMem0Planner()
    candidates = asyncio.run(planner.extract(({"role": "user", "content": content},)))

    assert [(item.text, item.kind) for item in candidates] == [
        ("\u6211\u662f\u767e\u5c81\u592a\u5976", MemoryKind.PROFILE),
        ("\u6211\u73b0\u5728\u6709\u70b9\u5e73\u9759", MemoryKind.PREFERENCE),
    ]


@pytest.mark.parametrize(
    "content",
    (
        "\u6211\u662f\u767e\u5c81\u592a\u5976\uff0c\u5e2e\u6211\u67e5\u8be2\u4e00\u4e0b\u6df1\u5733\u5929\u6c14",
        "\u6211\u662f\u767e\u5c81\u592a\u5976\uff0c\u5e2e\u6211\u67e5\u8be2\u5341\u4e00\u56fd\u5e86\u6df1\u5733\u5929\u6c14\u60c5\u51b5\uff0c\u8c22\u8c22",
        "\u6211\u662f\u767e\u5c81\u592a\u5976\uff0c\u5e2e\u6211\u67e5\u4e00\u4e0b\u73e0\u6d77\u5929\u6c14",
    ),
)
def test_rule_planner_drops_a_task_clause_bundled_with_a_name(
    content: str,
) -> None:
    """An imperative aimed at the assistant is the current task, not a memory.

    K3 2026-09-29 "输出记忆": "我是百岁太奶，帮我查询一下深圳天气" was stored as the
    preference "帮我查询一下深圳天气", and every "我是..." sentence produced one more
    task reminder.  Only explicit imperative openers (``_TASK_REQUEST_LEADS``)
    are dropped; a style preference keeps the same sentence position and stays,
    which is why the filter is a lead-word list rather than a clause heuristic.
    """

    planner = RuleBasedMem0Planner()
    candidates = asyncio.run(planner.extract(({"role": "user", "content": content},)))

    assert [(item.text, item.kind) for item in candidates] == [
        ("\u6211\u662f\u767e\u5c81\u592a\u5976", MemoryKind.PROFILE),
    ]


def test_rule_planner_drops_a_polite_task_clause_bundled_with_a_name() -> None:
    """K3 2026-09-30 "输出所有记忆内容": "我是小圆，请查询一下珠海今天的天气" stored

    the preference "请查询一下珠海今天的天气", because the request list held
    "查询一下" but not "请查询", and a residual is stripped of leading punctuation
    only.  The polite form is the common one, so those leads are listed -- as
    verb-specific words, never a bare "请", or the durable style preference
    "请用简洁清晰易懂的话来回答我的问题" would be dropped along with the requests.
    """

    planner = RuleBasedMem0Planner()
    candidates = asyncio.run(
        planner.extract(
            ({"role": "user", "content": "我是小圆，请查询一下珠海今天的天气"},)
        )
    )

    assert [(item.text, item.kind) for item in candidates] == [
        ("我是小圆", MemoryKind.PROFILE),
    ]


def test_rule_planner_keeps_a_style_preference_that_opens_with_a_polite_word() -> None:
    """Companion to the polite-request test: "请用..." is a durable preference."""

    planner = RuleBasedMem0Planner()
    candidates = asyncio.run(
        planner.extract(
            (
                {
                    "role": "user",
                    "content": "我是小圆，请用简洁清晰易懂的话来回答我的问题",
                },
            )
        )
    )

    assert [(item.text, item.kind) for item in candidates] == [
        ("我是小圆", MemoryKind.PROFILE),
        ("请用简洁清晰易懂的话来回答我的问题", MemoryKind.PREFERENCE),
    ]


def test_auto_extraction_cannot_replace_the_recorded_name() -> None:
    """K3 2026-09-30: the automatic pass read "我是从梧州来的" as a name and

    replaced the record holding ``profile.user_name``, so the real name was
    gone.  Both sentences degenerate to the topic "我", which is why a topic
    coincidence may no longer revise a record that holds a formal field the
    candidate does not claim.
    """

    planner = RuleBasedMem0Planner()
    existing = (
        LongTermMemory(
            memory_id="name",
            kind=MemoryKind.PROFILE,
            text="我是小圆",
            summary=None,
            tags=("mem0-derived", "slot:profile.user_name"),
            evidence=(),
            source="agent",
            confidence=0.65,
            sensitivity="private",
            revision=1,
            sync_policy=SyncPolicy.LOCAL_ONLY,
            created_at=1,
            updated_at=2,
        ),
    )

    async def run():
        extracted = await planner.extract(
            ({"role": "user", "content": "我是从梧州来的"},)
        )
        return extracted, await planner.merge(extracted, existing)

    extracted, mutations = asyncio.run(run())

    assert extracted == (MemoryCandidate("我是从梧州来的", MemoryKind.PROFILE, 0.65),)
    assert mutations[0].event is MutationEvent.ADD
    assert mutations[0].memory_id is None


def _memory(memory_id: str, text: str, kind: MemoryKind) -> LongTermMemory:
    return LongTermMemory(
        memory_id=memory_id,
        kind=kind,
        text=text,
        summary=None,
        tags=(),
        evidence=(),
        source="agent",
        confidence=0.8,
        sensitivity="private",
        revision=2,
        sync_policy=SyncPolicy.LOCAL_ONLY,
        created_at=1,
        updated_at=2,
    )


def test_parse_candidates_accepts_fenced_json_and_deduplicates() -> None:
    result = parse_candidates(
        """```json
        {"facts":[
          {"text":"我习惯用 uv 管理依赖","kind":"decision","confidence":0.9},
          "我习惯用 uv 管理依赖"
        ]}
        ```"""
    )
    assert result == (
        MemoryCandidate("我习惯用 uv 管理依赖", MemoryKind.DECISION, 0.9),
    )


def test_parse_mutations_rejects_unknown_update_id() -> None:
    with pytest.raises(ValueError, match="unknown memory id"):
        parse_mutations(
            {"memory": [{"id": "missing", "text": "new", "event": "UPDATE"}]},
            existing=(),
        )


def test_rule_planner_add_update_and_none() -> None:
    planner = RuleBasedMem0Planner()
    existing = (_memory("m1", "依赖管理：pip", MemoryKind.DECISION),)

    async def run():
        extracted = await planner.extract(
            ({"role": "user", "content": "请记住，依赖管理：uv"},)
        )
        updated = await planner.merge(extracted, existing)
        unchanged = await planner.merge(
            (MemoryCandidate("依赖管理：pip", MemoryKind.DECISION),), existing
        )
        added = await planner.merge(
            (MemoryCandidate("喜欢深色主题", MemoryKind.PREFERENCE),), existing
        )
        return updated, unchanged, added

    updated, unchanged, added = asyncio.run(run())
    assert updated[0].event is MutationEvent.UPDATE
    assert updated[0].memory_id == "m1"
    assert unchanged[0].event is MutationEvent.NONE
    assert added[0].event is MutationEvent.ADD


def test_rule_planner_does_not_turn_recall_queries_into_memories() -> None:
    planner = RuleBasedMem0Planner()

    async def run():
        bare_query = await planner.extract(
            ({"role": "user", "content": "终端配色偏好"},)
        )
        question = await planner.extract(
            ({"role": "user", "content": "我喜欢什么终端配色？"},)
        )
        statement = await planner.extract(
            ({"role": "user", "content": "我喜欢浅色终端配色"},)
        )
        return bare_query, question, statement

    bare_query, question, statement = asyncio.run(run())
    assert bare_query == ()
    assert question == ()
    assert statement == (
        MemoryCandidate("我喜欢浅色终端配色", MemoryKind.PREFERENCE, 0.65),
    )


def test_rule_planner_rejects_identity_questions_without_question_marks() -> None:
    planner = RuleBasedMem0Planner()

    async def run():
        results = []
        for text in (
            "你好，你还记得我是谁吗",
            "我是谁",
            "你知道我的名字吗",
            "我叫什么名字",
            "我的名字是什么",
        ):
            results.append(
                await planner.extract(({"role": "user", "content": text},))
            )
        return tuple(results)

    assert all(result == () for result in asyncio.run(run()))


def test_rule_planner_normalizes_greetings_and_ignores_title_generation() -> None:
    planner = RuleBasedMem0Planner()
    existing = (
        _memory(
            "terminal-theme",
            "你好，请记住，终端配色偏好：深色。",
            MemoryKind.PREFERENCE,
        ),
    )

    async def run():
        extracted = await planner.extract(
            ({"role": "user", "content": "请记住，终端配色偏好：浅色。只需简短确认。"},)
        )
        mutation = await planner.merge(extracted, existing)
        title = await planner.extract(
            (
                {
                    "role": "user",
                    "content": (
                        "Generate a concise conversation title. Return only the title.\n"
                        "User message: 你好，请记住，终端配色偏好：深色。\n"
                        "Assistant reply:"
                    ),
                },
            )
        )
        return extracted, mutation, title

    extracted, mutation, title = asyncio.run(run())
    assert extracted == (
        MemoryCandidate("终端配色偏好：浅色。", MemoryKind.PREFERENCE, 0.65),
    )
    assert mutation[0].event is MutationEvent.UPDATE
    assert mutation[0].memory_id == "terminal-theme"
    assert mutation[0].text == "终端配色偏好：浅色。"
    assert title == ()


class _Planner:
    async def extract(self, messages):
        return (MemoryCandidate("喜欢深色主题", MemoryKind.PREFERENCE, 0.9),)

    async def merge(self, candidates, existing):
        return parse_mutations(
            {
                "memory": [
                    {
                        "id": "m1",
                        "text": "不再使用浅色主题",
                        "event": "DELETE",
                    }
                ]
            },
            existing=existing,
        )


class _LongTerm:
    def __init__(self, existing):
        self.existing = existing

    async def search_headers(self, query, top_k=5):
        return [type("Header", (), {"memory_id": "m1"})()]

    async def get_many(self, memory_ids):
        return [self.existing]


class _Sessions:
    def __init__(self):
        self.events = []

    async def append_event(self, **kwargs):
        self.events.append(kwargs)


def test_delete_is_a_confirmed_later_proposal_not_automatic_forget() -> None:
    existing = _memory("m1", "喜欢浅色主题", MemoryKind.PREFERENCE)
    client = type("Client", (), {})()
    client.long_term = _LongTerm(existing)
    client.sessions = _Sessions()

    result = asyncio.run(
        Mem0Coordinator(_Planner()).ingest_turn(
            client,
            session_id="s1",
            run_id="r1",
            user_text="请忘记浅色主题",
            assistant_text="好的",
        )
    )
    assert result.delete_proposals == 1
    assert client.sessions.events[0]["event_type"] == "memory_delete_proposed"
    assert client.sessions.events[0]["payload"]["requires_confirm_token"] is True


def test_coordinator_marks_pii_derived_memory_sensitive() -> None:
    class Planner:
        async def extract(self, messages):
            del messages
            return (MemoryCandidate("我叫测试用户", MemoryKind.PROFILE, 0.9),)

        async def merge(self, candidates, existing):
            del existing
            return (
                type(
                    "Mutation",
                    (),
                    {
                        "event": MutationEvent.ADD,
                        "text": candidates[0].text,
                        "kind": candidates[0].kind,
                        "memory_id": None,
                        "confidence": candidates[0].confidence,
                    },
                )(),
            )

    class LongTerm:
        def __init__(self):
            self.puts = []

        async def search_headers(self, *args, **kwargs):
            return []

        async def get_many(self, memory_ids):
            del memory_ids
            return []

        async def put(self, **arguments):
            self.puts.append(arguments)

    client = type("Client", (), {})()
    client.long_term = LongTerm()
    client.sessions = _Sessions()

    asyncio.run(
        Mem0Coordinator(Planner()).ingest_turn(
            client,
            session_id="sensitive-session",
            run_id="sensitive-run",
            user_text="我叫测试用户",
            assistant_text="你好",
            sensitivity="sensitive",
        )
    )

    assert client.long_term.puts[0]["sensitivity"] == "sensitive"


def test_runtime_bridge_uses_configured_socket(monkeypatch) -> None:
    monkeypatch.setenv("BIANBU_AGENT_MEMORY_SOCKET", "/tmp/memory-test.sock")
    bridge = asyncio.run(get_memory_bridge(timeout=9.5))
    assert bridge.socket_path == "/tmp/memory-test.sock"


def test_provider_memory_kind_is_mapped_to_canonical_types() -> None:
    assert infer_memory_kind("我喜欢深色终端主题") is MemoryKind.PREFERENCE
    assert infer_memory_kind("我习惯用 uv 管理 Python 依赖") is MemoryKind.DECISION


def test_chinese_development_habit_is_extracted_and_updates_same_topic() -> None:
    planner = RuleBasedMem0Planner()
    existing = (_memory("package-manager", "依赖管理：pip", MemoryKind.DECISION),)

    async def run():
        extracted = await planner.extract(
            ({"role": "user", "content": "请记住，依赖管理：uv"},)
        )
        return extracted, await planner.merge(extracted, existing)

    extracted, mutations = asyncio.run(run())
    assert extracted == (
        MemoryCandidate("依赖管理：uv", MemoryKind.DECISION, 0.65),
    )
    assert mutations[0].event is MutationEvent.UPDATE
    assert mutations[0].memory_id == "package-manager"


def test_slot_replace_preserves_the_text_the_slot_cannot_hold() -> None:
    """K3 (10.0.91.67) 2026-09-24 09:57: the automatic name extraction replaced a slot
    record that also held a communication-style preference.  The replace may take the
    slot value, but the preference has to survive as its own plain memory.
    """

    legacy = (
        "\u7528\u6237\u81ea\u79f0\u201c\u767e\u5c81\u592a\u5976\u201d\uff0c\u4e0d\u719f\u6089\u79d1\u6280\u4ea7\u54c1\u3002"
        "\u5e0c\u671b\u56de\u7b54\u7b80\u6d01\u3001\u6e05\u6670\u3001\u901a\u4fd7\u6613\u61c2\uff0c\u907f\u514d\u4e13\u4e1a\u672f\u8bed\u548c\u590d\u6742\u6b65\u9aa4\u3002"
    )
    slotted = LongTermMemory(
        memory_id="name-slot",
        kind=MemoryKind.PROFILE,
        text=legacy,
        summary=None,
        tags=("slot:profile.user_name",),
        evidence=(),
        source="agent",
        confidence=0.8,
        sensitivity="private",
        revision=1,
        sync_policy=SyncPolicy.LOCAL_ONLY,
        created_at=1,
        updated_at=2,
    )

    class Planner:
        async def extract(self, messages):
            del messages
            return (MemoryCandidate("\u6211\u662f\u767e\u5c81\u592a\u5976", MemoryKind.PROFILE, 0.9),)

        async def merge(self, candidates, existing):
            return parse_mutations(
                {
                    "memory": [
                        {
                            "id": "name-slot",
                            "text": candidates[0].text,
                            "event": "UPDATE",
                        }
                    ]
                },
                existing=existing,
            )

    class LongTerm:
        def __init__(self):
            self.puts = []
            self.revisions = []

        async def search_headers(self, query, top_k=5):
            del query, top_k
            return [type("Header", (), {"memory_id": "name-slot"})()]

        async def get_many(self, memory_ids):
            del memory_ids
            return [slotted]

        async def put(self, **arguments):
            self.puts.append(arguments)
            return WriteReceipt("preserved-1", 1, {})

        async def revise(self, memory_id, **arguments):
            self.revisions.append((memory_id, arguments))
            return WriteReceipt(memory_id, 2, {})

    client = type("Client", (), {})()
    client.long_term = LongTerm()
    client.sessions = _Sessions()

    result = asyncio.run(
        Mem0Coordinator(Planner()).ingest_turn(
            client,
            session_id="s1",
            run_id="r1",
            user_text=(
                "\u6211\u662f\u767e\u5c81\u592a\u5976\uff0c\u5e74\u8f7b\u4eba\u7684\u4e1c\u897f\u6211\u90fd\u4e0d\u61c2\uff0c"
                "\u5f97\u7528\u7b80\u6d01\u6e05\u6670\u6613\u61c2\u7684\u8bdd\u6765\u56de\u7b54\u6211\u7684\u95ee\u9898"
            ),
            assistant_text="\u597d\u5627\uff0c\u592a\u5976",
        )
    )

    assert result.updated == 1
    assert client.long_term.revisions[0][1]["text"] == "\u6211\u662f\u767e\u5c81\u592a\u5976"
    assert len(client.long_term.puts) == 1
    assert client.long_term.puts[0]["text"] == (
        "\u4e0d\u719f\u6089\u79d1\u6280\u4ea7\u54c1\u3002\u5e0c\u671b\u56de\u7b54\u7b80\u6d01\u3001\u6e05\u6670\u3001\u901a\u4fd7\u6613\u61c2\uff0c\u907f\u514d\u4e13\u4e1a\u672f\u8bed\u548c\u590d\u6742\u6b65\u9aa4"
    )
    assert not any(
        str(tag).startswith("slot:") for tag in client.long_term.puts[0]["tags"]
    )
