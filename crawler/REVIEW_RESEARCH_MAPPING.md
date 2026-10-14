# 复盘优化层 · 调研映射

> 状态：指针（2026-10-14）
> 范围：Alpha Illusion 如何支撑已有 R1 / R2 / R3 与 paper 观察期；10-19 `factor_loop` 收哪些候选。不改 Python、门槛、默认参数。
> **不算有效性。**

`crawler/REVIEW_RESEARCH_MAPPING.md` 与本文同文。

## M-1 只作指针

周检门槛能主张什么，已经写在 `DECISION_ENGINE_WEEKLY_CHECK.md`「统计功效标注」（[#54](https://github.com/tspike630/trade/pull/54)，`crawler/` 同文）。复盘不重抄那段样本量级。

读 `exports/reports/review_weekly_*.md` 里的 n、unique_token、1h 上涨率时沿用该段：unique≥10 且 n≥15 只排除纯噪声级结论。当周上涨率仍按决策表单独读。临时 `--min-jev-strength 0.15` 且 `purpose=live_candidate` 的行单独脚注，不算有效性。**不改门槛数字。**

## The Alpha Illusion

[The Alpha Illusion](https://arxiv.org/abs/2605.16895)（arXiv:2605.16895）把端到端 LLM 交易代理报出的 alpha 放在回测或原型证据里，不把它当成部署证据。最低报告协议是 **P1–P6**（时间完整性、动态样本、反事实稳健、认知校准、真实成本、多智能体拆开），按主张强度分级。协议过了也不等于可部署。论文给出的稳妥结构是：LLM 做可审计的信息接口，校准、风险、执行各自独立。

这从学术上支撑我们已经在用的切分，不新开策略、不改闸门：

| 已有 | 证据停在哪 |
|------|------------|
| **R1** 只读信号绩效周报 | 历史 / 原型。不上实盘，不改参 |
| **R2** 归因 | 观察分桶。样本不足不调参 |
| **R3** 寻优脚手架 | 时间切分 + 稳健性。不过门槛不跑正式寻优，不写配置 |
| **paper 观察期** | 观察进程。回测数字和 LLM 叙述都不升格成部署结论 |

`factor_loop --llm` 若打开，也只停在信息接口。风险和执行仍在执行层。PM / RISK 不进 prompt 是执行层 **M-4**（`execution/RESEARCH_MAPPING.md`）。复盘不认领 M-4。

## factor_loop 收录（10-19）

**M-2 方向校准** 与 **M-3 算子化** 归指标加工层。复盘不写这两条的规格。执行层对 M-3 只留一行归属，规格不在执行（同文）。

2026-10-19 的 `review.factor_loop` **只接受**指标侧已经同时完成方向校准和算子化的候选。缺任何一条，不写 `status=accepted`。`accepted` 仍不是 `activated`。`INDICATORS_WEIGHT_*` 保持 **0**。临时 0.15 的 `live_candidate` 连 accepted 都不要写。

不认领 **M-4 / M-5 / M-7**（执行层，`execution/RESEARCH_MAPPING.md`）。不认领执行层的 PM **P-1**（仓位比例 / 回撤）。

## 首次命中（Prediction Arena 的 P-1）

口径已与看板 [#52](https://github.com/tspike630/trade/pull/52) 对齐：按 `pair_address` 取最早一笔已平仓，`pnl_usd>0` 记命中；`purpose=live_candidate` 可另列。旁证。指标侧不登记（`crawler/INDICATORS_FACTOR_CANDIDATES.md`）。与执行层 PM 的 P-1 不是同一条。**不算有效性。**

## 不算有效性

- 不改 A1 门槛、`JEV_MIN_STRENGTH`、权重、`DECISION_COOLDOWN_SEC` 的代码默认、`DECISION_MIN_LIQ_USD`
- 不改 `review/`。10-19 的收录规则写在本文，这次不改 `factor_loop.py`
- 文献里的协议和样本量级不进决策表
