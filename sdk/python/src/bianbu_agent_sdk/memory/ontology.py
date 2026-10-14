from __future__ import annotations

import re
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class MemoryFieldSpec:
    name: str
    aliases: tuple[str, ...]
    value_type: str = "string"
    allowed_values: tuple[str, ...] = ()
    max_length: int = 256


L3_FIELD_DICTIONARY = (
    MemoryFieldSpec("profile.user_name", ("姓名", "名字", "称呼", "user name"), max_length=64),
    MemoryFieldSpec(
        "schedule.timezone", ("时区", "timezone", "time zone"), "timezone", max_length=64
    ),
    MemoryFieldSpec(
        "schedule.week_start",
        ("每周第一天", "一周开始日", "week start"),
        "enum",
        ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"),
        16,
    ),
    MemoryFieldSpec(
        "schedule.default_reminder_time",
        ("默认提醒时间", "default reminder time"),
        "time",
        max_length=5,
    ),
    MemoryFieldSpec(
        "appearance.wallpaper_color_scheme",
        ("壁纸配色", "桌面背景主题", "wallpaper color scheme"),
        "enum",
        ("light", "dark", "system"),
        16,
    ),
    MemoryFieldSpec(
        "appearance.terminal_color_scheme",
        ("终端配色", "终端主题", "terminal theme"),
        "enum",
        ("light", "dark", "system"),
        16,
    ),
    MemoryFieldSpec(
        "appearance.interface_color_scheme",
        ("界面配色", "系统外观", "interface theme"),
        "enum",
        ("light", "dark", "system"),
        16,
    ),
    MemoryFieldSpec(
        "application.default_browser",
        ("默认浏览器", "browser"),
        "enum",
        ("firefox", "chromium", "chrome", "edge", "safari"),
        32,
    ),
    MemoryFieldSpec(
        "interaction.response_language",
        ("回答语言", "回复语言", "response language"),
        "language_tag",
        max_length=35,
    ),
    MemoryFieldSpec(
        "development.programming_language",
        ("编程语言", "开发语言", "programming language"),
        max_length=64,
    ),
    MemoryFieldSpec(
        "development.project_template",
        ("项目模板", "工程模板", "脚手架", "project template", "scaffold"),
        max_length=512,
    ),
    MemoryFieldSpec(
        "development.package_manager",
        ("依赖管理", "包管理器", "package manager"),
        "enum",
        ("uv", "pip", "poetry", "cargo", "npm", "pnpm", "yarn", "maven", "gradle"),
        32,
    ),
    MemoryFieldSpec(
        "development.test_framework",
        ("测试框架", "test framework"),
        max_length=64,
    ),
    MemoryFieldSpec("development.formatter", ("格式化工具", "formatter"), max_length=64),
    MemoryFieldSpec("development.linter", ("代码检查工具", "linter"), max_length=64),
    MemoryFieldSpec(
        "development.indentation",
        ("代码缩进", "缩进宽度", "indent width"),
        "integer",
        ("1", "2", "3", "4", "5", "6", "7", "8"),
        1,
    ),
    MemoryFieldSpec(
        "development.line_length",
        ("行宽", "每行字符", "line length", "line width"),
        "integer_range_40_240",
        max_length=3,
    ),
    MemoryFieldSpec(
        "development.naming_convention",
        ("命名风格", "命名规范", "naming convention"),
        max_length=128,
    ),
    MemoryFieldSpec("development.code_style", ("代码风格", "code style"), max_length=256),
    MemoryFieldSpec("development.editor", ("编辑器", "IDE", "editor"), max_length=64),
    MemoryFieldSpec("development.shell", ("默认 Shell", "常用 Shell", "shell"), max_length=64),
    *tuple(
        MemoryFieldSpec(
            f"development.runtime_version.{runtime}",
            (f"{runtime} 版本", f"{runtime} version"),
            "version",
            max_length=64,
        )
        for runtime in ("python", "rust", "node", "java", "go")
    ),
    *tuple(
        MemoryFieldSpec(
            f"development.command.{action}",
            (f"{action} 命令", f"{action} command"),
            "command",
            max_length=2048,
        )
        for action in ("test", "build", "format", "lint", "run", "install")
    ),
)


_FIELD_BY_NAME = {field.name: field for field in L3_FIELD_DICTIONARY}

# Write-gate tables (MEM-L3-02 / MEM-L3-03). The field dictionary above stays the
# single authoritative source: ``resolve_slot`` only ever returns a dictionary
# name, so a caller can no longer persist a self-invented slot name.
_SLOT_ALIASES: dict[str, str] = {}
for _field in L3_FIELD_DICTIONARY:
    _SLOT_ALIASES.setdefault(_field.name, _field.name)
    _SLOT_ALIASES.setdefault(_field.name.rsplit(".", 1)[-1], _field.name)
    for _alias in _field.aliases:
        _SLOT_ALIASES.setdefault(
            " ".join(_alias.strip().casefold().split()), _field.name
        )

# Per-record budgets in *characters*: "user" is the profile target, "memory" the
# notes target. They were introduced with the MCP server and are finally
# enforced on every write path.
CONTENT_LIMITS = {"user": 1375, "memory": 2200}
_THEME_ALIASES = {
    "浅色": "light",
    "亮色": "light",
    "light mode": "light",
    "深色": "dark",
    "暗色": "dark",
    "dark mode": "dark",
    "跟随系统": "system",
    "system default": "system",
}
_WEEK_ALIASES = {
    "周一": "monday",
    "星期一": "monday",
    "周二": "tuesday",
    "星期二": "tuesday",
    "周三": "wednesday",
    "星期三": "wednesday",
    "周四": "thursday",
    "星期四": "thursday",
    "周五": "friday",
    "星期五": "friday",
    "周六": "saturday",
    "星期六": "saturday",
    "周日": "sunday",
    "星期日": "sunday",
    "星期天": "sunday",
}


def field_dictionary() -> tuple[dict[str, object], ...]:
    """Return the stable L3 field contract for Runtime and product integrations."""

    return tuple(asdict(field) for field in L3_FIELD_DICTIONARY)


def known_slots() -> tuple[str, ...]:
    """Return the authoritative L3 slot names accepted by the write gate."""

    return tuple(_FIELD_BY_NAME)


def resolve_slot(slot: str) -> str | None:
    """Return the authoritative L3 slot for a name or alias, else None."""

    key = " ".join(str(slot).strip().casefold().split())
    if not key:
        return None
    return _SLOT_ALIASES.get(key)


def content_limit(target: str) -> int:
    """Return the durable text budget in characters for one memory target."""

    return CONTENT_LIMITS.get(str(target).strip(), CONTENT_LIMITS["memory"])


def validate_memory_text(target: str, text: str) -> str | None:
    """Return a failure code when text may not be stored durably, else None."""

    if any(ord(character) < 32 and character not in "\n\t" for character in text):
        return "invalid_content"
    if len(text) > content_limit(target):
        return "content_too_long"
    return None


def normalize_field_value(slot: str, value: object) -> str:
    """Validate and canonicalize a value for a formal L3 slot."""

    field = _FIELD_BY_NAME.get(str(slot).strip())
    if field is None:
        raise ValueError(f"unknown L3 field: {slot}")
    raw = str(value).strip()
    if any(ord(character) < 32 for character in raw):
        raise ValueError(f"control characters are not allowed for {field.name}")
    normalized = " ".join(raw.split())
    if not normalized or len(normalized.encode("utf-8")) > field.max_length:
        raise ValueError(f"invalid value length for {field.name}")
    lowered = normalized.casefold()
    if field.name.startswith("appearance."):
        lowered = _THEME_ALIASES.get(lowered, lowered)
    elif field.name == "schedule.week_start":
        lowered = _WEEK_ALIASES.get(lowered, lowered)
    elif field.name == "application.default_browser" and lowered == "google chrome":
        lowered = "chrome"
    if field.allowed_values and lowered not in field.allowed_values:
        raise ValueError(f"unsupported value for {field.name}: {normalized}")
    if field.value_type == "timezone" and not (
        lowered == "utc"
        or re.fullmatch(r"[A-Za-z_+-]+/[A-Za-z0-9_+\-/]+", normalized)
    ):
        raise ValueError("timezone must be UTC or an IANA timezone name")
    if field.value_type == "time" and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", normalized):
        raise ValueError("time must use HH:MM")
    if field.value_type == "language_tag" and not re.fullmatch(
        r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", normalized
    ):
        raise ValueError("response language must use a BCP 47 language tag")
    if field.value_type == "language_tag" and lowered.split("-", 1)[0] not in {
        "zh",
        "en",
        "ja",
        "ko",
        "fr",
        "de",
        "es",
        "pt",
        "ru",
    }:
        raise ValueError("response language uses an unsupported language subtag")
    if field.value_type == "version" and not re.fullmatch(r"v?\d+(?:\.\d+){0,3}(?:[-+][A-Za-z0-9.-]+)?", normalized):
        raise ValueError("runtime version is invalid")
    if field.value_type == "integer_range_40_240":
        try:
            number = int(normalized)
        except ValueError as error:
            raise ValueError("line length must be an integer") from error
        if not 40 <= number <= 240:
            raise ValueError("line length must be between 40 and 240")
        return str(number)
    return lowered if field.value_type == "enum" else normalized


def field_spec(slot: str) -> MemoryFieldSpec | None:
    """Return the authoritative spec for one slot name or alias, else None."""

    canonical = resolve_slot(slot)
    return _FIELD_BY_NAME.get(canonical) if canonical else None


def resolve_field_value(slot: str, value: object) -> tuple[str | None, str | None]:
    """Validate and canonicalize one value for a formal L3 field.

    MEM-L3-03: a write that names a slot explicitly asserts that its text *is*
    that field's value, so the dictionary's ``allowed_values``, ``value_type``
    and ``max_length`` declarations are enforced instead of being inert
    documentation.  Returns ``(canonical_value, failure_reason)`` with exactly
    one of the two set.  Prose keeps working; it simply may not claim a formal
    field, so store it without a slot.
    """

    spec = field_spec(slot)
    if spec is None:
        return None, f"unknown L3 field: {slot}"
    try:
        return normalize_field_value(spec.name, value), None
    except ValueError as error:
        return None, str(error)


def field_value_failure(slot: str, reason: str) -> dict[str, object]:
    """Build the tool payload for a value the L3 dictionary refuses."""

    spec = field_spec(slot)
    payload: dict[str, object] = {
        "success": False,
        "code": "invalid_value",
        "error": reason,
        "slot": slot,
        "assistant_instruction": (
            "Do not claim success. Retry with the field's documented value, or "
            "omit slot to store this text as a free memory."
        ),
    }
    if spec is not None:
        payload["value_type"] = spec.value_type
        payload["max_length"] = spec.max_length
        if spec.allowed_values:
            payload["allowed_values"] = list(spec.allowed_values)
    return payload


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


_CHINESE_QUESTION_ENDING = re.compile(
    r"(?:吗|呢|么|嘛|谁|什么|哪(?:个|位|一)?|几|多少|如何|怎么|怎样|为何|为什么|叫啥)"
    r"\s*[。！!…]*$"
)
_ENGLISH_QUESTION_START = re.compile(
    r"^(?:what|who|when|where|why|how|do|does|did|is|are|am|can|could|"
    r"would|will|have|has)\b"
)
#: Chinese interrogatives that keep their interrogative reading anywhere in the
#: sentence.  A question is not always marked by a trailing "吗/？": a model can
#: also store a "陈述式疑问句" such as "我喜欢用什么工具编码", where the interrogative
#: sits in the middle and the sentence reads like a declarative statement.  That
#: defect sample is exactly this shape, so the whole sentence has to be
#: inspected, not only its end.
#:
#: ``哪`` / ``几`` are matched together with the word that follows them ("哪个", "哪款",
#: "几点") and excluded in the collocations that are not questions ("哪怕", "几乎",
#: "几百"), which are conjunctions or quantities rather than question words.
_CHINESE_INTERROGATIVE = re.compile(
    r"什么|啥|怎么|怎样|如何|为什么|为啥|为何|何时|谁|多少|多久|多长时间|"
    r"哪(?!怕)[\u4e00-\u9fff]|几(?!乎|百|千|万|亿|十)"
)
#: Collocations that carry an interrogative word but are not questions.  They are
#: removed before the scan so that "无论什么情况都要先备份" and "什么时候都可以" stay durable
#: memories instead of being withheld as question-shaped text.
_CHINESE_INTERROGATIVE_EXEMPTIONS = (
    "没什么",
    "没怎么",
    "不怎么样",
    "无论什么",
    "不管什么",
    "不管怎么",
    "无论如何",
    "随便什么",
    "什么时候都",
    "什么都可以",
    "什么都",
    "怎么都行",
    "怎么样都行",
    "谁都可以",
    "谁都不",
)
_PROFILE_DECLARATION = re.compile(
    r"(?:^|[\s，,。；;！!])(?:"
    r"(?:我叫|我是|请叫我|可以叫我|你可以叫我|叫我)\s*[\"'“‘「『]?"
    r"[^\s，,。；;！？!?\"'”’」』]{1,64}"
    r"|(?:我的名字|我的姓名|用户名称|用户姓名|用户名字)\s*(?:是|为|叫|[:：])\s*"
    r"[\"'“‘「『]?[^\s，,。；;！？!?\"'”’」』]{1,64}"
    r"|用户自称\s*[\"'“‘「『]?[^\s，,。；;！？!?\"'”’」』]{1,64}"
    r")"
)
_ENGLISH_PROFILE_DECLARATION = re.compile(
    r"(?:^|[\s,.;!])(?:my name is|the user's name is|user(?:'s)? name is|"
    r"call me|you can call me)\s+[\"']?[^,.;!?\"']{1,64}",
    re.IGNORECASE,
)

#: ``profile.user_name`` is single-valued: the next write to the slot replaces
#: whatever it holds.  A sentence may only own the slot when it *is* the name.
#: "用户自称"百岁太奶"，不熟悉科技产品。希望回答简洁、清晰、通俗易懂，避免专业术语和复杂步骤。"
#: starts by naming the user and then states a durable preference; slotting the
#: whole sentence hides the preference inside a value that the automatic
#: extraction later overwrites with "我是百岁太奶" (K3, 2026-09-24 09:57:49), and
#: the preference is gone.  A declaration therefore keeps the slot only while
#: nothing durable follows the name.
_PROFILE_RESIDUAL_MAX_CHARS = 24
_PROFILE_CLAUSE_BOUNDARY = re.compile(r"[，,、；;。！!？?\n]")
_PROFILE_SENTENCE_END = re.compile(r"[。！!？?；;…]+\s*$")
_PROFILE_PARENTHETICAL = re.compile(r"[（(][^（()）]{0,48}[)）]")
_PROFILE_NOISE_CHARS = (
    " \t\n，,、；;。！!？?…\"'\u201c\u201d\u2018\u2019\u300c\u300d\u300e\u300f"
    "\uff08\uff09()[]\u3010\u3011:：-\u2014"
)
_PROFILE_NOISE = re.compile(f"[{re.escape(_PROFILE_NOISE_CHARS)}]+")
_PROFILE_ENGLISH_CONTINUATION = re.compile(
    r"\b(?:and|but|because|so|also|however|though|although|while|which|who|that)\b",
    re.IGNORECASE,
)


def _condensed(value: str) -> str:
    """Collapse whitespace without changing case; residuals stay user-visible."""

    return " ".join(value.split())


def _profile_declaration_parts(text: str) -> tuple[str, str] | None:
    """Return the text a name declaration sits between, or None when there is none."""

    value = _condensed(text)
    match = _PROFILE_DECLARATION.search(value) or _ENGLISH_PROFILE_DECLARATION.search(value)
    if match is None:
        return None
    return value[: match.start()], value[match.end() :]


def profile_declaration_residual(text: str) -> str | None:
    """Return what a name declaration leaves unsaid, or None when there is none.

    The residual is the sentence with the declared name cut out: empty for a
    sentence that only names the user, non-empty for one that keeps talking.
    ``None`` means the text declares no name at all, which is different from a
    declaration that says nothing else.
    """

    parts = _profile_declaration_parts(text)
    if parts is None:
        return None
    return f"{parts[0]} {parts[1]}"


def _residual_says_more(residual: str) -> bool:
    """Whether a name declaration's leftover text still carries durable content."""

    value = _PROFILE_SENTENCE_END.sub("", _PROFILE_PARENTHETICAL.sub(" ", residual)).strip()
    if not value:
        return False
    # A clause break means the sentence kept talking after the name: "我叫小圆，
    # 希望用大白话讲" states a preference the name slot cannot hold.  A terminator
    # that merely ends the sentence was already dropped above.
    if _PROFILE_CLAUSE_BOUNDARY.search(value):
        return True
    only = _PROFILE_NOISE.sub("", value)
    if len(only) > _PROFILE_RESIDUAL_MAX_CHARS:
        return True
    return bool(_PROFILE_ENGLISH_CONTINUATION.search(only))


def _profile_declaration_is_standalone(text: str) -> bool:
    """Whether a name declaration only names the user instead of also saying more."""

    parts = _profile_declaration_parts(text)
    if parts is None:
        return False
    before, after = parts
    if _PROFILE_ENGLISH_CONTINUATION.search(_condensed(text)):
        # "my name is Alice and I like tea": the pattern reads the whole clause as
        # the name, so the conjunction is what gives the extra statement away.
        return False
    if _PROFILE_NOISE.sub("", before):
        # "喜欢打羽毛球，我是小圆" states something before it names the user.
        return False
    return not _residual_says_more(after)


#: A name declaration owns ``profile.user_name`` only while what it declares can
#: be a name.  ``_PROFILE_DECLARATION`` reads any 1-64 characters after "我是"
#: as the name, so "我是从梧州来的" / "我是做后端的" / "我是说这个方案不行" were
#: all taken for the user's name (K3 2026-09-30: the slot went from "我是小圆"
#: to "我是从梧州来的" four seconds after the user said it, and the real name was
#: gone).  Only high-confidence rejects are listed -- a word that cannot open a
#: name, a particle that cannot end one, an identity noun, or an interrogative.
#: The asymmetry is deliberate: a rejected declaration is still kept as a plain
#: memory, while a wrong value in the slot destroys the name already recorded,
#: so an ambiguous shape keeps the slot.  Chinese single characters appear only
#: when they are not also surnames; 于/常/平/向/白/曾/都/和/干/计 stay out on
#: purpose.
_PROFILE_NAME_REJECT_OPENERS = (
    "从", "在", "有", "很", "真", "太", "挺", "蛮", "刚", "正", "第", "要",
    "会", "觉", "认", "希", "说", "想", "问", "做", "搞", "打", "来", "去",
    "回", "到", "请", "让", "被", "把", "给", "用", "靠", "往", "对", "跟",
    "与", "因", "为", "但", "而", "更", "就", "才", "只", "还", "也", "已",
    "最", "现", "暂", "总", "每", "各", "某", "无", "非", "未", "当", "随",
    "按", "据", "准", "待", "住", "不", "没",
    "有点", "有些", "觉得", "认为", "希望", "想要", "打算", "准备", "喜欢",
    "讨厌", "经常", "偶尔", "平时", "通常", "大概", "也许", "应该", "必须",
    "一定", "从来", "一直", "暂时", "目前", "现在", "以前", "以后", "最近",
    "已经", "曾经", "正在", "第一次", "是不是", "有没有",
    "when", "if", "after", "before", "because", "since", "while", "the",
    "how", "what", "why", "where", "who", "not", "still", "just", "always",
    "never", "really", "very", "from", "and", "but", "to", "for", "at", "in",
    "on",
)
_PROFILE_NAME_REJECT_ENDINGS = (
    "的", "了", "过", "着", "吗", "吧", "呢", "啊", "呀", "嘛", "啦", "哦",
    "咯", "嘞", "喔",
)
_PROFILE_NAME_REJECT_IDENTITIES = (
    "学生", "老师", "教师", "教授", "医生", "护士", "工人", "农民", "老板",
    "员工", "职员", "经理", "主管", "总监", "司机", "律师", "会计", "程序员",
    "工程师", "设计师", "大学生", "研究生", "博士生", "男的", "女的", "阿姨",
    "叔叔", "大爷", "大妈",
)
_PROFILE_NAME_REJECT_ANYWHERE = ("什么", "怎么", "多少", "哪里", "哪个", "谁")
_PROFILE_NAME_EDGE = (
    " \t\n，,。；;！!？?、:：=（）()【】[]"
    "\"'“”‘’「」『』"
)
_PROFILE_DECLARATION_LEADS = (
    "你可以叫我",
    "可以叫我",
    "请叫我",
    "我的名字",
    "我的姓名",
    "用户名称",
    "用户姓名",
    "用户名字",
    "用户自称",
    "叫我",
    "我叫",
    "我是",
)
_ENGLISH_DECLARATION_LEADS = (
    "the user's name is",
    "user's name is",
    "user name is",
    "my name is",
    "you can call me",
    "call me",
)


def _declared_name(text: str) -> str | None:
    """Return the name a declaration introduces, or None when it cannot be read."""

    value = _condensed(text)
    match = _PROFILE_DECLARATION.search(value)
    if match is not None:
        piece = match.group(0).lstrip(_PROFILE_NAME_EDGE)
        for lead in _PROFILE_DECLARATION_LEADS:
            if piece.startswith(lead):
                piece = piece[len(lead) :]
                break
        else:
            return None
        piece = piece.lstrip(_PROFILE_NAME_EDGE + "是为叫").rstrip(_PROFILE_NAME_EDGE)
        return piece or None
    match = _ENGLISH_PROFILE_DECLARATION.search(value)
    if match is None:
        return None
    piece = match.group(0)
    lowered = piece.casefold()
    for lead in _ENGLISH_DECLARATION_LEADS:
        index = lowered.find(lead)
        if index >= 0:
            piece = piece[index + len(lead) :]
            break
    else:
        return None
    return piece.strip(_PROFILE_NAME_EDGE) or None


def _looks_like_declared_name(value: str) -> bool:
    """Whether a declared value has a shape a real name can have."""

    name = _condensed(value).strip(_PROFILE_NAME_EDGE)
    if not name:
        return False
    if name.endswith(_PROFILE_NAME_REJECT_ENDINGS):
        return False
    if any(name.startswith(item) for item in _PROFILE_NAME_REJECT_OPENERS):
        return False
    if any(item in name for item in _PROFILE_NAME_REJECT_IDENTITIES):
        return False
    return not any(item in name for item in _PROFILE_NAME_REJECT_ANYWHERE)


def _names_the_user(text: str) -> bool:
    """Whether a declaration reads as a name and is therefore safe to slot."""

    if not _profile_declaration_is_standalone(text):
        return False
    declared = _declared_name(text)
    return declared is not None and _looks_like_declared_name(declared)


def slot_residual(text: str, slot: str | None) -> str | None:
    """Return the durable content a write to ``slot`` would otherwise drop.

    Slot records are single-valued, so revising one replaces its text outright.
    When the text it holds says something the slot cannot keep -- the
    communication-style preference bundled into a name record -- that content has
    to be written back as a plain memory instead of vanishing with the replace.
    """

    if slot != "profile.user_name" or _profile_declaration_is_standalone(text):
        return None
    residual = profile_declaration_residual(text)
    if residual is None:
        return None
    return residual.strip(_PROFILE_NOISE_CHARS) or None


def _chinese_interrogative(value: str) -> bool:
    """Return whether value carries a Chinese interrogative that asks something.

    Non-question collocations are stripped first so that durable statements which
    merely contain one of these words ("无论什么情况都要先备份") are not misread.
    """

    scrubbed = value
    for exempt in _CHINESE_INTERROGATIVE_EXEMPTIONS:
        scrubbed = scrubbed.replace(exempt, "")
    return _CHINESE_INTERROGATIVE.search(scrubbed) is not None


def is_question_like(text: str) -> bool:
    """Return whether text is phrased as a question rather than a durable fact.

    A question can be marked explicitly ("我叫什么名字" / "what is my name"), or be written
    in declarative form with the interrogative inside the sentence
    ("我喜欢用什么工具编码").  Both shapes have to be recognized, because a question
    stored as a preference is pollution: it is injected into later contexts as if
    it were a durable fact.
    """

    value = _normalized(text).strip()
    if not value:
        return False
    if "?" in value or "？" in value:
        return True
    if any(marker in value for marker in ("叫什么", "叫啥")):
        return True
    if _CHINESE_QUESTION_ENDING.search(value):
        return True
    if _chinese_interrogative(value):
        return True
    if any(marker in value for marker in ("是不是", "是否", "能否", "可否")):
        return True
    if value.startswith(("请问", "你知道", "你还记得", "你记得")):
        return True
    return bool(_ENGLISH_QUESTION_START.match(value))


def preference_slot(text: str) -> str | None:
    """Map stable profile, preference, and development fields to one slot."""

    value = _normalized(text)
    if not is_question_like(value) and _names_the_user(text):
        return "profile.user_name"

    if any(item in value for item in ("时区", "timezone", "time zone")):
        return "schedule.timezone"
    if any(item in value for item in ("每周第一天", "一周从", "week starts")):
        return "schedule.week_start"
    if any(
        item in value
        for item in ("默认提醒时间", "提醒默认时间", "default reminder time")
    ):
        return "schedule.default_reminder_time"

    wallpaper = ("壁纸", "背景图", "wallpaper", "desktop background")
    color = (
        "配色",
        "颜色",
        "主题",
        "浅色",
        "深色",
        "color scheme",
        "colour scheme",
        "theme",
        "light mode",
        "dark mode",
    )
    terminal = ("终端", "系统偏好", "terminal", "shell", "system preference")
    interface = ("界面", "系统外观", "ui", "interface", "system appearance")
    if any(item in value for item in wallpaper) and any(item in value for item in color):
        return "appearance.wallpaper_color_scheme"
    if any(item in value for item in terminal) and any(item in value for item in color):
        return "appearance.terminal_color_scheme"
    if any(item in value for item in interface) and any(item in value for item in color):
        return "appearance.interface_color_scheme"
    if any(
        item in value
        for item in (
            "浏览器",
            "browser",
            "firefox",
            "chromium",
            "chrome",
            "edge",
            "safari",
        )
    ):
        return "application.default_browser"
    if any(item in value for item in ("回答语言", "回复语言", "response language")):
        return "interaction.response_language"

    if any(item in value for item in ("项目模板", "工程模板", "脚手架", "project template", "scaffold")):
        return "development.project_template"
    if any(item in value for item in ("依赖管理", "包管理", "package manager")):
        return "development.package_manager"
    command_context = any(
        item in value
        for item in ("命令", "command", "执行命令", "run command")
    )
    if command_context:
        command_slots = (
            ("test", ("测试", "test", "pytest", "cargo test", "npm test")),
            ("build", ("构建", "编译", "build", "cargo build")),
            ("format", ("格式化", "format", "cargo fmt", "rustfmt", "prettier")),
            ("lint", ("检查", "lint", "clippy", "ruff", "eslint")),
            ("run", ("启动", "运行", "serve", "run", "dev server")),
            ("install", ("安装依赖", "install", "sync", "npm ci", "uv sync")),
        )
        for name, markers in command_slots:
            if any(marker in value for marker in markers):
                return f"development.command.{name}"
    if any(item in value for item in ("测试框架", "test framework", "pytest", "vitest", "jest")):
        return "development.test_framework"
    if any(item in value for item in ("格式化工具", "formatter", "rustfmt", "black", "prettier")):
        return "development.formatter"
    if any(item in value for item in ("代码检查", "静态检查", "linter", "clippy", "ruff", "eslint")):
        return "development.linter"
    if any(item in value for item in ("代码缩进", "indentation", "indent width")):
        return "development.indentation"
    if any(item in value for item in ("行宽", "每行字符", "line length", "line width")):
        return "development.line_length"
    if any(item in value for item in ("命名风格", "命名规范", "naming convention")):
        return "development.naming_convention"
    if any(item in value for item in ("代码风格", "编码风格", "code style")):
        return "development.code_style"
    if any(item in value for item in ("编辑器", "ide", "editor", "vscode", "vim", "neovim")):
        return "development.editor"
    if any(item in value for item in ("默认 shell", "常用 shell", "powershell", "bash", "zsh")):
        return "development.shell"

    version_context = any(
        item in value
        for item in ("版本", "version", "runtime", "工具链", "toolchain")
    ) or bool(re.search(r"\b(?:python|rust|node(?:\.js)?|java)\s*v?\d", value))
    if version_context:
        runtimes = (
            ("python", ("python",)),
            ("rust", ("rust", "rustc")),
            ("node", ("node", "node.js")),
            ("java", ("java", "jdk")),
            ("go", ("golang", "go ")),
        )
        for name, markers in runtimes:
            if any(marker in value for marker in markers):
                return f"development.runtime_version.{name}"

    languages = (
        "rust",
        "python",
        "javascript",
        "typescript",
        "java",
        "golang",
        "c++",
        "编程语言",
        "开发语言",
    )
    development = ("开发", "编程", "development", "programming", "coding")
    if any(item in value for item in languages) and any(
        item in value for item in development
    ):
        return "development.programming_language"
    return None


def memory_slot(tags: object, text: str) -> str | None:
    for raw_tag in tags or ():
        tag = str(raw_tag)
        if tag.startswith("slot:") and len(tag) > len("slot:"):
            return tag.removeprefix("slot:")
    return preference_slot(text)


__all__ = [
    "L3_FIELD_DICTIONARY",
    "MemoryFieldSpec",
    "CONTENT_LIMITS",
    "content_limit",
    "field_dictionary",
    "field_spec",
    "field_value_failure",
    "is_question_like",
    "known_slots",
    "memory_slot",
    "normalize_field_value",
    "preference_slot",
    "profile_declaration_residual",
    "resolve_field_value",
    "resolve_slot",
    "slot_residual",
    "validate_memory_text",
]
