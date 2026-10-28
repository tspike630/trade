# OBS_W1028 · REVIEW 脚注草案（docs-only）

**状态：** 意图草稿 / 未推 git / 不改运行时  
**来源全文：** arXiv:2605.23905（Meng & Chen）、arXiv:2607.02695（Noguer i Alonso）— 复盘 2026-10-28 已读全文  
**硬约束：** 零改闸；不上 LLM 主信号；#78 不熔断；衰减≠JEV 证伪；禁迁 18mo/5–7yr 至 Solana meme

## R1 意图（一句话）

在复盘文档层钉死两则脚注：**(i)** 文献中的 AI 采纳 \(\phi\) 量的是**信号生产**而非执行算法，故现网执行摩擦/拒单/hold 与「信号灭绝」分栏；**(ii)** 半衰期由计算浅度×群体预算增速决定、与信号强度 \(g(0)\) 无关，拥挤监视用预报互可压缩语言支撑 #78 **只健康度旁证**。

## 建议补丁落点（若 trade 仓有对应 docs）

优先路径草案（存在则以仓内为准，本轮只留草稿）：

1. `docs/review/README.md` 或 `docs/review/decay_framing.md` 增「分栏」小节  
2. `#78` / crowding 健康度旁证说明处加一句「互可压缩 / 预报相关 ≠ 毛敞口熔断」  
3. `factor_loop` / 候选登记纪律旁注：「不上 LLM 主信号」与 epiplexity §10.7 agentic 同质化推论同向（反例，非上线理由）

## 建议正文（可粘贴）

### REVIEW_φ_SIGNAL_NOT_EXEC

> Meng & Chen (arXiv:2605.23905) 定义 \(\phi\) 为机构用 AI/ML 做**收益预测 / 因子 / 组合权重**的占比，并显式排除 VWAP 等**算法执行**。拥挤通道是相关**预测**，不是相关执行切片。  
> **复盘动作：** 半胜仍亏 → 先查退出质量（#71/#72）；I-3 滑点、日损拒单、MAX_OPEN、短 hold 记为**执行/约束伪衰减**，不得直接记为「信号死亡 / α 半衰期耗尽」。  
> **禁止：** 把文中 18 个月 vs 5–7 年（权益/13F 校准仿真）写成 meme/Solana 纸面半衰期；衰减≠JEV 证伪。

### REVIEW_DEPTH_NOT_STRENGTH_AND_78

> Noguer i Alonso (arXiv:2607.02695) §8：\(t_{1/2}=\ln 2/(\lambda\gamma)\)；半衰期不含 \(g(0)\)（depth-not-strength）；发表≈\(\tau_{\mathrm{mkt}}\) 一次性上跳；拥挤≈预测流互可压缩（非仅毛敞口）；容量≈交易比特泄漏。  
> **复盘动作：** #78 拥挤继续**只健康度旁证、不熔断**；监视语言可写「信号/预报同质化或互相关」，禁止用毛名义敞口自动熔断。  
> **LLM：** §10.7 同质化基础模型 → 互可压缩最大、浅带衰减最快 → 作为**不上 LLM 主信号**的理论反例加固，**不**登记 LLM 主链、**不**进 M-A* 表。

## 明确不做

- 不改 strength / cooldown / min_liq / TP / SL / hold / max_open / SIZE_MULT  
- 不接 `high_bot_skip`  
- 不动 `.env` / Key / `*.db`  
- 不开 live / #71；不注资  
- 不推 git（除非人工确认 docs-only 且路径在既有 trade checkout）

## 验收

- [ ] 脚注出现在复盘文档，且带 arXiv 号与「仿真/理论、禁迁 meme」免责  
- [ ] 群内互批后才考虑合入 trade docs  
- [ ] 运行时参数 diff = 空
