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


## 量化失效前沿周研 2026-10-21（只读，不改闸）

本轮自读 5 篇全文/可核验 HTML。**不算有效性**；不改 TP/SL/hold/cooldown/min_liq/strength/max_open；不上 LLM 决策主路径；不接 high_bot_skip。C4 协议（10-21 12:00 锁、TP10/SL5/h60 唯一判定出口）**不因本节改动**。

### URL 清单（防漏读）

| URL | 状态 | 一句机制摘录 |
|-----|------|--------------|
| [Bailey 等 PBO](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf) | 已打开 | PBO＝样本内最优在样本外跌破中位数的条件概率，用 CSCV 估；样本内选优越用力，样本外越可能更差 |
| [Bailey & López de Prado DSR](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf) | 已打开 | 独立试验数 N 增大时，真 SR=0 下的期望最大 SR 也增大；不报试验数的回测＝选择偏差，DSR 同时校非正态 |
| [Kaminski & Lo 止损](https://www.smallake.kr/wp-content/uploads/2017/02/When_Do_Stop-Loss_Rules_Stop_Losses.pdf) | 已打开 | 随机游走下止损的「止损溢价」恒为负；只有亏损后存在动量（正自相关）时止损才增益，且在较低检查频率上才显现 |
| [A Midsummer Meme's Dream（arXiv:2507.01963）](https://arxiv.org/html/2507.01963) | 已打开 | 707 个 >100% 涨幅 meme 里 82.8% 有人为增长证据（刷量、LPI）；LPI＝小额买单在薄池里拉出大涨，多发生在建池中位 209 天后的老币 |
| [Ding/Luo/Xu SBFC2025 meme 操纵分解](https://sbfc.sydney.edu.au/2025/papers/SBFC2025_4G3_P154.pdf) | 已打开 | 6,000 个 Pumpfun 项目：刷量与评论机器人能拉来更多交易者；跑赢者多为创建者/狙击手、在早期择时出货 |
| [Harvey/Liu/Zhu NBER w20592](https://www.nber.org/papers/w20592) | 延后 | 仅打开落地页，未读全文，不引用其数字 |
| [Flash Boys 2.0（arXiv:1904.05234）](https://arxiv.org/abs/1904.05234) / [Quantifying BEV（arXiv:2101.05511）](https://arxiv.org/abs/2101.05511) | 延后 | 仅摘要页；采集层 `crawler/docs/ALPHA_DECAY_CROSS_LAYER_2026-10-17.md` 已有引用，本轮未自读全文 |

### 辩证对照

1. **真假衰减（PBO/DSR）× 271 格。** 截至 10-21 12:00 累计 271 格（C1–C4、出口 37 格等）本身就是多重试验。任何「最好那格」都带选择偏差，不能当 edge。现有 C4 单流预登记（固定切点、唯一判定出口、不平移窗口）正是 DSR/PBO 要的防线，**口径一致，不加闸**。夸大勿跟：格子高度相关，有效 N 远小于 271，不能机械用 271 去折算 DSR。可进周检观察脚注：凡报「最优格」，旁边写累计试验数 N 和「未去偏」字样；离线可对出口格跑一次 CSCV 估 PBO，只作脚注。
2. **执行侵蚀 ≠ 信号失效（Kaminski–Lo）× 止损占负 74.9%（口径：止损亏损 ÷ 全部负 PnL，10-21 定）。** 如果入场后亏到 −5% 之后的路径接近随机或均值回复，SL5 的止损溢价在结构上为负，这和「半胜仍亏主因退出质量」一致。夸大勿跟：论文是股指/期货日频；meme 有跳空归零尾部（本系统把无有效价判 −100%），止损在这里是截尾而不是提均值，**不能推出去掉止损**。可进周检观察脚注（只读旁簿，不改 SL）：统计触及 −5% 后 60 分钟内「继续跌到 −10%」与「回到入场价」的条件频率，判断亏损段是动量还是回复。采集已出一版（`/workspace/out/stop_post60_coverage_2026-10-21.csv`）：继续跌到 −10% 的 5/10，回到入场价的 1/10，污染期、不计格，方向上反对收紧 SL；池龄×liq 分层见指标 `/workspace/out/poolage_liq_stopshare_2026-10-21.md`，只观察不过滤。
3. **假量/假热度 × 信号可读性（2507.01963、SBFC2025）。** 高涨幅样本里人为增长占多数，成交量/热度类特征部分是被制造出来的；我们追注意力入场，结构上站在被收割的一侧。LPI 指向「老币 + 薄池」组合，与候选 B（liq<150k）和 `DECISION_MIN_LIQ_USD` 相关。夸大勿跟：82.8% 是以 >100% 赢家为条件，不是我们入场样本的基数；SBFC 样本多为 Pumpfun 联合曲线阶段，与我们的 DEX 币池不同。可进周检观察脚注：影簿已有 liq_bucket，可再按池龄（pairCreatedAt）× liq 分层看止损占负，**不作过滤**。
4. **拥挤/半衰期。** 本轮无新自读全文，沿用 `crawler/docs/ALPHA_DECAY_DIALECTIC_2026-10-17.md` 口径，不重复。

对照现有机制：#71/#72 硬旁证门槛不变；#78 拥挤健康度仍只作旁证；#90 AlphaAgent 三门槛对新候选（池龄分层若要升格）照用。警示映射见 `DECISION_ENGINE_WARNING_CASES.md`。


## 量化失效前沿周研 2026-10-28（只读，不改闸）

决策侧自读全文/可核验 HTML；与指标/复盘同日笔记交叉但不冒充未自读。**不算有效性**；不改 TP/SL/hold/cooldown/min_liq/strength/max_open；不上 LLM 决策主路径；不接 `high_bot_skip`。live/#71 关；禁注资。详页：`docs/QUANT_DECAY_FRONTIER_WEEKLY_DECISION_2026-10-28.md`。

### URL 清单（防漏读）

| URL | 状态 | 一句机制摘录 |
|-----|------|--------------|
| [Meng & Chen arXiv:2605.23905](https://arxiv.org/html/2605.23905) / [PDF](https://arxiv.org/pdf/2605.23905.pdf) | 已打开 | \(h(\phi)=\ln2/[\theta+\delta(\phi)]\)；**\(\phi\)=信号生产/组合权重，≠算法执行**；拥挤走相关预测；18mo vs 5–7yr 为权益/13F 仿真标定 |
| [Noguer i Alonso arXiv:2607.02695](https://arxiv.org/html/2607.02695) / [PDF](https://arxiv.org/pdf/2607.02695.pdf) | 已打开 | \(t_{1/2}=\ln2/(\lambda\gamma)\)；**depth-not-strength**（半衰期不含 \(g(0)\)）；拥挤=预报流互可压缩；发表=市场预算一次性上跳；agentic 同质化加速浅带衰减 |
| [He arXiv:2610.29500](https://arxiv.org/html/2610.29500) / [PDF](https://arxiv.org/pdf/2610.29500.pdf) | 已打开 | LLM 挖因审计：度量粒混母体 + 阳性保留目录使「零反转」无信息；全队列 923 可计分中 671+/252− rank-IC，且朴素参照亦为正 → **正 IC≠增量技能/可交易利润** |
| [Bitquery Pump.fun $100K wins](https://bitquery.io/investigations/who-got-rich-on-pumpfun) | 已打开 | 3170 笔 ≥$100K 账面 $752.5M：约 **26%** 真买卖利润、**51%** 手递手、**13%** wash loop；Top25 中 15/25 为 wash；展示 PnL≠可复制 alpha |
| [Wu & Yang Det-MASO arXiv:2610.34936](https://arxiv.org/pdf/2610.34936.pdf) | 已打开 | 多智能体共享目标易因子同质化；log-det 相关惩罚保多样性；报告 IC+1.1%/Sharpe+0.33（A股/美股日频） |
| [Chorok Lee arXiv:2512.11913](https://arxiv.org/abs/2512.11913) | 打开失败 | 已撤稿（v2）；ar5iv HTML「No content」；**永不引作证据** |
| Fuji 2610.35738 / LineageEvo 2610.29663 / Pockets of predictability | 延后 | 指标/采集已全文；本轮决策未自读，不引用其数字 |

### 辩证对照（对照 #71/#72/#78/#90 与现网 paper）

1. **执行侵蚀 ≠ 信号失效（φ 钉死分栏）。** 2605 显式：\(\phi\) 量信号生产，不含 VWAP/路由。现网半胜仍亏、止损占亏、MAX_OPEN=1、hold≈60、日损拒单 → **#71/#72 退出质量栏**，不是「α 半衰期耗尽 / 信号灭绝」。夸大勿跟：18mo/5–7yr **禁止**迁 Solana meme。可进周检观察脚注：凡写「衰减」须标明属执行栏还是信号栏。
2. **真假衰减 / 半衰期冲动（depth-not-strength）。** 2607：半衰期由浅度 \(\lambda\)×群体预算增速 \(\gamma\) 决定，**与信号强度 \(g(0)\) 无关**。对照 meme 5s·hold60：持仓错配叙事 ≠ 证据要拧 hold/TP。夸大勿跟：动态缩 hold / 跟半衰期拧参。可进周检观察脚注：禁止用「强度变弱」单独论证缩 hold。
3. **拥挤（#78）。** 2607 拥挤=预报互可压缩，非仅毛敞口；2605 拥挤通道=相关预测。→ **#78 继续只健康度旁证、不熔断**；监视语言可写信号同质化/互相关。夸大勿跟：毛名义敞口自动熔断、拥挤选因子赚 alpha（2512 撤稿且其自身也写 crowding 不产生均值 alpha）。
4. **LLM/假因子/公开特征商品化。** 2610.29500 强化 #90：全队列保留负例 + 度量粒对齐；阳性目录≠OOS 泛化。Det-MASO 同质化机制支持「候选池勿堆同构动量变体」**文档纪律**，回测增益错数量级、**不上 LLM 主路径**。Bitquery：榜单/聪明钱 PnL 可读性污染；**不接 high_bot_skip**。夸大勿跟：把 26% 写成「全市场洗盘率」或「JEV/自建 A 已证伪」。

**本轮故意不改：** strength / cooldown / min_liq / TP / SL / hold / max_open / SIZE_MULT；live/#71；禁注资；不接 high_bot_skip。

### 2026-10-28 补读（估参幻觉 / MRP / Profit Mirage / MEV）

| URL | 状态 | 一句机制摘录 |
|-----|------|--------------|
| [Gonçalves et al. 2024 OOS Alphas](https://finance-conference.wpcarey.asu.edu/sites/g/files/litvpz3416/files/2025-01/Out-of-Sample%20Alphas%20Post%20Publication.pdf) | 已打开 | 发表后 IS 仍正；pre-pub 权重 OS 均值≈−0.7% → 估参不可收获 |
| [Dadoo & Gao 2026](https://journals.gmu.edu/index.php/jssr/article/view/5634) | 已打开摘要 | 0.61→0.42→0.30；发表 kink 在年 FE 后≈0 |
| [MRP / Alexander–Fabozzi](https://arxiv.org/pdf/2604.08356.pdf) | 已打开 | 最差制度夏普=衰减风险诊断 |
| [Profit Mirage 2510.07920](https://arxiv.org/pdf/2510.07920.pdf) | 已打开 | 知识截止后蒸发 + 预训练泄漏 → 不上 LLM 主路径 |
| [MEV Slip 2309.13648](https://arxiv.org/pdf/2309.13648.pdf) | 已打开 | PEPE 对抗滑点概率约+80% vs USDC；对照 #71/#72 |
| [Deng–Zhou 2411.05803](https://arxiv.org/pdf/2411.05803.pdf) | 已打开 | 流动性跳+扩散洗盘检测；观察不改权 |
| Hua/Brogaard SSRN | 打开失败 | — |

可进周检观察脚注；不算有效性；不改闸。
