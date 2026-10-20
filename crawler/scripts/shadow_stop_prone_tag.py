#!/usr/bin/env python3
"""Phase0 影子打标旁路（双列只读）· 可 cron / 手动重跑。

列：
  - shadow_R3_vol_chg_rsi   (原 R3)
  - shadow_liq_lt_150k      (liq_usd 非 NULL 且 <150k；NULL≠低流动性)

用法（仓库根目录）:
  .venv/bin/python scripts/shadow_stop_prone_tag.py
  .venv/bin/python -m indicators.shadow_stop_prone
  .venv/bin/python scripts/shadow_stop_prone_tag.py --json-stats

只写 exports/shadow_skip_stop_prone_live.csv（及可选独立 sqlite）。
不改权重 / 闸 / .env，不写 paper_trading.db，不接 soft-skip。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from indicators.shadow_stop_prone import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
