"""SQLite upsert storage for tokens, pairs, pending enrich, and run checkpoints."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm_chain(chain_id: str) -> str:
    return chain_id.strip().lower()


def norm_address(address: str) -> str:
    """Force lowercase for stable PKs / batch keys (API accepts lowercased addrs)."""
    return address.strip().lower()


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
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

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
        self._migrate_lowercase_addresses()
        self._migrate_x_social()
        self._migrate_pair_history()
        self._migrate_quality_score()
        self._migrate_pair_created_at()

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
        if "last_seen_at" not in cols:
            cur.execute("ALTER TABLE tokens ADD COLUMN last_seen_at TEXT")
        if "last_enriched_at" not in cols:
            cur.execute("ALTER TABLE tokens ADD COLUMN last_enriched_at TEXT")
        # Backfill: treat updated_at as last_seen; enriched rows get last_enriched_at
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
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
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
    ) -> str:
        """Upsert a token profile. Preserves pair_count/has_pairs on update. Returns inserted|updated."""
        chain_id = norm_chain(chain_id)
        token_address = norm_address(token_address)
        now = _utc_now_iso()
        raw_json = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
        cur = self._conn.cursor()
        cur.execute(
            "SELECT 1 FROM tokens WHERE chain_id = ? AND token_address = ?",
            (chain_id, token_address),
        )
        exists = cur.fetchone() is not None
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
                pair_created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chain_id, pair_address) DO UPDATE SET
                token_address = excluded.token_address,
                raw_json = excluded.raw_json,
                source = excluded.source,
                updated_at = excluded.updated_at,
                pair_created_at = excluded.pair_created_at
            """,
            (chain_id, pair_address, token_address, raw_json, source, now, pair_created_at),
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

    def bump_counter(self, key: str, delta: int = 1) -> int:
        cur = self.get_meta(f"counter_{key}", "0") or "0"
        try:
            n = int(cur) + delta
        except ValueError:
            n = delta
        self.set_meta(f"counter_{key}", str(n))
        return n

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

    def list_pending_enrich(self) -> dict[str, list[str]]:
        """Return {chain_id: [token_address, ...]} ordered by added_at."""
        rows = self._conn.execute(
            "SELECT chain_id, token_address FROM pending_enrich ORDER BY added_at ASC"
        ).fetchall()
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
        n = self.get_cycle_number() + 1
        self.set_meta("cycle_number", str(n))
        return n

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
        self._conn.execute(
            """
            INSERT INTO x_watch (
                chain_id, token_address, symbol, query_key, mode,
                since_id, mention_count_total, mention_count_prev, mention_count_last,
                last_searched_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, NULL, 0, 0, 0, NULL, ?, ?)
            ON CONFLICT(chain_id, token_address) DO UPDATE SET
                symbol = COALESCE(excluded.symbol, x_watch.symbol),
                query_key = excluded.query_key,
                mode = excluded.mode,
                updated_at = excluded.updated_at
            """,
            (chain_id, token_address, symbol, query_key, mode, now, now),
        )
        self._conn.commit()

    def get_x_watch(self, chain_id: str, token_address: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM x_watch WHERE chain_id = ? AND token_address = ?",
            (norm_chain(chain_id), norm_address(token_address)),
        ).fetchone()
        return dict(row) if row else None

    def list_x_watch(self, *, mode: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        if mode:
            rows = self._conn.execute(
                """
                SELECT * FROM x_watch WHERE mode = ?
                ORDER BY COALESCE(last_searched_at, created_at) ASC LIMIT ?
                """,
                (mode, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT * FROM x_watch
                ORDER BY COALESCE(last_searched_at, created_at) ASC LIMIT ?
                """,
                (limit,),
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
                tier TEXT
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
        tier: str | None = None,
        recorded_at: str | None = None,
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
                price_usd, volume_h24, volume_h1, liquidity_usd, txns_h24, tier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chain_id, pair_address, tok, now,
                price_usd, volume_h24, volume_h1, liquidity_usd, txns_h24, tier,
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
        sql = """
            SELECT id, chain_id, pair_address, token_address, recorded_at,
                   price_usd, volume_h24, volume_h1, liquidity_usd, txns_h24, tier
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

