#!/usr/bin/env bash
# Periodic Binance spot quoteVolume refresh. Not started by
# run_binance_spot_loop.sh and not installed by this repository.
#
# Production today runs refresh_binance_spot_universe.py, which is not in
# this repo. That job must be stopped on this database before this loop
# starts. If it keeps rewriting symbols without quoteVolume_collected_at,
# the decision reader fail-closes every new buy.
#
# Interval must stay shorter than BINANCE_INDICATORS_FRESH_SEC (default
# 3600). One manual run is not enough: after the freshness window the
# reader rejects the whole book.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DB="${BINANCE_SPOT_UNIVERSE_DB:-$ROOT/exports/binance_spot_universe.db}"
INTERVAL_SEC="${BINANCE_UNIVERSE_QUOTE_INTERVAL_SEC:-300}"
FRESH_SEC="${BINANCE_INDICATORS_FRESH_SEC:-3600}"
LOG="${BINANCE_UNIVERSE_QUOTE_LOG:-$ROOT/logs/binance_universe_quote.log}"

if ! [[ "$INTERVAL_SEC" =~ ^[0-9]+$ && "$FRESH_SEC" =~ ^[0-9]+$ ]]; then
  echo "universe quote loop: interval and freshness must be whole seconds" >&2
  exit 2
fi
if (( INTERVAL_SEC <= 0 || INTERVAL_SEC >= FRESH_SEC )); then
  echo "universe quote loop: interval ${INTERVAL_SEC}s must be >0 and < freshness ${FRESH_SEC}s" >&2
  exit 2
fi

mkdir -p "$(dirname "$LOG")"
echo "universe quote loop db=${DB} interval=${INTERVAL_SEC}s fresh=${FRESH_SEC}s" >>"$LOG"

while true; do
  echo "==== $(date -Iseconds) quote refresh start ====" >>"$LOG"
  if .venv/bin/python -m indicators.binance_universe_quote --db "$DB" >>"$LOG" 2>&1; then
    echo "==== $(date -Iseconds) quote refresh ok ====" >>"$LOG"
  else
    echo "==== $(date -Iseconds) quote refresh FAILED rc=$? (previous snapshot left unchanged) ====" >>"$LOG"
  fi
  sleep "$INTERVAL_SEC"
done
