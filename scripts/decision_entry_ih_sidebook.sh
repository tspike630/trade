#!/usr/bin/env bash
# Entry sidebook B1: selfbuilt_momentum_a_v1 buys with entry_ih_age_h >= 1.
# DECISION_ENTRY_IH_SIDEBOOK defaults to 0 and this process exits without
# starting a loop. It does not start the resident decision arm.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
FLAG="${DECISION_ENTRY_IH_SIDEBOOK:-0}"
if [[ "$FLAG" != "1" ]]; then
  echo "DECISION_ENTRY_IH_SIDEBOOK=${FLAG}; entry ih sidebook not started"
  exit 0
fi
PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then PY="$(command -v python3)"; fi
export PYTHONPATH=.
OUT="${DECISION_ENTRY_IH_OUTPUT_DB:-$ROOT/decision_entry_ih_ge1h.db}"
exec "$PY" -m decision_engine.run \
  --strategy selfbuilt_momentum_a_v1 \
  --dry-run \
  --interval 60 \
  --purpose shadow_entry_ih_ge1h \
  --limit 2000 \
  --token-cooldown-min 60 \
  --position-pct 0.05 \
  --entry-ih-min-hours 1.0 \
  --output-db "$OUT"
