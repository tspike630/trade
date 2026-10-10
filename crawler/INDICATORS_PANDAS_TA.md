# INDICATORS · pandas-ta 旁路（ADD-ON，不替换）

> 日期：2026-10-10（HK）  
> 仓库：`/workspace/dexscreener-crawler`  
> 共识：在既有 `change_*` / `volatility_24h` / `volume_strength` **之上追加** RSI/MACD/BOLL，**不替换**旧口径；composite 权重仍为 0。

## 1. 做了什么

| 项 | 说明 |
|----|------|
| 依赖 | `.venv` 安装 `pandas-ta-classic`（import 名：`pandas_ta_classic`） |
| 新增 | `indicators/technical_lib.py` — 薄封装，仅算 RSI/MACD/BOLL |
| 扩展 | `indicators/technical.py` · `compute_one` **额外**写入新列；旧字段逻辑原样保留 |
| 表结构 | `token_indicators` + `indicator_history` 增加可空列；`base.py` ALTER 迁移 |
| 校验 | `scripts/compare_indicators.py` — 旧字段可算 + 新字段范围 sanity |
| 权重 | `INDICATORS_WEIGHT_*` 仍默认 0，未改 composite |

## 2. 新增列（均可空）

- `rsi_14`
- `macd` / `macd_signal` / `macd_hist`
- `boll_upper` / `boll_mid` / `boll_lower`

历史不足时写 `NULL`（不伪造）。无真 OHLC：H/L 用 close 代替（仅内部预留）。

## 3. 明确未改

- `change_5m/15m/1h/4h/24h`、`volatility_24h`、`volume_strength` 公式与调用路径
- `indicators/run.py` 调度、`main.py`、JEV / sentiment / composite 权重
- 教程里「整文件替换 technical.py」方案 **未采用**（按组共识旁路）

## 4. 文件清单

```
indicators/technical_lib.py          # 新增
indicators/technical.py              # 扩展 compute_one
indicators/base.py                   # TOKEN_INDICATOR_COLS + migrate + history INSERT
models/indicator_tables.sql          # CREATE 定义同步
scripts/compare_indicators.py        # 新增
requirements.txt                     # +pandas / +pandas-ta-classic
INDICATORS_PANDAS_TA.md              # 本文件
```

## 5. 验证命令

```bash
cd /workspace/dexscreener-crawler
.venv/bin/python -c "import pandas_ta_classic as ta; print(ta.version)"
.venv/bin/python -m py_compile indicators/technical_lib.py indicators/technical.py scripts/compare_indicators.py
.venv/bin/python scripts/compare_indicators.py --limit 5
# 仅重启 indicators.run（勿停 main / jev）
```

## 6. 备注

- MACD 列名按前缀映射（`MACD` / `MACDs` / `MACDh`），避免不同版本列顺序差异。
- 旁路字段暂不参与 composite；权重保持 0。
