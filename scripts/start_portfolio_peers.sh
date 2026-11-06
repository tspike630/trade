#!/usr/bin/env bash
# Official portfolio peers: portfolio_risk_v1 + portfolio_tight_sl_v1 (群钉).
# and_blend = 合臂拼盘旁证. Isolated race peers untouched. SIZE_MULT=7 MAX_OPEN=3.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
PY="${ROOT}/.venv/bin/python"; [[ -x "$PY" ]] || PY="$(command -v python3)"
mkdir -p logs
start_one() {
  local purpose="$1" paper="$2" ledger="$3"
  if pgrep -f "execution.run .*--accept-purpose ${purpose}( |$)" >/dev/null 2>&1; then
    echo "[portfolio] already $purpose"; return 0
  fi
  nohup env EXECUTION_PAPER_SIZE_MULT=7 EXECUTION_MAX_OPEN=3 EXECUTION_RISK_ENABLED=1 \
    PM_ENABLED=1 PM_TOKEN_DEDUP=1 PM_MAX_OPEN_PER_STRATEGY=3 PM_MAX_TOTAL_PCT=80 \
    "$PY" -m execution.run --loop --paper-enabled --paper-tick --pm-enabled \
    --paper-db "$paper" --ledger "$ledger" --accept-purpose "$purpose" \
    >>"logs/execution_peer_${purpose}.log" 2>&1 &
  echo "[portfolio] started $purpose pid=$!"; sleep 0.4
}
# PAUSED paper-focus Yang 2026-11-05 keep only race_tech_boll_fade — do not auto-restart: start_one race_portfolio_risk paper_eq_portfolio_risk.db execution_ledger_race_portfolio_risk.db
# PAUSED paper-focus Yang 2026-11-05 keep only race_tech_boll_fade — do not auto-restart: start_one race_portfolio_tight_sl paper_eq_portfolio_tight_sl.db execution_ledger_race_portfolio_tight_sl.db
# PAUSED paper-focus Yang 2026-11-05 keep only race_tech_boll_fade — do not auto-restart: start_one race_portfolio_and_blend paper_eq_portfolio_and_blend.db execution_ledger_race_portfolio_and_blend.db
echo "[portfolio] done."
