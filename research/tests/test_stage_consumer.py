"""Adversarial stage reads. A rogue price must not become a return."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from research.hist_calibration.stage_consumer import attach_verified_returns, study_from_stage

VALID = "https://data.binance.vision/data/spot/daily/klines/BTCUSDT/5m/BTCUSDT-5m-2026-11-09.zip"
ROGUE = "https://example.invalid/old/BTCUSDT-5m-2026-11-09.zip"
OPEN_MS = 1_794_182_400_000
VALID_OPEN = 100.0
ROGUE_OPEN = 998.9  # about 89890 bp above 100 if used as the next open


def _stage(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE vision_files (
            url TEXT PRIMARY KEY, sha256 TEXT, nbytes INTEGER,
            kind TEXT, status TEXT, detail TEXT
        );
        CREATE TABLE spot_klines (
            symbol TEXT, interval TEXT, open_time_ms INTEGER, close_time_ms INTEGER,
            quote_volume REAL, taker_buy_quote REAL, open REAL, source_url TEXT
        );
        CREATE TABLE um_book_depth_band (
            symbol TEXT, ts_ms INTEGER, percentage REAL, depth REAL,
            notional REAL, source_url TEXT
        );
        """
    )


def _manifest(urls: list[str], sha: str = "aa" * 32) -> dict:
    return {
        "symbols": ["BTCUSDT"],
        "start": "2026-11-09",
        "end": "2026-11-09",
        "interval": "5m",
        "iter_plan_urls": urls,
        "files": [{"url": url, "expected_sha256": sha, "symbol": "BTCUSDT"} for url in urls],
    }


def _file(conn: sqlite3.Connection, url: str, sha: str, status: str = "checksum_verified") -> None:
    conn.execute(
        "INSERT INTO vision_files VALUES (?, ?, 10, 'klines', ?, NULL)",
        (url, sha, status),
    )


def _bar(conn: sqlite3.Connection, url: str, open_px: float, open_ms: int = OPEN_MS, symbol: str = "BTCUSDT") -> None:
    conn.execute(
        "INSERT INTO spot_klines VALUES (?, '5m', ?, ?, 1, 1, ?, ?)",
        (symbol, open_ms, open_ms + 299_999, open_px, url),
    )


class StageConsumerTests(unittest.TestCase):
    def test_same_timestamp_conflict_does_not_pick_the_rogue_price(self) -> None:
        sha = "ab" * 32
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "stage.db")
            _stage(conn)
            _file(conn, VALID, sha)
            _file(conn, ROGUE, sha)
            _bar(conn, VALID, VALID_OPEN)
            _bar(conn, ROGUE, ROGUE_OPEN)
            conn.execute(
                "INSERT INTO um_book_depth_band VALUES ('BTCUSDT', ?, -5, 1, 1, ?)",
                (OPEN_MS, VALID),
            )
            conn.commit()
            report = study_from_stage(conn, _manifest([VALID, ROGUE], sha))
            conn.close()
        self.assertFalse(report["study_ready"])
        self.assertFalse(report["raw_stage_study_ready"])
        self.assertEqual(report["bars"], [])
        self.assertEqual(len(report["conflicts"]), 1)
        self.assertIsNone(report["obi_top5"])
        joined = attach_verified_returns(
            [{"symbol": "BTCUSDT", "event_ms": OPEN_MS - 1}],
            report["bars"],
        )
        self.assertIsNone(joined[0]["forward_return"])

    def test_unverified_rogue_hash_is_ignored_and_valid_bar_remains(self) -> None:
        sha = "cd" * 32
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "stage.db")
            _stage(conn)
            _file(conn, VALID, sha)
            _file(conn, ROGUE, "00" * 32, status="checksum_verified")
            _bar(conn, VALID, VALID_OPEN)
            _bar(conn, ROGUE, ROGUE_OPEN)
            conn.commit()
            report = study_from_stage(conn, _manifest([VALID], sha))
            conn.close()
        self.assertTrue(report["study_ready"])
        self.assertEqual(len(report["bars"]), 1)
        self.assertEqual(report["bars"][0]["open"], VALID_OPEN)
        self.assertGreater(report["ignored_rows"], 0)
        joined = attach_verified_returns(
            [{"symbol": "BTCUSDT", "event_ms": OPEN_MS}],
            report["bars"] + [{"symbol": "BTCUSDT", "interval": "5m", "open_time_ms": OPEN_MS + 300_000, "open": VALID_OPEN * 1.009, "source_url": VALID}],
        )
        self.assertAlmostEqual(joined[0]["forward_return"], 0.009)
        self.assertNotAlmostEqual(joined[0]["forward_return"], 8.989)

    def test_missing_manifest_and_out_of_batch_rows_are_not_study_ready(self) -> None:
        sha = "ef" * 32
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "stage.db")
            _stage(conn)
            self.assertFalse(study_from_stage(conn, None)["study_ready"])
            self.assertEqual(study_from_stage(conn, None)["verification"]["errors"], ["missing_manifest"])
            _file(conn, VALID, sha)
            _file(conn, ROGUE, sha)
            _bar(conn, ROGUE, ROGUE_OPEN)
            _bar(conn, VALID, VALID_OPEN, symbol="ETHUSDT")
            later = OPEN_MS + 86_400_000
            _bar(conn, VALID, ROGUE_OPEN, open_ms=later)
            conn.commit()
            report = study_from_stage(conn, _manifest([VALID], sha))
            conn.close()
        self.assertFalse(report["study_ready"])
        self.assertEqual(report["bars"], [])
        self.assertTrue(report["verification"]["ok"])

    def test_checksum_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "stage.db")
            _stage(conn)
            _file(conn, VALID, "11" * 32)
            _bar(conn, VALID, ROGUE_OPEN)
            conn.commit()
            report = study_from_stage(conn, _manifest([VALID], "22" * 32))
            conn.close()
        self.assertFalse(report["verification"]["ok"])
        self.assertFalse(report["study_ready"])
        self.assertEqual(report["bars"], [])
        self.assertTrue(any(item.startswith("checksum_mismatch") for item in report["verification"]["errors"]))


if __name__ == "__main__":
    unittest.main()
