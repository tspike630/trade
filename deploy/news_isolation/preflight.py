#!/usr/bin/env python3
"""Read-only check before copying the news-isolation bundle.

Exit 0: the live tree is the known production mix and the two shipped files
can be copied without touching the other layer's modules.
Exit 2: do not copy.
Present counterparty, CityU, and LSEG databases are expected. They are not a
reason to stop, and this script does not delete, rename, or unset anything.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ENGINE = "decision_engine/core/engine.py"
GUARDS = "decision_engine/core/live_buy_guards.py"
PERP = "decision_engine/core/perp_microstructure_and_beta_hedge.py"
CLUSTERS = "decision_engine/core/portfolio_clusters.py"
GATE = "decision_engine/core/news_shock_gate.py"

ENGINE_BEFORE = "612bd80af39ca2f6061229a67b39a14cd71cb91661947d8936c48bb101dd9d1f"
ENGINE_BEFORE_BYTES = 18185
GUARDS_SHA = "4ff94eb982a5afc1c60d919d9ba9557fcb10e42d157d18a20891ab61b4b8d9cc"
GUARDS_BYTES = 4028
PERP_SHA = "ea3d43afcb4e5aa6388c69e698742ac04b6ff3d10c27fa7a3f5dc8072c619a34"
PERP_BYTES = 29257
CLUSTERS_SHA = "5dbd84373298ecd7df2480e4766c79d0b037663afa60e29fa21768303aec26d1"
CLUSTERS_BYTES = 10395

MUST_STAY = (
    (GUARDS, GUARDS_SHA, GUARDS_BYTES),
    (PERP, PERP_SHA, PERP_BYTES),
    (CLUSTERS, CLUSTERS_SHA, CLUSTERS_BYTES),
)

DBS = (
    "data/counterparty/public_derivatives.db",
    "data/cityu/cityu_institutional.db",
    "data/lseg/lseg_macro.db",
)


def _sha(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: preflight.py ROOT", file=sys.stderr)
        return 1
    root = Path(argv[1]).resolve()
    problems: list[str] = []

    engine = root / ENGINE
    if not engine.is_file():
        problems.append(f"missing {ENGINE}")
    else:
        digest, size = _sha(engine)
        if digest != ENGINE_BEFORE or size != ENGINE_BEFORE_BYTES:
            problems.append(
                f"{ENGINE} is {digest} {size} bytes, not {ENGINE_BEFORE} {ENGINE_BEFORE_BYTES}"
            )
        else:
            print(f"ok replace-from {ENGINE} {digest} {size}")

    for rel, expect, nbytes in MUST_STAY:
        path = root / rel
        if not path.is_file():
            problems.append(f"missing {rel}; this bundle does not install it")
            continue
        digest, size = _sha(path)
        if digest != expect or size != nbytes:
            problems.append(f"{rel} is {digest} {size} bytes, not {expect} {nbytes}. Do not overwrite.")
        else:
            print(f"ok leave {rel} {digest} {size}")

    gate = root / GATE
    if gate.exists():
        digest, size = _sha(gate)
        problems.append(f"already present {GATE} {digest} {size} bytes")
    else:
        print(f"ok add {GATE}")

    for rel in DBS:
        path = root / rel
        if path.is_file():
            print(f"ok db present {rel} {path.stat().st_size} bytes; isolation will not open it")
        else:
            print(f"ok db absent {rel}; isolation fail-open does not need it")

    if problems:
        print("CONFLICT")
        for item in problems:
            print(f"- {item}")
        return 2
    print("PREFLIGHT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
