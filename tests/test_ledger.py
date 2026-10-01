"""员工推荐奖金账本测试：通过 inspect 核对业务历史，而不只检查返回码。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from referral_ledger import (
    InitialReferral,
    Ledger,
    ManualClock,
    RequestIdConflictError,
)

START = datetime(2024, 2, 28, 12, 0, tzinfo=timezone.utc)  # 2024 为闰年
DAY = timedelta(days=1)
PROBATION = timedelta(days=90)


def make_ledger(path, clock, **kw):
    return Ledger(
        str(path),
        clock=clock,
        initial_referrals=kw.get(
            "initial_referrals", [InitialReferral("r1", "e1", 10000)]
        ),
    )


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "ledger.db"


def hire(lg, req, rid="r1", version=0):
    res = lg.record_hiring(req, rid, version, "hired")
    assert res.ok, res
    return res


class TestProbationBoundary:
    """不足 90 天批准失败；恰好 90 天可批准；重放不重新求值；跨闰日；重开账本。"""

    def test_boundary_leap_day_replay_and_reopen(self, db_path):
        clock = ManualClock(START)
        lg = make_ledger(db_path, clock)
        hire_res = hire(lg, "h1")
        bonus_id = hire_res.data["bonus_id"]

        # 不足 90 天：89 天又 23 小时
        clock.set(START + PROBATION - timedelta(hours=1))
        early = lg.approve("a-early", bonus_id)
        assert not early.ok
        assert early.error == "probation_not_elapsed"
        # 90 个 24 小时跨越 2/29：2/28 12:00 + 90 天 = 5/28 12:00
        assert early.data["hired_at"] == "2024-02-28T12:00:00+00:00"
        assert early.data["eligible_at"] == "2024-05-28T12:00:00+00:00"
        assert lg.inspect("r1")["bonuses"][0]["status"] == "pending"

        # 推进到恰好 90 天，重放原请求：必须返回首次的失败结果，不重新求值
        clock.set(START + PROBATION)
        replay = lg.approve("a-early", bonus_id)
        assert replay == early
        assert lg.inspect("r1")["bonuses"][0]["status"] == "pending"

        # 新请求在边界时刻成功
        ok = lg.approve("a-on-time", bonus_id)
        assert ok.ok
        assert ok.data["approved_at"] == "2024-05-28T12:00:00+00:00"
        lg.close()

        # 重新打开账本后再次重放：结果不变，历史可核对
        lg2 = make_ledger(db_path, ManualClock(START + PROBATION + DAY))
        replay2 = lg2.approve("a-early", bonus_id)
        assert replay2 == early
        info = lg2.inspect("r1")
        bonus = info["bonuses"][0]
        assert bonus["status"] == "approved"
        assert bonus["approved_at"] == START + PROBATION
        assert info["status"] == "hired"
        assert info["version"] == 1
        lg2.close()

    def test_version_conflict_failure_is_memoized(self, db_path):
        clock = ManualClock(START)
        lg = make_ledger(db_path, clock)
        hire(lg, "h1")
        conflict = lg.record_hiring("u-bad", "r1", 0, "unhired")
        assert not conflict.ok
        assert conflict.error == "version_conflict"
        # 状态推进后重放同一请求：仍返回首次的版本冲突
        lg.record_hiring("u-ok", "r1", 1, "unhired")
        replay = lg.record_hiring("u-bad", "r1", 0, "unhired")
        assert replay == conflict
        info = lg.inspect("r1")
        assert info["status"] == "unhired"
        assert info["version"] == 2
        lg.close()

    def test_request_id_reuse_with_different_params_rejected(self, db_path):
        clock = ManualClock(START)
        lg = make_ledger(db_path, clock)
        first = hire(lg, "req-x")
        with pytest.raises(RequestIdConflictError):
            lg.record_hiring("req-x", "r1", 1, "unhired")  # 同 ID 不同参数
        with pytest.raises(RequestIdConflictError):
            lg.approve("req-x", 1)  # 同 ID 不同操作
        # 原结果不被覆盖
        assert lg.record_hiring("req-x", "r1", 0, "hired") == first
        lg.close()


class TestRevokeAndRehire:
    """撤销取消本轮奖金；再次录用重新累计 90 天；旧请求重放不影响新轮次。"""

    def test_approved_then_revoked_then_rehired(self, db_path):
        clock = ManualClock(START)
        lg = make_ledger(db_path, clock)
        b1 = hire(lg, "h1").data["bonus_id"]
        clock.set(START + PROBATION)
        approved = lg.approve("a1", b1)
        assert approved.ok

        # 撤销：approved 奖金被取消
        unhire = lg.record_hiring("u1", "r1", 1, "unhired")
        assert unhire.ok and unhire.data["changed"]
        info = lg.inspect("r1")
        assert info["bonuses"][0]["status"] == "cancelled"
        assert info["bonuses"][0]["cancelled_at"] == START + PROBATION

        # 再次录用：新轮次、新 pending 奖金、新录用时间
        rehire_at = START + PROBATION + 10 * DAY
        clock.set(rehire_at)
        rehire = hire(lg, "h2", version=2)
        b2 = rehire.data["bonus_id"]
        assert b2 != b1
        assert rehire.data["tenure_id"] != unhire.data["tenure_id"]

        # 旧奖金的批准重放：返回首次结果，但不改变已取消的历史
        assert lg.approve("a1", b1) == approved
        info = lg.inspect("r1")
        assert info["bonuses"][0]["status"] == "cancelled"
        # 旧奖金 ID 不能在新轮次被批准或支付
        old_approve = lg.approve("a-old-again", b1)
        assert not old_approve.ok and old_approve.error == "bonus_not_pending"
        old_pay = lg.pay("p-old", b1)
        assert not old_pay.ok and old_pay.error == "bonus_not_approved"
        # 旧录用命令的重放：返回首次结果，不影响新轮次
        assert lg.record_hiring("h1", "r1", 0, "hired").data["version"] == 1
        info = lg.inspect("r1")
        assert info["version"] == 3
        assert len(info["tenures"]) == 2
        assert len(info["bonuses"]) == 2

        # 新轮次必须等待自己的 90 天，不继承旧轮次的批准与录用时间
        clock.set(rehire_at + PROBATION - timedelta(hours=1))
        early = lg.approve("a2-early", b2)
        assert not early.ok
        assert early.error == "probation_not_elapsed"
        assert early.data["hired_at"] == "2024-06-07T12:00:00+00:00"

        # 重复同态录用：无副作用，不重置等待时间，不多发奖金
        for i in range(3):
            noop = lg.record_hiring(f"h-noop-{i}", "r1", 3, "hired")
            assert noop.ok and not noop.data["changed"]
            clock.advance(10 * DAY)
        info = lg.inspect("r1")
        assert info["version"] == 3
        tenure2 = [t for t in info["tenures"] if t["active"]][0]
        assert tenure2["hired_at"] == rehire_at
        assert [b for b in info["bonuses"] if b["tenure_id"] == tenure2["tenure_id"]]
        assert len(info["bonuses"]) == 2

        # 到达新轮次自己的 90 天边界后可以批准并支付
        clock.set(rehire_at + PROBATION)
        assert lg.approve("a2", b2).ok
        paid = lg.pay("p2", b2)
        assert paid.ok and paid.data["amount"] == 10000
        info = lg.inspect("r1")
        assert [b["status"] for b in info["bonuses"]] == ["cancelled", "paid_out"]
        lg.close()

    def test_paid_out_referral_gets_no_more_bonuses(self, db_path):
        clock = ManualClock(START)
        lg = make_ledger(db_path, clock)
        b1 = hire(lg, "h1").data["bonus_id"]
        clock.set(START + PROBATION)
        lg.approve("a1", b1)
        lg.pay("p1", b1)
        # 不得以已支付为由拒绝撤销或再次录用
        assert lg.record_hiring("u1", "r1", 1, "unhired").ok
        rehire = hire(lg, "h2", version=2)
        assert "bonus_id" not in rehire.data  # 不再生成奖金
        info = lg.inspect("r1")
        assert len(info["bonuses"]) == 1
        bonus = info["bonuses"][0]
        # paid_out 奖金永久保留原金额、归属轮次和支付时间
        assert bonus["status"] == "paid_out"
        assert bonus["amount"] == 10000
        assert bonus["tenure_id"] == info["tenures"][0]["tenure_id"]
        assert bonus["paid_at"] == START + PROBATION
        lg.close()


class BarrierClock:
    """所有线程在时钟读数处汇合，保证操作真正同时在途（不靠 sleep 碰运气）。"""

    def __init__(self, moment: datetime, barrier: threading.Barrier):
        self._moment = moment
        self._barrier = barrier

    def now(self) -> datetime:
        self._barrier.wait(timeout=30)
        return self._moment


class TestConcurrentRevokeVsPay:
    """撤销与支付竞争 + 相同 request_id 的支付重试：
    结果只能是「支付先成功且历史保留」或「撤销先成功且旧奖金不能支付」。"""

    def test_race_is_serizalizable(self, db_path):
        for round_no in range(20):
            path = db_path.with_name(f"race-{round_no}.db")
            self._run_round(path)

    def _run_round(self, path):
        moment = START + PROBATION
        setup = make_ledger(path, ManualClock(START))
        b1 = hire(setup, "h1").data["bonus_id"]
        setup_clock = ManualClock(START)
        setup_clock.set(moment)
        setup2 = make_ledger(path, setup_clock)
        assert setup2.approve("a1", b1).ok

        barrier = threading.Barrier(3)
        lg_a = make_ledger(path, BarrierClock(moment, barrier))
        lg_b = make_ledger(path, BarrierClock(moment, barrier))
        lg_c = make_ledger(path, BarrierClock(moment, barrier))

        with ThreadPoolExecutor(max_workers=3) as pool:
            f_revoke = pool.submit(
                lg_a.record_hiring, "rev-1", "r1", 1, "unhired")
            f_pay = pool.submit(lg_b.pay, "pay-1", b1)
            f_retry = pool.submit(lg_c.pay, "pay-1", b1)  # 同 request_id 重试
            revoke = f_revoke.result()
            pay = f_pay.result()
            retry = f_retry.result()

        # 撤销必须成功（已 approved 未支付的奖金不阻止撤销）
        assert revoke.ok and revoke.data["changed"]
        # 相同 request_id 的重试不得漂移：必须返回完全相同的结果
        assert pay == retry

        for lg in (lg_a, lg_b, lg_c, setup, setup2):
            lg.close()

        # 关闭重开后核对历史：状态变更与奖金变更原子可见、无部分撤销
        check = make_ledger(path, ManualClock(moment))
        info = check.inspect("r1")
        assert info["status"] == "unhired"
        assert info["version"] == 2
        assert len(info["tenures"]) == 1
        assert info["tenures"][0]["unhired_at"] == moment
        assert len(info["bonuses"]) == 1
        bonus = info["bonuses"][0]
        if pay.ok:
            # 支付先成功：历史永久保留，重试返回同一成功结果
            assert bonus["status"] == "paid_out"
            assert bonus["amount"] == 10000
            assert bonus["paid_at"] == moment
            assert pay.data["paid_at"] == "2024-05-28T12:00:00+00:00"
        else:
            # 撤销先成功：旧奖金已取消，支付与重试一致地失败
            assert pay.error == "bonus_not_approved"
            assert pay.data["status"] == "cancelled"
            assert bonus["status"] == "cancelled"
            assert bonus["cancelled_at"] == moment
            assert bonus["paid_at"] is None
        check.close()
