#!/usr/bin/env bash
# Thin wrapper → scripts/top.sh (see that file for usage).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$ROOT/scripts/top.sh" "$@"
