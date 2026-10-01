# 员工推荐奖金账本（referral_ledger）

用 Python + SQLite 实现的员工推荐奖金账本库。只处理录用、撤销录用、奖金批准与支付；
支付仅表示本地账本记账，不涉及实际转账。调用者需自行完成权限检查。

## 功能与规则

- 创建账本时接收初始推荐记录（唯一 ID、推荐员工 ID、固定整数分奖金），
  初始状态 `unhired`、`version = 0`；状态只有 `unhired` / `hired`，可反复切换。
- 时间由可注入时钟提供（UTC，假定时钟不倒退）。
- 四个操作：
  - `record_hiring(request_id, referral_id, expected_version, target_status)`：
    版本不符则失败；目标与当前状态相同则无副作用成功；实际切换时 version 加一。
    进入 `hired` 开启新任职轮次，录用时间取本次操作的时钟值。
  - `approve(request_id, bonus_id)`：仅 `pending -> approved`；对应轮次必须仍在持续，
    且自录用时刻起满 90 个 24 小时（恰好到达边界允许）。
  - `pay(request_id, bonus_id)`：仅 `approved -> paid_out`；批准必须仍属于当前持续轮次。
  - `inspect(referral_id)`：返回推荐状态、version、任职轮次与全部奖金历史。
- 不变量：
  - 每次进入新 `hired` 轮次，若该推荐从未产生 `paid_out` 奖金，恰好创建一笔本轮
    `pending` 奖金；任一推荐终身最多一笔 `paid_out`，任一轮次最多一笔奖金。
  - 撤销录用同时把本轮 `pending` / `approved` 奖金改为 `cancelled`（不可恢复）；
    再次录用重新累计 90 天，旧奖金 ID 不能用于新轮次。
  - `paid_out` 奖金永久保留原金额、归属轮次与支付时间；已支付不阻止撤销/再录用，
    但此类推荐再次录用不再生成奖金。
- 幂等：三个写操作都接收全局唯一 `request_id`。同 ID 同参数的重放返回首次的完整结果
  （包括版本冲突、未满 90 天等业务失败），不重新求值；同 ID 不同操作/参数抛出
  `RequestIdConflictError` 且不覆盖原结果。时钟读数不是命令参数。
- 并发与持久化：所有写操作在单个 `BEGIN IMMEDIATE` 事务内完成
  「幂等检查 -> 执行 -> 记录结果」，由 SQLite 写锁保证多实例并发等价于某个串行执行；
  不依赖单实例内存锁。关闭重开账本后语义不变。

## 运行环境

- Python 3.10+（仅标准库：`sqlite3`、`datetime`、`json`）
- 测试依赖：`pytest`
- 不需要 Docker / 数据库服务 / 缓存 / 消息队列；持久化只用 SQLite 本地文件，
  不监听任何端口。

## 快速开始

```python
from datetime import datetime, timezone
from referral_ledger import Ledger, InitialReferral, SystemClock

with Ledger(
    "ledger.db",
    clock=SystemClock(),  # 测试时可注入 ManualClock
    initial_referrals=[InitialReferral("ref-1", "emp-42", 10000)],  # 金额单位：分
) as lg:
    r = lg.record_hiring("req-1", "ref-1", expected_version=0, target_status="hired")
    bonus_id = r.data["bonus_id"]
    # ... 满 90 个 24 小时后 ...
    lg.approve("req-2", bonus_id)
    lg.pay("req-3", bonus_id)
    print(lg.inspect("ref-1"))
```

多实例并发：对同一 SQLite 文件路径创建多个 `Ledger` 实例即可，
无需调用方串行化。注意单个 `Ledger` 实例不保证线程安全，请每个线程使用自己的实例。

## 运行测试

```bash
cd task26100240-a
python3 -m pytest tests/ -v
```

测试通过 `inspect` 核对业务历史，覆盖：

- 录用后不足 90 天批准失败；推进到恰好 90 天重放原请求仍失败、新请求成功；
  跨越闰日（2024-02-28 + 90×24h = 2024-05-28）；重新打开账本后重放结果不变。
- 首轮奖金批准后撤销并再次录用：旧奖金的批准/支付与旧录用命令的重放均不影响新轮次；
  新轮次重新累计 90 天；重复同态录用不重置等待时间、不多发奖金。
- 两个实例让撤销与支付竞争，同时投递相同 `request_id` 的支付重试
  （用屏障时钟稳定制造并发，不靠 sleep）：结果只能是支付先成功且历史保留，
  或撤销先成功且旧奖金不能支付；无部分撤销、重复支付或重试结果漂移。

## 文件结构

- `referral_ledger/ledger.py` — 账本核心：schema、四个操作、幂等基础设施
- `referral_ledger/clock.py` — 时钟协议、`SystemClock`、`ManualClock`
- `tests/test_ledger.py` — 自动化测试
