"""Binance Vision archive staging. Not the live microstructure tables.

Daily and monthly zips are checksummed, parsed, and written only into a
caller-supplied stage database. ``bookDepth`` percentage bands are not
``obi_top5``. Top-trader account ratios stay separate from position ratios.
"""

from __future__ import annotations

import csv
import hashlib
import io
import sqlite3
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

VISION_BASE = "https://data.binance.vision/data"
KLINE_COLUMNS = (
    "open_time_ms",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time_ms",
    "quote_volume",
    "trade_count",
    "taker_buy_base",
    "taker_buy_quote",
    "ignore",
)
# Official spot kline index of taker buy quote asset volume.
TAKER_BUY_QUOTE_INDEX = 10
FIVE_MIN_MS = 300_000

_STAGE_DDL = """
CREATE TABLE IF NOT EXISTS vision_files (
    url TEXT PRIMARY KEY,
    sha256 TEXT,
    nbytes INTEGER,
    kind TEXT,
    status TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS um_metrics (
    symbol TEXT NOT NULL,
    create_time_ms INTEGER NOT NULL,
    account_long_short REAL,
    position_long_short REAL,
    source_url TEXT NOT NULL,
    PRIMARY KEY (symbol, create_time_ms)
);
CREATE TABLE IF NOT EXISTS um_book_depth_band (
    symbol TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    percentage REAL NOT NULL,
    depth REAL,
    notional REAL,
    source_url TEXT NOT NULL,
    PRIMARY KEY (symbol, ts_ms, percentage)
);
CREATE TABLE IF NOT EXISTS spot_klines (
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    open_time_ms INTEGER NOT NULL,
    close_time_ms INTEGER,
    quote_volume REAL,
    taker_buy_quote REAL,
    source_url TEXT NOT NULL,
    PRIMARY KEY (symbol, interval, open_time_ms)
);
CREATE TABLE IF NOT EXISTS stage_conflicts (
    kind TEXT,
    key TEXT,
    kept_url TEXT,
    dropped_url TEXT,
    detail TEXT
);
"""


def vision_url(kind: str, symbol: str, day: date, *, monthly: bool, interval: str = "5m") -> str:
    symbol = symbol.upper()
    if kind == "metrics":
        if monthly:
            name = f"{symbol}-metrics-{day.strftime('%Y-%m')}.zip"
            return f"{VISION_BASE}/futures/um/monthly/metrics/{symbol}/{name}"
        name = f"{symbol}-metrics-{day.isoformat()}.zip"
        return f"{VISION_BASE}/futures/um/daily/metrics/{symbol}/{name}"
    if kind == "bookDepth":
        if monthly:
            name = f"{symbol}-bookDepth-{day.strftime('%Y-%m')}.zip"
            return f"{VISION_BASE}/futures/um/monthly/bookDepth/{symbol}/{name}"
        name = f"{symbol}-bookDepth-{day.isoformat()}.zip"
        return f"{VISION_BASE}/futures/um/daily/bookDepth/{symbol}/{name}"
    if kind == "klines":
        if monthly:
            name = f"{symbol}-{interval}-{day.strftime('%Y-%m')}.zip"
            return f"{VISION_BASE}/spot/monthly/klines/{symbol}/{interval}/{name}"
        name = f"{symbol}-{interval}-{day.isoformat()}.zip"
        return f"{VISION_BASE}/spot/daily/klines/{symbol}/{interval}/{name}"
    raise ValueError(kind)


def checksum_url(zip_url: str) -> str:
    return zip_url + ".CHECKSUM"


def parse_checksum_text(text: str) -> str | None:
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        token = line.split()[0].strip().lower()
        if len(token) == 64 and all(ch in "0123456789abcdef" for ch in token):
            return token
    return None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_bytes(url: str, *, timeout: float, max_bytes: int, pauses: list[float] | None = None) -> tuple[int, bytes, str | None]:
    """GET one URL. Returns status, body, and error text.

    A body larger than ``max_bytes`` is refused and not returned in full.
    """
    request = Request(url, headers={"User-Agent": "trade-hist-calibration/0.1"})
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            status = int(getattr(response, "status", 200))
            chunks: list[bytes] = []
            total = 0
            while True:
                block = response.read(64 * 1024)
                if not block:
                    break
                total += len(block)
                if total > max_bytes:
                    return status, b"", f"exceeds_max_bytes:{max_bytes}"
                chunks.append(block)
            return status, b"".join(chunks), None
    except HTTPError as exc:
        if pauses is not None:
            pauses.append(0.0)
        return int(exc.code), b"", f"HTTPError:{exc.code}"
    except URLError as exc:
        return 0, b"", f"URLError:{exc.reason}"
    except TimeoutError as exc:
        return 0, b"", f"TimeoutError:{exc}"


def download_checked(
    url: str,
    dest: Path,
    *,
    timeout: float = 30.0,
    max_bytes: int = 40_000_000,
    attempts: int = 3,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """Download a zip and its CHECKSUM. Skip the zip when the cache already matches."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    check_url = checksum_url(url)
    report: dict[str, Any] = {"url": url, "checksum_url": check_url, "path": str(dest)}
    status, check_body, check_err = fetch_bytes(check_url, timeout=timeout, max_bytes=4096)
    report["checksum_status"] = status
    if check_err or status != 200:
        report["status"] = "checksum_unavailable"
        report["detail"] = check_err or f"status:{status}"
        return report
    expected = parse_checksum_text(check_body.decode("utf-8", errors="replace"))
    if expected is None:
        report["status"] = "checksum_unparsed"
        report["detail"] = check_body[:120].decode("utf-8", errors="replace")
        return report
    report["expected_sha256"] = expected
    if dest.is_file():
        cached = sha256_bytes(dest.read_bytes())
        if cached == expected:
            report["status"] = "cached"
            report["sha256"] = cached
            report["nbytes"] = dest.stat().st_size
            return report
    last_error = None
    for attempt in range(attempts):
        status, body, err = fetch_bytes(url, timeout=timeout, max_bytes=max_bytes)
        if err or status != 200 or not body:
            last_error = err or f"status:{status}"
            sleep(min(8.0, 0.5 * (2**attempt)))
            continue
        digest = sha256_bytes(body)
        if digest != expected:
            report["status"] = "checksum_mismatch"
            report["sha256"] = digest
            report["nbytes"] = len(body)
            return report
        partial = dest.with_suffix(dest.suffix + ".part")
        partial.write_bytes(body)
        partial.replace(dest)
        report["status"] = "downloaded"
        report["sha256"] = digest
        report["nbytes"] = len(body)
        return report
    report["status"] = "download_failed"
    report["detail"] = last_error
    return report


def _rows_from_zip(blob: bytes) -> tuple[list[list[str]], str | None]:
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if not names:
                return [], "zip_has_no_csv"
            text = archive.read(names[0]).decode("utf-8", errors="replace")
    except zipfile.BadZipFile:
        return [], "bad_zip"
    reader = csv.reader(io.StringIO(text))
    return [row for row in reader if row and any(cell.strip() for cell in row)], None


def epoch_to_ms(value: int) -> int:
    """Binance spot klines switched the open time to microseconds.

    Values above 1e14 are microseconds. Values above 1e11 are milliseconds.
    Smaller integers are seconds.
    """
    number = int(value)
    if number >= 10**14:
        return number // 1000
    if number >= 10**11:
        return number
    return number * 1000


def parse_clock_ms(text: str) -> int | None:
    """Integer epoch or ``YYYY-MM-DD HH:MM:SS`` interpreted as UTC."""
    raw = str(text or "").strip()
    if not raw:
        return None
    if _is_int(raw):
        return epoch_to_ms(int(raw))
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return int(parsed.timestamp() * 1000)
    return None


def _is_int(text: str) -> bool:
    try:
        int(str(text).strip())
    except (TypeError, ValueError):
        return False
    return True


def _float(text: str) -> float | None:
    try:
        value = float(str(text).strip())
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN
        return None
    return value


def parse_metrics_csv(rows: list[list[str]]) -> dict[str, Any]:
    """Split account and position top-trader ratios. Do not average them."""
    if not rows:
        return {"rows": [], "header": None, "detail": "empty"}
    header = None
    start = 0
    if not _is_int(rows[0][0]):
        header = [cell.strip() for cell in rows[0]]
        start = 1
    names = {name.lower(): index for index, name in enumerate(header or [])}
    parsed = []
    for row in rows[start:]:
        if header is None:
            if len(row) < 3:
                continue
            create_time = row[0]
            account = _float(row[1]) if len(row) > 1 else None
            position = _float(row[2]) if len(row) > 2 else None
            symbol = None
        else:
            def col(*keys: str) -> str | None:
                for key in keys:
                    index = names.get(key)
                    if index is not None and index < len(row):
                        return row[index]
                return None
            create_time = col("create_time", "timestamp", "time")
            symbol = col("symbol")
            account = _float(col("count_toptrader_long_short_ratio", "count_long_short_ratio") or "")
            position = _float(col("sum_toptrader_long_short_ratio", "sum_long_short_ratio") or "")
        create_ms = parse_clock_ms(create_time or "")
        if create_ms is None:
            continue
        parsed.append(
            {
                "symbol": None if symbol is None else symbol.strip().upper(),
                "create_time_ms": create_ms,
                "account_long_short": account,
                "position_long_short": position,
            }
        )
    return {"rows": parsed, "header": header, "detail": None, "obi_top5": None}


def parse_book_depth_csv(rows: list[list[str]]) -> dict[str, Any]:
    """Percentage-band depth. This is not a best-5 notional imbalance."""
    if not rows:
        return {"rows": [], "header": None, "series": "book_depth_percentage_band", "obi_top5": None}
    header = None
    start = 0
    if not _is_int(rows[0][0]):
        header = [cell.strip() for cell in rows[0]]
        start = 1
    names = {name.lower(): index for index, name in enumerate(header or [])}
    parsed = []
    for row in rows[start:]:
        if header is None:
            if len(row) < 3:
                continue
            ts, percentage, depth, notional = row[0], row[1], row[2], row[3] if len(row) > 3 else ""
        else:
            def col(*keys: str) -> str:
                for key in keys:
                    index = names.get(key)
                    if index is not None and index < len(row):
                        return row[index]
                return ""
            ts = col("timestamp", "time")
            percentage = col("percentage", "pct")
            depth = col("depth")
            notional = col("notional")
        ts_ms = parse_clock_ms(ts)
        pct = _float(percentage)
        if ts_ms is None or pct is None:
            continue
        parsed.append(
            {
                "ts_ms": ts_ms,
                "percentage": pct,
                "depth": _float(depth),
                "notional": _float(notional),
            }
        )
    return {
        "rows": parsed,
        "header": header,
        "series": "book_depth_percentage_band",
        "obi_top5": None,
        "not_obi_top5": True,
    }


def parse_spot_kline_csv(rows: list[list[str]], *, file_day: date | None, interval_ms: int = FIVE_MIN_MS) -> dict[str, Any]:
    """Spot klines. Filename day is a UTC open-time day, not a US session date."""
    if not rows:
        return {"rows": [], "header": None, "date_mismatches": [], "duplicate_opens": []}
    header = None
    start = 0
    if not _is_int(rows[0][0]):
        header = [cell.strip() for cell in rows[0]]
        start = 1
    parsed = []
    seen: dict[int, int] = {}
    duplicates: list[int] = []
    mismatches: list[int] = []
    for row in rows[start:]:
        if len(row) <= TAKER_BUY_QUOTE_INDEX or not _is_int(row[0]):
            continue
        open_ms = epoch_to_ms(int(row[0]))
        if open_ms in seen:
            duplicates.append(open_ms)
            continue
        seen[open_ms] = 1
        open_day = datetime.fromtimestamp(open_ms / 1000.0, tz=timezone.utc).date()
        if file_day is not None and open_day != file_day:
            mismatches.append(open_ms)
        close_ms = epoch_to_ms(int(row[6])) if len(row) > 6 and _is_int(row[6]) else open_ms + interval_ms - 1
        parsed.append(
            {
                "open_time_ms": open_ms,
                "close_time_ms": close_ms,
                "quote_volume": _float(row[7]) if len(row) > 7 else None,
                "taker_buy_quote": _float(row[TAKER_BUY_QUOTE_INDEX]),
            }
        )
    parsed.sort(key=lambda item: item["open_time_ms"])
    gaps = []
    for prev, nxt in zip(parsed, parsed[1:]):
        step = nxt["open_time_ms"] - prev["open_time_ms"]
        if step != interval_ms:
            gaps.append({"from_ms": prev["open_time_ms"], "to_ms": nxt["open_time_ms"], "step_ms": step})
    return {
        "rows": parsed,
        "header": header,
        "date_mismatches": mismatches,
        "duplicate_opens": duplicates,
        "gaps": gaps,
        "interval_ms": interval_ms,
    }


def closed_only(rows: Iterable[dict[str, Any]], *, now_ms: int, interval_ms: int = FIVE_MIN_MS) -> list[dict[str, Any]]:
    """Drop a bar whose close, or open plus the interval, is still after ``now_ms``."""
    kept = []
    for row in rows:
        close_ms = row.get("close_time_ms")
        open_ms = int(row["open_time_ms"])
        if close_ms is not None and int(close_ms) > now_ms:
            continue
        if open_ms + interval_ms > now_ms:
            continue
        kept.append(row)
    return kept


def ensure_stage(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(_STAGE_DDL)
    return conn


def stage_metrics(conn: sqlite3.Connection, symbol: str, rows: list[dict[str, Any]], source_url: str, *, prefer: str) -> dict[str, int]:
    """Insert metrics. ``prefer='daily'`` replaces a monthly row and records the overlap."""
    inserted = replaced = conflicts = 0
    for row in rows:
        key_symbol = (row.get("symbol") or symbol).upper()
        ts = int(row["create_time_ms"])
        existing = conn.execute(
            "SELECT account_long_short, position_long_short, source_url FROM um_metrics WHERE symbol=? AND create_time_ms=?",
            (key_symbol, ts),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO um_metrics (symbol, create_time_ms, account_long_short, position_long_short, source_url)
                VALUES (?, ?, ?, ?, ?)
                """,
                (key_symbol, ts, row.get("account_long_short"), row.get("position_long_short"), source_url),
            )
            inserted += 1
            continue
        same = existing[0] == row.get("account_long_short") and existing[1] == row.get("position_long_short")
        if same:
            continue
        if prefer == "daily":
            conn.execute(
                """
                UPDATE um_metrics SET account_long_short=?, position_long_short=?, source_url=?
                WHERE symbol=? AND create_time_ms=?
                """,
                (row.get("account_long_short"), row.get("position_long_short"), source_url, key_symbol, ts),
            )
            conn.execute(
                "INSERT INTO stage_conflicts (kind, key, kept_url, dropped_url, detail) VALUES (?, ?, ?, ?, ?)",
                ("um_metrics", f"{key_symbol}:{ts}", source_url, existing[2], "values_differ_kept_daily"),
            )
            replaced += 1
            conflicts += 1
        else:
            conn.execute(
                "INSERT INTO stage_conflicts (kind, key, kept_url, dropped_url, detail) VALUES (?, ?, ?, ?, ?)",
                ("um_metrics", f"{key_symbol}:{ts}", existing[2], source_url, "values_differ_kept_existing"),
            )
            conflicts += 1
    conn.commit()
    return {"inserted": inserted, "replaced": replaced, "conflicts": conflicts}


def iter_days(end: date, count: int) -> list[date]:
    return [end - timedelta(days=offset) for offset in range(count - 1, -1, -1)]
