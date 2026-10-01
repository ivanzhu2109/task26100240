"""90 天成熟度边界、失败结果的幂等重放、跨闰日、重开账本。"""
from datetime import datetime, timedelta, timezone

from referral_ledger import ErrorCode, Ledger, ManualClock

# 2024 是闰年：从 2 月 1 日起 90 天必然跨过 2 月 29 日
START = datetime(2024, 2, 1, 8, 30, tzinfo=timezone.utc)


def make_ledger(db_path, clock):
    return Ledger(str(db_path), [("r1", "emp-1", 500_00)], clock=clock)


def test_approve_before_90_days_fails_replay_stays_failed_then_succeeds(tmp_path):
    db = tmp_path / "ledger.db"
    clock = ManualClock(START)
    ledger = make_ledger(db, clock)

    hire = ledger.record_hiring("req-hire", "r1", 0, "hired")
    assert hire.ok and hire.bonus_id is not None
    bonus_id = hire.bonus_id

    # 不足 90 天：89 天 23:59:59 仍失败
    clock.set(START + timedelta(days=90) - timedelta(seconds=1))
    early = ledger.approve("req-approve", bonus_id)
    assert not early.ok and early.error == ErrorCode.STINT_NOT_MATURE.value

    # 推进到恰好 90 天，重放原请求：必须返回首次的失败结果，不得重新求值
    clock.set(START + timedelta(days=90))
    replay = ledger.approve("req-approve", bonus_id)
    assert replay == early, "重放必须返回首次失败结果"

    # 新请求在恰好 90 天边界成功
    approved = ledger.approve("req-approve-2", bonus_id)
    assert approved.ok and approved.bonus_id == bonus_id

    # 关闭并重开账本：重放语义不变
    ledger.close()
    ledger2 = make_ledger(db, clock)
    replay_after_reopen = ledger2.approve("req-approve", bonus_id)
    assert replay_after_reopen == early
    replay2_after_reopen = ledger2.approve("req-approve-2", bonus_id)
    assert replay2_after_reopen == approved

    # 通过 inspect 核对业务历史
    view = ledger2.inspect("r1")
    assert view.status == "hired" and view.version == 1
    assert len(view.stints) == 1 and view.stints[0].active
    assert view.stints[0].hired_at == START.isoformat()
    (bonus,) = view.bonuses
    assert bonus.bonus_id == bonus_id
    assert bonus.status == "approved"
    assert bonus.amount_cents == 500_00
    assert bonus.approved_at == (START + timedelta(days=90)).isoformat()
    ledger2.close()


def test_request_id_with_different_args_is_rejected(tmp_path):
    db = tmp_path / "ledger.db"
    clock = ManualClock(START)
    ledger = make_ledger(db, clock)

    first = ledger.record_hiring("req-x", "r1", 0, "hired")
    assert first.ok
    # 同一 request_id 搭配不同参数：拒绝且不覆盖原结果
    conflict = ledger.record_hiring("req-x", "r1", 0, "unhired")
    assert not conflict.ok
    assert conflict.error == ErrorCode.REQUEST_ID_CONFLICT.value
    # 同一 request_id 搭配不同操作：同样拒绝
    conflict2 = ledger.approve("req-x", "whatever")
    assert not conflict2.ok
    assert conflict2.error == ErrorCode.REQUEST_ID_CONFLICT.value
    # 原结果未被覆盖
    assert ledger.record_hiring("req-x", "r1", 0, "hired") == first

    view = ledger.inspect("r1")
    assert view.status == "hired" and view.version == 1
    ledger.close()
