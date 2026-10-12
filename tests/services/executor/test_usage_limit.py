"""Recognizing a usage limit and when it resets."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from flow_atelier.services.executor.usage_limit import (
    claude_reset,
    is_usage_limit,
    parse_reset,
)

BOGOTA = timezone(timedelta(hours=-5))
NOW = datetime(2026, 10, 12, 14, 30, tzinfo=BOGOTA)


def test_claude_and_codex_error_data_mark_a_usage_limit() -> None:
    """Each adapter's structured error data is recognized without any text."""
    assert is_usage_limit({"errorKind": "rate_limit"}, "Internal error")
    assert is_usage_limit({"codexErrorInfo": "usageLimitExceeded"}, "Internal error")


def test_other_failures_are_not_usage_limits() -> None:
    """Auth, overload and plain failures do not wait for a reset."""
    assert not is_usage_limit({"errorKind": "authentication_failed"}, "Internal error")
    assert not is_usage_limit({"errorKind": "overloaded"}, "Overloaded")
    assert not is_usage_limit(None, "context length exceeded")


def test_a_usage_limit_named_only_in_prose_is_recognized() -> None:
    """An agent with no structured data is recognized from its message."""
    assert is_usage_limit(None, "You've hit your usage limit. Try again later.")


def test_claude_meta_gives_the_reset_of_a_rejected_limit() -> None:
    """A rejected rate-limit report carries the reset as an epoch."""
    meta = {"_claude/rateLimit": {"status": "rejected", "resetsAt": 1791837000}}
    assert claude_reset(meta) == datetime.fromtimestamp(1791837000, UTC)


def test_claude_meta_ignores_a_limit_that_still_allows_turns() -> None:
    """A warning is not a refusal, so it sets no reset."""
    meta = {"_claude/rateLimit": {"status": "allowed_warning", "resetsAt": 1791837000}}
    assert claude_reset(meta) is None
    assert claude_reset(None) is None


def test_a_relative_reset_counts_from_now() -> None:
    """"try again in 2 hours 13 minutes" is that long from now."""
    text = "You've hit your usage limit. Try again in 2 hours 13 minutes."
    assert parse_reset(text, NOW) == NOW + timedelta(hours=2, minutes=13)


def test_a_clock_reset_later_today_is_today() -> None:
    """"resets 3pm" after 2:30pm means 3pm today."""
    assert parse_reset("5-hour limit reached · resets 3pm", NOW) == NOW.replace(
        hour=15, minute=0
    )


def test_a_clock_reset_already_past_is_tomorrow() -> None:
    """"try again at 9:05 AM" at 2:30pm means tomorrow morning."""
    expected = (NOW + timedelta(days=1)).replace(hour=9, minute=5)
    assert parse_reset("Please try again at 9:05 AM.", NOW) == expected


def test_a_reset_in_a_named_zone_uses_that_zone() -> None:
    """"resets 11pm (UTC)" is 11pm UTC, whatever the local zone."""
    reset = parse_reset("Claude usage limit reached; resets 11pm (UTC)", NOW)
    assert reset == datetime(2026, 10, 12, 23, 0, tzinfo=UTC)


def test_a_reset_with_a_date_uses_that_date() -> None:
    """"try again at Oct 14th, 2026 3:05 PM" names the day too."""
    reset = parse_reset("try again at Oct 14th, 2026 3:05 PM.", NOW)
    assert reset == datetime(2026, 10, 14, 15, 5, tzinfo=BOGOTA)


def test_text_without_a_time_gives_no_reset() -> None:
    """A bare number is not a clock time."""
    assert parse_reset("You've hit your usage limit.", NOW) is None
    assert parse_reset("resets after 5 tasks", NOW) is None
