# 执行层 — 挂起 / 观察登记

**Time (HK / UTC+8):** 2026-10-14  
**用途：** 执行层认领的观察项。C4 仍只登记、不实现。F-1 与 C3/F-3 只记账，不改退出默认，不启用新闸门，不改 loop，不写 `.env`。G-2 / G-11 是纪律，无新参数。G-3 是只读计算器，不接成闸门。

仓库根没有 `WAITING_ITEMS_REGISTRY.md`。本文件是执行层登记，不代替复盘层文件。复盘 **P-2 / P-3** 口径不在这里。E-1 / E-4 / E-5 归指标；E-3 与 N 类无代码。

本文件**不算有效性**。到至少 2026-10-19 周检旁证之前，这些条目只是观察，不是调参依据。

## C4 / E-2 — `target_band_failover`

| 项 | 内容 |
|----|------|
| **ID** | **C4 / E-2** `target_band_failover` |
| **状态** | **WAITING / 只观察。** 先登记，不立即改代码，不实现该逻辑。 |
| **书出处** | 《艾略特波浪理论》**p145**。预定价格目标有用：市场越过该位置时，提醒去看下一个计算出的位置；目标没有达到，或被超过的幅度很大时，重新考虑原先的计数。 |
| **可落地形式（未实现）** | 持仓期间若价格突破目标带，重估下一目标并继续持仓，与现有 `max_hold` 硬时间退出对照。 |
| **对照基线** | 现有 `max_hold`（默认 **60min**）+ TP/SL。信号未带 `tp_pct` / `sl_pct` / `max_hold_min` 时，缺省仍是已有的 10% / 5% / 60min（`execution/README.md`、`execution/exits.py`）。本登记不改这些数。 |
| **升级条件** | **立项须 R3 回测**对照后再谈。没有这次对照，不落 `exits`，不改运行时。 |
| **禁止** | **禁止盘中改** tp / sl / max_hold。不打开 `PM_MAX_PER_TOKEN_PCT`，不加新闸门。 |

### 与 C3 并列

拍板：**新增 C4**，不并入 C3。

- **C3** 仍是复盘表里的「趋势出场」（`docs/REVIEW_WARNING_CASES.md`）。触发仍只观察；回吐影子已接线，见下文 **C3 / F-3**。要改触发先做 R3 回测。不盘中追 tp / sl / max_hold。P-2 / P-3 口径不动。
- **C4** 与 C3 **并列**。C3 的语义是趋势出场观察；C4 的语义是目标带突破后重估下一目标并继续持仓，对照硬时间退出。两条都停在观察，分开写，避免糊成一条。

指针：

- 参数表现状：[`P1_PM_ROADMAP.md`](P1_PM_ROADMAP.md)「观察项」
- 调研映射：[`RESEARCH_MAPPING.md`](RESEARCH_MAPPING.md)
- 警示侧例：[`WARNING_CASES.md`](WARNING_CASES.md)
- 复盘 C3：`docs/REVIEW_WARNING_CASES.md`（`crawler/REVIEW_WARNING_CASES.md` 同文）

## F-1 — 信号源连亏

| 项 | 内容 |
|----|------|
| **状态** | **只记不熔断。** 同一 `strategy_id` 的平仓：`pnl_usd<0` 连亏加一，`pnl_usd>0` 清零。`pnl_usd==0` 不改计数。 |
| **落点** | `execution_ledger` 表 `strategy_streak`（`strategy_id`, `streak`, `last_pnl`, `updated_at`）与 `streak_events`。纸面平仓后写：`PaperExecutor.tick`，以及网关收到的成交卖出（`signal_sell`）。 |
| **开关** | `EXECUTION_F1_CIRCUIT` 缺省关。订单路径不读这个开关，不拒新开仓，也不平掉现仓。本版本没有自动熔断调用点。 |
| **只读** | `python -m execution.f1_streak_report`。输出最长连亏、样本数、P(第 3 笔仍亏 \| 已连亏≥2)。`--paper` 用 `paper_trading.db` 已平仓做交叉校验，不写库。 |
| **禁止** | 不把连亏接进 `execution/risk`。不改 `EXECUTION_DAILY_LOSS_USD`。不改决策层冷却（冷却在 decision_engine；这里只旁证）。 |

## C3 / F-3 — 趋势回吐影子

| 项 | 内容 |
|----|------|
| **状态** | **影子已接线，触发未改。** |
| **peak** | tick 对每笔 open 把当时的 mid 写入 `positions.meta_json.peak_price`，只升不降。经 `PaperExecutor` 开仓时，先记下开仓 mid。 |
| **平仓** | 表 `shadow_c3`：`entry_price`、`peak_price`、`exit_price`、`peak_pnl_pct`、`giveback_from_peak_pct`、`hold_min`、`actual_reason`、`actual_pnl_usd`。不写进 `order_events`：tick 平仓没有自己的 `execution_log_id`。 |
| **口径** | `peak_pnl_pct = (peak/entry - 1) × 100`。`giveback_from_peak_pct = (peak - exit_fill) / entry × 100`（从高点 mid 给回到平仓成交，占入场价的百分点数）。 |
| **触发** | `execution/exits.py` 的 take_profit / stop_loss / max_hold 与默认 **10 / 5 / 60** 不动。 |
| **只读** | `python -m execution.shadow_c3_report` 按 `actual_reason` 汇总平均回吐。 |
| **含义** | 要看的是从高点给回去多少，不是把持仓缩短当成「单笔亏得更小」。立项观察，不改退出。 |

## G-2 — 禁止摊薄

红线：不得引入摊薄亏损（不对亏损仓加仓，不把加仓写成降低成本）。本条不新增代码。现网没有摊薄路径。

## G-3 — 破产次数（只读）

`python -m execution.risk.bankruptcy`。`max_sequential_losses = floor(capital / per_trade_risk)`，`per_trade_risk = size_usd × (sl_pct/100)`。缺省按代码里的资本 10000、仓位 5%、止损 5%，可用参数覆盖。不读、不改 `EXECUTION_DAILY_LOSS_USD`，不接成闸门。

## G-11 — 不设绝对目标价

现网退出是信号卖出、百分比止盈（默认 10%）、百分比止损（默认 5%）、最长持有（默认 60 分钟）。没有固定报价目标。这与 G-11 相符。不新增目标价，不改上述默认。

## 不算有效性

- 到至少 **2026-10-19** 周检旁证之前，C4、F-1 连亏计数、C3 回吐影子、G-3 理论连亏次数 **不算有效性**，也不是启用依据。
- 纪律同 R1 / R2 / R3：`docs/REVIEW_R1_SPEC.md`、`docs/REVIEW_R3_SCAFFOLD.md`。没有 R3 对照，目标带切换只登记、不落代码。
