"""Hourly USDT quote-volume z-score selected on 2026-11-10.

This is not a formula from the design manual. Collection confirmed the
contract: the latest closed 1h Binance USDT quote volume compared with the
previous 20 closed 1h bars. The current bar is not part of the baseline.
Sample standard deviation uses divisor n-1. A value is stored only when 21
closed hourly bars are present and the baseline standard deviation is
finite and greater than zero.

The stored key is ``volume_z``. ``vol_z_1h`` is the only read alias, and
only when that same key is already on a row. ``volume_strength``,
``quote_volume_24h``, and ``orderbook_imbalance_20`` are not this series.
``obi_top5`` is not computed here.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import Any, Sequence

from decision_engine.core.binance_indicators import binance_indicators_fresh_sec

VOLUME_Z_CONTRACT_VERSION = "hourly_usdt_quote_v1"
VOLUME_Z_INTERVAL = "1h"
VOLUME_Z_QUOTE_ASSET = "USDT"
VOLUME_Z_KEY = "volume_z"
VOLUME_Z_READ_ALIAS = "vol_z_1h"
VOLUME_Z_BASELINE_BARS = 20
VOLUME_Z_MIN_CLOSED_BARS = VOLUME_Z_BASELINE_BARS + 1
VOLUME_Z_THRESHOLD = 3.8
_HOUR_MS = 3_600_000
_BINANCE_CLOSE_SPAN_MS = _HOUR_MS - 1
_OPEN_TIME_INDEX = 0
_CLOSE_TIME_INDEX = 6
_QUOTE_VOLUME_INDEX = 7
_TABLE = "decision_volume_z"


@dataclass(frozen=True)
class VolumeZRecord:
    symbol: str
    volume_z: float
    source_open_time_ms: int
    source_close_time_ms: int
    current_quote_volume: float
    baseline_count: int
    baseline_mean: float
    baseline_std: float
    contract_version: str = VOLUME_Z_CONTRACT_VERSION
    interval: str = VOLUME_Z_INTERVAL
    quote_asset: str = VOLUME_Z_QUOTE_ASSET


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _sample_std(values: Sequence[float]) -> float | None:
    count = len(values)
    if count < 2:
        return None
    mean = sum(values) / count
    variance = sum((item - mean) ** 2 for item in values) / (count - 1)
    std = math.sqrt(variance)
    if not math.isfinite(std) or std <= 0.0:
        return None
    return std


def _parse_source_bar(item: object) -> tuple[int, int, float] | None:
    """Return ``(open_ms, close_ms, quote_volume)`` for one 1h source bar."""
    if isinstance(item, (list, tuple)):
        if len(item) <= _QUOTE_VOLUME_INDEX:
            return None
        open_ms = _finite(item[_OPEN_TIME_INDEX])
        close_ms = _finite(item[_CLOSE_TIME_INDEX])
        quote = _finite(item[_QUOTE_VOLUME_INDEX])
    elif isinstance(item, dict):
        open_ms = _finite(item.get("open_time_ms"))
        close_ms = _finite(item.get("close_time_ms"))
        quote = _finite(item.get("quote_volume"))
    else:
        return None
    if open_ms is None or close_ms is None or quote is None:
        return None
    if quote < 0:
        return None
    open_i = int(open_ms)
    close_i = int(close_ms)
    if close_i - open_i != _BINANCE_CLOSE_SPAN_MS:
        return None
    return open_i, close_i, quote


def compute_volume_z(
    source_rows: Sequence[object],
    *,
    now_ms: int,
) -> tuple[float, int, int, float, float, float] | None:
    """Compute the z-score. ``None`` means do not store a value.

    The returned tuple is ``(volume_z, open_ms, close_ms, current_quote,
    baseline_mean, baseline_std)``.
    """
    parsed = [bar for item in source_rows if (bar := _parse_source_bar(item)) is not None]
    closed = [bar for bar in parsed if bar[1] <= int(now_ms)]
    by_open: dict[int, tuple[int, int, float]] = {}
    for bar in closed:
        previous = by_open.get(bar[0])
        if previous is None or bar[1] >= previous[1]:
            by_open[bar[0]] = bar
    ordered = sorted(by_open.values(), key=lambda bar: (bar[1], bar[0]))
    if len(ordered) < VOLUME_Z_MIN_CLOSED_BARS:
        return None
    current = ordered[-1]
    baseline = ordered[-(VOLUME_Z_MIN_CLOSED_BARS):-1]
    if len(baseline) != VOLUME_Z_BASELINE_BARS:
        return None
    if any(bar[1] >= current[1] for bar in baseline):
        return None
    quotes = [bar[2] for bar in baseline]
    std = _sample_std(quotes)
    if std is None:
        return None
    mean = sum(quotes) / len(quotes)
    score = (current[2] - mean) / std
    if not math.isfinite(score):
        return None
    return score, current[0], current[1], current[2], mean, std


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            symbol TEXT PRIMARY KEY,
            volume_z REAL NOT NULL,
            interval TEXT NOT NULL,
            quote_asset TEXT NOT NULL,
            source_open_time_ms INTEGER NOT NULL,
            source_close_time_ms INTEGER NOT NULL,
            current_quote_volume REAL NOT NULL,
            baseline_count INTEGER NOT NULL,
            baseline_mean REAL NOT NULL,
            baseline_std REAL NOT NULL,
            contract_version TEXT NOT NULL,
            computed_at_ms INTEGER NOT NULL
        )
        """
    )


def _delete_symbol(conn: sqlite3.Connection, symbol: str) -> None:
    _ensure_table(conn)
    conn.execute(f"DELETE FROM {_TABLE} WHERE symbol = ?", (symbol,))


def write_symbol_volume_z(
    conn: sqlite3.Connection,
    symbol: str,
    source_rows: Sequence[object],
    *,
    now_ms: int,
) -> VolumeZRecord | None:
    """Parse source bars, store one row, or delete the symbol when no value is due."""
    spot = str(symbol or "").strip().upper()
    if not spot:
        return None
    computed = compute_volume_z(source_rows, now_ms=now_ms)
    if computed is None:
        _delete_symbol(conn, spot)
        conn.commit()
        return None
    score, open_ms, close_ms, current_quote, mean, std = computed
    record = VolumeZRecord(
        symbol=spot,
        volume_z=score,
        source_open_time_ms=open_ms,
        source_close_time_ms=close_ms,
        current_quote_volume=current_quote,
        baseline_count=VOLUME_Z_BASELINE_BARS,
        baseline_mean=mean,
        baseline_std=std,
    )
    _ensure_table(conn)
    conn.execute(
        f"""
        INSERT OR REPLACE INTO {_TABLE} (
            symbol, volume_z, interval, quote_asset,
            source_open_time_ms, source_close_time_ms, current_quote_volume,
            baseline_count, baseline_mean, baseline_std,
            contract_version, computed_at_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.symbol,
            record.volume_z,
            record.interval,
            record.quote_asset,
            record.source_open_time_ms,
            record.source_close_time_ms,
            record.current_quote_volume,
            record.baseline_count,
            record.baseline_mean,
            record.baseline_std,
            record.contract_version,
            int(now_ms),
        ),
    )
    conn.commit()
    return record


def refresh_volume_z_from_klines(
    conn: sqlite3.Connection,
    *,
    now_ms: int,
    symbols: Sequence[str] | None = None,
) -> list[VolumeZRecord]:
    """Read stored ``binance_klines`` interval ``1h`` and write ``decision_volume_z``.

    This does not fetch candles and does not write ``binance_indicators``.
    """
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'binance_klines'"
    ).fetchone()
    if present is None:
        return []
    if symbols is None:
        found = conn.execute(
            "SELECT DISTINCT symbol FROM binance_klines WHERE interval = ?",
            (VOLUME_Z_INTERVAL,),
        ).fetchall()
        chosen = [str(row[0]) for row in found]
    else:
        chosen = [str(item).strip().upper() for item in symbols if str(item).strip()]
    written: list[VolumeZRecord] = []
    for symbol in chosen:
        rows = conn.execute(
            """
            SELECT open_time_ms, close_time_ms, quote_volume
            FROM binance_klines
            WHERE symbol = ? AND interval = ?
            ORDER BY close_time_ms ASC, open_time_ms ASC
            """,
            (symbol, VOLUME_Z_INTERVAL),
        ).fetchall()
        source = [
            {
                "open_time_ms": row[0],
                "close_time_ms": row[1],
                "quote_volume": row[2],
            }
            for row in rows
        ]
        record = write_symbol_volume_z(conn, symbol, source, now_ms=now_ms)
        if record is not None:
            written.append(record)
    return written


def _fresh(close_ms: int, now_ms: int) -> bool:
    window_ms = binance_indicators_fresh_sec() * 1000
    age = int(now_ms) - int(close_ms)
    if age < 0:
        return False
    if window_ms <= 0:
        return True
    return age <= window_ms


def attach_stored_volume_z(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    now_ms: int,
) -> list[dict[str, Any]]:
    """Copy a fresh stored ``volume_z`` onto candidate rows. Missing stays absent."""
    if not rows:
        return rows
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (_TABLE,),
    ).fetchone()
    if present is None:
        return rows
    try:
        cursor = conn.execute(
            f"""
            SELECT symbol, volume_z, interval, quote_asset, source_close_time_ms,
                   baseline_count, contract_version, baseline_std
            FROM {_TABLE}
            """
        )
        names = [str(item[0]) for item in cursor.description or ()]
        fetched = [dict(zip(names, row)) for row in cursor.fetchall()]
    except sqlite3.Error:
        return rows
    stored = {str(row["symbol"]).upper(): row for row in fetched}
    for candidate in rows:
        symbol = str(
            candidate.get("binance_symbol") or candidate.get("token_address") or ""
        ).strip().upper()
        saved = stored.get(symbol)
        if saved is None:
            continue
        score = _finite(saved["volume_z"])
        std = _finite(saved["baseline_std"])
        close_ms = saved["source_close_time_ms"]
        if (
            score is None
            or std is None
            or std <= 0
            or str(saved["interval"]) != VOLUME_Z_INTERVAL
            or str(saved["quote_asset"]) != VOLUME_Z_QUOTE_ASSET
            or str(saved["contract_version"]) != VOLUME_Z_CONTRACT_VERSION
            or int(saved["baseline_count"]) != VOLUME_Z_BASELINE_BARS
            or not _fresh(int(close_ms), now_ms)
        ):
            continue
        candidate[VOLUME_Z_KEY] = score
    return rows
