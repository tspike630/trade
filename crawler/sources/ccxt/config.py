"""CCXT side-channel config — env + defaults. Never log secrets (none needed)."""

from __future__ import annotations

import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except ImportError:
    _env = _ROOT / ".env"
    if _env.is_file():
        for _line in _env.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# 独立旁路库（不碰业务四库）
DB_PATH = os.environ.get("CCXT_DB_PATH", "").strip() or str(_ROOT / "ccxt.db")
if not Path(DB_PATH).is_absolute():
    DB_PATH = str((_ROOT / DB_PATH).resolve())

EXCHANGES = os.environ.get("CCXT_EXCHANGES", "binance,okx,kucoin").split(",")
TIMEFRAME = os.environ.get("CCXT_TIMEFRAME", "5m")
TOP_LIMIT = int(os.environ.get("CCXT_TOP_LIMIT", "50"))
INTERVAL_SEC = int(os.environ.get("CCXT_INTERVAL_SEC", "300"))
MIN_QUOTE_VOLUME = float(os.environ.get("CCXT_MIN_QUOTE_VOLUME", "100000.0"))
OHLCV_LIMIT = int(os.environ.get("CCXT_OHLCV_LIMIT", "60"))
