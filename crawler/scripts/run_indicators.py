#!/usr/bin/env python3
"""Thin wrapper: python scripts/run_indicators.py --full | --loop --interval 300"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from indicators.run import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
