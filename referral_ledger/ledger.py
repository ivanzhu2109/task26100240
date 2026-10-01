"""账本核心：SQLite 持久化 + 幂等 + 多实例并发安全。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .clock import Clock, SystemClock

STATUS_UNHIRED = "unhired"
STATUS_HIRED = "hired"

BONUS_PENDING = "pending"
BONUS_APPROVED = "approved"
BONUS_PAID_OUT = "paid_out"
BONUS_CANCELLED = "cancelled"

# 任职满 90 个 24 小时才允许批准（恰好到达边界时允许）
PROBATION = timedelta(days=90)

SCHEMA = """
CREATE TABLE IF NOT EXISTS referrals (
    id              TEXT PRIMARY KEY,
    employee_id     TEXT NOT NULL,
    bonus_amount    INTEGER NOT NULL,
    status          TEXT NOT NULL,
    version         INTEGER NOT NULL,
    current_tenure  INTEGER
);
CREATE TABLE IF NOT EXISTS tenures (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    referral_id     TEXT NOT NULL REFERENCES referrals(id),
    hired_at        TEXT NOT NULL,
    unhired_at      TEXT
);
CREATE TABLE IF NOT EXISTS bonuses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    referral_id     TEXT NOT NULL REFERENCES referrals(id),
    tenure_id       INTEGER NOT NULL REFERENCES tenures(id),
    amount          INTEGER NOT NULL,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    approved_at     TEXT,
    paid_at         TEXT,
    cancelled_at    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_bonus_per_tenure ON bonuses(tenure_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_paid_out_per_referral
    ON bonuses(referral_id) WHERE status = 'paid_out';
CREATE TABLE IF NOT EXISTS idempotency (
    request_id      TEXT PRIMARY KEY,
    operation       TEXT NOT NULL,
    params_json     TEXT NOT NULL,
    result_json     TEXT NOT NULL
);
"""


class RequestIdConflictError(Exception):
    """同一 request_id 被用于不同操作或不同参数。"""


@dataclass(frozen=True)
class InitialReferral:
    """创建账本时的初始推荐记录。"""

    referral_id: str
    employee_id: str
    bonus_amount: int  # 固定的整数分奖金


@dataclass
class OpResult:
    """写操作结果（含业务失败），幂等重放时原样返回。"""

    ok: bool
    operation: str
    error: Optional[str] = None
    data: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "ok": self.ok,
                "operation": self.operation,
                "error": self.error,
                "data": self.data,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> "OpResult":
        obj = json.loads(raw)
        return cls(
            ok=obj["ok"],
            operation=obj["operation"],
            error=obj["error"],
            data=obj["data"],
        )


def _to_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(raw: Optional[str]) -> Optional[datetime]:
    if raw is None:
        return None
    return datetime.fromisoformat(raw).astimezone(timezone.utc)


class Ledger:
    """员工推荐奖金账本。多个实例可并发操作同一 SQLite 文件。"""

    def __init__(
        self,
        db_path: str,
        clock: Optional[Clock] = None,
        initial_referrals: Optional[list[InitialReferral]] = None,
    ) -> None:
        self._db_path = db_path
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._conn = sqlite3.connect(
            db_path, timeout=60, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=60000")
        self._conn.executescript(SCHEMA)
        for ref in initial_referrals or []:
            self._conn.execute(
                "INSERT OR IGNORE INTO referrals"
                " (id, employee_id, bonus_amount, status, version, current_tenure)"
                " VALUES (?, ?, ?, ?, 0, NULL)",
                (ref.referral_id, ref.employee_id, ref.bonus_amount, STATUS_UNHIRED),
            )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- 幂等基础设施 ----

    def _run_idempotent(
        self, operation: str, request_id: str, params: dict, handler
    ) -> OpResult:
        """在单个 IMMEDIATE 事务内完成：幂等检查 -> 执行 -> 记录结果。

        SQLite 写锁保证多实例并发时等价于某个串行执行；
        检查与写入同事务，保证幂等结果原子可见。
        时钟在事务外读取（时钟读数不是命令参数，也不参与并发控制）。
        """
        now = self._clock.now()
        params_json = json.dumps(params, sort_keys=True)
        cur = self._conn
        cur.execute("BEGIN IMMEDIATE")
        try:
            row = cur.execute(
                "SELECT operation, params_json, result_json FROM idempotency"
                " WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is not None:
                if row["operation"] != operation or row["params_json"] != params_json:
                    cur.execute("ROLLBACK")
                    raise RequestIdConflictError(
                        f"request_id {request_id!r} already used for"
                        f" {row['operation']} with different params"
                    )
                result = OpResult.from_json(row["result_json"])
                cur.execute("COMMIT")
                return result
            result = handler(now)
            cur.execute(
                "INSERT INTO idempotency (request_id, operation, params_json,"
                " result_json) VALUES (?, ?, ?, ?)",
                (request_id, operation, params_json, result.to_json()),
            )
            cur.execute("COMMIT")
            return result
        except RequestIdConflictError:
            raise
        except Exception:
            cur.execute("ROLLBACK")
            raise

    # ---- 四个操作（后续步骤填充） ----

    def record_hiring(
        self,
        request_id: str,
        referral_id: str,
        expected_version: int,
        target_status: str,
    ) -> OpResult:
        """录用/撤销录用。版本不符失败；同状态为无副作用成功。"""
        if target_status not in (STATUS_HIRED, STATUS_UNHIRED):
            raise ValueError(f"invalid target_status: {target_status!r}")
        params = {
            "referral_id": referral_id,
            "expected_version": expected_version,
            "target_status": target_status,
        }

        def handler(now: datetime) -> OpResult:
            cur = self._conn
            ref = cur.execute(
                "SELECT * FROM referrals WHERE id = ?", (referral_id,)
            ).fetchone()
            if ref is None:
                return OpResult(False, "record_hiring", "referral_not_found",
                                {"referral_id": referral_id})
            if ref["version"] != expected_version:
                return OpResult(
                    False, "record_hiring", "version_conflict",
                    {"referral_id": referral_id,
                     "version": ref["version"],
                     "status": ref["status"]})
            if ref["status"] == target_status:
                return OpResult(
                    True, "record_hiring",
                    data={"referral_id": referral_id,
                          "version": ref["version"],
                          "status": ref["status"],
                          "changed": False})
            new_version = ref["version"] + 1
            now_s = _to_str(now)
            data: dict = {"referral_id": referral_id,
                          "version": new_version,
                          "status": target_status,
                          "changed": True}
            if target_status == STATUS_HIRED:
                cur.execute(
                    "INSERT INTO tenures (referral_id, hired_at) VALUES (?, ?)",
                    (referral_id, now_s))
                tenure_id = cur.execute(
                    "SELECT last_insert_rowid()").fetchone()[0]
                cur.execute(
                    "UPDATE referrals SET status = ?, version = ?,"
                    " current_tenure = ? WHERE id = ?",
                    (STATUS_HIRED, new_version, tenure_id, referral_id))
                data["tenure_id"] = tenure_id
                data["hired_at"] = now_s
                paid = cur.execute(
                    "SELECT COUNT(*) FROM bonuses WHERE referral_id = ?"
                    " AND status = ?",
                    (referral_id, BONUS_PAID_OUT)).fetchone()[0]
                if paid == 0:
                    cur.execute(
                        "INSERT INTO bonuses (referral_id, tenure_id, amount,"
                        " status, created_at) VALUES (?, ?, ?, ?, ?)",
                        (referral_id, tenure_id, ref["bonus_amount"],
                         BONUS_PENDING, now_s))
                    data["bonus_id"] = cur.execute(
                        "SELECT last_insert_rowid()").fetchone()[0]
            else:
                tenure_id = ref["current_tenure"]
                cur.execute(
                    "UPDATE tenures SET unhired_at = ? WHERE id = ?",
                    (now_s, tenure_id))
                cur.execute(
                    "UPDATE bonuses SET status = ?, cancelled_at = ?"
                    " WHERE tenure_id = ? AND status IN (?, ?)",
                    (BONUS_CANCELLED, now_s, tenure_id,
                     BONUS_PENDING, BONUS_APPROVED))
                cur.execute(
                    "UPDATE referrals SET status = ?, version = ?,"
                    " current_tenure = NULL WHERE id = ?",
                    (STATUS_UNHIRED, new_version, referral_id))
                data["tenure_id"] = tenure_id
                data["unhired_at"] = now_s
            return OpResult(True, "record_hiring", data=data)

        return self._run_idempotent(
            "record_hiring", request_id, params, handler)

    def approve(self, request_id: str, bonus_id: int) -> OpResult:
        """将 pending 奖金改为 approved（需满 90 个 24 小时）。"""
        params = {"bonus_id": bonus_id}

        def handler(now: datetime) -> OpResult:
            cur = self._conn
            bonus = cur.execute(
                "SELECT * FROM bonuses WHERE id = ?", (bonus_id,)
            ).fetchone()
            if bonus is None:
                return OpResult(False, "approve", "bonus_not_found",
                                {"bonus_id": bonus_id})
            if bonus["status"] != BONUS_PENDING:
                return OpResult(False, "approve", "bonus_not_pending",
                                {"bonus_id": bonus_id,
                                 "status": bonus["status"]})
            ref = cur.execute(
                "SELECT * FROM referrals WHERE id = ?",
                (bonus["referral_id"],)).fetchone()
            if ref["current_tenure"] != bonus["tenure_id"]:
                return OpResult(False, "approve", "tenure_not_active",
                                {"bonus_id": bonus_id,
                                 "tenure_id": bonus["tenure_id"]})
            tenure = cur.execute(
                "SELECT * FROM tenures WHERE id = ?",
                (bonus["tenure_id"],)).fetchone()
            hired_at = _parse(tenure["hired_at"])
            eligible_at = hired_at + PROBATION
            if now < eligible_at:
                return OpResult(False, "approve", "probation_not_elapsed",
                                {"bonus_id": bonus_id,
                                 "hired_at": tenure["hired_at"],
                                 "eligible_at": _to_str(eligible_at)})
            now_s = _to_str(now)
            cur.execute(
                "UPDATE bonuses SET status = ?, approved_at = ? WHERE id = ?",
                (BONUS_APPROVED, now_s, bonus_id))
            return OpResult(True, "approve",
                            data={"bonus_id": bonus_id,
                                  "status": BONUS_APPROVED,
                                  "approved_at": now_s})

        return self._run_idempotent("approve", request_id, params, handler)

    def pay(self, request_id: str, bonus_id: int) -> OpResult:
        """将 approved 奖金改为 paid_out（仅本地记账）。"""
        params = {"bonus_id": bonus_id}

        def handler(now: datetime) -> OpResult:
            cur = self._conn
            bonus = cur.execute(
                "SELECT * FROM bonuses WHERE id = ?", (bonus_id,)
            ).fetchone()
            if bonus is None:
                return OpResult(False, "pay", "bonus_not_found",
                                {"bonus_id": bonus_id})
            if bonus["status"] != BONUS_APPROVED:
                return OpResult(False, "pay", "bonus_not_approved",
                                {"bonus_id": bonus_id,
                                 "status": bonus["status"]})
            ref = cur.execute(
                "SELECT * FROM referrals WHERE id = ?",
                (bonus["referral_id"],)).fetchone()
            if ref["current_tenure"] != bonus["tenure_id"]:
                return OpResult(False, "pay", "tenure_not_active",
                                {"bonus_id": bonus_id,
                                 "tenure_id": bonus["tenure_id"]})
            now_s = _to_str(now)
            cur.execute(
                "UPDATE bonuses SET status = ?, paid_at = ? WHERE id = ?",
                (BONUS_PAID_OUT, now_s, bonus_id))
            return OpResult(True, "pay",
                            data={"bonus_id": bonus_id,
                                  "status": BONUS_PAID_OUT,
                                  "amount": bonus["amount"],
                                  "paid_at": now_s})

        return self._run_idempotent("pay", request_id, params, handler)

    def inspect(self, referral_id: str) -> dict:
        """查询推荐状态、version、任职轮次与全部奖金历史。"""
        cur = self._conn
        ref = cur.execute(
            "SELECT * FROM referrals WHERE id = ?", (referral_id,)
        ).fetchone()
        if ref is None:
            raise KeyError(f"unknown referral: {referral_id!r}")
        tenures = [
            {
                "tenure_id": t["id"],
                "hired_at": _parse(t["hired_at"]),
                "unhired_at": _parse(t["unhired_at"]),
                "active": t["id"] == ref["current_tenure"],
            }
            for t in cur.execute(
                "SELECT * FROM tenures WHERE referral_id = ? ORDER BY id",
                (referral_id,))
        ]
        bonuses = [
            {
                "bonus_id": b["id"],
                "tenure_id": b["tenure_id"],
                "amount": b["amount"],
                "status": b["status"],
                "created_at": _parse(b["created_at"]),
                "approved_at": _parse(b["approved_at"]),
                "paid_at": _parse(b["paid_at"]),
                "cancelled_at": _parse(b["cancelled_at"]),
            }
            for b in cur.execute(
                "SELECT * FROM bonuses WHERE referral_id = ? ORDER BY id",
                (referral_id,))
        ]
        return {
            "referral_id": ref["id"],
            "employee_id": ref["employee_id"],
            "bonus_amount": ref["bonus_amount"],
            "status": ref["status"],
            "version": ref["version"],
            "current_tenure_id": ref["current_tenure"],
            "tenures": tenures,
            "bonuses": bonuses,
        }
