"""pandas-ta thin wrapper for ADD-ON technical indicators (RSI/MACD/BOLL).

Does NOT replace change_*/volatility_24h/volume_strength — those stay in
indicators/technical.py. This module only computes library-backed fields
from a close (price) series. No true OHLC available from pair_history;
high/low default to close.

Import: pandas-ta-classic exposes ``pandas_ta_classic`` (not ``pandas_ta``).
When ``pandas`` / ``pandas_ta`` are unavailable in a lightweight runtime,
falls back to exact stdlib implementations of Wilder RSI(14), EMA MACD(12,26,9),
and BOLL(20, 2σ).
"""
from __future__ import annotations

import math
from typing import Any, Optional

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None  # type: ignore[assignment]

try:
    import pandas_ta_classic as ta
except ImportError:  # pragma: no cover — older forks may still use pandas_ta
    try:
        import pandas_ta as ta  # type: ignore
    except ImportError:
        ta = None  # type: ignore[assignment]


def _nan_list(n: int) -> list[float]:
    return [float("nan")] * n


def _align(series: Optional[Any], n: int) -> list[float]:
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
        ep = row.get("_epoch")
        if ep is None:
            ra = row.get("recorded_at")
            ts.append(i if not isinstance(ra, (int, float)) else int(ra))
        else:
            ts.append(int(ep))
    return ts, closes


def _stdlib_rsi_14(closes: list[float], length: int = 14) -> float | None:
    if len(closes) <= length:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[:length]) / length
    avg_loss = sum(losses[:length]) / length
    for i in range(length, len(gains)):
        avg_gain = (avg_gain * (length - 1) + gains[i]) / length
        avg_loss = (avg_loss * (length - 1) + losses[i]) / length
    if avg_loss == 0.0 and avg_gain == 0.0:
        return 50.0
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return _finite_or_none(100.0 - (100.0 / (1.0 + rs)))


def _stdlib_ema_series(values: list[float], span: int) -> list[float]:
    if len(values) < span:
        return []
    alpha = 2.0 / (span + 1.0)
    ema = sum(values[:span]) / span
    out = [ema]
    for v in values[span:]:
        ema = alpha * v + (1.0 - alpha) * ema
        out.append(ema)
    return out


def _stdlib_macd(
    closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[float | None, float | None, float | None]:
    if len(closes) < slow + signal - 1:
        return None, None, None
    alpha_f = 2.0 / (fast + 1.0)
    alpha_s = 2.0 / (slow + 1.0)
    ema_f = sum(closes[:fast]) / fast
    ema_f_by_idx: dict[int, float] = {fast - 1: ema_f}
    for idx in range(fast, len(closes)):
        ema_f = alpha_f * closes[idx] + (1.0 - alpha_f) * ema_f
        ema_f_by_idx[idx] = ema_f
    ema_s = sum(closes[:slow]) / slow
    macd_line: list[float] = [ema_f_by_idx[slow - 1] - ema_s]
    for idx in range(slow, len(closes)):
        ema_s = alpha_s * closes[idx] + (1.0 - alpha_s) * ema_s
        macd_line.append(ema_f_by_idx[idx] - ema_s)
    sig_series = _stdlib_ema_series(macd_line, signal)
    if not sig_series:
        return None, None, None
    m_val = _finite_or_none(macd_line[-1])
    s_val = _finite_or_none(sig_series[-1])
    if m_val is None or s_val is None:
        return None, None, None
    h_val = _finite_or_none(m_val - s_val)
    if h_val is None:
        return None, None, None
    return m_val, s_val, h_val


def _stdlib_bbands(
    closes: list[float], length: int = 20, std: float = 2.0
) -> tuple[float | None, float | None, float | None]:
    if len(closes) < length:
        return None, None, None
    window = closes[-length:]
    mid = sum(window) / length
    var = sum((x - mid) ** 2 for x in window) / length
    sd = math.sqrt(var)
    upper = _finite_or_none(mid + std * sd)
    mid_f = _finite_or_none(mid)
    lower = _finite_or_none(mid - std * sd)
    return upper, mid_f, lower


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
    empty: dict[str, float | None] = {
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

    if pd is None or ta is None:
        out = dict(empty)
        try:
            out["rsi_14"] = _stdlib_rsi_14(closes, length=14)
        except Exception:  # noqa: BLE001
            out["rsi_14"] = None
        try:
            m_val, s_val, h_val = _stdlib_macd(closes, fast=12, slow=26, signal=9)
            out["macd"] = m_val
            out["macd_signal"] = s_val
            out["macd_hist"] = h_val
        except Exception:  # noqa: BLE001
            out["macd"] = None
            out["macd_signal"] = None
            out["macd_hist"] = None
        try:
            bbu, bbm, bbl = _stdlib_bbands(closes, length=20, std=2.0)
            out["boll_upper"] = bbu
            out["boll_mid"] = bbm
            out["boll_lower"] = bbl
        except Exception:  # noqa: BLE001
            pass
        return out

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
            cols = list(macd_df.columns)
            if "macd" not in colmap and cols:
                colmap["macd"] = cols[0]
            if "hist" not in colmap and len(cols) > 1:
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
