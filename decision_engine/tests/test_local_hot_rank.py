"""Hot-token order uses local_score, not jev_strength."""

from __future__ import annotations

import logging
import sqlite3
import tempfile
import unittest
from pathlib import Path

from decision_engine.core.data_adapter import (
    HOT_RANK_COLUMNS,
    IndicatorDataAdapter,
    local_hot_rank_key,
)


class LocalHotRankTests(unittest.TestCase):
    def test_whitelist_excludes_jev(self) -> None:
        self.assertEqual(
            HOT_RANK_COLUMNS,
            ("local_score", "volume_strength", "change_5m"),
        )

    def test_rank_key_puts_null_local_score_last(self) -> None:
        rows = [
            {
                "local_score": None,
                "volume_strength": 9.0,
                "change_5m": 9.0,
                "jev_strength": 1.0,
            },
            {
                "local_score": -0.2,
                "volume_strength": 0.0,
                "change_5m": -50.0,
                "jev_strength": 0.0,
            },
            {
                "local_score": 0.2,
                "volume_strength": 0.0,
                "change_5m": 0.0,
                "jev_strength": 0.0,
            },
        ]
        ranked = sorted(rows, key=local_hot_rank_key)
        self.assertEqual([row["local_score"] for row in ranked], [0.2, -0.2, None])

    def _capture(self) -> tuple[logging.Handler, list[str]]:
        messages: list[str] = []

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                messages.append(record.getMessage())

        handler = _Handler()
        logger = logging.getLogger("dexscreener.decision_engine.data_adapter")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        return handler, messages

    def test_hot_rows_order_by_local_score_then_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ind.db"
            conn = sqlite3.connect(path)
            conn.execute(
                """
                CREATE TABLE token_indicators (
                    chain_id TEXT, token_address TEXT, tier TEXT,
                    jev_strength REAL, local_score REAL, volume_strength REAL,
                    change_5m REAL, updated_at TEXT
                )
                """
            )
            conn.executemany(
                """
                INSERT INTO token_indicators (
                    chain_id, token_address, tier, jev_strength, local_score,
                    volume_strength, change_5m, updated_at
                ) VALUES ('solana', ?, 'hot', ?, ?, ?, ?, ?)
                """,
                [
                    ("a", 0.9, 0.1, 9.0, 9.0, "2026-01-01T00:00:00Z"),
                    ("b", 0.1, 0.8, 0.1, 0.1, "2026-01-01T00:00:00Z"),
                    ("c", 1.0, None, 5.0, 1.0, "2026-01-01T00:00:00Z"),
                    ("d", 1.0, None, None, 50.0, "2026-01-02T00:00:00Z"),
                    ("e", 9.0, None, None, None, "2026-01-01T00:00:00Z"),
                    ("f", 9.0, -0.4, 0.0, -20.0, "2026-01-03T00:00:00Z"),
                ],
            )
            conn.commit()
            conn.close()
            with IndicatorDataAdapter(path) as adapter:
                rows = adapter.list_hot_tokens()
            self.assertEqual(
                [row["token_address"] for row in rows],
                ["b", "a", "f", "c", "d", "e"],
            )

    def test_no_hot_tier_falls_back_to_local_rank(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ind.db"
            conn = sqlite3.connect(path)
            conn.execute(
                """
                CREATE TABLE token_indicators (
                    chain_id TEXT, token_address TEXT, tier TEXT,
                    jev_strength REAL, local_score REAL, volume_strength REAL,
                    change_5m REAL, updated_at TEXT
                )
                """
            )
            conn.executemany(
                """
                INSERT INTO token_indicators
                    (chain_id, token_address, tier, jev_strength, local_score,
                     volume_strength, change_5m, updated_at)
                VALUES ('solana', ?, 'warm', ?, ?, ?, ?, '2026-01-01T00:00:00Z')
                """,
                [
                    ("hi_jev", 0.99, None, 0.2, 1.0),
                    ("hi_local", 0.01, 0.4, 0.2, 1.0),
                    ("hi_vol", 0.5, None, 3.0, -5.0),
                ],
            )
            conn.commit()
            conn.close()
            handler, messages = self._capture()
            try:
                with IndicatorDataAdapter(path) as adapter:
                    rows = adapter.list_hot_tokens(limit=10)
            finally:
                logging.getLogger(
                    "dexscreener.decision_engine.data_adapter"
                ).removeHandler(handler)
            self.assertEqual(
                [row["token_address"] for row in rows],
                ["hi_local", "hi_vol", "hi_jev"],
            )
            self.assertTrue(
                any("by local_score,volume_strength,change_5m" in msg for msg in messages),
                messages,
            )
            self.assertFalse(any("jev_strength" in msg for msg in messages), messages)


if __name__ == "__main__":
    unittest.main()
