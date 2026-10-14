# 复盘优化层 · 警示事例 → 复盘侧

> 状态：纪律说明（2026-10-14）
> 范围：复盘怎么读纸面警示，以及 **P-2 / P-3** 在 **2026-10-19** 周检交什么。不改 Python、配置、阈值、默认参数。
> **不算策略有效性。** A1 门槛（连续 2 周过滤后 1h ≥55% 且 unique_token≥10，见 `DECISION_ENGINE_WEEKLY_CHECK.md`）不动。

纸面观察：胜率约 **51%**，净盈亏仍为负。一笔低流动性（约 **1.2 万**）上的 `stop_loss` 约 **−84**，抹掉此前盈利。三条合称「半胜仍亏 / 单笔抹盈 / 薄池打穿」。

已经落地的是规则对齐，不是拟合曲线：live `min_liq=50000`（[#41](https://github.com/tspike630/trade/pull/41)，与回测同一道地板）；paper 观察 `DECISION_COOLDOWN_SEC=3600`（代码默认仍是 0）。临时 `--min-jev-strength 0.15` 只在 `purpose=live_candidate` 这条管道上，**不得写成 accepted**。下一轮证据是 **2026-10-19** 周一周检。

兄弟层：看板平仓旁证 [#45](https://github.com/tspike630/trade/pull/45)，指标纪律 `crawler/INDICATORS_AUDIT.md`（[#46](https://github.com/tspike630/trade/pull/46)），采集映射 `crawler/STABILIZE.md`（[#48](https://github.com/tspike630/trade/pull/48)）。决策层若另有警示笔记，P-1 仍归执行 / PM，不归复盘。

`crawler/REVIEW_WARNING_CASES.md` 与本文同文。

## 警示 → 复盘必须记下的三行

周报第 8 节现有的 liq 桶是**信号层前向收益**（`<50k / 50–500k / ≥500k / unknown`）。没有匹配成交时，成交块是 `no_fills`。那不是下面这三行。10-19 复盘笔记必须另记**已平仓**账，来源是 paper 已平仓，看板 #45 作旁证（胜率、最差单笔、剔除最差后、单笔抹盈、exit × liq）。不改 `review/report.py`。

| 警示 | 复盘侧必须写 |
|---|---|
| 半胜仍亏 | **胜率与期望分开。** 胜率 = 已平仓里 `pnl_usd>0` 的占比。期望 = 已平仓 `pnl_usd` 之和 / 笔数。约 51% 仍净亏时，两数都写，不把胜率当成期望。 |
| 单笔抹盈 | **最差单笔**（已平仓 `pnl_usd` 的最小值）。若去掉这一笔后账面为正、加上它为负，注明单笔抹盈。 |
| 薄池打穿 | 已平仓按入场流动性切 **`liq_<50k` vs `liq_≥50k`**，切点与 live `min_liq=50000`、看板 `liq_bucket_label` 相同。unknown 单列，不并进两边。约 1.2 万上的 `stop_loss` 落在 `<50k`。 |

适用时（胜率与净盈亏分裂，或薄池 `stop_loss` 打穿抹盈）把这三行标成 **R2 规则缺口旁证**：纸面规则曾落后于回测的 `min_liq`，#41 已做对齐。这不是新曲线，也不改 A1。**禁止**用这三行自动改 strength 或 `INDICATORS_WEIGHT_*`。

## 谁拥有什么

| 项 | 归属 |
|---|---|
| **P-2** 真库 IC/IR 之后的权重更新 | **复盘 + 指标。** 10-19 真库 IC/IR **首跑**之后只写设计（公式与门槛）。权重保持 **0**，不激活。指标侧指针：`crawler/INDICATORS_AUDIT.md`「P-2」。 |
| **P-3** `min_liq` 拒绝桶 + cooldown 是否生效 | **复盘拥有 10-19 旁证**，不是有效性。采集只交热层流动性覆盖（`crawler/STABILIZE.md`）。复盘记拒绝桶、cooldown，以及上表的平仓流动性桶。 |
| **P-1** PM 仓位比例 / 回撤 / 日亏 | **不归复盘。** `execution/README.md`：`PM_MAX_OPEN_PER_STRATEGY`、`PM_TOKEN_DEDUP`、`EXECUTION_DAILY_LOSS_USD`（默认未开）。 |
| **P-4** JEV 错价（信号强度 ≠ 胜率） | **不归复盘。** `crawler/INDICATORS_STEP2.md`。 |
| **P-5** Polymarket 共识概率观察候选 | **不归复盘。** 指标侧，当前未接线。 |
| 采集热层流动性覆盖 | **不归复盘。** #44 / `crawler/STABILIZE.md`。复盘引用，不改覆盖率门槛。 |
| **C3** 趋势出场 | **只观察。** 要立项先做 R3 回测。不盘中追 tp / sl / max_hold。 |

采集文里「止损与仓位是 P-2」只表示采集不改止损。执行单上的 **P-2** 是本表这一行。

## 周报怎么读门槛（M-1 指针）

n、unique_token、1h 上涨率的功效主张以周检「统计功效标注」为准（[#54](https://github.com/tspike630/trade/pull/54)，`DECISION_ENGINE_WEEKLY_CHECK.md`，`crawler/` 同文）。复盘周报沿用该段，不另写样本量级。过了 unique≥10 且 n≥15 只排除纯噪声级结论。**不算有效性。** Alpha Illusion 对 R1 / R2 / R3 与 paper 观察期的支撑，以及 10-19 `factor_loop` 只收指标已方向校准（M-2）且已算子化（M-3）的候选，见 `docs/REVIEW_RESEARCH_MAPPING.md`。

## 2026-10-19 复盘交付

与 `DECISION_ENGINE_WEEKLY_CHECK.md` 的登记表同一天。决策表三行（cooldown、`min_liq` skip、临时 0.15）保持原样；下面是复盘要交的。

1. **P-2。** 在现有周检 `review.run` 上打开已经存在的 `--factor-layers`（只读；`--min-strength 0.15 --min-liq 50000 --min-coverage 10 --min-price 1e-06` 沿用周检，不新开切点）。IC 写入默认 `exports/reports/factor_ic_history.csv`。一期只有 IC；IR 要至少两期（`ic_summary` 不足两期返回空）。空 IR 记「IR 未形成」，不用空值写权重。出数之后只写权重更新**设计**。`INDICATORS_WEIGHT_*` 保持 0，不激活。
2. **P-3。** 旁证两块，都不是有效性：`liq_below_min` / `no_liquidity` / 过闸 buy 三桶，以及 `cooldown_sec=3600` 与 skip 条数（登记口径已在周检表）；加上平仓 `liq_<50k` vs `≥50k`。不据此改 50000。
3. **平仓三行。** 胜率与期望分开、最差单笔、流动性两桶。适用时标 R2 规则缺口旁证。

```bash
cd /workspace/dexscreener-crawler
.venv/bin/python -m review.run --period 7d --report --factor-layers \
  --min-strength 0.15 --min-liq 50000 --min-coverage 10 --min-price 1e-06 \
  >> logs/review_weekly.log 2>&1
```

这是 10-19 的首跑，不是把 `--factor-layers` 改成常驻默认，也不是改 CLI 的默认参数。

## 纪律

- **不盘中加第三个统计参。** 这一轮已经在场的只有：`min_liq=50000`（规则对齐，已合）、观察用 cooldown 3600（代码默认仍 0）、临时强度 0.15（只在 `live_candidate` 命令上）。警示三行不是再加一道强度、权重或出场旋钮的理由。
- **accepted ≠ activated。** `review.factor_loop` 过门槛最高是 `status=accepted`（待激活）。accepted 不把权重打开，不启用策略。临时 0.15 的 `live_candidate` **连 accepted 都不要写**。
- **临时样本单独分桶。** `purpose=live_candidate` 且强度低于实时默认 0.3 的行，与 `purpose=sample`、与默认 0.3 的 `live_candidate` 分开记。`scripts/jev_validity.py` 不按 purpose 过滤，这些行会进周检输入；脚注它们，不要拿去过「连续 2 周 ≥55%」。平仓归因同样分开，避免把临时管道上的薄池止损混进默认账面而不标注。
- **C3** 见上表。观察可以写进当周笔记；退出参数的改动先有 R3 回测，再谈 R4。
- **factor_loop 收录。** 10-19 只接受指标 **M-2** 方向校准且 **M-3** 算子化之后的候选。缺一条不写 accepted。不认领 M-4 / M-5 / M-7。细则 `docs/REVIEW_RESEARCH_MAPPING.md`。不算有效性。

## 不改什么

- 不改 `JEV_MIN_STRENGTH`（仍 0.3）、`INDICATORS_WEIGHT_*`（仍 0）、`DECISION_COOLDOWN_SEC` 的代码默认（仍 0）、`DECISION_MIN_LIQ_USD`（仍 50000）
- 不改 A1 决策表、R3 寻优门槛、R4 闸门
- 不改 `review/` 代码。第 8 节前向分桶保持原样；平仓三行写在 10-19 笔记里
