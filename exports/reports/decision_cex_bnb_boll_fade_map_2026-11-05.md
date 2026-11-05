# 决策 · Binance CEX 旁簿映射写绿（2026-11-05）

杨令（≈12:46–12:49 HKT）：链上 LiveExecutor 不作 `tech_boll_fade` 真盘出口；转币安 CEX。先由决策把 CEX 旁簿映射写绿，执行再自动下单。本文件只写决策侧。不下单、不碰密钥、不摘 `LIVE_TRADING_DISABLED`。

## 身份

| 项 | 值 |
|---|---|
| purpose | `live_bnb_boll_fade`（`PURPOSE_LIVE_BNB_BOLL_FADE`，已进 `PURPOSES`） |
| strategy id | `tech_boll_fade_v1`（类 `TechBollFadeStrategy`，规则版本 `tech_boll_fade_v1_2026-11-01`） |
| 策略别名 | `--strategy live_bnb_boll_fade` 与 `--strategy race_tech_boll_fade` 都解析到同一个类 |
| 市场 | Binance **现货**、计价 **USDT** |
| 下单符号 | `BASEUSDT`（例 `ZZQBOLLUSDT`）。CCXT 统一写法 `BASE/USDT` 只作对照，不作为成交 id |

不复用、不改写：

- `race_tech_boll_fade`：纸面 DEX 对照，继续按 Solana `chain_id` + mint 发信号
- `live_tech_boll_fade`：先前链上出口。**冻结，不是本臂 CEX 出口**
- `live_candidate`：已废壳，不复活

只此一条实盘臂。本变更不登记第二套策略、不改阈值、不把 `local_score` 接进入场（影子权仍为 0）。

## 宇宙

CEX 旁簿和纸面 Solana DEX **不是同一个分布**。看板必须分列：DEX 纸面（`race_tech_boll_fade`）与币安旁簿（`live_bnb_boll_fade`）。不要把两边的 fill 加成同一条收益。

宇宙规则（只在 purpose=`live_bnb_boll_fade` 时生效）：

1. 扫描集仍是现有 `hot_tokens`（指标库）。因子字段不重算：`change_5m`、`volume_strength`、`boll_lower` / `boll_low`、`price_usd` / `price` 原样交给 `TechBollFadeStrategy`。
2. 入场条件与纸面相同：`change_5m <= -min_change_5m` 且 `volume_strength >= min` 且 `price < boll_lower`。缺字段跳过。默认门槛仍是 5 分钟跌幅 2、量能 0.5。
3. 只保留 **base 能对上 Binance 现货 USDT 上市集合** 的行。对不上、没有符号、或不是 USDT 计价的，丢弃（失败即关闭，不把 mint 写进本 purpose）。
4. 同一 `BASEUSDT` 只留热度排序里的第一行。

上市集合（只读本地库，不请求交易所、不读密钥）：

- 指标库或旁路库里的 `cex_listing_events`、`ccxt_listings`、`ccxt_ohlcv`、`ccxt_ticks`
- `exchange` 必须是 `binance`
- 现货：符号里不能有 `:`（`BTC/USDT:USDT` 这类合约不算）
- 计价必须是 `USDT`
- 旁路路径（文件存在才读）：指标库旁边的 `ccxt.db`，以及仓库根 / `crawler/` 下的 `ccxt.db` 与 `data/prior/db/prior.db`

集合为空时，本 purpose 本拍发射 0 条。执行不要用 DEX 行补。

## 符号映射

DEX 行上的 base 来自行内 `base_asset` / `base_symbol` / `base` / `symbol` / `token_symbol`，否则来自 `tokens.raw_json` 的 `symbol` 或 `baseToken.symbol`。

| 输入 | 结果 |
|---|---|
| `SOL`、`sol/usdt`、`SOLUSDT` | base `SOL`，成交 id `SOLUSDT` |
| `SOL/USDC`、`USDT`、带 `:` 的合约符号 | 不映射 |
| 上市集合里没有该 `BASEUSDT` | 不映射 |

写成 `decision_log` 的身份（执行用这些字段下单，不要用 DEX mint）：

| 字段 | 位置 | 含义 |
|---|---|---|
| `chain_id` | 列 | `binance` |
| `token_address` | 列 | `BASEUSDT` |
| `pair_address` | `raw_json` | `BASE/USDT`（CCXT 对照，不是池地址） |
| `extras.binance_symbol` | extras | 与 `token_address` 相同的 `BASEUSDT` |
| `extras.market` | extras | `spot` |
| `extras.quote_asset` | extras | `USDT` |
| `extras.venue` | extras | `binance` |
| `extras.universe` | extras | `binance_spot_usdt` |
| `extras.source_chain_id` / `extras.source_token_address` | extras | 来源 DEX 行，**只溯源，不是成交标的** |
| `extras.and_mode` | extras | 仍是 `tech_fade&boll_lower` |

`chain_id` / `token_address` 在本 purpose 上不再是 Solana mint。执行若把 `source_token_address` 当成币安符号，会下错单。

## 决策进程

写到主决策库（与纸面 race 相同的指标库），**不要**加 `--output-db`。`decision_engine.run` 在输出库与指标库不同、且 purpose 不是 `shadow_*` 时会把 purpose 改成 `shadow_010`，本臂信号就进不了 `live_bnb_boll_fade`。

```bash
python -m decision_engine.run \
  --strategy tech_boll_fade_v1 \
  --purpose live_bnb_boll_fade \
  --dry-run
```

`--strategy live_bnb_boll_fade` 等价，发出的 `strategy_id` 仍是 `tech_boll_fade_v1`。`mode` 保持 `dry_run`。本进程不下单。

纸面对照继续单独跑 `--purpose race_tech_boll_fade`，库仍是 `paper_eq_tech_boll_fade.db`。不要把纸面 peer 的 `--accept-purpose` 改成 `live_bnb_boll_fade`。

## 交给执行

自动下单只接这一条：

- `--accept-purpose live_bnb_boll_fade`
- 工具面是 `user-binance` 现货（或执行自己的 `LIVE_EXCHANGE_*`），符号用 `extras.binance_symbol`（即 `BASEUSDT`）
- 账本 / ledger 与纸面 `paper_eq_tech_boll_fade.db`、`execution_ledger_race_tech_boll_fade.db` 隔离
- **不要** `--accept-purpose live_tech_boll_fade`（链上出口已冻结）
- **不要** `--accept-purpose race_tech_boll_fade` 来做本臂真仓
- **不要** 把 `live_candidate` 当成本臂

执行在映射落地之后再自动下。决策侧没有 MCP 试单，也没有改真仓闸。名义规模、熔断由执行另定；那是风控壳，不是改策略。

共享的决策流动性地板（`DECISION_MIN_LIQ_USD`）仍作用在扫描行自带的 `liquidity_usd` 上。这不是新门槛，也没有改 boll-fade 公式。
