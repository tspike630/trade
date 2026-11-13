"""Read-only calibration audit. Does not download, trade, or unlock live.

The decision gate already owns ``OnchainCalibration`` and leaves every
on-chain label ``pending`` until an attested object is passed in. This module
does not import that gate and does not write ``news_events.onchain_status``.
It only decides whether the evidence is strong enough to *emit* the same
field names. ``live_enabled`` is always false.

Event clock is ``fetched_at``. A story whose ``published_at`` is more than
90 minutes earlier is a late archive and stays out of the tradable sample.
That rule is the lag, not "drop the first 164 rows". User-reported row counts
are never copied into the result.

``data.binance.vision`` bookDepth files are percentage buckets. Their 1%
notional imbalance is stored as ``book_depth_proxy`` and is not ``obi_top5``.
Futures ``sum_taker_long_short_vol_ratio`` is not spot ``taker_buy_ratio``.
Archived klines are historical state, not a claim that the row was received
online at the bar time. ``x_watch`` is a current counter and is not joined
backward onto old headlines.

Run against local files (this environment has none)::

    PYTHONPATH=crawler python -m indicators.calibration_audit \\
        --out-dir /tmp/calibration-audit \\
        --news-db news_events.db \\
        --ccxt-db ccxt.db \\
        --features-db features.db \\
        --vision-dir /path/to/already_downloaded_vision \\
        --benchmark-symbol BTCUSDT
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sqlite3
import statistics
import urllib.parse
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

CONTRACT_MAIN_SHA = "ab2d2ebbc1301e791821e5c6cb1e6e2fc08d1cff"
LIVE_ENABLED = False
LATE_ARCHIVE_LAG_MIN = 90
TITLE_DEDUP_MS = 6 * 3600 * 1000
EMBARGO_MIN = 240
HORIZONS_MIN = (15, 60, 240)
PRIMARY_HORIZON_MIN = 60
MIN_ARM = 30
TRAIN_FRACTION = 0.6
VALIDATION_FRACTION = 0.8
ALIGNED_QUANTILE = 0.60
DIVERGENT_QUANTILE = 0.40
DELAY_BARS = (1, 2)
BAR_MS = 5 * 60 * 1000
DEFAULT_FEE_BPS = 10.0
DELAY_SIGN_AGREE_MIN = 0.70
ALPHA = 0.05
Z_975 = 1.959963984540054
REQUESTED_WINDOW_DAYS = 30
CORE_UM_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT")
COINBASE_LIMIT_NOTE = (
    "Coinbase public candles via CCXT return at most 300 rows per request. "
    "This audit does not call the API and does not extend a short table to 30 days."
)

NEWS_COLUMNS = (
    "id",
    "source",
    "source_tier",
    "title",
    "url",
    "published_at",
    "fetched_at",
    "summary",
    "raw_json",
    "onchain_status",
    "notes",
)
FEATURE_COLUMNS = (
    "event_id",
    "symbol",
    "obi_top5",
    "obi_top5_source",
    "obi_source_ts_ms",
    "taker_buy_ratio",
    "taker_source_ts_ms",
    "whale_retail_divergence",
    "whale_source_ts_ms",
    "heat_prev",
    "heat_last",
    "heat_observed_at_ms",
)
X_WATCH_COLUMNS = (
    "chain_id",
    "token_address",
    "symbol",
    "query_key",
    "mode",
    "mention_count_total",
    "mention_count_prev",
    "mention_count_last",
    "last_searched_at",
    "created_at",
    "updated_at",
)

SCHEMA_QUERIES = (
    "PRAGMA table_info(news_events);",
    "SELECT COUNT(*) AS rows FROM news_events;",
    "SELECT source_tier, COUNT(*) AS n FROM news_events GROUP BY 1;",
    "SELECT onchain_status, COUNT(*) AS n FROM news_events GROUP BY 1;",
    "SELECT SUM(CASE WHEN (julianday(fetched_at) - julianday(published_at)) * 1440.0 > 90 THEN 1 ELSE 0 END) AS late_by_lag FROM news_events;",
    "PRAGMA table_info(ccxt_ohlcv);",
    "SELECT exchange, symbol, timeframe, COUNT(*) AS bars, MIN(ts) AS first_open_ms, MAX(ts) AS last_open_ms FROM ccxt_ohlcv GROUP BY 1, 2, 3;",
    "PRAGMA table_info(x_watch);",
    "SELECT COUNT(*) AS rows, MAX(updated_at) AS latest_updated_at FROM x_watch;",
    "PRAGMA table_info(calibration_features);",
    "PRAGMA table_info(vision_spot_klines);",
    "PRAGMA table_info(vision_um_metrics);",
    "PRAGMA table_info(vision_book_depth);",
)


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def parse_time_ms(value: object) -> int | None:
    """UTC epoch milliseconds. Naive datetimes are treated as UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    number = _finite(value)
    if number is not None and not isinstance(value, str):
        return int(number if number > 10**14 else number * 1000 if number < 10**11 else number)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        raw = int(text)
        if abs(raw) < 10**11:
            return raw * 1000
        if abs(raw) > 10**14:
            return raw // 1000
        return raw
    cleaned = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def connect_ro(path: Path) -> sqlite3.Connection:
    uri = "file:" + urllib.parse.quote(path.resolve().as_posix(), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> list[str] | None:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    return [str(row[1]) for row in rows]


def pragma_snapshot(path: Path | None, tables: Sequence[str]) -> dict[str, Any]:
    if path is None:
        return {"path": None, "availability": "not_configured", "tables": {}}
    if not path.is_file():
        return {"path": str(path), "availability": "missing_db", "tables": {}}
    try:
        conn = connect_ro(path)
    except sqlite3.Error as exc:
        return {"path": str(path), "availability": "error", "detail": str(exc), "tables": {}}
    tables_out: dict[str, Any] = {}
    try:
        for name in tables:
            cols = table_columns(conn, name)
            tables_out[name] = None if cols is None else {"columns": cols}
    finally:
        conn.close()
    return {"path": str(path), "availability": "ok", "tables": tables_out}


def nearest_rank(values: Sequence[float], q: float) -> float | None:
    ordered = sorted(float(item) for item in values)
    if not ordered:
        return None
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


def sample_mean_std(values: Sequence[float]) -> tuple[float | None, float | None]:
    if len(values) < 2:
        return None, None
    return statistics.fmean(values), statistics.stdev(values)


def two_sided_z(mean: float, std: float, count: int) -> dict[str, Any] | None:
    """Normal test. Used only at or above the predeclared minimum sample."""
    if count < MIN_ARM or std <= 0 or not math.isfinite(mean) or not math.isfinite(std):
        return None
    se = std / math.sqrt(count)
    if se <= 0:
        return None
    z = mean / se
    p_value = math.erfc(abs(z) / math.sqrt(2.0))
    half = Z_975 * se
    return {
        "n": count,
        "mean": mean,
        "std": std,
        "se": se,
        "z": z,
        "p_value": p_value,
        "ci95_low": mean - half,
        "ci95_high": mean + half,
        "ci_excludes_zero": (mean - half) > 0 or (mean + half) < 0,
        "method": "normal_approx_min_n_30_sample_std",
    }


def welch_z(left: Sequence[float], right: Sequence[float]) -> dict[str, Any] | None:
    if len(left) < MIN_ARM or len(right) < MIN_ARM:
        return None
    mean_l, std_l = sample_mean_std(left)
    mean_r, std_r = sample_mean_std(right)
    if mean_l is None or mean_r is None or std_l is None or std_r is None:
        return None
    if std_l <= 0 or std_r <= 0:
        return None
    se = math.sqrt(std_l**2 / len(left) + std_r**2 / len(right))
    if se <= 0:
        return None
    diff = mean_l - mean_r
    z = diff / se
    p_value = math.erfc(abs(z) / math.sqrt(2.0))
    half = Z_975 * se
    return {
        "n_left": len(left),
        "n_right": len(right),
        "mean_left": mean_l,
        "mean_right": mean_r,
        "diff": diff,
        "se": se,
        "z": z,
        "p_value": p_value,
        "ci95_low": diff - half,
        "ci95_high": diff + half,
        "ci_excludes_zero": (diff - half) > 0 or (diff + half) < 0,
        "method": "welch_normal_approx_min_arm_30",
    }


def holm(p_values: Sequence[float]) -> list[float]:
    count = len(p_values)
    order = sorted(range(count), key=lambda index: p_values[index])
    adjusted = [1.0] * count
    running = 0.0
    for rank, index in enumerate(order):
        val = min(1.0, (count - rank) * float(p_values[index]))
        running = max(running, val)
        adjusted[index] = running
    return adjusted


def _norm_title(title: str) -> str:
    return " ".join(title.casefold().split())


def prepare_news_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Count every row. Exclude late archives by lag, not by input order."""
    counted = 0
    by_tier: dict[str, int] = {}
    by_status: dict[str, int] = {}
    late_ids: list[int] = []
    bad_time = 0
    future = 0
    rumor = 0
    eligible: list[dict[str, Any]] = []
    for row in rows:
        counted += 1
        tier = str(row.get("source_tier") or "").strip().lower()
        status = str(row.get("onchain_status") or "pending").strip().lower() or "pending"
        by_tier[tier or "missing"] = by_tier.get(tier or "missing", 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
        published_ms = parse_time_ms(row.get("published_at"))
        fetched_ms = parse_time_ms(row.get("fetched_at"))
        if published_ms is None or fetched_ms is None:
            bad_time += 1
            continue
        lag_min = (fetched_ms - published_ms) / 60_000.0
        is_late = lag_min > LATE_ARCHIVE_LAG_MIN
        if is_late:
            late_ids.append(int(row.get("id") or 0))
        if tier != "whitelist":
            rumor += 1
            continue
        if fetched_ms + 60_000 < published_ms:
            future += 1
            continue
        if is_late:
            continue
        eligible.append(
            {
                "id": row.get("id"),
                "source": str(row.get("source") or "").strip().lower(),
                "title": str(row.get("title") or ""),
                "url": str(row.get("url") or ""),
                "published_ms": published_ms,
                "fetched_ms": fetched_ms,
                "stored_onchain_status": status,
            }
        )
    eligible.sort(key=lambda item: (int(item["fetched_ms"]), str(item["url"])))
    deduped: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_titles: dict[tuple[str, str], int] = {}
    title_dropped = 0
    for event in eligible:
        url = str(event["url"])
        if url and url in seen_urls:
            continue
        key = (str(event["source"]), _norm_title(str(event["title"])))
        previous = seen_titles.get(key)
        if previous is not None and int(event["published_ms"]) - previous <= TITLE_DEDUP_MS:
            title_dropped += 1
            continue
        if url:
            seen_urls.add(url)
        seen_titles[key] = int(event["published_ms"])
        deduped.append(event)
    return {
        "rows_counted_here": counted,
        "user_reported_counts_used": False,
        "by_tier": by_tier,
        "by_stored_onchain_status": by_status,
        "stored_labels_trusted": False,
        "late_rule": "fetched_at - published_at > 90 minutes; row order is not a filter",
        "late_archive_rows": len(late_ids),
        "bad_time_rows": bad_time,
        "rumor_or_non_whitelist_rows": rumor,
        "future_vs_fetch_rows": future,
        "title_dedup_dropped": title_dropped,
        "eligible": deduped,
    }


def split_and_purge(
    events: Sequence[Mapping[str, Any]],
    *,
    embargo_ms: int,
) -> dict[str, Any]:
    """60/20/20 on fetched_at, then drop windows that cross a cut or overlap."""
    ordered = sorted(events, key=lambda item: int(item["fetched_ms"]))
    count = len(ordered)
    train_end = int(count * TRAIN_FRACTION)
    validation_end = int(count * VALIDATION_FRACTION)
    bounds = {
        "train": ordered[:train_end],
        "validation": ordered[train_end:validation_end],
        "test": ordered[validation_end:],
    }
    cut_ms: list[int] = []
    if train_end < count:
        cut_ms.append(int(ordered[train_end]["fetched_ms"]))
    if validation_end < count and validation_end != train_end:
        cut_ms.append(int(ordered[validation_end]["fetched_ms"]))

    def crosses(event: Mapping[str, Any]) -> bool:
        start = int(event["fetched_ms"])
        stop = start + embargo_ms
        return any(start < cut < stop for cut in cut_ms)

    embargo_dropped = 0
    kept: dict[str, list[dict[str, Any]]] = {}
    for name, group in bounds.items():
        survivors = []
        for event in group:
            if crosses(event):
                embargo_dropped += 1
                continue
            survivors.append(dict(event))
        purged: list[dict[str, Any]] = []
        last_ms: int | None = None
        overlap_dropped = 0
        for event in survivors:
            start = int(event["fetched_ms"])
            if last_ms is not None and start < last_ms + embargo_ms:
                overlap_dropped += 1
                continue
            purged.append(event)
            last_ms = start
        kept[name] = purged
        kept[f"{name}_overlap_dropped"] = overlap_dropped  # type: ignore[assignment]
    return {
        "input": count,
        "embargo_ms": embargo_ms,
        "embargo_dropped": embargo_dropped,
        "train": kept["train"],
        "validation": kept["validation"],
        "test": kept["test"],
        "train_overlap_dropped": kept["train_overlap_dropped"],
        "validation_overlap_dropped": kept["validation_overlap_dropped"],
        "test_overlap_dropped": kept["test_overlap_dropped"],
    }


def bar_return(
    bars: Sequence[Mapping[str, Any]],
    *,
    event_ms: int,
    horizon_min: int,
    delay_bars: int = 0,
) -> float | None:
    """Entry is the first open at or after the event plus delay. No earlier bar."""
    opens = sorted(
        (
            (int(bar["open_ms"]), float(bar["open"]))
            for bar in bars
            if _finite(bar.get("open_ms")) is not None and _finite(bar.get("open")) not in (None, 0)
        ),
        key=lambda item: item[0],
    )
    if not opens:
        return None
    start = int(event_ms) + int(delay_bars) * BAR_MS
    entry = next((item for item in opens if item[0] >= start), None)
    if entry is None or entry[1] <= 0:
        return None
    exit_at = start + int(horizon_min) * 60_000
    exit_bar = next((item for item in opens if item[0] >= exit_at), None)
    if exit_bar is None or exit_bar[0] <= entry[0]:
        return None
    return exit_bar[1] / entry[1] - 1.0


def dedup_bars(bars: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Same open timestamp with a different OHLC is a conflict, not a second bar."""
    grouped: dict[int, list[Mapping[str, Any]]] = {}
    for bar in bars:
        open_ms = parse_time_ms(bar.get("open_ms"))
        if open_ms is None:
            continue
        grouped.setdefault(open_ms, []).append(bar)
    kept: list[dict[str, Any]] = []
    conflicts = 0
    for open_ms, group in sorted(grouped.items()):
        signature = {
            (
                _finite(item.get("open")),
                _finite(item.get("high")),
                _finite(item.get("low")),
                _finite(item.get("close")),
            )
            for item in group
        }
        if len(signature) > 1:
            conflicts += 1
            continue
        first = group[0]
        kept.append(
            {
                "open_ms": open_ms,
                "open": _finite(first.get("open")),
                "high": _finite(first.get("high")),
                "low": _finite(first.get("low")),
                "close": _finite(first.get("close")),
                "volume": _finite(first.get("volume")),
                "quote_volume": _finite(first.get("quote_volume")),
            }
        )
    return {"bars": kept, "conflicts": conflicts, "duplicate_rows": sum(len(v) - 1 for v in grouped.values())}


def coverage(bars: Sequence[Mapping[str, Any]], *, bar_ms: int = BAR_MS) -> dict[str, Any]:
    opens = sorted({int(bar["open_ms"]) for bar in bars})
    if len(opens) < 2:
        return {
            "bars": len(opens),
            "first_open_ms": opens[0] if opens else None,
            "last_open_ms": opens[-1] if opens else None,
            "missing_bars": None,
            "observed_days": None,
            "claim_30d": False,
        }
    expected = (opens[-1] - opens[0]) // bar_ms + 1
    present = len(opens)
    missing = int(expected - present)
    observed_days = (opens[-1] - opens[0]) / 86_400_000
    return {
        "bars": present,
        "first_open_ms": opens[0],
        "last_open_ms": opens[-1],
        "expected_bars": int(expected),
        "missing_bars": missing,
        "observed_days": observed_days,
        "claim_30d": observed_days >= REQUESTED_WINDOW_DAYS and missing == 0,
    }


def parse_pair(symbol: str) -> tuple[str, str] | None:
    text = symbol.strip().upper().replace(" ", "")
    if not text:
        return None
    for sep in ("/", "-", "_", ":"):
        if sep in text:
            base, _, quote = text.partition(sep)
            if base and quote:
                return base, quote
    for quote in ("USDT", "USDC", "USD", "BTC"):
        if text.endswith(quote) and len(text) > len(quote):
            return text[: -len(quote)], quote
    return None


def _spot_kline_row(raw: Sequence[str], symbol: str) -> dict[str, Any] | None:
    if len(raw) < 8:
        return None
    open_ms = parse_time_ms(raw[0])
    close_ms = parse_time_ms(raw[6])
    if open_ms is None:
        return None
    return {
        "symbol": symbol,
        "open_ms": open_ms,
        "open": _finite(raw[1]),
        "high": _finite(raw[2]),
        "low": _finite(raw[3]),
        "close": _finite(raw[4]),
        "volume": _finite(raw[5]),
        "close_ms": close_ms,
        "quote_volume": _finite(raw[7]),
        "source_kind": "archived_vision_spot_kline",
        "online_at_bar": False,
    }


def parse_vision_spot_csv(text: str, symbol: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in csv.reader(io.StringIO(text)):
        if not raw or raw[0].lower() in {"open_time", "opentime"}:
            continue
        parsed = _spot_kline_row(raw, symbol)
        if parsed is not None:
            rows.append(parsed)
    return rows


def parse_vision_metrics_csv(text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text))
    out: list[dict[str, Any]] = []
    for row in reader:
        lowered = {key.strip().lower(): value for key, value in row.items() if key}
        symbol = str(lowered.get("symbol") or "").upper()
        if not symbol:
            continue
        out.append(
            {
                "symbol": symbol,
                "create_time_ms": parse_time_ms(lowered.get("create_time")),
                "sum_open_interest": _finite(lowered.get("sum_open_interest")),
                "sum_open_interest_value": _finite(lowered.get("sum_open_interest_value")),
                "futures_taker_long_short_vol_ratio": _finite(
                    lowered.get("sum_taker_long_short_vol_ratio")
                ),
                "used_as_taker_buy_ratio": False,
                "source_kind": "archived_vision_um_metrics",
                "online_at_bar": False,
            }
        )
    return out


def parse_vision_bookdepth_csv(text: str, symbol: str) -> list[dict[str, Any]]:
    """Percentage buckets. ``percentage`` is not a price level and not top-5."""
    reader = csv.DictReader(io.StringIO(text))
    buckets: dict[int, dict[float, float]] = {}
    for row in reader:
        lowered = {key.strip().lower(): value for key, value in row.items() if key}
        ts = parse_time_ms(lowered.get("timestamp") or lowered.get("time"))
        pct = _finite(lowered.get("percentage"))
        notional = _finite(lowered.get("notional"))
        if ts is None or pct is None or notional is None:
            continue
        buckets.setdefault(ts, {})[pct] = notional
    out: list[dict[str, Any]] = []
    for ts, levels in sorted(buckets.items()):
        bid = levels.get(-1.0)
        ask = levels.get(1.0)
        proxy = None
        if bid is not None and ask is not None and bid + ask > 0:
            proxy = (bid - ask) / (bid + ask)
        out.append(
            {
                "symbol": symbol,
                "timestamp_ms": ts,
                "book_depth_proxy": proxy,
                "book_depth_proxy_kind": "bookdepth_percentage_bucket_1pct_notional",
                "is_obi_top5": False,
                "source_kind": "archived_vision_bookdepth",
                "online_at_bar": False,
            }
        )
    return out


def _symbol_from_vision_name(name: str) -> str | None:
    stem = Path(name).name
    for marker in ("-bookDepth-", "-metrics-", "-5m-", "-1m-", "-15m-"):
        if marker in stem:
            return stem.split(marker, 1)[0].upper() or None
    return None


def _read_text_member(path: Path) -> Iterable[tuple[str, str]]:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as handle:
            for info in handle.infolist():
                if info.is_dir() or not info.filename.lower().endswith(".csv"):
                    continue
                yield info.filename, handle.read(info).decode("utf-8-sig", "replace")
        return
    if path.suffix.lower() == ".csv":
        yield path.name, path.read_text(encoding="utf-8-sig")


def load_vision_dir(directory: Path | None) -> dict[str, Any]:
    empty: dict[str, Any] = {
        "availability": "not_configured",
        "spot_klines": [],
        "metrics": [],
        "book_depth": [],
        "unparsed": [],
        "files": 0,
    }
    if directory is None:
        return empty
    if not directory.is_dir():
        empty["availability"] = "missing_dir"
        empty["path"] = str(directory)
        return empty
    spot: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    books: list[dict[str, Any]] = []
    unparsed: list[str] = []
    files = 0
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".csv", ".zip"}:
            continue
        files += 1
        try:
            members = list(_read_text_member(path))
        except (OSError, zipfile.BadZipFile, UnicodeError) as exc:
            unparsed.append(f"{path.name}:{type(exc).__name__}")
            continue
        for name, text in members:
            symbol = _symbol_from_vision_name(name) or _symbol_from_vision_name(path.name)
            lowered = name.lower()
            if "bookdepth" in lowered:
                if symbol is None:
                    unparsed.append(name)
                    continue
                books.extend(parse_vision_bookdepth_csv(text, symbol))
            elif "metrics" in lowered:
                metrics.extend(parse_vision_metrics_csv(text))
            elif symbol is not None and ("kline" in lowered or "-5m-" in lowered or "-1m-" in lowered):
                spot.extend(parse_vision_spot_csv(text, symbol))
            else:
                unparsed.append(name)
    present = {item["symbol"] for item in spot} | {item["symbol"] for item in metrics} | {item["symbol"] for item in books}
    return {
        "availability": "ok",
        "path": str(directory),
        "files": files,
        "spot_klines": spot,
        "metrics": metrics,
        "book_depth": books,
        "unparsed": unparsed,
        "core_symbols_missing": [symbol for symbol in CORE_UM_SYMBOLS if symbol not in present],
        "book_depth_is_obi_top5": False,
        "metrics_ratio_is_taker_buy_ratio": False,
        "online_at_bar": False,
    }


def attach_features(
    events: Sequence[Mapping[str, Any]],
    features: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_id: dict[Any, Mapping[str, Any]] = {}
    for feature in features:
        by_id[feature.get("event_id")] = feature
    attached: list[dict[str, Any]] = []
    for event in events:
        item = dict(event)
        feature = by_id.get(event.get("id"))
        item["feature_ok"] = False
        item["feature_reject"] = "no_feature_row"
        if feature is None:
            attached.append(item)
            continue
        fetched = int(event["fetched_ms"])
        window_start = fetched - LATE_ARCHIVE_LAG_MIN * 60_000
        obi_ts = parse_time_ms(feature.get("obi_source_ts_ms"))
        taker_ts = parse_time_ms(feature.get("taker_source_ts_ms"))
        whale_ts = parse_time_ms(feature.get("whale_source_ts_ms"))
        heat_ts = parse_time_ms(feature.get("heat_observed_at_ms"))
        source = str(feature.get("obi_top5_source") or "")
        times = (obi_ts, taker_ts, whale_ts, heat_ts)
        if source != "live_top5":
            item["feature_reject"] = "obi_not_live_top5"
        elif any(ts is None or ts > fetched or ts < window_start for ts in times):
            item["feature_reject"] = "feature_clock"
        else:
            values = {
                "obi_top5": _finite(feature.get("obi_top5")),
                "taker_buy_ratio": _finite(feature.get("taker_buy_ratio")),
                "whale_retail_divergence": _finite(feature.get("whale_retail_divergence")),
            }
            heat_prev = _finite(feature.get("heat_prev"))
            heat_last = _finite(feature.get("heat_last"))
            if any(value is None for value in values.values()) or heat_prev is None or heat_last is None:
                item["feature_reject"] = "feature_non_finite"
            else:
                item.update(values)
                item["heat_delta"] = heat_last - heat_prev
                item["feature_symbol"] = str(feature.get("symbol") or "")
                item["feature_ok"] = True
                item["feature_reject"] = None
        item["book_depth_proxy"] = _finite(feature.get("book_depth_proxy"))
        item["book_depth_used_as_obi_top5"] = False
        attached.append(item)
    return attached


def attach_returns(
    events: Sequence[Mapping[str, Any]],
    bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    benchmark_symbol: str | None,
    symbol_by_event: Mapping[Any, str] | None,
) -> list[dict[str, Any]]:
    target_mode = "symbol_map" if symbol_by_event else ("benchmark" if benchmark_symbol else "unmapped")
    out: list[dict[str, Any]] = []
    for event in events:
        item = dict(event)
        if symbol_by_event is not None:
            symbol = symbol_by_event.get(event.get("id"))
        else:
            symbol = benchmark_symbol
        item["price_symbol"] = symbol
        item["price_target_mode"] = target_mode
        item["returns"] = {}
        item["delay_returns"] = {}
        bars = bars_by_symbol.get(symbol or "", [])
        if symbol and bars:
            for horizon in HORIZONS_MIN:
                item["returns"][horizon] = bar_return(
                    bars, event_ms=int(event["fetched_ms"]), horizon_min=horizon
                )
                item["delay_returns"][horizon] = {
                    delay: bar_return(
                        bars,
                        event_ms=int(event["fetched_ms"]),
                        horizon_min=horizon,
                        delay_bars=delay,
                    )
                    for delay in DELAY_BARS
                }
        out.append(item)
    return out


def _fit_cuts(train: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    ready = [event for event in train if event.get("feature_ok")]
    if len(ready) < MIN_ARM:
        return None
    obi = [float(event["obi_top5"]) for event in ready]
    taker = [float(event["taker_buy_ratio"]) for event in ready]
    whale = [float(event["whale_retail_divergence"]) for event in ready]
    heat = [float(event["heat_delta"]) for event in ready]
    cuts = {
        "min_heat_delta": nearest_rank(heat, ALIGNED_QUANTILE),
        "min_obi_aligned": nearest_rank(obi, ALIGNED_QUANTILE),
        "min_taker_aligned": nearest_rank(taker, ALIGNED_QUANTILE),
        "min_whale_aligned": nearest_rank(whale, ALIGNED_QUANTILE),
        "max_obi_divergent": nearest_rank(obi, DIVERGENT_QUANTILE),
        "max_taker_divergent": nearest_rank(taker, DIVERGENT_QUANTILE),
        "max_whale_divergent": nearest_rank(whale, DIVERGENT_QUANTILE),
        "max_heat_divergent": nearest_rank(heat, DIVERGENT_QUANTILE),
        "max_feature_age_min": float(LATE_ARCHIVE_LAG_MIN),
        "fit_on": "train_feature_quantiles_not_outcome_search",
        "train_feature_rows": len(ready),
    }
    if cuts["min_obi_aligned"] <= cuts["max_obi_divergent"]:
        return None
    if cuts["min_taker_aligned"] <= cuts["max_taker_divergent"]:
        return None
    if cuts["min_whale_aligned"] <= cuts["max_whale_divergent"]:
        return None
    if cuts["min_heat_delta"] <= cuts["max_heat_divergent"]:
        return None
    return cuts


def _arm(event: Mapping[str, Any], cuts: Mapping[str, Any]) -> str:
    if not event.get("feature_ok"):
        return "incomplete"
    aligned = (
        float(event["obi_top5"]) >= float(cuts["min_obi_aligned"])
        and float(event["taker_buy_ratio"]) >= float(cuts["min_taker_aligned"])
        and float(event["whale_retail_divergence"]) >= float(cuts["min_whale_aligned"])
        and float(event["heat_delta"]) >= float(cuts["min_heat_delta"])
    )
    divergent = (
        float(event["obi_top5"]) <= float(cuts["max_obi_divergent"])
        and float(event["taker_buy_ratio"]) <= float(cuts["max_taker_divergent"])
        and float(event["whale_retail_divergence"]) <= float(cuts["max_whale_divergent"])
        and float(event["heat_delta"]) <= float(cuts["max_heat_divergent"])
    )
    if aligned and divergent:
        return "conflict"
    if aligned:
        return "aligned"
    if divergent:
        return "divergent"
    return "neither"


def _sign_agreement(primary: Sequence[float | None], delayed: Sequence[float | None]) -> float | None:
    pairs = [(left, right) for left, right in zip(primary, delayed) if left is not None and right is not None and left != 0]
    if len(pairs) < MIN_ARM:
        return None
    same = sum(1 for left, right in pairs if (left > 0) == (right > 0))
    return same / len(pairs)


def evaluate_onchain(
    splits: Mapping[str, Any],
    *,
    fee_bps: float,
) -> dict[str, Any]:
    """Freeze train quantiles. Unlock only if the OOS grid survives Holm, fee, and delay."""
    base: dict[str, Any] = {
        "calibration_status": "not_calibrated",
        "onchain_calibration": None,
        "live_enabled": LIVE_ENABLED,
        "reason": "not_evaluated",
    }
    train = list(splits["train"])
    test = list(splits["test"])
    if train and train[0].get("price_target_mode") == "unmapped":
        base["reason"] = "no_symbol_map_or_benchmark"
        return base
    cuts = _fit_cuts(train)
    if cuts is None:
        base["reason"] = "train_features_below_min_or_cuts_overlap"
        return base
    tests: list[dict[str, Any]] = []
    p_values: list[float] = []
    for horizon in HORIZONS_MIN:
        aligned_returns = []
        divergent_returns = []
        aligned_delay: dict[int, list[float | None]] = {delay: [] for delay in DELAY_BARS}
        aligned_primary_for_delay: dict[int, list[float | None]] = {delay: [] for delay in DELAY_BARS}
        for event in test:
            side = _arm(event, cuts)
            ret = (event.get("returns") or {}).get(horizon)
            if side == "aligned" and ret is not None:
                aligned_returns.append(float(ret))
                for delay in DELAY_BARS:
                    delayed = ((event.get("delay_returns") or {}).get(horizon) or {}).get(delay)
                    aligned_delay[delay].append(None if delayed is None else float(delayed))
                    aligned_primary_for_delay[delay].append(float(ret))
            elif side == "divergent" and ret is not None:
                divergent_returns.append(float(ret))
        comparison = welch_z(aligned_returns, divergent_returns)
        fee = fee_bps / 10_000.0
        aligned_after = [value - fee for value in aligned_returns]
        divergent_after = [value - fee for value in divergent_returns]
        mean_a, std_a = sample_mean_std(aligned_after)
        mean_d, std_d = sample_mean_std(divergent_after)
        aligned_fee = None if mean_a is None or std_a is None else two_sided_z(mean_a, std_a, len(aligned_after))
        divergent_fee = None if mean_d is None or std_d is None else two_sided_z(mean_d, std_d, len(divergent_after))
        agreements = {
            str(delay): _sign_agreement(aligned_primary_for_delay[delay], aligned_delay[delay])
            for delay in DELAY_BARS
        }
        cell = {
            "horizon_min": horizon,
            "n_aligned": len(aligned_returns),
            "n_divergent": len(divergent_returns),
            "comparison": comparison,
            "aligned_after_fee": aligned_fee,
            "divergent_after_fee": divergent_fee,
            "delay_sign_agreement": agreements,
            "eligible": comparison is not None,
        }
        tests.append(cell)
        if comparison is not None:
            p_values.append(float(comparison["p_value"]))
    if not p_values:
        base["reason"] = "oos_arms_below_min_or_zero_variance"
        base["tests"] = tests
        base["cuts_frozen_but_not_emitted"] = {key: cuts[key] for key in cuts if key.startswith("min_") or key.startswith("max_")}
        return base
    adjusted = holm(p_values)
    cursor = 0
    for cell in tests:
        if not cell["eligible"]:
            cell["p_holm"] = None
            continue
        cell["p_holm"] = adjusted[cursor]
        cursor += 1
    primary = next(cell for cell in tests if cell["horizon_min"] == PRIMARY_HORIZON_MIN)
    reasons: list[str] = []
    comparison = primary.get("comparison")
    if comparison is None or primary.get("p_holm") is None or primary["p_holm"] >= ALPHA:
        reasons.append("primary_not_significant_after_holm")
    elif not comparison["ci_excludes_zero"] or comparison["diff"] <= 0:
        reasons.append("primary_ci_or_sign")
    fee_aligned = primary.get("aligned_after_fee")
    fee_divergent = primary.get("divergent_after_fee")
    if (
        fee_aligned is None
        or fee_divergent is None
        or fee_aligned["ci95_low"] <= 0
        or fee_divergent["ci95_high"] >= 0
    ):
        reasons.append("fee_sensitivity")
    agreements = primary.get("delay_sign_agreement") or {}
    if any(value is None or value < DELAY_SIGN_AGREE_MIN for value in agreements.values()):
        reasons.append("delay_sensitivity")
    base["tests"] = tests
    base["holm_family"] = "horizons_15_60_240_aligned_minus_divergent"
    if reasons:
        base["reason"] = ",".join(reasons)
        base["cuts_frozen_but_not_emitted"] = {
            "min_heat_delta": cuts["min_heat_delta"],
            "min_obi_aligned": cuts["min_obi_aligned"],
            "min_taker_aligned": cuts["min_taker_aligned"],
            "min_whale_aligned": cuts["min_whale_aligned"],
            "max_obi_divergent": cuts["max_obi_divergent"],
            "max_taker_divergent": cuts["max_taker_divergent"],
            "max_whale_divergent": cuts["max_whale_divergent"],
            "max_feature_age_min": cuts["max_feature_age_min"],
        }
        return base
    base["calibration_status"] = "oos_significant"
    base["reason"] = "primary_60m_survives_holm_fee_and_delay"
    base["onchain_calibration"] = {
        "min_heat_delta": cuts["min_heat_delta"],
        "min_obi_aligned": cuts["min_obi_aligned"],
        "min_taker_aligned": cuts["min_taker_aligned"],
        "min_whale_aligned": cuts["min_whale_aligned"],
        "max_obi_divergent": cuts["max_obi_divergent"],
        "max_taker_divergent": cuts["max_taker_divergent"],
        "max_whale_divergent": cuts["max_whale_divergent"],
        "max_feature_age_min": cuts["max_feature_age_min"],
    }
    return base


def audit_ccxt_rows(rows: Sequence[Mapping[str, Any]], *, fee_bps: float) -> dict[str, Any]:
    """Per venue, per quote. USD and USDT are not one series."""
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        if str(row.get("timeframe") or "5m") not in {"5m", "5"}:
            continue
        pair = parse_pair(str(row.get("symbol") or ""))
        if pair is None:
            continue
        base, quote = pair
        exchange = str(row.get("exchange") or "").strip().lower()
        open_ms = parse_time_ms(row.get("ts") if row.get("open_ms") is None else row.get("open_ms"))
        if open_ms is None:
            continue
        grouped.setdefault((exchange, base, quote), []).append(
            {
                "open_ms": open_ms,
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "volume": row.get("volume"),
                "quote_volume": row.get("quote_volume"),
            }
        )
    series: list[dict[str, Any]] = []
    for (exchange, base, quote), bars in sorted(grouped.items()):
        cleaned = dedup_bars(bars)
        kept = cleaned["bars"]
        span = coverage(kept)
        quote_missing = sum(1 for bar in kept if bar.get("quote_volume") is None)
        wicks = []
        breakouts = []
        previous: dict[str, Any] | None = None
        for bar in kept:
            if bar["open"] is None or bar["high"] is None or bar["low"] is None or bar["close"] is None:
                previous = None
                continue
            if bar["open"] > 0:
                wicks.append((min(bar["open"], bar["close"]) - bar["low"]) / bar["open"])
            if (
                previous is not None
                and int(bar["open_ms"]) - int(previous["open_ms"]) == BAR_MS
                and previous["high"] not in (None, 0)
                and bar["close"] > previous["high"]
            ):
                breakouts.append((bar["close"] - previous["high"]) / previous["high"] * 10_000.0)
            previous = bar
        fee = fee_bps
        surviving = [value for value in breakouts if value - fee > 0]
        note = COINBASE_LIMIT_NOTE if exchange == "coinbase" else None
        series.append(
            {
                "exchange": exchange,
                "base": base,
                "quote": quote,
                "volume_unit": "quote_volume" if quote_missing == 0 and kept else "base_volume_only",
                "quote_volume_missing_bars": quote_missing,
                "conflicts": cleaned["conflicts"],
                "coverage": span,
                "claim_30d": bool(span["claim_30d"]) and cleaned["conflicts"] == 0,
                "coinbase_limit_note": note,
                "lower_wick_n": len(wicks),
                "lower_wick_mean": statistics.fmean(wicks) if wicks else None,
                "up_breakout_bps_n": len(breakouts),
                "up_breakout_bps_mean": statistics.fmean(breakouts) if breakouts else None,
                "breakouts_surviving_fee": len(surviving),
                "fee_bps": fee_bps,
                "pooled_with_other_quotes": False,
            }
        )
    by_base_time: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for (exchange, base, quote), bars in grouped.items():
        for bar in dedup_bars(bars)["bars"]:
            if bar.get("close") is None:
                continue
            by_base_time.setdefault((base, int(bar["open_ms"])), []).append(
                {"exchange": exchange, "quote": quote, "close": bar["close"]}
            )
    compared = 0
    basis = 0
    observations: list[dict[str, Any]] = []
    for (_base, open_ms), points in by_base_time.items():
        for left in points:
            for right in points:
                if left["exchange"] >= right["exchange"]:
                    continue
                if left["quote"] != right["quote"]:
                    basis += 1
                    continue
                if right["close"] in (None, 0):
                    continue
                compared += 1
                observations.append(
                    {
                        "fetched_ms": open_ms,
                        "excess": abs((left["close"] - right["close"]) / right["close"] * 10_000.0) - fee_bps,
                    }
                )
    purged = split_and_purge(observations, embargo_ms=EMBARGO_MIN * 60_000)
    test_values = [float(item["excess"]) for item in purged["test"]]
    mean, std = sample_mean_std(test_values) if len(test_values) >= 2 else (None, None)
    test = None if mean is None or std is None else two_sided_z(mean, std, len(test_values))
    significant = bool(
        test
        and test["ci_excludes_zero"]
        and test["mean"] > 0
        and test["p_value"] < ALPHA
    )
    return {
        "series": series,
        "same_quote_comparisons": compared,
        "usd_usdt_pairs_not_pooled": basis,
        "oos_deviation_rows": len(test_values),
        "oos_fee_excess_bps_mean": mean,
        "fee_excess_test": test,
        "deviation_survives_fee": significant,
        "calibration_status": "oos_significant" if significant else "not_calibrated",
        "live_enabled": LIVE_ENABLED,
        "note": "Same-quote close deviation only. A short Coinbase table is not padded to 30 days.",
    }


def _load_news_db(path: Path | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if path is None:
        return [], {"availability": "not_configured"}
    if not path.is_file():
        return [], {"availability": "missing_db", "path": str(path)}
    conn = connect_ro(path)
    try:
        cols = table_columns(conn, "news_events")
        if cols is None:
            return [], {"availability": "schema_error", "detail": "news_events absent", "path": str(path)}
        missing = [name for name in ("published_at", "fetched_at", "source_tier", "url", "title") if name not in cols]
        if missing:
            return [], {"availability": "schema_error", "missing_columns": missing, "path": str(path)}
        selected = [name for name in NEWS_COLUMNS if name in cols]
        rows = [dict(row) for row in conn.execute(f"SELECT {', '.join(selected)} FROM news_events")]
    except sqlite3.Error as exc:
        return [], {"availability": "error", "detail": str(exc), "path": str(path)}
    finally:
        conn.close()
    return rows, {"availability": "ok", "path": str(path), "columns": cols}


def _load_feature_db(path: Path | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if path is None:
        return [], {"availability": "not_configured"}
    if not path.is_file():
        return [], {"availability": "missing_db", "path": str(path)}
    conn = connect_ro(path)
    try:
        cols = table_columns(conn, "calibration_features")
        if cols is None:
            return [], {"availability": "schema_error", "detail": "calibration_features absent", "path": str(path)}
        missing = [name for name in FEATURE_COLUMNS if name not in cols]
        if missing:
            return [], {"availability": "schema_error", "missing_columns": missing, "path": str(path)}
        rows = [dict(row) for row in conn.execute(f"SELECT {', '.join(FEATURE_COLUMNS)} FROM calibration_features")]
        symbol_map = {
            row["event_id"]: str(row["symbol"])
            for row in rows
            if row.get("symbol")
        }
    except sqlite3.Error as exc:
        return [], {"availability": "error", "detail": str(exc), "path": str(path)}
    finally:
        conn.close()
    return rows, {"availability": "ok", "path": str(path), "symbol_by_event": symbol_map}


def _load_ccxt(path: Path | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if path is None:
        return [], {"availability": "not_configured"}
    if not path.is_file():
        return [], {"availability": "missing_db", "path": str(path)}
    conn = connect_ro(path)
    try:
        cols = table_columns(conn, "ccxt_ohlcv")
        if cols is None:
            return [], {"availability": "schema_error", "detail": "ccxt_ohlcv absent", "path": str(path)}
        needed = ("exchange", "symbol", "timeframe", "ts", "open", "high", "low", "close")
        missing = [name for name in needed if name not in cols]
        if missing:
            return [], {"availability": "schema_error", "missing_columns": missing, "path": str(path)}
        quote = ", quote_volume" if "quote_volume" in cols else ""
        volume = ", volume" if "volume" in cols else ""
        rows = [
            dict(row)
            for row in conn.execute(
                f"SELECT exchange, symbol, timeframe, ts, open, high, low, close{volume}{quote} FROM ccxt_ohlcv"
            )
        ]
    except sqlite3.Error as exc:
        return [], {"availability": "error", "detail": str(exc), "path": str(path)}
    finally:
        conn.close()
    return rows, {"availability": "ok", "path": str(path)}


def _x_watch_inventory(path: Path | None) -> dict[str, Any]:
    snap = pragma_snapshot(path, ("x_watch",))
    info: dict[str, Any] = {
        "availability": snap["availability"],
        "path": snap.get("path"),
        "history_snapshots": False,
        "joined_to_past_news": False,
        "rows": None,
        "latest_updated_at": None,
    }
    if snap["availability"] != "ok" or not path:
        return info
    cols = (snap["tables"].get("x_watch") or {}).get("columns")
    if not cols:
        info["availability"] = "schema_error"
        return info
    conn = connect_ro(path)
    try:
        row = conn.execute("SELECT COUNT(*) AS n, MAX(updated_at) AS latest FROM x_watch").fetchone()
        info["rows"] = int(row["n"])
        info["latest_updated_at"] = row["latest"]
        info["history_table_present"] = table_columns(conn, "x_watch_history") is not None
    except sqlite3.Error as exc:
        info["availability"] = "error"
        info["detail"] = str(exc)
    finally:
        conn.close()
    return info


def build_report(
    *,
    news_rows: Sequence[Mapping[str, Any]] | None = None,
    feature_rows: Sequence[Mapping[str, Any]] | None = None,
    ccxt_rows: Sequence[Mapping[str, Any]] | None = None,
    bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    vision: Mapping[str, Any] | None = None,
    benchmark_symbol: str | None = None,
    symbol_by_event: Mapping[Any, str] | None = None,
    fee_bps: float = DEFAULT_FEE_BPS,
    news_info: Mapping[str, Any] | None = None,
    feature_info: Mapping[str, Any] | None = None,
    ccxt_info: Mapping[str, Any] | None = None,
    x_watch: Mapping[str, Any] | None = None,
    pragma: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    news_part = prepare_news_rows(news_rows or [])
    events = news_part.pop("eligible")
    if feature_rows is not None:
        events = attach_features(events, feature_rows)
    else:
        events = [dict(event, feature_ok=False, feature_reject="features_not_configured") for event in events]
    if symbol_by_event is None and feature_info and feature_info.get("symbol_by_event"):
        symbol_by_event = feature_info["symbol_by_event"]
    events = attach_returns(
        events,
        bars_by_symbol or {},
        benchmark_symbol=benchmark_symbol,
        symbol_by_event=symbol_by_event,
    )
    splits = split_and_purge(events, embargo_ms=EMBARGO_MIN * 60_000)
    onchain = evaluate_onchain(splits, fee_bps=fee_bps)
    if ccxt_info and ccxt_info.get("availability") not in {None, "ok"} and not ccxt_rows:
        cross = {
            "availability": ccxt_info.get("availability"),
            "path": ccxt_info.get("path"),
            "detail": ccxt_info.get("detail"),
            "missing_columns": ccxt_info.get("missing_columns"),
            "calibration_status": "not_calibrated",
            "live_enabled": LIVE_ENABLED,
        }
    elif ccxt_rows is None:
        cross = {
            "availability": "not_configured",
            "calibration_status": "not_calibrated",
            "live_enabled": LIVE_ENABLED,
        }
    else:
        cross = audit_ccxt_rows(ccxt_rows, fee_bps=fee_bps)
        cross["availability"] = (ccxt_info or {}).get("availability", "in_memory")
    return {
        "contract_main_sha": CONTRACT_MAIN_SHA,
        "live_enabled": LIVE_ENABLED,
        "thresholds_changed": False,
        "writes_news_events": False,
        "news": {
            **(dict(news_info) if news_info else {"availability": "in_memory" if news_rows is not None else "not_configured"}),
            **news_part,
            "eligible_after_lag_and_dedup": splits["input"],
            "split_counts": {
                "train": len(splits["train"]),
                "validation": len(splits["validation"]),
                "test": len(splits["test"]),
                "embargo_dropped": splits["embargo_dropped"],
                "train_overlap_dropped": splits["train_overlap_dropped"],
                "validation_overlap_dropped": splits["validation_overlap_dropped"],
                "test_overlap_dropped": splits["test_overlap_dropped"],
            },
            "onchain": onchain,
        },
        "features": dict(feature_info) if feature_info else {"availability": "in_memory" if feature_rows is not None else "not_configured"},
        "x_watch": dict(x_watch) if x_watch else {"availability": "not_configured", "history_snapshots": False, "joined_to_past_news": False},
        "cross_exchange": cross,
        "vision": dict(vision) if vision else {"availability": "not_configured", "book_depth_is_obi_top5": False},
        "schema_queries": list(SCHEMA_QUERIES),
        "pragma": dict(pragma) if pragma else {},
        "price_clock": "entry is the first 5m open at or after fetched_at; a bar that opened earlier is not used",
        "archive_clock_claim": "vision and any bar without a receipt timestamp are historical state, not an online fetch at the bar",
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    news = report["news"]
    onchain = news["onchain"]
    lines = [
        "# Calibration audit",
        "",
        f"- Contract read against main `{report['contract_main_sha']}`.",
        f"- `live_enabled`: `{report['live_enabled']}`. Thresholds and `news_events` are not written.",
        f"- News availability: `{news.get('availability')}`. Rows counted here: `{news.get('rows_counted_here')}`.",
        f"- Late rule: {news.get('late_rule')}. Late rows: `{news.get('late_archive_rows')}`.",
        "- User-reported 1859 and 164 are not inputs.",
        f"- On-chain status: `{onchain.get('calibration_status')}` ({onchain.get('reason')}).",
        f"- Cross-exchange status: `{report['cross_exchange'].get('calibration_status')}`.",
        f"- Vision bookDepth is obi_top5: `{report['vision'].get('book_depth_is_obi_top5', False)}`.",
        "",
        "## Schema queries",
        "",
        "Run these read-only statements on the local databases. Fill the JSON only from their output.",
        "",
    ]
    for query in report["schema_queries"]:
        lines.append(f"- `{query}`")
    lines.extend(
        [
            "",
            "## Split",
            "",
            "```json",
            json.dumps(news.get("split_counts"), indent=2, sort_keys=True),
            "```",
            "",
            "On-chain object is present only when status is `oos_significant`.",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def _json_ready(value: Any) -> Any:
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return value
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def write_outputs(report: Mapping[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = _json_ready(report)
    # Drop bulky bar payloads from the vision echo; keep counts.
    vision = payload.get("vision") or {}
    if "spot_klines" in vision:
        vision["spot_kline_rows"] = len(vision.pop("spot_klines"))
    if "metrics" in vision:
        vision["metrics_rows"] = len(vision.pop("metrics"))
    if "book_depth" in vision:
        vision["book_depth_rows"] = len(vision.pop("book_depth"))
    (out_dir / "calibration_audit.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "calibration_audit.md").write_text(render_markdown(payload), encoding="utf-8")


def run_from_paths(
    *,
    news_db: Path | None,
    ccxt_db: Path | None,
    features_db: Path | None,
    x_watch_db: Path | None,
    vision_dir: Path | None,
    benchmark_symbol: str | None,
    fee_bps: float,
) -> dict[str, Any]:
    news_rows, news_info = _load_news_db(news_db)
    feature_rows, feature_info = _load_feature_db(features_db)
    ccxt_rows, ccxt_info = _load_ccxt(ccxt_db)
    vision = load_vision_dir(vision_dir)
    bars: dict[str, list[dict[str, Any]]] = {}
    for row in vision.get("spot_klines") or []:
        bars.setdefault(row["symbol"], []).append(row)
    return build_report(
        news_rows=news_rows,
        feature_rows=feature_rows if feature_info.get("availability") == "ok" else [],
        ccxt_rows=ccxt_rows if ccxt_info.get("availability") == "ok" else [],
        bars_by_symbol=bars,
        vision=vision,
        benchmark_symbol=benchmark_symbol,
        symbol_by_event=feature_info.get("symbol_by_event"),
        fee_bps=fee_bps,
        news_info=news_info,
        feature_info=feature_info,
        ccxt_info=ccxt_info,
        x_watch=_x_watch_inventory(x_watch_db),
        pragma={
            "news": pragma_snapshot(news_db, ("news_events",)),
            "ccxt": pragma_snapshot(ccxt_db, ("ccxt_ohlcv",)),
            "features": pragma_snapshot(features_db, ("calibration_features",)),
            "x_watch": pragma_snapshot(x_watch_db, ("x_watch",)),
        },
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a read-only calibration audit. Does not download or trade.")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--news-db", default="")
    parser.add_argument("--ccxt-db", default="")
    parser.add_argument("--features-db", default="")
    parser.add_argument("--x-watch-db", default="")
    parser.add_argument("--vision-dir", default="")
    parser.add_argument("--benchmark-symbol", default="")
    parser.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS)
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = run_from_paths(
        news_db=Path(args.news_db) if args.news_db else None,
        ccxt_db=Path(args.ccxt_db) if args.ccxt_db else None,
        features_db=Path(args.features_db) if args.features_db else None,
        x_watch_db=Path(args.x_watch_db) if args.x_watch_db else None,
        vision_dir=Path(args.vision_dir) if args.vision_dir else None,
        benchmark_symbol=args.benchmark_symbol.strip().upper() or None,
        fee_bps=float(args.fee_bps),
    )
    write_outputs(report, Path(args.out_dir))
    print(
        f"calibration_status={report['news']['onchain']['calibration_status']} "
        f"cross={report['cross_exchange'].get('calibration_status')} "
        f"live_enabled={report['live_enabled']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
