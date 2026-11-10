"""Append-only observation log for one news poll.

The news database is never written here. ``onchain_status`` is copied onto
the log row and left unchanged in the source. Trading weights are not
updated. Two weeks of rows are a sample, not a calibration.

``obi_top5`` is recorded only when that column exists on the feature
source. ``orderbook_imbalance_20`` stays in its own column. ``taker_buy_ratio``
is a buy share in ``[0, 1]``. ``perp_taker_buy_sell_ratio`` stays in its own
column. There is no X news library in the 2026-11-10 inventory, so
``x_news_status`` is ``unavailable`` and no X event row is invented.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from decision_engine.core.news_shock_gate import (
    EVIDENCE_MAX_AGE_MIN,
    open_readonly,
    parse_ts,
    repo_root,
    resolve_derivatives_path,
    table_columns,
)

SCHEMA_VERSION = "feature_obs_v1"
X_NEWS_STATUS = "unavailable"
LOG_ENV = "NEWS_FEATURE_OBS_LOG"

_DDL = """
CREATE TABLE IF NOT EXISTS feature_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_id TEXT NOT NULL,
    event_id INTEGER NOT NULL,
    event_url TEXT NOT NULL,
    source TEXT,
    source_tier TEXT,
    symbol TEXT NOT NULL DEFAULT '',
    venue TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    fetched_at TEXT,
    observed_at TEXT NOT NULL,
    available_at TEXT,
    feature_at TEXT,
    obi_top5 REAL,
    obi_top5_status TEXT NOT NULL,
    orderbook_imbalance_20 REAL,
    imbalance20_status TEXT NOT NULL,
    taker_buy_ratio REAL,
    taker_buy_ratio_status TEXT NOT NULL,
    perp_taker_buy_sell_ratio REAL,
    perp_taker_status TEXT NOT NULL,
    whale_retail_divergence REAL,
    whale_status TEXT NOT NULL,
    x_news_status TEXT NOT NULL,
    mapping_status TEXT NOT NULL,
    missing_reason TEXT,
    schema_version TEXT NOT NULL,
    code_version TEXT NOT NULL,
    UNIQUE (poll_id, event_id, symbol, venue)
);
CREATE TABLE IF NOT EXISTS poll_meta (
    poll_id TEXT PRIMARY KEY,
    observed_at TEXT NOT NULL,
    x_news_status TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    code_version TEXT NOT NULL,
    event_rows INTEGER NOT NULL
);
"""


def default_log_path() -> Path:
    env = os.environ.get(LOG_ENV, "").strip()
    if env:
        return Path(env)
    return repo_root() / "news_feature_observation_log.db"


def code_version() -> str:
    override = os.environ.get("NEWS_OBS_CODE_VERSION", "").strip()
    if override:
        return override
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=str(repo_root()),
            timeout=2,
            stderr=subprocess.DEVNULL,
        )
        text = out.decode().strip()
        return text or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_min(feature_at: str | None, now: datetime) -> float | None:
    stamp = parse_ts(feature_at)
    if stamp is None:
        return None
    return (now.astimezone(timezone.utc) - stamp).total_seconds() / 60.0


class _FeatureRead:
    def __init__(self) -> None:
        self.obi_top5: float | None = None
        self.obi_status = "missing_column"
        self.imbalance20: float | None = None
        self.imbalance_status = "missing_db"
        self.taker: float | None = None
        self.taker_status = "missing_db"
        self.perp: float | None = None
        self.perp_status = "missing_db"
        self.whale: float | None = None
        self.whale_status = "missing_db"
        self.feature_at: str | None = None
        self.reason: str | None = None


def _latest_before(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...],
) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


def _read_features(
    *,
    symbol: str,
    venue: str,
    now: datetime,
    derivatives_db: Path | None,
    timeout_sec: float,
) -> _FeatureRead:
    """Read as-of features for one mapped symbol. Never renames columns."""
    out = _FeatureRead()
    if derivatives_db is None or not derivatives_db.exists():
        out.reason = "derivatives_missing"
        out.imbalance_status = "missing_db"
        out.taker_status = "missing_db"
        out.perp_status = "missing_db"
        out.whale_status = "missing_db"
        return out
    conn, failed, detail = open_readonly(derivatives_db, timeout_sec)
    if conn is None:
        status = failed or "error"
        out.reason = detail or status
        out.imbalance_status = status
        out.taker_status = status
        out.perp_status = status
        out.whale_status = status
        return out
    now_ms = int(now.timestamp() * 1000)
    try:
        micro_cols = table_columns(conn, "binance_spot_microstructure") or set()
        perp_cols = table_columns(conn, "binance_perp_counterparty") or set()
        if "obi_top5" in micro_cols:
            row = _latest_before(
                conn,
                """
                SELECT ts_ms, obi_top5 FROM binance_spot_microstructure
                WHERE symbol = ? AND ts_ms <= ?
                ORDER BY ts_ms DESC LIMIT 1
                """,
                (symbol, now_ms),
            )
            _assign_timed(out, row, "obi_top5", "obi_status", now, kind="obi")
        else:
            out.obi_status = "missing_column"
        if "orderbook_imbalance_20" in micro_cols:
            row = _latest_before(
                conn,
                """
                SELECT ts_ms, orderbook_imbalance_20 FROM binance_spot_microstructure
                WHERE symbol = ? AND ts_ms <= ?
                ORDER BY ts_ms DESC LIMIT 1
                """,
                (symbol, now_ms),
            )
            _assign_timed(out, row, "orderbook_imbalance_20", "imbalance_status", now, kind="imbalance")
        else:
            out.imbalance_status = "missing_column"
        taker_col = (
            "taker_buy_ratio_5m"
            if "taker_buy_ratio_5m" in micro_cols
            else ("taker_buy_ratio_1h" if "taker_buy_ratio_1h" in micro_cols else None)
        )
        if taker_col is None:
            out.taker_status = "missing_column"
        else:
            row = _latest_before(
                conn,
                f"""
                SELECT ts_ms, "{taker_col}" AS value FROM binance_spot_microstructure
                WHERE symbol = ? AND ts_ms <= ? AND "{taker_col}" IS NOT NULL
                ORDER BY ts_ms DESC LIMIT 1
                """,
                (symbol, now_ms),
            )
            _assign_timed(out, row, "value", "taker_status", now, kind="taker")
        if "perp_taker_buy_sell_ratio" in perp_cols:
            row = _latest_before(
                conn,
                """
                SELECT ts_ms, perp_taker_buy_sell_ratio FROM binance_perp_counterparty
                WHERE symbol = ? AND ts_ms <= ?
                ORDER BY ts_ms DESC LIMIT 1
                """,
                (symbol, now_ms),
            )
            _assign_timed(out, row, "perp_taker_buy_sell_ratio", "perp_status", now, kind="perp")
        else:
            out.perp_status = "missing_column"
        if "whale_retail_divergence" in perp_cols:
            row = _latest_before(
                conn,
                """
                SELECT ts_ms, whale_retail_divergence FROM binance_perp_counterparty
                WHERE symbol = ? AND ts_ms <= ?
                ORDER BY ts_ms DESC LIMIT 1
                """,
                (symbol, now_ms),
            )
            _assign_timed(out, row, "whale_retail_divergence", "whale_status", now, kind="whale")
        else:
            out.whale_status = "missing_column"
        if venue and venue.lower() not in {"binance", ""}:
            out.reason = "feature_store_is_binance_only"
    except sqlite3.Error as exc:
        out.reason = str(exc)
        out.taker_status = "error"
    finally:
        conn.close()
    return out


def _assign_timed(
    out: _FeatureRead,
    row: sqlite3.Row | None,
    column: str,
    status_attr: str,
    now: datetime,
    *,
    kind: str,
) -> None:
    if row is None or row[column] is None or row["ts_ms"] is None:
        setattr(out, status_attr, "missing_asof")
        return
    feature_at = datetime.fromtimestamp(int(row["ts_ms"]) / 1000.0, tz=timezone.utc)
    feature_iso = _iso(feature_at)
    age = _age_min(feature_iso, now)
    if out.feature_at is None:
        out.feature_at = feature_iso
    if age is None or age < 0 or age > EVIDENCE_MAX_AGE_MIN:
        setattr(out, status_attr, "expired")
        return
    value = float(row[column])
    setattr(out, status_attr, "ok")
    if kind == "obi":
        out.obi_top5 = value
    elif kind == "imbalance":
        out.imbalance20 = value
        out.imbalance_status = "depth20_not_top5"
    elif kind == "taker":
        out.taker = value
    elif kind == "perp":
        out.perp = value
        out.perp_status = "buy_sell_ratio_not_buy_share"
    elif kind == "whale":
        out.whale = value


def _open_log(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(_DDL)
    conn.commit()
    return conn


def record_news_poll(
    events: Sequence[Mapping[str, Any]],
    *,
    poll_id: str,
    observed_at: str,
    log_path: str | Path | None = None,
    derivatives_db: str | Path | None = None,
    symbol_by_url: Mapping[str, Mapping[str, str]] | None = None,
    timeout_sec: float = 1.0,
) -> dict[str, Any]:
    """Write one poll. Same ``poll_id`` is idempotent. Does not invent events."""
    if not poll_id:
        raise ValueError("poll_id is required")
    now = parse_ts(observed_at) or datetime.now(timezone.utc)
    path = Path(log_path) if log_path is not None else default_log_path()
    deriv = resolve_derivatives_path(derivatives_db)
    version = code_version()
    mapping = symbol_by_url or {}
    written = 0
    skipped = 0
    conn = _open_log(path)
    try:
        for event in events:
            event_id = event.get("id")
            url = str(event.get("url") or "")
            if event_id is None or not url:
                skipped += 1
                continue
            mapped = mapping.get(url) or {}
            symbol = str(mapped.get("symbol") or "").strip().upper()
            venue = str(mapped.get("venue") or "").strip().lower()
            if symbol and not venue:
                mapping_status = "mapping_failed"
                reason = "symbol_without_venue"
                symbol = ""
                venue = ""
                features = _FeatureRead()
                features.reason = reason
            elif not symbol:
                mapping_status = "unmapped"
                reason = "no_symbol_on_event"
                features = _FeatureRead()
                features.reason = reason
                features.obi_status = "unmapped"
                features.imbalance_status = "unmapped"
                features.taker_status = "unmapped"
                features.perp_status = "unmapped"
                features.whale_status = "unmapped"
            else:
                mapping_status = "mapped"
                reason = None
                features = _read_features(
                    symbol=symbol,
                    venue=venue,
                    now=now,
                    derivatives_db=deriv,
                    timeout_sec=timeout_sec,
                )
                if features.reason and reason is None:
                    reason = features.reason
            available_at = event.get("fetched_at") or event.get("available_at")
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO feature_observations (
                    poll_id, event_id, event_url, source, source_tier, symbol, venue,
                    published_at, fetched_at, observed_at, available_at, feature_at,
                    obi_top5, obi_top5_status, orderbook_imbalance_20, imbalance20_status,
                    taker_buy_ratio, taker_buy_ratio_status,
                    perp_taker_buy_sell_ratio, perp_taker_status,
                    whale_retail_divergence, whale_status,
                    x_news_status, mapping_status, missing_reason,
                    schema_version, code_version
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    poll_id,
                    int(event_id),
                    url,
                    event.get("source"),
                    event.get("source_tier"),
                    symbol,
                    venue,
                    event.get("published_at"),
                    event.get("fetched_at"),
                    _iso(now),
                    available_at,
                    features.feature_at,
                    features.obi_top5,
                    features.obi_status,
                    features.imbalance20,
                    features.imbalance_status,
                    features.taker,
                    features.taker_status,
                    features.perp,
                    features.perp_status,
                    features.whale,
                    features.whale_status,
                    X_NEWS_STATUS,
                    mapping_status,
                    reason,
                    SCHEMA_VERSION,
                    version,
                ),
            )
            if cur.rowcount and cur.rowcount > 0:
                written += 1
            else:
                skipped += 1
        conn.execute(
            """
            INSERT OR IGNORE INTO poll_meta (
                poll_id, observed_at, x_news_status, schema_version, code_version, event_rows
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (poll_id, _iso(now), X_NEWS_STATUS, SCHEMA_VERSION, version, written),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "poll_id": poll_id,
        "log_path": str(path),
        "rows_written": written,
        "rows_existing": skipped,
        "x_news_status": X_NEWS_STATUS,
        "schema_version": SCHEMA_VERSION,
        "code_version": version,
        "weights_changed": False,
        "source_onchain_status_written": False,
    }
