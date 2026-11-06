#!/usr/bin/env bash
# CEX side-book scaffold for purpose live_bnb_boll_fade (Binance spot USDT).
#
# DEFAULT: dry status only — does NOT start execution.run, does NOT place orders,
# does NOT touch LIVE_TRADING_DISABLED / SIZE_MULT / paper_eq_*.
#
# Review ③ NOT passed → keep DISABLED. Yang keys in 1:1 + review ③ green
# required before any ENABLE_* start. Even then this script only starts a
# paper-enabled accept-purpose peer (gateway Never calls LiveExecutor);
# real CEX fills need a separate Binance adapter + live gates.
#
# Usage:
#   ./scripts/start_live_bnb_boll_fade_cex_sidebook.sh           # dry report
#   ENABLE_LIVE_BNB_BOLL_FADE_PEER=1 ./scripts/...              # paper peer only
# Never: remove LIVE_TRADING_DISABLED from this script.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PURPOSE="live_bnb_boll_fade"
BOOK="live_bnb_boll_fade.db"
LEDGER="execution_ledger_live_bnb_boll_fade.db"
LOG="execution_peer_${PURPOSE}.log"

echo "[live_bnb_cex] purpose=${PURPOSE}"
echo "[live_bnb_cex] book=${BOOK} ledger=${LEDGER}"
echo "[live_bnb_cex] SIZE_MULT policy=20 (do not edit .env here)"

if [[ -e LIVE_TRADING_DISABLED ]]; then
  echo "[live_bnb_cex] LIVE_TRADING_DISABLED present — kill switch ON (required)"
else
  echo "[live_bnb_cex] WARN: LIVE_TRADING_DISABLED missing — refuse start; recreate kill switch" >&2
  exit 2
fi

if [[ ! -f "$BOOK" || ! -f "$LEDGER" ]]; then
  echo "[live_bnb_cex] missing book/ledger — run scaffold init first" >&2
  exit 3
fi

would_cmd=(
  .venv/bin/python -m execution.run --loop --paper-enabled --paper-tick
  --paper-db "$BOOK"
  --ledger "$LEDGER"
  --accept-purpose "$PURPOSE"
)

echo "[live_bnb_cex] would_start (DISABLED by default):"
printf '  %q' "${would_cmd[@]}"
echo

# Hard refuse any live trading enablement from this launcher.
if [[ "${LIVE_TRADING_ENABLED:-0}" == "1" ]]; then
  echo "[live_bnb_cex] REFUSE: LIVE_TRADING_ENABLED=1 — unset it; this launcher is paper-peer scaffold only" >&2
  exit 4
fi

if [[ "${ENABLE_LIVE_BNB_BOLL_FADE_PEER:-0}" != "1" ]]; then
  echo "[live_bnb_cex] DISABLED — set ENABLE_LIVE_BNB_BOLL_FADE_PEER=1 only after review ③ + Yang 1:1 keys (still paper fills until Binance gateway adapter)."
  echo "[live_bnb_cex] no process started"
  exit 0
fi

# Gated paper peer start (still cannot hit exchange: gateway → PaperExecutor only).
if [[ ! -x .venv/bin/python ]]; then
  echo "[live_bnb_cex] .venv/bin/python missing" >&2
  exit 5
fi
if pgrep -f "execution.run .*--accept-purpose ${PURPOSE}" >/dev/null 2>&1; then
  echo "[live_bnb_cex] already running accept-purpose ${PURPOSE}"
  exit 0
fi

mkdir -p logs
# shellcheck disable=SC2086
nohup env -u EXECUTION_PAPER_DB -u PAPER_DB_PATH -u EXECUTION_LEDGER_PATH -u EXECUTION_LEDGER_DB \
  .venv/bin/python -m execution.run --loop --paper-enabled --paper-tick \
  --paper-db "$BOOK" \
  --ledger "$LEDGER" \
  --accept-purpose "$PURPOSE" \
  >>"logs/$LOG" 2>&1 &
pid=$!
sleep 0.4
if kill -0 "$pid" 2>/dev/null; then
  echo "[live_bnb_cex] started PAPER peer only: purpose=${PURPOSE} pid=${pid} log=logs/${LOG}"
  echo "[live_bnb_cex] NOTE: gateway Never calls LiveExecutor; CEX orders still blocked"
else
  echo "[live_bnb_cex] FAILED (see logs/${LOG})" >&2
  exit 6
fi
