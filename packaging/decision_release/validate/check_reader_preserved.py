#!/usr/bin/env python3
"""Compare an installed top-5 reader file with this release. Read-only."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

MARKERS = (
    "MAX(obi_top5_source_ts_ms)",
    's.obi_top5_source_ts_ms',
    '_ts_is_fresh(r["obi_top5_source_ts_ms"]',
)
FORBIDDEN = (
    "attach_spot_obi_top5",
    "orderbook_imbalance_20 AS obi_top5",
)
READER_REL = "decision_engine/core/perp_microstructure_and_beta_hedge.py"


def release_root_from_here() -> Path:
    return Path(__file__).resolve().parents[1]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed", required=True, type=Path)
    parser.add_argument("--release-root", type=Path, default=None)
    parser.add_argument("--expect", choices=("match", "differ"), required=True)
    args = parser.parse_args()
    root = args.release_root or release_root_from_here()
    pinned = root / "deploy" / "READER_SHA256"
    bundle_reader = root / "deploy" / READER_REL
    installed = args.installed.expanduser().resolve()
    if not pinned.is_file() or not bundle_reader.is_file():
        print("release reader pin missing", file=sys.stderr)
        return 2
    if not installed.is_file():
        print(f"installed reader missing: {installed}", file=sys.stderr)
        return 2
    expected = pinned.read_text(encoding="utf-8").strip()
    bundle_hash = sha256_file(bundle_reader)
    if bundle_hash != expected:
        print("deploy/READER_SHA256 does not match the bundled reader", file=sys.stderr)
        return 2
    installed_hash = sha256_file(installed)
    text = installed.read_text(encoding="utf-8")
    missing = [marker for marker in MARKERS if marker not in text]
    present_forbidden = [marker for marker in FORBIDDEN if marker in text]
    same = installed_hash == expected
    print(f"installed_sha256={installed_hash}")
    print(f"bundle_sha256={expected}")
    print(f"bytes_equal={str(same).lower()}")
    print(f"missing_markers={','.join(missing) if missing else '-'}")
    print(f"forbidden_markers={','.join(present_forbidden) if present_forbidden else '-'}")
    if args.expect == "match":
        ok = same and not missing and not present_forbidden
    else:
        ok = (not same) and not present_forbidden
    print(f"expect={args.expect} result={'pass' if ok else 'fail'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
