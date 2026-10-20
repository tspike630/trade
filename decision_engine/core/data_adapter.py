"""Read-only SQLite adapter over token_indicators (and optional pair snapshots)."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from logging_setup import get_logger

from decision_engine import config as de_config

log = get_logger("decision_engine.data_adapter")

# Columns we care about for Step 1 strategies.
# token_indicators stores bands as boll_upper / boll_lower (not boll_up / boll_low).
# Missing columns are dropped by _available_cols.
_INDICATOR_COLS = (
    "chain_id",
    "token_address",
    "change_5m",
    "change_15m",
    "change_1h",
    "change_4h",
    "change_24h",
    "volatility_24h",
    "volume_strength",
    "jev_raw_score",
    "jev_score",
    "jev_signal",
    "jev_confidence",
    "jev_strength",
    "jev_updated_at",
    "jev_age_min",
    "tier",
    "primary_pair_address",
    "updated_at",
    "rsi_14",
    "macd",
    "macd_signal",
    "boll_mid",
    "boll_upper",
    "boll_lower",
)

_BOLL_COLS = ("boll_upper", "boll_mid", "boll_lower")


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def liquidity_usd_from_raw(raw: Any) -> float | None:
    """Read liquidity.usd from a pairs.raw_json blob.

    Matches get_pair_snapshot: only a JSON object with a dict ``liquidity``
    contributes. A missing pair, bad JSON, or a non-dict liquidity is None
    so the live entry floor can fail closed.
    """
    parsed: Any
    if isinstance(raw, dict):
        parsed = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    else:
        return None
    if not isinstance(parsed, dict):
        return None
    liquidity = parsed.get("liquidity")
    if not isinstance(liquidity, dict):
        return None
    return _safe_float(liquidity.get("usd"))


class IndicatorDataAdapter:
    """
    Open DB read-only (URI mode=ro). Never writes indicator / crawl tables.
    decision_log writes go through SignalBus with a separate RW connection.
    """

    def __init__(self, db_path: str | Path, *, use_ro_uri: bool = True):
        self.db_path = Path(db_path).resolve()
        self._use_ro_uri = use_ro_uri
        self._conn: sqlite3.Connection | None = None
        self._logged_boll = False
        self._open()

    def _open(self) -> None:
        if not self.db_path.is_file():
            log.warning("db missing path=%s (queries will return empty)", self.db_path)
            # Still open; SQLite creates empty file for non-URI — avoid that with URI
            # Use ro URI which fails if file missing; fall back to empty in-memory
            try:
                uri = f"file:{self.db_path}?mode=ro"
                self._conn = sqlite3.connect(uri, uri=True, timeout=10.0)
            except sqlite3.OperationalError:
                self._conn = sqlite3.connect(":memory:")
                log.warning("opened in-memory stub; no indicator rows available")
                self._log_boll_columns()
                return
        else:
            try:
                if self._use_ro_uri:
                    uri = f"file:{self.db_path}?mode=ro"
                    self._conn = sqlite3.connect(uri, uri=True, timeout=10.0)
                else:
                    self._conn = sqlite3.connect(str(self.db_path), timeout=10.0)
            except sqlite3.OperationalError as exc:
                log.warning("ro URI failed (%s); falling back to normal SELECT-only conn", exc)
                self._conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        assert self._conn is not None
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        log.info("data_adapter open ro db=%s", self.db_path)
        self._log_boll_columns()

    def _log_boll_columns(self) -> None:
        """Log which Bollinger columns this DB actually has (once per adapter)."""
        if self._logged_boll:
            return
        self._logged_boll = True
        present = self._table_columns("token_indicators")
        if not present:
            log.info("token_indicators boll columns selected=(none) table_missing=1")
            return
        chosen = [c for c in _BOLL_COLS if c in present]
        missing = [c for c in _BOLL_COLS if c not in present]
        log.info(
            "token_indicators boll columns selected=%s",
            ",".join(chosen) if chosen else "(none)",
        )
        if missing:
            log.warning(
                "token_indicators missing boll columns (PRAGMA filtered): %s",
                ",".join(missing),
            )

    @property
    def conn(self) -> sqlite3.Connection:
        assert self._conn is not None
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "IndicatorDataAdapter":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _table_exists(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (name,),
        ).fetchone()
        return row is not None

    def _table_columns(self, table: str) -> set[str]:
        if not self._table_exists(table):
            return set()
        try:
            return {
                str(r["name"])
                for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
        except sqlite3.OperationalError:
            return set()

    def _available_cols(self, table: str) -> tuple[str, ...]:
        """PRAGMA filter: only columns that actually exist on the table."""
        cols = self._table_columns(table)
        if not cols:
            return ()
        return tuple(c for c in _INDICATOR_COLS if c in cols)

    def count_indicators(self) -> int:
        if not self._table_exists("token_indicators"):
            return 0
        row = self.conn.execute("SELECT COUNT(*) AS n FROM token_indicators").fetchone()
        return int(row["n"]) if row else 0

    def list_hot_tokens(self, limit: int = 50) -> list[dict[str, Any]]:
        """
        Prefer tier='hot' ordered by jev_strength DESC NULLS LAST.

        tier alone is not trusted: a token that left the hot window can stay
        tier='hot' after funding/sentiment refresh the shared updated_at.
        When tokens.last_seen_at is joinable, require it inside the hot window.
        When tech_updated_at exists, also require that technical pass to be fresh.
        Missing columns degrade to the filters that are available.
        If every hot row is stale, return empty — do not fall back onto them.
        If there is no hot tier at all, fall back to top jev_strength globally.
        """
        if not self._table_exists("token_indicators"):
            log.warning("token_indicators table missing")
            return []
        col_names = self._available_cols("token_indicators")
        if not col_names:
            log.warning("token_indicators has no usable columns")
            return []
        limit = max(1, int(limit))
        ind_present = self._table_columns("token_indicators")
        has_tech = "tech_updated_at" in ind_present
        join_seen = "last_seen_at" in self._table_columns("tokens")

        now = datetime.now(timezone.utc)
        seen_cutoff = _iso_z(now - timedelta(seconds=de_config.HOT_SEEN_WINDOW_SEC))
        tech_cutoff = _iso_z(now - timedelta(seconds=de_config.TECH_FRESH_SEC))

        select_list = ", ".join(f"ti.{c}" for c in col_names)
        join_sql = ""
        where = ["ti.tier = 'hot'"]
        params: list[Any] = []
        if join_seen:
            join_sql = (
                " LEFT JOIN tokens t"
                " ON t.chain_id = ti.chain_id AND t.token_address = ti.token_address"
            )
            where.append("t.last_seen_at IS NOT NULL AND t.last_seen_at >= ?")
            params.append(seen_cutoff)
        if has_tech:
            where.append("ti.tech_updated_at IS NOT NULL AND ti.tech_updated_at >= ?")
            params.append(tech_cutoff)
        freshness_on = join_seen or has_tech

        n_hot_row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM token_indicators WHERE tier = 'hot'"
        ).fetchone()
        n_hot = int(n_hot_row["n"]) if n_hot_row else 0
        if freshness_on:
            n_fresh_row = self.conn.execute(
                f"SELECT COUNT(*) AS n FROM token_indicators ti{join_sql} "
                f"WHERE {' AND '.join(where)}",
                params,
            ).fetchone()
            n_fresh = int(n_fresh_row["n"]) if n_fresh_row else 0
            skipped = max(0, n_hot - n_fresh)
        else:
            n_fresh = n_hot
            skipped = 0
            log.info(
                "list_hot_tokens: freshness columns missing "
                "(tokens.last_seen_at=%s tech_updated_at=%s); tier-only",
                "yes" if join_seen else "no",
                "yes" if has_tech else "no",
            )

        order_updated = "ti.updated_at" if "updated_at" in ind_present else "ti.rowid"
        sql_hot = f"""
            SELECT {select_list}
            FROM token_indicators ti{join_sql}
            WHERE {' AND '.join(where)}
            ORDER BY COALESCE(ti.jev_strength, -1) DESC, {order_updated} DESC
            LIMIT ?
        """
        rows = self.conn.execute(sql_hot, (*params, limit)).fetchall()
        if rows:
            log.info(
                "list_hot_tokens: hot=%s fresh=%s skipped_stale=%s limit=%s got=%s "
                "last_seen=%s tech_updated_at=%s",
                n_hot,
                n_fresh,
                skipped,
                limit,
                len(rows),
                "on" if join_seen else "missing",
                "on" if has_tech else "missing",
            )
            return self._attach_jev_stance(self._attach_liquidity([dict(r) for r in rows]))

        if n_hot > 0:
            log.info(
                "list_hot_tokens: hot=%s fresh=%s skipped_stale=%s; "
                "not falling back onto stale tier='hot' rows",
                n_hot,
                n_fresh,
                skipped,
            )
            return []

        sql_any = f"""
            SELECT {", ".join(col_names)}
            FROM token_indicators
            ORDER BY COALESCE(jev_strength, -1) DESC, updated_at DESC
            LIMIT ?
        """
        rows = self.conn.execute(sql_any, (limit,)).fetchall()
        log.info(
            "list_hot_tokens: no hot tier; fallback top-%s by jev_strength (%s rows) "
            "skipped_stale=0",
            limit,
            len(rows),
        )
        return self._attach_jev_stance(self._attach_liquidity([dict(r) for r in rows]))

    def get_indicators(self, chain_id: str, token_address: str) -> dict[str, Any] | None:
        if not self._table_exists("token_indicators"):
            return None
        cols = ", ".join(self._available_cols("token_indicators"))
        if not cols:
            return None
        row = self.conn.execute(
            f"SELECT {cols} FROM token_indicators WHERE chain_id=? AND token_address=? LIMIT 1",
            (chain_id, token_address),
        ).fetchone()
        return _row_to_dict(row)

    def get_pair_snapshot(
        self, chain_id: str, pair_address: str
    ) -> dict[str, Any] | None:
        """Optional: latest pair row + light price/liq from raw_json if present."""
        if not self._table_exists("pairs"):
            return None
        row = self.conn.execute(
            """
            SELECT chain_id, pair_address, token_address, raw_json, updated_at
            FROM pairs
            WHERE chain_id=? AND pair_address=?
            LIMIT 1
            """,
            (chain_id, pair_address),
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        raw = out.get("raw_json")
        out["liquidity_usd"] = liquidity_usd_from_raw(raw)
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                out["price_usd"] = parsed.get("priceUsd")
            else:
                out["price_usd"] = None
        return out

    def _pair_meta_by_pair(
        self, keys: list[tuple[str, str]]
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Batch pairs → liquidity_usd + pair_created_at (ms epoch when present).

        ``pair_created_at`` is the pairs column (DexScreener pairCreatedAt, ms).
        Omitted from the meta dict when the column is missing or the value is NULL.
        """
        found: dict[tuple[str, str], dict[str, Any]] = {}
        if not keys:
            return found
        columns = self._table_columns("pairs")
        if not {"chain_id", "pair_address", "raw_json"} <= columns:
            return found
        want_created = "pair_created_at" in columns
        select_cols = "chain_id, pair_address, raw_json"
        if want_created:
            select_cols += ", pair_created_at"
        chunk_size = 80
        for start in range(0, len(keys), chunk_size):
            chunk = keys[start : start + chunk_size]
            clause = " OR ".join("(chain_id = ? AND pair_address = ?)" for _ in chunk)
            params: list[Any] = []
            for chain_id, pair_address in chunk:
                params.extend((chain_id, pair_address))
            try:
                fetched = self.conn.execute(
                    f"SELECT {select_cols} FROM pairs WHERE {clause}",
                    params,
                ).fetchall()
            except sqlite3.OperationalError as exc:
                log.warning("pair meta lookup failed: %s", exc)
                return found
            for row in fetched:
                meta: dict[str, Any] = {
                    "liquidity_usd": liquidity_usd_from_raw(row["raw_json"]),
                }
                if want_created:
                    raw_created = row["pair_created_at"]
                    if raw_created is not None:
                        try:
                            meta["pair_created_at"] = int(raw_created)
                        except (TypeError, ValueError):
                            pass
                found[(str(row["chain_id"]), str(row["pair_address"]))] = meta
        return found

    def _liquidity_by_pair(
        self, keys: list[tuple[str, str]]
    ) -> dict[tuple[str, str], float | None]:
        """Batch pairs.raw_json → liquidity_usd. Thin wrapper over ``_pair_meta_by_pair``."""
        return {
            key: meta.get("liquidity_usd")
            for key, meta in self._pair_meta_by_pair(keys).items()
        }

    def _attach_jev_stance(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Copy jev_signals.stance/fetched_at onto rows as ``jev_stance``/``jev_fetched_at`` (read-only)."""
        if not rows or not self._table_exists("jev_signals"):
            return rows
        for r in rows:
            try:
                got = self.conn.execute(
                    "SELECT stance, fetched_at FROM jev_signals WHERE chain_id=? AND token_address=? "
                    "ORDER BY fetched_at DESC LIMIT 1",
                    (r.get("chain_id"), r.get("token_address")),
                ).fetchone()
            except sqlite3.Error:
                got = None
            r["jev_stance"] = got[0] if got else None
            r["jev_fetched_at"] = got[1] if got else None
        return rows

    def _attach_liquidity(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Set liquidity_usd (+ pair_created_at when known) on scanned indicator rows.

        Missing pair/usd → liquidity_usd None. ``pair_created_at`` is only set when
        the pairs column has a value (ms epoch); otherwise the key is left unset.
        """
        if not rows:
            return rows
        keys: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            chain_id = str(row.get("chain_id") or "")
            pair = row.get("primary_pair_address")
            if not chain_id or pair is None or str(pair).strip() == "":
                continue
            key = (chain_id, str(pair))
            if key not in seen:
                seen.add(key)
                keys.append(key)
        found = self._pair_meta_by_pair(keys)
        with_liq = 0
        with_created = 0
        for row in rows:
            chain_id = str(row.get("chain_id") or "")
            pair = row.get("primary_pair_address")
            if not chain_id or pair is None or str(pair).strip() == "":
                row["liquidity_usd"] = None
                continue
            meta = found.get((chain_id, str(pair))) or {}
            liquidity = meta.get("liquidity_usd")
            row["liquidity_usd"] = liquidity
            if liquidity is not None:
                with_liq += 1
            created = meta.get("pair_created_at")
            if created is not None:
                row["pair_created_at"] = created
                with_created += 1
        log.info(
            "liquidity_usd attached rows=%s with_liq=%s missing=%s with_pair_created_at=%s",
            len(rows),
            with_liq,
            len(rows) - with_liq,
            with_created,
        )
        return rows
