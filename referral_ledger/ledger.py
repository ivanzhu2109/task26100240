"""员工推荐奖金账本。

并发与幂等：每个写操作在单个 BEGIN IMMEDIATE 事务内完成
「查幂等记录 -> 执行业务逻辑 -> 记录结果」，SQLite 写锁保证
多实例间的串行化与原子可见性。
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta
from typing import Callable, Iterable, Optional, Tuple

from .clock import Clock, SystemClock
from .models import (
    BonusInfo,
    BonusStatus,
    ErrorCode,
    ReferralStatus,
    ReferralView,
    Result,
    StintInfo,
)
from .storage import connect

MATURITY = timedelta(days=90)


class Ledger:
    def __init__(
        self,
        db_path: str,
        initial_referrals: Iterable[Tuple[str, str, int]] = (),
        clock: Optional[Clock] = None,
    ) -> None:
        self._conn = connect(db_path)
        self._clock = clock if clock is not None else SystemClock()
        self._init_referrals(initial_referrals)

    # ---------- 初始化 ----------

    def _init_referrals(self, initial_referrals) -> None:
        rows = [
            (referral_id, employee_id, int(amount_cents))
            for referral_id, employee_id, amount_cents in initial_referrals
        ]
        if not rows:
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.executemany(
                "INSERT OR IGNORE INTO referrals"
                " (referral_id, employee_id, amount_cents, status, version,"
                "  current_stint_id, has_paid_out)"
                " VALUES (?, ?, ?, ?, 0, NULL, 0)",
                [(rid, eid, amt, ReferralStatus.UNHIRED.value) for rid, eid, amt in rows],
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # ---------- 幂等执行框架 ----------

    def _run_idempotent(
        self, command: str, request_id: str, args: dict, fn: Callable[[], Result]
    ) -> Result:
        args_json = json.dumps(args, sort_keys=True)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT command, args_json, result_json FROM requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is not None:
                self._conn.execute("COMMIT")
                if row[0] != command or row[1] != args_json:
                    return Result(ok=False, error=ErrorCode.REQUEST_ID_CONFLICT.value)
                return Result(**json.loads(row[2]))
            result = fn()
            self._conn.execute(
                "INSERT INTO requests (request_id, command, args_json, result_json)"
                " VALUES (?, ?, ?, ?)",
                (request_id, command, args_json, json.dumps(dataclasses.asdict(result))),
            )
            self._conn.execute("COMMIT")
            return result
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # ---------- 写操作 ----------

    def record_hiring(
        self, request_id: str, referral_id: str, expected_version: int, target_status: str
    ) -> Result:
        target = ReferralStatus(target_status)
        args = {
            "referral_id": referral_id,
            "expected_version": expected_version,
            "target_status": target.value,
        }

        def fn() -> Result:
            row = self._conn.execute(
                "SELECT status, version, current_stint_id, has_paid_out"
                " FROM referrals WHERE referral_id = ?",
                (referral_id,),
            ).fetchone()
            if row is None:
                return Result(ok=False, error=ErrorCode.REFERRAL_NOT_FOUND.value)
            status, version, stint_id, has_paid_out = row
            if version != expected_version:
                return Result(
                    ok=False, error=ErrorCode.VERSION_CONFLICT.value, version=version
                )
            if status == target.value:
                return Result(ok=True, version=version)

            now_iso = self._clock.now().isoformat()
            new_version = version + 1
            if target is ReferralStatus.HIRED:
                cur = self._conn.execute(
                    "INSERT INTO stints (referral_id, hired_at, active) VALUES (?, ?, 1)",
                    (referral_id, now_iso),
                )
                new_stint_id = cur.lastrowid
                bonus_id = None
                if not has_paid_out:
                    bonus_id = f"{referral_id}:stint:{new_stint_id}"
                    self._conn.execute(
                        "INSERT INTO bonuses"
                        " (bonus_id, referral_id, stint_id, status, amount_cents, created_at)"
                        " SELECT ?, ?, ?, ?, amount_cents, ? FROM referrals"
                        " WHERE referral_id = ?",
                        (
                            bonus_id,
                            referral_id,
                            new_stint_id,
                            BonusStatus.PENDING.value,
                            now_iso,
                            referral_id,
                        ),
                    )
                self._conn.execute(
                    "UPDATE referrals SET status = ?, version = ?, current_stint_id = ?"
                    " WHERE referral_id = ?",
                    (ReferralStatus.HIRED.value, new_version, new_stint_id, referral_id),
                )
                return Result(ok=True, version=new_version, bonus_id=bonus_id)

            # 撤销录用：关闭当前轮次并取消其 pending/approved 奖金
            self._conn.execute(
                "UPDATE stints SET active = 0, unhired_at = ? WHERE stint_id = ?",
                (now_iso, stint_id),
            )
            self._conn.execute(
                "UPDATE bonuses SET status = ?, cancelled_at = ?"
                " WHERE stint_id = ? AND status IN (?, ?)",
                (
                    BonusStatus.CANCELLED.value,
                    now_iso,
                    stint_id,
                    BonusStatus.PENDING.value,
                    BonusStatus.APPROVED.value,
                ),
            )
            self._conn.execute(
                "UPDATE referrals SET status = ?, version = ?, current_stint_id = NULL"
                " WHERE referral_id = ?",
                (ReferralStatus.UNHIRED.value, new_version, referral_id),
            )
            return Result(ok=True, version=new_version)

        return self._run_idempotent("record_hiring", request_id, args, fn)

    def approve(self, request_id: str, bonus_id: str) -> Result:
        args = {"bonus_id": bonus_id}

        def fn() -> Result:
            row = self._conn.execute(
                "SELECT b.status, b.stint_id, s.active, s.hired_at"
                " FROM bonuses b JOIN stints s ON s.stint_id = b.stint_id"
                " WHERE b.bonus_id = ?",
                (bonus_id,),
            ).fetchone()
            if row is None:
                return Result(ok=False, error=ErrorCode.BONUS_NOT_FOUND.value)
            status, _stint_id, active, hired_at = row
            if status != BonusStatus.PENDING.value:
                return Result(ok=False, error=ErrorCode.BONUS_NOT_PENDING.value)
            if not active:
                return Result(ok=False, error=ErrorCode.NO_ACTIVE_STINT.value)
            now = self._clock.now()
            hired_dt = datetime.fromisoformat(hired_at)
            if now - hired_dt < MATURITY:
                return Result(ok=False, error=ErrorCode.STINT_NOT_MATURE.value)
            self._conn.execute(
                "UPDATE bonuses SET status = ?, approved_at = ? WHERE bonus_id = ?",
                (BonusStatus.APPROVED.value, now.isoformat(), bonus_id),
            )
            return Result(ok=True, bonus_id=bonus_id)

        return self._run_idempotent("approve", request_id, args, fn)

    def pay(self, request_id: str, bonus_id: str) -> Result:
        args = {"bonus_id": bonus_id}

        def fn() -> Result:
            row = self._conn.execute(
                "SELECT b.status, b.referral_id, s.active"
                " FROM bonuses b JOIN stints s ON s.stint_id = b.stint_id"
                " WHERE b.bonus_id = ?",
                (bonus_id,),
            ).fetchone()
            if row is None:
                return Result(ok=False, error=ErrorCode.BONUS_NOT_FOUND.value)
            status, referral_id, active = row
            if status != BonusStatus.APPROVED.value:
                return Result(ok=False, error=ErrorCode.BONUS_NOT_APPROVED.value)
            if not active:
                return Result(ok=False, error=ErrorCode.NO_ACTIVE_STINT.value)
            now_iso = self._clock.now().isoformat()
            self._conn.execute(
                "UPDATE bonuses SET status = ?, paid_at = ? WHERE bonus_id = ?",
                (BonusStatus.PAID_OUT.value, now_iso, bonus_id),
            )
            self._conn.execute(
                "UPDATE referrals SET has_paid_out = 1 WHERE referral_id = ?",
                (referral_id,),
            )
            return Result(ok=True, bonus_id=bonus_id)

        return self._run_idempotent("pay", request_id, args, fn)

    def inspect(self, referral_id: str) -> Optional[ReferralView]:
        row = self._conn.execute(
            "SELECT referral_id, employee_id, amount_cents, status, version,"
            " current_stint_id FROM referrals WHERE referral_id = ?",
            (referral_id,),
        ).fetchone()
        if row is None:
            return None
        stints = tuple(
            StintInfo(
                stint_id=r[0],
                hired_at=r[1],
                unhired_at=r[2],
                active=bool(r[3]),
            )
            for r in self._conn.execute(
                "SELECT stint_id, hired_at, unhired_at, active FROM stints"
                " WHERE referral_id = ? ORDER BY stint_id",
                (referral_id,),
            )
        )
        bonuses = tuple(
            BonusInfo(
                bonus_id=r[0],
                stint_id=r[1],
                status=r[2],
                amount_cents=r[3],
                created_at=r[4],
                approved_at=r[5],
                paid_at=r[6],
                cancelled_at=r[7],
            )
            for r in self._conn.execute(
                "SELECT bonus_id, stint_id, status, amount_cents, created_at,"
                " approved_at, paid_at, cancelled_at FROM bonuses"
                " WHERE referral_id = ? ORDER BY created_at, bonus_id",
                (referral_id,),
            )
        )
        return ReferralView(
            referral_id=row[0],
            employee_id=row[1],
            amount_cents=row[2],
            status=row[3],
            version=row[4],
            current_stint_id=row[5],
            stints=stints,
            bonuses=bonuses,
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
