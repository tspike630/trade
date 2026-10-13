"""pandas-ta thin wrapper for ADD-ON technical indicators (RSI/MACD/BOLL).

Does NOT replace change_*/volatility_24h/volume_strength — those stay in
indicators/technical.py. This module only computes library-backed fields
from a close (price) series. No true OHLC available from pair_history;
high/low default to close.

Import: pandas-ta-classic exposes ``pandas_ta_classic`` (not ``pandas_ta``).
"""
from __future__ import annotations

import math
from typing import Any, Optional

import pandas as pd

try:
    import pandas_ta_classic as ta
except ImportError:  # pragma: no cover — older forks may still use pandas_ta
    import pandas_ta as ta  # type: ignore


def _nan_list(n: int) -> list[float]:
    return [float("nan")] * n


def _align(series: Optional[pd.Series], n: int) -> list[float]:
    """Align pandas-ta Series to length n; pad leading NaN if short."""
    if series is None:
        return _nan_list(n)
    values = series.to_numpy(dtype="float64", na_value=float("nan"))
    if len(values) >= n:
        return [float(v) for v in values[-n:]]
    return _nan_list(n - len(values)) + [float(v) for v in values]


def _finite_or_none(v: Any) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def series_from_pair_history(rows: list[dict[str, Any]]) -> tuple[list[int], list[float]]:
    """Extract (epoch_sec_or_index, close) from pair_history-like dict rows."""
    closes: list[float] = []
    ts: list[int] = []
    for i, row in enumerate(rows):
        p = row.get("price_usd")
        try:
            px = float(p) if p is not None and p != "" else None
        except (TypeError, ValueError):
            px = None
        if px is None or px <= 0 or math.isnan(px) or math.isinf(px):
            continue
        closes.append(px)
        # Prefer recorded_at epoch if present as int/float; else sequential index
        ep = row.get("_epoch")
        if ep is None:
            ra = row.get("recorded_at")
            # leave sequential; caller may pass pre-parsed epochs via _epoch
            ts.append(i if not isinstance(ra, (int, float)) else int(ra))
        else:
            ts.append(int(ep))
    return ts, closes


def compute_pandas_ta(
    closes: list[float],
    *,
    high: list[float] | None = None,
    low: list[float] | None = None,
) -> dict[str, float | None]:
    """
    Compute last-bar RSI(14) / MACD(12,26,9) / BOLL(20,2σ).

    Returns keys aligned with token_indicators columns:
      rsi_14, macd, macd_signal, macd_hist,
      boll_upper, boll_mid, boll_lower
    Missing / insufficient history → None (never raises).
    """
    empty = {
        "rsi_14": None,
        "macd": None,
        "macd_signal": None,
        "macd_hist": None,
        "boll_upper": None,
        "boll_mid": None,
        "boll_lower": None,
    }
    if not closes or len(closes) < 2:
        return dict(empty)

    close = pd.Series(closes, dtype="float64")
    hi = pd.Series(high if high is not None else closes, dtype="float64")
    lo = pd.Series(low if low is not None else closes, dtype="float64")
    _ = hi, lo  # reserved for ATR / future OHLC indicators
    n = len(close)
    out = dict(empty)

    # --- RSI (Wilder, 14) ---
    try:
        rsi = ta.rsi(close, length=14)
        vals = _align(rsi, n)
        out["rsi_14"] = _finite_or_none(vals[-1])
    except Exception:  # noqa: BLE001
        out["rsi_14"] = None

    # --- MACD (12/26/9); map by column name (order varies across forks) ---
    try:
        macd_df = ta.macd(close, fast=12, slow=26, signal=9)
        if macd_df is not None and not macd_df.empty:
            colmap: dict[str, str] = {}
            for c in macd_df.columns:
                cu = str(c).upper()
                if "MACDH" in cu or cu.startswith("MACDH"):
                    colmap["hist"] = c
                elif "MACDS" in cu or (cu.startswith("MACDS") and "MACDH" not in cu):
                    colmap["signal"] = c
                elif cu.startswith("MACD") and "MACDH" not in cu and "MACDS" not in cu:
                    colmap["macd"] = c
            # Fallback by common pandas-ta-classic order: MACD, MACDh, MACDs
            cols = list(macd_df.columns)
            if "macd" not in colmap and cols:
                colmap["macd"] = cols[0]
            if "hist" not in colmap and len(cols) > 1:
                # classic 0.8.x: index 1 is hist
                for c in cols:
                    if "h" in str(c).lower() and c not in colmap.values():
                        colmap["hist"] = c
                        break
            if "signal" not in colmap and len(cols) > 2:
                for c in cols:
                    if c not in colmap.values():
                        colmap["signal"] = c
                        break
            if "macd" in colmap:
                out["macd"] = _finite_or_none(_align(macd_df[colmap["macd"]], n)[-1])
            if "signal" in colmap:
                out["macd_signal"] = _finite_or_none(
                    _align(macd_df[colmap["signal"]], n)[-1]
                )
            if "hist" in colmap:
                out["macd_hist"] = _finite_or_none(_align(macd_df[colmap["hist"]], n)[-1])
    except Exception:  # noqa: BLE001
        pass
    # Partial MACD triples are not a value. Callers keep the previous set or store NULL.
    if any(out[key] is None for key in ("macd", "macd_signal", "macd_hist")):
        out["macd"] = None
        out["macd_signal"] = None
        out["macd_hist"] = None

    # --- BOLL (20, 2σ) ---
    try:
        bb = ta.bbands(close, length=20, std=2.0)
        if bb is not None and not bb.empty:
            bbl = bbm = bbu = None
            for c in bb.columns:
                cu = str(c).upper()
                if cu.startswith("BBL"):
                    bbl = c
                elif cu.startswith("BBM"):
                    bbm = c
                elif cu.startswith("BBU"):
                    bbu = c
            if bbl is None and len(bb.columns) >= 1:
                bbl = bb.columns[0]
            if bbm is None and len(bb.columns) >= 2:
                bbm = bb.columns[1]
            if bbu is None and len(bb.columns) >= 3:
                bbu = bb.columns[2]
            if bbu is not None:
                out["boll_upper"] = _finite_or_none(_align(bb[bbu], n)[-1])
            if bbm is not None:
                out["boll_mid"] = _finite_or_none(_align(bb[bbm], n)[-1])
            if bbl is not None:
                out["boll_lower"] = _finite_or_none(_align(bb[bbl], n)[-1])
    except Exception:  # noqa: BLE001
        pass

    return out


# Aliases matching tutorial naming (boll_up / boll_low)
def compute_technical_last(closes: list[float]) -> dict[str, float | None]:
    """Same as compute_pandas_ta; also exposes boll_up/boll_low aliases."""
    out = compute_pandas_ta(closes)
    out["boll_up"] = out.get("boll_upper")
    out["boll_low"] = out.get("boll_lower")
    return out
