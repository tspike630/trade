# 决策 · live_bnb 读币安因子（对齐 #160）

杨令（2026-11-05，「只要币安的」）：`live_bnb_boll_fade` 吃币安现货。指标 PR #160 写 `binance_klines` / `binance_indicators`。决策按那份表结构读，不另抓 `exchangeInfo`，不下单，不动 `LIVE_TRADING_DISABLED`。

纸面 `race_tech_boll_fade` 仍扫 Solana。`tech_boll_fade_v1` 阈值不改。`local_score` 权为 0。

## `LIVE_BNB_FACTOR_SOURCE`

| 值 | 行为 |
|---|---|
| `auto`（默认） | 表 `binance_indicators` 已建好就**只读它**，哪怕本拍是空的，也不再用 K 线重算，也不用 DEX 补。表还没建、但 `binance_klines` 有 5m K，才在决策进程里用同一套公式算因子。两张表都没有，才退回 DEX 热池∩上市映射 |
| `binance` | 只读 CEX。指标表没有、K 线也没有 → 本拍 0 条 |
| `dex_map` | 旧映射旁路 |

采集层宇宙（`exports/binance_spot_universe.db` 表 `symbols`，或 `exports/reports/binance_spot_usdt_trading_2026-11-05.json`，或 `BINANCE_SPOT_UNIVERSE_DB` / `BINANCE_SPOT_UNIVERSE_JSON`）只约束 **K 线现算** 这条后备。指标表在的时候，宇宙以表里的行为准（指标任务已经按 518 个 TRADING `*USDT` 写过）。快照文件在但筛完是空的：K 线后备 0 条，`auto` 不用 DEX 补。

## 表（与 #160 相同）

`binance_indicators`：`symbol`，`status`，`interval`，`price_usd`，`change_5m`，`volume_strength`，`boll_lower`，`quote_volume_24h`，`tech_updated_at`，`updated_at`。

`liquidity_usd` 不另开门槛：读的时候把 `quote_volume_24h` 填进现有的 `DECISION_MIN_LIQ_USD` 地板。`BINANCE_INDICATORS_FRESH_SEC` 默认 3600。扫描窗口按 `change_5m` 升序，不是入场条件。其它 purpose 仍是 `DECISION_HOT_TOKEN_LIMIT`。`live_bnb_boll_fade` 不传 `--limit` 时至少 600（`LIVE_BNB_SCAN_LIMIT`），盖住约 518 个名字。显式 `--limit` 仍按传入值截断。

`binance_klines`：`symbol`，`interval='5m'`，`open_time_ms`，`open/high/low/close`，`volume`，`quote_volume`，`close_time_ms`。

K 线现算（仅指标表不存在时）对齐纸面参数，不改阈值：

| 字段 | 算法 |
|---|---|
| `change_5m` | `change_pct`，300 秒，5m 收盘对齐就是相邻两根 |
| `volume_strength` | 最近 1 小时 quote volume（USDT）/ 过去 24 小时这些读数的均值。某根缺 quote 才用 base volume |
| `boll_lower` | SMA(20) − 2 × 总体标准差（`ddof=0`），即 `bbands(length=20, std=2)` |
| `price_usd` | 最后一根已收盘 5m 的收盘价 |

未收盘 K 丢掉。`LIVE_BNB_KLINE_FETCH=1` 时，宇宙里还没有 K 线的符号可以打公开 `GET /api/v3/klines`（无密钥）。`LIVE_BNB_KLINE_FETCH_MAX` 默认 40，是请求预算，不是入场门。默认关闭。

指标表一旦落地，这条进程内算法就停用。生产主粮是指标任务写好的 `binance_indicators`。全宇宙靠进程内拉取会撞上预算，覆盖不全。

## 发出去的 extras

```json
{
  "binance_symbol": "BTCUSDT",
  "ccxt_symbol": "BTC/USDT",
  "venue": "binance",
  "market": "spot",
  "quote_asset": "USDT",
  "base_asset": "BTC",
  "universe": "binance_spot_usdt",
  "factor_source": "binance_indicators",
  "and_mode": "tech_fade&boll_lower"
}
```

`token_address` 与 `extras.binance_symbol` 都是 `BTCUSDT`。K 线现算时 `factor_source` 为 `binance_klines` 或 `binance_kline_fetch`。

## 干跑

不要加 `--output-db`。

```bash
python -m decision_engine.run \
  --strategy tech_boll_fade_v1 \
  --purpose live_bnb_boll_fade \
  --dry-run
```

指标表还没建、库里已有 `binance_klines` 时，上面这条会现算。只想打公开 K 线：

```bash
LIVE_BNB_KLINE_FETCH=1 \
  python -m decision_engine.run \
  --strategy tech_boll_fade_v1 \
  --purpose live_bnb_boll_fade \
  --dry-run
```

纸面对照仍是 `--purpose race_tech_boll_fade`。
