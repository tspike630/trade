#!/usr/bin/env python3
"""Thin wrapper: python scripts/run_jev.py --once | --loop --interval 900"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sources.jev import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
