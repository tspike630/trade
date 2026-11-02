#!/usr/bin/env bash
# Start isolated paper peer loops: 5 race_* arms + shadow_entry_ih_ge1h.
# Does NOT touch paper_trading.db, execution_ledger.db, LIVE, or C-exit.
# Requires: execution.run --accept-purpose (peer purpose-filter PR).
# SIZE_MULT / live gates / .env trading params are NOT modified here.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -e LIVE_TRADING_DISABLED ]]; then
  echo "[paper_peers] LIVE_TRADING_DISABLED present — paper peers only (OK)"
else
  echo "[paper_peers] WARN: LIVE_TRADING_DISABLED missing — still paper-only; do NOT open live" >&2
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
  echo "[paper_peers] WARN: .venv missing, fallback $PY" >&2
fi

mkdir -p logs

# purpose | paper_eq db | isolated execution ledger | optional decision_db (empty = main dexscreener.db)
PEERS=(
  "race_selfbuilt_a|paper_eq_selfbuilt_a.db|execution_ledger_race_selfbuilt_a.db|"
  "race_rsi_bounce|paper_eq_rsi_bounce.db|execution_ledger_race_rsi_bounce.db|"
  "race_macd_cross|paper_eq_macd_cross.db|execution_ledger_race_macd_cross.db|"
  "race_boll_breakout|paper_eq_boll_breakout.db|execution_ledger_race_boll_breakout.db|"
  "race_tech_mom|paper_eq_tech_mom.db|execution_ledger_race_tech_mom.db|"
  "shadow_entry_ih_ge1h|paper_eq_entry_ih1h.db|execution_ledger_shadow_entry_ih1h.db|decision_entry_ih_ge1h.db"
)

is_peer_running() {
  local purpose="$1"
  local paper_db="$2"
  pgrep -f "execution.run .*--accept-purpose ${purpose}" >/dev/null 2>&1 \
    || pgrep -f "execution.run .*--paper-db ${paper_db}" >/dev/null 2>&1
}

for row in "${PEERS[@]}"; do
  IFS='|' read -r purpose paper_db ledger_db decision_db <<<"$row"
  log="execution_peer_${purpose}.log"
  if [[ ! -f "$paper_db" ]]; then
    echo "[paper_peers] SKIP $purpose: missing $paper_db (run scripts/init_multi_paper_peers.sh)" >&2
    continue
  fi
  if [[ -n "$decision_db" && ! -f "$decision_db" ]]; then
    echo "[paper_peers] SKIP $purpose: missing decision db $decision_db" >&2
    continue
  fi
  if is_peer_running "$purpose" "$paper_db"; then
    echo "[paper_peers] already running: $purpose paper=$paper_db"
    continue
  fi

  extra_env=()
  extra_args=()
  if [[ -n "$decision_db" ]]; then
    # Sidebook decision DB (not dexscreener.db); peer_mode skips shadow-row content check.
    extra_env+=(EXECUTION_ALLOW_NONSTANDARD_DECISION_DB=1)
    extra_args+=(--db "$decision_db")
  fi

  # shellcheck disable=SC2086
  nohup env "${extra_env[@]}" "$PY" -m execution.run --loop --paper-enabled --paper-tick \
    --paper-db "$paper_db" \
    --ledger "$ledger_db" \
    --accept-purpose "$purpose" \
    "${extra_args[@]}" \
    >>"logs/$log" 2>&1 &
  pid=$!
  sleep 0.4
  if kill -0 "$pid" 2>/dev/null; then
    echo "[paper_peers] started: purpose=$purpose pid=$pid paper=$paper_db ledger=$ledger_db decision_db=${decision_db:-dexscreener.db} log=logs/$log"
  else
    echo "[paper_peers] FAILED: purpose=$purpose (see logs/$log)" >&2
  fi
done

echo "[paper_peers] done. Main live_candidate loop (paper_trading.db / execution_ledger.db) left untouched."
