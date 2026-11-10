"""python3 -m research.hist_calibration

Default is a help exit. Download, upsert, and activation are separate flags.
"""

from __future__ import annotations

from research.hist_calibration.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
