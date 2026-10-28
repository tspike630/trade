# 量化失效前沿调研辩证 · 复盘优化层 · 2026-10-28 HKT

## 元信息

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-28 ≈10:04 HKT |
| 轮换主题 | IC/alpha 半衰期 + 执行侵蚀 vs 信号死亡分栏 + LLM 因子同质化作**反例**（不上 LLM 主信号） |
| 层 | 复盘优化层；只读文献 → 文档级 OBS / REVIEW 脚注；**零改闸** |
| 对照硬口径 | Solana meme 纸面；半胜仍亏优先查退出质量（#71/#72）；#78 拥挤只健康度旁证不熔断；衰减≠JEV 证伪；factor_loop 只收已方向校准 M-2 + 算子化 M-3；M-A* 不进登记表；不上 LLM 主信号；不接 high_bot_skip 除非另有 R1+样本；不改 strength/cooldown/min_liq/TP/SL/hold/max_open；禁 .env/Key/*.db；live/#71 关；禁注资；SIZE_MULT=20/MAX_OPEN=1 口令仍在**本轮不改** |
| 已覆盖勿重开主批判 | 10-26 执行：2608.08405 / 2601.19570 / Null Zoo / 2502.04284；10-28 指标：Bitquery Pump.fun 10-22 / 2610.29500 / 2610.35738 / 2610.34936 / 2610.29663；10-21：2610.21846 / 2609.10246 / 2610.23283；**撤稿 2512.11913 永不引作证据** |
| 反跳读 | 主批判仅用「已读全文」；未写「已学完」 |

## URL/全文清单表

| # | 条目 | URL | 本地 | 状态 | 备注 |
|---|---|---|---|---|---|
| 1 | Meng & Chen, *AI-Driven Alpha Decay…* arXiv:2605.23905 | HTML https://arxiv.org/html/2605.23905 ；PDF https://arxiv.org/pdf/2605.23905.pdf | `alpha-decay-read/pdfs-20261028-review/2605.23905.pdf` + `.txt` | **已读全文** | WebFetch HTML 全文 + pdftotext；重点：\(h(\phi)=\ln2/[\theta+\delta(\phi)]\)、\(\phi\)=信号生产≠执行算法、competence illusion、extinction cascade、实证为校准仿真、18mo vs 5–7yr 为权益/13F 标定 |
| 2 | Noguer i Alonso, *Financial Epiplexity…* arXiv:2607.02695 | HTML https://arxiv.org/html/2607.02695 ；PDF https://arxiv.org/pdf/2607.02695.pdf | `…/2607.02695.pdf` + `.txt` | **已读全文** | WebFetch HTML 全文（含 §8 Decay/Crowding/Capacity）+ pdftotext；重点：\(t_{1/2}=\ln2/(\lambda\gamma)\)、depth-not-strength、publication 一次性抬 \(\tau_{\mathrm{mkt}}\)、拥挤=互可压缩、容量=比特泄漏、agentic/LLM 同质化推论 |
| 3 | Singha, *Forecast-to-Fill…* arXiv:2511.08571 | HTML https://arxiv.org/html/2511.08571 ；PDF https://arxiv.org/pdf/2511.08571.pdf | `…/2511.08571.pdf` + `.txt` | **延后**（截止：下一复盘周研或有「执行拖 vs 信号死」可迁移数字时再开） | PDF 已下且粗扫：黄金期货单策略宣传口径（净 Sharpe≈2.88、容量约 \$1B、成本 0.5–2.0× 线性恶化、T+1/T+2 延迟仍 Sharpe>2.2）。**未**给出可外推的「执行侵蚀 vs 信号死亡」一般分解公式；与本轮主题不完全对齐 → 不进主批判、不写「已读全文」 |
| — | 指标层今日已全文诸文 / 执行 10-26 | 见 `alpha-decay-weekly-20261028/`、`…20261026/` | — | **本轮不重开** | 避免重复主批判 |

诚实声明：优先集 = #1+#2 已读全文；#3 显式延后。**禁止**对仍「未开/延后」项写「已学完」。

## 机制摘录 + 辩证（仅已全文）

### A. arXiv:2605.23905 — Alpha 半衰期与「信号生产 ≠ 执行」

**数字 + 段落（原文）：**

1. **半衰期定理：** \(h(\phi)=\ln 2/[\theta+\delta(\phi)]\)，其中 \(\delta(\phi)=N\phi\rho a/\lambda(\phi)\)。基线标定 \(\phi\approx0.7\)、\(\rho\approx0.6\) 时，中频因子半衰期约 **18 个月**；相对 McLean–Pontiff 风格 pre-AI 标定约 **5–7 年**（文中亦写 \(\theta_k=0.012\) month\(^{-1}\) ⇒ \(h_k(0)=\ln2/\theta_k\approx58\) 月）。
2. **\(\phi\) 范围（文中显式）：** \(\phi\) = 用 AI/ML 做**信号生产与组合构建**的机构占比；**不是**算法执行（VWAP/智能路由等）。文称：两基金同执行算法但不同信号模型 → 不拥挤；同信号模型不同执行 → 仍拥挤。实证代理用 EDGAR 关键词盯「machine learning / predictive model / neural network」于收益预测，而非「algorithmic execution」。
3. **Competence illusion（Remark 3.8）：** \(\phi\ll\phi_k^{*}\) 时侵蚀相对再生 \(g_k\) 仍小，AI 预测看似很准 → 加速采纳；准确度部分来自套利前环境；进入 Phase II（文给 \(\phi\in[0.2,0.6]\)）后精度下滑常被归因「regime change」而非集体采纳的内生后果。
4. **Signal extinction cascade（Prop. 3.7）：** \(\phi>\phi_k^{*}\) 后 \(\sigma_k^{2,*}\propto(\phi_k^{*}/\phi)^{2}\to0\)；一信号灭绝后存活信号阈值 \(\phi_{-k,j}^{*}<\phi_j^{*}\)，按 \(\rho_k a_k/g_k^{1/\kappa}\) 脆弱度排序逐级死亡。
5. **实证自我披露：** §4 写明「calibrated simulations matched to real-world data」；13F 模拟收敛指数约 **+42%**（2013–2024，\(\bar S:0.21\to0.30\)），AI 组内 **+58%** vs 非 AI **+19%**；量化基金横截面月收益离散模拟 **4.1%→2.9%**（−29%）；中位量化 FH7 alpha 模拟 **3.6%→1.0%** 年化。作者承认直接因果识别仍待 IV/DiD 模板。
6. **四阶段生命周期（§5.1）：** Phase I \(\phi<0.2\) \(h\approx5\)–7yr；II \(0.2\)–\(0.6\) \(h\approx2\)–4yr；III \(0.6\)–\(0.9\) \(h<18\)mo；IV \(\phi>0.9\) 净 alpha≈0（预测，非已实证）。

**支持 / 驳外推 meme：**

- **支持（机制层）：** 「执行摩擦 ≠ 信号死亡」可被 \(\phi\) 定义钉死——现网 paper 的 I-3 滑点、日损拒单、MAX_OPEN=1、hold≈60m **都不是** \(\delta(\phi)\) 通道；半胜仍亏优先 #71/#72 退出质量，与文「signal extinction」不同桶。
- **驳外推：** 18mo / 5–7yr / \(\phi\approx0.7\) / 13F +42% **全是权益机构/13F/对冲基金仿真标定**；**禁止**迁到 Solana meme 纸面半衰期或写成「meme alpha 已 18 月死」。文本身承认仿真≠直接因果。
- **LLM 反例锚：** 文把 LLM 发布（GPT-3/ChatGPT/GPT-4/LLaMA）当作 NLP 暴露 DiD 冲击；同质化抬 \(\rho\) → 加速衰减。与硬口径「不上 LLM 主信号」同向，**不是**上主信号的理由。

**仅观察 / 可落地文档：**

- **可落地（docs-only）：** REVIEW 脚注——「\(\phi\)=信号生产；执行侵蚀另栏」；衰减≠JEV 证伪；数字不迁 meme。
- **仅观察：** extinction cascade / competence illusion 作解释性词汇；**不**改 strength/cooldown/hold，**不**接 skip。

---

### B. arXiv:2607.02695 — Epiplexity §8：深度非强度、拥挤、容量

**数字 + 段落（原文）：**

1. **衰减律（Prop. 8.1）：** 若剩余增益尾部 \(g(\tau)=g(\tau_0)e^{-\lambda(\tau-\tau_0)}\)，市场预算 \(\tau_{\mathrm{mkt}}(t)=\tau_0+\gamma t\)，则 \(g(t)=g(0)e^{-\lambda\gamma t}\)，**\(t_{1/2}=\ln2/(\lambda\gamma)\)**；Sharpe 天花板按 \(e^{-\lambda\gamma t/2}\) 衰减。
2. **Depth, not strength（Remark 8.1）：** 半衰期公式**不含**信号统计强度 \(g(0)\)；衰减速度由剖面浅度 \(\lambda\) 与群体预算增速 \(\gamma\) 决定。「平凡但计算深的信号可活过华丽浅信号」。**发表 = \(\tau_{\mathrm{mkt}}\) 对所传递比特的一次性上跳**（certificate 未必含周边基础设施）——解释部分异象发表后只部分衰减（引 McLean–Pontiff）。
3. **拥挤 = 预测流互可压缩（Prop. 8.2）：** 共享目标相关预报比特 \(b_{12}\) 上界活跃收益相关；\(\rho^2\leq\min(b_{12},g_1,g_2)/\sqrt{g_1 g_2}\)（高斯小边）。**观测残差相关下界共享预报比特**——无需对手模型文件。拥挤事件 = 共享比特同步强制揭示；**拥挤风险是互可压缩性属性，不只是毛敞口**。
4. **容量 = 比特泄漏（Prop. 8.3）：** 私有比特 \(\Gamma=G_B^{\mathrm{sw}}\)；参与率 \(\pi_t\) 泄漏率 \(\ell(\pi)\) 凸增；寿命超额 log-growth \(\leq\kappa_W\ln2\cdot\Gamma\)；固定总参与 \(Q\) 时**匀速参与最小化泄漏**（Jensen）。
5. **Agentic 推论（§10.7 / Prop. 10.4）：** 同质化基础模型+共享工具链 ⇒ 互可压缩最大 ⇒ 共享比特几乎立刻揭示、浅带拥堵最重、衰减最快。故意异质性价值随与商品类重叠 \(s_{iC}\) 上升。**「商品比特在均衡中无耐久私有比特」。**
6. **量级玩具（§11，非实证）：** \(p=0.60\) 时 \(1-h_2(p)\approx0.029\) bit/观测；\(\kappa_{SR}=1\)、日频独立下注时年化 Sharpe 天花板约 **3.2**（全 bit 可货币化上界）；\(\kappa_{SR}=0.25\) 时约 **1.6**。另：日频 Sharpe=3 隐含 \(g\gtrsim0.026\) bit/日。

**支持 / 驳外推 meme：**

- **支持：** 「深度非强度」+「发表一次性跳」强化：#78 拥挤只做健康度旁证、**不熔断**——互可压缩是监视器语言，不是闸值。容量=泄漏 与 10-26 执行层「冲击模型≠策略级容量」同族但换比特单位，**不**据此改 SIZE_MULT。
- **驳外推：** \(t_{1/2}=\ln2/(\lambda\gamma)\) 无 Solana/meme 标定；0.029 bit、Sharpe 天花板是**玩具/上界审计**，禁止写成现网可迁 alpha 或证伪 JEV。Agentic 段是**不上 LLM 主信号**的理论反例，禁止读成「该上 LLM」。
- **对 meme：** 发表/开源/同质工具链抬 \(\gamma\) 与 \(\rho\)——与「工厂化 meme 信号快死」叙事同向但**无链上数字**；不得用本文数字填 Pump.fun。

**仅观察 / 可落地文档：**

- **可落地（docs-only）：** REVIEW_* / #78 脚注——拥挤监视优先「预报/信号互相关或互可压缩代理」，非毛敞口熔断；衰减用「浅度×群体预算增速」语言，**禁止**把 \(g(0)\) 强弱当半衰期。
- **仅观察：** bit-leak 容量、Red Queen compute 竞赛；与现网 SIZE_MULT=20 口令正交，本轮不改。

---

### C. 2511.08571 Forecast-to-Fill — 延后理由（非主批判）

粗扫可见：成本 0.5–2.0× 下边缘「大致线性」恶化；T+1/T+2 延迟仍 Sharpe>2.2；摩擦调整容量约至 \$1B。属于**单资产执行稳健性宣传**，缺「信号死 vs 执行拖」可迁移分解与可复现公开账本。**状态=延后**，批判栏不引用其数字。

## 相对 10-21 / 10-26 / 指标 10-28 的增量判断

| 对照 | 是否增量 | 说明 |
|---|---|---|
| vs 10-21（backtest-live / meme factories / efficiency） | **有机制增量、无运行时增量** | 10-21 已有回测-实盘与 meme 工厂；本轮新增可引用的 \(h(\phi)\) 与 \(t_{1/2}=\ln2/(\lambda\gamma)\)，以及 **\(\phi\)≠执行** 钉死分栏 |
| vs 10-26 执行（容量 \(G_L\)、sandwich、Null Zoo） | **互补非重复** | 10-26 讲部署资本→侵蚀可识别性与冲击≠容量；本轮 epiplexity 用「比特泄漏 / 深度」换单位，**不**重开 \(G_L\) 实验设计 |
| vs 指标 10-28（Pump.fun 洗盘、LLM audit、Det-MASO 同质化） | **同向加固、换文献** | Det-MASO 已从日频多智能体同质化；本轮 Meng/Epiplexity 给均衡半衰期与「不上 LLM」的 Red Queen/agentic 反例；**不重开** Pump.fun 26% 或 Det-MASO +0.33 Sharpe |

**新 vs 已知重复：** 半衰期/拥挤/Red Queen **族内已知**；**新可落文档点**仅两条——(1) \(\phi\)=信号生产≠执行；(2) depth-not-strength + 拥挤=互可压缩（#78 健康度语言）。**无**新运行时参数、**无**接线 skip、**无**升主。

## 结论

- **建议文档补丁（docs-only OBS，路径草案）：**
  - `docs/review/OBS_W1028_REVIEW_phi_vs_exec_and_depth.md`（见同目录草稿 `/workspace/out/OBS_W1028_REVIEW_phi_vs_exec_and_depth.md`）
  - 内容要点：REVIEW 脚注两则——执行侵蚀≠信号死亡（钉 \(\phi\)）；#78 用互可压缩/预报相关作健康度、禁熔断；18mo/5–7yr/\(t_{1/2}\) **禁止**迁 meme；LLM 同质化=不上主信号反例。
- **禁止动作：** 改闸/改 strength/cooldown/min_liq/TP/SL/hold/max_open；接 high_bot_skip；动 .env/Key/*.db；开 live/#71；注资；把衰减写成 JEV 证伪。
- **群消息：** 短报有新 OBS 脚注（非改闸）；见 `GROUP_POST.md`。

SILENT_GROUP: false
