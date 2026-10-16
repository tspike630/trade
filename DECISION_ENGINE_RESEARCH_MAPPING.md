# 决策层 — 调研映射（只读）

**Time (HK / UTC+8):** 2026-10-17 00:32 HKT（AlphaAgent 准入纪律 + 资料包防漏读）  
**用途：** 两批文献在决策层怎么读。不改代码默认，不加过滤，不改门槛。  
`crawler/DECISION_ENGINE_RESEARCH_MAPPING.md` 与本文同文。本文件**不算有效性**。

纸面回撤旁证见 `DECISION_ENGINE_WARNING_CASES.md`。M-1 功效不在本文重抄。

## 第一批 → 不上 LLM 决策主路径，不上 agent 栈

Profit Mirage、StockBench、LLM≠humans、MAS·adaptive BTC：端到端 LLM 或多智能体当决策主路径时，回测或叙述里的收益不能升格成部署。决策主路径仍是规则 `jev_strength_v1`。不上 LLM 决策主路径，不上 agent 栈。

## 第二批 → 只读

Prophet Arena、LiveTradeBench、When Agents Trade、FinPos、PredictionMarketBench：**校准好 ≠ 赚钱**，**回测好看 ≠ 有效**。只作解读，已被 M-6 与 M-1 功效脚注覆盖。**不加新过滤。**


## AlphaAgent / 因子准入纪律（arXiv:2502.16789）

文献：[AlphaAgent: LLM-Driven Alpha Mining with Regularized Exploration to Counteract Alpha Decay](https://arxiv.org/abs/2502.16789)。**只读**。不上 LLM 决策主路径，不上 agent 挖因子栈，不改 `JEV_MIN_STRENGTH` / 权重 / cooldown / `DECISION_MIN_LIQ_USD`。

论文用三道正则压 alpha 衰减：相对既有库的 **原创性**（AST 相似）、假设与因子的 **对齐**、**复杂度上限**。本层落成**准入纪律**（候选≠生效）：

| 准入须 | 本层怎么读 | 不要做 |
|--------|------------|--------|
| **原创性**（相对既有因子） | 新因子相对已挂候选 / 已有策略须可陈述差异；同构重贴不算新候选 | 不要把「换个名字的旧式」标成新因子 |
| **可证伪市场假设** | 须有可证伪的市场假设（何失效、何观察能打脸）；对齐论文的 hypothesis–factor 一致性，本层加严为可证伪 | 不要只写叙事、不可检验的故事 |
| **复杂度上限** | 表达式 / 参数 / 依赖面有上限；过复杂优先拆或拒 | 不要用堆算子换回测曲线 |

**仅回测好看 ≠ 准入。** 回测 / 短窗胜率 / LLM 自评都不能单独跨过上面三道。过了三道仍是**候选**，生效只走周检 + Step2（见 `DECISION_ENGINE_WEEKLY_CHECK.md`）。假因子与拥挤加速衰减的警示映射见 `DECISION_ENGINE_WARNING_CASES.md`。

文献（含本篇）**只读，不进 LLM 决策主路径**——与上文「第一批」同口径。

## 资料包防漏读机制

资料包 / 含多链接附件**回群前必须附**：

1. **每条 URL 状态**：`已打开` | `打开失败` | `延后`
2. **一句机制摘录**（该条对决策层有用的一句，不是全文复述）

**未列清单禁止写「已学完」。** 可声明「本轮优先 N 条、其余延后」，延后须进清单并标 `延后`。失忆恢复见 `docs/RECOVERY_DECISION.md`「资料包防漏读」。

## 指针

| 已有 | 在哪 |
|------|------|
| **M-1** 统计功效 | `DECISION_ENGINE_WEEKLY_CHECK.md`「统计功效标注」（[#54](https://github.com/tspike630/trade/pull/54)）。unique_token≥10、raw n≥15、连续两周 1h ≥55% **不改** |
| **M-6** 高信心≠高收益 | `DECISION_ENGINE_WARNING_CASES.md`。信号量 ≠ PnL。临时 `--min-jev-strength 0.15` 且 `purpose=live_candidate` 保持未标记 |

不改 `JEV_MIN_STRENGTH`（仍是 **0.3**）、因子权重、`DECISION_COOLDOWN_SEC` 代码默认（仍是 **0**）、`DECISION_MIN_LIQ_USD` 默认（仍是 **50000**）。不改 `RiskManager`。**不算有效性。**
