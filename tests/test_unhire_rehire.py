"""撤销录用与再次录用：旧轮次奖金取消、旧请求重放不影响新轮次、90 天重新累计。"""
from datetime import datetime, timedelta, timezone

from referral_ledger import ErrorCode, Ledger, ManualClock

START = datetime(2025, 3, 10, 12, 0, tzinfo=timezone.utc)


def make_ledger(db_path, clock):
    return Ledger(str(db_path), [("r1", "emp-1", 800_00)], clock=clock)


def test_approved_bonus_cancelled_and_new_stint_is_independent(tmp_path):
    db = tmp_path / "ledger.db"
    clock = ManualClock(START)
    ledger = make_ledger(db, clock)

    # 首轮：录用 -> 满 90 天 -> 批准
    hire1 = ledger.record_hiring("req-hire-1", "r1", 0, "hired")
    old_bonus = hire1.bonus_id
    clock.advance(days=90)
    assert ledger.approve("req-approve-1", old_bonus).ok

    # 撤销：旧奖金被 cancelled
    unhire = ledger.record_hiring("req-unhire-1", "r1", 1, "unhired")
    assert unhire.ok and unhire.version == 2
    view = ledger.inspect("r1")
    (b,) = view.bonuses
    assert b.status == "cancelled" and b.cancelled_at == clock.now().isoformat()
    assert b.approved_at is not None  # 历史时间保留

    # 再次录用：新轮次、新 pending 奖金
    clock.advance(days=5)
    rehire_time = clock.now()
    hire2 = ledger.record_hiring("req-hire-2", "r1", 2, "hired")
    assert hire2.ok and hire2.bonus_id is not None
    new_bonus = hire2.bonus_id
    assert new_bonus != old_bonus

    # 旧奖金 ID 不能批准或支付（已 cancelled）
    r = ledger.approve("req-approve-old", old_bonus)
    assert not r.ok and r.error == ErrorCode.BONUS_NOT_PENDING.value
    r = ledger.pay("req-pay-old", old_bonus)
    assert not r.ok and r.error == ErrorCode.BONUS_NOT_APPROVED.value

    # 旧批准请求重放：返回首次成功结果，但不影响新轮次
    replay_approve = ledger.approve("req-approve-1", old_bonus)
    assert replay_approve.ok  # 幂等重放首次结果
    view = ledger.inspect("r1")
    statuses = {b.bonus_id: b.status for b in view.bonuses}
    assert statuses[old_bonus] == "cancelled"
    assert statuses[new_bonus] == "pending"

    # 旧录用命令重放：返回首次结果，不产生新轮次/新奖金
    replay_hire = ledger.record_hiring("req-hire-1", "r1", 0, "hired")
    assert replay_hire == hire1
    view = ledger.inspect("r1")
    assert len(view.stints) == 2 and len(view.bonuses) == 2
    assert view.version == 3

    # 新轮次必须重新累计 90 天：从首轮录用时间起算早已超过，但仍失败
    clock.set(rehire_time + timedelta(days=90) - timedelta(seconds=1))
    r = ledger.approve("req-approve-2", new_bonus)
    assert not r.ok and r.error == ErrorCode.STINT_NOT_MATURE.value

    # 重复同态录用：无副作用成功，不重置等待时间、不多发奖金
    dup = ledger.record_hiring("req-hire-dup", "r1", 3, "hired")
    assert dup.ok and dup.version == 3 and dup.bonus_id is None
    view = ledger.inspect("r1")
    assert len(view.stints) == 2 and len(view.bonuses) == 2
    assert view.stints[-1].hired_at == rehire_time.isoformat()

    # 恰好满 90 天可批准新奖金
    clock.set(rehire_time + timedelta(days=90))
    assert ledger.approve("req-approve-3", new_bonus).ok
    view = ledger.inspect("r1")
    statuses = {b.bonus_id: b.status for b in view.bonuses}
    assert statuses[new_bonus] == "approved"
    assert statuses[old_bonus] == "cancelled"
    ledger.close()


def test_paid_out_bonus_survives_and_no_new_bonus_after_rehire(tmp_path):
    db = tmp_path / "ledger.db"
    clock = ManualClock(START)
    ledger = make_ledger(db, clock)

    hire1 = ledger.record_hiring("req-h1", "r1", 0, "hired")
    clock.advance(days=90)
    assert ledger.approve("req-a1", hire1.bonus_id).ok
    pay_time = clock.now()
    assert ledger.pay("req-p1", hire1.bonus_id).ok

    # 已支付不阻碍撤销与再次录用
    assert ledger.record_hiring("req-u1", "r1", 1, "unhired").ok
    clock.advance(days=1)
    hire2 = ledger.record_hiring("req-h2", "r1", 2, "hired")
    assert hire2.ok and hire2.bonus_id is None  # 不再生成奖金

    view = ledger.inspect("r1")
    assert len(view.stints) == 2
    (b,) = view.bonuses
    assert b.status == "paid_out"
    assert b.amount_cents == 800_00
    assert b.stint_id == view.stints[0].stint_id
    assert b.paid_at == pay_time.isoformat()

    # 终身最多一笔 paid_out：新轮次走完 90 天也无奖金可批准
    clock.advance(days=90)
    r = ledger.approve("req-a2", "r1:stint:2")
    assert not r.ok and r.error == ErrorCode.BONUS_NOT_FOUND.value
    ledger.close()
