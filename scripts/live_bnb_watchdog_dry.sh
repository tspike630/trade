#!/usr/bin/env bash
# Fail-closed dry wrapper. This file cannot supervise and cannot pass a
# mode flag through. It only prints the preflight and exits.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="${PYTHONPATH:-.}"
PY="${ROOT}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python3)"
fi
exec "$PY" -m execution.live_bnb_watchdog --dry-run
