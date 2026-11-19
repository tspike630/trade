"""A marked synthetic calendar so unrelated live ticks can sit in a proven gap.

This is not the CityU snapshot and not the lesg workbook import.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path


def write_quiet_macro(path: Path, now_ms: int | None = None) -> Path:
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    span = 7 * 24 * 3_600_000
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE macro_events (
            country TEXT, indicator_name TEXT, relevance INTEGER,
            local_time_hkt TEXT, utc_ts TEXT, utc_ms INTEGER
        );
        CREATE TABLE central_bank_events (
            country TEXT, event_name TEXT, is_high_impact INTEGER,
            local_time_hkt TEXT, utc_ts TEXT, utc_ms INTEGER
        );
        """
    )
    conn.executemany(
        """
        INSERT INTO macro_events VALUES ('United States', 'synthetic bracket', 3, ?, ?, ?)
        """,
        [
            ("synthetic 00:00:00", "before", now_ms - span),
            ("synthetic 00:00:00", "after", now_ms + span),
        ],
    )
    conn.commit()
    conn.close()
    return path
