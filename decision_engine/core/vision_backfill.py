"""Keyless Binance Vision backfill for a fixed core universe.

The archive is not the live top-of-book. ``bookDepth`` rows are aggregated
notional at a percentage distance from the mid. They are not ``obi_top5``
and they are not a ladder of price levels. Metrics keep
``count_toptrader_long_short_ratio``, ``sum_toptrader_long_short_ratio``,
and ``count_long_short_ratio`` in separate columns. The first two are top
traders. The third is the global account ratio. They are not
``whale_retail_divergence``.

Spot kline timestamps are detected. A 16-digit value is microseconds.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

VISION_ROOT = "https://data.binance.vision/data"
SCHEMA_VERSION = "vision_backfill_v1"
USER_AGENT = "trade-vision-backfill/1.0"

# Chosen before any return was measured. Missing names stay in the report.
CORE_BASES: tuple[str, ...] = (
    "BTC",
    "ETH",
    "BNB",
    "SOL",
    "XRP",
    "DOGE",
    "ADA",
    "AVAX",
    "LINK",
    "LTC",
)

METRIC_COLUMNS = (
    "count_toptrader_long_short_ratio",
    "sum_toptrader_long_short_ratio",
    "count_long_short_ratio",
    "sum_taker_long_short_vol_ratio",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vision_files (
    url TEXT PRIMARY KEY,
    sha256 TEXT,
    byte_count INTEGER,
    row_count INTEGER,
    status TEXT NOT NULL,
    detail TEXT,
    schema_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vision_metrics (
    symbol TEXT NOT NULL,
    create_time_ms INTEGER NOT NULL,
    count_toptrader_long_short_ratio REAL,
    sum_toptrader_long_short_ratio REAL,
    count_long_short_ratio REAL,
    sum_taker_long_short_vol_ratio REAL,
    source_url TEXT NOT NULL,
    PRIMARY KEY (symbol, create_time_ms)
);
CREATE TABLE IF NOT EXISTS vision_book_depth (
    symbol TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    percentage REAL NOT NULL,
    depth REAL,
    notional REAL,
    source_url TEXT NOT NULL,
    PRIMARY KEY (symbol, timestamp_ms, percentage)
);
CREATE TABLE IF NOT EXISTS vision_spot_klines (
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    open_time_ms INTEGER NOT NULL,
    close_time_ms INTEGER NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL,
    quote_volume REAL,
    taker_buy_base_volume REAL,
    taker_buy_quote_volume REAL,
    timestamp_unit TEXT NOT NULL,
    source_url TEXT NOT NULL,
    PRIMARY KEY (symbol, interval, open_time_ms)
);
"""


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: str
    sha256: str | None
    byte_count: int
    detail: str | None
    payload: bytes | None


class RateLimiter:
    def __init__(self, min_interval_s: float) -> None:
        self.min_interval_s = min_interval_s
        self._next = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        if now < self._next:
            time.sleep(self._next - now)
        self._next = time.monotonic() + self.min_interval_s


def core_usdt_symbols(bases: Sequence[str] = CORE_BASES) -> tuple[str, ...]:
    return tuple(f"{base}USDT" for base in bases)


def detect_epoch_unit(value: int) -> str:
    """Return ``us``, ``ms``, or ``s``. Vision spot files may use microseconds."""
    magnitude = abs(int(value))
    if magnitude >= 10**15:
        return "us"
    if magnitude >= 10**12:
        return "ms"
    if magnitude >= 10**9:
        return "s"
    raise ValueError(f"timestamp {value} is not an epoch")


def epoch_to_ms(value: int, unit: str | None = None) -> int:
    kind = detect_epoch_unit(value) if unit is None else unit
    if kind == "us":
        return int(value) // 1000
    if kind == "ms":
        return int(value)
    if kind == "s":
        return int(value) * 1000
    raise ValueError(kind)


def closed_utc_days(end: date, days: int) -> tuple[date, ...]:
    """Inclusive closed dates. ``end`` itself is included when it is already closed."""
    if days < 1:
        raise ValueError("days must be positive")
    start = end - timedelta(days=days - 1)
    out: list[date] = []
    cursor = start
    while cursor <= end:
        out.append(cursor)
        cursor += timedelta(days=1)
    return tuple(out)


def kline_requests(symbol: str, dates: Sequence[date]) -> tuple[str, ...]:
    """Monthly zip when every day of that month is requested, otherwise daily.

    A partial month stays on daily files so a missing future day is not pulled.
    """
    by_month: dict[tuple[int, int], list[date]] = {}
    for item in dates:
        by_month.setdefault((item.year, item.month), []).append(item)
    urls: list[str] = []
    for (year, month), group in sorted(by_month.items()):
        month_start = date(year, month, 1)
        if month == 12:
            next_month = date(year + 1, 1, 1)
        else:
            next_month = date(year, month + 1, 1)
        span = (next_month - month_start).days
        if len(group) == span and min(group) == month_start:
            stamp = f"{year:04d}-{month:02d}"
            urls.append(
                f"{VISION_ROOT}/spot/monthly/klines/{symbol}/5m/{symbol}-5m-{stamp}.zip"
            )
            continue
        for item in sorted(group):
            stamp = item.isoformat()
            urls.append(
                f"{VISION_ROOT}/spot/daily/klines/{symbol}/5m/{symbol}-5m-{stamp}.zip"
            )
    return tuple(urls)


def daily_urls(kind: str, symbol: str, dates: Sequence[date]) -> tuple[str, ...]:
    if kind not in {"metrics", "bookDepth"}:
        raise ValueError(kind)
    root = "futures/um/daily"
    return tuple(
        f"{VISION_ROOT}/{root}/{kind}/{symbol}/{symbol}-{kind}-{item.isoformat()}.zip"
        for item in dates
    )


def parse_utc_text(value: str) -> int:
    text = value.strip().replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            stamp = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            return int(stamp.timestamp() * 1000)
        except ValueError:
            continue
    raise ValueError(f"not a UTC timestamp: {value}")


def _zip_texts(payload: bytes) -> list[tuple[str, str]]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        out: list[tuple[str, str]] = []
        for name in archive.namelist():
            if name.endswith("/"):
                continue
            out.append((name, archive.read(name).decode("utf-8")))
        return out


def parse_metrics_csv(text: str) -> list[dict[str, object]]:
    reader = csv.DictReader(io.StringIO(text))
    fields = set(reader.fieldnames or ())
    required = {"create_time", "symbol", *METRIC_COLUMNS}
    missing = required - fields
    if missing:
        raise ValueError("metrics columns missing " + ",".join(sorted(missing)))
    rows: list[dict[str, object]] = []
    for raw in reader:
        item: dict[str, object] = {
            "symbol": str(raw["symbol"]).strip().upper(),
            "create_time_ms": parse_utc_text(str(raw["create_time"])),
        }
        for column in METRIC_COLUMNS:
            text_value = (raw.get(column) or "").strip()
            item[column] = float(text_value) if text_value else None
        rows.append(item)
    return rows


def parse_book_depth_csv(text: str, symbol: str) -> list[dict[str, object]]:
    reader = csv.DictReader(io.StringIO(text))
    fields = set(reader.fieldnames or ())
    required = {"timestamp", "percentage", "depth", "notional"}
    if not required.issubset(fields):
        raise ValueError("bookDepth is not a top-of-book ladder: " + ",".join(sorted(fields)))
    rows: list[dict[str, object]] = []
    for raw in reader:
        rows.append(
            {
                "symbol": symbol.upper(),
                "timestamp_ms": parse_utc_text(str(raw["timestamp"])),
                "percentage": float(raw["percentage"]),
                "depth": float(raw["depth"]) if str(raw["depth"]).strip() else None,
                "notional": float(raw["notional"]) if str(raw["notional"]).strip() else None,
            }
        )
    return rows


def parse_spot_kline_csv(text: str, symbol: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for raw in csv.reader(io.StringIO(text)):
        if not raw or raw[0].startswith("open_time"):
            continue
        if len(raw) < 11:
            raise ValueError("spot kline row is short")
        open_raw = int(float(raw[0]))
        close_raw = int(float(raw[6]))
        unit = detect_epoch_unit(open_raw)
        if detect_epoch_unit(close_raw) != unit:
            raise ValueError("open and close timestamp units differ")
        open_ms = epoch_to_ms(open_raw, unit)
        close_ms = epoch_to_ms(close_raw, unit)
        span = close_ms - open_ms
        if span not in (299_999, 300_000):
            raise ValueError(f"not a closed 5m span: {span}")
        rows.append(
            {
                "symbol": symbol.upper(),
                "interval": "5m",
                "open_time_ms": open_ms,
                "close_time_ms": close_ms,
                "open": float(raw[1]),
                "high": float(raw[2]),
                "low": float(raw[3]),
                "close": float(raw[4]),
                "volume": float(raw[5]),
                "quote_volume": float(raw[7]),
                "taker_buy_base_volume": float(raw[9]),
                "taker_buy_quote_volume": float(raw[10]),
                "timestamp_unit": unit,
            }
        )
    return rows


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    return conn


def already_stored(conn: sqlite3.Connection, url: str, sha256: str | None) -> bool:
    row = conn.execute(
        "SELECT sha256, status FROM vision_files WHERE url = ?",
        (url,),
    ).fetchone()
    return bool(row and row[1] == "ok" and sha256 is not None and row[0] == sha256)


def fetch_bytes(url: str, limiter: RateLimiter, attempts: int = 4) -> FetchResult:
    last_detail = "not_attempted"
    for attempt in range(attempts):
        limiter.wait()
        request = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=60) as response:
                payload = response.read()
        except HTTPError as exc:
            if exc.code == 404:
                return FetchResult(url, "missing", None, 0, "404", None)
            last_detail = f"http_{exc.code}"
            if exc.code not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                return FetchResult(url, "error", None, 0, last_detail, None)
            time.sleep(2**attempt)
            continue
        except (URLError, TimeoutError, OSError) as exc:
            last_detail = type(exc).__name__
            if attempt == attempts - 1:
                return FetchResult(url, "error", None, 0, last_detail, None)
            time.sleep(2**attempt)
            continue
        digest = hashlib.sha256(payload).hexdigest()
        return FetchResult(url, "ok", digest, len(payload), None, payload)
    return FetchResult(url, "error", None, 0, last_detail, None)


def _record_file(
    conn: sqlite3.Connection,
    fetched: FetchResult,
    row_count: int,
) -> None:
    conn.execute(
        """
        INSERT INTO vision_files (url, sha256, byte_count, row_count, status, detail, schema_version)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            sha256=excluded.sha256,
            byte_count=excluded.byte_count,
            row_count=excluded.row_count,
            status=excluded.status,
            detail=excluded.detail,
            schema_version=excluded.schema_version
        """,
        (
            fetched.url,
            fetched.sha256,
            fetched.byte_count,
            row_count,
            fetched.status,
            fetched.detail,
            SCHEMA_VERSION,
        ),
    )


def ingest_zip(
    conn: sqlite3.Connection,
    kind: str,
    symbol: str,
    fetched: FetchResult,
) -> int:
    if fetched.status != "ok" or fetched.payload is None or fetched.sha256 is None:
        _record_file(conn, fetched, 0)
        conn.commit()
        return 0
    if already_stored(conn, fetched.url, fetched.sha256):
        return 0
    texts = _zip_texts(fetched.payload)
    written = 0
    if kind == "metrics":
        for _name, text in texts:
            for row in parse_metrics_csv(text):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO vision_metrics (
                        symbol, create_time_ms, count_toptrader_long_short_ratio,
                        sum_toptrader_long_short_ratio, count_long_short_ratio,
                        sum_taker_long_short_vol_ratio, source_url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["symbol"],
                        row["create_time_ms"],
                        row["count_toptrader_long_short_ratio"],
                        row["sum_toptrader_long_short_ratio"],
                        row["count_long_short_ratio"],
                        row["sum_taker_long_short_vol_ratio"],
                        fetched.url,
                    ),
                )
                written += 1
    elif kind == "bookDepth":
        for _name, text in texts:
            for row in parse_book_depth_csv(text, symbol):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO vision_book_depth (
                        symbol, timestamp_ms, percentage, depth, notional, source_url
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["symbol"],
                        row["timestamp_ms"],
                        row["percentage"],
                        row["depth"],
                        row["notional"],
                        fetched.url,
                    ),
                )
                written += 1
    elif kind == "klines":
        for _name, text in texts:
            for row in parse_spot_kline_csv(text, symbol):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO vision_spot_klines (
                        symbol, interval, open_time_ms, close_time_ms,
                        open, high, low, close, volume, quote_volume,
                        taker_buy_base_volume, taker_buy_quote_volume,
                        timestamp_unit, source_url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["symbol"],
                        row["interval"],
                        row["open_time_ms"],
                        row["close_time_ms"],
                        row["open"],
                        row["high"],
                        row["low"],
                        row["close"],
                        row["volume"],
                        row["quote_volume"],
                        row["taker_buy_base_volume"],
                        row["taker_buy_quote_volume"],
                        row["timestamp_unit"],
                        fetched.url,
                    ),
                )
                written += 1
    else:
        raise ValueError(kind)
    _record_file(conn, fetched, written)
    conn.commit()
    return written


def coverage_report(conn: sqlite3.Connection, symbols: Sequence[str]) -> dict[str, object]:
    files = [
        dict(zip(("url", "status", "detail", "row_count"), row))
        for row in conn.execute(
            "SELECT url, status, detail, row_count FROM vision_files ORDER BY url"
        )
    ]
    per_symbol: list[dict[str, object]] = []
    for symbol in symbols:
        metrics = conn.execute(
            "SELECT COUNT(*), MIN(create_time_ms), MAX(create_time_ms) FROM vision_metrics WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        depth = conn.execute(
            "SELECT COUNT(*), MIN(timestamp_ms), MAX(timestamp_ms) FROM vision_book_depth WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        klines = conn.execute(
            "SELECT COUNT(*), MIN(open_time_ms), MAX(open_time_ms) FROM vision_spot_klines WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        per_symbol.append(
            {
                "symbol": symbol,
                "metrics_rows": metrics[0],
                "metrics_start_ms": metrics[1],
                "metrics_end_ms": metrics[2],
                "book_depth_rows": depth[0],
                "book_depth_start_ms": depth[1],
                "book_depth_end_ms": depth[2],
                "kline_rows": klines[0],
                "kline_start_ms": klines[1],
                "kline_end_ms": klines[2],
                "listed_in_plan": True,
            }
        )
    missing = [row for row in files if row["status"] == "missing"]
    errors = [row for row in files if row["status"] == "error"]
    return {
        "schema_version": SCHEMA_VERSION,
        "core_bases": list(CORE_BASES),
        "symbols": per_symbol,
        "files": len(files),
        "missing": missing,
        "errors": errors,
        "complete": not missing and not errors and all(
            item["metrics_rows"] and item["kline_rows"] for item in per_symbol
        ),
        "book_depth_is_percentage_aggregate": True,
        "obi_top5_column": False,
        "metrics_columns_kept_separate": list(METRIC_COLUMNS),
    }


def write_manifest(path: Path, report: Mapping[str, object]) -> None:
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def iter_plan(symbols: Iterable[str], dates: Sequence[date]) -> list[tuple[str, str, str]]:
    planned: list[tuple[str, str, str]] = []
    for symbol in symbols:
        for url in daily_urls("metrics", symbol, dates):
            planned.append(("metrics", symbol, url))
        for url in daily_urls("bookDepth", symbol, dates):
            planned.append(("bookDepth", symbol, url))
        for url in kline_requests(symbol, dates):
            planned.append(("klines", symbol, url))
    return planned
