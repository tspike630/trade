#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate
while true; do
  python paper_trade.py tick 2>&1 | tail -5 || true
  sleep 300
done
