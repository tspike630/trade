# 量化失效前沿·周调研辩证 2026-10-28（指标加工层）

范围：alpha decay / 拥挤 / 洗盘·操纵 / meme 量价污染 / 回测-实盘偏差。只登记候选与文档，**不改权重（仍 0）、不改盘中闸、不算有效性、不对 JEV 下「已证伪」结论**。纸面/模拟；live/#71 关；禁注资。I-3 / `high_bot_skip` **未接线**；#72 退出质量 vs 衰减分栏；#91 ALPHA_DECAY 只观察登记。

**窗：** 优先 ~2026-10-14 → 10-28 HKT，相对 2026-10-21 指标周报之后的新材料。  
**纪律：** 主批判结论只使用「已全文」条目。

## 检索清单（诚实）

| 来源 / 查询 | 结果摘要 |
|---|---|
| arXiv `q-fin/pastweek?show=100`（至 10-27） | 扫过；相关：2610.34936 Det-MASO、2610.29663 LineageEvo、2610.35738 Fuji、2610.29500 Auditing LLM Alpha；其余多为期权/宏观/LOB，未进主批判 |
| arXiv `q-fin.TR/recent`、`cs.CE/pastweek` | TR：Fuji 交叉列入；cs.CE：2610.29500 审计、2610.34754 Polymarket smart money（显式延后） |
| WebSearch：alpha decay / factor crowding / wash / meme / backtest-live / Bitquery Oct 2026 | Bitquery Pump.fun wins（10-22）为窗内新主轴；Raydium CLMM / sandwich / Solana 58% 已由 10-21/10-26 覆盖 |
| Bitquery investigations 目录 | Pump.fun $100K wins（核实 10-22）已全文；Robinhood hand-off / 六链 $144B 核实日为 9-23/9-24 → 窗外，仅清单 |
| 已覆盖勿当「新」精读 | 10-21：2610.21846 / 2609.10246 / 2610.23283；10-26 采集层：Bitquery CLMM、Zheng 2607.28424、Aldridge 2610.22522、sandwich；更早 AlphaAgent / McLean–Pontiff / Falck / Cong wash 等 |

## 全文清单（诚实）

| 条目 | 状态 | 备注 |
|---|---|---|
| Bitquery，《Only 26% of pump.fun's $100K wins was real trading profit…》（核实 2026-10-22） | **已全文** | 窗内新；洗盘环 + 手递手污染「聪明钱」排行 |
| arXiv:2610.29500 He，《Auditing LLM Alpha-Factor Mining…》（2026-10-21 提交） | **已全文** | 窗内；度量粒 / 阳性保留偏差 / 全队列记账 |
| arXiv:2610.35738 Anchuri，《Fuji: … Demand Bursts on Robinhood Chain》（2026-10-27） | **已全文** | 窗内；爆发形态与 imitation/cascade；非 wash 主文 |
| arXiv:2610.34936 Wu & Yang，《Det-MASO…》（2026-10-26） | **已全文** | 窗内；因子同质化→加速衰减；A 股/美股日频，量级不可迁移 |
| arXiv:2610.29663 Liu et al.，《LineageEvo…》（2026-10-22） | **已全文**（摘要+实验表+消融） | 与 Det-MASO 同主题；CSI/S&P 日频；主批判从简 |
| Bitquery Robinhood Chain hand-off wash（核实 9-23） | 已打开 | **窗外**；机制可作旁证，不进本周主结论 |
| Bitquery 六链 wash $144B（核实 9-24）/ Solana volume bots 58%（9-23） | 已打开/先验 | **窗外或已覆盖**；不重开主批判 |
| Bitquery Raydium CLMM / sandwich / Zheng / Aldridge | 先验（10-26） | **本周不重开** |
| Ding et al. SSRN-5953738 Pumpfun 操纵分解 | PDF 已下 | **显式延后**：SBFC’25 / 非 10-21 后新窗主轴 |
| arXiv:2610.34754 Polymarket smart money | PDF 已下 | **显式延后**：预测市场≠现网 DEX 旁路 |
| arXiv:2610.30539 CLMM SPIDE | PDF 已下 | **显式延后**：AMM 数学，非失效主轴 |
| Sheikh & Bhat *Pockets of predictability*（Springer 10-02） | 先验（采集层已全文） | **不重开** |
| 2512.11913 crowding | 已撤稿 | 不引用 |

## 机制摘录 + 批判（仅「已全文」）

### A. Pump.fun「六位数赢利」只有约 26% 是真交易利润（Bitquery 10-22）——**采纳为污染机制，量级不迁到评分权**

- **机制：** 2025-10-30 → 2026-08-30 共 3,170 笔 ≥$100K 赢利、账面 $752.5M。按链上检验：约 **26%** 为真实买卖利润；**~51%** 为手递手（他人转币后卖出，多回溯到 creator 侧）；**~13%** 为 wash loop（主导买家 + 与 winner/creator 有直接转账）；另约 10% 无法两边坐实。Top25 里 **15/25** 是 wash loop。Wash loop 偏爱「直开 PumpSwap、池子极浅」的币（直开币占大赢利 12%、却占 wash loop 25%）。手递手在 2026-07 后占比暴增。
- **对照现网：** DexScreener / 排行榜「聪明钱」与 PnL 榜是量价与跟单污染源；现网权重仍 0、I-1 `address_labels` 稀、I-3 / `high_bot_skip` **未接**。该文说明：**展示 PnL ≠ 可复制 alpha**，且洗盘环与 creator 侧转账可把「迁移/毕业」叙事一起污染（与 10-21 文 B 的迁移分层同向）。
- **采纳：** 观察层可登记「榜单钱包 / 大赢利需转账图审核」旁证；**禁止**据此接线跳单或改权。
- **驳回迁移：** 26%/51%/13% 是 Pump.fun 六位数子集规则分类，不是全市场洗盘率，也不是现网入场信号阈值；不得写成「meme 量价已失效」或抬高 bot_share 权重。

### B. LLM 挖因子审计：度量粒错位 + 阳性保留（2610.29500）——**采纳（流程层），与 10-21 文 A / #91 同向**

- **机制：** 历史报告把「保留候选均值 Sharpe」与「每轮最大值均值」比、把「成功评估」与「全部返回结果」混母体；发布的 1,837 表达式目录在写出前丢掉 OOS Sharpe≤0，故「零反转率」无信息。全队列重评 1,047 冻结式：923 可计分中 671 正 / 252 负 mean rank-IC；简单未训练参照（成交量、20 日变动）亦为正相关 → **正 IC 分布≠增量技能/可交易利润**。
- **对照现网：** 与 2610.21846「研究墓地暂定拒绝」及本层「权重默认 0 / #91 只观察 / 不盘中改参」一致。任何因子候选（含 self-built A 旁证、未来 LLM 挖因）必须：**统一度量粒、全队列保留负例、禁止阳性目录冒充 OOS 泛化**。
- **采纳：** 文档级 OBS——候选评估协议对齐「全队列 + 版本化算子修正 + 不把正 IC 当有效性」。
- **驳回：** 文中 A 股 40 标的便利样本、非 DEX 分钟级；数字不迁移；**不得**据此宣称 JEV 或任何现网信号「已证伪」。

### C. Robinhood Chain 需求爆发形态（Fuji 2610.35738）——**采纳方向（时间结构），错数量级提醒**

- **机制：** 中位爆发峰值约 **2.5×** 常态 gas；肩（小时）+ 颈（分钟）双指数；爆发内池数≈平静期（约 1.05×）但半数以上池不同，交易笔数约 **1.40×**，发送地址约 **1.21×**；revert 耗气约翻倍。爆发到达 cascade 略优于自激，但不能排除自激；池内新进入者在约 **24s** 内超额自相关（imitation 与共享信号未因果分离）。
- **对照现网：** DexScreener 聚合 volume/活跃度在「爆发窗」会被短时 imitation/cascade 抬高；与洗盘膨胀（CLMM / Pump.fun）正交但同向污染「热度」。现网不读 Robinhood Chain 主喂，机制作旁证。
- **采纳：** OBS——爆发窗 vs 稳态窗分层记账（只计数）；**不**改 hot/评分权。
- **驳回：** 2.5× / 24s / 1.40× 来自单条 L2 前三月，不可当 Solana 主网或现网阈值阈值。

### D. 因子同质化与多样性正则（Det-MASO 2610.34936；旁证 LineageEvo 2610.29663）——**采纳机制表述，驳回回测增益迁移**

- **机制：** 单策略/共享目标多智能体易坍缩到相似因子；静态策略在 regime 切换下加速 alpha decay。Det-MASO 在智能体间相关矩阵上加 log-det 惩罚；报告 IC +1.1%、年化 +6.4%、Sharpe +0.33（相对其基线）。LineageEvo 用谱系分层分配搜索预算，消融显示去掉 LSPS 后谱系熵 0.405→完整 2.154，IC 同步掉。
- **对照现网：** 我们**不在盘中挖因升权**；权重仍 0。文中「多样性防衰减」支持「候选池勿只堆同构动量变体」的**文档纪律**，不支持把 LLM/多因子挖掘结果直接灌入评分。
- **采纳：** OBS——候选因子两两相关 / 谱系集中度只观察（若未来有候选池）。
- **驳回：** Qlib TopK、CSI/S&P 日频回测增益**错数量级**；与 DEX 新池、5s paper、退出质量（#72）问题正交，禁止把「+0.33 Sharpe」写成现网可迁 alpha。

## 本周结论

- **权重仍 0**；不接线 `high_bot_skip`；不盘中改参；不算有效性；不写「JEV 已证伪」；纸面 only。
- **相对 10-21 / 10-26：有新对照项**（A–D），但全部落在「观察/文档」层，无一触发升权或闸。
- **建议登记（仅 OBS / docs/candidates，无代码改权）：**
  - **OBS-W1028-1**（←A）：榜单/大赢利钱包需转账图 + wash-loop 双条件旁证；与 I-1 稀缺对照；**不跳单**。
  - **OBS-W1028-2**（←B）：#91 / 任何因子候选评估强制全队列保留负例 + 度量粒对齐；正 IC ≠ 有效性。
  - **OBS-W1028-3**（←C）：volume/热度爆发窗 vs 稳态窗分层计数；不改评分。
- **不登记为可接线闸：** Det-MASO/LineageEvo 多样性惩罚、Fuji 24s 自相关阈值、Pump.fun 26% 比例。

## 下一步（待群内互批，指标层自驱）

1. 若写 candidates 短页：只写 OBS-W1028-1/2/3 定义与「禁止动作」清单。  
2. 继续旁路观察洗盘/机器人供给（I-1/I-3 稀）与 #72 退出质量分栏，**勿混栏**。  
3. 下周检索起点：Bitquery 10-22 之后新调查；arXiv q-fin.TR / cs.CE pastweek。
