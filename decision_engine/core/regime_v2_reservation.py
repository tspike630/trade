"""Cross-process reservation for regime v2 caps.

The decision router writes one row per admitted buy. A second process that
still sees an empty execution ledger cannot take the same slot. Rows stay
until ``release_reservation`` is called. Nothing in this module deletes a
row because it is old.

Execution does not call ``release_reservation`` or ``confirm_reservation``.
A reserved buy that never becomes a ledger position keeps its slot. That is
fail-closed, not a finished confirm/release loop.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import sqlite3
from pathlib import Path
from typing import Any

RESERVATION_DB_ENV = "REGIME_V2_RESERVATION_DB"
RELEASE_INTERFACE_BLOCK = (
    "execution gateway does not call release_reservation or confirm_reservation; "
    "reserved rows stay occupied and are not dropped by age"
)


@dataclass(frozen=True)
class ReserveResult:
    ok: bool
    reason: str | None
    idempotent: bool
    signal_key: str


def reservation_db_path(environ: Mapping[str, str]) -> str | None:
    raw = str(environ.get(RESERVATION_DB_ENV, "")).strip()
    return raw or None


def signal_reservation_key(signal: Mapping[str, Any]) -> str:
    """Same signal replays to one row. A different symbol is a different key."""
    purpose = str(signal.get("purpose") or "").strip()
    symbol = str(
        signal.get("binance_symbol") or signal.get("symbol") or signal.get("token_address") or ""
    ).strip().upper()
    reason = str(signal.get("entry_reason") or "").strip()
    limit_px = signal.get("limit_price")
    stop_px = signal.get("stop_price")
    size = signal.get("size_pct")
    idem = str(signal.get("signal_idempotency_key") or signal.get("decision_log_id") or "").strip()
    return "|".join(
        (
            purpose,
            symbol,
            reason,
            "" if limit_px in (None, "") else str(limit_px),
            "" if stop_px in (None, "") else str(stop_px),
            "" if size in (None, "") else str(size),
            idem,
        )
    )


def _connect(path: str) -> sqlite3.Connection:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS regime_v2_reservation (
            signal_key TEXT PRIMARY KEY,
            purpose TEXT NOT NULL,
            symbol TEXT NOT NULL,
            cluster_key TEXT NOT NULL,
            regime TEXT NOT NULL,
            size_pct TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL,
            status TEXT NOT NULL,
            released_at_ms INTEGER
        )
        """
    )
    return conn


def active_reservations(path: str) -> list[dict[str, Any]]:
    conn = _connect(path)
    try:
        rows = conn.execute(
            """
            SELECT signal_key, purpose, symbol, cluster_key, regime, size_pct,
                   created_at_ms, status
            FROM regime_v2_reservation
            WHERE released_at_ms IS NULL
            """
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def release_reservation(path: str, signal_key: str, *, now_ms: int) -> bool:
    """Explicit release. Not called by the execution gateway."""
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """
            UPDATE regime_v2_reservation
            SET released_at_ms = ?, status = 'released'
            WHERE signal_key = ? AND released_at_ms IS NULL
            """,
            (int(now_ms), signal_key),
        )
        conn.execute("COMMIT")
        return cur.rowcount > 0
    except sqlite3.Error:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def try_reserve(
    path: str,
    signal: Mapping[str, Any],
    *,
    now_ms: int,
    open_symbols: Sequence[str],
    open_count: int,
    cluster_counts: Mapping[str, int],
    open_exposure_pct: float,
    max_concurrent: int,
    state4_max_concurrent: int,
    max_per_cluster: int,
    max_total_pct: float,
    state4_max_total_pct: float,
    size_pct: float,
    cluster_key: str,
    is_state4: bool,
) -> ReserveResult:
    """Insert one reserved buy if the locked book still has room.

    An existing row for the same key is success and does not add a second slot.
    """
    key = signal_reservation_key(signal)
    symbol = str(
        signal.get("binance_symbol") or signal.get("symbol") or signal.get("token_address") or ""
    ).strip().upper()
    purpose = str(signal.get("purpose") or "").strip()
    regime = str(signal.get("market_regime") or "").strip()
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """
            SELECT signal_key FROM regime_v2_reservation
            WHERE signal_key = ? AND released_at_ms IS NULL
            """,
            (key,),
        ).fetchone()
        if existing is not None:
            conn.execute("COMMIT")
            return ReserveResult(True, None, True, key)
        rows = conn.execute(
            """
            SELECT symbol, cluster_key, regime, size_pct
            FROM regime_v2_reservation
            WHERE released_at_ms IS NULL
            """
        ).fetchall()
        reserved_symbols = {str(row["symbol"] or "").upper() for row in rows}
        open_set = {str(item or "").upper() for item in open_symbols if str(item or "").strip()}
        if symbol and (symbol in open_set or symbol in reserved_symbols):
            conn.execute("COMMIT")
            return ReserveResult(False, "same_symbol_already_open", False, key)
        reserved_count = len(rows)
        state4_reserved = sum(1 for row in rows if str(row["regime"] or "") == "crowded_long_defensive")
        if is_state4:
            if state4_reserved >= state4_max_concurrent or open_count + reserved_count >= state4_max_concurrent:
                conn.execute("COMMIT")
                return ReserveResult(False, "max_concurrent_positions_reached", False, key)
        elif open_count + reserved_count >= max_concurrent:
            conn.execute("COMMIT")
            return ReserveResult(False, "max_concurrent_positions_reached", False, key)
        cluster_reserved = sum(1 for row in rows if str(row["cluster_key"] or "") == cluster_key)
        if cluster_key and int(cluster_counts.get(cluster_key, 0)) + cluster_reserved >= max_per_cluster:
            conn.execute("COMMIT")
            return ReserveResult(False, "cluster_concurrency_cap_reached", False, key)
        reserved_exposure = 0.0
        for row in rows:
            if str(row["symbol"] or "").upper() in open_set:
                continue
            try:
                reserved_exposure += float(row["size_pct"])
            except (TypeError, ValueError):
                conn.execute("COMMIT")
                return ReserveResult(False, "reservation_size_invalid", False, key)
        cap = state4_max_total_pct if is_state4 else max_total_pct
        if open_exposure_pct + reserved_exposure + size_pct > cap + 1e-9:
            conn.execute("COMMIT")
            return ReserveResult(False, "total_portfolio_exposure_cap_exceeded", False, key)
        conn.execute(
            """
            INSERT INTO regime_v2_reservation (
                signal_key, purpose, symbol, cluster_key, regime, size_pct,
                created_at_ms, status, released_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', NULL)
            """,
            (key, purpose, symbol, cluster_key, regime, str(size_pct), int(now_ms)),
        )
        conn.execute("COMMIT")
        return ReserveResult(True, None, False, key)
    except sqlite3.Error as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        return ReserveResult(False, f"reservation_store_error:{type(exc).__name__}", False, key)
    finally:
        conn.close()
