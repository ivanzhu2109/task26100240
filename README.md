# 员工推荐奖金账本（referral_ledger）

基于 Python 标准库 + SQLite 的本地账本库。只负责记账：录用、撤销录用、
奖金批准与支付（支付仅为本地记账，不发生实际转账）。调用方需自行完成权限检查。

## 运行环境

- Python 3.9+（开发验证使用 3.14），仅依赖标准库
- 测试需要 `pytest`
- 不需要 Docker / 数据库服务 / 网络；持久化只用一个 SQLite 文件

## 运行测试

```bash
python3 -m pytest tests/ -v
```

## 快速上手

```python
from datetime import datetime, timezone
from referral_ledger import Ledger, ManualClock

clock = ManualClock(datetime(2026, 1, 1, tzinfo=timezone.utc))  # 生产用 SystemClock()
ledger = Ledger(
    "ledger.db",
    initial_referrals=[("rec-1", "emp-42", 1000_00)],  # (推荐ID, 员工ID, 奖金分)
    clock=clock,
)

hire = ledger.record_hiring("req-1", "rec-1", expected_version=0, target_status="hired")
clock.advance(days=90)                                  # 满 90 个 24 小时（含边界）
ledger.approve("req-2", hire.bonus_id)
ledger.pay("req-3", hire.bonus_id)                      # 仅本地记账

view = ledger.inspect("rec-1")                          # 状态、version、任职轮次、全部奖金历史
ledger.close()
```

## API

- `Ledger(db_path, initial_referrals=(), clock=None)`
  创建/打开账本。初始推荐状态为 `unhired`、`version=0`；重复打开时初始记录幂等插入。
- `record_hiring(request_id, referral_id, expected_version, target_status) -> Result`
  版本不符返回 `version_conflict`；目标与当前状态相同则无副作用成功；
  实际切换时 `version+1`。进入 `hired` 开启新任职轮次（录用时间取本次时钟值），
  若该推荐从未产生 `paid_out` 奖金则创建一笔本轮 `pending` 奖金；
  进入 `unhired` 关闭当前轮次并将其 `pending`/`approved` 奖金置为 `cancelled`。
- `approve(request_id, bonus_id) -> Result`
  仅 `pending -> approved`；要求轮次仍在持续且自录用起已满 90 天（恰好到达允许）。
- `pay(request_id, bonus_id) -> Result`
  仅 `approved -> paid_out`；要求批准所属轮次仍在持续。
- `inspect(referral_id) -> ReferralView | None`
  返回推荐状态、version、全部任职轮次与全部奖金（状态、金额、各时间戳）。

`Result` 字段：`ok`、`error`（见 `ErrorCode`）、`version`、`bonus_id`。

## 设计要点

- **幂等**：三个写操作的结果（含版本冲突、未满 90 天等业务失败）随 `request_id`
  持久化在 `requests` 表。同 ID 同参数重放返回首次完整结果，绝不重新求值；
  同 ID 不同操作/参数返回 `request_id_conflict` 且不覆盖原结果。时钟读数不属于命令参数。
- **并发**：每个写操作在单个 `BEGIN IMMEDIATE` 事务内完成
  「查幂等记录 → 执行业务 → 记录结果」，SQLite 写锁保证多实例间串行化与原子可见；
  不依赖单实例内存锁。关闭重开后语义不变（测试覆盖）。
- **不变量**：每个 hired 轮次至多一笔奖金；任一推荐终身至多一笔 `paid_out`；
  `cancelled` 不可恢复；再次录用重新累计 90 天，旧奖金 ID 无法用于新轮次。

## 文件结构

- `referral_ledger/models.py` — 状态枚举、`Result`、`ReferralView` 等数据类型
- `referral_ledger/clock.py` — 可注入时钟（`SystemClock` / `ManualClock`），UTC
- `referral_ledger/storage.py` — SQLite 建表与连接（WAL）
- `referral_ledger/ledger.py` — 四个操作与幂等执行框架
- `tests/` — 90 天边界/闰日/重放、撤销再录用、双实例竞争（屏障同步，非 sleep）
