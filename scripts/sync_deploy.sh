#!/usr/bin/env bash
# Sync one component of the deploy dir to origin/main.
#
# Usage: scripts/sync_deploy.sh [--dry-run] <decision_engine|indicators|execution|crawler|dashboard>
#
# Env: DEPLOY_ROOT (default /workspace/dexscreener-crawler)
#      REPO_DIR    (default: the git repo containing this script)
#      OUT_DIR     (default /workspace/out, backups)
#
# Repo -> deploy mapping (deploy root is a flattened copy of repo crawler/,
# plus repo-root decision_engine/):
#   decision_engine  repo decision_engine/     -> deploy decision_engine/
#   indicators       repo crawler/indicators/  -> deploy indicators/
#   execution        repo execution/           -> deploy execution/   (repo crawler/execution must be identical, else abort)
#   dashboard        repo dashboard/           -> deploy dashboard/   (repo crawler/dashboard must be identical, else abort)
#   crawler          repo crawler/*.py, *.sh   -> deploy root (top-level files only, NO deletion at root)
#                    + repo crawler/{sources,worker,models}/ -> deploy {sources,worker,models}/ (with deletion)
#
# Never touches: __pycache__, .env*, *.db (+ -wal/-shm), logs/, DEPLOY_SHA.
# Does NOT restart any process.
set -euo pipefail

DRY=0
if [[ "${1:-}" == "--dry-run" ]]; then DRY=1; shift; fi
COMP="${1:-}"
DEPLOY_ROOT="${DEPLOY_ROOT:-/workspace/dexscreener-crawler}"
OUT_DIR="${OUT_DIR:-/workspace/out}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)}"

die() { echo "sync_deploy: $*" >&2; exit 1; }

case "$COMP" in
  decision_engine) PAIRS=("decision_engine:decision_engine") ;;
  indicators)      PAIRS=("crawler/indicators:indicators") ;;
  execution)       PAIRS=("execution:execution"); MIRROR="crawler/execution" ;;
  dashboard)       PAIRS=("dashboard:dashboard"); MIRROR="crawler/dashboard" ;;
  crawler)         PAIRS=("crawler/sources:sources" "crawler/worker:worker" "crawler/models:models") ;;
  *) die "usage: $0 [--dry-run] <decision_engine|indicators|execution|crawler|dashboard>" ;;
esac
[[ -d "$DEPLOY_ROOT" ]] || die "deploy root not found: $DEPLOY_ROOT"

git -C "$REPO_DIR" fetch -q origin main
SHA="$(git -C "$REPO_DIR" rev-parse origin/main)"
WT="$(mktemp -d /tmp/sync_deploy_${COMP}_XXXXXX)"
rmdir "$WT"
git -C "$REPO_DIR" worktree add -q --detach "$WT" "$SHA"
cleanup() { git -C "$REPO_DIR" worktree remove --force "$WT" >/dev/null 2>&1 || true; }
trap cleanup EXIT
echo "sync_deploy: component=$COMP origin/main=$SHA deploy_root=$DEPLOY_ROOT dry_run=$DRY"

if [[ -n "${MIRROR:-}" ]]; then
  diff -rq -x __pycache__ "$WT/${PAIRS[0]%%:*}" "$WT/$MIRROR" >/dev/null \
    || die "repo ${PAIRS[0]%%:*} and $MIRROR differ on main; resolve in repo first"
fi

DIFF_X=(-x __pycache__ -x '.env*' -x '*.db' -x '*.db-wal' -x '*.db-shm' -x logs -x DEPLOY_SHA)

# list deploy files that would be deleted (not in main), honoring excludes
extra_files() {  # $1 src  $2 dst
  (cd "$2" && find . -type f \
      ! -path '*/__pycache__/*' ! -path './logs/*' ! -name '.env*' \
      ! -name '*.db' ! -name '*.db-wal' ! -name '*.db-shm' ! -name DEPLOY_SHA -print) |
  while IFS= read -r f; do [[ -e "$1/$f" ]] || echo "${f#./}"; done
}

show_diff() {
  local rc=0
  for p in "${PAIRS[@]}"; do
    src="$WT/${p%%:*}"; dst="$DEPLOY_ROOT/${p##*:}"
    echo "== ${p%%:*} -> ${dst}"
    if [[ -d "$dst" ]]; then diff -rq "${DIFF_X[@]}" "$src" "$dst" || rc=1
    else echo "(deploy dir missing; would create)"; rc=1; fi
  done
  if [[ "$COMP" == crawler ]]; then
    echo "== crawler/*.py,*.sh -> $DEPLOY_ROOT/ (top-level, no deletion)"
    for f in "$WT"/crawler/*.py "$WT"/crawler/*.sh; do
      cmp -s "$f" "$DEPLOY_ROOT/$(basename "$f")" || { echo "differs/missing: $(basename "$f")"; rc=1; }
    done
  fi
  return $rc
}

if (( DRY )); then
  if show_diff; then echo "sync_deploy: dry-run: no diff"; else echo "sync_deploy: dry-run: differences above (nothing changed)"; fi
  exit 0
fi

TS="$(date +%Y%m%d%H%M%S)"
mkdir -p "$OUT_DIR"
BACKUP="$OUT_DIR/${COMP}_pre_sync_${SHA:0:7}_${TS}.tgz"
TAR_PATHS=()
for p in "${PAIRS[@]}"; do [[ -d "$DEPLOY_ROOT/${p##*:}" ]] && TAR_PATHS+=("${p##*:}"); done
if [[ "$COMP" == crawler ]]; then
  for f in "$WT"/crawler/*.py "$WT"/crawler/*.sh; do [[ -e "$DEPLOY_ROOT/$(basename "$f")" ]] && TAR_PATHS+=("$(basename "$f")"); done
fi
tar czf "$BACKUP" -C "$DEPLOY_ROOT" --exclude=__pycache__ --exclude='*.db' --exclude='*.db-wal' \
    --exclude='*.db-shm' --exclude='.env*' --exclude=logs "${TAR_PATHS[@]}"
echo "sync_deploy: backup $BACKUP"

for p in "${PAIRS[@]}"; do
  src="$WT/${p%%:*}"; dst="$DEPLOY_ROOT/${p##*:}"
  mkdir -p "$dst"
  extra_files "$src" "$dst" | while IFS= read -r f; do echo "delete $dst/$f"; rm -f -- "$dst/$f"; done
  cp -a "$src/." "$dst/"
done
if [[ "$COMP" == crawler ]]; then
  cp -a "$WT"/crawler/*.py "$WT"/crawler/*.sh "$DEPLOY_ROOT/"
fi

show_diff >/tmp/sync_deploy_${COMP}_diff.txt 2>&1 || { cat /tmp/sync_deploy_${COMP}_diff.txt; die "post-sync diff not empty (backup: $BACKUP)"; }
echo "sync_deploy: post-sync diff empty"

if [[ "$COMP" == crawler ]]; then
  TREE="$(python3 "$WT/scripts/tree_fingerprint.py" "$DEPLOY_ROOT" --top-level-only)"
  SHA_FILE="$DEPLOY_ROOT/DEPLOY_SHA.crawler"
else
  TREE="$(python3 "$WT/scripts/tree_fingerprint.py" "$DEPLOY_ROOT/${PAIRS[0]##*:}")"
  SHA_FILE="$DEPLOY_ROOT/${PAIRS[0]##*:}/DEPLOY_SHA"
fi
printf '%s\n%s\n' "$SHA" "$TREE" > "$SHA_FILE"
echo "sync_deploy: wrote $SHA_FILE sha=$SHA tree_sha256=${TREE:0:12}"
echo "sync_deploy: done (no process restarted)"
