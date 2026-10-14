"""Persist one paper close: F-1 streak and C3 shadow, one transaction.

Idempotent on ``position_id``. A second call does not move the streak.
Failures here must not roll back the paper close; callers catch and log.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from execution.observe_schema import ensure_observe_schema
from execution.shadow_c3 import (
    hold_minutes,
    parse_meta,
    resolve_peak,
    shadow_metrics,
)
from execution.streak import next_streak

BUSY_TIMEOUT_MS = 30_000


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _strategy_id(meta: dict[str, Any]) -> str:
    text = str(meta.get("strategy_id") or "").strip()
    return text or "unknown"


def _changes(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT changes()").fetchone()
    return int(row[0] if row else 0)


def record_closed_position(
    db: str | Path,
    position: dict[str, Any],
    *,
    source: str,
    mid_price: float | None = None,
) -> dict[str, Any]:
    """Write ``streak_events`` / ``strategy_streak`` / ``shadow_c3``.

    ``source`` is ``tick`` or ``signal_sell`` (the paper close path).
    """
    position_id = str(position.get("id") or "").strip()
    if not position_id:
        raise ValueError("position id is required")
    meta = parse_meta(position.get("meta_json"))
    strategy_id = _strategy_id(meta)
    try:
        pnl = float(position.get("pnl_usd") or 0.0)
    except (TypeError, ValueError):
        pnl = 0.0
    try:
        entry = float(position.get("entry_price") or 0.0)
    except (TypeError, ValueError):
        entry = 0.0
    try:
        exit_price = float(position.get("exit_price") or 0.0)
    except (TypeError, ValueError):
        exit_price = 0.0
    peak = resolve_peak(meta, entry, mid_price)
    peak_pnl_pct, giveback = shadow_metrics(
        entry=entry, peak=peak, exit_price=exit_price
    )
    reason = str(position.get("exit_reason") or position.get("reason") or "unknown")
    closed_at = position.get("closed_at")
    hold = hold_minutes(position.get("opened_at"), closed_at)
    now = _utc()

    path = Path(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        ensure_observe_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT streak FROM strategy_streak WHERE strategy_id = ?",
                (strategy_id,),
            ).fetchone()
            current = int(row[0]) if row else 0
            updated = next_streak(current, pnl)
            conn.execute(
                """
                INSERT OR IGNORE INTO streak_events (
                    strategy_id, position_id, pnl_usd, streak, source, event_ts
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (strategy_id, position_id, pnl, updated, str(source), now),
            )
            inserted_streak = _changes(conn) == 1
            if inserted_streak:
                conn.execute(
                    """
                    INSERT INTO strategy_streak (strategy_id, streak, last_pnl, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(strategy_id) DO UPDATE SET
                        streak = excluded.streak,
                        last_pnl = excluded.last_pnl,
                        updated_at = excluded.updated_at
                    """,
                    (strategy_id, updated, pnl, now),
                )
            conn.execute(
                """
                INSERT OR IGNORE INTO shadow_c3 (
                    position_id, strategy_id, entry_price, peak_price, exit_price,
                    peak_pnl_pct, giveback_from_peak_pct, hold_min, actual_reason,
                    actual_pnl_usd, closed_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position_id,
                    strategy_id,
                    entry,
                    peak,
                    exit_price,
                    peak_pnl_pct,
                    giveback,
                    hold,
                    reason,
                    pnl,
                    None if closed_at is None else str(closed_at),
                    now,
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        stored = conn.execute(
            "SELECT streak, last_pnl FROM strategy_streak WHERE strategy_id = ?",
            (strategy_id,),
        ).fetchone()
        shadow = conn.execute(
            "SELECT * FROM shadow_c3 WHERE position_id = ?",
            (position_id,),
        ).fetchone()
    finally:
        conn.close()
    shadow_row = dict(shadow) if shadow is not None else None
    return {
        "strategy_id": strategy_id,
        "position_id": position_id,
        "streak": int(stored[0]) if stored else 0,
        "last_pnl": None if stored is None else stored[1],
        "inserted_streak": inserted_streak,
        "shadow": shadow_row,
    }
