"""Read-only news shock gate for the five-state router.

The live path reads ``news_events`` once per tick and never writes it.
Crisis and Fed hawkish labels are the lexical safety rules supplied for this
gate. They are not fitted weights.

On-chain ``aligned`` / ``divergent`` stay ``pending`` unless the caller passes
an explicit :class:`OnchainCalibration`. No such object is constructed here,
and a stored ``onchain_status`` label is not trusted. Ranking weight stays off
unless the router is given a number for a test or a future attested config.

``x_watch`` is a current counter, not a history. This module may read that
current counter for the open tick. It does not copy the counter onto past
events. Historical rows live in the audit module.
"""

from __future__ import annotations

import html
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Mapping, Never, Sequence
from urllib.parse import quote

from logging_setup import get_logger

log = get_logger("decision_engine.news_shock")

MODE_RO = "ro"
CALIBRATION_STATUS = "not_calibrated"
NEWS_WINDOW_MIN = 90
EVIDENCE_MAX_AGE_MIN = NEWS_WINDOW_MIN
FUTURE_SKEW_MIN = 1.5
# A source timestamp more than one decision window before the local fetch is a
# late archive. The 2026-11-10 inventory found 164 such rows. They are not a
# fresh shock, and published_at is not the time they could have been traded.
LATE_ARCHIVE_LAG_MIN = NEWS_WINDOW_MIN
FETCH_AFTER_PUBLISH_SKEW_SEC = 60
NEWS_DB_ENV = "NEWS_EVENTS_DB_PATH"
X_WATCH_DB_ENV = "X_WATCH_DB_PATH"
DERIVATIVES_DB_ENV = "PUBLIC_DERIVATIVES_DB_PATH"

Availability = Literal["ok", "missing_db", "locked", "timeout", "schema_error", "error", "not_configured", "not_requested"]
Severity = Literal["none", "crisis", "hawkish"]
OnchainLabel = Literal["pending", "aligned", "divergent"]

# Same strings as regime_strategy_router. Kept here so this module does not
# import the router (the router imports the gate).
REGIME_CRISIS = "crisis_liquidation_halt"
REGIME_DEFENSIVE = "crowded_long_defensive"
REGIME_BULL = "cross_asset_momentum_pullback"

FED_SOURCE_IDS = frozenset({"fed_monetary", "fed_speeches"})

CRISIS_PHRASES: tuple[str, ...] = (
    "emergency rate hike",
    "intermeeting hike",
    "halt withdrawals",
    "halts withdrawals",
    "suspend withdrawals",
    "suspends withdrawals",
    "insolvency",
    "chapter 11",
    "bridge exploit",
    "exchange hacked",
    "sec emergency",
)
HAWKISH_PHRASES: tuple[str, ...] = (
    "rate hike",
    "raises target range",
    "increase the target range",
    "higher for longer",
    "further tightening",
    "hawkish",
    "persistent inflation",
    "inflation remains elevated",
    "tariff shock",
)

NEWS_EVENTS_COLUMNS = frozenset(
    {
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
    }
)
X_WATCH_COLUMNS = frozenset(
    {
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
    }
)
MICROSTRUCTURE_COLUMNS = frozenset(
    {
        "symbol",
        "ts_ms",
        "taker_buy_ratio_5m",
        "taker_buy_ratio_1h",
        "orderbook_imbalance_20",
    }
)
PERP_COUNTERPARTY_COLUMNS = frozenset(
    {
        "symbol",
        "ts_ms",
        "whale_retail_divergence",
    }
)

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|without|denies|denied|deny|rejects|rejected|reject|"
    r"isn['’]t|aren['’]t|wasn['’]t|weren['’]t)\b",
    re.IGNORECASE,
)
_PHRASE_CACHE: dict[str, re.Pattern[str]] = {}


class NewsRouteEffect(Enum):
    KEEP = "keep"
    CRISIS_HALT = "crisis_halt"
    HAWKISH_DEFENSIVE = "hawkish_defensive"
    DIVERGENT_REJECT = "divergent_reject"


@dataclass(frozen=True)
class NewsShockStatus:
    """Lexical shock. ``age_min`` is computed once, when the snapshot is built."""

    severity: Severity
    matched_keyword: str | None
    headline: str | None
    source: str | None
    age_min: float | None
    suppressed_reason: str | None = None
    published_at: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class XWatchPoint:
    """One current ``x_watch`` row. Not a historical heat series."""

    symbol: str | None
    token_address: str | None
    mention_count_prev: int | None
    mention_count_last: int | None
    last_searched_at: str | None


@dataclass(frozen=True)
class TimedValue:
    symbol: str
    ts_ms: int
    value: float


@dataclass(frozen=True)
class OnchainCalibration:
    """Explicit cuts. Construct this only from attested evidence or a test.

    There is no default instance. Leaving the argument ``None`` keeps every
    on-chain label at ``pending``.
    """

    min_heat_delta: int
    min_obi_aligned: float
    min_taker_aligned: float
    min_whale_aligned: float
    max_obi_divergent: float
    max_taker_divergent: float
    max_whale_divergent: float
    max_feature_age_min: float


@dataclass(frozen=True)
class OnchainJudgment:
    status: OnchainLabel
    reason: str


@dataclass(frozen=True)
class NewsReadSnapshot:
    mode: str
    availability: Availability
    detail: str | None
    db_path: str | None
    as_of: str
    window_min: int
    shocks: tuple[NewsShockStatus, ...]
    context_count: int
    scanned: int
    suppressed: int
    x_watch_availability: Availability
    x_watch_detail: str | None
    x_points: tuple[XWatchPoint, ...]
    feature_availability: Availability
    feature_detail: str | None
    obi_points: tuple[TimedValue, ...]
    taker_points: tuple[TimedValue, ...]
    whale_points: tuple[TimedValue, ...]


@dataclass(frozen=True)
class NewsOverlay:
    effect: NewsRouteEffect
    meta: dict[str, Any]


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "decision_engine").is_dir() and (candidate / "AGENTS.md").is_file():
            return candidate
    return here.parents[2]


def resolve_news_db_path(explicit: str | Path | None = None) -> Path:
    if explicit is not None and str(explicit).strip():
        return Path(explicit)
    env = os.environ.get(NEWS_DB_ENV, "").strip()
    if env:
        return Path(env)
    return repo_root() / "news_events.db"


def resolve_x_watch_path(explicit: str | Path | None = None) -> Path | None:
    """Only an explicit path or ``X_WATCH_DB_PATH``. Do not guess a crawler DB."""
    if explicit is not None and str(explicit).strip():
        return Path(explicit)
    env = os.environ.get(X_WATCH_DB_ENV, "").strip()
    if env:
        return Path(env)
    return None


def resolve_derivatives_path(explicit: str | Path | None = None) -> Path:
    if explicit is not None and str(explicit).strip():
        return Path(explicit)
    env = os.environ.get(DERIVATIVES_DB_ENV, "").strip()
    if env:
        return Path(env)
    return repo_root() / "data" / "counterparty" / "public_derivatives.db"


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def age_minutes(value: object, now: datetime) -> float | None:
    dt = parse_ts(value)
    if dt is None:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now.astimezone(timezone.utc) - dt).total_seconds() / 60.0


def to_epoch_ms(value: object) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            return None
        # Stored microstructure and ccxt bars are epoch milliseconds.
        return int(number)
    dt = parse_ts(value)
    if dt is None:
        return None
    return int(dt.timestamp() * 1000)


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _opt_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def visible_text(*parts: object) -> str:
    raw = " ".join(str(part) for part in parts if part)
    raw = html.unescape(raw)
    raw = _TAG_RE.sub(" ", raw)
    return _SPACE_RE.sub(" ", raw).strip()


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    cached = _PHRASE_CACHE.get(phrase)
    if cached is None:
        cached = re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(phrase)}(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
        _PHRASE_CACHE[phrase] = cached
    return cached


def _quote_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == '"':
            close = text.find('"', index + 1)
            if close < 0:
                break
            spans.append((index, close + 1))
            index = close + 1
            continue
        if char == "“":
            close = text.find("”", index + 1)
            if close < 0:
                break
            spans.append((index, close + 1))
            index = close + 1
            continue
        index += 1
    return spans


def _inside_quotes(spans: Sequence[tuple[int, int]], start: int, end: int) -> bool:
    for left, right in spans:
        if start >= left and end <= right:
            return True
    return False


def _negated(text: str, start: int) -> bool:
    window = text[max(0, start - 96) : start]
    for separator in ".!?;\n":
        cut = window.rfind(separator)
        if cut >= 0:
            window = window[cut + 1 :]
    tail = " ".join(window.split()[-6:])
    return _NEGATION_RE.search(tail) is not None


def first_unsuppressed_phrase(text: str, phrases: Sequence[str]) -> str | None:
    """Longest phrase that is not negated and not inside a quotation."""
    if not text:
        return None
    spans = _quote_spans(text)
    chosen: tuple[int, int, str] | None = None
    for phrase in phrases:
        pattern = _phrase_pattern(phrase)
        match = pattern.search(text)
        while match is not None:
            if not _inside_quotes(spans, match.start(), match.end()) and not _negated(text, match.start()):
                candidate = (-len(phrase), match.start(), phrase)
                if chosen is None or candidate < chosen:
                    chosen = candidate
                break
            match = pattern.search(text, match.end())
    if chosen is None:
        return None
    return chosen[2]


def _severity_rank(severity: Severity) -> int:
    if severity == "crisis":
        return 2
    if severity == "hawkish":
        return 1
    if severity == "none":
        return 0
    unreachable: Never = severity
    raise AssertionError(unreachable)


def _field(row: Mapping[str, Any], key: str) -> Any:
    """Read a column from a dict or ``sqlite3.Row`` (Row has no ``.get``)."""
    keys = row.keys() if hasattr(row, "keys") else None
    if keys is not None and key not in set(keys):
        return None
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def classify_news_row(
    row: Mapping[str, Any],
    *,
    now: datetime,
    max_age_min: float | None = NEWS_WINDOW_MIN,
) -> NewsShockStatus:
    """Classify one stored news row.

    The live clock is ``fetched_at`` (when the row was known locally). A story
    whose ``published_at`` is more than the decision window earlier than that
    fetch is a late archive and does not fire. The historical audit passes
    ``max_age_min=None`` so an older row can still be described; its event
    time remains ``fetched_at``, not the earlier source timestamp.
    """
    source = str(_field(row, "source") or "").strip().lower()
    tier = str(_field(row, "source_tier") or "").strip().lower()
    title = str(_field(row, "title") or "").strip()
    headline = title[:240] or None
    published = _field(row, "published_at")
    fetched = _field(row, "fetched_at")
    url = str(_field(row, "url") or "") or None
    published_age = age_minutes(published, now)
    fetched_age = age_minutes(fetched, now)
    known_age = fetched_age if fetched_age is not None else published_age
    base = {
        "headline": headline,
        "source": source or None,
        "published_at": None if published is None else str(published),
        "url": url,
        "age_min": known_age,
    }
    if tier != "whitelist":
        return NewsShockStatus(
            "none", None, suppressed_reason="rumor_or_non_whitelist", **base
        )
    if published_age is None:
        return NewsShockStatus("none", None, suppressed_reason="missing_or_bad_published_at", **base)
    if published_age < -FUTURE_SKEW_MIN or (fetched_age is not None and fetched_age < -FUTURE_SKEW_MIN):
        return NewsShockStatus("none", None, suppressed_reason="future", **base)
    published_dt = parse_ts(published)
    fetched_dt = parse_ts(fetched)
    if (
        published_dt is not None
        and fetched_dt is not None
        and (published_dt - fetched_dt).total_seconds() > FETCH_AFTER_PUBLISH_SKEW_SEC
    ):
        return NewsShockStatus("none", None, suppressed_reason="future_vs_fetch", **base)
    if max_age_min is not None:
        if fetched_age is None:
            return NewsShockStatus("none", None, suppressed_reason="missing_fetched_at", **base)
        if fetched_age > max_age_min:
            return NewsShockStatus("none", None, suppressed_reason="stale", **base)
        if (
            published_dt is not None
            and fetched_dt is not None
            and (fetched_dt - published_dt).total_seconds() > max_age_min * 60.0
        ):
            return NewsShockStatus("none", None, suppressed_reason="late_archive", **base)

    text = visible_text(_field(row, "title"), _field(row, "summary"))
    crisis = first_unsuppressed_phrase(text, CRISIS_PHRASES)
    if crisis is not None:
        return NewsShockStatus("crisis", crisis, suppressed_reason=None, **base)
    hawkish = first_unsuppressed_phrase(text, HAWKISH_PHRASES)
    if hawkish is not None:
        if source not in FED_SOURCE_IDS:
            return NewsShockStatus(
                "none", hawkish, suppressed_reason="hawkish_not_fed_source", **base
            )
        return NewsShockStatus("hawkish", hawkish, suppressed_reason=None, **base)
    return NewsShockStatus("none", None, suppressed_reason="no_keyword", **base)


def primary_shock(shocks: Sequence[NewsShockStatus]) -> NewsShockStatus | None:
    chosen: NewsShockStatus | None = None
    for shock in shocks:
        if shock.suppressed_reason is not None:
            continue
        if shock.severity == "none":
            continue
        if chosen is None:
            chosen = shock
            continue
        rank_new = _severity_rank(shock.severity)
        rank_old = _severity_rank(chosen.severity)
        age_new = shock.age_min if shock.age_min is not None else 10**9
        age_old = chosen.age_min if chosen.age_min is not None else 10**9
        if rank_new > rank_old or (rank_new == rank_old and age_new < age_old):
            chosen = shock
    return chosen


def _availability_from_sqlite_error(exc: BaseException) -> Availability:
    message = str(exc).lower()
    if "interrupt" in message:
        return "timeout"
    if "locked" in message:
        return "locked"
    if "busy" in message or "timeout" in message:
        return "timeout"
    return "error"


def sqlite_ro_uri(path: Path) -> str:
    quoted = quote(path.resolve().as_posix(), safe="/:")
    return f"file:{quoted}?mode=ro"


def open_readonly(
    path: Path,
    timeout_sec: float,
) -> tuple[sqlite3.Connection | None, Availability | None, str | None]:
    """Open SQLite in ``mode=ro``. Missing files are not created."""
    if timeout_sec < 0:
        timeout_sec = 0.0
    if not path.exists():
        return None, "missing_db", str(path)
    if path.is_dir():
        return None, "error", f"not_a_database:{path}"
    try:
        conn = sqlite3.connect(sqlite_ro_uri(path), uri=True, timeout=timeout_sec)
    except sqlite3.Error as exc:
        return None, _availability_from_sqlite_error(exc), str(exc)
    conn.row_factory = sqlite3.Row
    deadline = time.monotonic() + timeout_sec

    def _progress() -> int:
        if time.monotonic() >= deadline:
            return 1
        return 0

    try:
        conn.execute(f"PRAGMA busy_timeout = {int(timeout_sec * 1000)}")
        conn.execute("PRAGMA query_only = ON")
        conn.set_progress_handler(_progress, 1)
    except sqlite3.Error as exc:
        conn.close()
        return None, _availability_from_sqlite_error(exc), str(exc)
    return conn, None, None


def table_columns(conn: sqlite3.Connection, table: str) -> set[str] | None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        raise ValueError(f"unexpected table name: {table}")
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if found is None:
        return None
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return {str(row[1]) for row in rows}


def _empty_feature_fields() -> dict[str, Any]:
    return {
        "x_watch_availability": "not_configured",
        "x_watch_detail": None,
        "x_points": (),
        "feature_availability": "not_requested",
        "feature_detail": None,
        "obi_points": (),
        "taker_points": (),
        "whale_points": (),
    }


def error_snapshot(detail: str, *, now: datetime | None = None) -> NewsReadSnapshot:
    clock = now or datetime.now(timezone.utc)
    return NewsReadSnapshot(
        mode=MODE_RO,
        availability="error",
        detail=detail,
        db_path=None,
        as_of=_iso(clock),
        window_min=NEWS_WINDOW_MIN,
        shocks=(),
        context_count=0,
        scanned=0,
        suppressed=0,
        **_empty_feature_fields(),
    )


def _read_news_rows(
    path: Path,
    *,
    now: datetime,
    timeout_sec: float,
) -> tuple[Availability, str | None, tuple[NewsShockStatus, ...], int, int, int]:
    conn, failed, detail = open_readonly(path, timeout_sec)
    if conn is None:
        availability = failed or "error"
        return availability, detail, (), 0, 0, 0
    try:
        columns = table_columns(conn, "news_events")
        if columns is None or not NEWS_EVENTS_COLUMNS.issubset(columns):
            missing = sorted(NEWS_EVENTS_COLUMNS - (columns or set()))
            return "schema_error", "news_events missing " + ",".join(missing), (), 0, 0, 0
        rows = conn.execute(
            """
            SELECT id, source, source_tier, title, url, published_at,
                   fetched_at, summary, onchain_status
            FROM news_events
            WHERE lower(source_tier) = 'whitelist'
              AND published_at IS NOT NULL
            ORDER BY published_at DESC
            LIMIT 2000
            """
        ).fetchall()
    except sqlite3.Error as exc:
        return _availability_from_sqlite_error(exc), str(exc), (), 0, 0, 0
    finally:
        conn.close()

    shocks: list[NewsShockStatus] = []
    context = 0
    suppressed = 0
    for row in rows:
        status = classify_news_row(row, now=now, max_age_min=NEWS_WINDOW_MIN)
        if status.suppressed_reason in {
            "future",
            "future_vs_fetch",
            "stale",
            "late_archive",
            "missing_or_bad_published_at",
            "missing_fetched_at",
        }:
            suppressed += 1
            continue
        age = status.age_min
        if age is not None and -FUTURE_SKEW_MIN <= age <= NEWS_WINDOW_MIN:
            context += 1
        if status.severity != "none" and status.suppressed_reason is None:
            shocks.append(status)
        else:
            suppressed += 1
    return "ok", None, tuple(shocks), context, len(rows), suppressed


def _symbols_upper(symbols: Sequence[str] | None) -> tuple[str, ...]:
    if symbols is None:
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        symbol = str(raw or "").strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            out.append(symbol)
    return tuple(out[:500])


def _read_x_watch(
    path: Path | None,
    *,
    timeout_sec: float,
    now: datetime,
) -> tuple[Availability, str | None, tuple[XWatchPoint, ...]]:
    if path is None:
        return "not_configured", None, ()
    conn, failed, detail = open_readonly(path, timeout_sec)
    if conn is None:
        return failed or "error", detail, ()
    try:
        columns = table_columns(conn, "x_watch")
        if columns is None or not X_WATCH_COLUMNS.issubset(columns):
            missing = sorted(X_WATCH_COLUMNS - (columns or set()))
            return "schema_error", "x_watch missing " + ",".join(missing), ()
        select_expires = ", expires_at" if "expires_at" in columns else ""
        rows = conn.execute(
            f"""
            SELECT symbol, token_address, mention_count_prev, mention_count_last,
                   last_searched_at{select_expires}
            FROM x_watch
            """
        ).fetchall()
    except sqlite3.Error as exc:
        return _availability_from_sqlite_error(exc), str(exc), ()
    finally:
        conn.close()

    points: list[XWatchPoint] = []
    for row in rows:
        keys = row.keys()
        if "expires_at" in keys:
            expiry = parse_ts(row["expires_at"])
            if expiry is not None and expiry <= now.astimezone(timezone.utc):
                continue
        points.append(
            XWatchPoint(
                symbol=(None if row["symbol"] is None else str(row["symbol"]).strip().upper()) or None,
                token_address=(
                    None if row["token_address"] is None else str(row["token_address"]).strip().lower()
                )
                or None,
                mention_count_prev=_opt_int(row["mention_count_prev"]),
                mention_count_last=_opt_int(row["mention_count_last"]),
                last_searched_at=(
                    None if row["last_searched_at"] is None else str(row["last_searched_at"])
                ),
            )
        )
    return "ok", None, tuple(points)


def _in_clause(symbols: Sequence[str]) -> tuple[str, tuple[str, ...]]:
    marks = ",".join("?" for _ in symbols)
    return marks, tuple(symbols)


def _reduce_latest(rows: Sequence[tuple[str, int, float | None]]) -> tuple[TimedValue, ...]:
    best: dict[str, TimedValue] = {}
    for symbol, ts_ms, value in rows:
        if value is None:
            continue
        current = best.get(symbol)
        if current is None or ts_ms > current.ts_ms:
            best[symbol] = TimedValue(symbol=symbol, ts_ms=ts_ms, value=value)
    return tuple(best.values())


def _read_features(
    path: Path,
    symbols: Sequence[str],
    *,
    now: datetime,
    timeout_sec: float,
) -> tuple[Availability, str | None, tuple[TimedValue, ...], tuple[TimedValue, ...], tuple[TimedValue, ...]]:
    if not symbols:
        return "not_requested", None, (), (), ()
    conn, failed, detail = open_readonly(path, timeout_sec)
    if conn is None:
        return failed or "error", detail, (), (), ()
    as_of_ms = int(now.timestamp() * 1000)
    cutoff = as_of_ms - int(EVIDENCE_MAX_AGE_MIN * 60_000)
    marks, params = _in_clause(symbols)
    try:
        micro_cols = table_columns(conn, "binance_spot_microstructure")
        perp_cols = table_columns(conn, "binance_perp_counterparty")
        if micro_cols is None or not MICROSTRUCTURE_COLUMNS.issubset(micro_cols):
            missing = sorted(MICROSTRUCTURE_COLUMNS - (micro_cols or set()))
            return "schema_error", "binance_spot_microstructure missing " + ",".join(missing), (), (), ()
        if perp_cols is None or not PERP_COUNTERPARTY_COLUMNS.issubset(perp_cols):
            missing = sorted(PERP_COUNTERPARTY_COLUMNS - (perp_cols or set()))
            return "schema_error", "binance_perp_counterparty missing " + ",".join(missing), (), (), ()
        micro_rows = conn.execute(
            f"""
            SELECT symbol, ts_ms, orderbook_imbalance_20,
                   taker_buy_ratio_5m, taker_buy_ratio_1h
            FROM binance_spot_microstructure
            WHERE ts_ms >= ? AND ts_ms <= ? AND symbol IN ({marks})
            """,
            (cutoff, as_of_ms, *params),
        ).fetchall()
        perp_rows = conn.execute(
            f"""
            SELECT symbol, ts_ms, whale_retail_divergence
            FROM binance_perp_counterparty
            WHERE ts_ms >= ? AND ts_ms <= ? AND symbol IN ({marks})
            """,
            (cutoff, as_of_ms, *params),
        ).fetchall()
    except sqlite3.Error as exc:
        return _availability_from_sqlite_error(exc), str(exc), (), (), ()
    finally:
        conn.close()

    # orderbook_imbalance_20 is depth-20, not obi_top5. It is not loaded into
    # the top-5 slot. Taker columns here are buy/total ratios in [0, 1], not
    # perp_taker_buy_sell_ratio.
    obi_raw: list[tuple[str, int, float | None]] = []
    taker_raw: list[tuple[str, int, float | None]] = []
    for row in micro_rows:
        symbol = str(row["symbol"] or "").strip().upper()
        ts_ms = _opt_int(row["ts_ms"])
        if not symbol or ts_ms is None or ts_ms > as_of_ms:
            continue
        taker = _finite(row["taker_buy_ratio_5m"])
        if taker is None:
            taker = _finite(row["taker_buy_ratio_1h"])
        taker_raw.append((symbol, ts_ms, taker))
    whale_raw: list[tuple[str, int, float | None]] = []
    for row in perp_rows:
        symbol = str(row["symbol"] or "").strip().upper()
        ts_ms = _opt_int(row["ts_ms"])
        if not symbol or ts_ms is None or ts_ms > as_of_ms:
            continue
        whale_raw.append((symbol, ts_ms, _finite(row["whale_retail_divergence"])))
    return "ok", None, _reduce_latest(obi_raw), _reduce_latest(taker_raw), _reduce_latest(whale_raw)


def load_news_snapshot(
    db_path: str | Path | None = None,
    *,
    now: datetime | None = None,
    timeout_sec: float = 1.0,
    symbols: Sequence[str] | None = None,
    x_watch_db: str | Path | None = None,
    derivatives_db: str | Path | None = None,
) -> NewsReadSnapshot:
    """One read-only snapshot. Callers reuse it for every symbol in the tick."""
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    news_path = resolve_news_db_path(db_path)
    try:
        availability, detail, shocks, context, scanned, suppressed = _read_news_rows(
            news_path, now=clock, timeout_sec=timeout_sec
        )
    except sqlite3.Error as exc:
        availability = _availability_from_sqlite_error(exc)
        detail = str(exc)
        shocks, context, scanned, suppressed = (), 0, 0, 0
    except OSError as exc:
        availability = "error"
        detail = str(exc)
        shocks, context, scanned, suppressed = (), 0, 0, 0

    symbol_list = _symbols_upper(symbols)
    try:
        x_availability, x_detail, x_points = _read_x_watch(
            resolve_x_watch_path(x_watch_db), now=clock, timeout_sec=timeout_sec
        )
    except (sqlite3.Error, OSError) as exc:
        x_availability, x_detail, x_points = "error", str(exc), ()
    try:
        feature_availability, feature_detail, obi, taker, whale = _read_features(
            resolve_derivatives_path(derivatives_db),
            symbol_list,
            now=clock,
            timeout_sec=timeout_sec,
        )
    except (sqlite3.Error, OSError) as exc:
        feature_availability, feature_detail, obi, taker, whale = "error", str(exc), (), (), ()

    if availability not in {"ok", "missing_db"}:
        log.warning(
            "news shock snapshot availability=%s detail=%s path=%s",
            availability,
            detail,
            news_path,
        )
    elif availability == "missing_db":
        log.info("news shock snapshot missing path=%s", news_path)
    return NewsReadSnapshot(
        mode=MODE_RO,
        availability=availability,
        detail=detail,
        db_path=str(news_path),
        as_of=_iso(clock),
        window_min=NEWS_WINDOW_MIN,
        shocks=shocks,
        context_count=context,
        scanned=scanned,
        suppressed=suppressed,
        x_watch_availability=x_availability,
        x_watch_detail=x_detail,
        x_points=x_points,
        feature_availability=feature_availability,
        feature_detail=feature_detail,
        obi_points=obi,
        taker_points=taker,
        whale_points=whale,
    )


def _latest_point(
    points: Sequence[TimedValue],
    symbol: str,
    as_of_ms: int,
    *,
    max_age_min: float,
) -> TimedValue | None:
    best: TimedValue | None = None
    max_age_ms = int(max_age_min * 60_000)
    for point in points:
        if point.symbol != symbol or point.ts_ms > as_of_ms:
            continue
        if as_of_ms - point.ts_ms > max_age_ms:
            continue
        if best is None or point.ts_ms > best.ts_ms:
            best = point
    return best


def _row_symbol(row: Mapping[str, Any]) -> str:
    return str(row.get("binance_symbol") or row.get("symbol") or "").strip().upper()


def _match_x(
    row: Mapping[str, Any],
    points: Sequence[XWatchPoint],
) -> tuple[XWatchPoint | None, str | None]:
    symbol = _row_symbol(row)
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    token = str(row.get("token_address") or "").strip().lower()
    hits: list[XWatchPoint] = []
    for point in points:
        if token and point.token_address and point.token_address == token:
            hits.append(point)
            continue
        if point.symbol and point.symbol in {symbol, base}:
            hits.append(point)
    unique: dict[tuple[Any, ...], XWatchPoint] = {}
    for point in hits:
        unique[
            (
                point.symbol,
                point.token_address,
                point.mention_count_prev,
                point.mention_count_last,
                point.last_searched_at,
            )
        ] = point
    found = list(unique.values())
    if len(found) > 1:
        return None, "ambiguous_x_watch"
    if len(found) == 1:
        return found[0], None
    return None, None


def _heat_from_row(row: Mapping[str, Any]) -> tuple[int, int, str] | None:
    if "mention_count_prev" not in row or "mention_count_last" not in row:
        return None
    prev = _opt_int(row.get("mention_count_prev"))
    last = _opt_int(row.get("mention_count_last"))
    observed = row.get("last_searched_at")
    if prev is None or last is None or not observed:
        return None
    return prev, last, str(observed)


def judge_onchain(
    row: Mapping[str, Any],
    snapshot: NewsReadSnapshot,
    calibration: OnchainCalibration | None,
) -> OnchainJudgment:
    """Label alignment only from heat evolution plus book features.

    A headline, a stored ``onchain_status``, or a single heat counter is not
    evidence. ``orderbook_imbalance_20`` is depth-20 and is not ``obi_top5``.
    ``perp_taker_buy_sell_ratio`` is a different unit and is not
    ``taker_buy_ratio``. Incomplete evidence stays ``pending``. Without
    ``calibration`` the label stays ``pending`` even when every input is present.
    ``x_watch`` is a current counter, not a history, and its mention counts
    are page-capped near 10.
    """
    if snapshot.context_count <= 0:
        return OnchainJudgment("pending", "no_news_context")

    as_of = parse_ts(snapshot.as_of) or datetime.now(timezone.utc)
    as_of_ms = int(as_of.timestamp() * 1000)
    max_age = calibration.max_feature_age_min if calibration is not None else EVIDENCE_MAX_AGE_MIN
    symbol = _row_symbol(row)
    missing: list[str] = []

    # Live top-5 only. Depth-20 imbalance is a different series and is not copied in.
    obi = _finite(row.get("obi_top5"))
    if obi is None:
        obi = _finite(row.get("spot_obi_top5"))
    if obi is None:
        missing.append("obi_top5")

    taker = _finite(row.get("taker_buy_ratio"))
    if taker is None:
        taker = _finite(row.get("spot_taker_buy_ratio"))
    if taker is None:
        taker = _finite(row.get("taker_buy_ratio_5m"))
    if taker is None:
        taker = _finite(row.get("taker_buy_ratio_1h"))
    if taker is None and symbol:
        point = _latest_point(snapshot.taker_points, symbol, as_of_ms, max_age_min=max_age)
        taker = None if point is None else point.value
    if taker is None:
        missing.append("taker_buy_ratio")

    whale = _finite(row.get("whale_retail_divergence"))
    if whale is None and symbol:
        point = _latest_point(snapshot.whale_points, symbol, as_of_ms, max_age_min=max_age)
        whale = None if point is None else point.value
    if whale is None:
        missing.append("whale_retail_divergence")

    heat = _heat_from_row(row)
    heat_reason: str | None = None
    if heat is None:
        point, match_reason = _match_x(row, snapshot.x_points)
        if match_reason:
            heat_reason = match_reason
        elif point is None or point.mention_count_prev is None or point.mention_count_last is None:
            heat_reason = "heat_evolution"
        elif not point.last_searched_at:
            heat_reason = "heat_observed_at"
        else:
            heat = (point.mention_count_prev, point.mention_count_last, point.last_searched_at)
    if heat is None:
        missing.append(heat_reason or "heat_evolution")
    else:
        heat_age = age_minutes(heat[2], as_of)
        if heat_age is None:
            missing.append("heat_observed_at")
        elif heat_age < -FUTURE_SKEW_MIN:
            missing.append("heat_future")
        elif heat_age > max_age:
            missing.append("heat_stale")

    if missing:
        return OnchainJudgment("pending", "missing:" + ",".join(missing))
    if calibration is None:
        return OnchainJudgment("pending", "awaiting_calibration")
    if heat is None or obi is None or taker is None or whale is None:
        return OnchainJudgment("pending", "missing:evidence")

    delta = heat[1] - heat[0]
    if delta < calibration.min_heat_delta:
        return OnchainJudgment("pending", "heat_not_confirmed")
    aligned = (
        obi >= calibration.min_obi_aligned
        and taker >= calibration.min_taker_aligned
        and whale >= calibration.min_whale_aligned
    )
    divergent = (
        obi <= calibration.max_obi_divergent
        and taker <= calibration.max_taker_divergent
        and whale <= calibration.max_whale_divergent
    )
    if aligned and divergent:
        return OnchainJudgment("pending", "threshold_conflict")
    if aligned:
        return OnchainJudgment("aligned", "features_aligned")
    if divergent:
        return OnchainJudgment("divergent", "features_divergent")
    return OnchainJudgment("pending", "inconclusive")


def _meta_base(snapshot: NewsReadSnapshot, shock: NewsShockStatus | None) -> dict[str, Any]:
    return {
        "news_gate_mode": MODE_RO,
        "news_gate_availability": snapshot.availability,
        "news_gate_detail": snapshot.detail,
        "news_calibration_status": CALIBRATION_STATUS,
        "news_shock_severity": "none" if shock is None else shock.severity,
        "news_shock_keyword": None if shock is None else shock.matched_keyword,
        "news_shock_headline": None if shock is None else shock.headline,
        "news_shock_source": None if shock is None else shock.source,
        "news_shock_age_min": None if shock is None else shock.age_min,
        "news_shock_applied": False,
        "news_onchain_status": "pending",
        "news_onchain_reason": None,
        "news_aligned_rank": "shadow",
        "news_aligned_rank_weight": None,
        "perp_beta_hedge_already_on": False,
        "news_hedge_label_only": False,
        "news_priority": None,
    }


def observation_for_passthrough(snapshot: NewsReadSnapshot) -> dict[str, Any]:
    """Fields for a SELL (or any path that must not change the order)."""
    shock = primary_shock(snapshot.shocks) if snapshot.availability == "ok" else None
    meta = _meta_base(snapshot, shock)
    meta["news_shock_applied"] = False
    return meta


def evaluate_news_overlay(
    *,
    regime: str,
    allow_buy: bool,
    snapshot: NewsReadSnapshot,
    row: Mapping[str, Any],
    calibration: OnchainCalibration | None = None,
) -> NewsOverlay:
    """Tighten an existing decision. Never opens a buy that was already closed."""
    shock = primary_shock(snapshot.shocks) if snapshot.availability == "ok" else None
    meta = _meta_base(snapshot, shock)
    if snapshot.availability != "ok":
        meta["news_onchain_status"] = "pending"
        meta["news_onchain_reason"] = f"news_db_{snapshot.availability}"
        return NewsOverlay(NewsRouteEffect.KEEP, meta)

    judgment = judge_onchain(row, snapshot, calibration)
    meta["news_onchain_status"] = judgment.status
    meta["news_onchain_reason"] = judgment.reason

    if shock is not None and shock.severity == "crisis":
        if regime == REGIME_CRISIS:
            meta["news_priority"] = "existing_crisis"
            return NewsOverlay(NewsRouteEffect.KEEP, meta)
        meta["news_shock_applied"] = True
        meta["news_hedge_label_only"] = True
        meta["perp_beta_hedge_already_on"] = False
        return NewsOverlay(NewsRouteEffect.CRISIS_HALT, meta)

    if shock is not None and shock.severity == "hawkish":
        if regime == REGIME_CRISIS or regime == REGIME_DEFENSIVE or not allow_buy:
            meta["news_priority"] = "existing_tighter_or_crisis"
            return NewsOverlay(NewsRouteEffect.KEEP, meta)
        meta["news_shock_applied"] = True
        meta["news_hedge_label_only"] = True
        meta["perp_beta_hedge_already_on"] = False
        return NewsOverlay(NewsRouteEffect.HAWKISH_DEFENSIVE, meta)

    if regime == REGIME_BULL and allow_buy and judgment.status == "divergent":
        meta["news_shock_applied"] = True
        return NewsOverlay(NewsRouteEffect.DIVERGENT_REJECT, meta)
    return NewsOverlay(NewsRouteEffect.KEEP, meta)


def snapshot_observation(snapshot: NewsReadSnapshot) -> dict[str, Any]:
    shock = primary_shock(snapshot.shocks) if snapshot.availability == "ok" else None
    return {
        "mode": snapshot.mode,
        "availability": snapshot.availability,
        "detail": snapshot.detail,
        "calibration_status": CALIBRATION_STATUS,
        "aligned_rank_weight": None,
        "onchain_thresholds_enabled": False,
        "shock_count": len(snapshot.shocks),
        "context_count": snapshot.context_count,
        "scanned": snapshot.scanned,
        "x_watch": snapshot.x_watch_availability,
        "x_watch_detail": snapshot.x_watch_detail,
        "features": snapshot.feature_availability,
        "feature_detail": snapshot.feature_detail,
        "primary_severity": None if shock is None else shock.severity,
        "window_min": snapshot.window_min,
        "db_path": snapshot.db_path,
    }


def window_start(now: datetime, minutes: int = NEWS_WINDOW_MIN) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc) - timedelta(minutes=minutes)
