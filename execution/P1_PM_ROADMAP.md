# P-1 PM 参数集 — 现状与缺项

**Time (HK / UTC+8):** 2026-10-14  
**用途：** 更正执行单里「PM/RISK 仍关」的基线。只记授权后的 paper 进程覆盖，以及仍然缺的设计项。不改代码默认，不改任何数值，不写 `.env`。

本文件**不算有效性**。缺项里的净边际 X 与账户回撤阈值等 R2 回测。禁止盘中发明。

执行单（2026-10-14）开篇基线写的是：paper 已开，PM/RISK 仍关，`min_liq=50k` 已加，P2-06/07 未修。后半句里，PM/RISK「仍关」和「P2-07 未修」已经过期。代码仓库里的**默认**仍然关；**生产 paper 进程**在 2026-10-14 经杨授权后打开了下面这组开关。Live 仍关。

警示映射见 [`WARNING_CASES.md`](WARNING_CASES.md)。调研映射（M-4 服务层硬检查、M-5 先选择后仓位、M-7 滑点与 R2-pending 的单市场 15%）见 [`RESEARCH_MAPPING.md`](RESEARCH_MAPPING.md)。那份映射不改本文件的开关和数值。

## 代码默认（本文件不改）

| 开关 | 代码默认 | 出处 |
|------|----------|------|
| `EXECUTION_PAPER_ENABLED` / `--paper-enabled` | 关 | `execution/README.md`、`execution/run.py` |
| `EXECUTION_PAPER_TICK` / `--paper-tick` | 关 | `execution/run.py` |
| `PM_ENABLED` / `--pm-enabled` | 关（pass-through） | `execution/position_manager.py` |
| `PM_TOKEN_DEDUP`、`PM_STRATEGY_MUTEX`、`PM_MAX_*` | 关 | 同上 |
| `EXECUTION_RISK_ENABLED` | 关 | `execution/config.py` |
| `EXECUTION_DAILY_LOSS_USD` | 未设置 = 日亏检查关 | `execution/config.py`（空白或 `≤0` 也是关） |
| `EXECUTION_MAX_OPEN` | 数值默认 5；风控关时这条不跑 | `execution/config.py`、`execution/risk/limits.py` |
| 仓库内 systemd | `python -m execution.run --loop`（Step A，无 paper / PM 旗标） | `deploy/dexscreener-execution.service` |
| Live | 关 | `LIVE_TRADING_DISABLED`；执行层不调用 `LiveExecutor` |

`execution/README.md` 的环境表继续描述这些代码默认。下面的「已开」是进程环境与启动参数，不是把默认改成开，也不是改 unit 文件。

## 已开 — 生产 paper（2026-10-14，杨授权）

Loop 以 `--paper-enabled --paper-tick --pm-enabled` 运行。Live 仍关。

| 项 | 授权值 | 书中对照 | 说明 |
|----|--------|----------|------|
| `PM_ENABLED` | `1` | 风控层在场 | 买入在执行器之前走 `PositionManager.check_open` |
| `PM_TOKEN_DEDUP` | `1` | 单市场仓位限额（同票 1） | 同 chain+token 已有开仓则 `pm_rejected:token_dedup` |
| `PM_MAX_OPEN_PER_STRATEGY` | `5` | 同时持仓（书中 6–10；观察期更紧） | 达上限 `pm_rejected:max_open_per_strategy` |
| `EXECUTION_RISK_ENABLED` | `1` | 日亏 / 持仓数骨架 | 预交易风控开。SELL 仍跳过 `max_open`，满仓可以平 |
| `EXECUTION_MAX_OPEN` | `5` | 同上 | 与 PM 的每策略上限并列。风控开时才生效 |
| `EXECUTION_DAILY_LOSS_USD` | `200` | 每日亏损上限 | P2-06 的当日已实现亏损熔断。当日已实现 PnL < −200 USD 则拒 |

未出现在这组授权里、代码默认仍关的 PM 规则：`PM_STRATEGY_MUTEX`、`PM_MAX_TOTAL_PCT`、`PM_MAX_PER_TOKEN_PCT`、`PM_STRATEGY_PRIORITY`。本文件不把它们打开。

P2-06 / P2-07 的代码修复已经在库里：SELL 跳过 `max_open`、日亏限额会传入网关、百分点解析（`1` = 1%）、无账本 fail-closed（`pm_rejected:no_ledger`）。2026-10-14 扫码结论写的是「默认关、未当开闸验收」。这里把 **paper 进程已开** 和 **代码默认仍关** 分开记。实盘不开。

执行单把「每日亏损上限」和「20% 账户回撤」都算进未修的 P2-07。日亏上限就是上表的 `EXECUTION_DAILY_LOSS_USD=200`（P2-06 回路），paper 上已经开。账户回撤仍缺，见下节。

## 仍缺 — 设计 + R2，先不写代码

不发明 X%。不在盘中写死阈值。

| 书中项 | 书中量级 | 我们 | 下一步 |
|--------|----------|------|--------|
| **最低净边际** | 扣费后不到约 **4%** 不做 | **缺**。paper 上仍有 max_hold 吃滑点、微亏的低质量开仓 | 开仓前检查「最低净边际」。我们的 X 由 R2 回测确定。约 4% 是书中参照，不是本系统参数 |
| **账户级回撤熔断** | 约 **20%** | **缺**。已开的是当日已实现亏损 200 USD，不是账户回撤 | 与 P2-06（日亏回路）和 P2-07（无账本 fail-closed）对齐后再设计。不在盘中另写一个百分比 |
| **四级热度 → 仓位档** | Normal → Warning → Critical → Max，各级仓位不同 | **缺**。hot 列表仍是单档 `hot` | 只做设计。不接到 `size_pct` 或下单数量 |
| **单市场敞口上限** | Prediction Arena 硬顶约 **15%**（本层记为 P-2；不是周检表里复盘的真库 IC/IR） | **R2-pending**。`PM_MAX_PER_TOKEN_PCT` 保持关，paper 授权组里也没有它 | 只登记。不启用该变量，不在盘中把 15 写成参数。README 的 suggest 10 保持原样。见 [`RESEARCH_MAPPING.md`](RESEARCH_MAPPING.md) M-7 |

单市场去重与每日亏损上限已经在「已开」里（同票去重 + 每策略 5 + 日亏 200 USD）。它们不是缺项。单市场敞口 ≤15%（`PM_MAX_PER_TOKEN_PCT`）不是这组授权，上表单独登记为未来项。

## 观察项 — 只登记，不改运行时

不发明目标带宽度，不启用任何 env，不改 exits。全文在 [`WAITING_ITEMS.md`](WAITING_ITEMS.md)。

| ID | 项 | 状态 | 对照 / 升级 |
|----|----|------|-------------|
| **C4 / E-2** | `target_band_failover` | **WAITING / 只观察。** 持仓期间若价格突破目标带，重估下一目标并继续持仓。不实现。 | 基线 = 现有 `max_hold`（默认 60min）+ TP/SL。升级 = R3 回测对照后再谈。禁止盘中改 tp / sl / max_hold。**不算有效性**（至少到 2026-10-19 周检旁证）。 |

书出处 p145。与复盘 **C3**（趋势出场，`docs/REVIEW_WARNING_CASES.md`）**并列**：C3 仍只观察；C4 不并入 C3。

## 纪律

1. **R1** — 周报只读。PM / 风控拒因从 `order_events` 分桶（[`WARNING_CASES.md`](WARNING_CASES.md)）。不改参。
2. **R2** — 净边际 X、账户回撤，以及单市场 15% 敞口上限的回测依据。没有 R2，不落代码，不启用 `PM_MAX_PER_TOKEN_PCT`。
3. **R3** — 寻优在历史够长且周检跨门槛之后。短窗胜率不拿来追认。
4. **第三条盘中统计参数禁止。** 不改 SL%，不临时设净边际，不临时设回撤百分比，不临时设单市场 15%。
5. **PM / RISK 是服务层硬检查**（gateway → `position_manager` / `risk`），不是 prompt。Agent 不能覆盖。验收不变量见 [`RESEARCH_MAPPING.md`](RESEARCH_MAPPING.md) M-4。
6. 以上全部标注 **不算有效性**，直到 2026-10-19 周检。

## 验收（本文件能交的部分）

执行单第五节要「PM 参数表（含 4% 净边际 X 值的回测依据）+ 合并 commit」。本文件只交参数表现状。X 的回测依据还没有，所以这里没有行为变更。后续代码 PR 单独开，并带上 R2。
