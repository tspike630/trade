"""Base indicator engine: DB read helpers, schema ensure, upsert, light cache."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from logging_setup import get_logger
from store import norm_address, norm_chain

log = get_logger("indicators.base")

# How long this connection waits for a SQLite lock. Matches Store
# (store.py PRAGMA busy_timeout=30000). It is not how long the lock is held.
INDICATOR_BUSY_TIMEOUT_MS = 30000

_ROOT = Path(__file__).resolve().parents[1]
_SQL_PATH = _ROOT / "models" / "indicator_tables.sql"

# Lookback seconds for interval returns
INTERVAL_SEC = {
    "change_5m": 5 * 60,
    "change_15m": 15 * 60,
    "change_1h": 60 * 60,
    "change_4h": 4 * 60 * 60,
    "change_24h": 24 * 60 * 60,
}

# All updatable columns on token_indicators (excluding PK + updated_at)
TOKEN_INDICATOR_COLS = [
    "change_5m",
    "change_15m",
    "change_1h",
    "change_4h",
    "change_24h",
    "volatility_24h",
    "volume_strength",
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "boll_upper",
    "boll_mid",
    "boll_lower",
    "jev_score",
    "jev_funding_bias",
    "jev_updated_at",
    "jev_raw_score",
    "jev_signal",
    "jev_confidence",
    "jev_strength",
    "sentiment_score",
    "sentiment_mentions_1h",
    "sentiment_mentions_6h",
    "sentiment_mentions_24h",
    "sentiment_growth_1h",
    "sentiment_growth_6h",
    "sentiment_growth_24h",
    "sentiment_heat_score",
    "sentiment_polarity",
    "sentiment_updated_at",
    "spike_ratio",
    "spike",
    "composite_score",
    "composite_updated_at",
    "tier",
    "primary_pair_address",
    "sample_count",
    "tech_updated_at",
]

# History columns filled from the live row when the writer omits them.
# Explicit None in the writer payload is kept (not replaced).
_HISTORY_FILL_IF_ABSENT = (
    "change_5m",
    "change_15m",
    "change_1h",
    "change_4h",
    "change_24h",
    "volatility_24h",
    "volume_strength",
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "boll_upper",
    "boll_mid",
    "boll_lower",
    "jev_score",
    "jev_strength",
    "sentiment_score",
    "sentiment_mentions_1h",
    "sentiment_heat_score",
    "sentiment_polarity",
    "spike_ratio",
    "spike",
    "composite_score",
    "tier",
    "primary_pair_address",
)

# Columns added after Step 1 — ALTER on existing DBs
_MIGRATE_TOKEN_INDICATORS = [
    # pandas-ta ADD-ON
    ("rsi_14", "REAL"),
    ("macd", "REAL"),
    ("macd_signal", "REAL"),
    ("macd_hist", "REAL"),
    ("boll_upper", "REAL"),
    ("boll_mid", "REAL"),
    ("boll_lower", "REAL"),
    ("jev_raw_score", "REAL"),
    ("jev_signal", "TEXT"),
    ("jev_confidence", "REAL"),
    ("jev_strength", "REAL"),
    # Step 3 sentiment
    ("sentiment_mentions_6h", "REAL"),
    ("sentiment_mentions_24h", "REAL"),
    ("sentiment_growth_1h", "REAL"),
    ("sentiment_growth_6h", "REAL"),
    ("sentiment_growth_24h", "REAL"),
    ("sentiment_heat_score", "REAL"),
    ("sentiment_polarity", "REAL"),
    ("spike_ratio", "REAL"),
    ("spike", "INTEGER"),
    # Technical freshness. Funding/sentiment/composite must not write this.
    ("tech_updated_at", "TEXT"),
]
_MIGRATE_INDICATOR_HISTORY = [
    ("rsi_14", "REAL"),
    ("macd", "REAL"),
    ("macd_signal", "REAL"),
    ("macd_hist", "REAL"),
    ("boll_upper", "REAL"),
    ("boll_mid", "REAL"),
    ("boll_lower", "REAL"),
    ("jev_strength", "REAL"),
    ("sentiment_mentions_1h", "REAL"),
    ("sentiment_heat_score", "REAL"),
    ("sentiment_polarity", "REAL"),
    ("spike_ratio", "REAL"),
    ("spike", "INTEGER"),
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None


def to_epoch(ts: str | None) -> float | None:
    dt = parse_iso(ts)
    return dt.timestamp() if dt else None


class IndicatorStore:
    """
    Own sqlite connection to the crawler DB.
    READ from existing tables / jev_signals / x_watch / alerts; WRITE only to
    token_indicators / indicator_history.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout = {INDICATOR_BUSY_TIMEOUT_MS}")
        self._ensure_schema()
        # (chain, token) -> (monotonic_ts, row dict) short TTL cache
        self._cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._cache_ttl_sec = 30.0

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "IndicatorStore":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def _table_columns(self, table: str) -> set[str]:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r["name"] for r in rows}

    def _ensure_schema(self) -> None:
        sql = _SQL_PATH.read_text(encoding="utf-8")
        self._conn.executescript(sql)
        existing = self._table_columns("token_indicators")
        for col, typ in _MIGRATE_TOKEN_INDICATORS:
            if col not in existing:
                self._conn.execute(
                    f"ALTER TABLE token_indicators ADD COLUMN {col} {typ}"
                )
                log.info("migrated token_indicators.+%s", col)
        hist_cols = self._table_columns("indicator_history")
        for col, typ in _MIGRATE_INDICATOR_HISTORY:
            if col not in hist_cols:
                self._conn.execute(
                    f"ALTER TABLE indicator_history ADD COLUMN {col} {typ}"
                )
                log.info("migrated indicator_history.+%s", col)
        self._conn.commit()
        log.debug("indicator schema ensured from %s", _SQL_PATH.name)

    # --- read helpers (existing tables) ---

    def list_tokens_with_history(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT t.chain_id, t.token_address, t.has_pairs, t.pair_count,
                   t.last_seen_at, t.last_enriched_at, t.updated_at,
                   COUNT(h.id) AS hist_count,
                   MAX(h.recorded_at) AS last_hist_at
            FROM tokens t
            INNER JOIN pair_history h
              ON h.chain_id = t.chain_id AND h.token_address = t.token_address
            GROUP BY t.chain_id, t.token_address
            HAVING hist_count > 0
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def list_tokens_with_jev_signals(self) -> list[dict[str, Any]]:
        """Tokens that have a jev_signals row (funding refresh candidates)."""
        try:
            rows = self._conn.execute(
                """
                SELECT chain_id, token_address, jev_score, observed_score,
                       coverage_pct, score_kind, stance, analyzed_at, fetched_at,
                       error
                FROM jev_signals
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]

    def get_jev_signal(
        self, chain_id: str, token_address: str
    ) -> dict[str, Any] | None:
        try:
            row = self._conn.execute(
                """
                SELECT * FROM jev_signals
                WHERE chain_id = ? AND token_address = ?
                """,
                (norm_chain(chain_id), norm_address(token_address)),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return dict(row) if row else None

    def list_tokens_with_x_watch(self) -> list[dict[str, Any]]:
        """Tokens enrolled in x_watch (sentiment refresh candidates)."""
        try:
            rows = self._conn.execute(
                """
                SELECT chain_id, token_address, symbol, query_key, mode,
                       since_id, mention_count_total, mention_count_prev,
                       mention_count_last, last_searched_at, created_at, updated_at
                FROM x_watch
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]

    def get_x_watch(
        self, chain_id: str, token_address: str
    ) -> dict[str, Any] | None:
        try:
            row = self._conn.execute(
                """
                SELECT * FROM x_watch
                WHERE chain_id = ? AND token_address = ?
                """,
                (norm_chain(chain_id), norm_address(token_address)),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return dict(row) if row else None

    def get_recent_social_flags(
        self,
        chain_id: str,
        token_address: str,
        *,
        within_sec: int = 6 * 3600,
    ) -> dict[str, Any]:
        """
        READ alerts for social_spike / sentiment_anomaly on this token.
        Returns {spike, anomaly, texts} — texts are alert messages (tweet bodies
        are not persisted by x_api).
        """
        import json
        from datetime import datetime, timezone, timedelta

        out: dict[str, Any] = {"spike": False, "anomaly": False, "texts": []}
        chain_id = norm_chain(chain_id)
        token_address = norm_address(token_address)
        since = (
            datetime.now(timezone.utc) - timedelta(seconds=max(60, int(within_sec)))
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            rows = self._conn.execute(
                """
                SELECT type, message, payload_json, created_at
                FROM alerts
                WHERE chain_id = ? AND token_address = ?
                  AND type IN ('social_spike', 'sentiment_anomaly')
                  AND created_at >= ?
                ORDER BY created_at DESC
                LIMIT 50
                """,
                (chain_id, token_address, since),
            ).fetchall()
        except sqlite3.OperationalError:
            return out
        texts: list[str] = []
        for r in rows:
            t = r["type"]
            if t == "social_spike":
                out["spike"] = True
            elif t == "sentiment_anomaly":
                out["anomaly"] = True
            if r["message"]:
                texts.append(str(r["message"]))
            raw = r["payload_json"]
            if raw:
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError, json.JSONDecodeError):
                    payload = None
                if isinstance(payload, dict):
                    for key in ("symbol", "text", "sample_text", "snippet"):
                        v = payload.get(key)
                        if v:
                            texts.append(str(v))
        out["texts"] = texts
        return out

    def get_indicator_history_near(
        self,
        chain_id: str,
        token_address: str,
        *,
        lookback_sec: int,
        tolerance_sec: int | None = None,
    ) -> dict[str, Any] | None:
        """
        Closest indicator_history row near (now - lookback_sec).
        Used for sentiment growth baselines. READ only.

        Same-timestamp ties prefer a row with rsi_14 set, then the newest id,
        so a funding tick that stored NULL technicals does not hide the
        technical tick from the same second.
        """
        from datetime import datetime, timezone, timedelta

        chain_id = norm_chain(chain_id)
        token_address = norm_address(token_address)
        lookback_sec = max(60, int(lookback_sec))
        if tolerance_sec is None:
            tolerance_sec = min(max(lookback_sec // 2, 300), 6 * 3600)
        target = datetime.now(timezone.utc) - timedelta(seconds=lookback_sec)
        target_iso = target.strftime("%Y-%m-%dT%H:%M:%SZ")
        # Prefer row at or before target, else nearest after within tolerance window
        lo = (target - timedelta(seconds=tolerance_sec)).strftime("%Y-%m-%dT%H:%M:%SZ")
        hi = (target + timedelta(seconds=tolerance_sec)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            row = self._conn.execute(
                """
                SELECT * FROM indicator_history
                WHERE chain_id = ? AND token_address = ?
                  AND recorded_at >= ? AND recorded_at <= ?
                ORDER BY
                    ABS(strftime('%s', recorded_at) - strftime('%s', ?)) ASC,
                    (rsi_14 IS NULL) ASC,
                    id DESC
                LIMIT 1
                """,
                (chain_id, token_address, lo, hi, target_iso),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return dict(row) if row else None

    def pick_primary_pair(
        self, chain_id: str, token_address: str
    ) -> str | None:
        """Latest snapshot liquidity, then snapshot count.

        Historical peak liquidity is not used: a drained pool can still hold
        the max liquidity_usd ever recorded.
        """
        chain_id = norm_chain(chain_id)
        token_address = norm_address(token_address)
        row = self._conn.execute(
            """
            WITH ranked AS (
                SELECT pair_address,
                       liquidity_usd,
                       recorded_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY pair_address
                           ORDER BY recorded_at DESC, id DESC
                       ) AS rn,
                       COUNT(*) OVER (PARTITION BY pair_address) AS n
                FROM pair_history
                WHERE chain_id = ? AND token_address = ?
            )
            SELECT pair_address
            FROM ranked
            WHERE rn = 1
            ORDER BY
                CASE WHEN liquidity_usd IS NULL THEN 1 ELSE 0 END,
                liquidity_usd DESC,
                n DESC,
                recorded_at DESC
            LIMIT 1
            """,
            (chain_id, token_address),
        ).fetchone()
        return row["pair_address"] if row else None

    def load_pair_history(
        self,
        chain_id: str,
        pair_address: str,
        *,
        since: str | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        chain_id = norm_chain(chain_id)
        pair_address = norm_address(pair_address)
        limit = max(1, min(int(limit), 10000))
        sql = """
            SELECT id, chain_id, pair_address, token_address, recorded_at,
                   price_usd, volume_h24, volume_h1, liquidity_usd, txns_h24, tier
            FROM pair_history
            WHERE chain_id = ? AND pair_address = ?
        """
        params: list[Any] = [chain_id, pair_address]
        if since:
            sql += " AND recorded_at >= ?"
            params.append(since)
        sql += " ORDER BY recorded_at ASC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def load_token_history(
        self,
        chain_id: str,
        token_address: str,
        *,
        since: str | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """All pair_history rows for a token (any pair), ascending time."""
        chain_id = norm_chain(chain_id)
        token_address = norm_address(token_address)
        limit = max(1, min(int(limit), 10000))
        sql = """
            SELECT id, chain_id, pair_address, token_address, recorded_at,
                   price_usd, volume_h24, volume_h1, liquidity_usd, txns_h24, tier
            FROM pair_history
            WHERE chain_id = ? AND token_address = ?
        """
        params: list[Any] = [chain_id, token_address]
        if since:
            sql += " AND recorded_at >= ?"
            params.append(since)
        sql += " ORDER BY recorded_at ASC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    # --- write helpers (indicator tables only) ---

    def upsert_token_indicators(
        self, row: dict[str, Any], *, touch_updated_at: bool = True
    ) -> None:
        """
        Partial upsert: only columns present as keys in `row` are written.
        Technical and funding can update independently without wiping each other.

        ``touch_updated_at=False`` leaves the shared updated_at clock alone.
        ``tech_updated_at`` is written only when the caller puts it in ``row``
        (technical passes that do not recompute RSI/MACD/BOLL omit it).
        """
        chain_id = norm_chain(row["chain_id"])
        token_address = norm_address(row["token_address"])
        updated_at = row.get("updated_at") or utc_now_iso()
        cols = [c for c in TOKEN_INDICATOR_COLS if c in row]
        if cols:
            placeholders = ", ".join("?" for _ in cols)
            insert_cols = ", ".join(cols)
            assignments = ", ".join(f"{c}=excluded.{c}" for c in cols)
            if touch_updated_at:
                assignments = f"{assignments}, updated_at=excluded.updated_at"
            values = [row.get(c) for c in cols]
            self._conn.execute(
                f"""
                INSERT INTO token_indicators (
                    chain_id, token_address, {insert_cols}, updated_at
                ) VALUES (?, ?, {placeholders}, ?)
                ON CONFLICT(chain_id, token_address) DO UPDATE SET
                    {assignments}
                """,
                (chain_id, token_address, *values, updated_at),
            )
        elif touch_updated_at:
            self._conn.execute(
                """
                INSERT INTO token_indicators (chain_id, token_address, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chain_id, token_address) DO UPDATE SET
                    updated_at=excluded.updated_at
                """,
                (chain_id, token_address, updated_at),
            )
        self._conn.commit()
        key = (chain_id, token_address)
        prev: dict[str, Any] = {}
        if key in self._cache:
            prev = dict(self._cache[key][1])
        else:
            existing = self.get_token_indicators(
                chain_id, token_address, use_cache=False
            )
            if existing:
                prev = existing
        merged = {
            **prev,
            **{c: row.get(c) for c in cols},
            "chain_id": chain_id,
            "token_address": token_address,
        }
        if touch_updated_at:
            merged["updated_at"] = updated_at
        elif prev.get("updated_at"):
            merged["updated_at"] = prev["updated_at"]
        self._cache[key] = (time.monotonic(), merged)

    def insert_indicator_history(self, row: dict[str, Any]) -> None:
        """Append one history row.

        Keys absent from ``row`` are filled from the live token_indicators row
        when that value is non-null. Funding/sentiment/composite omit RSI/MACD/BOLL;
        without this fill their rows stored NULL and same-second readers kept
        the incomplete tick. Explicit None is preserved (the caller computed it).
        """
        chain_id = norm_chain(row["chain_id"])
        token_address = norm_address(row["token_address"])
        recorded_at = row.get("recorded_at") or utc_now_iso()
        payload = dict(row)
        live = self.get_token_indicators(chain_id, token_address, use_cache=True) or {}
        for col in _HISTORY_FILL_IF_ABSENT:
            if col not in payload and live.get(col) is not None:
                payload[col] = live[col]
        self._conn.execute(
            """
            INSERT INTO indicator_history (
                chain_id, token_address, recorded_at,
                change_5m, change_15m, change_1h, change_4h, change_24h,
                volatility_24h, volume_strength,
                rsi_14, macd, macd_signal, macd_hist,
                boll_upper, boll_mid, boll_lower,
                jev_score, jev_strength,
                sentiment_score, sentiment_mentions_1h,
                sentiment_heat_score, sentiment_polarity,
                spike_ratio, spike,
                composite_score,
                tier, primary_pair_address
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chain_id,
                token_address,
                recorded_at,
                payload.get("change_5m"),
                payload.get("change_15m"),
                payload.get("change_1h"),
                payload.get("change_4h"),
                payload.get("change_24h"),
                payload.get("volatility_24h"),
                payload.get("volume_strength"),
                payload.get("rsi_14"),
                payload.get("macd"),
                payload.get("macd_signal"),
                payload.get("macd_hist"),
                payload.get("boll_upper"),
                payload.get("boll_mid"),
                payload.get("boll_lower"),
                payload.get("jev_score"),
                payload.get("jev_strength"),
                payload.get("sentiment_score"),
                payload.get("sentiment_mentions_1h"),
                payload.get("sentiment_heat_score"),
                payload.get("sentiment_polarity"),
                payload.get("spike_ratio"),
                payload.get("spike"),
                payload.get("composite_score"),
                payload.get("tier"),
                payload.get("primary_pair_address"),
            ),
        )
        self._conn.commit()

    def purge_old_indicator_history(self, retention_days: int) -> int:
        """Delete indicator_history rows older than ``retention_days``.

        Same cutoff shape as Store.purge_old_pair_history. ``retention_days``
        is clamped to at least 1 so a zero/negative setting cannot wipe the table.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(1, int(retention_days)))
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        cur = self._conn.execute(
            "DELETE FROM indicator_history WHERE recorded_at < ?",
            (cutoff,),
        )
        self._conn.commit()
        return int(cur.rowcount)

    def get_token_indicators(
        self, chain_id: str, token_address: str, *, use_cache: bool = True
    ) -> dict[str, Any] | None:
        chain_id = norm_chain(chain_id)
        token_address = norm_address(token_address)
        key = (chain_id, token_address)
        if use_cache and key in self._cache:
            ts, cached = self._cache[key]
            if time.monotonic() - ts <= self._cache_ttl_sec:
                return dict(cached)
        row = self._conn.execute(
            """
            SELECT * FROM token_indicators
            WHERE chain_id = ? AND token_address = ?
            """,
            (chain_id, token_address),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        self._cache[key] = (time.monotonic(), d)
        return d


    # Allowed ORDER BY columns for public top-N API (whitelist only)
    TOP_SORT_COLUMNS = ("change_5m", "jev_strength", "volume_strength")

    def list_top_token_indicators(
        self,
        *,
        chain: str | None = None,
        sort: str = "change_5m",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """
        Top-N rows from token_indicators.
        sort must be in TOP_SORT_COLUMNS; NULLs sorted last; DESC by default.
        """
        sort_col = (sort or "change_5m").strip()
        if sort_col not in self.TOP_SORT_COLUMNS:
            raise ValueError(
                f"invalid sort={sort_col!r}; allowed={list(self.TOP_SORT_COLUMNS)}"
            )
        limit = max(1, min(int(limit), 100))
        sql = f"""
            SELECT * FROM token_indicators
            WHERE 1=1
        """
        params: list[Any] = []
        if chain:
            sql += " AND chain_id = ?"
            params.append(norm_chain(chain))
        # NULLS LAST, then DESC (strongest / largest first)
        sql += f"""
            ORDER BY
                CASE WHEN {sort_col} IS NULL THEN 1 ELSE 0 END,
                {sort_col} DESC,
                updated_at DESC
            LIMIT ?
        """
        params.append(limit)
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def count_token_indicators(self) -> int:
        return int(
            self._conn.execute("SELECT COUNT(*) FROM token_indicators").fetchone()[0]
        )

    def get_indicator_updated_at(
        self, chain_id: str, token_address: str
    ) -> str | None:
        """Shared row clock. Technical due-checks must use get_tech_updated_at."""
        row = self._conn.execute(
            """
            SELECT updated_at FROM token_indicators
            WHERE chain_id = ? AND token_address = ?
            """,
            (norm_chain(chain_id), norm_address(token_address)),
        ).fetchone()
        return row["updated_at"] if row else None

    def map_tech_updated_at(self) -> dict[tuple[str, str], str | None]:
        """tech_updated_at for every indicator row.

        A missing key means no indicator row yet, which is the same as NULL:
        technical has not completed a fresh pass. Callers must not substitute
        updated_at.
        """
        rows = self._conn.execute(
            """
            SELECT chain_id, token_address, tech_updated_at
            FROM token_indicators
            """
        ).fetchall()
        return {
            (norm_chain(r["chain_id"]), norm_address(r["token_address"])): r[
                "tech_updated_at"
            ]
            for r in rows
        }

    def map_token_recency(self) -> dict[tuple[str, str], dict[str, Any]]:
        """has_pairs / last_seen_at for tiering, without scanning pair_history."""
        rows = self._conn.execute(
            """
            SELECT chain_id, token_address, has_pairs, last_seen_at, updated_at
            FROM tokens
            """
        ).fetchall()
        return {
            (norm_chain(r["chain_id"]), norm_address(r["token_address"])): {
                "has_pairs": r["has_pairs"],
                "last_seen_at": r["last_seen_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        }

    def get_tech_updated_at(
        self, chain_id: str, token_address: str
    ) -> str | None:
        """When RSI/MACD/BOLL (and the rest of the technical pass) were last recomputed.

        NULL means technical has not completed a fresh pass. Callers must not
        substitute updated_at — funding and sentiment bump that column every round.
        """
        row = self._conn.execute(
            """
            SELECT tech_updated_at FROM token_indicators
            WHERE chain_id = ? AND token_address = ?
            """,
            (norm_chain(chain_id), norm_address(token_address)),
        ).fetchone()
        if not row:
            return None
        return row["tech_updated_at"]
