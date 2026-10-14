# 决策层 — 调研映射（只读）

**Time (HK / UTC+8):** 2026-10-14  
**用途：** 两批文献在决策层怎么读。不改代码默认，不加过滤，不改门槛。  
`crawler/DECISION_ENGINE_RESEARCH_MAPPING.md` 与本文同文。本文件**不算有效性**。

纸面回撤旁证见 `DECISION_ENGINE_WARNING_CASES.md`。M-1 功效不在本文重抄。

## 第一批 → 不上 LLM 决策主路径，不上 agent 栈

Profit Mirage、StockBench、LLM≠humans、MAS·adaptive BTC：端到端 LLM 或多智能体当决策主路径时，回测或叙述里的收益不能升格成部署。决策主路径仍是规则 `jev_strength_v1`。不上 LLM 决策主路径，不上 agent 栈。

## 第二批 → 只读

Prophet Arena、LiveTradeBench、When Agents Trade、FinPos、PredictionMarketBench：**校准好 ≠ 赚钱**，**回测好看 ≠ 有效**。只作解读，已被 M-6 与 M-1 功效脚注覆盖。**不加新过滤。**

## 指针

| 已有 | 在哪 |
|------|------|
| **M-1** 统计功效 | `DECISION_ENGINE_WEEKLY_CHECK.md`「统计功效标注」（[#54](https://github.com/tspike630/trade/pull/54)）。unique_token≥10、raw n≥15、连续两周 1h ≥55% **不改** |
| **M-6** 高信心≠高收益 | `DECISION_ENGINE_WARNING_CASES.md`。信号量 ≠ PnL。临时 `--min-jev-strength 0.15` 且 `purpose=live_candidate` 保持未标记 |

不改 `JEV_MIN_STRENGTH`（仍是 **0.3**）、因子权重、`DECISION_COOLDOWN_SEC` 代码默认（仍是 **0**）、`DECISION_MIN_LIQ_USD` 默认（仍是 **50000**）。不改 `RiskManager`。**不算有效性。**
