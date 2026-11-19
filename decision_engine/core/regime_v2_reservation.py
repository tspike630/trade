"""Cross-process reservation for regime v2 caps.

The decision router writes one row per admitted buy. A second process that
still sees an empty execution ledger cannot take the same slot. Nothing in
this module deletes a row because it is old.

``unknown`` stays occupied. A release requires a reason that matches the
current status. Live and dry-run rows do not share a concurrency count.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import sqlite3
from pathlib import Path
from typing import Any

RESERVATION_DB_ENV = "REGIME_V2_RESERVATION_DB"
RELEASE_INTERFACE_BLOCK = (
    "unknown reservations stay occupied; the gateway does not release them by age"
)
_RELEASE_FROM = {
    "definite_no_fill": frozenset({"reserved", "submit_pending"}),
    "exchange_rejected": frozenset({"submit_pending"}),
    "flat_close": frozenset({"held"}),
}
_CONFIRM_PHASES = frozenset({"submit_pending", "unknown", "held"})


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
    present = {str(row[1]) for row in conn.execute("PRAGMA table_info(regime_v2_reservation)")}
    if "mode" not in present:
        conn.execute(
            "ALTER TABLE regime_v2_reservation ADD COLUMN mode TEXT NOT NULL DEFAULT 'live'"
        )
    if "live_order_permitted" not in present:
        conn.execute(
            "ALTER TABLE regime_v2_reservation "
            "ADD COLUMN live_order_permitted INTEGER NOT NULL DEFAULT 0"
        )
    if "decision_log_id" not in present:
        conn.execute("ALTER TABLE regime_v2_reservation ADD COLUMN decision_log_id TEXT")
    if "release_reason" not in present:
        conn.execute("ALTER TABLE regime_v2_reservation ADD COLUMN release_reason TEXT")
    if "updated_at_ms" not in present:
        conn.execute("ALTER TABLE regime_v2_reservation ADD COLUMN updated_at_ms INTEGER")
    return conn


def active_reservations(path: str) -> list[dict[str, Any]]:
    conn = _connect(path)
    try:
        rows = conn.execute(
            """
            SELECT signal_key, purpose, symbol, cluster_key, regime, size_pct,
                   created_at_ms, status, mode, live_order_permitted, decision_log_id
            FROM regime_v2_reservation
            WHERE released_at_ms IS NULL
            """
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _book_mode(signal: Mapping[str, Any]) -> tuple[str, int] | None:
    """Live only when the decision marked the order permitted and not dry-run."""
    permitted = signal.get("live_order_permitted") is True
    mode_text = str(signal.get("mode") or "").strip().lower()
    if permitted and mode_text == "dry_run":
        return None
    if permitted:
        return "live", 1
    return "dry_run", 0


def _identity_matches(
    row: sqlite3.Row | Mapping[str, Any],
    *,
    symbol: str | None,
    purpose: str | None,
    decision_log_id: str | None,
) -> bool:
    if symbol is not None and str(row["symbol"] or "").upper() != symbol.strip().upper():
        return False
    if purpose is not None and str(row["purpose"] or "") != purpose:
        return False
    stored = str(row["decision_log_id"] or "").strip()
    if decision_log_id and stored and stored != decision_log_id:
        return False
    return True


def get_reservation(path: str, signal_key: str) -> dict[str, Any] | None:
    conn = _connect(path)
    try:
        row = conn.execute(
            """
            SELECT signal_key, purpose, symbol, cluster_key, regime, size_pct,
                   created_at_ms, status, mode, live_order_permitted,
                   decision_log_id, release_reason, released_at_ms
            FROM regime_v2_reservation
            WHERE signal_key = ?
            """,
            (signal_key,),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else dict(row)


def reservation_for_decision(
    path: str, decision_log_id: str
) -> tuple[dict[str, Any] | None, str | None]:
    """One occupying or released row for this decision. Two rows fail closed."""
    conn = _connect(path)
    try:
        rows = conn.execute(
            """
            SELECT signal_key, purpose, symbol, status, mode, live_order_permitted,
                   decision_log_id, release_reason, released_at_ms
            FROM regime_v2_reservation
            WHERE decision_log_id = ?
            """,
            (decision_log_id,),
        ).fetchall()
    finally:
        conn.close()
    if len(rows) > 1:
        return None, "reservation_decision_ambiguous"
    if not rows:
        return None, None
    return dict(rows[0]), None


def attach_decision_id(
    path: str,
    signal_key: str,
    *,
    decision_log_id: str,
    symbol: str,
    purpose: str,
) -> bool:
    """Fill an empty decision id. Does not change status and does not release."""
    if not decision_log_id.strip():
        return False
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM regime_v2_reservation WHERE signal_key = ?",
            (signal_key,),
        ).fetchone()
        if row is None or not _identity_matches(
            row, symbol=symbol, purpose=purpose, decision_log_id=decision_log_id
        ):
            conn.execute("COMMIT")
            return False
        if row["released_at_ms"] is not None:
            conn.execute("COMMIT")
            return False
        conn.execute(
            """
            UPDATE regime_v2_reservation
            SET decision_log_id = COALESCE(decision_log_id, ?)
            WHERE signal_key = ? AND released_at_ms IS NULL
            """,
            (decision_log_id, signal_key),
        )
        conn.execute("COMMIT")
        return True
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        return False
    finally:
        conn.close()


def confirm_reservation(
    path: str,
    signal_key: str,
    *,
    now_ms: int,
    phase: str,
    symbol: str,
    purpose: str,
    decision_log_id: str | None,
) -> tuple[bool, str | None]:
    """Move an occupying row forward. Never releases. Identity mismatch writes nothing."""
    if phase not in _CONFIRM_PHASES:
        return False, "reservation_phase_invalid"
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM regime_v2_reservation WHERE signal_key = ?",
            (signal_key,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return False, "reservation_missing"
        if not _identity_matches(
            row, symbol=symbol, purpose=purpose, decision_log_id=decision_log_id
        ):
            conn.execute("COMMIT")
            return False, "reservation_identity_mismatch"
        status = str(row["status"] or "")
        if status == "released" or row["released_at_ms"] is not None:
            conn.execute("COMMIT")
            return False, "reservation_released"
        if phase == "submit_pending" and int(row["live_order_permitted"] or 0) != 1:
            conn.execute("COMMIT")
            return False, "live_order_not_permitted"
        if str(row["mode"] or "") == "dry_run" and phase == "submit_pending":
            conn.execute("COMMIT")
            return False, "live_order_not_permitted"
        stored_decision = str(row["decision_log_id"] or "").strip()
        next_status = status
        if status == "unknown" and phase == "submit_pending":
            next_status = "unknown"
        elif status == "held" and phase in {"submit_pending", "unknown"}:
            next_status = "held"
        else:
            next_status = phase
        conn.execute(
            """
            UPDATE regime_v2_reservation
            SET status = ?, updated_at_ms = ?,
                decision_log_id = COALESCE(decision_log_id, ?)
            WHERE signal_key = ? AND released_at_ms IS NULL
            """,
            (next_status, int(now_ms), decision_log_id, signal_key),
        )
        conn.execute("COMMIT")
        return True, None
    except sqlite3.Error as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        return False, f"reservation_store_error:{type(exc).__name__}"
    finally:
        conn.close()


def release_reservation(
    path: str,
    signal_key: str,
    *,
    now_ms: int,
    reason: str = "definite_no_fill",
    symbol: str | None = None,
    purpose: str | None = None,
    decision_log_id: str | None = None,
) -> bool:
    """Release only from a status that this reason is allowed to close.

    ``unknown`` is never released. A second release of the same row is a no-op
    success. Identity mismatch leaves the row unchanged and returns False.
    """
    allowed = _RELEASE_FROM.get(reason)
    if allowed is None:
        return False
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM regime_v2_reservation WHERE signal_key = ?",
            (signal_key,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return False
        if not _identity_matches(
            row, symbol=symbol, purpose=purpose, decision_log_id=decision_log_id
        ):
            conn.execute("COMMIT")
            return False
        if row["released_at_ms"] is not None or str(row["status"] or "") == "released":
            conn.execute("COMMIT")
            return True
        status = str(row["status"] or "")
        if status == "unknown" or status not in allowed:
            conn.execute("COMMIT")
            return False
        conn.execute(
            """
            UPDATE regime_v2_reservation
            SET released_at_ms = ?, status = 'released', release_reason = ?,
                updated_at_ms = ?,
                decision_log_id = COALESCE(decision_log_id, ?)
            WHERE signal_key = ? AND released_at_ms IS NULL AND status = ?
            """,
            (int(now_ms), reason, int(now_ms), decision_log_id, signal_key, status),
        )
        conn.execute("COMMIT")
        return True
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        return False
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
    book = _book_mode(signal)
    if book is None:
        return ReserveResult(False, "live_order_not_permitted", False, key)
    book_mode, permitted_bit = book
    symbol = str(
        signal.get("binance_symbol") or signal.get("symbol") or signal.get("token_address") or ""
    ).strip().upper()
    purpose = str(signal.get("purpose") or "").strip()
    regime = str(signal.get("market_regime") or "").strip()
    decision_id = str(signal.get("decision_log_id") or "").strip() or None
    if not symbol:
        return ReserveResult(False, "reservation_identity_missing", False, key)
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
            SELECT symbol, cluster_key, regime, size_pct, mode
            FROM regime_v2_reservation
            WHERE released_at_ms IS NULL
            """
        ).fetchall()
        reserved_symbols = {str(row["symbol"] or "").upper() for row in rows}
        open_set = {str(item or "").upper() for item in open_symbols if str(item or "").strip()}
        if symbol and (symbol in open_set or symbol in reserved_symbols):
            conn.execute("COMMIT")
            return ReserveResult(False, "same_symbol_already_open", False, key)
        same_book = [row for row in rows if str(row["mode"] or "live") == book_mode]
        reserved_count = len(same_book)
        state4_reserved = sum(
            1 for row in same_book if str(row["regime"] or "") == "crowded_long_defensive"
        )
        if is_state4:
            if state4_reserved >= state4_max_concurrent or open_count + reserved_count >= state4_max_concurrent:
                conn.execute("COMMIT")
                return ReserveResult(False, "max_concurrent_positions_reached", False, key)
        elif open_count + reserved_count >= max_concurrent:
            conn.execute("COMMIT")
            return ReserveResult(False, "max_concurrent_positions_reached", False, key)
        cluster_reserved = sum(
            1 for row in same_book if str(row["cluster_key"] or "") == cluster_key
        )
        if cluster_key and int(cluster_counts.get(cluster_key, 0)) + cluster_reserved >= max_per_cluster:
            conn.execute("COMMIT")
            return ReserveResult(False, "cluster_concurrency_cap_reached", False, key)
        reserved_exposure = 0.0
        for row in same_book:
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
                created_at_ms, status, released_at_ms, mode, live_order_permitted,
                decision_log_id, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', NULL, ?, ?, ?, ?)
            """,
            (
                key,
                purpose,
                symbol,
                cluster_key,
                regime,
                str(size_pct),
                int(now_ms),
                book_mode,
                permitted_bit,
                decision_id,
                int(now_ms),
            ),
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
