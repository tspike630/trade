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

A ~34% missing rate is WARN. The rate is **10-19 周检旁证** and does not count toward strategy validity. The hourly report (`scripts/grok_report.py` → context markdown) prints one line under Health: hot 1h missing rate and `pending_enrich`. Do not change `ALERT_MIN_SCORE` or score weights from this metric. How the warning cases use this check is in the next section.

## 警示事例 → 采集侧

Polymarket 经验落地计划第四节的三条警示，映射到采集侧时只作旁证。**不算策略有效性。** 本文只记边界：docs-only，会话中不加参数。`ALERT_MIN_SCORE`、评分权重、现有 Telegram 路由、`history.extract_metrics` 的流动性写法保持原样。上一节的热层覆盖（#44）已经在跑，这里不另做监测。

| 警示 | 采集侧 |
|---|---|
| 半胜仍亏（约 51% 胜率仍大亏） | 仓位与入场（PM + `min_liq` + SL）。采集参数不动。**P-1** 不归采集。 |
| 单笔抹盈 / 薄池打穿（$0.95 黑天鹅，约 5% 抹掉约 95%；纸面 `stop_loss` 约 −84.33） | API 省略 `liquidity` 时 `liquidity_usd` 留 NULL，不编造。决策对缺失流动性以 `no_liquidity` fail-closed。#44 热层缺失率是 **10-19 周检旁证**。止损与仓位是 **P-2**，不归采集。**P-3** 采集只交上一节的流动性覆盖旁证，不拥有 `min_liq` 门槛。 |
| 洗盘 / 预言机单点 | 行情已是多源：DexScreener 主路径、Helius 链上线索、CCXT 旁路（`CCXT_SIDECHANNEL.md`）。周检人工核对留给人。**C1** 第二个 Telegram bot 保持 observe-only，不接入决策、不改现有推送路由。**P-4 / P-5** 不归采集。 |
| 未 enrich 的 `new_listing` | Helius 在 `tokens/v1` enrich 之前写入 `pending_enrich`，并可能发 `new_listing`（severity `info`，文案是 mint hint）。这条告警和未完成的 enrich 队列都不是可交易信号。流动性以 enrich 后的 `pair_history.liquidity_usd` 为准；payload 没有该字段则仍为 NULL。 |

归属：采集不拥有 **P-1 / P-2 / P-4 / P-5**。对 **P-3** 只提供上一节的热层流动性覆盖作为 10-19 旁证。以上全部标记为不算有效性。
