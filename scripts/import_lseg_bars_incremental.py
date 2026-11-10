#!/usr/bin/env python3
"""Incremental import of CityU LSEG bar CSVs into ``cross_asset_bars``.

``scripts/fetch_cityu_lseg_incremental.py`` only exports CSVs. It does not
open a database. The full CityU ingester always reads one fixed volume and
also reloads the institutional database. This entry imports only the two
bar files, into a database path you pass, and leaves every other table alone.

Bar time comes from the CSV ``Timestamp`` column. The importer clock is used
only to describe whether the latest bar is inside a trading session.

These exports are not imported, because their columns are not the columns
the existing readers use:

- ``lseg_cityu_etf_flows_nav.csv`` is fund flows plus NAV. The old ingest
  expects ``lseg_cityu_etf_nav_daily.csv`` and stores it as an untyped dump
  in ``lseg_etf_nav_daily``. A flow is not a NAV.
- ``lseg_cityu_starmine_crowding.csv`` mixes ``TR.ShortInterestPct`` with
  StarMine scores. ``lseg_short_interest_daily`` is read as
  ``ric, date, short_interest_pct``. Crowding scores are not that column.
- ``lseg_cityu_institutional_ownership.csv`` is quarterly holder share,
  holder count, and insider flow. Nothing in the decision engine reads it,
  and it is not the WRDS ``wrds_13f_holdings`` dump.
- ``lseg_cityu_cftc_cot_positioning.csv`` is weekly ``get_history`` bars for
  ``0#...CFTC`` RICs. The reader expects ``symbol='BTC'`` plus
  ``open_interest``, ``lev_money_net``, and ``asset_mgr_net``. Those
  position columns are not in the history export.

No LSEG login, no subscription call, and no synthetic rows.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

BAR_FILENAMES = (
    "lseg_cityu_core_refresh_bars.csv",
    "lseg_cityu_incremental_bars.csv",
)
INSTITUTIONAL_DB_NAME = "cityu_institutional.db"
INSTITUTIONAL_TABLES = frozenset(
    {
        "wrds_optionmetrics_vsurfd",
        "wrds_13f_holdings",
        "wrds_crsp_treasuries",
        "orbis_bank_liquidity_risk",
        "lseg_cftc_cot_positioning",
        "lseg_short_interest_daily",
    }
)

# Export field lists from fetch_cityu_lseg_incremental.py. These are the
# requested Refinitiv fields, not verified CSV headers from a live export.
SLOW_EXPORT_CONTRACTS: dict[str, dict[str, object]] = {
    "lseg_cityu_etf_flows_nav.csv": {
        "safe_to_import": False,
        "export_fields": [
            "TR.FundNetFlows.date",
            "TR.FundNetFlows",
            "TR.NETASSETVAL",
            "TR.CompanyMarketCap",
            "TR.SharesOutstanding",
        ],
        "existing_ingest_file": "lseg_cityu_etf_nav_daily.csv",
        "existing_table": "lseg_etf_nav_daily",
        "reader": None,
        "cycle": "daily fund print; one business day of lag stays inside the cycle",
        "blocked_because": (
            "TR.FundNetFlows is net creation or redemption. TR.NETASSETVAL is NAV. "
            "The existing ingest file is a different name and is stored as an untyped "
            "dump. Renaming this file onto lseg_etf_nav_daily would call a flow a NAV."
        ),
    },
    "lseg_cityu_starmine_crowding.csv": {
        "safe_to_import": False,
        "export_fields": [
            "TR.ShortInterestPct.date",
            "TR.ShortInterestPct",
            "TR.StarMineShortSqueeze",
            "TR.StarMineARM",
            "TR.StarMineSmartHoldings",
            "TR.StarMineCombinedAlpha",
        ],
        "existing_ingest_file": "lseg_cityu_short_interest_daily.csv",
        "existing_table": "lseg_short_interest_daily",
        "reader_columns": ["ric", "date", "short_interest_pct"],
        "reader_rics": ["MARA.O", "CLSK.O", "RIOT.O", "COIN.O", "MSTR.O"],
        "cycle": "slow short-interest print; 2026-11-09 is still inside the cycle on 2026-11-10",
        "blocked_because": (
            "The file mixes short-interest percent with StarMine crowding scores. "
            "Refinitiv CSV headers from get_data are not verified here, so "
            "TR.ShortInterestPct cannot be split into ric/date/short_interest_pct. "
            "A crowding score is not short_interest_pct."
        ),
    },
    "lseg_cityu_institutional_ownership.csv": {
        "safe_to_import": False,
        "export_fields": [
            "TR.PctOfSharesOutHeld.date",
            "TR.PctOfSharesOutHeld",
            "TR.NumOfInstitutionalHolders",
            "TR.InsiderNetSharesTraded",
        ],
        "existing_ingest_file": None,
        "existing_table": "wrds_13f_holdings",
        "reader": None,
        "cycle": "quarterly; a 30-minute age check does not apply",
        "blocked_because": (
            "Quarterly holder share, holder count, and insider flow are not the "
            "WRDS 13F holdings dump, and no decision-engine reader selects these fields."
        ),
    },
    "lseg_cityu_cftc_cot_positioning.csv": {
        "safe_to_import": False,
        "export_source": "rd.get_history(interval='1W') plus cot_family_ric",
        "existing_ingest_file": "lseg_cityu_cftc_cot_positioning.csv",
        "existing_table": "lseg_cftc_cot_positioning",
        "reader_columns": ["date", "symbol", "open_interest", "lev_money_net", "asset_mgr_net"],
        "reader_filter": "symbol = 'BTC'",
        "cycle": "Tuesday as-of, Friday publication; 2026-11-03 is still the current report on 2026-11-10",
        "blocked_because": (
            "Weekly price history for 0#...CFTC RICs does not contain open interest "
            "or leveraged-fund and asset-manager net positions. Those reader columns "
            "cannot be derived from this export."
        ),
    },
}

_US_CASH_SUFFIXES = (".O", ".P", ".K", ".Z", ".N")
_CRYPTO_SPOT = frozenset({"BTC=", "ETH=", "SOL="})
_FX = frozenset(
    {
        "EUR=", "JPY=", "CNH=", "AUDJPY=", "EURJPY=", "KRW=", "SGD=", "CHF=",
        "MXN=", "XAU=", "SOFR=", "USONFFE=",
    }
)
_NEW_YORK = ZoneInfo("America/New_York")
_UPDATABLE_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "bid",
    "ask",
    "spread_bps",
    "bid_nummov",
    "ask_nummov",
    "quote_imbalance",
    "vwap",
)
_ADDED_COLUMNS = tuple((name, "REAL") for name in _UPDATABLE_FIELDS)


@dataclass(frozen=True)
class BarRow:
    ric: str
    interval: str
    utc_ts: str
    utc_ms: int
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    bid: float | None
    ask: float | None
    spread_bps: float | None
    bid_nummov: float | None
    ask_nummov: float | None
    quote_imbalance: float | None
    vwap: float | None
    provided: frozenset[str] = frozenset()


@dataclass
class ImportResult:
    ok: bool
    exit_code: int
    db_path: str
    files: list[str] = field(default_factory=list)
    upserted: int = 0
    rejected: int = 0
    verified: int = 0
    freshness: list[dict[str, object]] = field(default_factory=list)
    refused: list[dict[str, object]] = field(default_factory=list)
    message: str = ""


class ImportError_(RuntimeError):
    """The import did not commit. Existing rows are unchanged."""


def _finite(val: object) -> float | None:
    if val is None:
        return None
    text = str(val).strip()
    if text == "":
        return None
    try:
        num = float(text)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _first(row: Mapping[str, str], *names: str) -> float | None:
    for name in names:
        if name in row:
            num = _finite(row.get(name))
            if num is not None:
                return num
    return None


def parse_source_timestamp(raw: object) -> tuple[str, int] | None:
    """Return UTC ``(iso_z, epoch_ms)`` from a LSEG ``Timestamp`` cell.

    An explicit ``Z`` or numeric offset is honored. A naive value is read as
    UTC, which is how the existing bar ingest treats ``Timestamp``. The
    importer clock is never substituted.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z") or _has_numeric_offset(text):
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        normalized = normalized.replace(" ", "T", 1)
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        utc = parsed.astimezone(timezone.utc)
        return utc.strftime("%Y-%m-%dT%H:%M:%SZ"), int(utc.timestamp() * 1000)
    # Full naive datetimes are UTC, matching the existing space-separated ingest.
    # A ``T`` separator must not fall through to the date-only format.
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y/%m/%dT%H:%M:%S"):
        try:
            naive = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return naive.strftime("%Y-%m-%dT%H:%M:%SZ"), int(naive.timestamp() * 1000)
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            naive = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return naive.strftime("%Y-%m-%dT%H:%M:%SZ"), int(naive.timestamp() * 1000)
    return None


def _has_numeric_offset(text: str) -> bool:
    if len(text) < 6 or text[-3] != ":":
        return False
    sign = text[-6]
    return sign in "+-" and text[-5:-3].isdigit() and text[-2:].isdigit()


def bar_from_csv_row(row: Mapping[str, str]) -> BarRow | None:
    ric = str(row.get("ric") or row.get("RIC") or "").strip()
    interval = str(row.get("interval") or row.get("Interval") or "").strip()
    stamp = row.get("Timestamp")
    if stamp is None:
        stamp = row.get("timestamp")
    parsed = parse_source_timestamp(stamp)
    if not ric or not interval or parsed is None:
        return None
    utc_ts, utc_ms = parsed
    provided: set[str] = set()
    open_px = _imported(row, provided, "open", "OPEN_PRC", "MID_OPEN", "OPEN_BID")
    high = _imported(row, provided, "high", "HIGH_1", "MID_HIGH", "BID_HIGH_1")
    low = _imported(row, provided, "low", "LOW_1", "MID_LOW", "BID_LOW_1")
    close = _imported(row, provided, "close", "TRDPRC_1", "MID_PRICE")
    volume = _imported(row, provided, "volume", "ACVOL_UNS", "NUM_MOVES")
    bid = _imported(row, provided, "bid", "BID")
    ask = _imported(row, provided, "ask", "ASK")
    bid_moves = _imported(row, provided, "bid_nummov", "BID_NUMMOV")
    ask_moves = _imported(row, provided, "ask_nummov", "ASK_NUMMOV")
    vwap = _imported(row, provided, "vwap", "VWAP")
    if close is None and "close" not in provided and bid is not None:
        close = bid
        provided.add("close")
    spread = None
    if bid is not None and ask is not None and bid > 0 and ask >= bid:
        mid = 0.5 * (bid + ask)
        if mid > 0:
            spread = round(((ask - bid) / mid) * 10_000.0, 4)
            provided.add("spread_bps")
    imbalance = None
    if bid_moves is not None and ask_moves is not None and (bid_moves + ask_moves) > 0:
        imbalance = round((bid_moves - ask_moves) / (bid_moves + ask_moves), 4)
        provided.add("quote_imbalance")
    return BarRow(
        ric=ric,
        interval=interval,
        utc_ts=utc_ts,
        utc_ms=utc_ms,
        open=open_px,
        high=high,
        low=low,
        close=close,
        volume=volume,
        bid=bid,
        ask=ask,
        spread_bps=spread,
        bid_nummov=bid_moves,
        ask_nummov=ask_moves,
        quote_imbalance=imbalance,
        vwap=vwap,
        provided=frozenset(provided),
    )


def _imported(row: Mapping[str, str], provided: set[str], field: str, *names: str) -> float | None:
    """Parse a value only when one of ``names`` is a real CSV column with a number."""
    if not any(name in row for name in names):
        return None
    value = _first(row, *names)
    if value is None:
        return None
    provided.add(field)
    return value


def read_bar_csv(path: Path) -> tuple[list[BarRow], int]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not _has_timestamp_header(reader.fieldnames):
            raise ImportError_(f"{path.name} has no Timestamp column")
        rows: list[BarRow] = []
        rejected = 0
        for raw in reader:
            parsed = bar_from_csv_row(raw)
            if parsed is None:
                rejected += 1
                continue
            rows.append(parsed)
    return rows, rejected


def _has_timestamp_header(names: Sequence[str]) -> bool:
    return any(str(name).strip().lower() == "timestamp" for name in names)


def dedupe_bars(rows: Iterable[BarRow]) -> list[BarRow]:
    """Last row for a ``(ric, interval, utc_ms)`` key wins."""
    keyed: dict[tuple[str, str, int], BarRow] = {}
    for row in rows:
        keyed[(row.ric, row.interval, row.utc_ms)] = row
    return list(keyed.values())


def ensure_bar_schema(conn: sqlite3.Connection) -> None:
    """Create the bar table, or add missing value columns without dropping rows."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cross_asset_bars'"
    ).fetchone()
    if exists is None:
        conn.execute(
            """
            CREATE TABLE cross_asset_bars (
                ric TEXT NOT NULL,
                interval TEXT NOT NULL,
                utc_ts TEXT NOT NULL,
                utc_ms INTEGER NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                bid REAL,
                ask REAL,
                spread_bps REAL,
                bid_nummov REAL,
                ask_nummov REAL,
                quote_imbalance REAL,
                vwap REAL,
                PRIMARY KEY (ric, interval, utc_ms)
            )
            """
        )
    else:
        have = {str(row[1]) for row in conn.execute("PRAGMA table_info(cross_asset_bars)")}
        for name, col_type in _ADDED_COLUMNS:
            if name not in have:
                conn.execute(f"ALTER TABLE cross_asset_bars ADD COLUMN {name} {col_type}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cab_ric_int_ms ON cross_asset_bars(ric, interval, utc_ms DESC)"
    )


def _upsert_bars(conn: sqlite3.Connection, rows: Sequence[BarRow]) -> None:
    """Insert or update only columns the CSV actually carried.

    ``INSERT OR REPLACE`` deletes the row and would null ``open_interest``,
    ``block_volume``, and any other column this import does not own.
    """
    if not rows:
        return
    have = {str(row[1]) for row in conn.execute("PRAGMA table_info(cross_asset_bars)")}
    grouped: dict[tuple[str, ...], list[BarRow]] = {}
    for row in rows:
        fields = tuple(
            name
            for name in _UPDATABLE_FIELDS
            if name in row.provided and name in have and getattr(row, name) is not None
        )
        grouped.setdefault(fields, []).append(row)
    for fields, group in grouped.items():
        cols = ["ric", "interval", "utc_ms", "utc_ts", *fields]
        assignments = ["utc_ts=excluded.utc_ts", *[f"{name}=excluded.{name}" for name in fields]]
        sql = (
            f"INSERT INTO cross_asset_bars ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)}) "
            f"ON CONFLICT(ric, interval, utc_ms) DO UPDATE SET {', '.join(assignments)}"
        )
        payload = []
        for row in group:
            values: list[object] = [row.ric, row.interval, row.utc_ms, row.utc_ts]
            values.extend(getattr(row, name) for name in fields)
            payload.append(values)
        conn.executemany(sql, payload)


def verify_bars(conn: sqlite3.Connection, rows: Sequence[BarRow]) -> int:
    if not rows:
        return 0
    checked = 0
    for sample in (rows[0], rows[len(rows) // 2], rows[-1]):
        found = conn.execute(
            """
            SELECT utc_ts, utc_ms, close FROM cross_asset_bars
            WHERE ric = ? AND interval = ? AND utc_ms = ?
            """,
            (sample.ric, sample.interval, sample.utc_ms),
        ).fetchone()
        if found is None or int(found[1]) != sample.utc_ms or str(found[0]) != sample.utc_ts:
            raise ImportError_("verification failed: stored time does not match Timestamp")
        if found[2] != sample.close:
            raise ImportError_("verification failed: stored close does not match the CSV")
        checked += 1
    return checked


def session_family(ric: str) -> str:
    symbol = str(ric or "").strip()
    if symbol in _CRYPTO_SPOT:
        return "crypto_spot"
    if symbol.endswith(_US_CASH_SUFFIXES):
        return "us_cash"
    if symbol in _FX or (symbol.endswith("=") and not symbol[:3].isalpha()):
        return "fx"
    if symbol.endswith(("c1", "c2")) or symbol.startswith("."):
        return "listed_session"
    if symbol.endswith("="):
        return "fx"
    return "unclassified"


def _ny(moment: datetime) -> datetime:
    return moment.astimezone(_NEW_YORK)


def _us_cash_open(moment: datetime) -> bool:
    """US cash session in America/New_York, so EST and EDT both map to 09:30–16:00."""
    local = _ny(moment)
    if local.weekday() >= 5:
        return False
    minutes = local.hour * 60 + local.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


def _last_us_session_date(moment: datetime) -> date:
    local = _ny(moment)
    if _us_cash_open(moment):
        return local.date()
    if local.weekday() < 5 and (local.hour * 60 + local.minute) >= 16 * 60:
        return local.date()
    cursor = local - timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor.date()


def _fx_open(moment: datetime) -> bool:
    weekday = moment.weekday()
    minutes = moment.hour * 60 + moment.minute
    if weekday == 5:  # Saturday
        return False
    if weekday == 6:  # Sunday
        return minutes >= 22 * 60
    if weekday == 4 and minutes >= 22 * 60:  # Friday after 22:00 UTC
        return False
    return True


def bar_freshness(ric: str, interval: str, latest_utc_ms: int, now_ms: int) -> str:
    """Session-aware label for the latest bar. This does not delete data.

    A flat 30-minute rule is not used. Daily bars and closed sessions stay
    ``closed_session`` or ``fresh`` when the bar belongs to the latest session.
    """
    family = session_family(ric)
    latest = datetime.fromtimestamp(latest_utc_ms / 1000.0, tz=timezone.utc)
    now = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)
    age_ms = now_ms - latest_utc_ms
    if family == "unclassified":
        return "unclassified"
    if interval == "1d":
        return _daily_freshness(family, latest.date(), now)
    open_now = _family_open(family, now)
    if not open_now:
        if _bar_in_last_closed_session(family, latest, now):
            return "closed_session"
        return "missed_session"
    limit_ms = _open_session_limit_ms(interval)
    if limit_ms is None:
        return "unclassified"
    if age_ms <= limit_ms:
        return "fresh"
    return "stale_in_session"


def _family_open(family: str, moment: datetime) -> bool:
    if family == "crypto_spot":
        return True
    if family == "us_cash":
        return _us_cash_open(moment)
    if family == "fx":
        return _fx_open(moment)
    if family == "listed_session":
        return _fx_open(moment)
    return False


def _open_session_limit_ms(interval: str) -> int | None:
    if interval == "5min":
        return 15 * 60 * 1000
    if interval == "1h":
        return 2 * 60 * 60 * 1000
    return None


def _daily_freshness(family: str, bar_day: date, now: datetime) -> str:
    if family == "crypto_spot":
        age_days = (now.date() - bar_day).days
        return "fresh" if age_days <= 1 else "stale_in_session"
    expected = _last_us_session_date(now) if family == "us_cash" else now.date()
    if family != "us_cash" and not _family_open(family, now):
        expected = _last_weekday(now.date(), 4 if now.weekday() in (5, 6) else now.weekday())
    if bar_day == expected or bar_day == now.date():
        return "fresh" if _family_open(family, now) else "closed_session"
    if bar_day < expected:
        return "missed_session"
    return "fresh"


def _last_weekday(day: date, weekday: int) -> date:
    cursor = day
    while cursor.weekday() != weekday:
        cursor -= timedelta(days=1)
    return cursor


def _bar_in_last_closed_session(family: str, latest: datetime, now: datetime) -> bool:
    if family == "us_cash":
        session_day = _last_us_session_date(now)
        local = _ny(latest)
        if local.date() != session_day:
            return False
        minutes = local.hour * 60 + local.minute
        return 9 * 60 + 30 <= minutes <= 16 * 60
    if family in ("fx", "listed_session"):
        return _fx_open(latest) and (now - latest) <= timedelta(days=3)
    return False


def slow_variable_cycle(kind: str, source_day: date, asof: date) -> str:
    """Cycle label for slow prints. A 30-minute age is not consulted.

    CFTC is the Tuesday report whose Friday release is already on or before
    ``asof``. On 2026-11-10 that report is still 2026-11-03. Short interest
    dated 2026-11-09 is still inside its slow window on 2026-11-10.
    """
    if kind == "cftc":
        current = _current_cftc_asof(asof)
        if source_day == current:
            return "current_cycle"
        if source_day == current - timedelta(days=7):
            return "prior_cycle"
        return "outside_cycle"
    if kind == "short_interest":
        age_days = (asof - source_day).days
        if 0 <= age_days <= 10:
            return "inside_slow_cycle"
        return "outside_cycle"
    if kind == "ownership":
        age_days = (asof - source_day).days
        if 0 <= age_days <= 120:
            return "inside_slow_cycle"
        return "outside_cycle"
    if kind == "etf_flow":
        age_days = (asof - source_day).days
        if 0 <= age_days <= 4:
            return "inside_slow_cycle"
        return "outside_cycle"
    return "unclassified"


def _current_cftc_asof(asof: date) -> date:
    days_since_tuesday = (asof.weekday() - 1) % 7
    latest_tuesday = asof - timedelta(days=days_since_tuesday)
    publication = latest_tuesday + timedelta(days=3)
    if asof < publication:
        return latest_tuesday - timedelta(days=7)
    return latest_tuesday


def uses_thirty_minute_rule(kind: str) -> bool:
    """Slow prints never use a 30-minute freshness cutoff."""
    del kind
    return False


def contract_for_filename(name: str) -> dict[str, object] | None:
    found = SLOW_EXPORT_CONTRACTS.get(Path(name).name)
    if found is None:
        return None
    return {"file": Path(name).name, **found}


def _latest_freshness(rows: Sequence[BarRow], now_ms: int) -> list[dict[str, object]]:
    latest: dict[tuple[str, str], BarRow] = {}
    for row in rows:
        key = (row.ric, row.interval)
        prev = latest.get(key)
        if prev is None or row.utc_ms > prev.utc_ms:
            latest[key] = row
    report = []
    for (ric, interval), row in sorted(latest.items()):
        report.append(
            {
                "ric": ric,
                "interval": interval,
                "source_utc_ms": row.utc_ms,
                "source_utc_ts": row.utc_ts,
                "session": session_family(ric),
                "freshness": bar_freshness(ric, interval, row.utc_ms, now_ms),
            }
        )
    return report


def _refuse_institutional(db_path: Path) -> None:
    if db_path.name == INSTITUTIONAL_DB_NAME:
        raise ImportError_(
            f"refusing {db_path}: bars do not go into the CityU institutional database"
        )
    if not db_path.is_file():
        return
    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        names = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    if names & INSTITUTIONAL_TABLES:
        raise ImportError_(
            f"refusing {db_path}: file contains CityU institutional tables and will not be rewritten"
        )


def resolve_inputs(
    input_dir: Path | None,
    files: Sequence[Path],
) -> tuple[list[Path], list[dict[str, object]]]:
    selected: list[Path] = []
    refused: list[dict[str, object]] = []
    if input_dir is not None:
        if not input_dir.is_dir():
            raise ImportError_(f"input dir not found: {input_dir}")
        for name in BAR_FILENAMES:
            candidate = input_dir / name
            if candidate.is_file():
                selected.append(candidate)
        for path in sorted(input_dir.glob("*.csv")):
            contract = contract_for_filename(path.name)
            if contract is not None:
                refused.append(contract)
    for raw in files:
        path = Path(raw)
        contract = contract_for_filename(path.name)
        if contract is not None:
            refused.append(contract)
            continue
        if path.name not in BAR_FILENAMES:
            raise ImportError_(f"{path.name} is not a bar export this entry can load")
        if not path.is_file():
            raise ImportError_(f"bar file not found: {path}")
        if path not in selected:
            selected.append(path)
    # Directory scan can list a contract twice if --file repeats it.
    unique_refused: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in refused:
        key = str(item.get("file"))
        if key in seen:
            continue
        seen.add(key)
        unique_refused.append(item)
    return selected, unique_refused


def import_lseg_bars(
    db_path: Path,
    *,
    input_dir: Path | None = None,
    files: Sequence[Path] = (),
    now_ms: int | None = None,
) -> ImportResult:
    """Import bar CSVs in one transaction. Failure rolls back."""
    db_path = Path(db_path)
    clock_ms = int(now_ms if now_ms is not None else datetime.now(timezone.utc).timestamp() * 1000)
    try:
        selected, refused = resolve_inputs(input_dir, files)
    except ImportError_ as exc:
        return ImportResult(False, 2, str(db_path), message=str(exc))
    if not selected:
        return ImportResult(
            False,
            2,
            str(db_path),
            refused=refused,
            message="no bar CSV to import",
        )
    parsed: list[BarRow] = []
    rejected = 0
    try:
        for path in selected:
            rows, bad = read_bar_csv(path)
            parsed.extend(rows)
            rejected += bad
    except (OSError, ImportError_, csv.Error) as exc:
        return ImportResult(False, 1, str(db_path), files=[str(p) for p in selected], message=str(exc))
    rows = dedupe_bars(parsed)
    if parsed and not rows:
        return ImportResult(False, 2, str(db_path), files=[str(p) for p in selected], rejected=rejected, message="no valid bar rows")
    try:
        _refuse_institutional(db_path)
    except ImportError_ as exc:
        return ImportResult(False, 2, str(db_path), message=str(exc))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        conn.execute("BEGIN")
        ensure_bar_schema(conn)
        _upsert_bars(conn, rows)
        verified = verify_bars(conn, rows)
        conn.commit()
    except Exception as exc:  # noqa: BLE001 — caller must see a clean database
        conn.rollback()
        return ImportResult(
            False,
            1,
            str(db_path),
            files=[str(p) for p in selected],
            rejected=rejected,
            refused=refused,
            message=f"rolled back: {type(exc).__name__}: {exc}",
        )
    finally:
        conn.close()
    return ImportResult(
        True,
        0,
        str(db_path),
        files=[str(p) for p in selected],
        upserted=len(rows),
        rejected=rejected,
        verified=verified,
        freshness=_latest_freshness(rows, clock_ms),
        refused=refused,
        message="committed",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import CityU LSEG bar CSVs into cross_asset_bars.")
    parser.add_argument("--db", type=Path, required=True, help="Target lseg_macro.db. Not cityu_institutional.db.")
    parser.add_argument("--input-dir", type=Path, default=None, help="Directory to scan for the two bar CSV names.")
    parser.add_argument("--file", action="append", default=[], type=Path, help="Explicit bar CSV. Repeatable.")
    parser.add_argument(
        "--now-ms",
        type=int,
        default=None,
        help="Clock used only for the session freshness report. Does not replace Timestamp.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = import_lseg_bars(
        args.db,
        input_dir=args.input_dir,
        files=args.file,
        now_ms=args.now_ms,
    )
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    if result.message and not result.ok:
        print(result.message, file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
