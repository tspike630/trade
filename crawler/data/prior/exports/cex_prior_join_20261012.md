# CEX listings × 链上先验联合归因（Step 2）— 20261012

- 生成: `2026-10-12T00:42:36+08:00` (HK)
- **observe-only / 默认关 / 不调权**
- 输入: `ccxt.db` × `data/prior/db/prior.db` × `dexscreener.db`

## 口径
- symbol 匹配: `upper(base of CEX symbol) == upper(pairs.baseToken.symbol)`
- canonical pair: `max liquidity.usd, tie-break earliest pair_created_at`
- `first_seen_ts` = CCXT 旁路首次见到，**≠** 交易所官方上线日
- quote-like base（USDT/USDC/TRY…）不计为可匹配新币

## 匹配率

| 指标 | 值 |
|------|----|
| n_listings | 100 |
| eligible (非 quote-like base) | 95 |
| skipped quote-like | 5 |
| matched → pairs | 86 (90.5% eligible) |
| matched → prior.token_ohlcv | 16 (16.8% eligible) |
| matched either | 86 (90.5% eligible) |
| unmatched eligible | 9 |
| exchanges | kucoin, okx |

### Unmatched / skipped

- `okx` `USDT/TRY` base=`USDT` — quote_like_base
- `okx` `USDC/USDT` base=`USDC` — quote_like_base
- `okx` `USDT/BRL` base=`USDT` — quote_like_base
- `kucoin` `USDT/USDC` base=`USDT` — quote_like_base
- `kucoin` `USDC/USDT` base=`USDC` — quote_like_base
- `okx` `MAGIC/USDT` base=`MAGIC` — no_pair_no_prior_ticker
- `okx` `OKB/USDT` base=`OKB` — no_pair_no_prior_ticker
- `okx` `BCH/USDT` base=`BCH` — no_pair_no_prior_ticker
- `okx` `MAGIC/TRY` base=`MAGIC` — no_pair_no_prior_ticker
- `okx` `ZK/USDT` base=`ZK` — no_pair_no_prior_ticker
- `okx` `STX/USDT` base=`STX` — no_pair_no_prior_ticker
- `kucoin` `PNT/USDT` base=`PNT` — no_pair_no_prior_ticker
- `kucoin` `EVAA/USDT` base=`EVAA` — no_pair_no_prior_ticker
- `kucoin` `LONGXIA/USDT` base=`LONGXIA` — no_pair_no_prior_ticker

## 延迟（链上 → CEX first_seen）

| 来源 | n | median (h) | p25 | p75 | p90 |
|------|---|------------|-----|-----|-----|
| pair_created_at → cex | 86 | 784.8706 | 184.1976 | 1946.2646 | 2660.9021 |
| prior discovery_ts → cex | 16 | 7055.9398 | 7047.9092 | 7056.2662 | 7070.1176 |

- caveat: mega-cap / collision 样本 latency 不可解释为上市延迟

- pair symbol collision: n=86 median=4.0 p90=15.0 (同 ticker 池数)

## 上线后短窗收益

### ccxt_ohlcv

| horizon | n | median | p25 | p75 |
|---------|---|--------|-----|-----|
| 15m | 35 | 0.019014 | 0.009922 | 0.04373 |
| 1h | 35 | 0.019014 | 0.009922 | 0.04373 |
| 4h | 35 | 0.001919 | 0.0 | 0.006519 |

### pair_history（matched canonical pair）

| horizon | n | median | p25 | p75 |
|---------|---|--------|-----|-----|
| 15m | 34 | 0.0 | 0.0 | 0.0 |
| 1h | 34 | 0.0 | 0.0 | 0.0 |
| 4h | 27 | 0.0 | 0.0 | 0.0 |

## 与 prior 基线对照（只并列）

- graduation_rate: `0.001983`
- same_day_death_rate: `0.6867`
- gmgn median_peak_return_x: `1.35`
- price_behavior ret_1h median: `-0.000167`
- price_behavior pump_4h multiple p50: `1.442444`

- CEX 旁路 listings 当前以主流/高流动性现货为主，与 memecoin prior (graduation≈0.001983, same_day_death≈0.6867) 不在同一抽样总体；并列对照即可，不可直接做 uplift。
- 链上同 ticker 撞车严重时，latency(pair_created→cex) 对 mega-cap 无因果意义（wrapped/clone pair 的 created_at ≠ 资产诞生）。
- post-listing 短窗收益依赖 sidechannel 持续拉取；冷启动当下 forward bars 常为 0。

## Warnings

- ⚠ small n=100 ccxt_listings；统计仅观察，勿外推为上市溢价先验
- ⚠ ccxt_listings.first_seen_ts = 旁路首次见到，≠ CEX 官方上线日；冷启动会把 BTC/ETH 等存量也记入
- ⚠ symbol 精确匹配存在严重撞车（同 ticker 多 mint）；canonical = 最高流动性 pair

## 不做 / 默认关
- 不写业务四库、不改 INDICATORS_WEIGHT_*、不接 trading / alerts
- 不自动挂 review 周报（§6b 仍只做 CEX 市场基准；本 join 留给复盘决定是否旁挂）
- 验证「链上新币 → CEX 上线 → 拉升」需更大 n + 真上线事件源后再谈加权

