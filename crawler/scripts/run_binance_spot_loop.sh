#!/usr/bin/env bash
set -euo pipefail
cd /workspace/dexscreener-crawler
export BINANCE_SPOT_UNIVERSE_DB=/workspace/dexscreener-crawler/exports/binance_spot_universe.db
export BINANCE_SPOT_UNIVERSE_JSON=/workspace/dexscreener-crawler/exports/reports/binance_spot_usdt_trading_2026-11-05.json
LOG=/workspace/agent-tools/bnb_kline_loop.log
INTERVAL_SEC="${BNB_KLINE_LOOP_SEC:-300}"
while true; do
  echo "==== $(date -Iseconds) refresh start ====" >>"$LOG"
  .venv/bin/python -m indicators.binance_spot --db dexscreener.db --pause 0.05 >>"$LOG" 2>&1 || echo "==== $(date -Iseconds) refresh FAILED rc=$? ====" >>"$LOG"
  echo "==== $(date -Iseconds) refresh done ====" >>"$LOG"
  sleep "$INTERVAL_SEC"
done
