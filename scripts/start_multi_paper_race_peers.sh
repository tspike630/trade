#!/usr/bin/env bash
# Start isolated paper peer loops: race_* arms + shadow_entry_ih_ge1h.
# Does NOT touch paper_trading.db, execution_ledger.db, LIVE, or C-exit.
# Requires: execution.run --accept-purpose (peer purpose-filter PR).
# SIZE_MULT / live gates / .env trading params are NOT modified here.
#
# Exit-synth env (paper_executor.tp_sl_hold_from_signal):
#   EXECUTION_PAPER_TP_PCT / EXECUTION_PAPER_SL_PCT / EXECUTION_PAPER_MAX_HOLD_MIN
#   Signal/extras/raw explicit values still win; env only fills omissions.
#   Purpose → MAX_HOLD (TP/SL stay DEFAULT 10/5 unless purpose extras decide):
#     *timeexit* / *a_timeexit_1h* / *jev_timeexit* / *rsi_macd_timeexit* → 60
#     *timestop_48h* → 2880
#     *tier_72h7d* → 4320
#     *trail* → keep 60 + log FAKE_TRAIL_UNTIL_COLUMN (main paper has no trail col)
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
# Peer exit-synth must be set per purpose below — do not inherit a global default.
unset EXECUTION_PAPER_TP_PCT EXECUTION_PAPER_SL_PCT EXECUTION_PAPER_MAX_HOLD_MIN

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

# Map purpose → EXECUTION_PAPER_MAX_HOLD_MIN (+ trail fake note). TP/SL documented as 10/5.
peer_exit_synth_env() {
  local purpose="$1"
  local hold="" note=""
  case "$purpose" in
    *timestop_48h*) hold="2880"; note="timestop_48h" ;;
    *tier_72h7d*) hold="4320"; note="tier_72h7d" ;;
    *trail*)
      hold="60"
      note="FAKE_TRAIL_UNTIL_COLUMN"
      ;;
    *timeexit*|*a_timeexit_1h*|*jev_timeexit*|*rsi_macd_timeexit*)
      hold="60"
      note="timeexit_1h"
      ;;
  esac
  if [[ -z "$hold" ]]; then
    return 0
  fi
  printf '%s\n' "EXECUTION_PAPER_MAX_HOLD_MIN=${hold}"
  # Documented defaults (export so peer logs/env dump show intent; same as hardcoded).
  printf '%s\n' "EXECUTION_PAPER_TP_PCT=10"
  printf '%s\n' "EXECUTION_PAPER_SL_PCT=5"
  echo "[paper_peers] exit_synth purpose=${purpose} MAX_HOLD=${hold} TP=10 SL=5 note=${note}" >&2
  if [[ "$note" == "FAKE_TRAIL_UNTIL_COLUMN" ]]; then
    echo "[paper_peers] FAKE_TRAIL_UNTIL_COLUMN purpose=${purpose} (main paper has no trail column)" >&2
  fi
}

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
  while IFS= read -r kv; do
    [[ -n "$kv" ]] || continue
    extra_env+=("$kv")
  done < <(peer_exit_synth_env "$purpose")

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
