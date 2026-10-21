#!/usr/bin/env bash
# Behavior tests for scripts/sync_deploy.sh using temp git repos + deploy dir.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
FAIL=0; N=0
ok()   { N=$((N+1)); echo "ok $N - $1"; }
bad()  { N=$((N+1)); FAIL=$((FAIL+1)); echo "not ok $N - $1"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1"; fi; }

git init -q --bare "$T/origin.git"
git clone -q "$T/origin.git" "$T/repo" 2>/dev/null
cd "$T/repo"
git config user.email t@t; git config user.name t
mkdir -p decision_engine/core decision_engine/tests scripts deploy execution crawler/execution
echo "a=1" > decision_engine/__init__.py
echo "b=2" > decision_engine/core/b.py
echo "t" > decision_engine/tests/t.py
echo "x=1" > execution/run.py; cp execution/run.py crawler/execution/run.py
printf '[Unit]\nDescription=x\n' > deploy/dexscreener-execution.service
cp "$HERE/sync_deploy.sh" "$HERE/tree_fingerprint.py" scripts/
git add -A; git commit -qm init; git branch -M main; git push -q origin main 2>/dev/null
SHA="$(git rev-parse HEAD)"
S="$T/repo/scripts/sync_deploy.sh"
D="$T/deploy"; mkdir -p "$D/decision_engine/core" "$D/execution"
echo "a=OLD" > "$D/decision_engine/__init__.py"       # overwrite
echo "extra" > "$D/decision_engine/core/extra.py"     # extra
echo "keep" > "$D/decision_engine/x.db"               # excluded
export DEPLOY_ROOT="$D" REPO_DIR="$T/repo" OUT_DIR="$T/out" SYSTEMD_UNIT_DIR="$T/units"
# make systemctl unavailable for determinism
export PATH="$T/bin:$PATH"; mkdir -p "$T/bin"; printf '#!/bin/sh\nexit 1\n' > "$T/bin/systemctl"; chmod +x "$T/bin/systemctl"

out="$("$S" bogus 2>&1)"; rc=$?; check "unknown component -> exit 1" "[[ $rc == 1 ]]"
out="$("$S" --frob decision_engine 2>&1)"; rc=$?; check "unknown flag -> exit 1" "[[ $rc == 1 ]]"
out="$("$S" decision_engine 2>&1)"; rc=$?
check "real run without --sha -> exit 1" "[[ $rc == 1 ]] && grep -q 'sha <commit> is required' <<<\"\$out\""
out="$("$S" --sha 0000000 decision_engine 2>&1)"; rc=$?; check "bad --sha -> exit 1" "[[ $rc == 1 ]]"

out="$("$S" --dry-run decision_engine 2>&1)"; rc=$?
check "dry-run exit 0" "[[ $rc == 0 ]]"
check "dry-run prints sha" "grep -q \"sha=$SHA\" <<<\"\$out\""
check "dry-run will-add core/b.py" "grep -A2 -- '-- will-add' <<<\"\$out\" | grep -q 'decision_engine/core/b.py'"
check "dry-run will-overwrite __init__" "grep -A1 -- '-- will-overwrite' <<<\"\$out\" | grep -q 'decision_engine/__init__.py'"
check "dry-run will-delete marked needs --allow-delete" "grep -A1 'will-delete (needs --allow-delete' <<<\"\$out\" | grep -q 'core/extra.py'"
check "dry-run tests dir is synced too (add)" "grep -q 'decision_engine/tests/t.py' <<<\"\$out\""
check "dry-run changed nothing" "grep -q OLD '$D/decision_engine/__init__.py' && [[ ! -e '$D/decision_engine/core/b.py' ]]"
check ".db not listed" "! grep -q 'x.db' <<<\"\$out\""

out="$("$S" --sha "${SHA:0:10}" decision_engine 2>&1)"; rc=$?
check "real run w/ extras, no --allow-delete -> exit 2" "[[ $rc == 2 ]] && grep -q 'extras present, not deleted' <<<\"\$out\""
check "extra left in place" "[[ -e '$D/decision_engine/core/extra.py' ]]"
check "overwrite + add applied" "grep -q 'a=1' '$D/decision_engine/__init__.py' && [[ -e '$D/decision_engine/core/b.py' ]]"
check "DEPLOY_SHA 2 lines" "[[ \$(sed -n 1p '$D/decision_engine/DEPLOY_SHA') == $SHA && \$(sed -n 2p '$D/decision_engine/DEPLOY_SHA' | wc -c) == 65 ]]"
check "db untouched" "grep -q keep '$D/decision_engine/x.db'"
B1="$(ls "$T"/out/decision_engine_pre_sync_*.tgz | head -1)"
check "backup has overwritten file" "tar tzf '$B1' | grep -q 'decision_engine/__init__.py'"

sleep 1
out="$("$S" --sha "$SHA" --allow-delete decision_engine 2>&1)"; rc=$?
check "--allow-delete -> exit 0 clean" "[[ $rc == 0 ]] && grep -q 'sync_deploy: clean' <<<\"\$out\""
check "extra deleted" "[[ ! -e '$D/decision_engine/core/extra.py' ]]"
B2="$(ls -t "$T"/out/decision_engine_pre_sync_*.tgz | head -1)"
check "deleted file is in backup" "tar tzf '$B2' | grep -q 'decision_engine/core/extra.py'"
check "db still untouched" "grep -q keep '$D/decision_engine/x.db'"

# --sha must equal origin/main HEAD
echo "c=3" > decision_engine/c.py; git add -A; git commit -qm two; git push -q origin main 2>/dev/null
out="$("$S" --sha "$SHA" decision_engine 2>&1)"; rc=$?
check "stale --sha != origin/main -> exit 1" "[[ $rc == 1 ]] && grep -q 'refusing' <<<\"\$out\""

# systemd compare (read-only)
out="$("$S" --dry-run execution 2>&1)"; check "systemd not-installed" "grep -q 'not-installed' <<<\"\$out\""
mkdir -p "$T/units"; cp deploy/dexscreener-execution.service "$T/units/"
out="$("$S" --dry-run execution 2>&1)"; check "systemd same" "grep -q 'execution.service same' <<<\"\$out\""
echo "Extra=1" >> "$T/units/dexscreener-execution.service"; cp "$T/units/dexscreener-execution.service" "$T/u.bak"
out="$("$S" --dry-run execution 2>&1)"; check "systemd differ, unit not written" "grep -q 'execution.service differ' <<<\"\$out\" && cmp -s '$T/units/dexscreener-execution.service' '$T/u.bak'"

echo "1..$N  failures=$FAIL"
exit $(( FAIL > 0 ))
