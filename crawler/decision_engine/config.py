"""Decision engine config — env + defaults. Never log secrets."""

from __future__ import annotations

import os
from pathlib import Path

# Load project .env the same way as root config (keys never printed)
_ROOT = Path(__file__).resolve().parents[1]
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

# Prefer root config DB_PATH when available
try:
    from config import DB_PATH as _ROOT_DB_PATH  # type: ignore
except Exception:  # noqa: BLE001
    _ROOT_DB_PATH = "dexscreener.db"

DB_PATH = os.environ.get("DECISION_DB_PATH", "").strip() or str(_ROOT_DB_PATH)
# Resolve relative to project root
if not Path(DB_PATH).is_absolute():
    DB_PATH = str((_ROOT / DB_PATH).resolve())

TICK_INTERVAL_SEC = int(os.environ.get("DECISION_TICK_INTERVAL_SEC", "60"))
HOT_TOKEN_LIMIT = int(os.environ.get("DECISION_HOT_TOKEN_LIMIT", "50"))
DEFAULT_STRATEGY = os.environ.get("DECISION_DEFAULT_STRATEGY", "jev_strength_v1")

# JEV strength strategy defaults
JEV_MIN_STRENGTH = float(os.environ.get("DECISION_JEV_MIN_STRENGTH", "0.3"))
JEV_POSITION_PCT = float(os.environ.get("DECISION_JEV_POSITION_PCT", "0.05"))

# Optional API fallback (Step 1 prefers SQLite; unused unless enabled)
API_BASE = os.environ.get("DECISION_API_BASE", "http://127.0.0.1:8080").rstrip("/")
# API_SECRET exists in env but must NEVER be logged or written by this package
USE_API_FALLBACK = os.environ.get("DECISION_USE_API_FALLBACK", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

DECISION_DRY_RUN_DEFAULT = True
