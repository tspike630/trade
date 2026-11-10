"""Bars-only LSEG CSV import. No network and no institutional reload."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from decision_engine.core.macro_circuit_breaker import MAX_CROSS_ASSET_AGE_MS
from scripts import import_lseg_bars_incremental as imp


def _ms(iso: str) -> int:
    parsed = imp.parse_source_timestamp(iso)
    assert parsed is not None
    return parsed[1]


class ImportLsegBarsTest(unittest.TestCase):
    def _write(self, path: Path, rows: list[dict[str, str]]) -> None:
        headers = list(rows[0])
        lines = [",".join(headers)]
        for row in rows:
            lines.append(",".join(row[name] for name in headers))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_timestamp_offset_is_utc_and_naive_stays_utc(self) -> None:
        spaced = imp.parse_source_timestamp("2026-11-10 13:30:00")
        tee = imp.parse_source_timestamp("2026-11-10T13:30:00")
        offset = imp.parse_source_timestamp("2026-11-10T08:30:00-05:00")
        assert spaced is not None and tee is not None and offset is not None
        self.assertEqual(spaced[0], "2026-11-10T13:30:00Z")
        self.assertEqual(tee[0], "2026-11-10T13:30:00Z")
        self.assertNotEqual(tee[0], "2026-11-10T00:00:00Z")
        self.assertEqual(offset[0], "2026-11-10T13:30:00Z")
        self.assertEqual(spaced[1], tee[1])
        self.assertEqual(spaced[1], offset[1])
        self.assertEqual(MAX_CROSS_ASSET_AGE_MS, 30 * 60 * 1000)

    def test_import_is_idempotent_and_keeps_unrelated_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "lseg_macro.db"
            conn = sqlite3.connect(db)
            imp.ensure_bar_schema(conn)
            conn.execute(
                """
                INSERT INTO cross_asset_bars (
                    ric, interval, utc_ts, utc_ms, open, high, low, close, volume,
                    bid, ask, spread_bps, bid_nummov, ask_nummov, quote_imbalance, vwap
                ) VALUES ('KEEP', '1d', '2020-01-01T00:00:00Z', 1, 1, 1, 1, 1, NULL,
                          NULL, NULL, NULL, NULL, NULL, NULL, NULL)
                """
            )
            conn.commit()
            conn.close()
            csv_path = root / "lseg_cityu_incremental_bars.csv"
            self._write(
                csv_path,
                [
                    {
                        "Timestamp": "2026-11-10 13:30:00",
                        "ric": "NVDA.O",
                        "interval": "5min",
                        "TRDPRC_1": "100",
                        "BID": "99.5",
                        "ASK": "100.5",
                    },
                    {
                        "Timestamp": "2026-11-10 13:30:00",
                        "ric": "NVDA.O",
                        "interval": "5min",
                        "TRDPRC_1": "101",
                        "BID": "100",
                        "ASK": "102",
                    },
                ],
            )
            first = imp.import_lseg_bars(db, files=[csv_path], now_ms=_ms("2026-11-10 15:00:00"))
            self.assertTrue(first.ok, first.message)
            self.assertEqual(first.upserted, 1)
            second = imp.import_lseg_bars(db, files=[csv_path], now_ms=_ms("2026-11-10 15:00:00"))
            self.assertTrue(second.ok, second.message)
            conn = sqlite3.connect(db)
            count = conn.execute("SELECT COUNT(*) FROM cross_asset_bars").fetchone()[0]
            close = conn.execute(
                "SELECT close, utc_ts FROM cross_asset_bars WHERE ric='NVDA.O'"
            ).fetchone()
            keep = conn.execute("SELECT close FROM cross_asset_bars WHERE ric='KEEP'").fetchone()
            conn.close()
            self.assertEqual(count, 2)
            self.assertEqual(close[0], 101)
            self.assertEqual(close[1], "2026-11-10T13:30:00Z")
            self.assertEqual(keep[0], 1)

    def test_failed_upsert_rolls_back_existing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "lseg_macro.db"
            conn = sqlite3.connect(db)
            imp.ensure_bar_schema(conn)
            conn.execute(
                """
                INSERT INTO cross_asset_bars (
                    ric, interval, utc_ts, utc_ms, open, high, low, close, volume,
                    bid, ask, spread_bps, bid_nummov, ask_nummov, quote_imbalance, vwap
                ) VALUES ('KEEP', '1d', '2020-01-01T00:00:00Z', 1, 1, 1, 1, 7, NULL,
                          NULL, NULL, NULL, NULL, NULL, NULL, NULL)
                """
            )
            conn.commit()
            conn.close()
            csv_path = root / "lseg_cityu_core_refresh_bars.csv"
            self._write(
                csv_path,
                [{
                    "Timestamp": "2026-11-10 13:35:00",
                    "ric": "SMH.O",
                    "interval": "5min",
                    "TRDPRC_1": "50",
                }],
            )

            def boom(conn, rows):
                del conn, rows
                raise RuntimeError("disk full")

            original = imp._upsert_bars
            imp._upsert_bars = boom
            try:
                result = imp.import_lseg_bars(db, files=[csv_path])
            finally:
                imp._upsert_bars = original
            self.assertFalse(result.ok)
            self.assertEqual(result.exit_code, 1)
            self.assertIn("rolled back", result.message)
            conn = sqlite3.connect(db)
            rows = conn.execute("SELECT ric, close FROM cross_asset_bars ORDER BY ric").fetchall()
            conn.close()
            self.assertEqual(rows, [("KEEP", 7)])

    def test_refuses_institutional_db_and_slow_exports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inst = root / "cityu_institutional.db"
            sqlite3.connect(inst).close()
            csv_path = root / "lseg_cityu_incremental_bars.csv"
            self._write(
                csv_path,
                [{
                    "Timestamp": "2026-11-10 00:00:00",
                    "ric": "BTC=",
                    "interval": "1d",
                    "TRDPRC_1": "1",
                }],
            )
            refused = imp.import_lseg_bars(inst, files=[csv_path])
            self.assertEqual(refused.exit_code, 2)
            marked = root / "looks_like_lseg.db"
            conn = sqlite3.connect(marked)
            conn.execute("CREATE TABLE orbis_bank_liquidity_risk (id TEXT)")
            conn.commit()
            conn.close()
            marked_result = imp.import_lseg_bars(marked, files=[csv_path])
            self.assertEqual(marked_result.exit_code, 2)
            conn = sqlite3.connect(marked)
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            conn.close()
            self.assertNotIn("cross_asset_bars", tables)

            slow = root / "lseg_cityu_etf_flows_nav.csv"
            slow.write_text("TR.FundNetFlows,TR.NETASSETVAL\n1,2\n", encoding="utf-8")
            crowding = root / "lseg_cityu_starmine_crowding.csv"
            crowding.write_text("TR.StarMineShortSqueeze\n80\n", encoding="utf-8")
            report = imp.import_lseg_bars(root / "lseg_macro.db", input_dir=root, files=[csv_path])
            self.assertTrue(report.ok, report.message)
            refused_names = {item["file"] for item in report.refused}
            self.assertIn("lseg_cityu_etf_flows_nav.csv", refused_names)
            self.assertIn("lseg_cityu_starmine_crowding.csv", refused_names)
            self.assertTrue(all(item["safe_to_import"] is False for item in report.refused))
            conn = sqlite3.connect(root / "lseg_macro.db")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            conn.close()
            self.assertEqual(tables, {"cross_asset_bars"})

            only_slow = imp.import_lseg_bars(root / "other.db", files=[slow])
            self.assertEqual(only_slow.exit_code, 2)
            self.assertFalse((root / "other.db").exists())

    def test_session_freshness_and_slow_cycles_do_not_use_thirty_minutes(self) -> None:
        tuesday_morning = _ms("2026-11-10 06:00:00")
        monday_close = _ms("2026-11-09 19:55:00")
        friday_close = _ms("2026-11-06 19:55:00")
        self.assertEqual(
            imp.bar_freshness("NVDA.O", "5min", monday_close, tuesday_morning),
            "closed_session",
        )
        self.assertEqual(
            imp.bar_freshness("NVDA.O", "5min", friday_close, tuesday_morning),
            "missed_session",
        )
        us_open = _ms("2026-11-10 15:00:00")
        recent = _ms("2026-11-10 14:50:00")
        lagged = _ms("2026-11-10 14:00:00")
        self.assertEqual(imp.bar_freshness("NVDA.O", "5min", recent, us_open), "fresh")
        self.assertEqual(imp.bar_freshness("NVDA.O", "5min", lagged, us_open), "stale_in_session")
        btc_recent = us_open - 5 * 60 * 1000
        btc_old = us_open - 20 * 60 * 1000
        self.assertEqual(imp.bar_freshness("BTC=", "5min", btc_recent, us_open), "fresh")
        self.assertEqual(imp.bar_freshness("BTC=", "5min", btc_old, us_open), "stale_in_session")
        self.assertEqual(
            imp.slow_variable_cycle("cftc", date(2026, 11, 3), date(2026, 11, 10)),
            "current_cycle",
        )
        self.assertEqual(
            imp.slow_variable_cycle("short_interest", date(2026, 11, 9), date(2026, 11, 10)),
            "inside_slow_cycle",
        )
        self.assertFalse(imp.uses_thirty_minute_rule("cftc"))
        self.assertFalse(imp.uses_thirty_minute_rule("short_interest"))
        contract = imp.contract_for_filename("lseg_cityu_starmine_crowding.csv")
        assert contract is not None
        self.assertIn("short_interest_pct", contract["blocked_because"])
        etf = imp.contract_for_filename("lseg_cityu_etf_flows_nav.csv")
        assert etf is not None
        self.assertIn("NAV", str(etf["blocked_because"]))

    def test_update_keeps_columns_the_csv_did_not_carry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "lseg_macro.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE cross_asset_bars (
                    ric TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    utc_ts TEXT NOT NULL,
                    utc_ms INTEGER NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume REAL,
                    bid REAL,
                    open_interest REAL,
                    block_volume REAL,
                    PRIMARY KEY (ric, interval, utc_ms)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO cross_asset_bars (
                    ric, interval, utc_ts, utc_ms, open, high, low, close, volume,
                    bid, open_interest, block_volume
                ) VALUES ('NVDA.O', '5min', '2026-11-10T13:30:00Z', ?, 1, 1, 1, 1, 9, 3.5, 80, 12)
                """,
                (imp.parse_source_timestamp("2026-11-10T13:30:00")[1],),
            )
            conn.execute(
                """
                INSERT INTO cross_asset_bars (
                    ric, interval, utc_ts, utc_ms, open, high, low, close, volume,
                    bid, open_interest, block_volume
                ) VALUES ('KEEP', '1d', '2020-01-01T00:00:00Z', 1, 1, 1, 1, 7, NULL, 4, 5, 6)
                """
            )
            conn.commit()
            conn.close()
            csv_path = root / "lseg_cityu_core_refresh_bars.csv"
            self._write(
                csv_path,
                [{
                    "Timestamp": "2026-11-10T13:30:00",
                    "ric": "NVDA.O",
                    "interval": "5min",
                    "TRDPRC_1": "101",
                }],
            )
            result = imp.import_lseg_bars(db, files=[csv_path])
            self.assertTrue(result.ok, result.message)
            conn = sqlite3.connect(db)
            nvda = conn.execute(
                "SELECT close, bid, open_interest, block_volume FROM cross_asset_bars WHERE ric='NVDA.O'"
            ).fetchone()
            keep = conn.execute(
                "SELECT close, bid, open_interest, block_volume FROM cross_asset_bars WHERE ric='KEEP'"
            ).fetchone()
            conn.close()
            self.assertEqual(nvda, (101, 3.5, 80, 12))
            self.assertEqual(keep, (7, 4, 5, 6))

    def test_old_bar_table_without_bid_column_keeps_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "lseg_macro.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE cross_asset_bars (
                    ric TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    utc_ts TEXT NOT NULL,
                    utc_ms INTEGER NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL NOT NULL,
                    volume REAL,
                    yield_val REAL,
                    desc TEXT,
                    PRIMARY KEY (ric, interval, utc_ms)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO cross_asset_bars (
                    ric, interval, utc_ts, utc_ms, open, high, low, close, volume, yield_val, desc
                ) VALUES ('KEEP', '1d', '2020-01-01T00:00:00Z', 1, 1, 2, 0.5, 1.5, 8, 4.25, 'note')
                """
            )
            conn.commit()
            conn.close()
            csv_path = root / "lseg_cityu_incremental_bars.csv"
            self._write(
                csv_path,
                [{
                    "Timestamp": "2026-11-10 13:30:00",
                    "ric": "SMH.O",
                    "interval": "5min",
                    "TRDPRC_1": "50",
                }],
            )
            result = imp.import_lseg_bars(db, files=[csv_path])
            self.assertTrue(result.ok, result.message)
            conn = sqlite3.connect(db)
            keep = conn.execute(
                "SELECT close, yield_val, desc FROM cross_asset_bars WHERE ric='KEEP'"
            ).fetchone()
            smh = conn.execute(
                "SELECT close FROM cross_asset_bars WHERE ric='SMH.O'"
            ).fetchone()
            cols = {row[1] for row in conn.execute("PRAGMA table_info(cross_asset_bars)")}
            conn.close()
            self.assertEqual(keep, (1.5, 4.25, "note"))
            self.assertEqual(smh[0], 50)
            self.assertIn("bid", cols)
            self.assertIn("yield_val", cols)

    def test_module_does_not_log_in_or_reload_the_institutional_library(self) -> None:
        text = Path(imp.__file__).read_text(encoding="utf-8")
        for banned in ("import refinitiv", "rd.open_session", "/Volumes/NO NAME", "wrds.Connection", "DROP TABLE"):
            self.assertNotIn(banned, text)
        self.assertIn("lseg_cityu_core_refresh_bars.csv", text)
        self.assertIn("lseg_cityu_incremental_bars.csv", text)


if __name__ == "__main__":
    unittest.main()
