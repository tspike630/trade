#!/usr/bin/env bash
# One-shot Top query via GET /indicators/top (api_server).
#
# Usage:
#   ./top.sh local            # sort=local_score
#   ./top.sh change5m         # sort=change_5m
#   ./top.sh volume           # sort=volume_strength
#   ./top.sh local solana 10  # optional: chain limit
#   ./scripts/top.sh local
#
# Env (read from process env, else project .env — never printed):
#   API_SECRET   → X-API-Key header (required when api_server auth is on)
#   API_HOST     default 127.0.0.1
#   API_PORT     default 8080
#
# Defaults: chain=solana, limit=20
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

_usage() {
  cat <<'USAGE'
Usage: ./top.sh <local|change5m|volume> [chain] [limit]

  local     → GET /indicators/top?sort=local_score
  change5m  → GET /indicators/top?sort=change_5m
  volume    → GET /indicators/top?sort=volume_strength

jev_strength is not a sort key. Defaults: chain=solana, limit=20
Auth: X-API-Key from API_SECRET (env or .env). Key is never printed.
Host: http://${API_HOST:-127.0.0.1}:${API_PORT:-8080}

Examples:
  ./top.sh local
  ./top.sh change5m solana 20
  ./top.sh volume bsc 10
USAGE
}

# Load selected keys from .env into the environment if not already set.
# Does not echo values.
_load_dotenv_keys() {
  local f="$ROOT/.env"
  [[ -f "$f" ]] || return 0
  local line key val
  while IFS= read -r line || [[ -n "$line" ]]; do
    # trim CR
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    [[ "$line" == *"="* ]] || continue
    key="${line%%=*}"
    val="${line#*=}"
    # trim key whitespace
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    case "$key" in
      API_SECRET|API_PORT|API_HOST|ALLOW_INSECURE_API) ;;
      *) continue ;;
    esac
    # already set in environment wins (matches python-dotenv override=False)
    if [[ -n "${!key+x}" ]]; then
      continue
    fi
    # strip surrounding quotes
    if [[ "$val" =~ ^\".*\"$ ]]; then
      val="${val:1:-1}"
    elif [[ "$val" =~ ^\'.*\'$ ]]; then
      val="${val:1:-1}"
    fi
    export "$key=$val"
  done < "$f"
}

_load_dotenv_keys

MODE="${1:-}"
CHAIN="${2:-solana}"
LIMIT="${3:-20}"

if [[ -z "$MODE" || "$MODE" == "-h" || "$MODE" == "--help" ]]; then
  _usage
  [[ -z "$MODE" ]] && exit 1
  exit 0
fi

case "$MODE" in
  local)     SORT="local_score" ;;
  change5m)  SORT="change_5m" ;;
  volume)    SORT="volume_strength" ;;
  jev)
    echo "error: jev_strength sort is retired; use local, change5m, or volume" >&2
    exit 2
    ;;
  *)
    echo "error: unknown mode '$MODE' (expected local|change5m|volume)" >&2
    _usage >&2
    exit 2
    ;;
esac

if ! [[ "$LIMIT" =~ ^[0-9]+$ ]] || (( LIMIT < 1 || LIMIT > 100 )); then
  echo "error: limit must be an integer 1–100 (got '$LIMIT')" >&2
  exit 2
fi

HOST="${API_HOST:-127.0.0.1}"
PORT="${API_PORT:-8080}"
BASE="http://${HOST}:${PORT}"
URL="${BASE}/indicators/top?chain=$(printf '%s' "$CHAIN" | sed 's/ /%20/g')&sort=${SORT}&limit=${LIMIT}"

CURL_ARGS=(-sS)
if [[ -n "${API_SECRET:-}" ]]; then
  CURL_ARGS+=(-H "X-API-Key: ${API_SECRET}")
fi

# stderr: mode summary only (no secrets)
echo "top mode=${MODE} sort=${SORT} chain=${CHAIN} limit=${LIMIT} url=${BASE}/indicators/top" >&2

HTTP_CODE=0
BODY="$(curl "${CURL_ARGS[@]}" -w '\n%{http_code}' "$URL")" || {
  echo "error: curl failed talking to ${BASE} (is api_server running?)" >&2
  exit 3
}
HTTP_CODE="${BODY##*$'\n'}"
BODY="${BODY%$'\n'*}"

if [[ "$HTTP_CODE" != "200" ]]; then
  echo "error: HTTP ${HTTP_CODE} from /indicators/top" >&2
  # body may contain detail JSON — safe to print (no API key)
  printf '%s\n' "$BODY" >&2
  exit 4
fi

# Pretty-print if jq/python available; else raw
if command -v jq >/dev/null 2>&1; then
  printf '%s\n' "$BODY" | jq .
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  printf '%s\n' "$BODY" | "$ROOT/.venv/bin/python" -m json.tool
else
  printf '%s\n' "$BODY"
fi
