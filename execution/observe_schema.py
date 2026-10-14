"""SQLite tables for F-1 streaks and C3/F-3 shadow giveback.

Lives on execution_ledger.db. Tick closes have no execution_log row, so this
is not an order_events status. CREATE is idempotent.
"""

from __future__ import annotations

import sqlite3

OBSERVE_DDL = """
CREATE TABLE IF NOT EXISTS strategy_streak (
    strategy_id TEXT PRIMARY KEY,
    streak INTEGER NOT NULL,
    last_pnl REAL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS streak_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    position_id TEXT NOT NULL,
    pnl_usd REAL NOT NULL,
    streak INTEGER NOT NULL,
    source TEXT NOT NULL,
    event_ts TEXT NOT NULL,
    UNIQUE(position_id)
);
CREATE INDEX IF NOT EXISTS idx_streak_events_strategy
    ON streak_events (strategy_id, id);
CREATE TABLE IF NOT EXISTS shadow_c3 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    entry_price REAL NOT NULL,
    peak_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    peak_pnl_pct REAL NOT NULL,
    giveback_from_peak_pct REAL NOT NULL,
    hold_min REAL,
    actual_reason TEXT NOT NULL,
    actual_pnl_usd REAL,
    closed_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(position_id)
);
CREATE INDEX IF NOT EXISTS idx_shadow_c3_reason
    ON shadow_c3 (actual_reason);
"""


def ensure_observe_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(OBSERVE_DDL)
