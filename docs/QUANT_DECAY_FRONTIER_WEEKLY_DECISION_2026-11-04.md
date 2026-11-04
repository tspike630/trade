# 量化失效前沿周研 · 决策引擎层 · 2026-11-04 HKT

**范围：** 真假衰减 / 拥挤·半衰期 / LLM·假因子·假量 / 执行侵蚀≠信号失效。  
**硬约束：** 不算有效性；不改 TP/SL/hold/cooldown/min_liq/strength/max_open；不上 LLM 主路径；不接 `high_bot_skip`；live/#71 关；真金不注资；SIZE_MULT=20；满窗前不冠。  
**对照：** #71/#72 硬旁证、#78 拥挤健康度、#90 AlphaAgent 三门槛 / `promo_cpcv_gate_v0`；`DECISION_ENGINE_RESEARCH_MAPPING.md` / `WARNING_CASES.md`；现网观察顶 `tech_boll_fade` 出口质量。  
**交叉：** 指标 `docs/QUANT_DECAY_FRONTIER_WEEKLY_2026-11-04.md`（commit 33b960d）与 `docs/obs/alpha_decay_promote_protocol_20261104.md` 已有同窗笔记；下表仅列**决策自打开**条目。群帖必须是【决策｜…】，勿复读【指标｜…】原文。

**现网锚（2026-11-04 ≈09:00–10:20 HKT，勿发明）：**  
观察顶 `tech_boll_fade` ≈+117.56% · n_cl=30 · 空仓；日历按**首笔 fill** 起算未满 S7 → 不冠。出口旁证：30 笔净≈+$11,756，+$18,663 全来自 9 笔 TP；OOS EV 564→220 更像 TP 命中率掉；止损占负≈82% → **#72 退出质量分栏，勿写成信号死**。decision≈49 流；r90 两盘续；权/INDICATORS 观察。

本地全文：`/workspace/alpha_decay_frontier_2026-11-04/`（pdf + txt/）。

---

## URL 清单（防漏读）

| # | URL | 状态 | 一句机制摘录 |
|---|-----|------|--------------|
| 1 | https://arxiv.org/abs/2611.03384 · PDF 本地 `promote_anytime_2611.03384.pdf` + txt | **已打开** | Promote：出生时分配可预测测试财富 π；只积出生后成本调整 e-过程；未校正重复检验 null 上 FWER≈94.5%→Promote 0.0%；**证书控历史假晋升，不是部署后仍赚钱**（需 funded revalidation） |
| 2 | https://arxiv.org/abs/2611.03453 · PDF/txt `penalize_common_trade_*` | **已打开** | Signal collision：市场看见共同交易非模型名；重叠通行费 Lx_i Q_{-i}；ETF 周频 φ=0.75 时 MDD 23.6%→14.1%，Sharpe≈不变，bootstrap MDD 区间含 0；需 holdings/flow 才能归因「AI crowding」 |
| 3 | https://arxiv.org/abs/2611.02616 · PDF/txt `herding_racing_*` | **已打开** | herding share h 混 p 与 κ；Uniswap 小单 h 高(0.65–0.86)但 κ 仅 0.05–0.10→像拆单非模仿羊群；高 κ 多为 CEX 价格竞速，Binance-only 过滤器可去掉约 ≥3/4 跨账户协方差 |
| 4 | https://arxiv.org/abs/2611.01656 · PDF/txt `aida_mev_*` | **已打开** | AIDA：AI 多小时中断日新 executor 发布≈0.67×，**已部署套利/夹心执行量基本不变**；便宜代码降建 bot 门槛，私有流/延迟/资本决定谁吃执行 |
| 5 | https://arxiv.org/abs/2610.06947 · PDF `factorbench_*`（pdftotext） | **已打开** | FactorBench≈5k 因子/9 方法/5 市；训练 IC 常塌；风格中性后残差 IC 常近 0/负；**无一范式在因子→池→组合全链一致占优**；LLM 池更近 Alpha101 |
| 6 | https://youngandcalculated.substack.com/p/a-factor-can-keep-working-and-still | **已打开**（公开段；付费墙后半未强开） | 拥挤可不杀死预测关系但抬升冲击/融资/尾部；α_net=α_gross−成本；AQR 口径：简单拥挤弱预测「alpha 消失」、较强预测「挤兑尾部」 |
| 7 | https://arxiv.org/abs/2512.11913 · 本地 `not_all_crowd_*` | **已打开（修订稿）·勿当强证据** | 10-28 曾标撤稿；现 v 修订为 Return-Decay Residuals…：衰减残差作 crowding 代理**不稳健**（评分/表征/目标敏感；区域 log-loss 恶化；作者自限非 crowding 识别）。**勿引 v1 交易 alpha 数字** |
| 8 | Chigireva SSRN 7588318 Market Capacity… | **延后** | 指标已 DEFERRED；决策本轮未强开 SSRN 全文 |
| 9 | Hua/Feng crowding SSRN / Feng vol-timing | **打开失败** | 先验 403；本轮未复开 |
| 10 | arXiv:2608.08405 Robustness or Crowding | **延后** | 窗内相关但本轮未自读全文，不引用其数字 |
| 11 | arXiv:2611.01799 Aldridge Learning AI Agents… | **延后** | TR 交叉列表；决策本轮未自读 |
| 12 | 指标层 11-04 全文笔记 / Promote obs | **交叉已读** | 不冒充未自读 PDF；机制以本表自开为准 |

**计数：** 已打开=7（含 Substack 公开段 + 2512 修订）；打开失败=1；延后=3；交叉他层=1。**未列「已学完」。**

arXiv 扫面：`q-fin/recent`（Tue 3 Nov 2026）与 `q-fin.TR/recent` 已过目；主命中 #1–#4。

---

## 辩证（四认领块）· 决策侧独特对照

### 1. 真假衰减

- **Promote（2611.03384）× `promo_cpcv_gate_v0` / #90：** 自适应挖规则 = 多重试验；「出生后证据 + 晋升≠同日交易 + funded revalidation」与现网**满窗∧n≥30/50·跨窗·非单路径·首笔 fill 起算日历**同向。指标侧落 #91/权=0；**决策侧**把同一机制映射到**冠名/升 live_candidate 门禁语言**，不是权重门禁。
- **FactorBench（2610.06947）：** 挖矿范式进步 ≠ OOS 经济价值；残差 IC 塌 → 加固「训练/短窗 IC≠准入」。对照 #90 原创性/可证伪/复杂度三门槛。
- **2512 修订：** 旧「拥挤可交易」叙事已被作者自家修订削弱 → 勿复活 v1。
- **夸大勿跟：** 把 e-过程直接接成盘中晋升闸；用 FactorBench CAGR/Sharpe 给 paper peer 冠名；引用 2512 v1 数字。
- **可进周检观察脚注：** 报观察顶/最优 peer 时并列「累计试验族 N + 未去偏 / 未满窗」；晋升与部署分栏（证书≠现役 edge）。

### 2. 拥挤 / 发表后衰减 / 半衰期 × 5s·hold60

- **Collision（2611.03453）× #78：** 多 paper peer 策略名可正交，市场仍可看见共同退出方向；机制可作健康度词表（共同方向加载→尾部）。ETF 周频 MDD 数值与 bootstrap 含 0 → **拒绝**迁入 meme 5s 或拧 SIZE_MULT/φ。
- **Herding κ（2611.02616）：** 跨钱包共向高 ≠ 羊群；缺 κ/CEX 条件化时 **禁止**把地址共向写成 skip 依据。
- **Substack：** 信号仍在 ≠ 可交易；拥挤更像尾部/成本，不是均值 alpha 死亡开关。
- **半衰期：** 本周无新「权益半衰期数值」强增量可迁；继续拒绝把文献半衰期拧 hold60。
- **夸大勿跟：** 毛敞口/peer 数自动熔断；高 bot_trade_share → 接 `high_bot_skip`；用 collision φ 改仓位闸。
- **可进周检观察脚注：** #78 笔记可写「策略正交≠交易正交 / h≠κ」；不熔断。

### 3. LLM / 假因子 / 公开特征商品化 / 假量

- FactorBench：LLM 池更近 Alpha101、风格重叠高 → **公开特征商品化**观察；**不上 LLM 决策主路径**（相对 #90 / Profit Mirage 同向加固）。
- AIDA：AI 掉线停的是**新代码发布**，不是已部署执行 → 勿把「编码 AI 限用/掉线」误读成策略 alpha 死或 JEV 证伪。
- **夸大勿跟：** 上 LLM 挖因主路径；把 AIDA 写成「AI 已杀死链上 alpha」。
- **可进周检观察脚注：** 新候选须过 #90 三门槛 + 全队列负例意识（沿用 10-28 He 审计口径）。

### 4. 执行侵蚀 ≠ 信号失效

- **硬锚 tech_boll_fade：** +117.56%/n30 空仓仍未满窗不冠；净利高度集中在 9 笔 TP；OOS EV 564→220 + 止损占负≈82% → **先走 #71/#72 退出质量栏**，禁止写成「信号/半衰期已死」。
- AIDA 分栏：开发依赖 AI ≠ 运行时信号通道；已部署 bot 不停 → 同构「执行/工程摩擦 ≠ δ(φ) 信号衰减」。
- Substack / Collision 尾部：实现层成本与共同退出可侵蚀净利而不否定毛预测。
- **夸大勿跟：** 删止损/拧 TP；把 TP 命中率掉写成自建臂灭绝；把 AI 掉线当 live 熔断由头。
- **可进周检观察脚注：** 凡写「衰减」须标明执行栏 vs 信号栏；出口质量分栏续挂 #72。

---

## 与指标 1104 的差异点（决策独特）

| 点 | 指标 1104 | 本决策周研 |
|----|-----------|------------|
| Promote | #91 / 权=0 / 不中途接闸 | **`promo_cpcv_gate_v0`·冠名/升 live_candidate**（满窗·n·跨窗·首笔 fill）；晋升≠部署 |
| Collision / 拥挤 | 候选词表；不改权 | **#78 健康度**；多 peer「策略正交≠交易正交」；不熔断、不拧 SIZE_MULT |
| Herding | I-3 度量勿误读 | **明确拒绝 `high_bot_skip`**（决策主路径不接） |
| AIDA | 弱对照；禁 JEV 证伪 | **执行侵蚀≠信号失效**硬分栏；对照 #71/#72 + tech_boll_fade 出口旁证 |
| FactorBench | 过拟合/新颖性观察 | **加固 #90 + 不上 LLM 主路径**；训练 IC≠冠名 |
| 现网数字 | 未绑具体纸面出口结构 | **钉 tech_boll_fade TP 集中度 / OOS EV / 止损占负** |

---

## 文档增量

- 本页：`docs/QUANT_DECAY_FRONTIER_WEEKLY_DECISION_2026-11-04.md`
- 追加 `DECISION_ENGINE_RESEARCH_MAPPING.md`「2026-11-04」节与 `DECISION_ENGINE_WARNING_CASES.md`「W-2026-11-04」；`crawler/` 同文双份。
- **零改**运行时 / .env / Key / \*.db / TP·SL·hold / live/#71 / SIZE_MULT。

## POST_GROUP 判定

相对指标帖与上周决策 10-28：**有**决策侧新批判与新机制映射（Promote→冠名门禁、collision→#78 peer、AIDA→执行≠信号、FactorBench→#90、现网出口锚）→ **POST_GROUP=yes**（由父代理发群；本执行器不发群）。
