# Decision Engine — 警示事例（执行单 → 已有控制）

**Time (HK / UTC+8):** 2026-10-17 00:32 HKT（假因子/拥挤加速衰减）  
**用途：** 把执行单 / 看板平仓旁证里的三条警示，映射到**已经有的**控制。不新造阈值，不改代码默认。

本文件**不算有效性**。不改 `JEV_MIN_STRENGTH`（仍是 **0.3**）、因子权重、`DECISION_COOLDOWN_SEC` 代码默认（仍是 **0**）、`DECISION_MIN_LIQ_USD` 默认（仍是 **50000**）。临时 `--min-jev-strength 0.15` 且 `purpose=live_candidate` 的行保持未标记，不得标成 accepted。2026-10-19 周检的 cooldown / `min_liq` 分桶 / 临时 0.15 三行仍是观察登记，见 `DECISION_ENGINE_WEEKLY_CHECK.md`。

看板旁证是 PR #45 总览上的平仓块：胜率、最差单笔、剔除最差、单笔抹盈，以及 `exit_reason` × 流动性桶。落地时记下的纸面侧例是胜率约 **51%**、最差单笔约 **−84**、剔除最差后约 **+36**、单笔抹盈 **是**。那是观察，不是周检样本。

### 2026-10-14 纸面回撤（观察）

快照约 **23:47 HKT**：权益 ≈ **$9683**（相对 $10000 约 **−3.2%**），空仓，已平 **43** 笔；已实现 ≈ **−$317**；胜率 ≈ **44%**；首次命中 **40%**（n=10）。**不算有效性。** 旁证，不是周检样本。

归因来自执行 / 复盘：`stop_loss`×3 ≈ **−305**（最差 ≈ **−$190**）占主导；`max_hold`×40 ≈ **−12** 是次要。执行 `daily_loss` 拒绝过一次（已实现 ≈ −317，低于 −200）——保护，不是失效。

反思：`live_candidate` 仍在流入。`decision_log` 里 `purpose=live_candidate` 到约 **23:54 HKT** 计 **83** 条，策略 `jev_strength_v1`，显式 min **0.15**。**信号量 ≠ PnL。** 半胜仍亏 / 薄池打穿已经映射到 `DECISION_MIN_LIQ_USD`、cooldown、R1–R3。

调整：**盘中不改** `DECISION_MIN_LIQ_USD`、cooldown、`JEV_MIN_STRENGTH`、临时 **0.15**。2026-10-19 的 P-3 旁证继续挂着，见 `DECISION_ENGINE_WEEKLY_CHECK.md`。

## 三条警示 → 已有控制

| 警示 | 看到什么 | 已有控制 | 不要做 |
|------|----------|----------|--------|
| **半胜仍亏** | 胜率约一半仍可净亏（仓位 / 边际 / 成本） | 净边际不在决策层。执行层已有开关：`PM_MAX_OPEN_PER_STRATEGY`、`PM_TOKEN_DEDUP`、`EXECUTION_DAILY_LOSS_USD`（默认关）。见 `execution/README.md` | 不要因为短窗胜率偏低，在盘中改强度或权重 |
| **单笔抹盈 / 薄池打穿** | 一笔低流动性 `stop_loss`（约 −84，桶 `liq_<50k`）可以抹掉此前盈利 | live `RiskManager` 买入地板 `DECISION_MIN_LIQ_USD=50000`（PR #41）。对照 `DECISION_ENGINE_FILTER_PARITY.md` | 不要再发明第三条盘中过滤。parity 里的 `min_price` / `min_coverage` 继续记为 gap |
| **$0.95 黑天鹅（类比）** | 很小的残余失败面可以主导整段 PnL | `no_liquidity` / `liq_below_min` fail-closed；临时 paper 观察 `DECISION_COOLDOWN_SEC=3600`，对齐 `max_hold`。配方见 `DECISION_ENGINE_DRYRUN_24H.md` | 统计性改参之前要有回测证据、parity 变更记录、以及周检验证（R1 / R2 / R3） |

### 半胜仍亏

短窗胜率约 51% 仍可以净亏。亏在仓位、边际和成本，不在「把 `JEV_MIN_STRENGTH` 或权重临时拧一下」。决策层默认强度保持 **0.3**。0.15 只出现在显式命令上，而且那些行不算有效性。

**P-1**（净边际）等 PM 执行单和 R2 归因，不在本文件落地，也不在决策引擎里加净边际计算。仓位上限、同标的去重、日亏限额的归属是执行 / PM：`execution/README.md` 的 `PM_MAX_OPEN_PER_STRATEGY`、`PM_TOKEN_DEDUP`、`EXECUTION_DAILY_LOSS_USD`。三闸默认关。

### 单笔抹盈 / 薄池打穿

看板把「剔除最差后仍为正、合计为负」标成单笔抹盈。侧例是薄池止损：`stop_loss` × `liq_<50k`，最差约 −84，其余约 +36。live 买入地板就是为了挡住这类入场，数值与回测 `FilterConfig.min_liq` 同为 **50000**（PR #41）。

闸门在 `decision_engine/core/risk_manager.py`：流动性缺失或非有限数 → 日志 `reason=no_liquidity`；低于下限 → `reason=liq_below_min`。buy 在写入 `decision_log` 之前丢弃。卖出不进这条闸门。数据源是扫描行上的 `pairs.raw_json` 当前快照，与回测 / 周检优先 `pair_history` 不同；parity 表已记下，这里不改数据源。

**P-3**（`min_liq` 周检分桶）归决策层，看板分桶是旁证。2026-10-19 只登记三桶：缺失（`no_liquidity`）、低于 50000（`liq_below_min`）、过闸后写入的 buy。`min_liq_usd=` 应为 `50000.0`。不要为了分桶改 50000，也不要在盘中再加一条过滤。

### $0.95 黑天鹅（类比）

这不是新策略，也不是要去交易 0.95 的合约。类比是：标价 0.95 时失败面看起来只剩 0.05，一次打穿仍能盖过很多次小赢。薄池和缺失流动性是同一类残余风险——次数可以很少，对 PnL 的占比可以很大。

所以两条已经失败关闭的路径保持关闭：`no_liquidity`、`liq_below_min`。临时 paper 观察的冷却保持进程环境变量 `DECISION_COOLDOWN_SEC=3600`，对齐回测与 paper 的 `max_hold_min=60`、`time_exit` 默认 `max_hold_seconds=3600`。代码默认仍是 **0**，不要把默认改成 3600。漏掉这个环境变量时冷却是关的。冷却状态在进程内存里，重启清零。

`purpose` 不要混：

| 进程 | purpose | 强度 |
|------|---------|------|
| 周一三五 10:19 采样 | `sample` | 显式 `--min-jev-strength 0.15` |
| 临时 paper 观察 | `live_candidate`（必须和 0.15 写在一起） | 显式 `--min-jev-strength 0.15` |
| 2026-10-10 那段常驻 | `live_candidate`（不传 `--purpose`） | 实时默认 **0.3** |

低于实时默认 0.3 且漏传 `--purpose` 时，purpose 写成 `sample`。执行网关拒 `sample`。`sample` 不是 paper 候选。临时 0.15 的 `live_candidate` 行保持未标记。

任何统计性改参之前：

1. **回测证据** — 正式对比走 `DECISION_ENGINE_STEP2.md`，不要用短窗胜率代替。
2. **变更记录** — 先改 `DECISION_ENGINE_FILTER_PARITY.md` 里对应的一行，写明回测、周检、live 是否一起动。
3. **周检验证** — R1 周报 `python -m review.run`（`REVIEW_R1_SPEC.md`，`docs/` 与 `crawler/` 各一份）。R2 是完整归因，P-1 等它，决策层不在这里做。R3 寻优脚手架不改参（`REVIEW_R3_SCAFFOLD.md`，同两处）：历史 ≥7 天且周检跨门槛之后才谈正式寻优，见 `DECISION_ENGINE_WEEKLY_CHECK.md`。


## 假因子 / 拥挤加速衰减（旁证）

来源旁证：AlphaAgent（arXiv:2502.16789）指出 LLM/同质因子易挤进同一失效面，**拥挤会加速 alpha 衰减**；「回测好看」的假因子尤其危险。映射到本层**已有**观察，**不改运行参**。

| 警示 | 看到什么 | 已有控制 / 登记 | 不要做 |
|------|----------|-----------------|--------|
| **假因子（回测好看≠有效）** | 短窗曲线好看、无原创性 / 无可证伪假设 / 过复杂 | 准入纪律见 `DECISION_ENGINE_RESEARCH_MAPPING.md`「AlphaAgent / 因子准入纪律」。有效性仍只走周检 + Step2 | 不要因回测好看把候选标成 accepted / activated；不要盘中改强度或权重 |
| **拥挤加速衰减** | 同类策略信号扎堆（日均 buy↑、unique/buy↓、峰值小时密度↑） | **#78** `scripts/signal_crowding.py`：**只旁证**，**不否决 Step2**。但**强制写入当周周检健康度笔记**（见 `DECISION_ENGINE_WEEKLY_CHECK.md` 分栏） | 不要把拥挤度升格成硬否决；不要据此改 cooldown / min_liq / strength |
| **INTEL 权重 0 裸跑风险** | `top10_share` / `dev_balance_delta` / `bot_trade_share` 等权重仍为 **0** 时若当盘中参用 | 权重 0 前**不接** INTEL 因子进盘中参（`docs/RECOVERY_DECISION.md` §7）。只读挂卡 ≠ 生效 | 不要在权重与周检流程打开前把 INTEL 候选当过滤或打分 |

**不算有效性。** 不改 `JEV_MIN_STRENGTH`（0.3）、`DECISION_MIN_LIQ_USD`（50000）、`DECISION_COOLDOWN_SEC` 代码默认（0）、TP/SL。候选≠生效。

## 归属

| 项 | 谁做 | 本文件 |
|----|------|--------|
| **P-1** 净边际（半胜仍亏：仓位 / 成本） | 等 PM 执行单 + R2 | 只指向执行层 max_open / dedup / daily_loss。不实现 |
| **P-2 / P-4 / P-5** | 指标层 / 复盘层 | 不另写 IC/IR 或 JEV 错价。有效性仍只走周检与 `review.run` |
| **P-3** `min_liq` 周检分桶 | 决策层 + 看板旁证 | 10-19 登记表已在周检文档。看板 `liq_<50k` × `stop_loss` 只作旁证 |

## 不算有效性

- 临时 `--min-jev-strength 0.15 --purpose live_candidate` 的 buy **保持未标记，不得标成 accepted**。
- `scripts/jev_validity.py` 按 `strategy_id=jev_strength_v1` 的全部 buy 读取，**不按 purpose 过滤**，这些行会进入周检输入。报告里单独脚注，不要拿它们去过「连续 2 周 ≥55%」门槛。
- 2026-10-19 的 cooldown 是否生效、`min_liq` 三桶、临时 0.15 观察行，三行都是观察登记。不改阈值、不改默认强度、不把临时观察标成 accepted。
- **M-6**：高信心≠高收益，信号强度≠胜率（PolyBench / Prediction Arena）。只作解读，不加新过滤。临时 `purpose=live_candidate` 且 `--min-jev-strength 0.15` 的行保持未标记，**不算有效性**。第二批文献（Prophet Arena / LiveTradeBench / When Agents Trade / FinPos / PredictionMarketBench）同样只作解读：**校准好 ≠ 赚钱**，**回测好看 ≠ 有效**；已被本条与 M-1 功效脚注覆盖（`DECISION_ENGINE_WEEKLY_CHECK.md`「统计功效标注」，[#54](https://github.com/tspike630/trade/pull/54)）。**不加新过滤。**
- 文献映射（第一批不上 LLM 决策主路径、不上 agent 栈；第二批只读；**AlphaAgent 准入三门槛**）见 `DECISION_ENGINE_RESEARCH_MAPPING.md`（`crawler/` 同文）。
- **假因子 / 拥挤加速衰减**（#78 健康度旁证强制入周检笔记；INTEL 权重0裸跑风险）：只旁证，不改运行参，不否决 Step2。


## W-2026-10-21 前沿周研警示（只读）

- **最优格≠edge**：多格搜索后挑最好的一格必带选择偏差（Bailey & López de Prado DSR/PBO）；报最优格须附累计试验数，C4 单流预登记为准。
- **止损不必然止损**：随机游走下止损溢价恒负（Kaminski & Lo）；止损占负高先查亏损段是动量还是回复，不据此删止损（meme 有归零尾部）。
- **假热度**：高涨幅 meme 多数带刷量/LPI 证据（arXiv:2507.01963）；量与热度特征可读性打折，不据此直接加过滤。


## W-2026-10-28 前沿周研警示（只读）

- **φ≠执行误判：** AI 采纳 \(\phi\)（arXiv:2605.23905）量的是信号生产不是执行；半胜仍亏/止损占亏先走 #71/#72 退出质量，禁止写成「信号灭绝」。18mo 半衰期禁止迁 meme。
- **半衰期冲动缩仓：** depth-not-strength（arXiv:2607.02695）→ 半衰期不含 \(g(0)\)；禁止因「强度弱/半衰期短」拧 hold/TP/SL。
- **拥挤熔断冲动：** 拥挤=预报互可压缩（同文）；#78 只健康度旁证，禁止毛敞口熔断。
- **阳性 LLM 因子目录：** 审计 arXiv:2610.29500 — 度量粒混母体 + 丢掉负 OOS →「零反转」无信息；正 IC≠可交易；继续 #90 三门槛 + 不上 LLM 主路径。
- **榜单 PnL 污染：** Bitquery Pump.fun（2026-10-22）六位数赢利仅约 26% 真交易利润；展示聪明钱≠可复制；不接 `high_bot_skip`、不改评分权。

### W-2026-10-28 补读

- **估参幻觉：** 发表后「仍正」的 IS alpha 用发表前权重 OS≈0（Gonçalves）— 勿把 paper 期半胜当 live edge。
- **发表猝死叙事：** 多数是渐进 OOS（Dadoo）— 勿把每次下滑写成「一发表就死/拥挤死亡」。
- **高夏普≠耐用：** MRP 最差制度诊断只观察。
- **Profit Mirage 泄漏：** 过知识截止收益蒸发 — 加固不上 LLM 主路径。
- **MEV/对抗滑点：** meme 对成熟对约 +80% 对抗滑点概率（ETH）— 执行侵蚀栏，勿写成信号失效；量级勿硬迁 Solana。

## W-2026-11-04 · 决策失效周研警示（Promote / Collision / Herding / FactorBench / 出口）

1. **勿把 Promote e-过程接成盘中晋升闸或同日部署开关。** 文献自限：证书控历史假晋升，不保证部署后赚钱。现网仍只认 `promo_cpcv_gate_v0` 满窗·n·跨窗·首笔 fill；满窗前不冠。
2. **勿用 collision φ / ETF 周频 MDD 拧 SIZE_MULT、max_open 或 peer 熔断。** bootstrap 区间可含 0；meme 5s·hold60 拒绝迁入周频半衰期数值。#78 仅健康度旁证。
3. **勿把跨钱包共向 / 高 bot_trade_share 接 `high_bot_skip`。** Herding κ 显示 h≠κ；缺条件化时共向更像拆单或 CEX 竞速。
4. **勿把 FactorBench 训练 IC / LLM 池 CAGR 当冠名或上 LLM 决策主路径。** 残差 IC 常塌；无一范式全链占优。
5. **勿把 tech_boll_fade 的 OOS EV↓ / TP 命中集中 / 止损占负写成「信号死」。** 先走 #71/#72 退出质量栏；日历未满 S7 不冠。AI 掉线停新代码≠已部署信号通道死（AIDA）。
6. **勿复活 2512.11913 v1 拥挤交易数字。** 修订稿已削弱残差 crowding 代理。

仍有效：live/#71 关；真金不注资；SIZE_MULT=20；ret≤−90%→WIPE+反做；一策一盘。
