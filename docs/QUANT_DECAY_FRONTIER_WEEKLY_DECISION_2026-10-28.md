# 量化失效前沿周研 · 决策引擎层 · 2026-10-28 HKT

**范围：** 真假衰减 / 拥挤·半衰期 / LLM·假因子·假量 / 执行侵蚀≠信号失效。  
**硬约束：** 不算有效性；不改 TP/SL/hold/cooldown/min_liq/strength/max_open；不上 LLM 主路径；不接 `high_bot_skip`；live/#71 关；禁注资。  
**对照：** #71/#72 硬旁证、#78 拥挤健康度、#90 AlphaAgent 三门槛；`DECISION_ENGINE_RESEARCH_MAPPING.md` / `WARNING_CASES.md`；现网 paper 半胜仍亏主因退出质量。  
**交叉：** 指标 `docs/QUANT_DECAY_FRONTIER_WEEKLY_2026-10-28.md`、复盘 `…_REVIEW.md` 已有同日笔记；下表仅列**决策自打开**条目。

## URL 清单（防漏读）

| # | URL | 状态 | 一句机制摘录 |
|---|-----|------|--------------|
| 1 | https://arxiv.org/html/2605.23905 + PDF | **已打开** | \(h(\phi)=\ln2/[\theta+\delta(\phi)]\)；\(\phi\)=信号生产≠算法执行；仿真 18mo vs 5–7yr |
| 2 | https://arxiv.org/html/2607.02695 + PDF | **已打开** | \(t_{1/2}=\ln2/(\lambda\gamma)\)；depth-not-strength；拥挤=互可压缩；发表一次性抬市场预算 |
| 3 | https://arxiv.org/html/2610.29500 + PDF | **已打开** | 度量粒错位 + 阳性保留；全队列正 IC 分布≠增量技能/利润 |
| 4 | https://bitquery.io/investigations/who-got-rich-on-pumpfun | **已打开** | ≥$100K 赢利约 26% 真利润 / 51% 手递手 / 13% wash；Top25 多为 wash |
| 5 | https://arxiv.org/pdf/2610.34936.pdf | **已打开** | Det-MASO：同质化加速衰减；log-det 多样性；日频回测增益不可迁 |
| 6 | https://arxiv.org/abs/2512.11913 | **打开失败** | 撤稿；不引用 |
| 7 | Fuji / LineageEvo / Pockets… | **延后** | 他层已读；决策本轮未自读全文 |

未列「已学完」。本地核验副本：`/workspace/frontier1028/`（2605/2607/29500/34936 pdf+txt）。

## 辩证（四认领块）

### 1. 真假衰减

- **过拟合伪 Alpha：** 2610.29500 阳性目录与度量粒混母体 = 典型伪证据链；对照 #90 + 周检门槛，不把短窗/正 IC 当准入。
- **真衰减 vs 风格噪声：** 2605/2607 给均衡半衰期语言，但标定在权益/13F；meme 纸面亏损结构仍是退出质量主导 → **先分栏再谈衰减**。
- **夸大勿跟：** 用 18mo 半衰期或「AI 已杀死 alpha」否决自建 A / 改 strength。  
- **可进周检观察脚注：** 报滚动胜率下降时，并列「止损占亏是否仍主导」。

### 2. 拥挤 / 发表后衰减 / 半衰期 × 5s·hold60

- 文献半衰期月～年级；我方 hold60 是执行设计，**不是**证据要跟半衰期拧参。
- #78 用互可压缩/信号互相关作健康度语言即可；**不熔断**。
- **夸大勿跟：** 动态缩短 max_hold；把撤稿 2512 当 crowding 交易 alpha 证据。

### 3. LLM / 假因子 / 公开特征商品化 / 假量

- #90 三门槛继续；2610.29500 补「全队列+负例保留」。
- Bitquery：聪明钱榜污染 → 信号可读性降级；**观察**，不接线跳单。
- Det-MASO：同质化风险叙事采纳；+0.33 Sharpe **驳回迁移**。
- **夸大勿跟：** 上 LLM 挖因主路径；把 26% 当全市场洗盘率。

### 4. 执行侵蚀 ≠ 信号失效

- 2605 \(\phi\) 定义钉死：滑点/MEV/止损/日损/MAX_OPEN **不是** \(\delta(\phi)\) 通道。
- 现网：半胜仍亏 → #71/#72；live 关 → 不做「发表后衰减」实盘外推。
- **夸大勿跟：** 删止损、或把执行摩擦写成 JEV/自建 A「已死」。

## 文档增量

- 已追加 `DECISION_ENGINE_RESEARCH_MAPPING.md`「2026-10-28」节与 `DECISION_ENGINE_WARNING_CASES.md`「W-2026-10-28」。
- **零改运行时 / .env / Key / \*.db。**

## 群结论意向

有可核对新机制（φ≠exec、depth-not-strength、LLM 审计、榜单 PnL 污染）→ 短中文贴「我要赚米」；不改闸。

## 补读增量（同日执行器核验，决策自打开）

相对上文首轮帖的**新增**已打开条目：

| URL | 状态 | 一句机制 |
|-----|------|----------|
| [Gonçalves/Loudis/Ogden 2024 PDF](https://finance-conference.wpcarey.asu.edu/sites/g/files/litvpz3416/files/2025-01/Out-of-Sample%20Alphas%20Post%20Publication.pdf) | 已打开 | 发表后 IS alpha 仍正；用**发表前最优权重**算 OS 均值约 **−0.7%≈0** → 估参不可收获 ≠ 单纯拥挤死亡 |
| [Dadoo & Gao 2026 JSSR](https://journals.gmu.edu/index.php/jssr/article/view/5634) | 已打开（摘要 HTML；全文 PDF 404） | 0.61→0.42→0.30%/月（−51%）；堆叠 DiD 发表拐点在 cohort×年 FE 后 −0.05（t=−0.6）→ 多为 OOS/风格，非发表日猝死 |
| [Alexander & Fabozzi MRP](https://arxiv.org/pdf/2604.08356.pdf)（本地 `strategy_decay.pdf`） | 已打开 | MRP＝跨制度最差风险调整收益；高全样本 Sharpe ≠ 耐用 |
| [Profit Mirage arXiv:2510.07920](https://arxiv.org/pdf/2510.07920.pdf) | 已打开 | LLM 过知识截止后收益蒸发（文称约 50–62% 量级衰减）+ 预训练泄漏/记忆；FinLeak-Bench |
| [Don’t Let MEV Slip arXiv:2309.13648](https://arxiv.org/pdf/2309.13648.pdf) | 已打开 | PEPE 对抗滑点概率约 **+80%** vs USDC；大单冲击+滑点主导（ETH Uniswap，量级勿硬迁 Solana） |
| [Deng & Zhou wash liq jump/diffusion](https://arxiv.org/pdf/2411.05803.pdf) | 已打开 | 流动性跳+扩散联合抬升刻画洗盘；量价可读性污染观察 |
| Hua/Brogaard SSRN | 打开失败（403） | — |

**辩证补丁：** 估参幻觉（Gonçalves）与「发表猝死」弱化（Dadoo）→ 短窗仍有 IC/半胜 ≠ live edge，也勿把每次 OOS 下滑写成拥挤死亡。MRP 只作周检观察诊断。Profit Mirage 泄漏通道加固不上 LLM 主路径（相对 #90 同质化另加一条）。MEV/PEPE → #71/#72 退出质量栏，执行≠信号失效。
