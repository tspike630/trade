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
from dataclasses import dataclass, replace
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

# The CSV label is stored as written. Binance has not documented a bin-edge
# convention for these files, so availability stays unverified unless the
# operator explicitly asks for a research clock.
LABEL_CONVENTION = "unverified"
LABEL_AVAILABILITY = "unknown"
METRIC_AVAILABILITY_UNKNOWN = "unknown"
METRIC_AVAILABILITY_BIN_END = "explicit_conservative_bin_end"
METRIC_BIN_MS = 300_000
BOOK_DEPTH_SEMANTICS = "percentage_from_mid_aggregate"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vision_files (
    url TEXT PRIMARY KEY,
    sha256 TEXT,
    byte_count INTEGER,
    row_count INTEGER,
    status TEXT NOT NULL,
    detail TEXT,
    schema_version TEXT NOT NULL,
    checksum_sha256 TEXT,
    checksum_status TEXT,
    downloaded_at_ms INTEGER,
    http_last_modified TEXT
);
CREATE TABLE IF NOT EXISTS vision_metrics (
    symbol TEXT NOT NULL,
    create_time_ms INTEGER NOT NULL,
    count_toptrader_long_short_ratio REAL,
    sum_toptrader_long_short_ratio REAL,
    count_long_short_ratio REAL,
    sum_taker_long_short_vol_ratio REAL,
    source_url TEXT NOT NULL,
    create_time_raw TEXT,
    create_time_unit TEXT,
    label_convention TEXT,
    label_availability TEXT,
    feature_available_at_ms INTEGER,
    feature_availability_basis TEXT,
    downloaded_at_ms INTEGER,
    http_last_modified TEXT,
    PRIMARY KEY (symbol, create_time_ms)
);
CREATE TABLE IF NOT EXISTS vision_book_depth (
    symbol TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    percentage REAL NOT NULL,
    depth REAL,
    notional REAL,
    source_url TEXT NOT NULL,
    depth_semantics TEXT,
    downloaded_at_ms INTEGER,
    http_last_modified TEXT,
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
    downloaded_at_ms INTEGER,
    http_last_modified TEXT,
    PRIMARY KEY (symbol, interval, open_time_ms)
);
"""

_ADDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "vision_files": (
        ("checksum_sha256", "TEXT"),
        ("checksum_status", "TEXT"),
        ("downloaded_at_ms", "INTEGER"),
        ("http_last_modified", "TEXT"),
    ),
    "vision_metrics": (
        ("create_time_raw", "TEXT"),
        ("create_time_unit", "TEXT"),
        ("label_convention", "TEXT"),
        ("label_availability", "TEXT"),
        ("feature_available_at_ms", "INTEGER"),
        ("feature_availability_basis", "TEXT"),
        ("downloaded_at_ms", "INTEGER"),
        ("http_last_modified", "TEXT"),
    ),
    "vision_book_depth": (
        ("depth_semantics", "TEXT"),
        ("downloaded_at_ms", "INTEGER"),
        ("http_last_modified", "TEXT"),
    ),
    "vision_spot_klines": (
        ("downloaded_at_ms", "INTEGER"),
        ("http_last_modified", "TEXT"),
    ),
}


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: str
    sha256: str | None
    byte_count: int
    detail: str | None
    payload: bytes | None
    downloaded_at_ms: int | None = None
    http_last_modified: str | None = None
    checksum_sha256: str | None = None
    checksum_status: str | None = None


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
        raw_label = str(raw["create_time"]).strip()
        item: dict[str, object] = {
            "symbol": str(raw["symbol"]).strip().upper(),
            "create_time_raw": raw_label,
            "create_time_unit": _civil_unit(raw_label),
            "create_time_ms": parse_utc_text(raw_label),
            "label_convention": LABEL_CONVENTION,
            "label_availability": LABEL_AVAILABILITY,
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


def _civil_unit(label: str) -> str:
    clock = label.strip().split(" ")[-1]
    if "." in clock:
        return "civil_utc_fractional_second"
    return "civil_utc_second"


def metric_feature_clock(create_time_ms: int, mode: str) -> tuple[int | None, str]:
    """Research clock, separate from the CSV label and from download time.

    ``unknown`` leaves feature availability unset. ``explicit_conservative_bin_end``
    adds five minutes for a study that wants the bin treated as closed only at
    its end. That choice is not a Binance attestation and does not change
    ``create_time_ms``.
    """
    if mode == METRIC_AVAILABILITY_UNKNOWN:
        return None, METRIC_AVAILABILITY_UNKNOWN
    if mode == METRIC_AVAILABILITY_BIN_END:
        return int(create_time_ms) + METRIC_BIN_MS, METRIC_AVAILABILITY_BIN_END
    raise ValueError(f"unsupported metric availability mode {mode}")


def parse_sha256sum(text: str, filename: str) -> str:
    """Parse a Binance ``.CHECKSUM`` body (``sha256sum`` layout)."""
    line = ""
    for candidate in text.splitlines():
        if candidate.strip():
            line = candidate.strip()
            break
    if not line:
        raise ValueError("empty checksum")
    parts = line.split()
    if len(parts) != 2:
        raise ValueError("checksum line is not sha256sum layout")
    digest, name = parts
    if len(digest) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in digest):
        raise ValueError("checksum is not a sha256 hex digest")
    bare = name[1:] if name.startswith("*") else name
    if Path(bare).name != Path(filename).name:
        raise ValueError(f"checksum name {bare} does not match {Path(filename).name}")
    return digest.lower()


def checksum_url_for(url: str) -> str:
    return url + ".CHECKSUM"


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    for table, columns in _ADDED_COLUMNS.items():
        existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, declaration in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
    forbidden = {
        str(row[1])
        for table in ("vision_metrics", "vision_book_depth", "vision_spot_klines", "vision_files")
        for row in conn.execute(f"PRAGMA table_info({table})")
    }
    if "obi_top5" in forbidden:
        raise RuntimeError("vision tables must not carry obi_top5")
    conn.commit()
    return conn


def already_stored(conn: sqlite3.Connection, url: str, sha256: str | None) -> bool:
    row = conn.execute(
        "SELECT sha256, status FROM vision_files WHERE url = ?",
        (url,),
    ).fetchone()
    return bool(row and row[1] == "ok" and sha256 is not None and row[0] == sha256)


def stored_ok(conn: sqlite3.Connection, url: str) -> bool:
    """True when this URL was imported. A later run must not download it again."""
    row = conn.execute(
        "SELECT status, sha256 FROM vision_files WHERE url = ?",
        (url,),
    ).fetchone()
    return bool(row and row[0] == "ok" and row[1])


def _clock_ms() -> int:
    return int(time.time() * 1000)


def _header_last_modified(headers: object) -> str | None:
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    value = getter("Last-Modified")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def fetch_bytes(url: str, limiter: RateLimiter, attempts: int = 4) -> FetchResult:
    last_detail = "not_attempted"
    for attempt in range(attempts):
        limiter.wait()
        request = Request(url, headers={"User-Agent": USER_AGENT})
        downloaded_at_ms = _clock_ms()
        try:
            with urlopen(request, timeout=60) as response:
                payload = response.read()
                published = _header_last_modified(response.headers)
        except HTTPError as exc:
            published = _header_last_modified(exc.headers)
            if exc.code == 404:
                return FetchResult(
                    url, "missing", None, 0, "404", None, downloaded_at_ms, published
                )
            last_detail = f"http_{exc.code}"
            if exc.code not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                return FetchResult(
                    url, "error", None, 0, last_detail, None, downloaded_at_ms, published
                )
            time.sleep(2**attempt)
            continue
        except (URLError, TimeoutError, OSError) as exc:
            last_detail = type(exc).__name__
            if attempt == attempts - 1:
                return FetchResult(
                    url, "error", None, 0, last_detail, None, downloaded_at_ms, None
                )
            time.sleep(2**attempt)
            continue
        digest = hashlib.sha256(payload).hexdigest()
        return FetchResult(
            url, "ok", digest, len(payload), None, payload, downloaded_at_ms, published
        )
    return FetchResult(url, "error", None, 0, last_detail, None, _clock_ms(), None)


def _record_file(
    conn: sqlite3.Connection,
    fetched: FetchResult,
    row_count: int,
) -> None:
    conn.execute(
        """
        INSERT INTO vision_files (
            url, sha256, byte_count, row_count, status, detail, schema_version,
            checksum_sha256, checksum_status, downloaded_at_ms, http_last_modified
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            sha256=excluded.sha256,
            byte_count=excluded.byte_count,
            row_count=excluded.row_count,
            status=excluded.status,
            detail=excluded.detail,
            schema_version=excluded.schema_version,
            checksum_sha256=excluded.checksum_sha256,
            checksum_status=excluded.checksum_status,
            downloaded_at_ms=excluded.downloaded_at_ms,
            http_last_modified=excluded.http_last_modified
        """,
        (
            fetched.url,
            fetched.sha256,
            fetched.byte_count,
            row_count,
            fetched.status,
            fetched.detail,
            SCHEMA_VERSION,
            fetched.checksum_sha256,
            fetched.checksum_status,
            fetched.downloaded_at_ms,
            fetched.http_last_modified,
        ),
    )


def _fail_file(conn: sqlite3.Connection, fetched: FetchResult, detail: str) -> None:
    failed = replace(fetched, status="error", detail=detail[:300], payload=None)
    _record_file(conn, failed, 0)
    conn.commit()


def ingest_zip(
    conn: sqlite3.Connection,
    kind: str,
    symbol: str,
    fetched: FetchResult,
    *,
    availability_mode: str = METRIC_AVAILABILITY_UNKNOWN,
) -> int:
    if fetched.status != "ok" or fetched.payload is None or fetched.sha256 is None:
        _record_file(conn, fetched, 0)
        conn.commit()
        return 0
    if already_stored(conn, fetched.url, fetched.sha256):
        return 0
    try:
        texts = _zip_texts(fetched.payload)
        written = 0
        if kind == "metrics":
            for _name, text in texts:
                for row in parse_metrics_csv(text):
                    feature_ms, feature_basis = metric_feature_clock(
                        int(row["create_time_ms"]), availability_mode
                    )
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO vision_metrics (
                            symbol, create_time_ms, count_toptrader_long_short_ratio,
                            sum_toptrader_long_short_ratio, count_long_short_ratio,
                            sum_taker_long_short_vol_ratio, source_url,
                            create_time_raw, create_time_unit, label_convention,
                            label_availability, feature_available_at_ms,
                            feature_availability_basis, downloaded_at_ms, http_last_modified
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            row["symbol"],
                            row["create_time_ms"],
                            row["count_toptrader_long_short_ratio"],
                            row["sum_toptrader_long_short_ratio"],
                            row["count_long_short_ratio"],
                            row["sum_taker_long_short_vol_ratio"],
                            fetched.url,
                            row["create_time_raw"],
                            row["create_time_unit"],
                            row["label_convention"],
                            row["label_availability"],
                            feature_ms,
                            feature_basis,
                            fetched.downloaded_at_ms,
                            fetched.http_last_modified,
                        ),
                    )
                    written += 1
        elif kind == "bookDepth":
            for _name, text in texts:
                for row in parse_book_depth_csv(text, symbol):
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO vision_book_depth (
                            symbol, timestamp_ms, percentage, depth, notional, source_url,
                            depth_semantics, downloaded_at_ms, http_last_modified
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            row["symbol"],
                            row["timestamp_ms"],
                            row["percentage"],
                            row["depth"],
                            row["notional"],
                            fetched.url,
                            BOOK_DEPTH_SEMANTICS,
                            fetched.downloaded_at_ms,
                            fetched.http_last_modified,
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
                            timestamp_unit, source_url, downloaded_at_ms, http_last_modified
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                            fetched.downloaded_at_ms,
                            fetched.http_last_modified,
                        ),
                    )
                    written += 1
        else:
            raise ValueError(kind)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, UnicodeError, OSError) as exc:
        conn.rollback()
        detail = f"bad_zip:{type(exc).__name__}:{exc}"
        _fail_file(conn, fetched, detail)
        return 0
    _record_file(conn, fetched, written)
    conn.commit()
    return written


def import_planned_file(
    conn: sqlite3.Connection,
    kind: str,
    symbol: str,
    url: str,
    fetcher: object,
    limiter: RateLimiter,
    *,
    availability_mode: str = METRIC_AVAILABILITY_UNKNOWN,
) -> str:
    """Verify the publisher checksum, then import. One file cannot stop the run.

    A URL already stored as ``ok`` is not downloaded again.
    """
    if stored_ok(conn, url):
        return "skipped_stored"
    get = getattr(fetcher, "__call__", None)
    if get is None:
        raise TypeError("fetcher must be callable")
    try:
        checksum = fetcher(checksum_url_for(url), limiter)
    except (OSError, URLError, TimeoutError, HTTPError) as exc:
        _fail_file(
            conn,
            FetchResult(url, "error", None, 0, None, None, _clock_ms(), None),
            f"checksum_fetch:{type(exc).__name__}",
        )
        return "error"
    if not isinstance(checksum, FetchResult):
        _fail_file(
            conn,
            FetchResult(url, "error", None, 0, None, None, _clock_ms(), None),
            "checksum_fetch:bad_result",
        )
        return "error"
    if checksum.status != "ok" or not checksum.payload:
        detail = "checksum_404" if checksum.status == "missing" else f"checksum_{checksum.detail or checksum.status}"
        status = "missing" if checksum.status == "missing" else "error"
        _record_file(
            conn,
            replace(
                checksum,
                url=url,
                status=status,
                detail=detail,
                payload=None,
                sha256=None,
                checksum_status="missing" if checksum.status == "missing" else "fetch_error",
            ),
            0,
        )
        conn.commit()
        return status
    try:
        expected = parse_sha256sum(checksum.payload.decode("utf-8", errors="replace"), url)
    except ValueError as exc:
        _fail_file(
            conn,
            replace(checksum, url=url, payload=None, sha256=None, checksum_status="malformed"),
            f"checksum_malformed:{exc}",
        )
        return "error"
    try:
        fetched = fetcher(url, limiter)
    except (OSError, URLError, TimeoutError, HTTPError) as exc:
        _fail_file(
            conn,
            FetchResult(url, "error", None, 0, None, None, _clock_ms(), None, expected, "fetch_error"),
            f"zip_fetch:{type(exc).__name__}",
        )
        return "error"
    if not isinstance(fetched, FetchResult):
        _fail_file(
            conn,
            FetchResult(url, "error", None, 0, None, None, _clock_ms(), None, expected, "fetch_error"),
            "zip_fetch:bad_result",
        )
        return "error"
    if fetched.status != "ok" or fetched.payload is None or fetched.sha256 is None:
        status = "missing" if fetched.status == "missing" else "error"
        detail = "zip_404" if status == "missing" else (fetched.detail or fetched.status)
        _record_file(
            conn,
            replace(
                fetched,
                url=url,
                status=status,
                detail=detail,
                payload=None,
                checksum_sha256=expected,
                checksum_status="parsed_zip_absent",
            ),
            0,
        )
        conn.commit()
        return status
    if fetched.sha256.lower() != expected:
        _record_file(
            conn,
            replace(
                fetched,
                status="error",
                detail="checksum_mismatch",
                payload=None,
                checksum_sha256=expected,
                checksum_status="mismatch",
            ),
            0,
        )
        conn.commit()
        return "error"
    verified = replace(fetched, checksum_sha256=expected, checksum_status="verified")
    try:
        ingest_zip(conn, kind, symbol, verified, availability_mode=availability_mode)
    except Exception as exc:  # noqa: BLE001 — one bad file must not stop the plan
        _fail_file(conn, replace(verified, payload=None), f"import:{type(exc).__name__}:{exc}")
        return "error"
    row = conn.execute("SELECT status FROM vision_files WHERE url = ?", (url,)).fetchone()
    if row is None or row[0] != "ok":
        return "error"
    return "ok"


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
    errors = [row for row in files if row["status"] not in {"ok", "missing"}]
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
        "book_depth_semantics": BOOK_DEPTH_SEMANTICS,
        "obi_top5_column": False,
        "metrics_columns_kept_separate": list(METRIC_COLUMNS),
        "metric_label_convention": LABEL_CONVENTION,
        "metric_label_availability": LABEL_AVAILABILITY,
        "clocks": {
            "create_time_ms": "CSV label parsed as UTC. Not an availability attestation.",
            "create_time_raw": "Original metrics create_time text.",
            "downloaded_at_ms": "When this process stored the HTTP response.",
            "http_last_modified": "Publisher Last-Modified header when the server sent one.",
            "feature_available_at_ms": (
                "Null unless the operator set explicit_conservative_bin_end. "
                "That research clock is label plus 5 minutes and is neither "
                "download time nor publication time."
            ),
        },
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
