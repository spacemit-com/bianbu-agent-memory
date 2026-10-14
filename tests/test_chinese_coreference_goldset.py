from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


DATASET = Path(__file__).parent / "data" / "chinese_coreference_goldset.jsonl"
REQUIRED_FIELDS = {
    "id",
    "category",
    "history",
    "current_session_id",
    "current_question",
    "expected_referent",
    "expected_action_or_answer",
    "tags",
}


def _cases() -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in DATASET.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_coreference_goldset_has_required_schema_and_coverage() -> None:
    cases = _cases()
    assert len(cases) >= 50
    assert len({case["id"] for case in cases}) == len(cases)

    tag_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    for case in cases:
        assert REQUIRED_FIELDS <= case.keys()
        assert isinstance(case["history"], list) and case["history"]
        assert all(
            isinstance(turn, dict)
            and {"session_id", "user", "assistant"} <= turn.keys()
            and all(str(turn[key]).strip() for key in ("session_id", "user", "assistant"))
            for turn in case["history"]
        )
        assert all(str(case[field]).strip() for field in REQUIRED_FIELDS - {"history", "tags"})
        assert isinstance(case["tags"], list) and case["tags"]
        tag_counts.update(str(tag) for tag in case["tags"])
        category_counts[str(case["category"])] += 1

    for marker in ("这个", "那个", "刚才", "第一个"):
        assert tag_counts[marker] >= 10
    assert tag_counts["cross_session"] >= 8
    assert tag_counts["conflict"] >= 6
    assert category_counts["冲突指令"] >= 6
