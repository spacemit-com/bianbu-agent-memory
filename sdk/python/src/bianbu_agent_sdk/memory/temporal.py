from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import os
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from datetime import tzinfo


DEFAULT_TIMEZONE = "Asia/Shanghai"

_RELATIVE_DAY_PATTERN = re.compile(
    r"(?P<zh>大后天|前天|后天|明天|昨天|今天)"
    r"|(?P<en>day after tomorrow|tomorrow|yesterday|today)",
    re.IGNORECASE,
)
_RELATIVE_DAY_OFFSETS = {
    "前天": -2,
    "昨天": -1,
    "今天": 0,
    "明天": 1,
    "后天": 2,
    "大后天": 3,
    "yesterday": -1,
    "today": 0,
    "tomorrow": 1,
    "day after tomorrow": 2,
}
_ABSOLUTE_DATE_PATTERN = re.compile(
    r"(?:(?P<year>20\d{2})[年\-/\.])?"
    r"(?P<month>1[0-2]|0?[1-9])[月\-/\.]"
    r"(?P<day>3[01]|[12]\d|0?[1-9])日?"
)
_TIME_PATTERN = re.compile(
    r"(?:(?P<period>凌晨|早上|上午|中午|下午|傍晚|晚上)\s*)?"
    r"(?P<hour>2[0-3]|[01]?\d)"
    r"(?:[:：点时]\s*(?P<minute>[0-5]?\d)?\s*分?)"
)
_REMINDER_INTENT = re.compile(
    r"(?:提醒我|请.{0,6}提醒|记得.{0,12}提醒|设置.{0,8}提醒|"
    r"remind\s+me|set\s+(?:a\s+)?reminder)",
    re.IGNORECASE,
)
_QUESTION_ENDING = re.compile(r"(?:[?？]|吗|么|呢)\s*$")


@dataclass(frozen=True)
class DateResolution:
    phrase: str
    value: date


@dataclass(frozen=True)
class ReminderResolution:
    original_text: str
    due_at: datetime
    timezone_name: str

    @property
    def canonical_text(self) -> str:
        return (
            f"提醒事项：计划时间 {self.due_at.isoformat(timespec='minutes')} "
            f"({self.timezone_name})；原始内容：{self.original_text}"
        )


def timezone_name() -> str:
    return os.environ.get("BIANBU_MEMORY_TIMEZONE", DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE


def local_timezone(name: str | None = None) -> tzinfo:
    selected = name or timezone_name()
    try:
        return ZoneInfo(selected)
    except ZoneInfoNotFoundError:
        # Minimal images and Windows source checkouts may not ship the tzdata
        # package. China has no DST, so a fixed UTC+8 fallback is deterministic.
        if selected == DEFAULT_TIMEZONE:
            return timezone(timedelta(hours=8), name=DEFAULT_TIMEZONE)
        return timezone.utc


def now_local(name: str | None = None) -> datetime:
    return datetime.now(local_timezone(name))


def from_epoch_ms(value: int, name: str | None = None) -> datetime:
    return datetime.fromtimestamp(value / 1000, timezone.utc).astimezone(
        local_timezone(name)
    )


def format_epoch_ms_display(value: int, name: str | None = None) -> str:
    """Render an epoch-millisecond stamp the way a person reads a clock.

    A bare epoch integer, and even an offset-qualified ISO-8601 string, are
    machine shapes: a model asked to quote one restyles it on its own, so the
    same instant reaches users as "2026-09-24T11:31:01+08:00", "2026-09-24
    11:31" or a date written out in prose, depending on the run. Handing
    callers a finished rendering removes that freedom. No offset is appended
    because the reader is the user of this device, whose clock is already the
    local one.
    """

    return from_epoch_ms(int(value), name).strftime("%Y-%m-%d %H:%M")


def resolve_dates(text: str, anchor: datetime) -> tuple[DateResolution, ...]:
    values: list[DateResolution] = []
    seen: set[tuple[str, date]] = set()
    for match in _RELATIVE_DAY_PATTERN.finditer(text):
        phrase = str(match.group(0))
        key = phrase.casefold()
        value = anchor.date() + timedelta(days=_RELATIVE_DAY_OFFSETS[key])
        identity = (key, value)
        if identity not in seen:
            values.append(DateResolution(phrase, value))
            seen.add(identity)
    for match in _ABSOLUTE_DATE_PATTERN.finditer(text):
        year = int(match.group("year") or anchor.year)
        try:
            value = date(year, int(match.group("month")), int(match.group("day")))
        except ValueError:
            continue
        phrase = str(match.group(0))
        identity = (phrase.casefold(), value)
        if identity not in seen:
            values.append(DateResolution(phrase, value))
            seen.add(identity)
    return tuple(values)


def normalize_relative_dates(text: str, *, anchor: datetime | None = None) -> str:
    """Resolve relative days once at ingestion; preserve every stated clock."""
    anchored = anchor or now_local()
    return _RELATIVE_DAY_PATTERN.sub(
        lambda match: (anchored.date() + timedelta(
            days=_RELATIVE_DAY_OFFSETS[match.group(0).casefold()]
        )).isoformat(), text,
    )


def _resolved_clock(text: str) -> time:
    match = _TIME_PATTERN.search(text)
    if match is None:
        return time(9, 0)
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    period = str(match.group("period") or "")
    if period in {"下午", "傍晚", "晚上"} and hour < 12:
        hour += 12
    elif period == "中午" and hour < 11:
        hour += 12
    elif period == "凌晨" and hour == 12:
        hour = 0
    return time(hour, minute)


def extract_reminder(
    text: str,
    *,
    anchor: datetime | None = None,
) -> ReminderResolution | None:
    normalized = text.strip()
    if (
        not normalized
        or _REMINDER_INTENT.search(normalized) is None
        or _QUESTION_ENDING.search(normalized) is not None
    ):
        return None
    anchored = anchor or now_local()
    dates = resolve_dates(normalized, anchored)
    if not dates:
        return None
    due_at = datetime.combine(
        dates[0].value,
        _resolved_clock(normalized),
        tzinfo=anchored.tzinfo,
    )
    return ReminderResolution(normalized, due_at, timezone_name())


def temporal_annotation(
    text: str,
    *,
    recorded_at_ms: int,
    current: datetime | None = None,
) -> str:
    recorded = from_epoch_ms(recorded_at_ms)
    now = current or now_local()
    values = resolve_dates(text, recorded)
    parts = [f"recorded_at={recorded.isoformat(timespec='seconds')}"]
    if values:
        resolved = []
        for item in values:
            delta = (item.value - now.date()).days
            relation = (
                "today"
                if delta == 0
                else "tomorrow"
                if delta == 1
                else "yesterday"
                if delta == -1
                else f"{delta:+d}d_from_today"
            )
            resolved.append(f"{item.phrase}->{item.value.isoformat()}({relation})")
        parts.append("relative_dates=" + ",".join(resolved))
    return " ".join(parts)


def current_time_context(current: datetime | None = None) -> str:
    value = current or now_local()
    return (
        f"[current_local_time={value.isoformat(timespec='seconds')} "
        f"timezone={timezone_name()}] Historical words such as 明天/tomorrow are "
        "anchored to each memory item's recorded_at and relative_dates metadata; "
        "do not reinterpret them relative to the current request."
    )


__all__ = [
    "DateResolution",
    "ReminderResolution",
    "current_time_context",
    "extract_reminder",
    "format_epoch_ms_display",
    "from_epoch_ms",
    "now_local",
    "normalize_relative_dates",
    "resolve_dates",
    "temporal_annotation",
    "timezone_name",
]
