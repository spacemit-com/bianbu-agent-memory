from bianbu_agent_sdk.memory.types import (
    MemoryIdentity,
    MemoryKind,
    MemoryRecallItem,
    SessionSummaryPatch,
    SyncPolicy,
)


def test_identity_is_server_shaped_and_immutable() -> None:
    identity = MemoryIdentity.from_dict(
        {
            "account_id": "local:test",
            "device_id": "k3",
            "agent_id": "io.bianbu.test",
            "agent_instance_id": "instance",
            "memory_space_id": "space",
            "capabilities": ["memory.long_term.read"],
        }
    )
    assert identity.agent_id == "io.bianbu.test"
    assert identity.agent_name == "io.bianbu.test"
    assert identity.capabilities == frozenset({"memory.long_term.read"})


def test_summary_patch_omits_unspecified_fields() -> None:
    patch = SessionSummaryPatch(current_status="working", next_steps=("test",))
    assert patch.to_dict() == {"current_status": "working", "next_steps": ["test"]}
    assert SyncPolicy.ACCOUNT_AGENT.value == "account_agent"


def test_recall_item_pairs_the_epoch_stamp_with_a_printable_time() -> None:
    item = MemoryRecallItem(
        memory_id="mem-1",
        kind=MemoryKind.PROFILE,
        text="我是百岁太奶",
        revision=1,
        source="agent",
        updated_at=1789893195471,
    )

    payload = item.to_dict()

    assert payload["updated_at"] == 1789893195471
    assert payload["updated_at_display"] == "2026-09-20 16:33"


def test_recall_item_omits_the_printable_time_it_cannot_produce() -> None:
    def item_with(updated_at):
        return MemoryRecallItem(
            memory_id="mem-1",
            kind=MemoryKind.FACT,
            text="无时间戳的记录",
            revision=1,
            source="agent",
            updated_at=updated_at,
        )

    # A record the backend never stamped must not render as 1970.
    assert "updated_at_display" not in item_with(0).to_dict()
    assert item_with(0).to_dict()["updated_at"] == 0
    assert "updated_at" not in item_with(None).to_dict()


def test_recall_item_carries_the_stored_tags() -> None:
    def item_with(tags):
        return MemoryRecallItem(
            memory_id="mem-1",
            kind=MemoryKind.FACT,
            text="机房断电演练固定在每周三凌晨",
            revision=1,
            source="agent",
            tags=tags,
        )

    # The user-designated mark lives only in the tags, so a read that drops
    # them cannot answer "which of these did I mark important".
    marked = item_with(("mem0-derived", "important")).to_dict()
    assert marked["tags"] == ["mem0-derived", "important"]

    # An untagged record keeps its old shape: no empty list to mistake for a mark.
    assert "tags" not in item_with(()).to_dict()
