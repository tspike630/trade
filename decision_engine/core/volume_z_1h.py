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

The only writer is ``persist_volume_z_for_decision_tick``, called from the
decision tick and from ``python -m decision_engine.core.volume_z_1h --db``.
It reads ``binance_klines`` where ``interval = '1h'`` in that same SQLite
file. It does not fetch and it does not start a collector loop. The latest
21 closed bars must be consecutive hours. A gap, a conflicting duplicate
hour, or an invalid newest quote deletes any previous ``volume_z``.
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

log = logging.getLogger("decision_engine.volume_z_1h")

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


def _raw_times_and_quote(item: object) -> tuple[int, int, float | None] | None:
    """Split one source row into open, close, and quote.

    ``quote is None`` means the timestamps are a closed 1h bar but the quote
    volume is missing, negative, or non-finite. A row that is not a 1h bar
    returns ``None`` and is ignored.
    """
    if isinstance(item, (list, tuple)):
        if len(item) <= _CLOSE_TIME_INDEX:
            return None
        open_ms = _finite(item[_OPEN_TIME_INDEX])
        close_ms = _finite(item[_CLOSE_TIME_INDEX])
        quote = _finite(item[_QUOTE_VOLUME_INDEX]) if len(item) > _QUOTE_VOLUME_INDEX else None
    elif isinstance(item, dict):
        open_ms = _finite(item.get("open_time_ms"))
        close_ms = _finite(item.get("close_time_ms"))
        quote = _finite(item.get("quote_volume"))
    else:
        return None
    if open_ms is None or close_ms is None:
        return None
    open_i = int(open_ms)
    close_i = int(close_ms)
    if close_i - open_i != _BINANCE_CLOSE_SPAN_MS:
        return None
    if quote is None or quote < 0:
        quote_out: float | None = None
    else:
        quote_out = quote
    return open_i, close_i, quote_out


def _closed_hours(
    source_rows: Sequence[object],
    *,
    now_ms: int,
) -> list[tuple[int, int, float | None]]:
    by_open: dict[int, tuple[int, int, float | None]] = {}
    for item in source_rows:
        parsed = _raw_times_and_quote(item)
        if parsed is None or parsed[1] > int(now_ms):
            continue
        previous = by_open.get(parsed[0])
        if previous is None:
            by_open[parsed[0]] = parsed
            continue
        if previous[2] is None or parsed[2] is None or previous[2] != parsed[2]:
            by_open[parsed[0]] = (parsed[0], max(previous[1], parsed[1]), None)
            continue
        if parsed[1] >= previous[1]:
            by_open[parsed[0]] = parsed
    return sorted(by_open.values(), key=lambda bar: bar[0])


def inspect_volume_z(
    source_rows: Sequence[object],
    *,
    now_ms: int,
) -> dict[str, Any]:
    """Explain one symbol. ``status=ready`` is the only case that stores a value."""
    ordered = _closed_hours(source_rows, now_ms=now_ms)
    base = {
        "closed_hours": len(ordered),
        "volume_z": None,
        "source_open_time_ms": None,
        "source_close_time_ms": None,
        "current_quote_volume": None,
        "baseline_mean": None,
        "baseline_std": None,
    }
    if not ordered:
        return {**base, "status": "no_closed_1h_bar"}
    if ordered[-1][2] is None:
        return {**base, "status": "newest_closed_bar_invalid"}
    if len(ordered) < VOLUME_Z_MIN_CLOSED_BARS:
        return {**base, "status": "fewer_than_21_closed_hours"}
    window = ordered[-VOLUME_Z_MIN_CLOSED_BARS :]
    for older, newer in zip(window, window[1:]):
        if newer[0] - older[0] != _HOUR_MS or older[2] is None or newer[2] is None:
            return {**base, "status": "latest_21_not_consecutive"}
    current = window[-1]
    quotes = [float(bar[2]) for bar in window[:-1]]
    std = _sample_std(quotes)
    if std is None:
        return {**base, "status": "zero_or_nonfinite_std"}
    mean = sum(quotes) / len(quotes)
    assert current[2] is not None
    score = (current[2] - mean) / std
    if not math.isfinite(score):
        return {**base, "status": "nonfinite_score"}
    return {
        **base,
        "status": "ready",
        "volume_z": score,
        "source_open_time_ms": current[0],
        "source_close_time_ms": current[1],
        "current_quote_volume": current[2],
        "baseline_mean": mean,
        "baseline_std": std,
    }


def compute_volume_z(
    source_rows: Sequence[object],
    *,
    now_ms: int,
) -> tuple[float, int, int, float, float, float] | None:
    """Compute the z-score. ``None`` means do not store a value.

    The latest 21 closed 1h bars must be consecutive (open times exactly one
    hour apart). The current bar is excluded from the baseline. An invalid
    newest closed bar does not fall back to an older window.

    The returned tuple is ``(volume_z, open_ms, close_ms, current_quote,
    baseline_mean, baseline_std)``.
    """
    found = inspect_volume_z(source_rows, now_ms=now_ms)
    if found["status"] != "ready":
        return None
    return (
        float(found["volume_z"]),
        int(found["source_open_time_ms"]),
        int(found["source_close_time_ms"]),
        float(found["current_quote_volume"]),
        float(found["baseline_mean"]),
        float(found["baseline_std"]),
    )


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
        _ensure_table(conn)
        if symbols is None:
            conn.execute(f"DELETE FROM {_TABLE}")
            conn.commit()
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
    if symbols is None:
        keep = {item.upper() for item in chosen}
        _ensure_table(conn)
        stored_symbols = [
            str(row[0]) for row in conn.execute(f"SELECT symbol FROM {_TABLE}").fetchall()
        ]
        for symbol in stored_symbols:
            if symbol.upper() not in keep:
                conn.execute(f"DELETE FROM {_TABLE} WHERE symbol = ?", (symbol,))
        conn.commit()
    return written


def persist_volume_z_for_decision_tick(db_path: Path, *, now_ms: int) -> None:
    """One writer: the decision tick, not a second kline collector.

    Opens a short read-write connection to the same SQLite the indicator
    reader already uses. Writes only ``decision_volume_z``. Does not fetch
    candles and does not update ``binance_indicators`` or ``binance_klines``.
    """
    path = Path(db_path)
    if not path.is_file():
        return
    conn = sqlite3.connect(str(path), timeout=10.0)
    try:
        refresh_volume_z_from_klines(conn, now_ms=now_ms)
    except sqlite3.Error as exc:
        log.warning("volume_z persist skipped path=%s err=%s", path, exc)
    finally:
        conn.close()


def _fresh_window_ms() -> int:
    """Same window as ``binance_indicators_fresh_sec``. 0 disables the cutoff."""
    raw = os.environ.get("BINANCE_INDICATORS_FRESH_SEC", "3600")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = 3600
    return (value if value > 0 else 0) * 1000


def source_close_is_fresh(close_ms: int, now_ms: int) -> bool:
    """True when the source close is inside the indicator freshness window.

    The window is ``BINANCE_INDICATORS_FRESH_SEC`` (default 3600 seconds),
    inclusive. A consecutive 1h series switches to the new bar at its close
    timestamp, so the previous bar is not asked to stay fresh for a second
    hour. Age ``3600000`` still passes. Age ``3600001`` does not.
    """
    return _fresh(close_ms, now_ms)


def _fresh(close_ms: int, now_ms: int) -> bool:
    window_ms = _fresh_window_ms()
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
            SELECT symbol, volume_z, interval, quote_asset,
                   source_open_time_ms, source_close_time_ms,
                   current_quote_volume, baseline_count, baseline_mean,
                   baseline_std, contract_version
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
        if not _stored_matches_source(conn, symbol, saved, now_ms=now_ms):
            continue
        candidate[VOLUME_Z_KEY] = _finite(saved["volume_z"])
    return rows


def _stored_matches_source(
    conn: sqlite3.Connection,
    symbol: str,
    saved: dict[str, Any],
    *,
    now_ms: int,
) -> bool:
    """Require version, freshness, and a recompute that matches the stored bar."""
    score = _finite(saved.get("volume_z"))
    std = _finite(saved.get("baseline_std"))
    close_ms = saved.get("source_close_time_ms")
    if (
        score is None
        or std is None
        or std <= 0
        or close_ms is None
        or str(saved.get("interval") or "") != VOLUME_Z_INTERVAL
        or str(saved.get("quote_asset") or "") != VOLUME_Z_QUOTE_ASSET
        or str(saved.get("contract_version") or "") != VOLUME_Z_CONTRACT_VERSION
        or int(saved.get("baseline_count") or 0) != VOLUME_Z_BASELINE_BARS
        or not _fresh(int(close_ms), now_ms)
    ):
        return False
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'binance_klines'"
    ).fetchone()
    if present is None:
        return False
    try:
        source_rows = conn.execute(
            """
            SELECT open_time_ms, close_time_ms, quote_volume
            FROM binance_klines
            WHERE symbol = ? AND interval = ?
            """,
            (symbol, VOLUME_Z_INTERVAL),
        ).fetchall()
    except sqlite3.Error:
        return False
    source = [
        {"open_time_ms": row[0], "close_time_ms": row[1], "quote_volume": row[2]}
        for row in source_rows
    ]
    computed = compute_volume_z(source, now_ms=now_ms)
    if computed is None:
        return False
    got, open_ms, recomputed_close, current_quote, mean, recomputed_std = computed
    return (
        int(saved.get("source_open_time_ms") or -1) == open_ms
        and int(close_ms) == recomputed_close
        and math.isclose(score, got, rel_tol=0.0, abs_tol=1e-9)
        and math.isclose(float(saved.get("current_quote_volume") or -1), current_quote, rel_tol=0.0, abs_tol=1e-9)
        and math.isclose(float(saved.get("baseline_mean") or -1), mean, rel_tol=0.0, abs_tol=1e-9)
        and math.isclose(std, recomputed_std, rel_tol=0.0, abs_tol=1e-9)
    )


def main(argv: list[str] | None = None) -> int:
    """One-shot writer. The decision tick calls the same persist function.

    ``python -m decision_engine.core.volume_z_1h --db <indicator.sqlite>``
    Does not fetch and does not place orders. ``--db`` is required.
    This module does not import crawler config, so it does not load ``.env``.
    """
    import argparse
    import time

    parser = argparse.ArgumentParser(description="Persist decision_volume_z from 1h binance_klines")
    parser.add_argument("--db", required=True)
    parser.add_argument("--now-ms", type=int, default=0)
    args = parser.parse_args(argv)
    now_ms = int(args.now_ms) if args.now_ms else int(time.time() * 1000)
    persist_volume_z_for_decision_tick(Path(args.db), now_ms=now_ms)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
