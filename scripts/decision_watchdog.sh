#!/usr/bin/env bash
# 自建 A race_selfbuilt_a（平权旁簿；已废默认 live_candidate 主槽）
# 杨口令 SIZE_MULT=20≈全仓；废主簿特权；不开 live/#71
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
LOG="$ROOT/logs/decision_watchdog.log"
ROLE="$ROOT/logs/decision_engine.ROLE"
mkdir -p "$ROOT/logs"
PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then PY="$(command -v python3)"; fi
export PYTHONPATH=.
export DECISION_TECH_FRESH_SEC="${DECISION_TECH_FRESH_SEC:-86400}"
export DECISION_HOT_SEEN_WINDOW_SEC="${DECISION_HOT_SEEN_WINDOW_SEC:-86400}"

ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }

ensure_one() {
  local label="$1" pattern="$2" cmd="$3"
  if pgrep -f "$pattern" >/dev/null 2>&1; then return 0; fi
  echo "$(ts) RESTART $label via $PY" >>"$LOG"
  # shellcheck disable=SC2086
  nohup env PYTHONPATH=. DECISION_TECH_FRESH_SEC="$DECISION_TECH_FRESH_SEC" \
    DECISION_HOT_SEEN_WINDOW_SEC="$DECISION_HOT_SEEN_WINDOW_SEC" \
    "$PY" $cmd >>"$ROOT/logs/decision_${label}.log" 2>&1 &
  sleep 1
  local pid; pid="$(pgrep -f "$pattern" | head -1 || true)"
  echo "$(ts) RESTART $label pid=${pid:-failed}" >>"$LOG"
}

rewrite_role() {
  local main_pid
  main_pid="$(pgrep -f 'strategy selfbuilt_momentum_a_v1.*purpose race_selfbuilt_a' | head -1 || echo '?')"
  printf '%s\n' "${main_pid} selfbuilt_a race_selfbuilt_a size_pct=0.05 × SIZE_MULT=20 ≈全仓（平权；watchdog $(ts)）" >"$ROLE"
}

echo "$(ts) decision_watchdog start race_selfbuilt_a (no live_candidate) SIZE_MULT=20" >>"$LOG"
while true; do
  # PAUSED by Yang 2026-11-05: all paper emitters off; do not restart race_selfbuilt_a.
  if [[ "${DECISION_ENTRY_IH_SIDEBOOK:-0}" == "1" ]]; then
    ensure_one entry_ih_ge1h \
      'strategy selfbuilt_momentum_a_v1 .*--purpose shadow_entry_ih_ge1h' \
      "-m decision_engine.run --strategy selfbuilt_momentum_a_v1 --dry-run --interval 60 --purpose shadow_entry_ih_ge1h --limit 2000 --token-cooldown-min 60 --position-pct 0.05 --entry-ih-min-hours 1.0 --output-db $ROOT/decision_entry_ih_ge1h.db"
  fi
  rewrite_role
  sleep 120
done
