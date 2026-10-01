"""可注入时钟：所有时间均为 UTC，假定时钟不倒退。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    """时钟协议：返回当前 UTC 时间（tz-aware）。"""

    def now(self) -> datetime:
        ...


class SystemClock:
    """真实系统时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock:
    """测试用手动时钟，可自由推进。"""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware (UTC)")
        self._now = start.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value.astimezone(timezone.utc)

    def advance(self, delta) -> None:
        self._now = self._now + delta
