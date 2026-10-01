"""可注入时钟：生产用系统时钟，测试用手动时钟。时间一律为 UTC。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """返回当前 UTC 时间（tz-aware）。假定不倒退。"""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock:
    """测试用：可手动推进的时钟。"""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware (UTC)")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("value must be timezone-aware (UTC)")
        self._now = value

    def advance(self, **kwargs) -> None:
        from datetime import timedelta
        self._now = self._now + timedelta(**kwargs)
