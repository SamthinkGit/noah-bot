"""Scheduling helpers for the Disboard autobump loop.

Disboard's cooldown is two hours, so the loop waits a random interval between
three and four hours: it never hits the cooldown and it never bumps on a
predictable clock.
"""

import random
from datetime import datetime, timedelta, timezone


DISBOARD_APPLICATION_ID = "302050872383242240"
BUMP_COMMAND_NAME = "bump"
BUMP_MIN_SECONDS = 3 * 60 * 60
BUMP_MAX_SECONDS = 4 * 60 * 60
BUMP_RETRY_SECONDS = 10 * 60


def random_bump_delay() -> float:
    return random.uniform(BUMP_MIN_SECONDS, BUMP_MAX_SECONDS)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def next_bump_timestamp(delay_seconds: float) -> str:
    return (utc_now() + timedelta(seconds=delay_seconds)).isoformat()


def seconds_until(timestamp: str | None) -> float:
    """Seconds left until a stored ISO timestamp, clamped to zero."""

    if not timestamp:
        return 0.0

    try:
        scheduled = datetime.fromisoformat(timestamp)
    except ValueError:
        return 0.0

    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=timezone.utc)

    return max((scheduled - utc_now()).total_seconds(), 0.0)


def format_delay(delay_seconds: float) -> str:
    total_minutes = int(delay_seconds // 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}min"
    if hours:
        return f"{hours}h"
    return f"{minutes}min"


def discord_timestamp(delay_seconds: float) -> str:
    epoch_seconds = int((utc_now() + timedelta(seconds=delay_seconds)).timestamp())
    return f"<t:{epoch_seconds}:R>"
