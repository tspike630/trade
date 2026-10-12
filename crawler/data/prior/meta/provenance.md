# 数据来源审计
| source | 获取日期 | URL | 许可 | 文件 | 备注 |
|--------|----------|-----|------|------|------|
| solrpds | 2026-10-10 | https://github.com/DeFiLabX/SolRPDs | CC-BY-4.0 | dataset/CSV/2021~2024.csv (116,308 行原始；入库 116,304，跳过空 MINT 4) | 池级；INACTIVITY_STATUS 为 rug 信号；论文 arXiv:2504.07132；浅克隆 depth=1 |
| redpump | 2026-10-10 | https://doi.org/10.5281/zenodo.20633486 | CC-BY-4.0 | RED-PUMP-2026-v1_v1.6_reproducibility.zip (348,529,377 字节) | Zenodo API record 20633486 的 files[].links.self 指向 record 23018519；结局主表 `04_frozen_inputs/red_pump_2026_v1_outcomes.csv.gz`（列 graduated_at,mint,symbol,outcome,minutes_to_outcome,...）；入库有效 GRADUATED/TIMEOUT=832,956（跳过畸形行 215）；GRADUATED=1,652。主队列论文口径（cohort_flow/MANUSCRIPT_METRICS）：749,816 / GRADUATED 1,597 / 毕业率 0.21%；采集 2026-05-08~06-10；论文 arXiv:2607.02823 |
| gmgn | 2026-10-10 | https://www.kaggle.com/datasets/moussaouallaf/gmgn-solana-token-charts | CC-BY-SA-4.0 (Kaggle) | combined_tokens.csv (12,355,379 B / ~11.8MB) + gmgn-solana-token-charts.kagglehub-v3.csv.gz (3.1M) | **完整数据集 n=956**（Kaggle v3 Rows=956；单文件；~120928 candles；~126 bars/token；无更大 pack；验收 unique**=956**）；early-window 监控中位~1.6m → 1h/4h/24h 存活≈0% 为覆盖限制；death_reason=监控终止≠归零 |
