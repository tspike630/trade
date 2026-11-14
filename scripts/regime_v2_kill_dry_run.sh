#!/usr/bin/env bash
# Isolated regime v2 dry-run. Requires the kill file and a private --output-db.
# Does not start execution, does not source .env, and does not call Binance.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROFILE="${ROOT}/configs/live_regime_v2.env"
KILL_FILE="${REGIME_V2_KILL_FILE:-${ROOT}/LIVE_TRADING_DISABLED}"
OUTPUT_DB=""
SNAPSHOT=""
PURPOSE="live_bnb_boll_fade"

usage() {
  echo "usage: $0 --output-db /tmp/.../decision.db [--kill-file PATH] [--feature-snapshot PATH]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-db)
      OUTPUT_DB="${2:-}"
      shift 2
      ;;
    --kill-file)
      KILL_FILE="${2:-}"
      shift 2
      ;;
    --feature-snapshot)
      SNAPSHOT="${2:-}"
      shift 2
      ;;
    --purpose)
      PURPOSE="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "REFUSE: unknown argument $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ -z "$OUTPUT_DB" ]]; then
  echo "REFUSE: --output-db is required" >&2
  exit 2
fi
if [[ ! -f "$KILL_FILE" ]]; then
  echo "REFUSE: kill file missing ($KILL_FILE); will not start a decision run" >&2
  exit 4
fi
case "$OUTPUT_DB" in
  /tmp/*|/var/tmp/*) ;;
  *)
    echo "REFUSE: --output-db must be under /tmp or /var/tmp" >&2
    exit 2
    ;;
esac
base="$(basename "$OUTPUT_DB")"
case "$base" in
  dexscreener.db|execution_ledger_live_bnb_boll_fade.db|execution_ledger_live_bnb_boll_classic.db|news_events.db|public_derivatives.db|lseg_macro.db)
    echo "REFUSE: --output-db basename is a production store" >&2
    exit 2
    ;;
esac

ISOLATED="$(cd "$(dirname "$OUTPUT_DB")" && pwd)"
SOURCE_DB="${ISOLATED}/indicator_source.db"
REPORT="${ISOLATED}/regime_v2_acceptance.json"
mkdir -p "$ISOLATED"
if [[ ! -f "$SOURCE_DB" ]]; then
  python3 - "$SOURCE_DB" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("CREATE TABLE IF NOT EXISTS isolation_marker (note TEXT)")
conn.commit()
conn.close()
PY
fi

set -a
# shellcheck disable=SC1090
source "$PROFILE"
set +a

# Isolation overrides. The profile names the production ledgers; this run
# does not open them. dex_map avoids a Binance request from an empty source.
export DECISION_EXECUTION_LEDGER="${ISOLATED}/execution_ledger_${PURPOSE}.db"
export LIVE_BNB_FACTOR_SOURCE=dex_map
unset BINANCE_API_KEY BINANCE_API_SECRET BINANCE_API_KEY_CURRENT BINANCE_API_SECRET_CURRENT || true
unset BINANCE_API_KEY_NEW BINANCE_API_SECRET_NEW || true

cd "$ROOT"
set +e
python3 -m decision_engine.run \
  --strategy tech_boll_fade_v1 \
  --purpose "$PURPOSE" \
  --dry-run \
  --once \
  --regime-v2-isolated-purpose \
  --position-pct "$REGIME_V2_POSITION_PCT" \
  --db "$SOURCE_DB" \
  --output-db "$OUTPUT_DB"
engine_status=$?
set -e

snapshot_args=()
if [[ -n "$SNAPSHOT" ]]; then
  snapshot_args=(--feature-snapshot "$SNAPSHOT")
fi

set +e
python3 -m decision_engine.core.regime_v2_acceptance \
  --profile "$PROFILE" \
  --root "$ROOT" \
  --output-db "$OUTPUT_DB" \
  --source-db "$SOURCE_DB" \
  --isolated-dir "$ISOLATED" \
  --kill-file "$KILL_FILE" \
  --engine-exit-code "$engine_status" \
  --report "$REPORT" \
  "${snapshot_args[@]}"
report_status=$?
set -e

echo "report=$REPORT engine_exit=$engine_status cutover_exit=$report_status"
exit "$report_status"
