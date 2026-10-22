"""Decision engine config — env + defaults. Never log secrets."""

from __future__ import annotations

import math
import os
from pathlib import Path

# Load project .env the same way as root config (keys never printed).
# At the monorepo root, crawl config and .env live under crawler/.
_PKG_PARENT = Path(__file__).resolve().parents[1]
if (_PKG_PARENT / "logging_setup.py").is_file():
    _ROOT = _PKG_PARENT
elif (_PKG_PARENT / "crawler" / "logging_setup.py").is_file():
    _ROOT = _PKG_PARENT / "crawler"
else:
    _ROOT = _PKG_PARENT
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

# DECISION_DB_PATH wins; DB_PATH is the crawler/shared fallback (same resolution on both trees).
DB_PATH = (
    os.environ.get("DECISION_DB_PATH", "").strip()
    or os.environ.get("DB_PATH", "").strip()
    or str(_ROOT_DB_PATH)
)
# Resolve relative to project root
if not Path(DB_PATH).is_absolute():
    DB_PATH = str((_ROOT / DB_PATH).resolve())

TICK_INTERVAL_SEC = int(os.environ.get("DECISION_TICK_INTERVAL_SEC", "60"))
HOT_TOKEN_LIMIT = int(os.environ.get("DECISION_HOT_TOKEN_LIMIT", "50"))
DEFAULT_STRATEGY = os.environ.get("DECISION_DEFAULT_STRATEGY", "jev_strength_v1")

# Reader-side hot freshness. Indicators own the writes; this layer only drops stale rows.
# Window matches crawler HOT_SEEN_WINDOW_SEC (1h): tier='hot' alone is not enough.
HOT_SEEN_WINDOW_SEC = int(os.environ.get("DECISION_HOT_SEEN_WINDOW_SEC", "3600"))
# tech_updated_at, when the column exists, must be within ~2× the indicator hot interval.
_INDICATORS_HOT_INTERVAL_SEC = int(os.environ.get("INDICATORS_HOT_INTERVAL_SEC", "300"))
TECH_FRESH_SEC = int(
    os.environ.get(
        "DECISION_TECH_FRESH_SEC",
        str(max(1, _INDICATORS_HOT_INTERVAL_SEC * 2)),
    )
)

# Per-(strategy_id, chain_id, token_address) buy cooldown. Default 0 keeps dry-run
# observation unchanged (the same token may be signaled every tick).
# Step B (paper) should set DECISION_COOLDOWN_SEC ≈ DECISION_TICK_INTERVAL_SEC
# so a token is not re-bought every tick while position-manager dedup is off.
DECISION_COOLDOWN_SEC = float(os.environ.get("DECISION_COOLDOWN_SEC", "0") or "0")


def _env_float(raw: str | None, default: float) -> float:
    """Parse a DECISION_* float. Empty or non-finite values keep the default."""
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return value


def _env_optional_positive(name: str) -> float | None:
    """Unset, blank, non-positive, or non-finite → None (feature off)."""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


# Live/dry-run buy liquidity floor (USD). Aligned with backtest
# FilterConfig.min_liq in decision_engine/backtest/filters.py (50_000).
# RiskManager drops a buy when liquidity_usd is missing (no_liquidity, fail-closed)
# or below this floor (liq_below_min), before SignalBus writes decision_log.
# Sells are not filtered. This is an entry risk filter only: it does not change
# JEV strength, factor weights, purpose gating, or paper/live trading flags.
# Env: DECISION_MIN_LIQ_USD. Invalid values fall back to 50000.
DECISION_MIN_LIQ_USD = _env_float(os.environ.get("DECISION_MIN_LIQ_USD", "50000"), 50_000.0)

# JEV strength strategy defaults
JEV_MIN_STRENGTH = float(os.environ.get("DECISION_JEV_MIN_STRENGTH", "0.3"))
JEV_POSITION_PCT = float(os.environ.get("DECISION_JEV_POSITION_PCT", "0.05"))

# Ordinary buy size. Paper notional is full-size at execution; this layer must
# not answer by lifting every signal. Cap stays 0.05 (do not raise to 0.25).
# A strategy position_pct below the cap is kept. Above the cap is clipped
# unless the thick-pool rule below promotes the buy.
# Env: DECISION_ORDINARY_SIZE_PCT. Invalid values fall back to 0.05.
ORDINARY_SIZE_PCT = _env_float(os.environ.get("DECISION_ORDINARY_SIZE_PCT", "0.05"), 0.05)

# Thick-pool full size. size_pct reaches this only when liquidity, pool age,
# R3=0, and jev_strength all pass (see decision_engine.strategies.entry_sizing).
# Does not change admission floors (50k liq / strength / coverage) or #71.
# Env: DECISION_THICK_POOL_SIZE_PCT. Invalid values fall back to 1.0.
THICK_POOL_SIZE_PCT = _env_float(os.environ.get("DECISION_THICK_POOL_SIZE_PCT", "1.0"), 1.0)
THICK_MIN_LIQ_USD = _env_float(os.environ.get("DECISION_THICK_MIN_LIQ_USD", "150000"), 150_000.0)
THICK_MIN_POOL_AGE_DAYS = _env_float(
    os.environ.get("DECISION_THICK_MIN_POOL_AGE_DAYS", "7"), 7.0
)
# Frozen R3 sample-in thresholds. Same predicate as
# crawler/indicators/shadow_stop_prone.eval_r3 (vol > 2.5 AND
# (change_1h < 0 OR rsi < 30 OR rsi >= 70)).
THICK_R3_VOL_THR = _env_float(os.environ.get("DECISION_THICK_R3_VOL_THR", "2.5"), 2.5)
THICK_R3_RSI_LO = _env_float(os.environ.get("DECISION_THICK_R3_RSI_LO", "30"), 30.0)
THICK_R3_RSI_HI = _env_float(os.environ.get("DECISION_THICK_R3_RSI_HI", "70"), 70.0)
THICK_MIN_JEV_STRENGTH = _env_float(
    os.environ.get("DECISION_THICK_MIN_JEV_STRENGTH", "0.15"), 0.15
)

# Optional stricter thick-pool liquidity tier. Unset = off (150k rule stands).
# Set DECISION_THICK_STRICT_MIN_LIQ_USD=500000 to require liq >= 500k before
# full size. Does not change the 50k admission floor and does not consult
# supply_gaps.
THICK_STRICT_MIN_LIQ_USD = _env_optional_positive("DECISION_THICK_STRICT_MIN_LIQ_USD")
# Reject JEV buys whose jev_updated_at (or jev_age_min when the indicator layer
# provides it) is older than this. 0 disables. Audit 2026-10-21: all 143 paper
# trades used jev data 48-248h old.
JEV_MAX_AGE_MIN = float(os.environ.get("DECISION_JEV_MAX_AGE_MIN", "1440"))
# Require an explicit directional bullish stance in jev_signals; a NOT BULLISH /
# NEUTRAL / INSUFFICIENT EVIDENCE stance promoted to "bullish" by score band is rejected.
JEV_REQUIRE_BULLISH_STANCE = os.environ.get("DECISION_JEV_REQUIRE_BULLISH_STANCE", "1") != "0"

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
