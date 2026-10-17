# Decision Engine — 警示事例（执行单 → 已有控制）

**Time (HK / UTC+8):** 2026-10-17 ~10:15 HKT（假因子/拥挤 + Alpha衰减误判）  
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

## Alpha 衰减误判（2026-10-17 旁证）

来源：`docs/ALPHA_DECAY_DIALECTIC_2026-10-17.md`（#92）+ 跨层补丁 `docs/ALPHA_DECAY_CROSS_LAYER_2026-10-17.md`（Early Bird / Cong–Victor / Lou–Polk / 执行 CFM·FlashBoys）。**不算有效性。不改运行参。**

| 警示 | 看到什么 | 已有控制 / 登记 | 不要做 |
|------|----------|-----------------|--------|
| **执行侵蚀误判为 Alpha 死亡** | 止损占亏主导（纸面旁证可至 ~71%）、半胜仍亏、图谱「滑点吞噬 Alpha」 | #71 硬旁证；上文「半胜仍亏」；滑点/止损 = **退出质量**，不是 JEV 信号失效 | 不要否决 JEV；不要盘中改 strength / hold / TP / SL；不要把执行亏写成「真衰减」去下线策略 |
| **半衰期冲动缩仓** | 「信号只能持有更短 → 动态缩 hold / 拧 TP」 | hold60、cooldown 观察；文献半衰期多为**月级**，与我方 5s paper loop 不是同一调参证据 | 不要动态缩短 `max_hold`；不要拧 TP/SL |
| **拥挤熔断冲动** | 日均 buy↑、unique/buy↓、图谱「高拥挤降杠杆」、**衰减残差当 crowding** | **#78** 只旁证、强制入周检健康度笔记；**不否决 Step2**；2512.11913：残差≠拥挤识别 | 不要升格硬否决；不要用衰减残差升格 #78；不要据此改 cooldown / min_liq / strength |
| **公开特征商品化误读为新 Alpha** | 公共量价/标签可见度↑、再叠浅层 Dex 量价「新因子」 | Early Bird 宣传窗后收益消散；INTEL/浅层量价**权重 0**；交叉补丁 §3(c) | 不要加权重或接线；不要把商品化供给当边际 alpha |
| **假量/洗盘污染误读为信号** | 短窗成交量飙升、图谱「对倒假放量」、LPI 叙事 | Cong/Victor + **Midsummer 2507.01963**（高收益 meme wash/LPI 文内 82.8%）；AMM/LPI **不可**照搬进闸；`high_bot_skip` **未接** | 不要把量飙升当买入信号；不要接 skip；**不要把 70%/30%/82.8% 拷进过滤** |

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
- **Alpha 衰减误判**（执行侵蚀≠信号失效；半衰期勿缩 hold；拥挤勿熔断；**公开特征商品化**勿叠量价；**假量污染**勿当信号、不接 `high_bot_skip`）：见上文；#92 `docs/ALPHA_DECAY_DIALECTIC_2026-10-17.md` + 跨层 `docs/ALPHA_DECAY_CROSS_LAYER_2026-10-17.md`。**不改** strength / min_liq / cooldown / TP/SL / hold / max_open。
