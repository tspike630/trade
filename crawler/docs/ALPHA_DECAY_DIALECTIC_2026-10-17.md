# Alpha 衰减辩证笔记（决策侧认领三块）

**Time (HK / UTC+8):** 2026-10-17 ~10:15 HKT  
**范围：** 真假衰减鉴别 / 拥挤与半衰期 / LLM·假因子·自毁悖论；顺带滑点≠信号失效。  
**硬约束：** 只读文献；不算有效性；不否决 Step2；**不改** JEV_MIN_STRENGTH / cooldown / min_liq / TP / SL / hold / max_open。  
**对照：** `#71` 止损占亏硬旁证、`#78` 拥挤健康度、`#90` AlphaAgent 三门槛；`DECISION_ENGINE_RESEARCH_MAPPING.md` / `WARNING_CASES.md` / `WEEKLY_CHECK.md`。

图谱来源：群「我要赚米」附件《Alpha衰减与量化失效：全景研究图谱》（豆包工作 AI 生成框架；本轮**不整篇复述**）。

---

## 1. 本轮 URL 清单（状态 + 一句机制）

| # | URL | 状态 | 一句机制摘录 |
|---|-----|------|--------------|
| 1 | https://arxiv.org/abs/2502.16789 | **已打开** | AlphaAgent：LLM 挖因子若无正则 → 同质化拥挤加速衰减；三道正则（AST 原创性 / 假设–因子对齐 / 复杂度上限）压衰减。 |
| 2 | https://arxiv.org/html/2502.16789v2 | **已打开** | 同上全文 HTML；明确 GP/过拟合与 LLM 同质化是衰减主因，非「立刻缩 hold」。 |
| 3 | https://arxiv.org/abs/2001.04185 | **已打开** | 因子拥挤：用订单流失衡与因子再平衡信号相关度量 crowding；Momentum 可解释 1–2% 订单流且近年上升 → 拥挤抬升冲击成本、侵蚀可实现收益。 |
| 4 | https://ar5iv.labs.arxiv.org/html/2001.04185 | **已打开** | 同上 HTML；强调 crowding → 表现恶化 + 交易成本↑ + 系统风险，**不是**自动否决信号本身。 |
| 5 | https://arxiv.org/abs/2105.01380 | **已打开** | Falck/Rej/Thesmar：OOS Sharpe 衰减主因偏**过拟合**（发表年份、运算复杂度、对异常值敏感）解释力远大于套利易度；发表年 alone ~30% 方差。 |
| 6 | https://gwern.net/doc/economics/2016-mclean.pdf | **已打开** | McLean–Pontiff：97 预测因子 OOS −26%、发表后 −58%；其中约 32ppt 归因发表后交易学习；相关升高 ≈ 拥挤同向。 |
| 7 | https://www.top1000funds.com/wp-content/uploads/2021/05/SSRN-id2580551.pdf | **已打开** | Di Mascio/Lines/Naik *Alpha Decay*：机构新买入 alpha 约 12 月内衰减至 0，半衰期约 **4 个月**；持有期(~2.2y) > 预测期 → **持仓错配**，不等于「应立刻拧 TP/缩 hold」。 |
| 8 | https://onlinelibrary.wiley.com/doi/10.1111/jofi.12365 | **打开失败** | Wiley 正文超时；机制以 #6 PDF 为准（同文 JoF 2016）。 |
| 9 | https://doi.org/10.1080/14697688.2022.2098810 | **延后** | QF 正式刊名 *When do systematic strategies decay?*；机制同 #5 arXiv 预印本已打开。 |
| 10 | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3986499 | **打开失败** | Factor Information Decay（Flint/Vermaak）：摘要层知因子暴露半衰期因因子而异（价值/质量更长、动量更快）→ 调仓周期应因子特异，非一刀切缩 hold。 |
| 11 | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5023380 | **打开失败** | Dynamics of Factor Crowding：进入门槛低的因子更易拥挤失效；高门槛因子更稳——对照我方 JEV 规则主路径 vs LLM 批量挖因子。 |

**本轮优先已打开可核验 ≥6：** #1–#7（含 HTML 镜像）。未列「已学完」；#8–#11 已标状态。

---

## 2. 图谱 vs 我方已有机制对照

| 图谱说法 | 我方已有 | 判定 |
|----------|----------|------|
| 拥挤：资金同向 → IC 下滑、踩踏 | `#78` `signal_crowding`：日均 buy / unique/buy / 峰值小时密度；**强制入周检健康度笔记**；**不否决 Step2**；不改参 | **已覆盖**（观察口径）；图谱「高拥挤降杠杆」对我方 = **夸大勿跟**（无盘中熔断权） |
| Alpha 半衰期变短 → 动态缩短持有期 | paper `max_hold=60`、cooldown 观察 3600s；决策层**不改 hold/TP/SL** | **半覆盖**；图谱「跟随半衰期动态调仓」= **夸大勿跟**（文献半衰期月级，我方 5s paper + hold60 是执行设计，不是证据要拧参） |
| 过拟合伪 Alpha vs 真衰减 | 周检门槛 + M-1 功效脚注；退役表「连续 N 周 <50% 且样本足 → 降级勿无限调参」；Falck 等：复杂度/outlier 敏感预测衰减 | **已覆盖方向**；缺口：尚未做正式**置换检验 / 滚动 IC 证伪流水线**（只观察斜率脚注）——**进 10-19 旁证观察**，不算有效性 |
| Regime shift / 风格回撤误判衰减 | 周检「不算有效性」、临时 0.15 未标记；#71 硬旁证只认止损占亏/日开仓 | **已覆盖精神**；缺口：无显式 regime 标签分桶——**观察-only** |
| 滑点吞噬 Alpha（执行侵蚀） | WARNING：半胜仍亏、止损占亏主导（~71% 量级旁证）、薄池打穿；`DECISION_MIN_LIQ`、cooldown 观察 | **已覆盖且更准**：图谱易把执行侵蚀说成「Alpha 衰减」→ 我方结论必须是 **退出质量/执行侵蚀 ≠ 信号失效，勿否决 JEV** |
| LLM 因子爆炸 / 自毁悖论 / Meta-Alpha | RESEARCH `#90` 三门槛；第一批「不上 LLM 决策主路径」；WARNING 假因子行 | **已覆盖**；图谱「核心竞争力=隐藏 Alpha / 实时自适应下线」= **夸大勿跟**（会把 LLM 挖因子升主路径） |
| 因果因子更慢衰减 / 相变 / 营养级 | 无对应实现 | **缺口延后**；与现网 paper 半胜仍亏无关，不进本轮改文档主文 |
| 对抗衰减：动态半衰期系统自动拧 hold | 与「不改运行参」硬冲突 | **夸大勿跟** |

---

## 3. 对现网 paper 的批判结论（止损占亏主导、近 24h 略正、max_open=2）

1. **什么不该改（本轮故意不改）：**  
   `JEV_MIN_STRENGTH`（0.3）、`DECISION_COOLDOWN_SEC` 代码默认（0）、临时观察 cooldown=3600、`DECISION_MIN_LIQ_USD`（50000）、TP/SL、`max_hold`/hold60、`max_open`（含 PM_MAX_OPEN=2 一类）、权重、任何「跟着半衰期缩 hold / 拧 TP」冲动。

2. **滑点/止损 vs 信号：**  
   止损占亏 ~71% 量级旁证说明亏在**退出路径与冲击**，不是「JEV 已发表后衰减」；McLean 发表后衰减是横截面因子被套利的故事，不能直接映射到「盘中改 strength」。**退出质量 ≠ 信号失效 → 不否决 JEV、不否决 Step2（除非 #71/#72 硬门槛触发）。**

3. **拥挤：**  
   Volpati et al. 支持「拥挤抬成本」；我方 `#78` 已是正确落点（健康度旁证）。**勿**把拥挤升格熔断，**勿**据此缩 hold。

4. **真假衰减：**  
   Falck et al.：过拟合指标（复杂度、outlier 敏感、发表年）预测衰减强于「易套利」→ 对照我方「回测好看≠准入」与 AlphaAgent 复杂度门槛一致。滚动 IC/置换检验：可作 **10-19 周检旁证观察脚注**，**不算有效性、不改 ≥55% 门槛**。

5. **LLM：**  
   图谱 3.1 自毁悖论成立为叙事，但 AlphaAgent 自己的结论是「要有三道正则」而非「上 LLM 主路径」。我方 `#90` 已把三门槛落成**准入纪律**；继续 **不上 LLM 决策主路径 / 不上 agent 挖因子栈**。

6. **进 10-19 周检旁证观察（只记不改参）：**  
   - 既有：cooldown 生效、`min_liq` 三桶、临时 0.15 未标记、`#78` 拥挤、`#71` 止损占亏。  
   - 建议加记一句：若有滚动 1h 胜率斜率脚注，注明「斜率下降 ≠ 真衰减；需对照止损占亏是否仍主导」——防把执行侵蚀判成 alpha 死亡。

---

## 4. 建议补进 RESEARCH / WARNING 的短 diff 草案（纯文档）

### RESEARCH_MAPPING 追加小节草案

```markdown
## Alpha 衰减图谱辩证（2026-10-17，认领三块）

来源：群附件《Alpha衰减与量化失效》框架 + 本轮核验文献（见 docs/ALPHA_DECAY_DIALECTIC_2026-10-17.md）。**只读。不算有效性。不改运行参。**

| 认领块 | 文献锚点 | 本层怎么读 | 不要做 |
|--------|----------|------------|--------|
| 真假衰减 | McLean–Pontiff；Falck/Rej/Thesmar | OOS/发表后衰减常混「过拟合伪 Alpha」与「学习交易」；过拟合代理（复杂度、outlier）预测力强。周检门槛 + 退役表已防无限调参；滚动 IC/置换只作旁证观察 | 不要把短窗回撤判真衰减；不要盘中改 strength |
| 拥挤与半衰期 | Volpati et al. crowding；Di Mascio et al. half-life≈4月 | 拥挤抬冲击成本；半衰期错配 ≠ 立刻缩 hold。对照 #78 健康度旁证（不熔断）、hold60 / 5s paper | 不要因半衰期叙事拧 TP/SL/hold；不要把 #78 升格硬否决 |
| LLM/假因子/自毁 | AlphaAgent (#90) | 自毁悖论成立为风险叙事；落点仍是三门槛准入 + 不上 LLM 主路径 | 不要把 LLM 挖因子当决策主路径 |

**滑点/止损：** 执行侵蚀（止损占亏主导）≠ 信号失效。勿据此否决 JEV / Step2（硬否决只认 #71/#72）。
```

### WARNING_CASES 追加小节草案

```markdown
## Alpha 衰减误判（2026-10-17 旁证）

| 警示 | 看到什么 | 已有控制 | 不要做 |
|------|----------|----------|--------|
| **执行侵蚀误判为 Alpha 死亡** | 止损占亏主导、半胜仍亏、滑点叙事 | #71 硬旁证；半胜仍亏映射执行层；本条辩证笔记 | 不要否决 JEV；不要盘中改 strength/hold/TP/SL |
| **半衰期冲动缩仓** | 「信号只能持有更短」类主张 | hold60 / cooldown 观察；文献半衰期月级 ≠ 我方应拧参 | 不要动态缩短 max_hold / 拧 TP |
| **拥挤熔断冲动** | 日均 buy↑、unique/buy↓ | #78 只旁证入笔记 | 不要升格硬否决 Step2 |
```

---

## 5. 本轮故意不改的运行参清单

- `JEV_MIN_STRENGTH`（默认 0.3）  
- 临时 `--min-jev-strength 0.15` 标记规则（仍未标记 / 不算有效性）  
- `DECISION_COOLDOWN_SEC` 代码默认 **0**；临时观察 3600 **不升默认**  
- `DECISION_MIN_LIQ_USD` / `FilterConfig.min_liq` **50000**  
- TP / SL / `max_hold`（含 hold60）/ time_exit  
- `max_open` / `PM_MAX_OPEN_PER_STRATEGY`（含现网 2）  
- 因子权重 / INTEL 权重 0 边界  
- 周检 ≥55% / unique_token≥10 / n≥15 门槛  
- `#78` 拥挤：不升格否决；`#71/#72` 硬门槛公式不改  

