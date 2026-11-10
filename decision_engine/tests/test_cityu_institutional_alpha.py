"""Unit tests for CityU Institutional Multi-Asset Alpha, CFTC COT, NVDA/SMH AI Lead-Lag & V2 Cross-Asset Gates."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from decision_engine.core.cityu_institutional_alpha import (
    _cftc_cycle,
    attach_cityu_institutional_context,
    cftc_publication_utc,
    compute_cot_regime_from_series,
    due_cftc_asof,
    load_cityu_institutional_snapshot,
)
from scripts.import_lseg_bars_incremental import slow_variable_cycle
from decision_engine.core.macro_circuit_breaker import evaluate_macro_gate
from decision_engine.core.perp_microstructure_and_beta_hedge import (
    evaluate_perp_microstructure,
)


class TestCityUInstitutionalAlpha(unittest.TestCase):
    def test_cot_regime_identifies_institutional_squeeze_and_crowded_longs(self) -> None:
        # 1) Asset managers accumulating (+12% -> +18% of OI) while leveraged funds deeply short (-8% -> -16% of OI)
        lev_series = [-10.0, -9.5, -10.2, -11.0, -9.8, -10.5, -11.2, -10.1, -9.9, -11.5, -12.0, -16.5]
        am_series = [10.0, 10.5, 11.0, 10.2, 10.8, 11.1, 10.4, 10.9, 11.2, 11.5, 12.0, 16.0]
        lev_z, am_z, regime, mult = compute_cot_regime_from_series(lev_series, am_series)
        assert lev_z is not None and am_z is not None
        self.assertLess(lev_z, -0.5)
        self.assertGreater(am_z, 0.5)
        self.assertEqual(regime, "cot_institutional_squeeze_favorable")
        self.assertAlmostEqual(mult, 1.12, places=3)

        # 2) Leveraged hedge funds overcrowded long
        lev_crowded = [2.0, 2.5, 1.8, 2.2, 2.1, 1.9, 2.3, 2.0, 2.4, 2.1, 2.2, 9.5]
        am_neutral = [5.0, 5.1, 4.9, 5.0, 5.2, 4.8, 5.1, 5.0, 4.9, 5.1, 5.0, 5.1]
        _, _, regime2, mult2 = compute_cot_regime_from_series(lev_crowded, am_neutral)
        self.assertEqual(regime2, "cot_leveraged_long_crowded")
        self.assertAlmostEqual(mult2, 0.82, places=3)

    def test_nvda_smh_ai_semiconductor_lead_lag_gate_and_boost(self) -> None:
        # 1) NVDA/SMH 1h dump <= -2.0% blocks AI token dip-buying
        verdict_dump = evaluate_perp_microstructure(
            {
                "symbol": "FETUSDT",
                "funding_rate": 0.0001,
                "nvda_1h_ret_pct": -2.45,
                "smh_1h_ret_pct": -1.80,
            }
        )
        self.assertFalse(verdict_dump.allow)
        self.assertEqual(verdict_dump.reject_reason, "ai_equity_nvda_smh_dump")

        # 2) NVDA/SMH 1h breakout >= +1.20% boosts conviction by 1.15x
        verdict_boost = evaluate_perp_microstructure(
            {
                "symbol": "TAOUSDT",
                "funding_rate": 0.00005,
                "nvda_1h_ret_pct": 1.65,
                "smh_1h_ret_pct": 1.35,
            }
        )
        self.assertTrue(verdict_boost.allow)
        self.assertEqual(verdict_boost.perp_regime, "cityu_institutional_bullish_lead")
        self.assertAlmostEqual(verdict_boost.size_readjustment_multiplier, 1.15, places=3)

    def test_cityu_v2_cross_asset_macro_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "lseg_macro.db"
            conn = sqlite3.connect(str(db_path))
            conn.executescript(
                """
                CREATE TABLE macro_events (
                    country TEXT, indicator_name TEXT, relevance INTEGER,
                    utc_ts TEXT, utc_ms INTEGER, local_time_hkt TEXT
                );
                CREATE TABLE central_bank_events (
                    country TEXT, event_name TEXT, is_high_impact INTEGER,
                    utc_ts TEXT, utc_ms INTEGER, local_time_hkt TEXT
                );
                CREATE TABLE cross_asset_bars (
                    ric TEXT, interval TEXT, utc_ts TEXT, utc_ms INTEGER,
                    close REAL, spread_bps REAL, quote_imbalance REAL
                );
                """
            )
            now_ms = 1_762_000_000_000
            # Insert BTCc1 = 80000 and BTCc2 = 79800 (-25 bps backwardation < -15 bps floor)
            conn.execute(
                "INSERT INTO cross_asset_bars VALUES ('BTCc1', '5min', '2026-11-01T12:00:00Z', ?, 80000.0, 5.0, 0.0)",
                (now_ms,),
            )
            conn.execute(
                "INSERT INTO cross_asset_bars VALUES ('BTCc2', '5min', '2026-11-01T12:00:00Z', ?, 79800.0, 5.0, 0.0)",
                (now_ms,),
            )
            conn.commit()
            conn.close()

            status = evaluate_macro_gate(
                db_path,
                now_ms=now_ms,
                environ={"DECISION_MACRO_CITYU_V2_GATE": "1"},
            )
            self.assertEqual(status.halt_reason, "cross_asset_cme_term_backwardation")

    def test_attach_cityu_institutional_context_with_temp_dbs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inst_db = Path(tmp) / "cityu_institutional.db"
            lseg_db = Path(tmp) / "lseg_macro.db"
            c1 = sqlite3.connect(str(inst_db))
            c1.execute(
                "CREATE TABLE lseg_cftc_cot_positioning (date TEXT, symbol TEXT, open_interest TEXT, lev_money_net TEXT, asset_mgr_net TEXT)"
            )
            now_ms = 1_762_000_000_000
            # Newest print is the Tuesday whose Friday 15:30 New York release
            # is already past this clock. The date-only importer is not used.
            end = due_cftc_asof(datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc))
            for i in range(15):
                day = end - timedelta(days=14 - i)
                c1.execute(
                    "INSERT INTO lseg_cftc_cot_positioning VALUES (?, 'BTC', '20000', ?, ?)",
                    (day.isoformat(), str(-2000 - i * 200), str(2000 + i * 200)),
                )
            c1.commit()
            c1.close()

            c2 = sqlite3.connect(str(lseg_db))
            c2.execute(
                "CREATE TABLE cross_asset_bars (ric TEXT, interval TEXT, utc_ms INTEGER, close REAL)"
            )
            for idx in range(13):
                px = 200.0 * (1.015 if idx == 0 else 1.0)
                c2.execute(
                    "INSERT INTO cross_asset_bars VALUES ('NVDA.O', '5min', ?, ?)",
                    (now_ms - idx * 300_000, px),
                )
            c2.commit()
            c2.close()

            snap = load_cityu_institutional_snapshot(inst_db, lseg_db, now_ms=now_ms)
            self.assertEqual(snap.cot_regime, "cot_institutional_squeeze_favorable")
            self.assertAlmostEqual(snap.nvda_1h_ret_pct or 0.0, 1.5, places=2)

            rows = [{"binance_symbol": "FETUSDT"}, {"binance_symbol": "BTCUSDT"}]
            attach_cityu_institutional_context(rows, inst_db_path=inst_db, lseg_db_path=lseg_db, now_ms=now_ms)
            self.assertEqual(rows[0]["cot_regime"], "cot_institutional_squeeze_favorable")
            self.assertAlmostEqual(rows[0]["nvda_1h_ret_pct"], 1.5, places=2)

    def test_cftc_cycle_matches_the_importer_contract(self) -> None:
        samples = (
            date(2026, 11, 10),
            date(2026, 11, 6),
            date(2026, 11, 7),
            date(2025, 11, 1),
        )
        for asof in samples:
            for source in (date(2020, 3, 24), date(2026, 11, 3), asof, asof - timedelta(days=7)):
                self.assertEqual(
                    _cftc_cycle(source, asof),
                    slow_variable_cycle("cftc", source, asof),
                )
        # Date-only Friday 00:00 UTC already names 2026-11-03. The reader
        # waits until 15:30 America/New_York. There is no holiday calendar,
        # so a Thursday file does not move that instant earlier.
        friday = datetime(2026, 11, 6, 15, 29, tzinfo=ZoneInfo("America/New_York"))
        released = datetime(2026, 11, 6, 15, 30, tzinfo=ZoneInfo("America/New_York"))
        self.assertEqual(due_cftc_asof(friday), date(2026, 10, 27))
        self.assertEqual(due_cftc_asof(released), date(2026, 11, 3))
        self.assertEqual(cftc_publication_utc(date(2026, 11, 3)), released.astimezone(timezone.utc))
        self.assertEqual(
            slow_variable_cycle("cftc", date(2026, 11, 3), date(2026, 11, 6)),
            "current_cycle",
        )
        thursday = datetime(2026, 11, 5, 18, 0, tzinfo=ZoneInfo("America/New_York"))
        self.assertEqual(due_cftc_asof(thursday), date(2026, 10, 27))

    def test_2020_and_prior_week_do_not_stamp_the_current_regime(self) -> None:
        now_ms = 1_762_000_000_000
        with tempfile.TemporaryDirectory() as tmp:
            inst_db = Path(tmp) / "cityu_institutional.db"
            conn = sqlite3.connect(inst_db)
            conn.execute(
                "CREATE TABLE lseg_cftc_cot_positioning (date TEXT, symbol TEXT, open_interest TEXT, lev_money_net TEXT, asset_mgr_net TEXT)"
            )
            # A 2020 series that would be crowded if the date were ignored.
            for i, lev in enumerate([2.0, 2.5, 1.8, 2.2, 2.1, 1.9, 2.3, 2.0, 2.4, 2.1, 2.2, 9.5]):
                day = date(2020, 1, 7) + timedelta(days=7 * i)
                conn.execute(
                    "INSERT INTO lseg_cftc_cot_positioning VALUES (?, 'BTC', '10000', ?, '0')",
                    (day.isoformat(), str(lev * 100)),
                )
            conn.commit()
            old = load_cityu_institutional_snapshot(inst_db, None, now_ms=now_ms)
            self.assertEqual(old.cot_regime, "neutral")
            self.assertIsNone(old.btc_cot_lev_z)
            conn.execute("DELETE FROM lseg_cftc_cot_positioning")
            prior = date(2025, 10, 21)
            for i, lev in enumerate([2.0, 2.5, 1.8, 2.2, 2.1, 1.9, 2.3, 2.0, 2.4, 2.1, 2.2, 9.5]):
                day = prior - timedelta(days=7 * (11 - i))
                conn.execute(
                    "INSERT INTO lseg_cftc_cot_positioning VALUES (?, 'BTC', '10000', ?, '0')",
                    (day.isoformat(), str(lev * 100)),
                )
            conn.commit()
            conn.close()
            stale_week = load_cityu_institutional_snapshot(inst_db, None, now_ms=now_ms)
            self.assertEqual(stale_week.cot_regime, "neutral")
            self.assertIsNone(stale_week.btc_cot_lev_z)


if __name__ == "__main__":
    unittest.main()
