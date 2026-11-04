# 量化失效前沿·周调研辩证 2026-11-04（指标加工层）

- **角色：** 指标加工层 · 旁路只读辩证（非执行改闸）
- **今日：** 2026-11-04 Asia/Hong_Kong
- **窗：** ≈2026-10-21 → 2026-11-04（近 1–2 周；相对 11-02 采集/看板周研增量）
- **硬约束（现网锚，不发明状态）：**
  - `INDICATORS_WEIGHT_*` 默认 **0**；观察/候选 **永不**盘中接闸
  - I-3 / `high_bot_skip` **未接线**（wired=False）
  - **#72** 退出质量 vs 衰减分栏（半胜仍亏主因退出质量，不把文献当有效性判决）
  - **#91** `ALPHA_DECAY` **观察口径**（只旁证，不冠名/不改权）
  - JEV 仅为旁路数据入库；**禁止**写「JEV 已被证伪」
  - 本周 **不**建议、不执行：改权、盘中门禁、live/#71、注资、接 skip
  - 若值得登记：仅 **候选表 / docs** 路径建议，不激活

本地 PDF 目录：`/workspace/alpha_decay_frontier_2026-11-04/`

---

## 1. 候选清单与全文诚实状态

| # | 文献 | 日期 | URL | FULLTEXT |
|---|------|------|-----|----------|
| 1 | Benhamou et al.，《Penalize the Common Trade: A Collision-Aware Layer for Learned Portfolios》 | **2026-11-03** arXiv:2611.03453 | https://arxiv.org/abs/2611.03453 · PDF 本地 `penalize_common_trade_2611.03453.pdf` | **FULLTEXT_OK**（HTML abs+正文 + PDF≈595KB） |
| 2 | Benhamou et al.，《Can We Stop the Backtest Yet? Anytime-Valid Promotion Tests…（Promote）》 | **2026-11-03** arXiv:2611.03384 · ICAIF 2026 | https://arxiv.org/abs/2611.03384 · `promote_anytime_2611.03384.pdf` | **FULLTEXT_OK**（HTML+PDF≈648KB） |
| 3 | Wu，《AIDA: An AI Outage Registry with Evidence from On-Chain MEV》 | **2026-11-03** arXiv:2611.01656 | https://arxiv.org/abs/2611.01656 · `aida_mev_2611.01656.pdf` | **FULLTEXT_OK**（HTML+PDF≈580KB） |
| 4 | Bai & Kan，《Herding or Racing? Decomposing Cross-Account Order-Flow Persistence…》 | **2026-11-02** arXiv:2611.02616 · ICAIF’26 workshop | https://arxiv.org/abs/2611.02616 · `herding_racing_2611.02616.pdf` | **FULLTEXT_OK**（HTML+PDF≈504KB） |
| 5 | Wang & Ventre，《FactorBench: A Portfolio-Aware Benchmark for Automated Factor Mining》 | **2026-10** arXiv:2610.06947（略早于主窗起点，作过拟合/新颖性强对照保留） | https://arxiv.org/abs/2610.06947 · `factorbench_2610.06947.pdf` | **FULLTEXT_OK**（HTML+PDF≈14.2MB） |
| 6 | Quant Enthusiasts，《A Factor Can Keep Working and Still Become Untradeable》 | **2026-10-21** Substack | https://youngandcalculated.substack.com/p/a-factor-can-keep-working-and-still | **FULLTEXT_OK**（公开部分 WebFetch；后部付费墙 → 机制摘录仅限已见段落） |
| 7 | Chigireva，《Market Capacity and Crowded Alpha…》SSRN | **2026-10-09** | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7588318 | **DEFERRED**（本轮未强开 SSRN 全文；仅摘要级） |
| 8 | Sheikh & Bhat，《Pockets of predictability…》Springer | 2026-10-02 | 先验周研已全文 | **本周不重开** |
| 9 | Gafarov 2610.39458 / Páez 2610.29485 / Certified Alpha Capacity 2610.01115 / From Alpha to Execution 2610.23749 | 10-21～11-02 窗内已由 11-02 周研全文 | 见 `/workspace/alpha_decay_frontier_2026-11-02/` | **本周不重开**（避免重复辩证） |
| 10 | Early Rug Pull wash-trading BSC arXiv:2603.13830 | 2026-03 | arXiv | **窗外** → 不纳入本周强对照 |
| 11 | Feng crowding/vol-timing SSRN | ≈10-23 | SSRN | **FAIL**（先验 SSRN 403；本轮未复开） |

**计数：** FULLTEXT_OK = **6**（#1–#6）；FAIL = **1**（#11）；DEFERRED = **1**（#7）；显式不重开/窗外 = 其余。

arXiv 扫面旁证：`q-fin/recent`（Tue 3 Nov 2026）与 `q-fin.TR/recent` 已人工过目；本周新命中以 #1–#4 为主。

---

## 2. 机制摘录（仅 FULLTEXT_OK）

### A. Penalize the Common Trade（2611.03453）— 信号碰撞 / 共同交易外部性
- 关键：不同学习系统可因共享数据/目标/表征做出**几乎相同的交易** → 「signal collision」；市场看见的是交易，不是模型名。
- 线性冲击博弈：私人纳什暴露 \(x^{\mathrm{NE}}\) 大于社会最优 \(x^{\mathrm{SO}}\)；重叠通行费 \(L x_i Q_{-i}\) 可实现社会选择。
- 反馈稳定阈：\(\lvert\lambda N(a\rho+b)\rvert/(1+cN)<1\)；共同动作惩罚 \(c\) 可衰减环增益。
- 实证（冻结 10 ETF、760 周至 2026-07、10bp 成本）：动量基线 MDD −23.6% → collision-aware \(\phi=0.75\) 时 −14.1%；Sharpe 几乎不变（0.664→0.667）；**配对 bootstrap MDD 增益区间含 0**（[−3.3, 18.4]pp）。
- 作者自限：需 holdings/flow 才能把 PC1 代理归因到「AI crowding」；当前是可审计风险控制关联，非因果证明。

**批判标签：** 股票/ETF 周频尺度；数值 **拒绝**迁入 meme 5s。机制「共同方向加载→尾部」可作观察词表。

### B. Promote（2611.03384）— anytime-valid 晋升门 / 过拟合工作流
- 自适应研究代理会持续挖规则、放弃弱者、盯着阈值；未校正重复检验在 null 路径上 **FWER≈94.5%**；Promote 财富守恒 e-过程 → 同设定 **0.0%** 假族晋升。
- 规则：出生时分配可预测测试财富 \(\pi_h\)；\(\sum\pi\leq 1\)；只用**出生后**成本调整得分更新；晋升后经济仓位不早于 \(\tau_h+1\)。
- 关键义声明：证书控制的是「历史假晋升」；**不是**「先前边在部署时仍赚钱」——需另开 funded revalidation epoch。
- 市场伪 OOS 重放：24 候选 **0 晋升**；最大证据约 1.93 vs 边界 480。

**批判标签：** 对指标侧「满窗∧n≥30 才冠 / 权仍 0」高度同向；**拒绝**把 e-过程直接接成盘中权重门禁。

### C. AIDA + On-Chain MEV（2611.01656）— AI 掉线 vs 已部署 bot
- AIDA：30 供应商、5 868 去重事故；配对 500 万+ 已验证 MEV 交易。
- 发现 1：套利/清算地址入场扩张（公开状态+代码即可），夹心攻击者收缩（需私有流/builder）；执行仍集中（前 50 地址≈2/3 成交）。
- 发现 2：AI 多小时中断日，**新 executor 发布**约降至 0.67×，滞后 2 日仍低，第 3 日反弹；**已部署套利/夹心执行量**基本不变。
- 含义：便宜代码降低「建 bot」门槛；私有流/延迟/资本决定「谁吃到执行」。

**批判标签：** 不证明、也不证伪任何 JEV/指标有效性；对「high_bot_skip」仍是观察旁证，**拒绝接线**。勿把「AI 掉线」误读成策略 alpha 死。

### D. Herding or Racing（2611.02616）— 勿把跨钱包相关当成羊群
- 分解：herding share \(h\) 混了同账户概率 \(p\) 与跨/同账户相关比 \(\kappa\)。
- Uniswap v3 小单：\(h\) 高（0.65–0.86）但 \(\kappa\) 仅 **0.05–0.10** → 成对看像拆单，不是模仿羊群。
- 链上订单簿 / Uniswap ≥$100k：\(\kappa\) 升至 ~0.34–0.76；高 \(\kappa\) 多为 **CEX 价格竞速**；仅 Binance 特征的交叉拟合过滤器可去掉约 ≥3/4 跨账户协方差。
- LP 主要为竞速流付费（对齐 Binance 的三分位 markout 最差）。

**批判标签：** 强适用「I-3 bot_trade_share / 地址标签」**度量诚实**——高跨钱包共向 ≠ 羊群；需 \(\kappa\) 或信号条件化。**拒绝**据此改权或接 skip。

### E. FactorBench（2610.06947）— 挖矿范式进步 ≠ OOS 经济价值
- ~5 000 因子 / 9 方法 / 5 股市；训练 IC 常高、验证/测试塌；风格中性后残差 IC 常近零或负。
- 池内冗余高（尤其部分 RL）；LLM 池更靠近 Alpha101。
- **无一范式在因子→池→组合全链路上一致占优**；多头账面漂亮不等于多空残差 alpha。

**批判标签：** 对「因子新颖性/过拟合」词表强；股票日频 20d 目标，**拒绝**把其 CAGR/Sharpe 当 paper peer 冠名依据。

### F. YoungandCalculated（2026-10-21）— 信号仍在 ≠ 可交易
- 拥挤可不杀死预测关系，但抬升冲击/融资/尾部；\(\alpha_{\mathrm{net}}=\alpha_{\mathrm{gross}}-\)成本项。
- AQR 综述口径：简单拥挤指标弱预测「alpha 消失」，较强预测「挤兑尾部」。
- 容量是 alpha 的一部分：\(P(Q)=\alpha Q-C(Q)\)。

**批判标签：** 与 **#72 退出质量 vs 衰减分栏**、**#91 ALPHA_DECAY 观察**同向；纸面小规模下「AUM 容量」优先级仍低于微观冲击/退出质量。付费墙后半未强开 → 不外推未见表格。

---

## 3. 辩证对照（文献机制 × 指标旁路）

| 文献机制 | 现网能/不能 | 立场 |
|----------|-------------|------|
| Promote：出生后证据 + 财富预算 + 晋升≠同日交易 | 现网已有「满窗∧n≥30 才冠 / 权=0 / 观察不盘中接闸」；**无** e-过程 ledger | **采纳观察**（强化 #91 观察口径与「不中途接闸」）；**拒绝接线** anytime 门禁进权重 |
| Collision layer：惩罚共同收益模态 | 多纸面 peer 并行、SIZE_MULT=20；无「共同交易方向」PC 代理层 | **采纳观察**（候选 docs：collision/crowding 词表）；**拒绝**盘中 φ 惩罚或改权 |
| Herding \(h\) vs \(\kappa\)：小单跨钱包相关≠羊群 | I-3 `bot_trade_share` 稀、单地址标签；缺 \(\kappa\)/CEX 对齐分解 | **采纳观察**（度量勿误读）；**拒绝** high_bot_skip |
| AIDA：新 bot 发布停、执行不停 | 与 skip 门禁无关；JEV 旁路存活≠有效性 | **弱对照**；**禁止**「JEV 证伪」表述；**拒绝接线** |
| FactorBench：挖矿 IC 塌/风格重叠 | 指标不造挖矿动物园；观察榜已分列 WIPE/OUTLIER/r90 | **采纳观察**（过拟合/新颖性）；**拒绝**用训练 IC 冠名 |
| 信号仍在 vs 不可交易 | #72 退出质量分栏；tech_fade OUTLIER 先例 | **采纳观察**；**拒绝**改 hold/TP/SL/权 |
| 发表后衰减数值（McLean 族） | 本周无新股票异象半衰期强增量（已在先验） | **拒绝套用数值**到 meme 5s |

**总判：** 本周有 **可登记对照项**（非「无新」），但全部落在 **观察/候选 docs** 层；**零**建议立即改权或盘中接闸。文献描述市场机制 ≠ 本系统 paper 有效性已证实/证伪。

---

## 4. 对照项登记建议（≤3，不激活）

仅建议写入候选表或 docs，**不激活、不改代码闸**：

1. **OBS-W1104-1（#91 / Promote）：** 「出生后证据 + 晋升与部署分离 + funded revalidation」对照现网满窗冠名与权=0；若落 docs，建议路径 `docs/obs/alpha_decay_promote_protocol_20261104.md`（trade 仓 docs 旁证，非 `.env`/权）。
2. **OBS-W1104-2（I-3 度量）：** herding share vs \(\kappa\)；禁止把「跨地址同向」直接标羊群/接 skip；候选表备注即可。
3. **OBS-W1104-3（#72）：** 「信号仍工作 vs 实现层不可交易 / collision 尾部」→ 继续分栏退出质量 vs 衰减；不合并成单一「失效」旗。

Collision-aware / FactorBench / AIDA：记入周研证据即可，**不**单开激活项。

---

## 5. 群发短版草稿（「我要赚米」，2–6 句）

【指标｜量化失效周研 1104】
窗内新全文 4+2：Promote anytime 晋升门（2611.03384）、共同交易碰撞层（2611.03453）、链上 herding≠κ（2611.02616）、AIDA/MEV+AI 掉线（2611.01656）；旁证 FactorBench 挖矿 OOS 塌、10-21「因子仍在可不可交易」。  
对照：强化 #91 观察口径与「晋升≠部署」；I-3 勿把跨钱包共向当羊群；#72 继续分栏退出质量 vs 衰减。  
权仍 0；high_bot_skip 未接；不改闸、不算有效性、不写 JEV 证伪。证据：`/workspace/quant_decay_weekly_20261104.md`。

---

## 6. 执行者声明

- 本轮：WebSearch + arXiv recent/TR + WebFetch HTML + PDF 落盘 `/workspace/alpha_decay_frontier_2026-11-04/`。
- **未**改权重、**未**接 high_bot_skip、**未**盘中门禁、**未** git push、**未**宣称 JEV 证伪、**未**改 paper 闸。
- SSRN #7 DEFERRED；Feng SSRN FAIL（先验）。
- trade 仓 docs 建议见 §4；仅当箱内已有 clone 且获令时再提交——本轮只写 `/workspace`。
