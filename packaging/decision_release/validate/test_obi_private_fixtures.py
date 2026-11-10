#!/usr/bin/env python3
"""Private top-5 fixtures for the consumer proof. No production database."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

NOW_MS = 1_700_000_000_000
WINDOW_MS = 3_600_000
ROOT = Path(__file__).resolve().parents[1]
PROVE = Path(__file__).resolve().parent / "prove_obi_consumer.py"


def _run(db: Path, now_ms: int | None = NOW_MS, extra: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(PROVE), "--db", str(db), "--release-root", str(ROOT)]
    if now_ms is not None:
        cmd.extend(["--now-ms", str(now_ms)])
    if extra:
        cmd.extend(extra)
    return subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C"},
    )


def _full_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE binance_spot_microstructure (
            symbol TEXT,
            ts_ms INTEGER,
            source_ts_ms INTEGER,
            collected_at_ms INTEGER,
            kline_5m_close_ts_ms INTEGER,
            book_update_id INTEGER,
            bid_depth_5_usd REAL,
            ask_depth_5_usd REAL,
            orderbook_imbalance_20 REAL,
            obi_top5 REAL,
            obi_top5_source_ts_ms INTEGER,
            obi_top5_error TEXT
        )
        """
    )


class ObiPrivateFixtureTest(unittest.TestCase):
    def test_stale_future_missing_and_error_newest_do_not_stamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "fixtures.db"
            conn = sqlite3.connect(db)
            _full_schema(conn)
            conn.executemany(
                """
                INSERT INTO binance_spot_microstructure (
                    symbol, ts_ms, source_ts_ms, collected_at_ms, kline_5m_close_ts_ms,
                    book_update_id, bid_depth_5_usd, ask_depth_5_usd,
                    orderbook_imbalance_20, obi_top5, obi_top5_source_ts_ms, obi_top5_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("BTCUSDT", NOW_MS + 50_000, NOW_MS, NOW_MS, NOW_MS, 10, 1, 1, -0.5, -0.25, NOW_MS - 1_000, None),
                    ("STALEUSDT", NOW_MS, NOW_MS, NOW_MS, NOW_MS, 11, 1, 1, -0.5, -0.25, NOW_MS - WINDOW_MS - 1, None),
                    ("FUTUREUSDT", NOW_MS, NOW_MS, NOW_MS, NOW_MS, 12, 1, 1, -0.5, -0.25, NOW_MS + 1, None),
                    ("MISSINGUSDT", NOW_MS, NOW_MS, NOW_MS, NOW_MS, 13, 1, 1, -0.4, None, None, None),
                    ("ERRUSDT", NOW_MS - 5_000, NOW_MS, NOW_MS, NOW_MS, 14, 1, 1, -0.5, -0.125, NOW_MS - 5_000, None),
                    (
                        "ERRUSDT",
                        NOW_MS,
                        NOW_MS,
                        NOW_MS,
                        NOW_MS,
                        15,
                        1,
                        1,
                        -0.5,
                        -0.875,
                        NOW_MS - 1_000,
                        "obi_top5_source_ts_gap",
                    ),
                    ("BOUNDUSDT", NOW_MS, NOW_MS, NOW_MS, NOW_MS, 16, 1, 1, -0.375, 0.125, NOW_MS - WINDOW_MS, None),
                ],
            )
            conn.commit()
            before = conn.execute("SELECT COUNT(*) FROM binance_spot_microstructure").fetchone()[0]
            conn.close()

            proc = _run(db)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            report = json.loads(proc.stdout)
            self.assertEqual(report["kind"], "obi_consumer_attach")
            self.assertTrue(report["not_a_raw_listing"])
            self.assertEqual(report["now_ms"], NOW_MS)
            self.assertEqual(report["db_path"], str(db.resolve()))
            self.assertTrue(report["write_probe"]["write_rejected"])
            self.assertTrue(report["write_probe"]["query_only"])
            self.assertTrue(report["attach_mode_ro"])
            self.assertTrue(report["rows_unchanged"])
            self.assertEqual(report["rows_before"], before)
            self.assertFalse(report["imported_config"])
            self.assertFalse(report["imported_dotenv"])
            self.assertEqual(report["logger"], "stub")
            self.assertFalse(report["coverage_complete"])
            self.assertEqual(report["contract_violations"], 0)
            by_symbol = {row["symbol"]: row for row in report["symbols"]}
            btc = by_symbol["BTCUSDT"]
            self.assertTrue(btc["stamped"])
            self.assertEqual(btc["stamped_obi_top5"], -0.25)
            self.assertEqual(btc["orderbook_imbalance_20"], -0.5)
            self.assertTrue(btc["distinct_top5_vs_top20"])
            self.assertEqual(btc["obi_top5_source_ts_ms"], NOW_MS - 1_000)
            self.assertNotEqual(btc["row_ts_ms"], btc["obi_top5_source_ts_ms"])
            self.assertEqual(report["btc"]["symbol"], "BTCUSDT")
            for name in ("STALEUSDT", "FUTUREUSDT", "MISSINGUSDT", "ERRUSDT"):
                self.assertFalse(by_symbol[name]["stamped"], name)
                self.assertIsNone(by_symbol[name]["stamped_obi_top5"], name)
                self.assertNotEqual(
                    by_symbol[name]["stamped_obi_top5"],
                    by_symbol[name]["orderbook_imbalance_20"],
                )
            self.assertEqual(by_symbol["ERRUSDT"]["error"], "obi_top5_source_ts_gap")
            self.assertEqual(by_symbol["ERRUSDT"]["obi_top5"], -0.875)
            self.assertTrue(by_symbol["BOUNDUSDT"]["stamped"])
            self.assertEqual(by_symbol["BOUNDUSDT"]["stamped_obi_top5"], 0.125)
            self.assertCountEqual(report["unstamped_symbols"], ["STALEUSDT", "FUTUREUSDT", "MISSINGUSDT", "ERRUSDT"])

            conn = sqlite3.connect(db)
            after = conn.execute("SELECT COUNT(*) FROM binance_spot_microstructure").fetchone()[0]
            probe = conn.execute(
                "SELECT name FROM sqlite_master WHERE name='obi_proof_write_probe'"
            ).fetchone()
            conn.close()
            self.assertEqual(after, before)
            self.assertIsNone(probe)

    def test_missing_top5_column_is_not_filled_from_top20(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "missing-column.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE binance_spot_microstructure (
                    symbol TEXT, ts_ms INTEGER, orderbook_imbalance_20 REAL
                )
                """
            )
            conn.execute(
                "INSERT INTO binance_spot_microstructure VALUES ('BTCUSDT', ?, -0.5013)",
                (NOW_MS,),
            )
            conn.commit()
            conn.close()
            proc = _run(db)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            report = json.loads(proc.stdout)
            self.assertFalse(report["columns_present"]["obi_top5"])
            self.assertFalse(report["coverage_complete"])
            self.assertEqual(report["btc"]["orderbook_imbalance_20"], -0.5013)
            self.assertFalse(report["btc"]["stamped"])
            self.assertIsNone(report["btc"]["stamped_obi_top5"])

    def test_clock_and_database_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "empty.db"
            sqlite3.connect(db).close()
            missing_clock = _run(db, now_ms=None)
            self.assertNotEqual(missing_clock.returncode, 0)
            missing_db = _run(Path(tmp) / "absent.db")
            self.assertEqual(missing_db.returncode, 2)


if __name__ == "__main__":
    unittest.main()
