# Execution — 警示事例（执行单 → 执行侧）

**Time (HK / UTC+8):** 2026-10-14  
**用途：** 把执行单第四节警示，以及看板平仓旁证，映射到执行层已经有的成本模型与仓位闸。不改代码默认，不改 TP/SL/hold，不改 PM 数值。

本文件**不算有效性**。统计类观察留到 2026-10-19 周检。不在盘中新增第三条统计参数：名义 SL%、净边际、账户回撤阈值都保持不动，等 R2。

代码默认仍是关：`PM_ENABLED=0`、`EXECUTION_RISK_ENABLED=0`、`EXECUTION_DAILY_LOSS_USD` 未设置、`EXECUTION_PAPER_ENABLED=0`。2026-10-14 杨授权的 paper 进程覆盖见 [`P1_PM_ROADMAP.md`](P1_PM_ROADMAP.md)。Live 仍关（`LIVE_TRADING_DISABLED`；本层不调用 `LiveExecutor`）。

看板旁证（PR #45）记下的纸面侧例：胜率约一半仍可净亏；最差单笔约 **−84**；剔除最差后早段盈利被抹掉。那是观察，不是周检样本。

2026-10-14 纸面旁证（**不算有效性**，挂到 2026-10-19 周检）：实现约 **−317**。主因是 `stop_loss` ×3 约 **−305**；次要是 `max_hold` ×40 约 **−12**。当时空仓。`order_events` 已有 **1** 次日亏拒绝，reason 形如 `daily_loss:2026-10-14:…<-200.0`。日亏闸是保护，不是失效。信号或成交量不等于盈亏。此后新拒绝的 reason 是稳定键 `daily_loss`，`today` / `pnl` / `limit` 写在 detail，看板分成 `risk_rejected:daily_loss`。不据此改 `EXECUTION_DAILY_LOSS_USD`、TP/SL/hold 或 PM 数值。

同层索引：指标层纪律在 `crawler/INDICATORS_AUDIT.md`，采集侧在 `crawler/STABILIZE.md`「警示事例 → 采集侧」，决策侧在 `DECISION_ENGINE_WARNING_CASES.md`（PR #47）。调研映射（M-4 / M-5 / M-7）在 [`RESEARCH_MAPPING.md`](RESEARCH_MAPPING.md)。E-2 / C4 `target_band_failover` 已登记、不改运行时，见 [`WAITING_ITEMS.md`](WAITING_ITEMS.md)。

## 三条警示 → 执行侧

| 警示 | 执行侧看到什么 | 已有控制 | 不要做 |
|------|----------------|----------|--------|
| **半胜仍亏 / 单笔抹盈** | paper 胜率约 50% 仍可净亏。一笔薄池 `stop_loss`（约 −84，liq≈12k，滑点 2%）抹掉早段盈利 | `pricing.py` 的滑点档只给成交价记成本。PM 与日亏是第二道，现状见 `P1_PM_ROADMAP.md` | 不要把滑点档当成硬安全带；不要为了把曲线拧平而盘中改 SL% |
| **$0.95 黑天鹅 → 薄池打穿** | 名义止损 5%，标记价约走了 −17%，再叠加 2% 滑点，单笔亏损大于名义 SL | 入场质量归决策 `min_liq=50k`（[PR #41](https://github.com/tspike630/trade/pull/41)）。执行保留滑点记账 + PM | 不要在执行层用改 SL% 去修这条曲线 |
| **洗盘 / 单点** | 不归执行层新参数 | 人在环周检；多源行情在采集侧（`crawler/STABILIZE.md`） | 不在这里加数据源或预言机逻辑 |

### 半胜仍亏 / 单笔抹盈

短窗胜率约一半仍可以净亏。亏在仓位、边际和成交成本。名义止损百分比保持 `execution/exits.py` 的 `DEFAULT_SL_PCT = 5.0`（信号未带 `sl_pct` 时；TP / max_hold 仍是 10% / 60min）。

纸面侧例（观察，不算有效性）：一笔薄流动性 `stop_loss`，流动性约 **12k**，滑点档 **2%**，盈亏约 **−84**，抹掉此前盈利。这笔的价格位移大约 **−17%**，大于名义 5%。止损触发比较的是 **mid 与入场成交价**（入场已含买滑点）；平仓成交再吃一档卖出滑点。薄池里 mid 可以穿过名义线。滑点档解释的是成交成本，解释不了整段 −17%。

`execution/pricing.py` `slippage_rate`：

| 流动性 | 滑点 |
|--------|------|
| `<50k` 或未知 | 2% |
| `50k ~ 500k` | 1% |
| `≥500k` | 0.3% |

liq≈12k 落在 `<50k`，所以是 2%。买入成交 `mid × (1+slip)`，信号卖与 tick 平仓 `mid × (1−slip)`。权益标记用 mid，不用滑点后的成交价。

这些档是成本模型，**不是硬安全带**。它们不拒绝开仓，也不把亏损封在 5%。

这笔平仓走 paper tick（`execution.exits.scan_exits`）。Tick 只更新 paper 持仓和 `fills`，不追加 `execution_log` / `order_events`（`decision_log_id` 在开仓回执上唯一）。周检要找这笔记账，看 paper 的 `exit_reason=stop_loss` 与当时流动性。−84 不在 `order_events` 里。

### $0.95 黑天鹅 → 薄池打穿

书里的类比：标在 0.95 时，失败面看起来只剩 0.05，一次打穿亏掉的是 95%。映射到本系统，就是薄池一笔打穿抹掉多笔小赢。这不是要去交易 0.95 的合约，也不产生新的止损百分比。

入场质量在决策层：live buy 地板 `DECISION_MIN_LIQ_USD=50000`（PR #41）。执行层不拥有这条闸。执行保留两道已经存在的第二线：

1. 滑点档（上表）— 记账成本。
2. PositionManager + 预交易风控 — 仓位与日亏。参数现状见 [`P1_PM_ROADMAP.md`](P1_PM_ROADMAP.md)。

盘中把 SL% 从 5 改成别的数，就是第三条统计参数。禁止。信号自己带了 `tp_pct` / `sl_pct` / `max_hold_min` 时仍用信号值；缺省保持 10% / 5% / 60min。

### 周检拒因：从 `order_events` 分桶

PM 拒绝发生在校验通过之后。`validation_ok=1`，`validation_errors` 是空的。原因在事件流里，不在校验错误列。

同一笔回执上，`order_events.event = 'rejected'`，`detail_json.stage = 'position_manager'`，`detail_json.reason` 形如：

- `pm_rejected:max_open_per_strategy`
- `pm_rejected:token_dedup`
- 同格式还有 `pm_rejected:strategy_mutex`、`pm_rejected:max_per_token_pct`、`pm_rejected:max_total_pct`、`pm_rejected:no_ledger`

回执 `receipt_json.status` 是 `pm_rejected`，`error` 与事件里的 reason 相同。`execution_log.status` 因为有执行错误而写成 `rejected`。

预交易风控拒绝走同一条事件，`stage='risk'`。`max_open` 的 reason 仍形如 `max_open:<strategy>:<n>>=<limit>`。日亏拒绝的 reason 是稳定键 `daily_loss`（date / pnl / limit 不在 reason 里），`detail` 带 `today`、`pnl`、`limit`。回执 `risk[]` 同形，看板把它分成 `risk_rejected:daily_loss`。10-14 那条历史行仍是 `daily_loss:<date>:<pnl><-<limit>`，按前缀 `daily_loss` 归并。这些也不进 `validation_errors`。

对照：purpose 门禁（`purpose_sample` / `purpose_missing` / `purpose_invalid`）会写入 `validation_errors`，事件 `stage='purpose'`。校验失败是 `stage='validation'`。10-19 旁证如果只扫 `validation_errors`，会漏掉 PM 与风控拒绝。

分桶（只读；`EXECUTION_EVENTS_ENABLED` 默认开）：

```sql
SELECT
  json_extract(detail_json, '$.stage') AS stage,
  COALESCE(
    json_extract(detail_json, '$.reason'),
    json_extract(detail_json, '$.error')
  ) AS reason,
  COUNT(*) AS n
FROM order_events
WHERE event = 'rejected'
GROUP BY 1, 2
ORDER BY n DESC;
```

`UNIQUE(execution_log_id, event)`：每个信号最多一条 `rejected`。Tick 平仓不在这张表。

## 归属

| 项 | 谁做 | 本文件 |
|----|------|--------|
| 入场 `min_liq=50k` | 决策层 PR #41 | 只引用。不改地板 |
| 滑点档 | 执行 `pricing.py` | 说明它是成本模型。不改档位 |
| 名义 SL 5% | `exits.py` 默认 | 不盘中改 |
| PM / 日亏现状与缺项 | `P1_PM_ROADMAP.md` | 指针 |
| 10-19 PM 拒因旁证 | 本层 `order_events` | 上节 SQL。不算有效性 |
| C4 / E-2 `target_band_failover` | 执行观察登记 | 只登记。见 [`WAITING_ITEMS.md`](WAITING_ITEMS.md)。不改 tp / sl / max_hold |

## 不算有效性

- 约 50% 胜率、约 −84、约 −17% 价格位移、liq≈12k，都是纸面观察，不拿去过周检门槛。
- 在 2026-10-19 周检之前，不把这些数字当成调 SL、调滑点档、调净边际的依据。
- 纪律：R1 周报只读（`docs/REVIEW_R1_SPEC.md`）；R2 完整归因之后才谈净边际 X 与账户回撤；R3 寻优不在盘中追认（`docs/REVIEW_R3_SCAFFOLD.md`）。
