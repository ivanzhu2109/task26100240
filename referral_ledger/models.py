"""类型定义：状态枚举、结果与快照数据结构。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional


class ReferralStatus(str, enum.Enum):
    UNHIRED = "unhired"
    HIRED = "hired"


class BonusStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    PAID_OUT = "paid_out"
    CANCELLED = "cancelled"


class ErrorCode(str, enum.Enum):
    REFERRAL_NOT_FOUND = "referral_not_found"
    BONUS_NOT_FOUND = "bonus_not_found"
    VERSION_CONFLICT = "version_conflict"
    NO_ACTIVE_STINT = "no_active_stint"
    BONUS_NOT_PENDING = "bonus_not_pending"
    BONUS_NOT_APPROVED = "bonus_not_approved"
    STINT_NOT_MATURE = "stint_not_mature"
    REQUEST_ID_CONFLICT = "request_id_conflict"


@dataclass(frozen=True)
class Result:
    """写操作的统一返回。ok=False 时 error 为 ErrorCode 字符串。"""
    ok: bool
    error: Optional[str] = None
    version: Optional[int] = None
    bonus_id: Optional[str] = None


@dataclass(frozen=True)
class BonusInfo:
    bonus_id: str
    stint_id: int
    status: str
    amount_cents: int
    created_at: str
    approved_at: Optional[str]
    paid_at: Optional[str]
    cancelled_at: Optional[str]


@dataclass(frozen=True)
class StintInfo:
    stint_id: int
    hired_at: str
    unhired_at: Optional[str]
    active: bool


@dataclass(frozen=True)
class ReferralView:
    referral_id: str
    employee_id: str
    amount_cents: int
    status: str
    version: int
    current_stint_id: Optional[int]
    stints: tuple = field(default_factory=tuple)
    bonuses: tuple = field(default_factory=tuple)
