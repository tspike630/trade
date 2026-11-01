"""SQLite upsert storage for tokens, pairs, pending enrich, and run checkpoints."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from history import TXN_FLOW_COLUMNS, extract_metrics, pair_history_column_sql


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _ro_uri(path: Path) -> str:
    return "file:" + quote(path.resolve().as_posix(), safe="/") + "?mode=ro"


_CLUE_DESC_PREFIXES = ("helius logsSubscribe", "helius ", "x mention")


def has_dex_profile(raw: dict[str, Any] | None) -> bool:
    """True when raw_json already carries a non-empty DexScreener-style profile."""
    if not isinstance(raw, dict):
        return False

    def _obj(key: str) -> dict[str, Any]:
        value = raw.get(key)
        return value if isinstance(value, dict) else {}

    def _text(value: object) -> bool:
        return isinstance(value, str) and bool(value.strip())

    info = _obj("info")
    base = _obj("baseToken")
    for obj in (raw, info, base):
        if _text(obj.get("name")) or _text(obj.get("symbol")):
            return True
    for obj in (raw, info):
        desc = obj.get("description")
        if _text(desc) and not str(desc).startswith(_CLUE_DESC_PREFIXES):
            return True
        for key in ("links", "websites", "socials"):
            if obj.get(key):
                return True
        if _text(obj.get("icon")) or _text(obj.get("header")) or _text(obj.get("imageUrl")):
            return True
    return False


def _append_clue_note(note: str | None, source: str) -> str:
    parts = [part for part in (note or "").split(",") if part]
    if source not in parts:
        parts.append(source)
    return ",".join(parts)


def _x_watch_ttl_sec(mode: str) -> int:
    env_name = "X_WATCH_HOT_TTL_SEC" if mode == "hot" else "X_EARLY_WATCH_SEC"
    default = "21600" if mode == "hot" else "3600"
    try:
        return max(60, int(os.environ.get(env_name, default)))
    except ValueError:
        return int(default)


def _x_watch_max_rows() -> int:
    try:
        return max(1, int(os.environ.get("X_WATCH_MAX_ROWS", "200")))
    except ValueError:
        return 200


def _as_text(value: str | bytes | bytearray | memoryview | None) -> str | None:
    """Coerce sqlite TEXT-or-BLOB payloads to str (post-rebuild BLOB affinity)."""
    if value is None:
        return None
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return str(value)


def norm_chain(chain_id: str | bytes | bytearray | memoryview) -> str:
    text = _as_text(chain_id)
    if text is None:
        raise TypeError("chain_id is required")
    return text.strip().lower()


def norm_address(address: str | bytes | bytearray | memoryview) -> str:
    """Force lowercase for stable PKs / batch keys (API accepts lowercased addrs)."""
    text = _as_text(address)
    if text is None:
        raise TypeError("address is required")
    return text.strip().lower()


def fetch_latest_pair_price(
    conn: sqlite3.Connection, chain_id: str, pair_address: str
) -> dict[str, Any] | None:
    """Newest non-null pair_history price. Uses idx_pair_history_lookup, one row.

    Does not apply a staleness window; callers decide max age.
    """
    row = conn.execute(
        """
        SELECT price_usd, recorded_at
        FROM pair_history
        WHERE chain_id = ? AND pair_address = ? AND price_usd IS NOT NULL
        ORDER BY recorded_at DESC
        LIMIT 1
        """,
        (norm_chain(chain_id), norm_address(pair_address)),
    ).fetchone()
    if row is None or row["price_usd"] is None:
        return None
    try:
        price = float(row["price_usd"])
    except (TypeError, ValueError):
        return None
    return {
        "price_usd": price,
        "recorded_at": row["recorded_at"],
        "source": "pair_history",
    }


def fetch_pair_spot(
    conn: sqlite3.Connection, chain_id: str, pair_address: str
) -> dict[str, Any] | None:
    """pairs.raw_json priceUsd stamped with pairs.updated_at."""
    row = conn.execute(
        """
        SELECT raw_json, updated_at
        FROM pairs
        WHERE chain_id = ? AND pair_address = ?
        """,
        (norm_chain(chain_id), norm_address(pair_address)),
    ).fetchone()
    if row is None:
        return None
    try:
        raw = json.loads(row["raw_json"])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict) or raw.get("priceUsd") is None:
        return None
    try:
        price = float(raw["priceUsd"])
    except (TypeError, ValueError):
        return None
    return {
        "price_usd": price,
        "recorded_at": row["updated_at"],
        "source": "pairs.priceUsd",
    }


def _parse_pair_created_at(raw: dict[str, Any]) -> int | None:
    """DexScreener pairCreatedAt as ms epoch int; missing/invalid -> None (never invent)."""
    v = raw.get("pairCreatedAt")
    if v is None or v == "":
        return None
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return n

class Store:
    def __init__(self, db_path: str | Path, *, readonly: bool = False):
        self.db_path = Path(db_path)
        self.readonly = readonly
        if readonly:
            if not self.db_path.is_file():
                raise FileNotFoundError(f"database not found: {self.db_path}")
            self._conn = sqlite3.connect(_ro_uri(self.db_path), uri=True, timeout=30.0)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA query_only=ON")
            self._conn.execute("PRAGMA busy_timeout=30000")
            return
        self._conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    @classmethod
    def open_ro(cls, db_path: str | Path) -> "Store":
        """Read-only connection. Skips schema migration so it does not take a write lock."""
        return cls(db_path, readonly=True)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _table_columns(self, table: str) -> set[str]:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r["name"] for r in rows}

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS tokens (
                chain_id TEXT NOT NULL,
                token_address TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                source TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                pair_count INTEGER,
                has_pairs INTEGER,
                PRIMARY KEY (chain_id, token_address)
            );

            CREATE TABLE IF NOT EXISTS pairs (
                chain_id TEXT NOT NULL,
                pair_address TEXT NOT NULL,
                token_address TEXT,
                raw_json TEXT NOT NULL,
                source TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                market_cap REAL,
                fdv REAL,
                PRIMARY KEY (chain_id, pair_address)
            );

            CREATE TABLE IF NOT EXISTS pending_enrich (
                chain_id TEXT NOT NULL,
                token_address TEXT NOT NULL,
                added_at TEXT NOT NULL,
                PRIMARY KEY (chain_id, token_address)
            );

            CREATE INDEX IF NOT EXISTS idx_pairs_token
                ON pairs (chain_id, token_address);

            CREATE INDEX IF NOT EXISTS idx_tokens_has_pairs
                ON tokens (has_pairs);

            CREATE TABLE IF NOT EXISTS run_checkpoint (
                cycle_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                step TEXT NOT NULL,
                last_batch_meta TEXT,
                stats_json TEXT,
                flush_pending INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_run_checkpoint_status
                ON run_checkpoint (status);

            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                type TEXT NOT NULL,
                chain_id TEXT,
                token_address TEXT,
                pair_address TEXT,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                payload_json TEXT,
                delivered INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_alerts_dedupe
                ON alerts (type, chain_id, token_address, pair_address, created_at);
            """
        )
        self._conn.commit()
        self._migrate_add_pair_flags()
        self._migrate_tier_timestamps()
        self._migrate_clue_note()
        self._migrate_lowercase_addresses()
        self._migrate_x_social()
        self._migrate_pair_history()
        self._migrate_txn_flow_columns()
        self._migrate_quality_score()
        self._migrate_pair_created_at()
        self._migrate_market_cap_columns()
        self._migrate_intel_loop_i1()
        self._migrate_intel_loop_i3()

    def _migrate_add_pair_flags(self) -> None:
        cols = self._table_columns("tokens")
        cur = self._conn.cursor()
        if "pair_count" not in cols:
            cur.execute("ALTER TABLE tokens ADD COLUMN pair_count INTEGER")
        if "has_pairs" not in cols:
            cur.execute("ALTER TABLE tokens ADD COLUMN has_pairs INTEGER")
        self._conn.commit()


    def _migrate_tier_timestamps(self) -> None:
        cols = self._table_columns("tokens")
        cur = self._conn.cursor()
        changed = False
        if "last_seen_at" not in cols:
            cur.execute("ALTER TABLE tokens ADD COLUMN last_seen_at TEXT")
            changed = True
        if "last_enriched_at" not in cols:
            cur.execute("ALTER TABLE tokens ADD COLUMN last_enriched_at TEXT")
            changed = True
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        # Backfill once. An unconditional UPDATE takes a RESERVED lock even
        # when it matches zero rows, so later Store() opens must skip it.
        row = cur.execute(
            "SELECT value FROM schema_meta WHERE key = 'tier_ts_backfill_v1'"
        ).fetchone()
        if row is not None:
            if changed:
                self._conn.commit()
            return
        cur.execute(
            """
            UPDATE tokens
            SET last_seen_at = COALESCE(last_seen_at, updated_at)
            WHERE last_seen_at IS NULL
            """
        )
        cur.execute(
            """
            UPDATE tokens
            SET last_enriched_at = COALESCE(last_enriched_at, updated_at)
            WHERE has_pairs IS NOT NULL AND last_enriched_at IS NULL
            """
        )
        cur.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('tier_ts_backfill_v1', ?)",
            (_utc_now_iso(),),
        )
        self._conn.commit()

    def _migrate_clue_note(self) -> None:
        cols = self._table_columns("tokens")
        if "clue_note" not in cols:
            self._conn.execute("ALTER TABLE tokens ADD COLUMN clue_note TEXT")
            self._conn.commit()

    def _migrate_lowercase_addresses(self) -> None:
        """Normalize chain_id / addresses to lowercase; merge PK collisions."""
        cur = self._conn.cursor()
        # Use a flag table so we only rewrite once after opt rollout
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        row = cur.execute(
            "SELECT value FROM schema_meta WHERE key = 'addr_lower_v1'"
        ).fetchone()
        if row is not None:
            return

        # --- tokens ---
        tokens = cur.execute(
            "SELECT chain_id, token_address, raw_json, source, updated_at, pair_count, has_pairs FROM tokens"
        ).fetchall()
        merged_tokens: dict[tuple[str, str], sqlite3.Row] = {}
        for t in tokens:
            key = (norm_chain(t["chain_id"]), norm_address(t["token_address"]))
            prev = merged_tokens.get(key)
            if prev is None or (t["updated_at"] or "") >= (prev["updated_at"] or ""):
                merged_tokens[key] = t

        cur.execute("DELETE FROM tokens")
        for (cid, addr), t in merged_tokens.items():
            cur.execute(
                """
                INSERT INTO tokens (chain_id, token_address, raw_json, source, updated_at, pair_count, has_pairs)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cid,
                    addr,
                    t["raw_json"],
                    t["source"],
                    t["updated_at"],
                    t["pair_count"],
                    t["has_pairs"],
                ),
            )

        # --- pairs ---
        pairs = cur.execute(
            "SELECT chain_id, pair_address, token_address, raw_json, source, updated_at FROM pairs"
        ).fetchall()
        merged_pairs: dict[tuple[str, str], sqlite3.Row] = {}
        for p in pairs:
            key = (norm_chain(p["chain_id"]), norm_address(p["pair_address"]))
            prev = merged_pairs.get(key)
            if prev is None or (p["updated_at"] or "") >= (prev["updated_at"] or ""):
                merged_pairs[key] = p

        cur.execute("DELETE FROM pairs")
        for (cid, paddr), p in merged_pairs.items():
            taddr = p["token_address"]
            if taddr:
                taddr = norm_address(taddr)
            cur.execute(
                """
                INSERT INTO pairs (chain_id, pair_address, token_address, raw_json, source, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (cid, paddr, taddr, p["raw_json"], p["source"], p["updated_at"]),
            )

        # --- pending_enrich (if any) ---
        try:
            pending = cur.execute(
                "SELECT chain_id, token_address, added_at FROM pending_enrich"
            ).fetchall()
        except sqlite3.OperationalError:
            pending = []
        if pending:
            cur.execute("DELETE FROM pending_enrich")
            seen: set[tuple[str, str]] = set()
            for p in pending:
                key = (norm_chain(p["chain_id"]), norm_address(p["token_address"]))
                if key in seen:
                    continue
                seen.add(key)
                cur.execute(
                    "INSERT INTO pending_enrich (chain_id, token_address, added_at) VALUES (?, ?, ?)",
                    (key[0], key[1], p["added_at"]),
                )

        cur.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('addr_lower_v1', ?)",
            (_utc_now_iso(),),
        )
        self._conn.commit()

    def get_token_flags(self, chain_id: str, token_address: str) -> tuple[int | None, int | None]:
        """Return (pair_count, has_pairs); either may be None if never enriched."""
        row = self._conn.execute(
            "SELECT pair_count, has_pairs FROM tokens WHERE chain_id = ? AND token_address = ?",
            (norm_chain(chain_id), norm_address(token_address)),
        ).fetchone()
        if row is None:
            return None, None
        return row["pair_count"], row["has_pairs"]

    def upsert_token(
        self,
        chain_id: str,
        token_address: str,
        raw: dict[str, Any],
        source: str,
        *,
        clue: bool = False,
    ) -> str:
        """Upsert a token profile. Preserves pair_count/has_pairs on update.

        Returns inserted|updated|preserved. Clue sources (Helius/X) that hit an
        existing non-empty Dex profile only annotate clue_note; they do not
        replace raw_json, source, or last_seen_at.
        """
        chain_id = norm_chain(chain_id)
        token_address = norm_address(token_address)
        now = _utc_now_iso()
        cur = self._conn.cursor()
        existing = cur.execute(
            "SELECT raw_json, source, clue_note FROM tokens WHERE chain_id = ? AND token_address = ?",
            (chain_id, token_address),
        ).fetchone()
        if clue and existing is not None and not has_dex_profile(raw):
            try:
                existing_raw = json.loads(existing["raw_json"])
            except (json.JSONDecodeError, TypeError):
                existing_raw = None
            if has_dex_profile(existing_raw):
                note = _append_clue_note(existing["clue_note"], source)
                cur.execute(
                    """
                    UPDATE tokens SET clue_note = ?
                    WHERE chain_id = ? AND token_address = ?
                    """,
                    (note, chain_id, token_address),
                )
                self._conn.commit()
                return "preserved"
        raw_json = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
        exists = existing is not None
        cur.execute(
            """
            INSERT INTO tokens (
                chain_id, token_address, raw_json, source, updated_at,
                pair_count, has_pairs, last_seen_at, last_enriched_at
            )
            VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, NULL)
            ON CONFLICT(chain_id, token_address) DO UPDATE SET
                raw_json = excluded.raw_json,
                source = excluded.source,
                updated_at = excluded.updated_at,
                last_seen_at = excluded.last_seen_at
            """,
            (chain_id, token_address, raw_json, source, now, now),
        )
        self._conn.commit()
        return "updated" if exists else "inserted"

    def set_token_pair_stats(
        self,
        chain_id: str,
        token_address: str,
        pair_count: int,
    ) -> None:
        chain_id = norm_chain(chain_id)
        token_address = norm_address(token_address)
        has_pairs = 1 if pair_count > 0 else 0
        now = _utc_now_iso()
        self._conn.execute(
            """
            UPDATE tokens
            SET pair_count = ?, has_pairs = ?, updated_at = ?, last_enriched_at = ?
            WHERE chain_id = ? AND token_address = ?
            """,
            (pair_count, has_pairs, now, now, chain_id, token_address),
        )
        self._conn.commit()

    def upsert_pair(
        self,
        chain_id: str,
        pair_address: str,
        raw: dict[str, Any],
        source: str,
        token_address: str | None = None,
    ) -> str:
        chain_id = norm_chain(chain_id)
        pair_address = norm_address(pair_address)
        now = _utc_now_iso()
        raw_json = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
        pair_created_at = _parse_pair_created_at(raw)
        metrics = extract_metrics(raw)
        market_cap = metrics["market_cap"]
        fdv = metrics["fdv"]
        if token_address is None:
            base = raw.get("baseToken") or {}
            token_address = base.get("address")
        if token_address:
            token_address = norm_address(str(token_address))
        cur = self._conn.cursor()
        cur.execute(
            "SELECT 1 FROM pairs WHERE chain_id = ? AND pair_address = ?",
            (chain_id, pair_address),
        )
        exists = cur.fetchone() is not None
        cur.execute(
            """
            INSERT INTO pairs (
                chain_id, pair_address, token_address, raw_json, source, updated_at,
                pair_created_at, market_cap, fdv
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chain_id, pair_address) DO UPDATE SET
                token_address = excluded.token_address,
                raw_json = excluded.raw_json,
                source = excluded.source,
                updated_at = excluded.updated_at,
                pair_created_at = excluded.pair_created_at,
                market_cap = excluded.market_cap,
                fdv = excluded.fdv
            """,
            (
                chain_id,
                pair_address,
                token_address,
                raw_json,
                source,
                now,
                pair_created_at,
                market_cap,
                fdv,
            ),
        )
        self._conn.commit()
        return "updated" if exists else "inserted"


    def get_pair_raw(self, chain_id: str, pair_address: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT raw_json FROM pairs WHERE chain_id = ? AND pair_address = ?",
            (norm_chain(chain_id), norm_address(pair_address)),
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["raw_json"])
        except json.JSONDecodeError:
            return None

    def token_exists(self, chain_id: str, token_address: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM tokens WHERE chain_id = ? AND token_address = ?",
            (norm_chain(chain_id), norm_address(token_address)),
        ).fetchone()
        return row is not None

    # --- alerts ---

    def insert_alert(
        self,
        *,
        alert_type: str,
        severity: str,
        message: str,
        chain_id: str | None = None,
        token_address: str | None = None,
        pair_address: str | None = None,
        payload: dict[str, Any] | None = None,
        delivered: bool = False,
    ) -> int:
        now = _utc_now_iso()
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO alerts (
                created_at, type, chain_id, token_address, pair_address,
                severity, message, payload_json, delivered
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                alert_type,
                norm_chain(chain_id) if chain_id else None,
                norm_address(token_address) if token_address else None,
                norm_address(pair_address) if pair_address else None,
                severity,
                message,
                json.dumps(payload or {}, ensure_ascii=False),
                1 if delivered else 0,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def mark_alert_delivered(self, alert_id: int) -> None:
        self._conn.execute(
            "UPDATE alerts SET delivered = 1 WHERE id = ?",
            (alert_id,),
        )
        self._conn.commit()

    def has_recent_alert(
        self,
        alert_type: str,
        *,
        chain_id: str = "",
        token_address: str = "",
        pair_address: str = "",
        within_sec: int = 300,
    ) -> bool:
        from datetime import datetime, timezone, timedelta

        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=within_sec)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        cid = norm_chain(chain_id) if chain_id else ""
        taddr = norm_address(token_address) if token_address else ""
        paddr = norm_address(pair_address) if pair_address else ""
        row = self._conn.execute(
            """
            SELECT 1 FROM alerts
            WHERE type = ?
              AND COALESCE(chain_id, '') = ?
              AND COALESCE(token_address, '') = ?
              AND COALESCE(pair_address, '') = ?
              AND created_at >= ?
            LIMIT 1
            """,
            (alert_type, cid, taddr, paddr, cutoff),
        ).fetchone()
        return row is not None

    def count_alerts(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0])

    def _bump_meta(self, key: str, delta: int = 1) -> int:
        """Atomic increment. Avoids read-modify-write lost updates across processes."""
        delta = int(delta)
        self._conn.execute(
            """
            INSERT INTO schema_meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = CAST(
                    CAST(schema_meta.value AS INTEGER) + CAST(excluded.value AS INTEGER)
                    AS TEXT
                )
            """,
            (key, str(delta)),
        )
        self._conn.commit()
        raw = self.get_meta(key, "0") or "0"
        try:
            return int(raw)
        except ValueError:
            return delta

    def bump_counter(self, key: str, delta: int = 1) -> int:
        return self._bump_meta(f"counter_{key}", delta)

    def get_counters(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for key in ("cycles_ok", "cycles_fail", "alerts_emitted", "validation_errors"):
            raw = self.get_meta(f"counter_{key}", "0") or "0"
            try:
                out[key] = int(raw)
            except ValueError:
                out[key] = 0
        return out

    def set_last_success_at(self, iso_ts: str) -> None:
        self.set_meta("last_success_at", iso_ts)

    def get_last_success_at(self) -> str | None:
        return self.get_meta("last_success_at")

    # --- pending enrich queue ---

    def add_pending_enrich(self, chain_id: str, token_address: str) -> None:
        chain_id = norm_chain(chain_id)
        token_address = norm_address(token_address)
        self._conn.execute(
            """
            INSERT OR IGNORE INTO pending_enrich (chain_id, token_address, added_at)
            VALUES (?, ?, ?)
            """,
            (chain_id, token_address, _utc_now_iso()),
        )
        self._conn.commit()

    def remove_pending_enrich(self, chain_id: str, token_addresses: list[str]) -> None:
        if not token_addresses:
            return
        chain_id = norm_chain(chain_id)
        cur = self._conn.cursor()
        cur.executemany(
            "DELETE FROM pending_enrich WHERE chain_id = ? AND token_address = ?",
            [(chain_id, norm_address(a)) for a in token_addresses],
        )
        self._conn.commit()

    def list_pending_enrich(self, *, limit: int | None = None) -> dict[str, list[str]]:
        """Return {chain_id: [token_address, ...]} ordered by added_at.

        ``limit`` caps rows loaded into one cycle (P2-20). None loads the table.
        """
        sql = "SELECT chain_id, token_address FROM pending_enrich ORDER BY added_at ASC"
        params: list[Any] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        rows = self._conn.execute(sql, params).fetchall()
        out: dict[str, list[str]] = {}
        for r in rows:
            out.setdefault(r["chain_id"], []).append(r["token_address"])
        return out

    def count_pending_enrich(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM pending_enrich").fetchone()[0])

    def count_tokens(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0])

    def count_pairs(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM pairs").fetchone()[0])

    def count_tokens_has_pairs(self, has_pairs: int) -> int:
        return int(
            self._conn.execute(
                "SELECT COUNT(*) FROM tokens WHERE has_pairs = ?", (has_pairs,)
            ).fetchone()[0]
        )

    def count_tokens_unenriched(self) -> int:
        return int(
            self._conn.execute(
                "SELECT COUNT(*) FROM tokens WHERE has_pairs IS NULL"
            ).fetchone()[0]
        )

    # --- run checkpoint / resume (Phase 2) ---

    def create_checkpoint(
        self,
        cycle_id: str,
        *,
        flush_pending: bool = False,
        stats: dict[str, Any] | None = None,
    ) -> None:
        now = _utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO run_checkpoint (
                cycle_id, started_at, updated_at, finished_at,
                status, step, last_batch_meta, stats_json, flush_pending
            ) VALUES (?, ?, ?, NULL, 'running', 'started', NULL, ?, ?)
            """,
            (
                cycle_id,
                now,
                now,
                json.dumps(stats or {}, ensure_ascii=False),
                1 if flush_pending else 0,
            ),
        )
        self._conn.commit()

    def update_checkpoint(
        self,
        cycle_id: str,
        *,
        step: str | None = None,
        status: str | None = None,
        last_batch_meta: dict[str, Any] | None = None,
        stats: dict[str, Any] | None = None,
        finished: bool = False,
    ) -> None:
        now = _utc_now_iso()
        row = self._conn.execute(
            "SELECT step, status, last_batch_meta, stats_json FROM run_checkpoint WHERE cycle_id = ?",
            (cycle_id,),
        ).fetchone()
        if row is None:
            return
        new_step = step if step is not None else row["step"]
        new_status = status if status is not None else row["status"]
        meta = (
            json.dumps(last_batch_meta, ensure_ascii=False)
            if last_batch_meta is not None
            else row["last_batch_meta"]
        )
        stats_json = (
            json.dumps(stats, ensure_ascii=False, default=str)
            if stats is not None
            else row["stats_json"]
        )
        finished_at = now if finished else None
        if finished:
            self._conn.execute(
                """
                UPDATE run_checkpoint
                SET updated_at = ?, finished_at = ?, status = ?, step = ?,
                    last_batch_meta = ?, stats_json = ?
                WHERE cycle_id = ?
                """,
                (now, finished_at, new_status, new_step, meta, stats_json, cycle_id),
            )
        else:
            self._conn.execute(
                """
                UPDATE run_checkpoint
                SET updated_at = ?, status = ?, step = ?,
                    last_batch_meta = ?, stats_json = ?
                WHERE cycle_id = ?
                """,
                (now, new_status, new_step, meta, stats_json, cycle_id),
            )
        self._conn.commit()

    def get_incomplete_checkpoint(self) -> dict[str, Any] | None:
        """Return the most recent running/interrupted checkpoint, if any."""
        row = self._conn.execute(
            """
            SELECT * FROM run_checkpoint
            WHERE status IN ('running', 'interrupted')
              AND step != 'done'
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def get_checkpoint(self, cycle_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM run_checkpoint WHERE cycle_id = ?",
            (cycle_id,),
        ).fetchone()
        return dict(row) if row else None

    # --- Phase 3 scheduler / tiers ---

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        self._conn.commit()

    def get_cycle_number(self) -> int:
        raw = self.get_meta("cycle_number", "0") or "0"
        try:
            return int(raw)
        except ValueError:
            return 0

    def bump_cycle_number(self) -> int:
        return self._bump_meta("cycle_number", 1)

    def get_last_profile_poll_at(self) -> str | None:
        return self.get_meta("last_profile_poll_at")

    def set_last_profile_poll_at(self, iso_ts: str) -> None:
        self.set_meta("last_profile_poll_at", iso_ts)

    def list_tokens_for_tiering(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT chain_id, token_address, has_pairs, pair_count,
                   last_seen_at, last_enriched_at, updated_at
            FROM tokens
            """
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Phase 5 API queries ---

    def list_tokens_api(
        self,
        *,
        chain: str | None = None,
        has_pairs: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        sql = """
            SELECT chain_id, token_address, source, updated_at, pair_count, has_pairs,
                   last_seen_at, last_enriched_at, raw_json,
                   quality_score, quality_tags, scored_at
            FROM tokens WHERE 1=1
        """
        params: list[Any] = []
        if chain:
            sql += " AND chain_id = ?"
            params.append(norm_chain(chain))
        if has_pairs is not None:
            sql += " AND has_pairs = ?"
            params.append(int(has_pairs))
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["raw"] = json.loads(d.pop("raw_json"))
            except json.JSONDecodeError:
                d["raw"] = None
                d.pop("raw_json", None)
            try:
                d["quality_tags"] = json.loads(d.get("quality_tags") or "[]")
            except (json.JSONDecodeError, TypeError):
                d["quality_tags"] = []
            out.append(d)
        return out

    def get_token_api(self, chain_id: str, token_address: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT chain_id, token_address, source, updated_at, pair_count, has_pairs,
                   last_seen_at, last_enriched_at, raw_json,
                   quality_score, quality_tags, scored_at
            FROM tokens WHERE chain_id = ? AND token_address = ?
            """,
            (norm_chain(chain_id), norm_address(token_address)),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["raw"] = json.loads(d.pop("raw_json"))
        except json.JSONDecodeError:
            d["raw"] = None
            d.pop("raw_json", None)
        try:
            d["quality_tags"] = json.loads(d.get("quality_tags") or "[]")
        except (json.JSONDecodeError, TypeError):
            d["quality_tags"] = []
        return d

    def list_pairs_api(
        self,
        *,
        chain: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        sql = """
            SELECT chain_id, pair_address, token_address, source, updated_at, raw_json
            FROM pairs WHERE 1=1
        """
        params: list[Any] = []
        if chain:
            sql += " AND chain_id = ?"
            params.append(norm_chain(chain))
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["raw"] = json.loads(d.pop("raw_json"))
            except json.JSONDecodeError:
                d["raw"] = None
                d.pop("raw_json", None)
            out.append(d)
        return out

    def get_pair_api(self, chain_id: str, pair_address: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT chain_id, pair_address, token_address, source, updated_at, raw_json
            FROM pairs WHERE chain_id = ? AND pair_address = ?
            """,
            (norm_chain(chain_id), norm_address(pair_address)),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["raw"] = json.loads(d.pop("raw_json"))
        except json.JSONDecodeError:
            d["raw"] = None
            d.pop("raw_json", None)
        return d

    def list_alerts_api(
        self,
        *,
        since: str | None = None,
        alert_type: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        sql = """
            SELECT id, created_at, type, chain_id, token_address, pair_address,
                   severity, message, payload_json, delivered
            FROM alerts WHERE 1=1
        """
        params: list[Any] = []
        if since:
            sql += " AND created_at >= ?"
            params.append(since)
        if alert_type:
            sql += " AND type = ?"
            params.append(alert_type)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d.pop("payload_json") or "{}")
            except json.JSONDecodeError:
                d["payload"] = {}
                d.pop("payload_json", None)
            out.append(d)
        return out

    def latest_checkpoint(self) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT * FROM run_checkpoint
            ORDER BY started_at DESC LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None

    def _migrate_x_social(self) -> None:
        """P1.3: X mention watch state + request budget counters."""
        cur = self._conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS x_watch (
                chain_id TEXT NOT NULL,
                token_address TEXT NOT NULL,
                symbol TEXT,
                query_key TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'early',
                since_id TEXT,
                mention_count_total INTEGER NOT NULL DEFAULT 0,
                mention_count_prev INTEGER NOT NULL DEFAULT 0,
                mention_count_last INTEGER NOT NULL DEFAULT 0,
                last_searched_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (chain_id, token_address)
            );
            CREATE INDEX IF NOT EXISTS idx_x_watch_mode ON x_watch (mode);
            CREATE TABLE IF NOT EXISTS x_budget (
                bucket TEXT PRIMARY KEY,
                request_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            """
        )
        cols = self._table_columns("x_watch")
        if "expires_at" not in cols:
            cur.execute("ALTER TABLE x_watch ADD COLUMN expires_at TEXT")
        self._conn.commit()

    def upsert_x_watch(
        self,
        chain_id: str,
        token_address: str,
        *,
        symbol: str | None = None,
        query_key: str,
        mode: str = "early",
    ) -> None:
        chain_id = norm_chain(chain_id)
        token_address = norm_address(token_address)
        now = _utc_now_iso()
        expires_at = _iso_after(_x_watch_ttl_sec(mode))
        self._conn.execute(
            """
            INSERT INTO x_watch (
                chain_id, token_address, symbol, query_key, mode,
                since_id, mention_count_total, mention_count_prev, mention_count_last,
                last_searched_at, created_at, updated_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, NULL, 0, 0, 0, NULL, ?, ?, ?)
            ON CONFLICT(chain_id, token_address) DO UPDATE SET
                symbol = COALESCE(excluded.symbol, x_watch.symbol),
                query_key = excluded.query_key,
                mode = excluded.mode,
                expires_at = excluded.expires_at,
                updated_at = excluded.updated_at
            """,
            (chain_id, token_address, symbol, query_key, mode, now, now, expires_at),
        )
        self._conn.commit()

    def prune_x_watch(self) -> int:
        """Drop expired watches and evict low-activity rows over the cap.

        Never-searched rows are evicted last so a full FIFO cannot block new tokens.
        """
        now = _utc_now_iso()
        cur = self._conn.cursor()
        cur.execute(
            "DELETE FROM x_watch WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        )
        removed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        total = int(cur.execute("SELECT COUNT(*) FROM x_watch").fetchone()[0])
        overflow = total - _x_watch_max_rows()
        if overflow > 0:
            cur.execute(
                """
                DELETE FROM x_watch WHERE rowid IN (
                    SELECT rowid FROM x_watch
                    ORDER BY
                        CASE WHEN last_searched_at IS NULL THEN 1 ELSE 0 END,
                        mention_count_total ASC,
                        created_at ASC
                    LIMIT ?
                )
                """,
                (overflow,),
            )
            if cur.rowcount and cur.rowcount > 0:
                removed += cur.rowcount
        self._conn.commit()
        return removed

    def get_x_watch(self, chain_id: str, token_address: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM x_watch WHERE chain_id = ? AND token_address = ?",
            (norm_chain(chain_id), norm_address(token_address)),
        ).fetchone()
        return dict(row) if row else None

    def list_x_watch(self, *, mode: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        if not self.readonly:
            self.prune_x_watch()
        now = _utc_now_iso()
        # Unsearched (new) rows first so a large already-searched FIFO cannot
        # starve tokens that have not had a search yet.
        order = """
            ORDER BY
                CASE WHEN last_searched_at IS NULL THEN 0 ELSE 1 END,
                COALESCE(last_searched_at, created_at) ASC
            LIMIT ?
        """
        if mode:
            rows = self._conn.execute(
                f"""
                SELECT * FROM x_watch
                WHERE mode = ?
                  AND (expires_at IS NULL OR expires_at >= ?)
                {order}
                """,
                (mode, now, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"""
                SELECT * FROM x_watch
                WHERE expires_at IS NULL OR expires_at >= ?
                {order}
                """,
                (now, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_x_watch_search(
        self,
        chain_id: str,
        token_address: str,
        *,
        since_id: str | None,
        mention_count_last: int,
        bump_total: int,
    ) -> dict[str, Any] | None:
        """Rotate prev←last, set last, bump total, update since_id. Returns updated row."""
        chain_id = norm_chain(chain_id)
        token_address = norm_address(token_address)
        now = _utc_now_iso()
        row = self.get_x_watch(chain_id, token_address)
        if not row:
            return None
        prev = int(row.get("mention_count_last") or 0)
        total = int(row.get("mention_count_total") or 0) + max(0, int(bump_total))
        new_since = since_id or row.get("since_id")
        self._conn.execute(
            """
            UPDATE x_watch SET
                since_id = ?,
                mention_count_prev = ?,
                mention_count_last = ?,
                mention_count_total = ?,
                last_searched_at = ?,
                updated_at = ?
            WHERE chain_id = ? AND token_address = ?
            """,
            (
                new_since,
                prev,
                int(mention_count_last),
                total,
                now,
                now,
                chain_id,
                token_address,
            ),
        )
        self._conn.commit()
        return self.get_x_watch(chain_id, token_address)

    def bump_x_budget(self, bucket: str, delta: int = 1) -> int:
        now = _utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO x_budget (bucket, request_count, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(bucket) DO UPDATE SET
                request_count = request_count + excluded.request_count,
                updated_at = excluded.updated_at
            """,
            (bucket, delta, now),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT request_count FROM x_budget WHERE bucket = ?", (bucket,)
        ).fetchone()
        return int(row["request_count"]) if row else delta

    def get_x_budget(self, bucket: str) -> int:
        row = self._conn.execute(
            "SELECT request_count FROM x_budget WHERE bucket = ?", (bucket,)
        ).fetchone()
        return int(row["request_count"]) if row else 0

    def list_recent_tokens(
        self, *, within_sec: int, limit: int = 20
    ) -> list[dict[str, Any]]:
        from datetime import datetime, timezone, timedelta

        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=within_sec)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        limit = max(1, min(int(limit), 100))
        rows = self._conn.execute(
            """
            SELECT chain_id, token_address, has_pairs, pair_count,
                   last_seen_at, last_enriched_at, updated_at, raw_json, source
            FROM tokens
            WHERE COALESCE(last_seen_at, updated_at) >= ?
            ORDER BY COALESCE(last_seen_at, updated_at) DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["raw"] = json.loads(d.pop("raw_json"))
            except (json.JSONDecodeError, TypeError, KeyError):
                d["raw"] = None
                d.pop("raw_json", None)
            out.append(d)
        return out

    def guess_token_symbol(self, chain_id: str, token_address: str) -> str | None:
        """Best-effort symbol from pairs.raw_json baseToken or token raw_json."""
        chain_id = norm_chain(chain_id)
        token_address = norm_address(token_address)
        row = self._conn.execute(
            """
            SELECT raw_json FROM pairs
            WHERE chain_id = ? AND token_address = ?
            ORDER BY updated_at DESC LIMIT 1
            """,
            (chain_id, token_address),
        ).fetchone()
        if row:
            try:
                raw = json.loads(row["raw_json"])
                sym = (raw.get("baseToken") or {}).get("symbol")
                if sym and isinstance(sym, str) and 1 <= len(sym) <= 20:
                    return sym.strip()
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
        trow = self._conn.execute(
            "SELECT raw_json FROM tokens WHERE chain_id = ? AND token_address = ?",
            (chain_id, token_address),
        ).fetchone()
        if trow:
            try:
                raw = json.loads(trow["raw_json"])
                for key in ("symbol", "name"):
                    v = raw.get(key)
                    if isinstance(v, str) and 1 <= len(v) <= 20 and " " not in v.strip():
                        return v.strip()
            except (json.JSONDecodeError, TypeError):
                pass
        return None

    def _migrate_pair_history(self) -> None:
        cur = self._conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS pair_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chain_id TEXT NOT NULL,
                pair_address TEXT NOT NULL,
                token_address TEXT,
                recorded_at TEXT NOT NULL,
                price_usd REAL,
                volume_h24 REAL,
                volume_h1 REAL,
                liquidity_usd REAL,
                txns_h24 INTEGER,
                tier TEXT,
                market_cap REAL,
                fdv REAL
            );
            CREATE INDEX IF NOT EXISTS idx_pair_history_lookup
                ON pair_history (chain_id, pair_address, recorded_at);
            CREATE INDEX IF NOT EXISTS idx_pair_history_token
                ON pair_history (chain_id, token_address, recorded_at);
            """
        )
        # last snapshot stamp on pairs for due checks
        cols = self._table_columns("pairs")
        if "last_history_at" not in cols:
            cur.execute("ALTER TABLE pairs ADD COLUMN last_history_at TEXT")
        self._conn.commit()

    def _migrate_txn_flow_columns(self) -> None:
        """Nullable order-flow columns on pair_history (idempotent).

        volume_h1 already exists and is reused. No backfill: raw_json only
        holds the latest body. Missing values stay NULL, never 0.
        """
        cur = self._conn.cursor()
        cols = self._table_columns("pair_history")
        for name, typ in TXN_FLOW_COLUMNS:
            if name not in cols:
                cur.execute(f"ALTER TABLE pair_history ADD COLUMN {name} {typ}")
        self._conn.commit()

    def _migrate_market_cap_columns(self) -> None:
        """Nullable market_cap / fdv on pair_history and pairs.

        No backfill from raw_json: a stored pair payload is the latest body,
        not the market cap at each historical snapshot. Old rows stay NULL
        until the next upsert or history snapshot. Dex omitting the field
        writes NULL; fdv is never copied into market_cap.
        """
        cur = self._conn.cursor()
        for table in ("pair_history", "pairs"):
            cols = self._table_columns(table)
            if "market_cap" not in cols:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN market_cap REAL")
            if "fdv" not in cols:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN fdv REAL")
        self._conn.commit()

    def _migrate_intel_loop_i1(self) -> None:
        """Additive INTEL_LOOP I-1 tables. No backfill and no invented rows.

        address_labels.token_mint is '' when the label is not token-scoped
        (SQLite PRIMARY KEY cannot hold NULL). Holder rows for one snapshot_id
        share recorded_at and the supply basis. Transfer edges are a light stub.
        """
        cur = self._conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS address_labels (
                chain_id TEXT NOT NULL,
                address TEXT NOT NULL,
                label_type TEXT NOT NULL,
                token_mint TEXT NOT NULL DEFAULT '',
                pair_address TEXT,
                source TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (chain_id, address, label_type, token_mint)
            );

            CREATE INDEX IF NOT EXISTS idx_address_labels_mint
                ON address_labels (chain_id, token_mint, label_type);

            CREATE INDEX IF NOT EXISTS idx_address_labels_type
                ON address_labels (label_type);

            CREATE TABLE IF NOT EXISTS token_holder_snapshots (
                chain_id TEXT NOT NULL,
                token_address TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                rank INTEGER NOT NULL,
                holder_address TEXT NOT NULL,
                address_kind TEXT NOT NULL,
                amount_raw TEXT,
                amount_ui REAL,
                pct_of_supply REAL,
                total_supply_raw TEXT,
                total_supply_ui REAL,
                source TEXT NOT NULL,
                PRIMARY KEY (chain_id, token_address, snapshot_id, rank)
            );

            CREATE INDEX IF NOT EXISTS idx_holder_snapshots_token_time
                ON token_holder_snapshots (chain_id, token_address, recorded_at);

            CREATE TABLE IF NOT EXISTS token_transfer_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chain_id TEXT NOT NULL,
                token_address TEXT NOT NULL,
                from_address TEXT NOT NULL,
                to_address TEXT NOT NULL,
                amount_raw TEXT,
                amount_ui REAL,
                signature TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                source TEXT NOT NULL
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_transfer_edges_dedupe
                ON token_transfer_edges (
                    chain_id,
                    signature,
                    token_address,
                    from_address,
                    to_address,
                    IFNULL(amount_raw, '')
                );

            CREATE INDEX IF NOT EXISTS idx_transfer_edges_token_time
                ON token_transfer_edges (chain_id, token_address, recorded_at);
            """
        )
        self._conn.commit()

    def _migrate_intel_loop_i3(self) -> None:
        """Additive I-3 bot markers. No backfill. Missing inputs stay NULL.

        ``bot_fill_observations`` is one parsed fill (fee payer + mint whose
        raw balance changed). ``bot_reason`` NULL means the row was not
        flagged, not that the wallet is human. ``token_transfer_edges.bot_reason``
        is stamped only after a heuristic hits that signature.
        """
        cur = self._conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS bot_fill_observations (
                chain_id TEXT NOT NULL,
                signature TEXT NOT NULL,
                wallet TEXT NOT NULL,
                token_address TEXT NOT NULL,
                slot INTEGER,
                block_time INTEGER,
                recorded_at TEXT NOT NULL,
                fee_lamports INTEGER,
                compute_unit_price INTEGER,
                compute_unit_limit INTEGER,
                priority_fee_lamports INTEGER,
                source TEXT NOT NULL,
                bot_reason TEXT,
                PRIMARY KEY (chain_id, signature, wallet, token_address)
            );

            CREATE INDEX IF NOT EXISTS idx_bot_fills_wallet
                ON bot_fill_observations (chain_id, wallet, token_address, recorded_at);

            CREATE INDEX IF NOT EXISTS idx_bot_fills_token_time
                ON bot_fill_observations (chain_id, token_address, recorded_at);
            """
        )
        cols = self._table_columns("token_transfer_edges")
        if "bot_reason" not in cols:
            cur.execute("ALTER TABLE token_transfer_edges ADD COLUMN bot_reason TEXT")
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_transfer_edges_bot_reason
                ON token_transfer_edges (bot_reason)
            """
        )
        self._conn.commit()

    def _migrate_quality_score(self) -> None:
        cols = self._table_columns("tokens")
        cur = self._conn.cursor()
        if "quality_score" not in cols:
            cur.execute("ALTER TABLE tokens ADD COLUMN quality_score REAL")
        if "quality_tags" not in cols:
            cur.execute("ALTER TABLE tokens ADD COLUMN quality_tags TEXT")
        if "scored_at" not in cols:
            cur.execute("ALTER TABLE tokens ADD COLUMN scored_at TEXT")
        self._conn.commit()


    def _migrate_pair_created_at(self) -> None:
        """Add pairs.pair_created_at (ms epoch, nullable) for token_age attribution."""
        cols = self._table_columns("pairs")
        cur = self._conn.cursor()
        if "pair_created_at" not in cols:
            cur.execute("ALTER TABLE pairs ADD COLUMN pair_created_at INTEGER")
        # Optional index for age / recency filters (cheap; nullable column).
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_pairs_pair_created_at ON pairs (pair_created_at)"
        )
        self._conn.commit()

    def backfill_pair_created_at(self, *, batch_size: int = 2000) -> dict[str, int]:
        """One-shot: parse pairCreatedAt from raw_json into pair_created_at. No inventing."""
        self._migrate_pair_created_at()
        cur = self._conn.cursor()
        rows = cur.execute(
            "SELECT chain_id, pair_address, raw_json FROM pairs WHERE pair_created_at IS NULL"
        ).fetchall()
        updated = 0
        skipped = 0
        bad_json = 0
        pending: list[tuple[int | None, str, str]] = []
        for row in rows:
            try:
                raw = json.loads(row["raw_json"])
            except (json.JSONDecodeError, TypeError):
                bad_json += 1
                skipped += 1
                continue
            if not isinstance(raw, dict):
                skipped += 1
                continue
            ms = _parse_pair_created_at(raw)
            if ms is None:
                skipped += 1
                continue
            pending.append((ms, row["chain_id"], row["pair_address"]))
            if len(pending) >= batch_size:
                cur.executemany(
                    """
                    UPDATE pairs SET pair_created_at = ?
                    WHERE chain_id = ? AND pair_address = ?
                    """,
                    pending,
                )
                updated += len(pending)
                pending.clear()
        if pending:
            cur.executemany(
                """
                UPDATE pairs SET pair_created_at = ?
                WHERE chain_id = ? AND pair_address = ?
                """,
                pending,
            )
            updated += len(pending)
        self._conn.commit()
        return {
            "scanned_null": len(rows),
            "updated": updated,
            "skipped_missing": skipped,
            "bad_json": bad_json,
        }

    def insert_pair_history(
        self,
        chain_id: str,
        pair_address: str,
        *,
        token_address: str | None = None,
        price_usd: float | None = None,
        volume_h24: float | None = None,
        volume_h1: float | None = None,
        liquidity_usd: float | None = None,
        txns_h24: int | None = None,
        market_cap: float | None = None,
        fdv: float | None = None,
        tier: str | None = None,
        recorded_at: str | None = None,
        txns_m5_buys: int | None = None,
        txns_m5_sells: int | None = None,
        txns_h1_buys: int | None = None,
        txns_h1_sells: int | None = None,
        volume_m5: float | None = None,
    ) -> int:
        chain_id = norm_chain(chain_id)
        pair_address = norm_address(pair_address)
        tok = norm_address(token_address) if token_address else None
        now = recorded_at or _utc_now_iso()
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO pair_history (
                chain_id, pair_address, token_address, recorded_at,
                price_usd, volume_h24, volume_h1, liquidity_usd, txns_h24, tier,
                market_cap, fdv,
                txns_m5_buys, txns_m5_sells, txns_h1_buys, txns_h1_sells, volume_m5
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chain_id, pair_address, tok, now,
                price_usd, volume_h24, volume_h1, liquidity_usd, txns_h24, tier,
                market_cap, fdv,
                txns_m5_buys, txns_m5_sells, txns_h1_buys, txns_h1_sells, volume_m5,
            ),
        )
        cur.execute(
            """
            UPDATE pairs SET last_history_at = ? WHERE chain_id = ? AND pair_address = ?
            """,
            (now, chain_id, pair_address),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def get_pair_last_history_at(self, chain_id: str, pair_address: str) -> str | None:
        row = self._conn.execute(
            "SELECT last_history_at FROM pairs WHERE chain_id = ? AND pair_address = ?",
            (norm_chain(chain_id), norm_address(pair_address)),
        ).fetchone()
        if row and row["last_history_at"]:
            return row["last_history_at"]
        # fallback max from history
        row2 = self._conn.execute(
            """
            SELECT MAX(recorded_at) AS m FROM pair_history
            WHERE chain_id = ? AND pair_address = ?
            """,
            (norm_chain(chain_id), norm_address(pair_address)),
        ).fetchone()
        return row2["m"] if row2 else None

    def latest_pair_price(
        self, chain_id: str, pair_address: str
    ) -> dict[str, Any] | None:
        """Newest pair_history quote: ORDER BY recorded_at DESC LIMIT 1."""
        return fetch_latest_pair_price(self._conn, chain_id, pair_address)

    def pair_spot(self, chain_id: str, pair_address: str) -> dict[str, Any] | None:
        """Current pairs.priceUsd plus pairs.updated_at (may be newer than history)."""
        return fetch_pair_spot(self._conn, chain_id, pair_address)

    def list_pair_history(
        self,
        chain_id: str,
        pair_address: str,
        *,
        limit: int = 200,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 2000))
        columns = pair_history_column_sql(self._table_columns("pair_history"))
        sql = f"""
            SELECT {columns}
            FROM pair_history
            WHERE chain_id = ? AND pair_address = ?
        """
        params: list[Any] = [norm_chain(chain_id), norm_address(pair_address)]
        if since:
            sql += " AND recorded_at >= ?"
            params.append(since)
        sql += " ORDER BY recorded_at ASC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def purge_old_pair_history(self, retention_days: int) -> int:
        from datetime import datetime, timezone, timedelta
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(1, retention_days))
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        cur = self._conn.cursor()
        cur.execute("DELETE FROM pair_history WHERE recorded_at < ?", (cutoff,))
        self._conn.commit()
        return int(cur.rowcount)

    def set_token_quality(
        self,
        chain_id: str,
        token_address: str,
        score: float,
        tags: list[str],
    ) -> None:
        now = _utc_now_iso()
        self._conn.execute(
            """
            UPDATE tokens SET quality_score = ?, quality_tags = ?, scored_at = ?, updated_at = ?
            WHERE chain_id = ? AND token_address = ?
            """,
            (
                float(score),
                json.dumps(tags, ensure_ascii=False),
                now,
                now,
                norm_chain(chain_id),
                norm_address(token_address),
            ),
        )
        self._conn.commit()

    def get_token_quality(self, chain_id: str, token_address: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT quality_score, quality_tags, scored_at
            FROM tokens WHERE chain_id = ? AND token_address = ?
            """,
            (norm_chain(chain_id), norm_address(token_address)),
        ).fetchone()
        if not row:
            return None
        tags = []
        try:
            tags = json.loads(row["quality_tags"] or "[]")
        except json.JSONDecodeError:
            tags = []
        return {
            "quality_score": row["quality_score"],
            "quality_tags": tags,
            "scored_at": row["scored_at"],
        }

    def list_top_pairs_by_metric(
        self,
        *,
        metric: str = "volume",
        limit: int = 10,
        chain: str | None = None,
    ) -> list[dict[str, Any]]:
        """Best-effort top gainers/volume from current pair raw_json."""
        limit = max(1, min(int(limit), 50))
        sql = "SELECT chain_id, pair_address, token_address, raw_json, updated_at FROM pairs"
        params: list[Any] = []
        if chain:
            sql += " WHERE chain_id = ?"
            params.append(norm_chain(chain))
        rows = self._conn.execute(sql, params).fetchall()
        scored: list[tuple[float, dict[str, Any]]] = []
        for r in rows:
            try:
                raw = json.loads(r["raw_json"])
            except json.JSONDecodeError:
                continue
            price_change = None
            pc = raw.get("priceChange") or {}
            if isinstance(pc, dict):
                try:
                    price_change = float(pc.get("h24") or pc.get("h6") or pc.get("h1") or 0)
                except (TypeError, ValueError):
                    price_change = 0.0
            vol = 0.0
            v = raw.get("volume") or {}
            if isinstance(v, dict):
                try:
                    vol = float(v.get("h24") or 0)
                except (TypeError, ValueError):
                    vol = 0.0
            liq = 0.0
            li = raw.get("liquidity") or {}
            if isinstance(li, dict):
                try:
                    liq = float(li.get("usd") or 0)
                except (TypeError, ValueError):
                    liq = 0.0
            if metric == "gainer":
                key_v = price_change or 0.0
            elif metric == "liquidity":
                key_v = liq
            else:
                key_v = vol
            scored.append(
                (
                    key_v,
                    {
                        "chain_id": r["chain_id"],
                        "pair_address": r["pair_address"],
                        "token_address": r["token_address"],
                        "price_usd": raw.get("priceUsd"),
                        "price_change_h24": price_change,
                        "volume_h24": vol,
                        "liquidity_usd": liq,
                        "symbol": (raw.get("baseToken") or {}).get("symbol"),
                        "updated_at": r["updated_at"],
                    },
                )
            )
        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored[:limit]]

    def count_alerts_since(self, since_iso: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM alerts WHERE created_at >= ?",
            (since_iso,),
        ).fetchone()
        return int(row["c"]) if row else 0

    def count_pair_history(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM pair_history").fetchone()[0])

    def _table_exists(self, name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        return row is not None

    def upsert_address_label(
        self,
        chain_id: str,
        address: str,
        label_type: str,
        *,
        token_mint: str | None = None,
        pair_address: str | None = None,
        source: str,
        now: str | None = None,
    ) -> str:
        """Insert or refresh a label. first_seen_at is kept. Returns inserted|updated|skipped.

        An empty address, label, or source is skipped. Nothing is invented.
        """
        chain_id = norm_chain(chain_id)
        address = norm_address(address)
        label_type = label_type.strip().lower()
        source = source.strip()
        if not address or not label_type or not source:
            return "skipped"
        mint = norm_address(token_mint) if token_mint else ""
        pair = norm_address(pair_address) if pair_address else None
        ts = now or _utc_now_iso()
        cur = self._conn.cursor()
        existing = cur.execute(
            """
            SELECT first_seen_at FROM address_labels
            WHERE chain_id = ? AND address = ? AND label_type = ? AND token_mint = ?
            """,
            (chain_id, address, label_type, mint),
        ).fetchone()
        if existing is None:
            cur.execute(
                """
                INSERT INTO address_labels (
                    chain_id, address, label_type, token_mint, pair_address,
                    source, first_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (chain_id, address, label_type, mint, pair, source, ts, ts),
            )
            self._conn.commit()
            return "inserted"
        cur.execute(
            """
            UPDATE address_labels
            SET pair_address = COALESCE(?, pair_address),
                source = ?,
                updated_at = ?
            WHERE chain_id = ? AND address = ? AND label_type = ? AND token_mint = ?
            """,
            (pair, source, ts, chain_id, address, label_type, mint),
        )
        self._conn.commit()
        return "updated"

    def list_address_labels(
        self,
        chain_id: str,
        *,
        token_mint: str | None = None,
        label_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Labels for a chain. Empty token_mint in the table is returned as None."""
        sql = (
            "SELECT chain_id, address, label_type, token_mint, pair_address, "
            "source, first_seen_at, updated_at FROM address_labels WHERE chain_id = ?"
        )
        params: list[Any] = [norm_chain(chain_id)]
        if token_mint is not None:
            sql += " AND token_mint = ?"
            params.append(norm_address(token_mint) if token_mint else "")
        if label_type is not None:
            sql += " AND label_type = ?"
            params.append(label_type.strip().lower())
        sql += " ORDER BY updated_at DESC, address ASC"
        rows = self._conn.execute(sql, params).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            mint = row["token_mint"] or None
            out.append(
                {
                    "chain_id": row["chain_id"],
                    "address": row["address"],
                    "label_type": row["label_type"],
                    "token_mint": mint,
                    "pair_address": row["pair_address"],
                    "source": row["source"],
                    "first_seen_at": row["first_seen_at"],
                    "updated_at": row["updated_at"],
                }
            )
        return out

    def insert_holder_snapshot(
        self,
        chain_id: str,
        token_address: str,
        snapshot: dict[str, Any],
        *,
        max_rank: int = 10,
    ) -> int:
        """Persist one top-holder snapshot. Returns rows written.

        All rows share snapshot_id, recorded_at, and the supply basis.
        pct_of_supply is recomputed from amount_ui and total_supply_ui.
        A missing supply or amount leaves pct NULL. Empty input writes nothing.
        """
        if not isinstance(snapshot, dict):
            return 0
        snapshot_id = str(snapshot.get("snapshot_id") or "").strip()
        recorded_at = str(snapshot.get("recorded_at") or "").strip()
        source = str(snapshot.get("source") or "").strip()
        raw_rows = snapshot.get("rows")
        if not snapshot_id or not recorded_at or not source or not isinstance(raw_rows, list):
            return 0
        chain_id = norm_chain(chain_id)
        token_address = norm_address(token_address)
        if not token_address:
            return 0
        supply_raw = snapshot.get("total_supply_raw")
        if supply_raw is not None:
            supply_raw = str(supply_raw).strip() or None
        supply_ui = _optional_real(snapshot.get("total_supply_ui"))
        cap = max(1, int(max_rank))
        pending: list[tuple[Any, ...]] = []
        seen_ranks: set[int] = set()
        for item in raw_rows:
            if not isinstance(item, dict):
                continue
            rank_value = item.get("rank")
            if isinstance(rank_value, bool):
                continue
            try:
                rank = int(rank_value)
            except (TypeError, ValueError):
                continue
            if rank < 1 or rank > cap or rank in seen_ranks:
                continue
            holder = item.get("holder_address")
            if not isinstance(holder, str) or not holder.strip():
                continue
            kind = str(item.get("address_kind") or "")
            if kind not in ("owner", "token_account"):
                continue
            amount_ui = _optional_real(item.get("amount_ui"))
            amount_raw = item.get("amount_raw")
            if amount_raw is not None:
                amount_raw = str(amount_raw).strip() or None
            pending.append(
                (
                    chain_id,
                    token_address,
                    snapshot_id,
                    recorded_at,
                    rank,
                    norm_address(holder),
                    kind,
                    amount_raw,
                    amount_ui,
                    _pct_of_supply(amount_ui, supply_ui),
                    supply_raw,
                    supply_ui,
                    source,
                )
            )
            seen_ranks.add(rank)
        if not pending:
            return 0
        cur = self._conn.cursor()
        try:
            cur.executemany(
                """
                INSERT OR REPLACE INTO token_holder_snapshots (
                    chain_id, token_address, snapshot_id, recorded_at, rank,
                    holder_address, address_kind, amount_raw, amount_ui,
                    pct_of_supply, total_supply_raw, total_supply_ui, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                pending,
            )
            self._conn.commit()
        except sqlite3.Error:
            self._conn.rollback()
            raise
        return len(pending)

    def list_holder_snapshot_rows(
        self,
        chain_id: str,
        token_address: str,
        *,
        snapshot_id: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT chain_id, token_address, snapshot_id, recorded_at, rank, "
            "holder_address, address_kind, amount_raw, amount_ui, pct_of_supply, "
            "total_supply_raw, total_supply_ui, source "
            "FROM token_holder_snapshots WHERE chain_id = ? AND token_address = ?"
        )
        params: list[Any] = [norm_chain(chain_id), norm_address(token_address)]
        if snapshot_id is not None:
            sql += " AND snapshot_id = ?"
            params.append(snapshot_id)
        sql += " ORDER BY recorded_at ASC, snapshot_id ASC, rank ASC"
        return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def latest_holder_snapshot(
        self,
        chain_id: str,
        token_address: str,
    ) -> list[dict[str, Any]]:
        """Rows of the newest snapshot only, so a reader stays on one timestamp."""
        row = self._conn.execute(
            """
            SELECT snapshot_id FROM token_holder_snapshots
            WHERE chain_id = ? AND token_address = ?
            ORDER BY recorded_at DESC, snapshot_id DESC
            LIMIT 1
            """,
            (norm_chain(chain_id), norm_address(token_address)),
        ).fetchone()
        if row is None:
            return []
        return self.list_holder_snapshot_rows(
            chain_id, token_address, snapshot_id=row["snapshot_id"]
        )

    def insert_transfer_edges(self, rows: list[dict[str, Any]]) -> int:
        """Insert parsed transfer edges. Incomplete rows are skipped, not filled in."""
        if not rows:
            return 0
        pending: list[tuple[Any, ...]] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            chain_id = norm_chain(str(item.get("chain_id") or ""))
            token = item.get("token_address")
            src = item.get("from_address")
            dst = item.get("to_address")
            signature = item.get("signature")
            recorded_at = item.get("recorded_at")
            source = item.get("source")
            if not all(
                isinstance(value, str) and value.strip()
                for value in (chain_id, token, src, dst, signature, recorded_at, source)
            ):
                continue
            amount_raw = item.get("amount_raw")
            if amount_raw is not None:
                amount_raw = str(amount_raw).strip() or None
            pending.append(
                (
                    chain_id,
                    norm_address(str(token)),
                    norm_address(str(src)),
                    norm_address(str(dst)),
                    amount_raw,
                    _optional_real(item.get("amount_ui")),
                    str(signature).strip(),
                    str(recorded_at).strip(),
                    str(source).strip(),
                )
            )
        if not pending:
            return 0
        cur = self._conn.cursor()
        before = cur.execute("SELECT COUNT(*) AS c FROM token_transfer_edges").fetchone()["c"]
        cur.executemany(
            """
            INSERT OR IGNORE INTO token_transfer_edges (
                chain_id, token_address, from_address, to_address,
                amount_raw, amount_ui, signature, recorded_at, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            pending,
        )
        self._conn.commit()
        after = cur.execute("SELECT COUNT(*) AS c FROM token_transfer_edges").fetchone()["c"]
        return int(after) - int(before)

    def intel_observe_counts(self) -> dict[str, Any]:
        """I-1 observe counts. Missing tables are unavailable, not zeros that look filled."""
        empty = {
            "available": False,
            "labeled_dev_n": 0,
            "holder_snapshot_n": 0,
            "holder_snapshot_rows": 0,
            "tokens_with_top10": 0,
            "transfer_edge_n": 0,
        }
        needed = ("address_labels", "token_holder_snapshots", "token_transfer_edges")
        try:
            for name in needed:
                if not self._table_exists(name):
                    return {**empty, "error": f"missing table {name}"}
            dev = self._conn.execute(
                "SELECT COUNT(*) AS c FROM address_labels WHERE label_type = 'dev'"
            ).fetchone()
            snaps = self._conn.execute(
                """
                SELECT COUNT(DISTINCT snapshot_id) AS snaps,
                       COUNT(DISTINCT chain_id || ':' || token_address) AS tokens,
                       COUNT(*) AS rows
                FROM token_holder_snapshots
                """
            ).fetchone()
            edges = self._conn.execute(
                "SELECT COUNT(*) AS c FROM token_transfer_edges"
            ).fetchone()
        except sqlite3.OperationalError as exc:
            return {**empty, "error": type(exc).__name__}
        return {
            "available": True,
            "labeled_dev_n": int(dev["c"] or 0),
            "holder_snapshot_n": int(snaps["snaps"] or 0),
            "holder_snapshot_rows": int(snaps["rows"] or 0),
            "tokens_with_top10": int(snaps["tokens"] or 0),
            "transfer_edge_n": int(edges["c"] or 0),
        }

    def insert_bot_observations(self, rows: list[dict[str, Any]]) -> int:
        """Insert parsed bot-fill rows. Incomplete rows are skipped.

        Conflict keeps the first recorded_at and fills only NULL measurements.
        ``bot_reason`` is not set here.
        """
        if not rows:
            return 0
        pending: list[tuple[Any, ...]] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            chain_id = norm_chain(str(item.get("chain_id") or ""))
            signature = item.get("signature")
            wallet = item.get("wallet")
            token = item.get("token_address")
            recorded_at = item.get("recorded_at")
            source = item.get("source")
            if not all(
                isinstance(value, str) and value.strip()
                for value in (chain_id, signature, wallet, token, recorded_at, source)
            ):
                continue
            pending.append(
                (
                    chain_id,
                    str(signature).strip(),
                    norm_address(str(wallet)),
                    norm_address(str(token)),
                    _optional_int(item.get("slot")),
                    _optional_int(item.get("block_time")),
                    str(recorded_at).strip(),
                    _optional_int(item.get("fee_lamports")),
                    _optional_int(item.get("compute_unit_price")),
                    _optional_int(item.get("compute_unit_limit")),
                    _optional_int(item.get("priority_fee_lamports")),
                    str(source).strip(),
                )
            )
        if not pending:
            return 0
        cur = self._conn.cursor()
        cur.executemany(
            """
            INSERT INTO bot_fill_observations (
                chain_id, signature, wallet, token_address, slot, block_time,
                recorded_at, fee_lamports, compute_unit_price, compute_unit_limit,
                priority_fee_lamports, source, bot_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(chain_id, signature, wallet, token_address) DO UPDATE SET
                slot = COALESCE(bot_fill_observations.slot, excluded.slot),
                block_time = COALESCE(bot_fill_observations.block_time, excluded.block_time),
                fee_lamports = COALESCE(
                    bot_fill_observations.fee_lamports, excluded.fee_lamports
                ),
                compute_unit_price = COALESCE(
                    bot_fill_observations.compute_unit_price, excluded.compute_unit_price
                ),
                compute_unit_limit = COALESCE(
                    bot_fill_observations.compute_unit_limit, excluded.compute_unit_limit
                ),
                priority_fee_lamports = COALESCE(
                    bot_fill_observations.priority_fee_lamports,
                    excluded.priority_fee_lamports
                )
            """,
            pending,
        )
        self._conn.commit()
        return len(pending)

    def list_bot_observations(
        self,
        chain_id: str,
        wallet: str,
        token_address: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Recent fills for one wallet and token, oldest first within the cap."""
        cap = max(1, int(limit))
        rows = self._conn.execute(
            """
            SELECT chain_id, signature, wallet, token_address, slot, block_time,
                   recorded_at, fee_lamports, compute_unit_price, compute_unit_limit,
                   priority_fee_lamports, source, bot_reason
            FROM bot_fill_observations
            WHERE chain_id = ? AND wallet = ? AND token_address = ?
            ORDER BY recorded_at DESC, signature DESC
            LIMIT ?
            """,
            (norm_chain(chain_id), norm_address(wallet), norm_address(token_address), cap),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def merge_bot_observation_reason(
        self,
        chain_id: str,
        signature: str,
        wallet: str,
        token_address: str,
        reason: str,
    ) -> str | None:
        """Union ``reason`` into the fill row. Does not clear an existing reason."""
        merged_reason = _merge_bot_reason(None, reason)
        if not merged_reason:
            return None
        cur = self._conn.cursor()
        row = cur.execute(
            """
            SELECT bot_reason FROM bot_fill_observations
            WHERE chain_id = ? AND signature = ? AND wallet = ? AND token_address = ?
            """,
            (
                norm_chain(chain_id),
                signature.strip(),
                norm_address(wallet),
                norm_address(token_address),
            ),
        ).fetchone()
        if row is None:
            return None
        merged = _merge_bot_reason(row["bot_reason"], merged_reason)
        cur.execute(
            """
            UPDATE bot_fill_observations
            SET bot_reason = ?
            WHERE chain_id = ? AND signature = ? AND wallet = ? AND token_address = ?
            """,
            (
                merged,
                norm_chain(chain_id),
                signature.strip(),
                norm_address(wallet),
                norm_address(token_address),
            ),
        )
        self._conn.commit()
        return merged

    def merge_bot_edge_reason(
        self,
        chain_id: str,
        token_address: str,
        signature: str,
        reason: str,
    ) -> int:
        """Stamp transfer edges for one signature. Edges without a row stay absent."""
        merged_reason = _merge_bot_reason(None, reason)
        if not merged_reason:
            return 0
        cur = self._conn.cursor()
        rows = cur.execute(
            """
            SELECT id, bot_reason FROM token_transfer_edges
            WHERE chain_id = ? AND token_address = ? AND signature = ?
            """,
            (norm_chain(chain_id), norm_address(token_address), signature.strip()),
        ).fetchall()
        updated = 0
        for row in rows:
            merged = _merge_bot_reason(row["bot_reason"], merged_reason)
            if merged == (row["bot_reason"] or ""):
                continue
            cur.execute(
                "UPDATE token_transfer_edges SET bot_reason = ? WHERE id = ?",
                (merged, row["id"]),
            )
            updated += 1
        self._conn.commit()
        return updated

    def bot_observe_counts(self) -> dict[str, Any]:
        """I-3 observe counts. A missing table is unavailable, not a filled zero."""
        empty = {
            "available": False,
            "bot_suspect_n": 0,
            "bot_edge_n": 0,
        }
        try:
            if not self._table_exists("address_labels"):
                return {**empty, "error": "missing table address_labels"}
            if not self._table_exists("bot_fill_observations"):
                return {**empty, "error": "missing table bot_fill_observations"}
            if not self._table_exists("token_transfer_edges"):
                return {**empty, "error": "missing table token_transfer_edges"}
            if "bot_reason" not in self._table_columns("token_transfer_edges"):
                return {**empty, "error": "missing column token_transfer_edges.bot_reason"}
            suspect = self._conn.execute(
                "SELECT COUNT(*) AS c FROM address_labels WHERE label_type = 'bot_suspect'"
            ).fetchone()
            edges = self._conn.execute(
                """
                SELECT COUNT(*) AS c FROM token_transfer_edges
                WHERE bot_reason IS NOT NULL AND bot_reason != ''
                """
            ).fetchone()
        except sqlite3.OperationalError as exc:
            return {**empty, "error": type(exc).__name__}
        return {
            "available": True,
            "bot_suspect_n": int(suspect["c"] or 0),
            "bot_edge_n": int(edges["c"] or 0),
        }

    def bot_share_inputs(
        self,
        chain_id: str,
        token_address: str,
        *,
        since_recorded_at: str | None = None,
    ) -> dict[str, Any]:
        """Numerator and denominator for a later bot_trade_share.

        Denominator = parsed fills in the window. Numerator = those with a
        bot_reason. Unparsed transactions are left out, not counted as human.
        ``share`` is NULL when the denominator is 0. A share of 0 is not a
        human label and is not a skip signal.
        """
        empty = {
            "available": False,
            "total_fills": None,
            "bot_fills": None,
            "share": None,
        }
        try:
            if not self._table_exists("bot_fill_observations"):
                return {**empty, "error": "missing table bot_fill_observations"}
            sql = (
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN bot_reason IS NOT NULL AND bot_reason != '' "
                "THEN 1 ELSE 0 END) AS bot "
                "FROM bot_fill_observations WHERE chain_id = ? AND token_address = ?"
            )
            params: list[Any] = [norm_chain(chain_id), norm_address(token_address)]
            if since_recorded_at:
                sql += " AND recorded_at >= ?"
                params.append(since_recorded_at)
            row = self._conn.execute(sql, params).fetchone()
        except sqlite3.OperationalError as exc:
            return {**empty, "error": type(exc).__name__}
        total = int(row["total"] or 0)
        bot_fills = int(row["bot"] or 0)
        return {
            "available": True,
            "total_fills": total,
            "bot_fills": bot_fills,
            "share": None if total == 0 else bot_fills / total,
        }


def _optional_int(value: Any) -> int | None:
    """Non-negative int, or None. Bools and floats are not coerced."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def _merge_bot_reason(existing: str | None, incoming: str | None) -> str:
    parts: set[str] = set()
    for blob in (existing, incoming):
        if not isinstance(blob, str):
            continue
        parts.update(part for part in blob.split(",") if part)
    return ",".join(sorted(parts))


def _optional_real(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _pct_of_supply(amount_ui: float | None, supply_ui: float | None) -> float | None:
    """Same-response ratio. Not a concentration factor. NULL if either side is missing."""
    if amount_ui is None or supply_ui is None or supply_ui <= 0:
        return None
    return float(amount_ui) / float(supply_ui)


def format_intel_observe_line(counts: dict[str, Any] | None) -> str:
    """Hourly observe line. The counts are 10-19 side evidence, not validity."""
    if not isinstance(counts, dict) or counts.get("error") or not counts.get("available"):
        return "I-1 observe (旁证, 不计有效性): unavailable"
    return (
        "I-1 observe (旁证, 不计有效性): "
        f"labeled_dev_n={int(counts.get('labeled_dev_n') or 0)} "
        f"holder_snapshot_n={int(counts.get('holder_snapshot_n') or 0)} "
        f"tokens_with_top10={int(counts.get('tokens_with_top10') or 0)}"
    )


def format_bot_observe_line(counts: dict[str, Any] | None) -> str:
    """Hourly I-3 line. Counts are 10-19 side evidence, not a skip signal."""
    if not isinstance(counts, dict) or counts.get("error") or not counts.get("available"):
        return "I-3 observe (旁证, 不计有效性): unavailable"
    return (
        "I-3 observe (旁证, 不计有效性): "
        f"bot_suspect_n={int(counts.get('bot_suspect_n') or 0)} "
        f"bot_edge_n={int(counts.get('bot_edge_n') or 0)}"
    )

