"""Exact-bar cross-venue deviation. Missing bars stay missing.

Only the same base and the same quote are paired. USDT is not converted
into USD. High and low are not interpolated across a gap, and a one-sided
print is not called a wick.
"""

from __future__ import annotations

import sqlite3
import statistics
from typing import Mapping, Sequence


def pair_exact_bars(
    left: Mapping[int, Mapping[str, float]],
    right: Mapping[int, Mapping[str, float]],
) -> list[dict[str, float]]:
    """Pair bars that share an open timestamp. Gaps are counted, not filled."""
    paired: list[dict[str, float]] = []
    for open_ms in sorted(set(left) & set(right)):
        a = left[open_ms]
        b = right[open_ms]
        if b["close"] == 0:
            continue
        paired.append(
            {
                "open_time_ms": float(open_ms),
                "close_a": a["close"],
                "close_b": b["close"],
                "high_a": a["high"],
                "low_a": a["low"],
                "deviation_bps": (a["close"] - b["close"]) / b["close"] * 10_000.0,
            }
        )
    return paired


def gap_counts(
    left: Mapping[int, Mapping[str, float]],
    right: Mapping[int, Mapping[str, float]],
) -> dict[str, int]:
    only_left = len(set(left) - set(right))
    only_right = len(set(right) - set(left))
    return {
        "aligned": len(set(left) & set(right)),
        "only_left": only_left,
        "only_right": only_right,
        "filled_by_interpolation": 0,
    }


def summarize_pairs(pairs: Sequence[Mapping[str, float]]) -> dict[str, float | int | None]:
    if not pairs:
        return {"n": 0, "median_abs_bps": None, "p95_abs_bps": None}
    absolutes = sorted(abs(float(item["deviation_bps"])) for item in pairs)
    index = min(len(absolutes) - 1, max(0, int(round(0.95 * (len(absolutes) - 1)))))
    return {
        "n": len(pairs),
        "median_abs_bps": statistics.median(absolutes),
        "p95_abs_bps": absolutes[index],
    }


def load_venue_bars(
    conn: sqlite3.Connection,
    exchange: str,
    symbol: str,
) -> dict[int, dict[str, float]]:
    rows = conn.execute(
        """
        SELECT ts, open, high, low, close
        FROM ccxt_ohlcv
        WHERE exchange = ? AND symbol = ? AND timeframe = '5m'
        ORDER BY ts
        """,
        (exchange, symbol),
    ).fetchall()
    out: dict[int, dict[str, float]] = {}
    for ts, open_, high, low, close in rows:
        if close is None or high is None or low is None:
            continue
        out[int(ts)] = {
            "open": float(open_),
            "high": float(high),
            "low": float(low),
            "close": float(close),
        }
    return out
