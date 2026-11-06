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

公开 REST `GET /api/v3/klines`（不需要密钥）。因子沿用纸面同一套函数：`change_pct`、`volume_strength`、`compute_pandas_ta` 的 BOLL(20, 2σ)，收盘价当 close。`volume_h1` 样本是该根 K 线往前 1 小时的 quote volume 合计，比值公式没改。纸面 `race_tech_boll_fade` 的阈值仍是 5 分钟跌 2、量能 0.5、价格在下轨下。`live_bnb_boll_fade` 的入场已换成 Entry Zone，见 `docs/live_bnb_entry_zone_2026-11-05.md`。跌 2 / 破下轨只留在该臂 extras 里做诊断，不再挡新区间。

只写这两张表，和 Solana `token_indicators` / `pair_history` 分开：

- `binance_klines`
- `binance_indicators`

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

这是扫描窗口，不是入场门。纸面 `race_tech_boll_fade` 的 `min_change_5m` / `volume_strength` / 破下轨 / TP10 / SL5 / hold 不动。`live_bnb_boll_fade` 用 Entry Zone 的绝对止损和 `boll_mid` 止盈，不套纸面 SL5/TP10。其它 purpose 继续用 `DECISION_HOT_TOKEN_LIMIT`（默认 50）。`live_bnb_boll_fade` 不传 `--limit` 时窗口至少 `LIVE_BNB_SCAN_LIMIT`（600），盖住约 518 个 TRADING `*USDT`，避免跌幅排在 51 名之后的破下轨被 `ORDER BY change_5m ASC` 裁掉。显式 `--limit` 仍按传入值截断。K 线后备的 1 小时量优先用 quote volume（USDT），与指标任务一致；某根缺 quote 才退回 base volume。

`race_tech_boll_fade` 仍读 Solana `token_indicators`，不看这两张表。

## 新买单：3M quoteVolume 与当前宇宙（2026-11-06）

只作用在 `live_bnb_boll_fade` 和 `live_bnb_boll_classic` 的**新买**。纸面、链上、卖出、以及已经挂上的保护单都不走这道门。执行层的单臂单仓闸不改。

门槛与宇宙刷新共用一个环境变量，默认都是 300 万：

`BINANCE_SPOT_MIN_QUOTE_VOLUME`（`3e6` / `3000000`）。空白或无法解析时用 300 万，不用 0。

一笔新买要同时满足：

1. 符号在当前主宇宙快照里（`symbols` 表，或 `BINANCE_SPOT_UNIVERSE_DB` / `BINANCE_SPOT_UNIVERSE_JSON`）。快照没挂上时新买失败关闭，旧的 `binance_indicators` 行不能把已移出的名字买回来。
2. 有一份新鲜的 24 小时 USDT `quoteVolume`，大于等于上面的门槛。来源按顺序：`ccxt_ticks.quote_volume_24h` 加 `ts`，否则宇宙表 `symbols.quoteVolume_24h` 加 `quoteVolume_collected_at`。后者是 `python -m indicators.binance_universe_quote` 在 `GET /api/v3/ticker/24hr` 与 `exchangeInfo` 都解析成功时写下的采集时刻，不是文件 mtime，也不是迁移时刻。只有 `quoteVolume_24h`、没有 `quoteVolume_collected_at` 的旧行失败关闭。不用 base volume，不用 `liquidity_usd`，不用 5 分钟 K 线，也不用 `binance_indicators.quote_volume_24h`。缺、非有限、没有采集时刻、过期，都失败关闭。新鲜窗口沿用 `BINANCE_INDICATORS_FRESH_SEC`（默认 3600 秒）；把它设成 0 不会把过期行情放进来。

上线顺序（本 PR 不执行，也还没有接到现网调度）：

1. 停掉对同一个 `binance_spot_universe.db` 的 `refresh_binance_spot_universe.py`。该脚本不在本仓库，现有 crontab / `run_binance_spot_loop.sh` 也不调用它。K 线循环只读 `symbols`，不会删 `quoteVolume_collected_at`。若库外脚本继续重建 `symbols` 或不改时间只改 `quoteVolume_24h`，采集时刻会丢，或出现新 quote 配旧时间。
2. 合入 reader。旧行没有 `quoteVolume_collected_at`，新买保持拒绝。
3. 用 `scripts/run_binance_universe_quote_loop.sh` 周期跑 `python -m indicators.binance_universe_quote`。默认 300 秒，必须小于 `BINANCE_INDICATORS_FRESH_SEC`（默认 3600）。只跑一次会在窗口过后整批拒买。成功的一轮在同一事务里写下该次 ticker 的 quote 和同一个采集时刻；失败不改上一轮。不要用文件 mtime。
4. 干跑 `live_bnb_boll_fade`，看 `live_bnb_turnover` 与 `live_bnb_top_buys`。决策进程要读到 `exports/binance_spot_universe.db`（指标库同级的 `exports/`，或 `BINANCE_SPOT_UNIVERSE_DB`）。

不要摘 `LIVE_TRADING_DISABLED`，不要启动 classic。本仓库没有现网调度，不能视为端到端已接通。

扫描窗口仍是至少 600，先筛过门槛和宇宙，再跑原来的 Entry Zone 或 classic 腿、流动性地板和冷却。通过之后按 `volume_strength` 从高到低，相同则 24h quoteVolume 从高到低，再按符号。每个 purpose 每一拍最终新买最多 `LIVE_BNB_TOP_BUYS`（默认 2）。只有 1 个合格就发 1 个，不补齐；一个都没有就发 0。被策略、风险或冷却丢掉的名字不占名额。同一符号只留排序更高的一条。卖出不计数、不因门槛被丢掉。

执行层仍是每个 purpose 最多 1 个持仓。决策多给的第 2 个候选不会变成第二笔成交，也不绕过持仓闸。
