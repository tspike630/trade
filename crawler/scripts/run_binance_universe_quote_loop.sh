#!/usr/bin/env bash
# Periodic public Binance spot quoteVolume refresh.
# Not started by run_binance_spot_loop.sh. Not installed by this repository.
# Do not start it until the operator retires refresh_binance_spot_universe.py
# on the same database. That script is not in this repo; running it again
# DROP/CREATEs symbols and removes quoteVolume_collected_at.
#
# One writer, one authoritative db. Mirrors are byte copies of that commit
# (SQLite backup + rename), not a second ticker fetch.
#
# Production layout (dexscreener-crawler is the work root, .venv is beside it):
#   bash scripts/run_binance_universe_quote_loop.sh \
#     --work-root /workspace/dexscreener-crawler \
#     --python /workspace/dexscreener-crawler/.venv/bin/python \
#     --db /workspace/dexscreener-crawler/exports/binance_spot_universe.db \
#     --json /workspace/dexscreener-crawler/exports/reports/binance_spot_usdt_trading_2026-11-05.json \
#     --mirror /workspace/dexscreener-crawler/crawler/exports/binance_spot_universe.db \
#     --mirror /workspace/exports/binance_spot_universe.db
# --dry-run prints the resolved command and exits. It does not fetch or loop.
set -euo pipefail

WORK_ROOT=""
PYTHON=""
DB=""
JSON=""
INTERVAL_SEC="${BINANCE_UNIVERSE_QUOTE_INTERVAL_SEC:-300}"
FRESH_SEC="${BINANCE_INDICATORS_FRESH_SEC:-3600}"
LOG="${BINANCE_UNIVERSE_QUOTE_LOG:-}"
DRY_RUN=0
MIRRORS=()

usage() {
  echo "usage: $0 --work-root DIR --python PATH --db PATH [--json PATH] [--mirror PATH ...] [--interval SEC] [--dry-run]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --work-root) WORK_ROOT="${2:-}"; shift 2 ;;
    --python) PYTHON="${2:-}"; shift 2 ;;
    --db) DB="${2:-}"; shift 2 ;;
    --json) JSON="${2:-}"; shift 2 ;;
    --mirror) MIRRORS+=("${2:-}"); shift 2 ;;
    --interval) INTERVAL_SEC="${2:-}"; shift 2 ;;
    --log) LOG="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ -z "$WORK_ROOT" || -z "$PYTHON" || -z "$DB" ]]; then
  echo "universe quote loop: --work-root, --python, and --db are required" >&2
  usage
  exit 2
fi
if ! [[ "$INTERVAL_SEC" =~ ^[0-9]+$ && "$FRESH_SEC" =~ ^[0-9]+$ ]]; then
  echo "universe quote loop: interval and freshness must be whole seconds" >&2
  exit 2
fi
if (( INTERVAL_SEC <= 0 || INTERVAL_SEC >= FRESH_SEC )); then
  echo "universe quote loop: interval ${INTERVAL_SEC}s must be >0 and < freshness ${FRESH_SEC}s" >&2
  exit 2
fi

if [[ -z "$LOG" ]]; then
  LOG="$WORK_ROOT/logs/binance_universe_quote.log"
fi

CMD=("$PYTHON" -m indicators.binance_universe_quote --db "$DB")
if [[ -n "$JSON" ]]; then
  CMD+=(--json "$JSON")
fi
for mirror in "${MIRRORS[@]}"; do
  CMD+=(--mirror "$mirror")
done

echo "work_root=${WORK_ROOT}"
echo "python=${PYTHON}"
echo "db=${DB}"
echo "json=${JSON}"
echo "interval=${INTERVAL_SEC}"
echo "fresh=${FRESH_SEC}"
for mirror in "${MIRRORS[@]}"; do
  echo "mirror=${mirror}"
done
printf 'command='
printf '%q ' "${CMD[@]}"
echo

if (( DRY_RUN == 1 )); then
  exit 0
fi

cd "$WORK_ROOT"
mkdir -p "$(dirname "$LOG")"
echo "universe quote loop work_root=${WORK_ROOT} db=${DB} interval=${INTERVAL_SEC}s" >>"$LOG"

while true; do
  echo "==== $(date -Iseconds) quote refresh start ====" >>"$LOG"
  if "${CMD[@]}" >>"$LOG" 2>&1; then
    echo "==== $(date -Iseconds) quote refresh ok ====" >>"$LOG"
  else
    echo "==== $(date -Iseconds) quote refresh FAILED rc=$? (previous snapshot left unchanged) ====" >>"$LOG"
  fi
  sleep "$INTERVAL_SEC"
done
