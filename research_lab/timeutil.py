"""Timezone-aware UTC timestamps. Naive datetimes are rejected."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemUTCClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FrozenClock:
    def __init__(self, instant: datetime) -> None:
        self._now = as_utc(instant)

    def now(self) -> datetime:
        return self._now

    def set(self, instant: datetime) -> None:
        self._now = as_utc(instant)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("naive datetime rejected; timestamps must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


def parse_utc(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return as_utc(parsed)


def isoformat_utc(value: datetime) -> str:
    return as_utc(value).isoformat()
