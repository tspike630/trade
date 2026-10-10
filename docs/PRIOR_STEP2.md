# PRIOR_STEP2 状态（GMGN 完整数据集已落地并重跑）

> 时间：2026-10-10（Asia/Hong_Kong）
> 定位：旁路研究；只碰 `data/prior/`、`models/prior_tables.sql`、`scripts/prior_*.py`

## 已完成
- kagglehub 下载成功：`moussaouallaf/gmgn-solana-token-charts` →  
  `/home/box/.cache/kagglehub/datasets/moussaouallaf/gmgn-solana-token-charts/versions/3/combined_tokens.csv`
- 文件已拷入 `data/prior/raw/gmgn/unpacked/combined_tokens.csv`；源路径与 gzip 副本在 `data/prior/raw/gmgn/`；旧 aside 另存 `sample_aside/`
- `python3 scripts/prior_gmgn_ingest.py` → token_ohlcv 行=956 / 唯一 token=956
- `python3 scripts/prior_ohlcv.py` → `data/prior/exports/survival_20261010_v2.md` + `baseline_params.json`（RED-PUMP/CoinGecko/SolRPDs 保留）
- **用户校正**：本包即为完整数据集（Rows=956，单文件 combined_tokens.csv，~120928 OHLCV candles，~126 bars/token，~11.8MB）；**无更大 pack**。已去掉 SAMPLE / 非完整 pack 误标。

## 验收对照
| 项 | 目标 | 结果 |
|---|---|---|
| unique tokens | **=956** | **956** PASS（完整数据集；非 >1000） |
| 存活 1h/4h/24h | early-window 覆盖限制下可≈0% | 0%/0%/0% — 监控跨度 P50=1.6m（覆盖限制，非下载失败，非 RED-PUMP 假象） |
| peak P50 | ≠≈1440 | **1.0m** PASS（GMGN 价值在 early peak） |
| death_reason | 监控终止≠归零 | PASS |
| 报告标注 | 完整数据集 n=956 | PASS |

## 不做
- 不碰业务四库 / INDICATORS_WEIGHT_* / trading
