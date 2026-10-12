"""Recognize an agent that refused a turn because its plan's usage ran out.

A subscription agent that hits its 5-hour or weekly limit fails the turn
with a JSON-RPC error. Each adapter marks it in the error's ``data``:
claude-agent-acp sends ``{"errorKind": "rate_limit"}`` and codex-acp sends
``{"codexErrorInfo": "usageLimitExceeded"}``. Claude also reports the reset
as an epoch in a ``usage_update`` (:data:`CLAUDE_RATE_LIMIT_META`). Other
agents only say it in prose, so :func:`parse_reset` reads the common
phrasings: "resets 3pm", "try again at 3:05 PM", "try again in 2 hours".
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CLAUDE_RATE_LIMIT_META = "_claude/rateLimit"

_LIMIT_TEXT = re.compile(
    r"usage limit|hit your (?:usage )?limit|(?:5-hour|weekly|session) limit"
    r"|out of (?:usage|credits)",
    re.IGNORECASE,
)

_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
_DURATION = re.compile(
    r"\bin\s+((?:\d+\s*(?:days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b"
    r"[\s,]*(?:and\s+)?)+)",
    re.IGNORECASE,
)
_DURATION_PART = re.compile(r"(\d+)\s*([dhms])", re.IGNORECASE)
_CLOCK = re.compile(
    r"\b(?:resets?|again)\s+(?:at\s+)?"
    r"(?:(?P<month>[A-Z][a-z]{2,8})\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+"
    r"(?:(?P<year>\d{4}),?\s+)?(?:at\s+)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap])?\.?m?\.?"
    r"(?:\s*\((?P<tz>[^)]+)\))?",
    re.IGNORECASE,
)
_MONTHS = {
    name: i
    for i, names in enumerate(
        [("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
         ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
         ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
         ("dec", "december")],
        start=1,
    )
    for name in names
}


def is_usage_limit(data: Any, message: str) -> bool:
    """Say whether a failed turn's error means the plan's usage ran out.

    :param data: the JSON-RPC error's ``data`` member.
    :param message: the error message and any text the agent sent with it.
    :returns: ``True`` for a usage limit, ``False`` for any other failure.
    """
    if isinstance(data, dict):
        if data.get("errorKind") == "rate_limit":
            return True
        if data.get("codexErrorInfo") == "usageLimitExceeded":
            return True
    return bool(_LIMIT_TEXT.search(message))


def claude_reset(meta: Any) -> datetime | None:
    """Read the reset time from a Claude ``usage_update``'s ``_meta``.

    :param meta: the update's ``_meta`` mapping.
    :returns: when the rejected limit resets, or ``None`` when the update
        reports no rejected limit.
    """
    if not isinstance(meta, dict):
        return None
    info = meta.get(CLAUDE_RATE_LIMIT_META)
    if not isinstance(info, dict) or info.get("status") != "rejected":
        return None
    resets_at = info.get("resetsAt")
    if not isinstance(resets_at, int | float):
        return None
    return datetime.fromtimestamp(resets_at, UTC)


def parse_reset(text: str, now: datetime) -> datetime | None:
    """Find when a limit resets in an agent's own words.

    A clock time with no date means its next occurrence. A clock time with
    no zone, and no zone in parentheses, is read in ``now``'s zone.

    :param text: the agent's error message and output.
    :param now: the current time, timezone-aware.
    :returns: the reset time, or ``None`` when the text names none.
    """
    duration = _DURATION.search(text)
    if duration:
        seconds = sum(
            int(n) * _UNIT_SECONDS[unit.lower()]
            for n, unit in _DURATION_PART.findall(duration.group(1))
        )
        if seconds:
            return now + timedelta(seconds=seconds)

    clock = _CLOCK.search(text)
    if not clock:
        return None
    hour = int(clock["hour"])
    minute = int(clock["minute"] or 0)
    if clock["ampm"]:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if clock["ampm"].lower() == "p" else 0)
    elif clock["minute"] is None:
        # A bare number with neither minutes nor am/pm is not a time.
        return None
    if hour > 23 or minute > 59:
        return None

    zone = now.tzinfo
    if clock["tz"]:
        try:
            zone = ZoneInfo(clock["tz"].strip())
        except (ZoneInfoNotFoundError, ValueError):
            pass
    local_now = now.astimezone(zone)
    if clock["month"] and clock["month"].lower().rstrip(".") in _MONTHS:
        month = _MONTHS[clock["month"].lower().rstrip(".")]
        year = int(clock["year"]) if clock["year"] else local_now.year
        try:
            return local_now.replace(
                year=year, month=month, day=int(clock["day"]),
                hour=hour, minute=minute, second=0, microsecond=0,
            )
        except ValueError:
            return None
    reset = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset <= local_now:
        reset += timedelta(days=1)
    return reset
