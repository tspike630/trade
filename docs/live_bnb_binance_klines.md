# live_bnb · 币安现货 K 线 tech_boll 旁路（2026-11-05）

杨令（2026-11-05 ≈14:38 HKT，房「我要赚米」）「只要币安的」：`live_bnb_boll_fade` 的数据宇宙是币安现货，不是 DEX 热池∩符号映射。

≈14:41 HKT 消息采集层已经把主宇宙落到共享机，K 线任务**只迭代这份名单**，不再自己打 `exchangeInfo`。

## 符号从哪来

按顺序用第一个能读出 TRADING `*USDT` 的文件：

| 顺序 | 路径 |
|---|---|
| 1 | 环境变量 `BINANCE_SPOT_UNIVERSE_DB` |
| 2 | `exports/binance_spot_universe.db` 表 `symbols`（采集层，518 个 TRADING `*USDT`） |
| 3 | `crawler/exports/binance_spot_universe.db`（同一份的兼容位置） |
| 4 | 环境变量 `BINANCE_SPOT_UNIVERSE_JSON` |
| 5 | `exports/reports/binance_spot_usdt_trading_2026-11-05.json` |
| 6 | `crawler/exports/reports/` 下同名 JSON |

挂卡：`hangcards/binance_primary_universe_20261105.md`（只说明，不解析）。

`*.db` 与 `exports/reports/*` 不进 Git。跑任务的机器上文件要在。名单为空就退出，不用 DEX 行凑，也不临时去拉 `exchangeInfo`。

表 `symbols` 认这些列名：`symbol` / `binance_symbol`，`base` / `base_asset`，`quote` / `quote_asset`，`status`。`status` 不是 `TRADING` 的丢掉。合约符号（带 `:`）丢掉。

## 写到哪

公开 REST `GET /api/v3/klines`（不需要密钥）。因子沿用纸面同一套函数：`change_pct`、`volume_strength`、`compute_pandas_ta` 的 BOLL(20, 2σ)，收盘价当 close。`volume_h1` 样本是该根 K 线往前 1 小时的 quote volume 合计，比值公式没改。阈值仍是 5 分钟跌 2、量能 0.5、价格在下轨下。

只写这两张表，和 Solana `token_indicators` / `pair_history` 分开：

- `binance_klines`（另存公开 K 线数组下标 10 的 `taker_buy_quote_volume`）
- `binance_indicators`（另存影子列 `avg_trade_size`、`trades_spike_ratio`、`taker_buy_quote_ratio`、`change_5m_z`）

这些列只作旁证，不进 `list_binance_hot_tokens` 的扫描行，也不进 `tech_boll_fade` 硬门。`change_5m_z` 是当前 5m 收益（百分点，与 `change_5m` 同单位）除以前 20 根 5m 收益的 σ，不是把入场改成 k·σ，也不拿 `volatility_24h` 当这个 σ。BOLL 仍是 20, 2σ。口径见 `docs/crowding_spot_shadow_columns_2026-11-05.md`。`volume_strength` 仍是原来的 1 小时 quote 合计比 24 小时均值。

`INDICATORS_WEIGHT_*` 保持 0。不把 `local_score` 当入场门。不下单。

```bash
cd /workspace
PYTHONPATH=crawler python -m indicators.binance_spot --dry-run
PYTHONPATH=crawler python -m indicators.binance_spot --db crawler/dexscreener.db
# 冒烟可加 --max-symbols 5 或 --symbol BTCUSDT（必须在主宇宙里）
```

`--db` 是决策已经打开的指标库。任务只往里加 `binance_*` 表。

## 决策怎么读

读函数：`decision_engine.core.binance_indicators.list_binance_hot_tokens`。

返回行已经是 `TechBollFadeStrategy` 能扫的形状：`chain_id=binance`，`token_address=BASEUSDT`，`change_5m` / `volume_strength` / `boll_lower` / `price_usd`，以及 `extras` 用的 `binance_symbol`、`universe=binance_spot_usdt`。`liquidity_usd` 填的是 24 小时 quote volume，给**现有** `DECISION_MIN_LIQ_USD` 地板用，不是新分数门。

`DecisionEngine` 在 purpose=`live_bnb_boll_fade` 时：

| `LIVE_BNB_FACTOR_SOURCE` | 行为 |
|---|---|
| `auto`（默认） | `binance_indicators` 表已存在就只读它。表还没建过，才走原来的 DEX 热池∩上市映射 |
| `binance` | 只读 CEX 表。表空或过期 → 本拍 0 条，不用 DEX 补 |
| `dex_map` | 旧映射旁路。纸面对照不走这条 |

新鲜度：`BINANCE_INDICATORS_FRESH_SEC`（默认 3600）。扫描顺序默认 `change_5m` 升序，可用 `BINANCE_INDICATORS_SORT=volume_strength` 或 `quote_volume_24h`。

这是扫描窗口，不是入场门。`min_change_5m` / `volume_strength` / BOLL / TP / SL / hold 仍是纸面 `tech_boll_fade_v1`。其它 purpose 继续用 `DECISION_HOT_TOKEN_LIMIT`（默认 50）。`live_bnb_boll_fade` 不传 `--limit` 时窗口至少 `LIVE_BNB_SCAN_LIMIT`（600），盖住约 518 个 TRADING `*USDT`，避免跌幅排在 51 名之后的破下轨被 `ORDER BY change_5m ASC` 裁掉。显式 `--limit` 仍按传入值截断。K 线后备的 1 小时量优先用 quote volume（USDT），与指标任务一致；某根缺 quote 才退回 base volume。

`race_tech_boll_fade` 仍读 Solana `token_indicators`，不看这两张表。
