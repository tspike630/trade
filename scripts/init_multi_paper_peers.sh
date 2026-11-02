#!/usr/bin/env bash
# Init / verify peer PaperLedger DBs for multi-paper queue. Does NOT start loops.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${ROOT}/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"
"$PY" - <<'PY'
from pathlib import Path
from paper_trade import PaperLedger
ROOT = Path(".").resolve()
START = 10000.0
DBS = [
    "paper_eq_selfbuilt_a.db",
    "paper_eq_entry_ih1h.db",
    "paper_eq_rsi_bounce.db",
    "paper_eq_macd_cross.db",
    "paper_eq_boll_breakout.db",
    "paper_eq_tech_mom.db",
    "paper_eq_jev_timeexit.db",
    "paper_eq_jev_trail.db",
    "paper_eq_jev_rsi_and.db",
    "paper_eq_combined_jev_tech.db",
    "paper_eq_rsi_macd_and.db",
    "paper_eq_macd_boll_and.db",
    "paper_eq_rsi_boll_and.db",
    "paper_eq_tech_macd_and.db",
]
for name in DBS:
    p = ROOT / name
    existed = p.exists()
    with PaperLedger(p) as L:
        n = L.conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        if n == 0:
            L.set_cash(START)
        print(f"{'ok_exist' if existed else 'created':10s} {name} cash={L.get_cash()} positions={n}")
print("C-exit book left untouched: paper_trading_c_exit.db")
print("No execution loops started.")
PY
