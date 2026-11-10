"""Point-in-time description of news shocks. Does not enable a threshold.

The report stays ``not_calibrated``. Descriptive returns are not a suggestion
to turn on rank weights. A current ``x_watch`` row is never treated as the
heat that existed at an older headline.

Join rules, all evaluated per event time ``t``:

- Price entry is the first bar whose open time is ``>= t``. Bars that opened
  earlier are not used, including their close.
- Exit is the first later bar whose open time is ``>= t + horizon``.
- Book and whale features must have ``ts_ms <= t`` and be no older than the
  90 minute evidence window. A later snapshot is not copied backward.
- Heat needs two ``indicator_history`` points at or before ``t``. ``x_watch``
  counters are refused.
- ``news_events`` has no symbol. Without ``--benchmark-symbol`` or a symbol
  map, price and book joins are untestable rather than assumed.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from decision_engine.core.news_shock_gate import (
    CRISIS_PHRASES,
    EVIDENCE_MAX_AGE_MIN,
    HAWKISH_PHRASES,
    LATE_ARCHIVE_LAG_MIN,
    classify_news_row,
    open_readonly,
    parse_ts,
    resolve_news_db_path,
    table_columns,
)

HORIZONS_MIN = (5, 15, 60, 240, 1440)
BAR_MIN = 5

# Cited from the parent read-only inventory at 2026-11-10 14:37:32 HKT.
# This process does not have those databases and does not recompute the figures.
LOCAL_INVENTORY_2026_11_10: dict[str, Any] = {
    "recomputed_here": False,
    "audit_time_hkt": "2026-11-10 14:37:32 HKT",
    "news_rows": 1869,
    "prior_count_not_used": 1859,
    "whitelist": 1645,
    "rumor": 224,
    "url_duplicate_groups": 0,
    "title_duplicate_groups": 0,
    "onchain_status_pending": 1869,
    "aligned_or_divergent_labels": 0,
    "published_before_first_fetch": 164,
    "no_symbol_column": True,
    "explicit_usdt_pair_mentions": {"CCUSDT": 3},
    "official_fed_rss_in_stored_sources": False,
    "standalone_x_news_db": False,
    "x_watch_rows": 64,
    "x_watch_stale_as_of_audit": True,
    "indicator_history_heat_rows": 24187,
    "indicator_history_heat_tokens": 290,
    "heat_window": "2026-10-10 through 2026-10-31; mentions capped at 10; no join to news_events",
    "obi_top5_column": False,
    "orderbook_imbalance_20": "depth-20, about [-1, 1], from 2026-11-09 10:33:54 HKT, 14 symbols; not obi_top5",
    "whale_retail_divergence": "binance_perp_counterparty, 25 symbols, 2026-11-09 09:35:06 HKT to 2026-11-10 13:53:39 HKT, observed [0.378, 3.139], not [-1, 1]",
    "volume_z_column": False,
    "binance_klines": "5m only, 507 symbols, 2026-11-03 21:50 HKT to 2026-11-10 14:25 HKT",
    "binance_5m_aligned_news": 375,
    "okx_5m_aligned_news_separate": 723,
    "venues_not_pooled": True,
    "binance_btcusdt": {
        "n": 375,
        "ret_5m_mean": -0.000691,
        "ret_60m_n": 372,
        "ret_60m_mean": -0.001164,
        "ret_1440m_n": 245,
        "ret_1440m_mean": -0.003946,
    },
    "crisis_terms_in_binance_window": 0,
    "fed_or_hawkish_terms_in_binance_window": 1,
    "false_positive_rate": None,
}
MIN_TEST_EVENTS = 30
MIN_FEATURE_COVERAGE = 0.8
CONTROL_LAG_MS = 7 * 24 * 3600 * 1000
HEAT_LOOKBACK_MS = 24 * 3600 * 1000
TITLE_DEDUP_MS = 6 * 3600 * 1000
_BAR_TIMEFRAMES = ("1m", "5m", "15m", "1h")


def _as_of(now: datetime | None) -> datetime:
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return clock.astimezone(timezone.utc)


def _norm_title(title: str) -> str:
    return " ".join(title.casefold().split())


def wilson_interval(successes: int, total: int, z: float = 1.96) -> list[float] | None:
    if total <= 0:
        return None
    proportion = successes / total
    denom = 1.0 + z * z / total
    center = (proportion + z * z / (2.0 * total)) / denom
    margin = z * math.sqrt((proportion * (1.0 - proportion) + z * z / (4.0 * total)) / total) / denom
    return [max(0.0, center - margin), min(1.0, center + margin)]


def quantile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return ordered[low]
    weight = pos - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def asof_numeric(
    points: Sequence[tuple[int, float]],
    event_ms: int,
    max_age_ms: int,
) -> tuple[int, float] | None:
    """Latest point at or before ``event_ms`` inside the evidence window."""
    best: tuple[int, float] | None = None
    for ts_ms, value in points:
        if ts_ms > event_ms or event_ms - ts_ms > max_age_ms:
            continue
        if best is None or ts_ms > best[0]:
            best = (ts_ms, value)
    return best


def heat_delta(
    points: Sequence[tuple[int, float]],
    event_ms: int,
    max_age_ms: int,
) -> float | None:
    """Change between the last two heat prints at or before the event.

    One print is a level, not an evolution. A print after the event is ignored.
    """
    eligible = sorted((ts_ms, value) for ts_ms, value in points if ts_ms <= event_ms)
    if len(eligible) < 2:
        return None
    latest_ts, latest_value = eligible[-1]
    if event_ms - latest_ts > max_age_ms:
        return None
    previous: tuple[int, float] | None = None
    for ts_ms, value in eligible[:-1]:
        if latest_ts - ts_ms <= HEAT_LOOKBACK_MS:
            previous = (ts_ms, value)
    if previous is None:
        return None
    return latest_value - previous[1]


def kline_forward_return(
    bars: Sequence[tuple[int, float | None, float | None]],
    event_ms: int,
    horizon_min: int,
    *,
    bar_min: int = BAR_MIN,
) -> float | None:
    """Return exit_close / entry_open - 1.

    Entry is the open of the first bar whose open time is at or after the
    event. A bar that opened earlier is not used, even if it closes later.
    Exit is the close of the bar that ends at entry_open + horizon.
    """
    ordered = sorted(bars, key=lambda item: item[0])
    entry: tuple[int, float | None, float | None] | None = None
    for ts_ms, open_px, close_px in ordered:
        if ts_ms >= event_ms and open_px is not None:
            entry = (ts_ms, open_px, close_px)
            break
    if entry is None or entry[1] in (None, 0):
        return None
    exit_open_ms = entry[0] + (horizon_min - bar_min) * 60_000
    if exit_open_ms < entry[0]:
        return None
    exit_bar: tuple[int, float | None, float | None] | None = None
    for ts_ms, open_px, close_px in ordered:
        if ts_ms >= exit_open_ms and ts_ms >= entry[0]:
            exit_bar = (ts_ms, open_px, close_px)
            break
    if exit_bar is None or exit_bar[0] > exit_open_ms + bar_min * 60_000:
        return None
    if exit_bar[2] is None:
        return None
    return exit_bar[2] / float(entry[1]) - 1.0


def forward_volume(
    bars: Sequence[tuple[int, float | None]],
    event_ms: int,
    horizon_min: int,
) -> float | None:
    horizon_ms = event_ms + horizon_min * 60_000
    total = 0.0
    seen = False
    for ts_ms, volume in bars:
        if ts_ms < event_ms or ts_ms >= horizon_ms or volume is None:
            continue
        total += volume
        seen = True
    if not seen:
        return None
    return total


def dedup_events(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(events, key=lambda item: (int(item["published_ms"]), str(item.get("url") or "")))
    seen_urls: set[str] = set()
    seen_titles: dict[tuple[str, str], int] = {}
    kept: list[dict[str, Any]] = []
    for event in ordered:
        url = str(event.get("url") or "")
        if url and url in seen_urls:
            continue
        title_key = (str(event.get("source") or ""), _norm_title(str(event.get("title") or "")))
        published_ms = int(event["published_ms"])
        previous = seen_titles.get(title_key)
        if previous is not None and published_ms - previous <= TITLE_DEDUP_MS:
            continue
        if url:
            seen_urls.add(url)
        seen_titles[title_key] = published_ms
        kept.append(event)
    return kept


def _event_clock_ms(item: Mapping[str, Any]) -> int:
    """Tradable clock. ``fetched_at`` when present; never a late ``published_at``."""
    return int(item.get("event_ms") or item["published_ms"])


def time_split(events: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Chronological 60/20/20 split on fetched_at. The test slice is the latest known rows."""
    ordered = sorted(events, key=_event_clock_ms)
    count = len(ordered)
    train_end = int(count * 0.6)
    validation_end = int(count * 0.8)
    return {
        "train": ordered[:train_end],
        "validation": ordered[train_end:validation_end],
        "test": ordered[validation_end:],
    }


def _table_points(
    path: Path | None,
    timeout_sec: float,
) -> tuple[sqlite3.Connection | None, str | None]:
    if path is None:
        return None, "not_configured"
    if not path.exists():
        return None, "missing_db"
    conn, failed, detail = open_readonly(path, timeout_sec)
    if conn is None:
        return None, detail or failed or "error"
    return conn, None


def _load_news(path: Path, *, now: datetime, timeout_sec: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    info: dict[str, Any] = {"path": str(path), "availability": "ok"}
    if not path.exists():
        info["availability"] = "missing_db"
        return [], info
    conn, detail = _table_points(path, timeout_sec)
    if conn is None:
        info["availability"] = "error"
        info["detail"] = detail
        return [], info
    try:
        columns = table_columns(conn, "news_events")
        if columns is None:
            info["availability"] = "schema_error"
            info["detail"] = "news_events absent"
            return [], info
        rows = conn.execute(
            """
            SELECT id, source, source_tier, title, url, published_at, fetched_at, summary,
                   onchain_status
            FROM news_events
            """
        ).fetchall()
    except sqlite3.Error as exc:
        info["availability"] = "error"
        info["detail"] = str(exc)
        return [], info
    finally:
        conn.close()

    events: list[dict[str, Any]] = []
    rumor = 0
    future = 0
    bad_time = 0
    late_archive = 0
    pending_labels = 0
    labeled = 0
    as_of_ms = int(now.timestamp() * 1000)
    for row in rows:
        tier = str(row["source_tier"] or "").strip().lower()
        label = str(row["onchain_status"] or "").strip().lower()
        if label in {"aligned", "divergent"}:
            labeled += 1
        else:
            pending_labels += 1
        if tier != "whitelist":
            rumor += 1
            continue
        status = classify_news_row(row, now=now, max_age_min=None)
        if status.suppressed_reason in {"future", "future_vs_fetch"}:
            future += 1
            continue
        published = parse_ts(row["published_at"])
        fetched = parse_ts(row["fetched_at"])
        if fetched is None or published is None:
            bad_time += 1
            continue
        published_ms = int(published.timestamp() * 1000)
        event_ms = int(fetched.timestamp() * 1000)
        if event_ms > as_of_ms or published_ms > as_of_ms + 60_000:
            future += 1
            continue
        lag_min = (event_ms - published_ms) / 60_000.0
        is_late = lag_min > LATE_ARCHIVE_LAG_MIN
        if is_late:
            late_archive += 1
        events.append(
            {
                "id": row["id"],
                "source": str(row["source"] or "").strip().lower(),
                "title": str(row["title"] or ""),
                "summary": str(row["summary"] or ""),
                "url": str(row["url"] or ""),
                "published_at": str(row["published_at"] or ""),
                "fetched_at": str(row["fetched_at"] or ""),
                "published_ms": published_ms,
                "event_ms": event_ms,
                "late_archive": is_late,
                "tradable_at_published_at": not is_late,
                "onchain_status": label or "pending",
                "severity": status.severity,
                "matched_keyword": status.matched_keyword,
            }
        )
    info["rumor_ignored"] = rumor
    info["future_ignored"] = future
    info["bad_published_at"] = bad_time
    info["late_archive_rows"] = late_archive
    info["onchain_pending_or_unlabeled"] = pending_labels
    info["onchain_labeled"] = labeled
    info["false_positive_rate_computable"] = False
    info["rows"] = len(rows)
    return events, info


def _decode_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", "replace")
    text = str(value).strip()
    return text or None


def _load_ccxt(conn: sqlite3.Connection) -> dict[str, list[tuple[int, float | None, float | None, float | None]]]:
    columns = table_columns(conn, "ccxt_ohlcv")
    if columns is None or not {"symbol", "ts", "close", "timeframe"}.issubset(columns):
        return {}
    open_sql = '"open"' if "open" in columns else "NULL"
    rows = conn.execute(
        f"SELECT symbol, ts, {open_sql} AS open_px, close, volume, timeframe FROM ccxt_ohlcv"
    ).fetchall()
    present = {str(row["timeframe"] or "") for row in rows}
    chosen = next((name for name in _BAR_TIMEFRAMES if name in present), None)
    grouped: dict[str, list[tuple[int, float | None, float | None, float | None]]] = {}
    for row in rows:
        if chosen is not None and str(row["timeframe"] or "") != chosen:
            continue
        symbol = str(row["symbol"] or "").strip().upper()
        try:
            ts_ms = int(row["ts"])
            close = float(row["close"])
        except (TypeError, ValueError):
            continue
        open_px = None if row["open_px"] is None else float(row["open_px"])
        volume = row["volume"]
        vol = None if volume is None else float(volume)
        grouped.setdefault(symbol, []).append((ts_ms, open_px, close, vol))
    return grouped


def _load_binance_klines(
    conn: sqlite3.Connection,
) -> dict[str, list[tuple[int, float | None, float | None, float | None]]]:
    columns = table_columns(conn, "binance_klines")
    needed = {"symbol", "interval", "open_time_ms", "open", "close"}
    if columns is None or not needed.issubset(columns):
        return {}
    rows = conn.execute(
        """
        SELECT symbol, open_time_ms, open, close, volume
        FROM binance_klines
        WHERE interval = '5m'
        """
    ).fetchall()
    grouped: dict[str, list[tuple[int, float | None, float | None, float | None]]] = {}
    for row in rows:
        symbol = str(row["symbol"] or "").strip().upper()
        try:
            grouped.setdefault(symbol, []).append(
                (
                    int(row["open_time_ms"]),
                    float(row["open"]),
                    float(row["close"]),
                    None if row["volume"] is None else float(row["volume"]),
                )
            )
        except (TypeError, ValueError):
            continue
    return grouped


def _load_named_points(
    conn: sqlite3.Connection,
    table: str,
    value_column: str,
    *,
    required: set[str],
) -> dict[str, list[tuple[int, float]]]:
    columns = table_columns(conn, table)
    if columns is None or not required.issubset(columns) or value_column not in columns:
        return {}
    grouped: dict[str, list[tuple[int, float]]] = {}
    for row in conn.execute(f'SELECT symbol, ts_ms, "{value_column}" AS value FROM "{table}"'):
        symbol = str(row["symbol"] or "").strip().upper()
        if not symbol or row["ts_ms"] is None or row["value"] is None:
            continue
        grouped.setdefault(symbol, []).append((int(row["ts_ms"]), float(row["value"])))
    return grouped


def _load_heat(
    conn: sqlite3.Connection,
) -> tuple[dict[tuple[str, str], list[tuple[int, float]]], bool]:
    columns = table_columns(conn, "indicator_history")
    needed = {"chain_id", "token_address", "recorded_at", "sentiment_heat_score"}
    if columns is None or not needed.issubset(columns):
        return {}, False
    grouped: dict[tuple[str, str], list[tuple[int, float]]] = {}
    # recorded_at is TEXT in some rows and BLOB in others. Decode before parse.
    # Do not ORDER BY or MIN/MAX the raw column.
    for row in conn.execute(
        """
        SELECT chain_id, token_address, recorded_at, sentiment_heat_score
        FROM indicator_history
        WHERE sentiment_heat_score IS NOT NULL
        """
    ):
        stamp = parse_ts(_decode_text(row["recorded_at"]))
        if stamp is None:
            continue
        key = (str(row["chain_id"] or "").strip().lower(), str(row["token_address"] or "").strip().lower())
        grouped.setdefault(key, []).append((int(stamp.timestamp() * 1000), float(row["sentiment_heat_score"])))
    return grouped, True


def _venue_bars(
    symbol: str,
    binance_bars: Mapping[str, Sequence[tuple[int, float | None, float | None, float | None]]],
    ccxt_bars: Mapping[str, Sequence[tuple[int, float | None, float | None, float | None]]],
) -> Sequence[tuple[int, float | None, float | None, float | None]]:
    """Pick one venue. A slash symbol is ccxt; anything else is Binance. Never both."""
    if not symbol:
        return ()
    if "/" in symbol:
        return ccxt_bars.get(symbol, ())
    return binance_bars.get(symbol, ())


def _event_symbol(
    event: Mapping[str, Any],
    symbol_by_url: Mapping[str, Mapping[str, str]],
    benchmark_symbol: str | None,
) -> dict[str, str]:
    mapped = symbol_by_url.get(str(event.get("url") or ""), {})
    symbol = str(mapped.get("symbol") or benchmark_symbol or "").strip().upper()
    token = str(mapped.get("token_address") or "").strip().lower()
    chain = str(mapped.get("chain_id") or "").strip().lower()
    return {"symbol": symbol, "token_address": token, "chain_id": chain}


def _summarize_returns(values: Sequence[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "mean": None if not values else sum(values) / len(values),
        "q10": quantile(values, 0.10),
        "q50": quantile(values, 0.50),
        "q90": quantile(values, 0.90),
    }


def audit_news_shock_history(
    *,
    news_db: str | Path | None = None,
    market_db: str | Path | None = None,
    derivatives_db: str | Path | None = None,
    indicator_db: str | Path | None = None,
    benchmark_symbol: str | None = None,
    symbol_by_url: Mapping[str, Mapping[str, str]] | None = None,
    now: datetime | None = None,
    timeout_sec: float = 2.0,
) -> dict[str, Any]:
    """Describe history. The returned object never carries an enabled threshold."""
    clock = _as_of(now)
    resolved_news = Path(news_db) if news_db is not None else resolve_news_db_path(None)
    raw_events, news_info = _load_news(resolved_news, now=clock, timeout_sec=timeout_sec)
    deduped = dedup_events(raw_events)
    splits = time_split(deduped)
    symbol_map = symbol_by_url or {}

    untestable: list[dict[str, str]] = []
    notes = [
        "x_watch stores mention_count_prev/last for the current watch only. "
        "Those counters are not a time series and were not copied onto older headlines.",
        "A microstructure or heat row newer than the headline is not used as if it had been known then.",
        "news_events has no symbol column. Joins use an explicit symbol map or --benchmark-symbol only.",
        "orderbook_imbalance_20 is not obi_top5. perp_taker_buy_sell_ratio is not taker_buy_ratio.",
        "Binance 5m and OKX/KuCoin 5m are separate venues and are not stitched.",
        "token_indicators sentiment is a current snapshot and is not copied onto older events.",
    ]
    untestable.extend(
        [
            {
                "series": "obi_top5",
                "reason": "No obi_top5 column. orderbook_imbalance_20 is a 20-level imbalance and is not used in its place.",
            },
            {
                "series": "volume_z",
                "reason": "No volume_z column in the inventoried tables.",
            },
            {
                "series": "onchain_labels",
                "reason": "The 2026-11-10 inventory has onchain_status=pending for all 1869 rows. False-positive rate is not computed.",
            },
            {
                "series": "x_news_library",
                "reason": "No standalone X news database. x_watch is 64 current Solana rows, not a timestamped heat history.",
            },
        ]
    )

    binance_bars: dict[str, list[tuple[int, float | None, float | None, float | None]]] = {}
    ccxt_bars: dict[str, list[tuple[int, float | None, float | None, float | None]]] = {}
    price_source = "none"
    market_note = "market db not supplied"
    if market_db is None:
        untestable.append({"series": "forward_price", "reason": "market db not supplied"})
        untestable.append({"series": "forward_volume", "reason": "market db not supplied"})
    else:
        market_path = Path(market_db)
        conn, detail = _table_points(market_path, timeout_sec)
        if conn is None:
            market_note = detail or "market db unavailable"
            untestable.append({"series": "forward_price", "reason": market_note})
            untestable.append({"series": "forward_volume", "reason": market_note})
        else:
            try:
                binance_bars = _load_binance_klines(conn)
                ccxt_bars = _load_ccxt(conn)
                has_x_watch = table_columns(conn, "x_watch") is not None
                has_token_indicators = table_columns(conn, "token_indicators") is not None
            except sqlite3.Error as exc:
                market_note = str(exc)
                binance_bars = {}
                ccxt_bars = {}
                has_x_watch = False
                has_token_indicators = False
            finally:
                conn.close()
            if binance_bars and ccxt_bars:
                price_source = "binance_klines_and_ccxt_kept_separate"
                market_note = (
                    "binance_klines and ccxt_ohlcv were both readable. Returns are not pooled. "
                    "Entry is the next 5m open at or after fetched_at."
                )
            elif binance_bars:
                price_source = "binance_klines"
                market_note = "binance_klines 5m only. Entry is the next open at or after fetched_at."
            elif ccxt_bars:
                price_source = "ccxt_ohlcv_separate"
                market_note = (
                    "ccxt_ohlcv only. OKX/KuCoin bars stay in their own venue and are not treated as Binance."
                )
            else:
                market_note = "no binance_klines 5m or ccxt_ohlcv series was readable"
                untestable.append({"series": "forward_price", "reason": market_note})
                untestable.append({"series": "forward_volume", "reason": market_note})
            if has_token_indicators:
                notes.append("token_indicators is present and was not read")
            if has_x_watch:
                notes.append("market db contains x_watch; its current counters were not read")

    obi: dict[str, list[tuple[int, float]]] = {}
    taker: dict[str, list[tuple[int, float]]] = {}
    whale: dict[str, list[tuple[int, float]]] = {}
    if derivatives_db is None:
        untestable.append({"series": "obi_asof", "reason": "derivatives db not supplied; orderbook_imbalance_20 not backfilled"})
        untestable.append({"series": "taker_buy_ratio_asof", "reason": "derivatives db not supplied"})
        untestable.append({"series": "whale_retail_divergence_asof", "reason": "derivatives db not supplied"})
    else:
        conn, detail = _table_points(Path(derivatives_db), timeout_sec)
        if conn is None:
            reason = detail or "derivatives db unavailable"
            untestable.append({"series": "obi_asof", "reason": reason})
            untestable.append({"series": "taker_buy_ratio_asof", "reason": reason})
            untestable.append({"series": "whale_retail_divergence_asof", "reason": reason})
        else:
            try:
                obi = _load_named_points(
                    conn,
                    "binance_spot_microstructure",
                    "orderbook_imbalance_20",
                    required={"symbol", "ts_ms", "orderbook_imbalance_20"},
                )
                taker_5 = _load_named_points(
                    conn,
                    "binance_spot_microstructure",
                    "taker_buy_ratio_5m",
                    required={"symbol", "ts_ms", "taker_buy_ratio_5m"},
                )
                taker_1 = _load_named_points(
                    conn,
                    "binance_spot_microstructure",
                    "taker_buy_ratio_1h",
                    required={"symbol", "ts_ms", "taker_buy_ratio_1h"},
                )
                taker = dict(taker_1)
                taker.update(taker_5)
                whale = _load_named_points(
                    conn,
                    "binance_perp_counterparty",
                    "whale_retail_divergence",
                    required={"symbol", "ts_ms", "whale_retail_divergence"},
                )
            except sqlite3.Error as exc:
                reason = str(exc)
                untestable.append({"series": "obi_asof", "reason": reason})
            finally:
                conn.close()
            if not obi:
                untestable.append(
                    {
                        "series": "obi_asof",
                        "reason": "no orderbook_imbalance_20 rows. obi_top5 is not a stored column and was not invented",
                    }
                )
            if not taker:
                untestable.append({"series": "taker_buy_ratio_asof", "reason": "no taker_buy_ratio_5m or taker_buy_ratio_1h rows"})
            if not whale:
                untestable.append({"series": "whale_retail_divergence_asof", "reason": "no whale_retail_divergence rows"})

    heat: dict[tuple[str, str], list[tuple[int, float]]] = {}
    heat_schema = False
    x_watch_present = False
    if indicator_db is None:
        untestable.append(
            {
                "series": "x_heat_timeseries",
                "reason": "indicator_history not supplied. x_watch current counters cannot replace it",
            }
        )
    else:
        conn, detail = _table_points(Path(indicator_db), timeout_sec)
        if conn is None:
            untestable.append({"series": "x_heat_timeseries", "reason": detail or "indicator db unavailable"})
        else:
            try:
                heat, heat_schema = _load_heat(conn)
                x_watch_present = table_columns(conn, "x_watch") is not None
            except sqlite3.Error as exc:
                untestable.append({"series": "x_heat_timeseries", "reason": str(exc)})
            finally:
                conn.close()
            if not heat_schema:
                untestable.append(
                    {
                        "series": "x_heat_timeseries",
                        "reason": "indicator_history.sentiment_heat_score time series is absent",
                    }
                )
            elif not any(str(item.get("token_address") or "") for item in symbol_map.values()):
                untestable.append(
                    {
                        "series": "x_heat_timeseries",
                        "reason": "heat rows exist but news_events has no token key, and no symbol map was supplied",
                    }
                )
            if x_watch_present:
                notes.append("indicator db contains x_watch; those current counters were not used as historical heat")

    max_age_ms = int(EVIDENCE_MAX_AGE_MIN * 60_000)
    enriched: list[dict[str, Any]] = []
    for event in deduped:
        link = _event_symbol(event, symbol_map, benchmark_symbol)
        symbol = link["symbol"]
        token = link["token_address"]
        chain = link["chain_id"]
        price_bars = _venue_bars(symbol, binance_bars, ccxt_bars)
        ohlc = [(ts_ms, open_px, close_px) for ts_ms, open_px, close_px, _volume in price_bars]
        volumes = [(ts_ms, volume) for ts_ms, _open, _close, volume in price_bars]
        event_ms = _event_clock_ms(event)
        record = dict(event)
        record["join_symbol"] = symbol or None
        record["join_token"] = token or None
        record["venue"] = "ccxt" if "/" in symbol else "binance"
        record["ret_5m"] = kline_forward_return(ohlc, event_ms, 5)
        record["ret_15m"] = kline_forward_return(ohlc, event_ms, 15)
        record["ret_60m"] = kline_forward_return(ohlc, event_ms, 60)
        record["ret_240m"] = kline_forward_return(ohlc, event_ms, 240)
        record["ret_1440m"] = kline_forward_return(ohlc, event_ms, 1440)
        record["volume_60m"] = forward_volume(volumes, event_ms, 60)
        record["imbalance20_asof"] = (
            None if not symbol else _value(asof_numeric(obi.get(symbol, []), event_ms, max_age_ms))
        )
        record["taker_asof"] = None if not symbol else _value(asof_numeric(taker.get(symbol, []), event_ms, max_age_ms))
        record["whale_asof"] = None if not symbol else _value(asof_numeric(whale.get(symbol, []), event_ms, max_age_ms))
        record["obi_top5_asof"] = None
        record["heat_delta"] = None
        if chain and token:
            record["heat_delta"] = heat_delta(heat.get((chain, token), []), event_ms, max_age_ms)
        enriched.append(record)

    def _covered(name: str) -> int:
        return sum(1 for event in enriched if event.get(name) is not None)

    coverage_counts = {
        "forward_return_60m": _covered("ret_60m"),
        "forward_volume_60m": _covered("volume_60m"),
        "imbalance20_asof": _covered("imbalance20_asof"),
        "obi_top5_asof": _covered("obi_top5_asof"),
        "taker_asof": _covered("taker_asof"),
        "whale_asof": _covered("whale_asof"),
        "heat_delta": _covered("heat_delta"),
    }
    denom = max(len(enriched), 1)
    coverage = {key: count / denom for key, count in coverage_counts.items()}
    if not benchmark_symbol and not symbol_map:
        untestable.append(
            {
                "series": "event_symbol_join",
                "reason": "no benchmark symbol and no symbol map; per-asset returns and book features were not guessed",
            }
        )

    test_urls = {item.get("url") for item in splits["test"]}
    test_events = [event for event in enriched if event.get("url") in test_urls]
    test_returns = [float(event["ret_60m"]) for event in test_events if event.get("ret_60m") is not None]

    test_n = len(splits["test"])
    feature_ready = min(coverage["taker_asof"], coverage["whale_asof"], coverage["heat_delta"])
    gates_failed = ["production_history_not_attested"]
    if news_info.get("availability") != "ok":
        gates_failed.append("news_events_unreadable")
    if test_n < MIN_TEST_EVENTS:
        gates_failed.append(f"test_split_below_{MIN_TEST_EVENTS}")
    if feature_ready < MIN_FEATURE_COVERAGE:
        gates_failed.append(f"asof_feature_coverage_below_{MIN_FEATURE_COVERAGE}")
    if untestable:
        gates_failed.append("untestable_series_present")

    return {
        "calibration_status": "not_calibrated",
        "production_attested": False,
        "thresholds_enabled": False,
        "recommended_thresholds": None,
        "threshold_suggestion_withheld_reason": (
            "Descriptive quantiles and miss rates are not a calibrated gate. "
            "This build does not write weights, even if every sample gate were met."
        ),
        "news": news_info,
        "methodology": {
            "time_split": "Events ordered by fetched_at. Earliest 60% train, next 20% validation, latest remainder test. No shuffle. published_at is not the trade clock.",
            "dedup": "Unique url, then same source and normalized title inside 6 hours keeps the earliest published_at.",
            "sample_size": f"A later calibration would require at least {MIN_TEST_EVENTS} test-split events. This run does not relax that.",
            "coverage": "Share of deduped events with a point-in-time value. Missing stays missing.",
            "cost": "No fee, spread, or impact is applied. Do not read a positive mean as a tradable edge.",
            "benchmark": "Same symbol or token, 7 days earlier, same 60m forward return. Reported only as a descriptive gap.",
            "confidence_interval": "A Wilson interval would require labeled false positives. None are computed.",
            "multiple_testing": (
                f"Phrase family size {(len(CRISIS_PHRASES) + len(HAWKISH_PHRASES)) * len(HORIZONS_MIN)} "
                "is crisis phrases plus hawkish phrases, times horizons 15/60/240. "
                "A future search has to be pre-registered or adjusted with Bonferroni or Benjamini-Hochberg. "
                "This run does not search."
            ),
            "no_lookahead": "Features require timestamp <= fetched_at. Entry is the first 5m open at or after fetched_at. A bar that opened earlier is not used.",
            "no_snapshot_backfill": "Current x_watch, token_indicators, and any feature row after fetched_at are excluded. Venues are not stitched.",
            "price_source": price_source,
            "price_note": market_note,
        },
        "sample": {
            "raw_whitelist_or_kept_inputs": len(raw_events),
            "after_dedup": len(deduped),
            "train": len(splits["train"]),
            "validation": len(splits["validation"]),
            "test": len(splits["test"]),
            "rumor_ignored": news_info.get("rumor_ignored", 0),
            "future_ignored": news_info.get("future_ignored", 0),
            "late_archive_rows": news_info.get("late_archive_rows", 0),
            "onchain_labeled": news_info.get("onchain_labeled", 0),
        },
        "coverage_counts": coverage_counts,
        "coverage_fraction": coverage,
        "untestable": _unique_untestable(untestable),
        "notes": notes,
        "local_inventory_2026_11_10": LOCAL_INVENTORY_2026_11_10,
        "descriptive": {
            "label": "not_for_activation",
            "test_return_60m": _summarize_returns(test_returns),
            "false_positive_rate": {
                "status": "not_computed",
                "reason": (
                    "No historical aligned or divergent labels. "
                    "A positive BTC return is not a false positive."
                ),
                "labeled_rows_in_this_run": news_info.get("onchain_labeled", 0),
            },
            "benchmark_60m": {
                "status": "not_pooled",
                "note": "Binance and ccxt stays in separate books. This run does not publish a stitched control.",
            },
            "cost_applied": False,
        },
        "gates_failed": gates_failed,
        "gates_passed": False,
        "minimum_gates": {
            "test_split_events": MIN_TEST_EVENTS,
            "asof_feature_coverage": MIN_FEATURE_COVERAGE,
            "horizons_min": list(HORIZONS_MIN),
            "production_attestation": False,
        },
    }


def _value(point: tuple[int, float] | None) -> float | None:
    if point is None:
        return None
    return point[1]


def _unique_untestable(items: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    for item in items:
        key = (item.get("series", ""), item.get("reason", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append({"series": key[0], "reason": key[1]})
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Describe historical news shocks. Does not enable thresholds or trade."
    )
    parser.add_argument("--news-db", default=None)
    parser.add_argument("--market-db", default=None)
    parser.add_argument("--derivatives-db", default=None)
    parser.add_argument("--indicator-db", default=None)
    parser.add_argument("--benchmark-symbol", default=None)
    parser.add_argument(
        "--symbol-map",
        default=None,
        help="JSON object of url -> {symbol, token_address, chain_id}",
    )
    parser.add_argument("--out", default=None, help="Write the report JSON here. Never a live config.")
    args = parser.parse_args(argv)
    symbol_map = None
    if args.symbol_map:
        symbol_map = json.loads(Path(args.symbol_map).read_text(encoding="utf-8"))
    report = audit_news_shock_history(
        news_db=args.news_db,
        market_db=args.market_db,
        derivatives_db=args.derivatives_db,
        indicator_db=args.indicator_db,
        benchmark_symbol=args.benchmark_symbol,
        symbol_by_url=symbol_map,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
