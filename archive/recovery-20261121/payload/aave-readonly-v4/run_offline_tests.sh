#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec bwrap --unshare-all --clearenv --setenv PATH /usr/bin:/bin --setenv PYTHONDONTWRITEBYTECODE 1 \
 --ro-bind /usr /usr --ro-bind /lib /lib --ro-bind /lib64 /lib64 \
 --proc /proc --dev /dev --tmpfs /tmp --ro-bind "$HERE" /review --chdir /review \
 /usr/bin/python3 -B -m unittest -v test_aave_evidence_review test_candidate_v3_regression test_t584_fixes test_t586_oco
