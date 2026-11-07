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

这是扫描窗口，不是入场门。纸面 `race_tech_boll_fade` 的 `min_change_5m` / `volume_strength` / 破下轨 / TP10 / SL5 / hold 不动。`live_bnb_boll_fade` 用 Entry Zone 的绝对止损和 `boll_mid` 止盈，不套纸面 SL5/TP10。其它 purpose 继续用 `DECISION_HOT_TOKEN_LIMIT`（默认 50）。`live_bnb_boll_fade` 不传 `--limit` 时窗口至少 `LIVE_BNB_SCAN_LIMIT`（600），盖住约 518 个 TRADING `*USDT`，避免跌幅排在 51 名之后的破下轨被 `ORDER BY change_5m ASC` 裁掉。显式 `--limit` 仍按传入值截断。K 线后备的 1 小时量优先用 quote volume（USDT），与指标任务一致。决策侧存的 K 线是 `(open_ms, open, high, low, close, base, quote)`，quote 在下标 6。原始 REST JSON 的下标 6 是收盘时间、下标 7 才是 quote，不要混用。某根 quote 缺失（`None`）才退回 base volume。quote 为 0 是真零，不退回 base；整段都是 0 时 `volume_strength` 没有正的均值，该符号不算出来，而不是改用 base 数量。

`race_tech_boll_fade` 仍读 Solana `token_indicators`，不看这两张表。

## 只读观察列（决策侧不计算）

`list_binance_hot_tokens` 的每一行都带这 8 个键：`change_7d`、`volume_7d_avg`、`taker_buy_quote_volume_5m`、`buy_ratio_5m`、`buy_ratio_1h`、`avg_trade_usd_5m`、`trade_size_ratio`、`lower_wick_ratio`。旧表没有其中某一列、SQL 是 NULL、空白或非有限数，读出来都是 `None`。不补 0，不按 24h×7 或残缺窗口现造 7 日，也不在决策侧打开 12 根或 24 小时的计算。读路径不 `INSERT` / `UPDATE` / `ALTER`。

算术在指标任务（`indicators.binance_spot`，另一份变更）里做完再落库。约定：

- `change_7d`、`volume_7d_avg`：本地 7 日已收盘 K 线没有完整覆盖时为 NULL。
- `taker_buy_quote_volume_5m`：原始 K 线的 taker buy quote（REST 下标 10）。REST 下标 9 是 taker buy base，决策侧不读。`buy_ratio_5m` 由上游用该根 quote 去除。
- `buy_ratio_1h`：必须是连续 12 根已收盘 5 分钟，且每根的 taker quote 都齐全。少一根或缺一侧就是 NULL，不把残缺的一小时算进去。
- `avg_trade_usd_5m`：该根 quote volume / trades。`trade_size_ratio` 的 24 小时基准是整段 `sum(quote_volume)/sum(trades)`，窗口要完整；不是先算每根平均再取平均。覆盖不满则为 NULL。
- `lower_wick_ratio` 这个名字保留。库存公式是 `(close-low)/(high-low)`，即收盘在最高最低之间的位置，不是下影线 `(min(open, close)-low)/(high-low)`。决策侧照原值传出，不改写成下影线。

`tech_boll_fade` 现有的跌幅、量能、下轨和 Entry Zone 门槛都不读这 7 个新值。`trade_size_ratio` 本来就是 Entry Zone 的坐标：NULL 仍当缺列，继续走已有的 `avg_trade_size` / K 线回退；库里的有限数仍是原来的坐标。`INDICATORS_WEIGHT_*` 仍为 0。两臂保持暂停，本读路径不下单。

这些键在热行上。现网 `live_bnb_boll_fade` / `live_bnb_boll_classic` 只跑 `tech_boll_fade_v1`，不会因为观察列而改入场。已经在找 `change_7d` / `volume_7d_avg` 的纸面策略（`lifecycle_momentum`、`cs_mom_1w`、`cs_fade_1w`）如果被排进同一个 purpose，会看到非 NULL 的值。本变更不把那些策略接到币安臂上。

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
3. 只用 `scripts/run_binance_universe_quote_loop.sh`，并写明工作根、解释器和主库，不要靠脚本所在目录猜 `crawler/.venv`。采集名单在写入时就去掉不足 `BINANCE_SPOT_MIN_QUOTE_VOLUME`（默认 300 万）的现货，因为 K 线循环会扫整张 `symbols`、自己不再滤 quote。决策侧仍按同一门槛和新鲜度复核，Top 2 不变。镜像用主库提交后的 SQLite backup 再改名覆盖，不是再拉一次行情；某一份镜像失败就留着它上一份完整文件。JSON 同样在主库提交之后整文件替换。只跑一次会在 3600 秒后整批拒买，所以周期必须短于新鲜窗口。用户启动公开行情 loop 的确认还没落地，本仓库不自启。
4. 干跑 `live_bnb_boll_fade`，看 `live_bnb_turnover` 与 `live_bnb_top_buys`。决策进程要读到 `exports/binance_spot_universe.db`（指标库同级的 `exports/`，或 `BINANCE_SPOT_UNIVERSE_DB`）。

不要摘 `LIVE_TRADING_DISABLED`，不要启动 classic。本仓库没有现网调度，不能视为端到端已接通。

扫描窗口仍是至少 600，先筛过门槛和宇宙，再跑原来的 Entry Zone 或 classic 腿、流动性地板和冷却。通过之后按 `volume_strength` 从高到低，相同则 24h quoteVolume 从高到低，再按符号。每个 purpose 每一拍最终新买最多 `LIVE_BNB_TOP_BUYS`（默认 2）。只有 1 个合格就发 1 个，不补齐；一个都没有就发 0。被策略、风险或冷却丢掉的名字不占名额。同一符号只留排序更高的一条。卖出不计数、不因门槛被丢掉。

执行层仍是每个 purpose 最多 1 个持仓。决策多给的第 2 个候选不会变成第二笔成交，也不绕过持仓闸。
