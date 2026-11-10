#!/usr/bin/env bash
# Sync one component of the deploy dir to an exact origin/main commit.
#
# Usage:
#   scripts/sync_deploy.sh --dry-run [--sha <commit>] <component>
#   scripts/sync_deploy.sh --sha <commit> [--allow-delete] <component>
#   component: decision_engine | indicators | execution | crawler | dashboard
#
# --dry-run       print will-add / will-overwrite / will-delete lists; change nothing.
#                 --sha optional (defaults to origin/main; the sha used is printed).
# --sha <commit>  REQUIRED for real runs. Must equal origin/main HEAD after fetch
#                 (full sha or unique prefix), else exit 1. That exact sha is checked out.
# --allow-delete  delete deploy files absent from main. Without it, sync only
#                 adds/overwrites; extras are listed, left in place, exit code 2.
#
# Exit codes: 0 clean, 1 error / post-sync diff, 2 extras present (not deleted).
#
# Env: DEPLOY_ROOT (default /workspace/dexscreener-crawler), REPO_DIR (default:
#      repo containing this script), OUT_DIR (default /workspace/out, backups),
#      SYSTEMD_UNIT_DIR (default /etc/systemd/system; read-only unit compare).
#
# Repo -> deploy mapping (deploy root = flattened copy of repo crawler/, plus
# repo-root decision_engine/):
#   decision_engine  repo decision_engine/     -> deploy decision_engine/
#   indicators       repo crawler/indicators/  -> deploy indicators/
#   execution        repo execution/           -> deploy execution/   (repo crawler/execution must match)
#   dashboard        repo dashboard/           -> deploy dashboard/   (repo crawler/dashboard must match)
#   crawler          repo crawler/*.py,*.sh    -> deploy root (top-level files; never deleted at root)
#                    repo crawler/{sources,worker,models}/ -> deploy {sources,worker,models}/
#
# Never touches __pycache__, .env*, *.db(-wal/-shm), logs/, DEPLOY_SHA*. Never
# restarts processes. Never writes systemd units.
set -euo pipefail

DRY=0; ALLOW_DELETE=0; WANT_SHA=""; COMP=""
usage() { echo "usage: $0 [--dry-run] [--sha <commit>] [--allow-delete] <decision_engine|indicators|execution|crawler|dashboard>" >&2; exit 1; }
die() { echo "sync_deploy: $*" >&2; exit 1; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --allow-delete) ALLOW_DELETE=1 ;;
    --sha) [[ $# -ge 2 && -n "$2" ]] || die "--sha needs a value"; WANT_SHA="$2"; shift ;;
    --sha=*) WANT_SHA="${1#--sha=}"; [[ -n "$WANT_SHA" ]] || die "--sha needs a value" ;;
    -h|--help) usage ;;
    -*) die "unknown flag: $1" ;;
    *) [[ -z "$COMP" ]] || die "only one component allowed"; COMP="$1" ;;
  esac
  shift
done

MIRROR=""
case "$COMP" in
  decision_engine) PAIRS=("decision_engine:decision_engine") ;;
  indicators)      PAIRS=("crawler/indicators:indicators") ;;
  execution)       PAIRS=("execution:execution"); MIRROR="crawler/execution" ;;
  dashboard)       PAIRS=("dashboard:dashboard"); MIRROR="crawler/dashboard" ;;
  crawler)         PAIRS=("crawler/sources:sources" "crawler/worker:worker" "crawler/models:models") ;;
  *) usage ;;
esac
(( DRY )) || [[ -n "$WANT_SHA" ]] || die "--sha <commit> is required for a real (non --dry-run) sync"

DEPLOY_ROOT="${DEPLOY_ROOT:-/workspace/dexscreener-crawler}"
OUT_DIR="${OUT_DIR:-/workspace/out}"
SYSTEMD_UNIT_DIR="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)}"
[[ -d "$DEPLOY_ROOT" ]] || die "deploy root not found: $DEPLOY_ROOT"

git -C "$REPO_DIR" fetch -q origin main || die "git fetch origin main failed"
MAIN_SHA="$(git -C "$REPO_DIR" rev-parse origin/main)"
if [[ -n "$WANT_SHA" ]]; then
  FULL="$(git -C "$REPO_DIR" rev-parse --verify -q "${WANT_SHA}^{commit}" 2>/dev/null)" \
    || die "--sha $WANT_SHA does not resolve to a unique commit"
  [[ "$FULL" == "$MAIN_SHA" ]] || die "--sha $WANT_SHA ($FULL) != origin/main HEAD ($MAIN_SHA); refusing"
fi
SHA="$MAIN_SHA"

WT="$(mktemp -d "/tmp/sync_deploy_${COMP}_XXXXXX")"; rmdir "$WT"
git -C "$REPO_DIR" worktree add -q --detach "$WT" "$SHA"
cleanup() { git -C "$REPO_DIR" worktree remove --force "$WT" >/dev/null 2>&1 || true; }
trap cleanup EXIT
echo "sync_deploy: component=$COMP sha=$SHA (origin/main) deploy_root=$DEPLOY_ROOT dry_run=$DRY allow_delete=$ALLOW_DELETE"

if [[ -n "$MIRROR" && -d "$WT/$MIRROR" ]]; then
  diff -rq -x __pycache__ "$WT/${PAIRS[0]%%:*}" "$WT/$MIRROR" >/dev/null \
    || die "repo ${PAIRS[0]%%:*} and $MIRROR differ at $SHA; resolve in repo first"
fi

_list_files() {  # $1 dir -> relative file paths (excluded patterns removed)
  [[ -d "$1" ]] || return 0
  (cd "$1" && find . -type f \
      ! -path '*/__pycache__/*' ! -path './logs/*' ! -name '.env*' \
      ! -name '*.db' ! -name '*.db-wal' ! -name '*.db-shm' ! -name 'DEPLOY_SHA*' -print) |
    sed 's|^\./||' | LC_ALL=C sort
}

declare -A SRC_MAP=()
# compute ADD / OVERWRITE / DELETE arrays of absolute deploy paths, plus src map
plan() {
  ADD=(); OVR=(); DEL=(); SRC_MAP=()
  local p src dst f
  for p in "${PAIRS[@]}"; do
    src="$WT/${p%%:*}"; dst="$DEPLOY_ROOT/${p##*:}"
    while IFS= read -r f; do
      [[ -n "$f" ]] || continue
      if [[ ! -e "$dst/$f" ]]; then ADD+=("$dst/$f"); SRC_MAP["$dst/$f"]="$src/$f"
      elif ! cmp -s "$src/$f" "$dst/$f"; then OVR+=("$dst/$f"); SRC_MAP["$dst/$f"]="$src/$f"; fi
    done < <(_list_files "$src")
    while IFS= read -r f; do
      [[ -n "$f" ]] || continue
      [[ -e "$src/$f" ]] || DEL+=("$dst/$f")
    done < <(_list_files "$dst")
  done
  if [[ "$COMP" == crawler ]]; then
    for f in "$WT"/crawler/*.py "$WT"/crawler/*.sh; do
      [[ -e "$f" ]] || continue
      local t="$DEPLOY_ROOT/$(basename "$f")"
      if [[ ! -e "$t" ]]; then ADD+=("$t"); SRC_MAP["$t"]="$f"
      elif ! cmp -s "$f" "$t"; then OVR+=("$t"); SRC_MAP["$t"]="$f"; fi
    done
    local collector="$WT/scripts/fetch_public_derivatives_and_counterparty.py"
    if [[ -f "$collector" ]]; then
      local t="$DEPLOY_ROOT/scripts/fetch_public_derivatives_and_counterparty.py"
      if [[ ! -e "$t" ]]; then ADD+=("$t"); SRC_MAP["$t"]="$collector"
      elif ! cmp -s "$collector" "$t"; then OVR+=("$t"); SRC_MAP["$t"]="$collector"; fi
    fi
  fi
}

print_list() {  # $1 title, rest items
  local title="$1"; shift
  echo "-- $title ($#)"
  local i; for i in "$@"; do echo "   ${i#$DEPLOY_ROOT/}"; done
}

systemd_compare() {
  [[ "$COMP" == execution ]] || return 0
  local unit="dexscreener-execution.service" repo_unit="$WT/deploy/dexscreener-execution.service" installed=""
  [[ -f "$repo_unit" ]] || { echo "systemd: repo deploy/$unit not present; skip"; return 0; }
  local tmp; tmp="$(mktemp)"
  if command -v systemctl >/dev/null 2>&1 && systemctl cat "$unit" >"$tmp" 2>/dev/null; then
    sed -i '1{/^# /d}' "$tmp"; installed="systemctl cat $unit"
  elif [[ -f "$SYSTEMD_UNIT_DIR/$unit" ]]; then
    cp "$SYSTEMD_UNIT_DIR/$unit" "$tmp"; installed="$SYSTEMD_UNIT_DIR/$unit"
  fi
  if [[ -z "$installed" ]]; then echo "systemd: $unit not-installed (read-only check; nothing written)"
  elif diff -q "$repo_unit" "$tmp" >/dev/null; then echo "systemd: $unit same ($installed)"
  else echo "systemd: $unit differ ($installed vs repo deploy/$unit; not written):"; diff "$repo_unit" "$tmp" | sed 's/^/   /' || true
  fi
  rm -f "$tmp"
}

plan
print_list "will-add" "${ADD[@]+"${ADD[@]}"}"
print_list "will-overwrite" "${OVR[@]+"${OVR[@]}"}"
if (( ALLOW_DELETE )); then print_list "will-delete" "${DEL[@]+"${DEL[@]}"}"
else print_list "will-delete (needs --allow-delete; otherwise left in place)" "${DEL[@]+"${DEL[@]}"}"; fi
systemd_compare

if (( DRY )); then
  echo "sync_deploy: dry-run done, nothing changed (sha=$SHA)"
  exit 0
fi

TS="$(date +%Y%m%d%H%M%S)"
mkdir -p "$OUT_DIR"
BACKUP="$OUT_DIR/${COMP}_pre_sync_${SHA:0:7}_${TS}.tgz"
LIST="$(mktemp)"
for f in "${OVR[@]+"${OVR[@]}"}" "${DEL[@]+"${DEL[@]}"}"; do echo "${f#$DEPLOY_ROOT/}" >>"$LIST"; done
for p in "${PAIRS[@]}"; do [[ -d "$DEPLOY_ROOT/${p##*:}" ]] && echo "${p##*:}" >>"$LIST"; done
tar czf "$BACKUP" -C "$DEPLOY_ROOT" --exclude=__pycache__ --exclude='*.db' --exclude='*.db-wal' \
    --exclude='*.db-shm' --exclude='.env*' --exclude=logs -T "$LIST"
rm -f "$LIST"
echo "sync_deploy: backup $BACKUP (includes files to overwrite/delete)"

for t in "${ADD[@]+"${ADD[@]}"}" "${OVR[@]+"${OVR[@]}"}"; do
  mkdir -p "$(dirname "$t")"; cp -a "${SRC_MAP[$t]}" "$t"
done
if (( ALLOW_DELETE )); then
  for t in "${DEL[@]+"${DEL[@]}"}"; do echo "delete ${t#$DEPLOY_ROOT/}"; rm -f -- "$t"; done
fi

plan
if (( ${#ADD[@]} + ${#OVR[@]} > 0 )); then
  print_list "still-missing" "${ADD[@]+"${ADD[@]}"}"; print_list "still-different" "${OVR[@]+"${OVR[@]}"}"
  die "post-sync diff not empty (backup: $BACKUP)"
fi

if [[ "$COMP" == crawler ]]; then
  TREE="$(python3 "$SCRIPT_DIR/tree_fingerprint.py" "$DEPLOY_ROOT" --top-level-only)"; SHA_FILE="$DEPLOY_ROOT/DEPLOY_SHA.crawler"
else
  TREE="$(python3 "$SCRIPT_DIR/tree_fingerprint.py" "$DEPLOY_ROOT/${PAIRS[0]##*:}")"; SHA_FILE="$DEPLOY_ROOT/${PAIRS[0]##*:}/DEPLOY_SHA"
fi
printf '%s\n%s\n' "$SHA" "$TREE" > "$SHA_FILE"
echo "sync_deploy: wrote ${SHA_FILE#$DEPLOY_ROOT/} sha=$SHA tree_sha256=${TREE:0:12}"

if (( ${#DEL[@]} > 0 )); then
  print_list "extras present, not deleted (re-run with --allow-delete to remove)" "${DEL[@]}"
  echo "sync_deploy: extras present, not deleted (no process restarted)"
  exit 2
fi
echo "sync_deploy: clean (no process restarted)"
