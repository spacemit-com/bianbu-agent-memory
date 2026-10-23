from __future__ import annotations

import pytest

from bianbu_agent_sdk.memory import field_dictionary, normalize_field_value
from bianbu_agent_sdk.memory.ontology import (
    is_question_like,
    preference_slot,
    slot_residual,
)


def test_l3_dictionary_has_unique_formal_names_and_required_development_fields() -> None:
    fields = field_dictionary()
    names = [str(field["name"]) for field in fields]

    assert len(names) == len(set(names))
    assert {
        "development.project_template",
        "development.package_manager",
        "development.code_style",
        "development.editor",
        "development.shell",
        "development.command.test",
        "development.command.build",
        "development.command.format",
        "development.command.lint",
        "development.command.run",
        "development.command.install",
    }.issubset(names)


@pytest.mark.parametrize(
    ("slot", "value", "expected"),
    (
        ("schedule.timezone", "Asia/Shanghai", "Asia/Shanghai"),
        ("schedule.week_start", "星期一", "monday"),
        ("appearance.terminal_color_scheme", "深色", "dark"),
        ("application.default_browser", "Google Chrome", "chrome"),
        ("interaction.response_language", "zh-CN", "zh-CN"),
        ("development.package_manager", "UV", "uv"),
        ("development.indentation", "4", "4"),
        ("development.line_length", 100, "100"),
        ("development.runtime_version.python", "3.13.2", "3.13.2"),
        ("development.command.test", "uv run pytest -q", "uv run pytest -q"),
    ),
)
def test_l3_values_are_canonicalized(slot: str, value: object, expected: str) -> None:
    assert normalize_field_value(slot, value) == expected


@pytest.mark.parametrize(
    ("slot", "value"),
    (
        ("schedule.timezone", "Shanghai"),
        ("schedule.default_reminder_time", "25:00"),
        ("interaction.response_language", "Chinese"),
        ("development.indentation", "9"),
        ("development.line_length", "400"),
        ("development.runtime_version.rust", "latest"),
        ("development.package_manager", "unknown-manager"),
        ("development.command.run", "line1\nline2"),
        ("unknown.field", "value"),
    ),
)
def test_l3_values_reject_invalid_contract_values(slot: str, value: object) -> None:
    with pytest.raises(ValueError):
        normalize_field_value(slot, value)


@pytest.mark.parametrize(
    "text",
    (
        "你还记得我是谁吗",
        "我是谁",
        "你知道我的名字吗",
        "我叫什么名字",
        "我的名字是什么",
        "我是不是小圆",
        "who am I",
        "what is my name",
    ),
)
def test_identity_questions_are_not_user_name_declarations(text: str) -> None:
    assert is_question_like(text) is True
    assert preference_slot(text) is None


@pytest.mark.parametrize(
    "text",
    (
        "我是小圆",
        "我叫小圆",
        "请叫我小圆",
        '用户自称"小圆"（可能为昵称）',
        "我的名字是小圆",
        "用户名称为 Acceptance User 742。",
        "my name is Alice",
    ),
)
def test_explicit_user_name_declarations_use_stable_slot(text: str) -> None:
    assert is_question_like(text) is False
    assert preference_slot(text) == "profile.user_name"


@pytest.mark.parametrize(
    "text",
    (
        "以后叫我小元",
        "之后叫我小圆",
        "称呼我为太奶",
        "希望被称呼为「太奶」",
        "用户希望被称呼为「太奶」",
    ),
)
def test_address_declarations_use_their_own_stable_slot(text: str) -> None:
    """K3 2026-10-15, defect 7136281755: "称呼" used to be an alias of

    ``profile.user_name``, so an address could only be stored by overwriting the
    name, and a sentence that declared both owned neither slot and had to be kept
    as unslotted prose.  An address now fills its own single-valued field, so a
    rename and a re-address reach the field they belong to.
    """

    assert is_question_like(text) is False
    assert preference_slot(text) == "profile.preferred_address"


def test_a_sentence_that_declares_a_name_and_an_address_owns_neither_slot() -> None:
    """One single-valued slot cannot hold two fields; extraction splits the sentence.

    Both halves are declared in one message, so neither field may claim the whole
    text -- that is exactly how the address ended up buried in a name record.
    """

    assert preference_slot("我的名字叫王源，以后叫我小元") is None
    assert preference_slot(
        "用户名字是 Dong，希望被称呼为「太奶」。"
    ) is None


@pytest.mark.parametrize(
    "text",
    (
        "我是从梧州来的",
        "我是做后端的",
        "我是说这个方案不行",
        "我是第一次来",
        "我是有点累",
        "我是刚下班",
        "我是很忙",
        "我是学生",
        "call me when you are free",
    ),
)
def test_a_predicate_after_the_declaration_does_not_take_the_name_slot(
    text: str,
) -> None:
    """K3 2026-09-30 "输出所有记忆内容": the record holding ``profile.user_name``

    went from "我是小圆" (rev 1) to "我是从梧州来的" (rev 2, same id), because the
    declaration pattern reads anything after "我是" as the name, and the slot is
    single-valued with newest-write-wins.  A declaration now owns the slot only
    when the value it declares can be a name; otherwise the sentence is kept as
    a plain memory, so a wrong value can no longer destroy the recorded name.
    """

    assert preference_slot(text) is None


#: K3 (10.0.91.67) 2026-09-24 09:57: 写入侧把"自称 + 说话风格偏好"合成一条记忆，
#: 分类把整句贴上了 `slot:profile.user_name`；4 秒后自动抽取把同槽位值改成
#: "我是百岁太奶"，整段文本被替换，偏好随之消失（同 id，revision 1→2）。
_SLOTTED_NAME_PLUS_PREFERENCE = (
    "\u7528\u6237\u81ea\u79f0\u201c\u767e\u5c81\u592a\u5976\u201d\uff0c\u4e0d\u719f\u6089\u79d1\u6280\u4ea7\u54c1\u3002"
    "\u5e0c\u671b\u56de\u7b54\u7b80\u6d01\u3001\u6e05\u6670\u3001\u901a\u4fd7\u6613\u61c2\uff0c\u907f\u514d\u4e13\u4e1a\u672f\u8bed\u548c\u590d\u6742\u6b65\u9aa4\u3002"
)


@pytest.mark.parametrize(
    "text",
    (
        _SLOTTED_NAME_PLUS_PREFERENCE,
        "\u6211\u662f\u767e\u5c81\u592a\u5976\uff0c\u5e74\u8f7b\u4eba\u7684\u4e1c\u897f\u6211\u90fd\u4e0d\u61c2\uff0c\u5f97\u7528\u7b80\u6d01\u6e05\u6670\u6613\u61c2\u7684\u8bdd\u6765\u56de\u7b54\u6211\u7684\u95ee\u9898",
        "\u6211\u53eb\u5c0f\u5706\uff0c\u5e0c\u671b\u7528\u5927\u767d\u8bdd\u8bb2",
        "\u559c\u6b22\u6253\u7fbd\u6bdb\u7403\uff0c\u6211\u662f\u5c0f\u5706",
        "my name is Alice and I like tea",
    ),
)
def test_name_declarations_that_keep_talking_do_not_own_the_slot(text: str) -> None:
    """A single-valued slot must not swallow a sentence that says something else."""

    assert preference_slot(text) is None


def test_slot_residual_reports_what_a_slot_replace_would_drop() -> None:
    assert slot_residual(_SLOTTED_NAME_PLUS_PREFERENCE, "profile.user_name") == (
        "\u4e0d\u719f\u6089\u79d1\u6280\u4ea7\u54c1\u3002\u5e0c\u671b\u56de\u7b54\u7b80\u6d01\u3001\u6e05\u6670\u3001\u901a\u4fd7\u6613\u61c2\uff0c\u907f\u514d\u4e13\u4e1a\u672f\u8bed\u548c\u590d\u6742\u6b65\u9aa4"
    )
    # A record that only names the user has nothing to preserve.
    assert slot_residual("\u6211\u662f\u767e\u5c81\u592a\u5976", "profile.user_name") is None
    # Only the slot about to be written is inspected.
    assert slot_residual(_SLOTTED_NAME_PLUS_PREFERENCE, "development.editor") is None


def test_slot_residual_reports_what_an_address_replace_would_drop() -> None:
    """The address field is single-valued too, so a replace keeps its residual."""

    assert slot_residual("\u4ee5\u540e\u53eb\u6211\u5c0f\u5143\uff0c\u56de\u7b54\u8981\u7b80\u6d01", "profile.preferred_address") == "\u56de\u7b54\u8981\u7b80\u6d01"
    # A clause that only asks to be addressed has nothing to preserve.
    assert slot_residual("\u4ee5\u540e\u53eb\u6211\u5c0f\u5143", "profile.preferred_address") is None


@pytest.mark.parametrize(
    "text",
    (
        "我喜欢用什么工具编码",
        "我喜欢用哪款笔记软件",
        "用户平时用什么编辑器",
        "我平时怎么安排工作",
        "用户什么时候开始用 Linux 的",
        "我用多少内存合适",
        "用户在哪里办公",
    ),
)
def test_declarative_questions_are_not_durable_memories(text: str) -> None:
    """陈述式疑问句：无问号但仍在询问，对标污染样本原文。

    The interrogative sits mid-sentence, so the trailing "吗/？" checks alone miss
    it.  Such text must not become a durable memory.
    """

    assert is_question_like(text) is True


def test_declarative_question_production_sample_has_no_slot() -> None:
    """生产污染样本没有槽位关键词，只能靠中段疑问词拦下。"""

    sample = "我喜欢用什么工具编码"

    assert is_question_like(sample) is True
    assert preference_slot(sample) is None


@pytest.mark.parametrize(
    "text",
    (
        "无论什么情况都要先备份",
        "什么时候都可以",
        "没什么特别的偏好",
        "怎么都行",
        "谁都不许动我的配置",
        "随便什么都行",
    ),
)
def test_interrogative_words_inside_statements_are_durable(text: str) -> None:
    """含疑问词但不是疑问的搭配要继续当阳性记忆保留。"""

    assert is_question_like(text) is False


def test_declarative_preferences_still_resolve_their_slot() -> None:
    """防回归：扩大疑问句识别不能把正常的偏好声明也拦下。"""

    assert is_question_like("我喜欢用 vim 写代码") is False
    assert preference_slot("我喜欢用 vim 写代码") == "development.editor"
