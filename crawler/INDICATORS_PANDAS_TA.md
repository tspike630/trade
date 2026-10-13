# INDICATORS · pandas-ta 旁路（ADD-ON，不替换）

> 日期：2026-10-10（HK）  
> 仓库：`/workspace/dexscreener-crawler`  
> 共识：在既有 `change_*` / `volatility_24h` / `volume_strength` **之上追加** RSI/MACD/BOLL，**不替换**旧口径；composite 权重仍为 0。
> V2 增量（2026-10-13）：`technical_lib` 另算 **atr_14 / adx_14（只算不落库）**。

## 1. 做了什么

| 项 | 说明 |
|----|------|
| 依赖 | `.venv` 安装 `pandas-ta-classic`（import 名：`pandas_ta_classic`） |
| 新增 | `indicators/technical_lib.py` — 薄封装：RSI/MACD/BOLL（落库）+ ATR/ADX（只算） |
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

## 7. 样本与重启（2026-10-10 22:05 HKT）

样本（`solana/2j5sas7xy776…`，151 根 history）：

| 字段 | 值 |
|------|-----|
| change_1h（旧，保留） | -11.178 |
| volatility_24h（旧，保留） | 7.211 |
| volume_strength（旧，保留） | 0.608 |
| rsi_14 | 48.777 |
| macd / signal / hist | 1.01e-4 / 1.97e-4 / -9.56e-5 |
| boll_upper / mid / lower | 0.004873 / 0.004405 / 0.003937 |

重启：仅 `indicators.run` → PID 2299758（`main.py` / `sources.jev` 未动）。  
推送：`tspike630/trade` commit `d2a2bd0`（`crawler/`，无 secrets）。

## 8. V2 增量 · ATR / ADX（只算不落库）

> 日期：2026-10-13（HK）  
> 相对教程《方案1 V2》：教程要求整文件覆盖 + `import pandas_ta` —— **本仓库禁止照做**。  
> 实际做法：在既有 `pandas_ta_classic` 旁路之上增量补 ATR/ADX。

| 项 | 说明 |
|----|------|
| 计算 | `indicators/technical_lib.py` · `compute_pandas_ta` 增加 `atr_14` / `adx_14` |
| 参数 | `ta.atr(hi,lo,close,length=14)` · `ta.adx(...)`；列名按前缀映射（`ATR*` / `ADX_`） |
| 落库 | **否**。不改 `indicator_tables.sql`、`TOKEN_INDICATOR_COLS`、不 ALTER、不写 `token_indicators` |
| 写库隔离 | `technical.py` 的 `PANDAS_TA_FIELDS` 仍仅 rsi/macd/boll；`base.py` upsert 只认 TOKEN_INDICATOR_COLS |
| OHLC 局限 | pair_history 无真 high/low → 缺省 H=L=close；ATR/ADX 可用但保真度有限 |
| 权重 | 未改，仍为 0 |

### 验收

```bash
cd /workspace/dexscreener-crawler
.venv/bin/python -m py_compile indicators/technical_lib.py scripts/compare_indicators.py
.venv/bin/python scripts/compare_indicators.py --limit 5
# 输出应含 atr_14=… adx_14=…；短历史可为 None；有值则 atr≥0、adx 约 0–100
```

假序列自测（len≥50 应出数）：

```bash
.venv/bin/python -c "
from indicators.technical_lib import compute_pandas_ta
closes=[100+i*0.1+(i%7)*0.3 for i in range(60)]
r=compute_pandas_ta(closes)
print({k:r[k] for k in ('atr_14','adx_14','rsi_14')})
print('short', compute_pandas_ta(closes[:5])['atr_14'])
"
```
