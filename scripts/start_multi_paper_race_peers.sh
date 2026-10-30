#!/usr/bin/env bash
# Start isolated paper peer loops for the 5 race_* arms (flat PaperLedger).
# Does NOT touch paper_trading.db, execution_ledger.db, LIVE, or C-exit.
# Requires: execution.run --accept-purpose (peer purpose-filter PR).
# SIZE_MULT / live gates / .env trading params are NOT modified here.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -e LIVE_TRADING_DISABLED ]]; then
  echo "[race_peers] LIVE_TRADING_DISABLED present — paper peers only (OK)"
else
  echo "[race_peers] WARN: LIVE_TRADING_DISABLED missing — still paper-only; do NOT open live" >&2
fi

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# Never inherit main book paths into peer processes.
unset EXECUTION_PAPER_DB PAPER_DB_PATH EXECUTION_LEDGER_PATH EXECUTION_LEDGER_DB
unset EXECUTION_ACCEPT_PURPOSE

if [[ -x .venv/bin/python ]]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="$(command -v python3)"
  echo "[race_peers] WARN: .venv missing, fallback $PY" >&2
fi

mkdir -p logs

# purpose | paper_eq db | isolated execution ledger
PEERS=(
  "race_selfbuilt_a|paper_eq_selfbuilt_a.db|execution_ledger_race_selfbuilt_a.db"
  "race_rsi_bounce|paper_eq_rsi_bounce.db|execution_ledger_race_rsi_bounce.db"
  "race_macd_cross|paper_eq_macd_cross.db|execution_ledger_race_macd_cross.db"
  "race_boll_breakout|paper_eq_boll_breakout.db|execution_ledger_race_boll_breakout.db"
  "race_tech_mom|paper_eq_tech_mom.db|execution_ledger_race_tech_mom.db"
)

is_peer_running() {
  local purpose="$1"
  local paper_db="$2"
  pgrep -f "execution.run .*--accept-purpose ${purpose}" >/dev/null 2>&1 \
    || pgrep -f "execution.run .*--paper-db ${paper_db}" >/dev/null 2>&1
}

for row in "${PEERS[@]}"; do
  IFS='|' read -r purpose paper_db ledger_db <<<"$row"
  log="execution_peer_${purpose}.log"
  if [[ ! -f "$paper_db" ]]; then
    echo "[race_peers] SKIP $purpose: missing $paper_db (run scripts/init_multi_paper_peers.sh)" >&2
    continue
  fi
  if is_peer_running "$purpose" "$paper_db"; then
    echo "[race_peers] already running: $purpose paper=$paper_db"
    continue
  fi
  # shellcheck disable=SC2086
  nohup "$PY" -m execution.run --loop --paper-enabled --paper-tick \
    --paper-db "$paper_db" \
    --ledger "$ledger_db" \
    --accept-purpose "$purpose" \
    >>"logs/$log" 2>&1 &
  pid=$!
  sleep 0.4
  if kill -0 "$pid" 2>/dev/null; then
    echo "[race_peers] started: purpose=$purpose pid=$pid paper=$paper_db ledger=$ledger_db log=logs/$log"
  else
    echo "[race_peers] FAILED: purpose=$purpose (see logs/$log)" >&2
  fi
done

echo "[race_peers] done. Main live_candidate loop (paper_trading.db / execution_ledger.db) left untouched."
