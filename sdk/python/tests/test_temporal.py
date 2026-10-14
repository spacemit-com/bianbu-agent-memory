from datetime import datetime

from bianbu_agent_sdk.memory.temporal import (
    extract_reminder,
    format_epoch_ms_display,
    temporal_annotation,
    normalize_relative_dates,
)


def test_relative_date_normalization_preserves_two_activities_and_midnight():
    anchor = datetime.fromisoformat("2026-12-31T23:59:00+08:00")
    value = normalize_relative_dates("帮我记住明天早上9点开会，晚上7点瑜伽", anchor=anchor)
    assert value == "帮我记住2027-01-01早上9点开会，晚上7点瑜伽"
    assert normalize_relative_dates(value, anchor=datetime.fromisoformat("2027-01-02T10:00:00+08:00")) == value


def test_reminder_is_materialized_against_recording_day() -> None:
    anchor = datetime.fromisoformat("2026-08-12T20:04:00+08:00")
    result = extract_reminder("记得明天上午9点提醒我完成工程对接", anchor=anchor)
    assert result is not None
    assert result.due_at.isoformat() == "2026-08-13T09:00:00+08:00"
    assert "2026-08-13T09:00+08:00" in result.canonical_text


def test_historical_relative_word_is_not_reinterpreted_on_next_day() -> None:
    annotation = temporal_annotation(
        "明天上午9点提醒我完成工程对接",
        recorded_at_ms=1786536240000,  # 2026-08-12 20:04 Asia/Shanghai
        current=datetime.fromisoformat("2026-08-13T09:21:00+08:00"),
    )
    assert "明天->2026-08-13(today)" in annotation


def test_persisted_stamps_have_a_ready_to_show_form() -> None:
    # The display value is what a model copies to the user, so it must already
    # be the local minute a reader recognises, with nothing left to restyle.
    assert format_epoch_ms_display(1789893195471) == "2026-09-20 16:33"
