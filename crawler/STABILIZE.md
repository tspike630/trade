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
