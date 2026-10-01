"""两个独立实例并发操作同一账本：撤销与支付竞争 + 同 request_id 支付重试。"""
import threading
from datetime import datetime, timedelta, timezone

from referral_ledger import ErrorCode, Ledger, ManualClock

START = datetime(2025, 6, 1, 0, 0, tzinfo=timezone.utc)


def make_pair(db_path, clock):
    a = Ledger(str(db_path), [("r1", "emp-1", 900_00)], clock=clock)
    b = Ledger(str(db_path), clock=clock)  # 独立实例，同一账本文件
    return a, b


def prepare(ledger, clock):
    hire = ledger.record_hiring("req-hire", "r1", 0, "hired")
    clock.advance(days=90)
    assert ledger.approve("req-approve", hire.bonus_id).ok
    return hire.bonus_id


def assert_pay_wins(ledger, bonus_id, pay_result, pay_retry):
    view = ledger.inspect("r1")
    assert view.status == "unhired" and view.version == 2
    (stint,) = view.stints
    assert not stint.active and stint.unhired_at is not None
    (bonus,) = view.bonuses
    assert bonus.status == "paid_out"
    assert bonus.paid_at is not None and bonus.cancelled_at is None
    assert pay_result.ok and pay_retry == pay_result


def assert_unhire_wins(ledger, bonus_id, pay_result, pay_retry):
    view = ledger.inspect("r1")
    assert view.status == "unhired" and view.version == 2
    (stint,) = view.stints
    assert not stint.active
    (bonus,) = view.bonuses
    assert bonus.status == "cancelled"
    assert bonus.cancelled_at is not None and bonus.paid_at is None
    assert not pay_result.ok
    assert pay_result.error == ErrorCode.BONUS_NOT_APPROVED.value
    assert pay_retry == pay_result  # 重试结果不漂移


def test_sequential_pay_then_unhire(tmp_path):
    clock = ManualClock(START)
    a, b = make_pair(tmp_path / "l.db", clock)
    bonus_id = prepare(a, clock)
    pay = b.pay("req-pay", bonus_id)
    assert a.record_hiring("req-unhire", "r1", 1, "unhired").ok
    retry = b.pay("req-pay", bonus_id)
    assert_pay_wins(a, bonus_id, pay, retry)
    a.close(); b.close()


def test_sequential_unhire_then_pay(tmp_path):
    clock = ManualClock(START)
    a, b = make_pair(tmp_path / "l.db", clock)
    bonus_id = prepare(a, clock)
    assert a.record_hiring("req-unhire", "r1", 1, "unhired").ok
    pay = b.pay("req-pay", bonus_id)
    retry = b.pay("req-pay", bonus_id)
    assert_unhire_wins(a, bonus_id, pay, retry)
    a.close(); b.close()


def test_concurrent_unhire_vs_pay_with_retry(tmp_path):
    """屏障同步制造竞争；无论谁先，结果必须可串行化且重试不漂移。"""
    for i in range(30):
        db = tmp_path / f"race-{i}.db"
        clock = ManualClock(START)
        a, b = make_pair(db, clock)
        bonus_id = prepare(a, clock)

        barrier = threading.Barrier(3)
        outcomes = {}

        def do_unhire():
            barrier.wait()
            outcomes["unhire"] = a.record_hiring("req-unhire", "r1", 1, "unhired")

        def do_pay():
            barrier.wait()
            outcomes["pay"] = b.pay("req-pay", bonus_id)

        t1 = threading.Thread(target=do_unhire)
        t2 = threading.Thread(target=do_pay)
        t1.start(); t2.start()
        barrier.wait()  # 同时放行两个操作
        t1.join(); t2.join()

        assert outcomes["unhire"].ok, "撤销不得因支付状态被拒绝"
        pay_retry = b.pay("req-pay", bonus_id)

        if outcomes["pay"].ok:
            assert_pay_wins(a, bonus_id, outcomes["pay"], pay_retry)
        else:
            assert_unhire_wins(a, bonus_id, outcomes["pay"], pay_retry)

        # 不变量：恰一笔奖金；paid_out 与 cancelled 互斥；无重复支付
        view = a.inspect("r1")
        assert len(view.bonuses) == 1
        assert len([x for x in view.bonuses if x.status == "paid_out"]) <= 1
        a.close(); b.close()


def test_concurrent_same_request_id_pay_retries(tmp_path):
    """两个实例同时用同一 request_id 支付：恰好一次生效，结果一致。"""
    for i in range(20):
        db = tmp_path / f"dup-{i}.db"
        clock = ManualClock(START)
        a, b = make_pair(db, clock)
        bonus_id = prepare(a, clock)

        barrier = threading.Barrier(3)
        results = []

        def do_pay(inst):
            barrier.wait()
            results.append(inst.pay("req-pay", bonus_id))

        t1 = threading.Thread(target=do_pay, args=(a,))
        t2 = threading.Thread(target=do_pay, args=(b,))
        t1.start(); t2.start()
        barrier.wait()
        t1.join(); t2.join()

        assert len(results) == 2
        assert results[0] == results[1], "同一 request_id 的结果必须一致"
        assert results[0].ok
        view = a.inspect("r1")
        (bonus,) = view.bonuses
        assert bonus.status == "paid_out"
        a.close(); b.close()
