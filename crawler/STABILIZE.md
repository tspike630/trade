# Stabilize ops runbook

Live stabilize crawler: `main.py --helius-listen --sources x --loop` (do not kill without cause).
Kill-switch file `LIVE_TRADING_DISABLED` must stay present. Live trading stays gated off.

## Start

```bash
cd /workspace/dexscreener-crawler
source .venv/bin/activate
# Secrets only via process env or .env (config.py loads .env). stabilize_run.sh exports NO secrets.
export ENABLED_CHAINS="${ENABLED_CHAINS:-solana}"   # stabilize_run.sh also defaults this
./stabilize_run.sh
# or: nohup ./stabilize_run.sh >> logs/stabilize.out 2>&1 &
```

## Stop

```bash
# Prefer graceful: SIGTERM the main.py loop pid (find via pgrep or heartbeat)
pgrep -af 'main.py --helius-listen'
# kill <pid>   # SIGTERM; do NOT remove LIVE_TRADING_DISABLED
```

## Health

```bash
python main.py --health
python daily_check.py                 # PASS/WARN/FAIL → logs/daily_check-*.json
# heartbeat: data/heartbeat.json (or configured path) updated while --loop runs
ls -lt backups/ | head
test -f LIVE_TRADING_DISABLED && echo kill-switch=OK
```

## Daily / periodic

```bash
# One-shot backup (retain 7)
./scripts/backup_once.sh
# or: python backup.py --retain 7

# Paper tick (virtual ledger only)
python paper_trade.py tick
python paper_trade.py status

# Grok incremental report (no Telegram unless GROK_REPORT_TELEGRAM=1)
python scripts/grok_report.py --mode hourly --incremental

# Schedule examples: deploy/crontab.stabilize.example (+ deploy/crontab.example / systemd timers)
```

## Paper cash

- Default `PAPER_START_CASH=10000` (env).
- Small books: `python paper_trade.py init --cash 1000` or `--cash 1000 --reset-cash`.
- Optional auto-open (off): `PAPER_AUTO_OPEN=1` + `PAPER_AUTO_MIN_SCORE=70` + `PAPER_AUTO_SIZE_USD=50`.

## Telegram channels

During stabilize it is OK if MAIN/ALERT chat ids are unset and only ADMIN (or `TELEGRAM_CHAT_ID`) is set — alerts fall back. Documented in env.example.

## Optional hooks (default OFF)

- `DD_AUTO_ON_NEW_LISTING=1` / `DD_AUTO_ENABLED=1` — write DD markdown under `exports/reports/` for high-score / pending new listings.
- `PAPER_AUTO_OPEN=1` — open paper positions of `PAPER_AUTO_SIZE_USD` when score ≥ threshold.

## Notes on daily_check WARNs

- Non-`ENABLED_CHAINS` residuals (e.g. robinhood / other chains from earlier multi-chain runs) → WARN only; filter with `ENABLED_CHAINS=solana`.
- Heartbeat stale when loop not running → expected WARN; while stabilize `--loop` is up, heartbeat should stay fresh.

## Hot liquidity coverage (R1, not strategy validity)

DexScreener sometimes omits `liquidity` on a pair payload. `history.extract_metrics` stores `liquidity_usd` NULL and does not invent a number. Decision then fail-closes that buy with `no_liquidity`. The existing `tokens/v1` enrich already snapshots liquidity only when the payload includes it; there is no second fill.

`daily_check.py` check「热层流动性覆盖」reads hot `pair_history` (level = last 1h; 24h is context only):

- WARN if missing (NULL or ≤0) **> 25%**, if the hot 1h window is empty, or if n < 20 while the missing rate is > 60%
- FAIL if missing **> 60%** and n ≥ 20

A ~34% missing rate is WARN. The rate is **10-19 周检旁证** and does not count toward strategy validity. The hourly report (`scripts/grok_report.py` → context markdown) prints one line under Health: hot 1h missing rate and `pending_enrich`. Do not change `ALERT_MIN_SCORE` or score weights from this metric. How the warning cases use this check is in 「警示事例 → 采集侧」.

## G-13 异常高 APY（代码标签，观察）

宣传 APY/APR/yield ≥ **1000%**（或文案里的无限年化、≥10x APY）是信用风险信号，不是机会。`HeuristicRiskScanner` 只扫已经落库的文案：pair 的 `labels`、代币名称/符号、`info` 描述与社交 label、boosts 里的字符串，以及 token profile 的 `description` / `links` label。不读价格、成交量、流动性数字，不加新源。

命中则打 `risk_g13_high_apy`，并走现有 `apply_risk_to_score` 软扣 10 分（单独不够 `risk_elevated` 的 20 分门槛）。没有这段文案就不打标。省略 `liquidity` 仍是 NULL / `no_liquidity`，不编造。

`daily_check.py` 与小时报各计一次带该标签的代币数。观察项，供决策以后降权。本层不自动拒绝、不改 `ALERT_MIN_SCORE`、不改循环。**不算有效性。**

## G-5 market-cap denominator (observe, not validity)

G-5 (`turnover_spike` / `turnover_exhaust` = `volume_h24 / marketCap`, alternate `/ fdv`) reads the denominator from the same snapshot as volume. `history.extract_metrics` copies DexScreener root `marketCap` (alias `market_cap`) and `fdv` onto `pair_history.market_cap` and `pair_history.fdv`. `upsert_pair` writes the same two nullable columns on `pairs` for the latest payload. `tokens` stays without them: one token row is not the `volume_h24` snapshot.

Omitted, empty, or non-numeric fields are NULL. `fdv` is not copied into `market_cap`. Old rows are not backfilled from `raw_json` (that JSON is the latest body, not the cap at each past snapshot). Liquidity extraction is unchanged.

The ratio itself stays with indicators. This note does not score G-5.

`daily_check` check「热层流动性覆盖」adds `mcap_hot_1h`: hot 1h NULL counts for those columns. The liquidity PASS/WARN/FAIL level does not use them. The count is **10-19 周检旁证**, not strategy validity. `ALERT_MIN_SCORE`, Telegram routing, and loop intervals stay as they are.

## INTEL_LOOP I-1（钱包标签与持仓快照）

I-1 已落地的是表和尽力填充，不是因子。`address_labels` 记地址标签（这次会写 `dev` 和 `whale_top10`；`token_mint` 可空，库里空串表示不绑代币）。`token_holder_snapshots` 记同一次 RPC 的前 10 持仓：同一 `snapshot_id` 与 `recorded_at`，每行带 `amount_raw` / `amount_ui`，以及同一笔 `total_supply_raw` / `total_supply_ui`。供应量或数量缺失时 `pct_of_supply` 与对应列为 NULL，不编造持仓或余额。`pct_of_supply` 只是这一行的数量除以同一响应里的供应，不是 `concentration_*`。`top10_share` 与 `dev_balance_delta` 的公式仍归指标。I-2 等指标来读这些表再算；本层不写因子、不降权、不改买卖。

填充挂在已经在用的 Helius `getTransaction` 上（`_handle_notification` → `enqueue_solana_mints`）。确认过的 `initializeMint` / `initializeMint2` 若带 `mintAuthority`，且该地址不是已知程序或报价资产，写成 `dev`。没有该字段、或 authority 是程序地址，就不打标。日志正则命中的 mint 不打标。持仓快照走同一个 Helius HTTP endpoint 的 `getTokenLargestAccounts` + `getTokenSupply` + `getMultipleAccounts`（解析 owner）。仓库里没有 DAS 客户端，DexScreener 的 pair payload 也没有 holder 列表。`HOLDER_SNAPSHOT_ENABLED` 默认开，只在纸面观察；设为 `0` 则跳过这三次额外 RPC，dev 标签和下面的转账边仍从已拉取的交易里写。RPC 失败或空列表则跳过该代币，不造行。owner 解析失败时，行里留下 token account，`address_kind=token_account`，并且不打 `whale_top10`。

`token_transfer_edges` 只记这笔已经拉下来的 `getTransaction` 里、且 mint 属于本次入队代币的 SPL `transfer` / `transferChecked`。没有 mint 的转账丢掉。全量转账图和历史回填是 Phase-2。

`daily_check` 检查「INTEL_LOOP I-1」和小时报只计 `labeled_dev_n`、`holder_snapshot_n`、`tokens_with_top10`。空表是 PASS。查询失败是 WARN，不是 FAIL。这是 **10-19 旁证**，不算策略有效性。不改 `ALERT_MIN_SCORE`、cooldown、`min_liq`、Telegram 路由，也不动 LIVE 闸。I-3 的采集标记见下一节。I-4（OTC）不在这次。

## INTEL_LOOP I-3（bot 标记，只采集）

I-3 采集侧补的是以后填 `bot_trade_share` 的上游标记。候选不等于生效。高 bot 占比不跳单，不改执行，不接 PM。`ALERT_MIN_SCORE`、cooldown、`min_liq`、Telegram 路由、LIVE 闸都不动。

`address_labels.label_type` 可以写成 `bot_suspect`（钱包 + 代币）。证据不够就不写。没有「不是 bot」这一档。

`bot_fill_observations` 记这笔已经拉下来的 `getTransaction` 里，手续费支付者对某个代币的原始余额发生了变化。`slot`、`block_time`、`compute_unit_price` 缺了就是 NULL，不用入库时间冒充链上时间。余额对不上、或支付者是已知程序，就不插这一行。`token_transfer_edges.bot_reason` 只在同一笔签名被标上之后才写，否则保持 NULL。没解析出的转账不补边。

三条启发式都是尽力而为，失败就关闭：

- 同一 `slot`、同一钱包、同一代币、至少两笔不同签名：`same_slot_repeat`。`blockTime` 只有秒，没有毫秒字段就不编造毫秒间隔。同一 slot 是链上能看见的亚秒重复。没有 slot 就不打这条。
- 至少三笔，`block_time` 间隔完全相等且大于 0，整段落在最近 1 小时：`fixed_interval`。间隔不等、少一笔、或没有 `block_time`，就不打。
- 顶层最后一条 `SetComputeUnitPrice` 的 micro-lamports ≥ 1000000：`priority_fee`。指令不在、解析不了、或价格更低，就不打。不拿 `meta.fee` 反推竞价。

不额外打 RPC。日志正则命中的 mint 不打标。`BOT_MARKERS_ENABLED` 默认开，只在纸面观察；设为 `0` 则实时监听不解析、不打 `bot_suspect`。dev 标签、转账边、持仓快照仍按 I-1。

窗口份额的分子分母在 `bot_share_inputs`：被标上的已解析成交 / 已解析成交。分母为 0 时份额是 NULL。没解析到的成交不进分母，不当成人类。份额 0 也不是人类标签，更不是跳单。

`daily_check` 检查「INTEL_LOOP I-3 bot 标记」和小时报只计 `bot_suspect_n`、`bot_edge_n`。空是 PASS。查询失败或表不在是 WARN，不是 FAIL。这是 **10-19 旁证**，不算策略有效性，也不是跳单依据。执行层只读 `slippage_report` 仍不自动跳单。I-4（OTC）不在这次。

## 警示事例 → 采集侧

Polymarket 经验落地计划第四节的三条警示，映射到采集侧时只作旁证。**不算策略有效性。** 本文只记边界：docs-only，会话中不加参数。`ALERT_MIN_SCORE`、评分权重、现有 Telegram 路由、`history.extract_metrics` 的流动性写法保持原样。「热层流动性覆盖」那一节（#44）已经在跑，这里不另做监测。

| 警示 | 采集侧 |
|---|---|
| 半胜仍亏（约 51% 胜率仍大亏） | 仓位与入场（PM + `min_liq` + SL）。采集参数不动。**P-1** 不归采集。 |
| 单笔抹盈 / 薄池打穿（$0.95 黑天鹅，约 5% 抹掉约 95%；纸面 `stop_loss` 约 −84.33） | API 省略 `liquidity` 时 `liquidity_usd` 留 NULL，不编造。决策对缺失流动性以 `no_liquidity` fail-closed。#44 热层缺失率是 **10-19 周检旁证**。止损与仓位是 **P-2**，不归采集。**P-3** 采集只交「热层流动性覆盖」旁证，不拥有 `min_liq` 门槛。 |
| 洗盘 / 预言机单点 | 行情已是多源：DexScreener 主路径、Helius 链上线索、CCXT 旁路（`CCXT_SIDECHANNEL.md`）。周检人工核对留给人。**C1** 第二个 Telegram bot 保持 observe-only，不接入决策、不改现有推送路由。**P-4 / P-5** 不归采集。 |
| 未 enrich 的 `new_listing` | Helius 在 `tokens/v1` enrich 之前写入 `pending_enrich`，并可能发 `new_listing`（severity `info`，文案是 mint hint）。这条告警和未完成的 enrich 队列都不是可交易信号。流动性以 enrich 后的 `pair_history.liquidity_usd` 为准；payload 没有该字段则仍为 NULL。 |

归属：采集不拥有 **P-1 / P-2 / P-4 / P-5**。对 **P-3** 只提供「热层流动性覆盖」作为 10-19 旁证。以上全部标记为不算有效性。10-14 纸面旁证与文献边界见下一节。

## 学习/反思（10-14）

2026-10-14 HKT paper 的采集侧登记。**不算策略有效性。不盘中改参。** 不改运行参数、`ALERT_MIN_SCORE`、评分权重、现有 Telegram 路由、`history.extract_metrics` 的流动性写法、爬虫 loop。止损、日亏闸、仓位仍不归采集。

纸面旁证：权益约 $9683（相对 $10k 起点约 −3.2%），当时持仓已平；已平 43 笔；已实现约 −$317，主因 `stop_loss` ×3 约 −305（最差单笔约 −$190）；日亏闸已触发。回撤主因是止损打穿和日亏闸，不是采集饿死。当时供流正常（PID 约 650835，心跳正常，cycle 约 4400+）。看板把 `daily_loss` 从 risk 串拆开，见 [#60](https://github.com/tspike630/trade/pull/60)。供流正常不等于可以调参。

#44 热层：当日 hot 1h 流动性缺失约 32%，与上一节约 34% 同属 >25% WARN，仍记约 30%+。这仍是 **10-19 周检旁证**，不是改参依据。DexScreener 省略 `liquidity` 时 `liquidity_usd` 留 NULL，不编造；决策对缺失流动性仍以 `no_liquidity` fail-closed。`ALERT_MIN_SCORE` 与评分权重不动。

文献只观察，不接入。第二批评的是 LLM 交易代理、预测源或多智能体扇出：LiveTradeBench、When Agents Trade、Prophet Arena、FinPos、PredictionMarketBench。此前一批同样只观察：Profit Mirage、StockBench、MAS / adaptive BTC。采集不新增 LLM 预测源，不做 agent 扇出。行情保持规则快照：DexScreener 主路径、Helius 链上线索、CCXT 旁路（`CCXT_SIDECHANNEL.md`）。文献里的收益和代理排名不是本系统参数。

| 文献 | 采集侧 |
|---|---|
| [LiveTradeBench](https://arxiv.org/abs/2511.03628) | LLM 实盘组合分配评测。不把模型输出当新行情源。 |
| [When Agents Trade](https://arxiv.org/abs/2510.11695) | 多市场 agent 竞技场。不扇出交易 agent。 |
| [Prophet Arena](https://arxiv.org/abs/2510.17638) | LLM 对预测市场事件报概率。不接入预测源。 |
| [FinPos](https://arxiv.org/abs/2510.27251) | 持仓感知的交易 agent。仓位不归采集。 |
| [PredictionMarketBench](https://arxiv.org/abs/2602.00133) | 预测市场订单簿回放。不改采集 loop。 |
| [Profit Mirage](https://arxiv.org/abs/2510.07920)、[StockBench](https://arxiv.org/abs/2510.02209) | 回测收益会在知识窗口外消失；LLM 股票代理多数跑不赢买入持有。不把文献收益写成有效性。 |
| [MAS / adaptive BTC](https://arxiv.org/abs/2510.08068) | 多 agent 加口头反馈调 BTC。不接 Reflect agent，不改权重。 |

以上全部 **不算有效性**。不盘中改参。

## 警示事例 → 复盘侧（指针）

复盘拥有平仓归因分桶，以及 **P-2 / P-3** 的 10-19 旁证。全文在 `REVIEW_WARNING_CASES.md`（仓库 `docs/REVIEW_WARNING_CASES.md` 同文）。

- **P-2**（复盘 + 指标）：10-19 真库 IC/IR 首跑之后只写权重更新设计。权重保持 0，不激活。「警示事例 → 采集侧」表里「止损与仓位是 P-2」只表示采集不改止损；执行单上的 P-2 不是采集项。
- **P-3**：复盘记 `min_liq` 拒绝桶、cooldown 是否生效，以及平仓 `liq_<50k` vs `≥50k`。旁证，不是有效性。采集只交「热层流动性覆盖」那一节的覆盖率。
- 复盘不拥有 PM 仓位比例 / 回撤、JEV 错价论述、采集流动性覆盖。

不算有效性。此处不加参数。
