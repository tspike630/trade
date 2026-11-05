#!/usr/bin/env bash
# Production supervisor for the single live_bnb_boll_fade execution loop.
# Does not delete LIVE_TRADING_DISABLED, does not set BINANCE_SPOT_LIVE_ENABLED,
# and does not print or rewrite API keys. Preflight refuses until the operator
# has already done those steps and supplied a key plus BINANCE_SPOT_BOOK_USDT.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="${PYTHONPATH:-.}"
PY="${ROOT}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python3)"
fi
exec "$PY" -m execution.live_bnb_watchdog --supervise
