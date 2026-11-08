"""Tests for Milestone 19: LSEG SmartEconomics Surprise / Dealer Microstructure Gate & Funding Z-Score Pipeline."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crawler.indicators.binance_spot import ensure_schema, upsert_indicator
from decision_engine.core.binance_indicators import list_binance_hot_tokens
from decision_engine.core.macro_circuit_breaker import evaluate_macro_gate
from decision_engine.core.signal_bus import PURPOSE_LIVE_BNB_BOLL_FADE
from decision_engine.strategies.funding_z_fade import (
    FundingZFadeStrategy,
    compute_funding_z_from_series,
    evaluate_funding_z_fade,
)


def _create_macro_test_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE macro_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ric TEXT,
            local_date_hkt TEXT,
            local_time_hkt TEXT,
            utc_ts TEXT,
            utc_ms INTEGER,
            country TEXT,
            relevance INTEGER,
            indicator_name TEXT,
            period TEXT,
            reuters_poll REAL,
            actual REAL,
            surprise REAL,
            prior REAL,
            revised REAL,
            smart_economics REAL,
            predicted_surprise REAL,
            min_forecast REAL,
            max_forecast REAL,
            economist_count INTEGER
        );
        CREATE TABLE central_bank_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            local_date_hkt TEXT,
            local_time_hkt TEXT,
            utc_ts TEXT,
            utc_ms INTEGER,
            country TEXT,
            event_name TEXT,
            is_high_impact INTEGER
        );
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
            bid REAL,
            ask REAL,
            spread_bps REAL,
            bid_nummov REAL,
            ask_nummov REAL,
            quote_imbalance REAL,
            PRIMARY KEY (ric, interval, utc_ms)
        );
        """
    )
    return conn


class TestLsegMicrostructureAndFundingZGap(unittest.TestCase):
    def test_lseg_out_of_bounds_macro_shock_extends_blackout_to_90m(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "lseg_macro.db"
            conn = _create_macro_test_db(db_path)
            release_ms = 1_760_000_000_000
            # US CPI actual=3.6 beats max_forecast=3.3 (out-of-bounds shock!)
            conn.execute(
                """
                INSERT INTO macro_events (
                    ric, local_date_hkt, local_time_hkt, utc_ts, utc_ms,
                    country, relevance, indicator_name, reuters_poll, actual,
                    surprise, min_forecast, max_forecast
                ) VALUES (?, '2025-10-10', '20:30:00', '2025-10-10T12:30:00Z', ?,
                          'United States', 3, 'CPI YY', 3.1, 3.6, 0.5, 2.9, 3.3)
                """,
                ("USCPI=ECI", release_ms),
            )
            conn.commit()
            conn.close()

            # 60 minutes after release (> normal 30m window, < 90m shock window)
            now_60m = release_ms + 60 * 60_000
            status = evaluate_macro_gate(
                db_path,
                now_ms=now_60m,
                purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
                environ={"DECISION_MACRO_SURPRISE_DYNAMIC": "1"},
            )
            self.assertEqual(status.halt_reason, "macro_surprise_shock_blackout")
            self.assertIn("out_of_bounds", str(status.detail))

    def test_lseg_inline_release_fast_clears_after_5m(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "lseg_macro.db"
            conn = _create_macro_test_db(db_path)
            release_ms = 1_760_000_000_000
            # US CPI actual=3.1 matches reuters_poll=3.1 inside [2.9, 3.3]
            conn.execute(
                """
                INSERT INTO macro_events (
                    ric, local_date_hkt, local_time_hkt, utc_ts, utc_ms,
                    country, relevance, indicator_name, reuters_poll, actual,
                    surprise, min_forecast, max_forecast
                ) VALUES (?, '2025-10-10', '20:30:00', '2025-10-10T12:30:00Z', ?,
                          'United States', 3, 'CPI YY', 3.1, 3.1, 0.0, 2.9, 3.3)
                """,
                ("USCPI=ECI", release_ms),
            )
            conn.commit()
            conn.close()

            # 10 minutes after release (> 5m inline window, < 30m standard window)
            now_10m = release_ms + 10 * 60_000
            status = evaluate_macro_gate(
                db_path,
                now_ms=now_10m,
                purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
                environ={"DECISION_MACRO_SURPRISE_DYNAMIC": "1"},
            )
            self.assertIsNone(status.halt_reason)

    def test_lseg_dealer_spread_and_quote_imbalance_microstructure_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "lseg_macro.db"
            conn = _create_macro_test_db(db_path)
            bar_ms = 1_760_000_000_000
            conn.execute(
                """
                INSERT INTO cross_asset_bars (
                    ric, interval, utc_ts, utc_ms, close, spread_bps, quote_imbalance
                ) VALUES ('BTCc1', '5min', '2025-10-10T12:00:00Z', ?, 90000.0, 10.0, -0.75)
                """,
                (bar_ms,),
            )
            conn.commit()
            conn.close()

            status = evaluate_macro_gate(
                db_path,
                now_ms=bar_ms + 60_000,
                purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
                environ={"DECISION_MACRO_MICROSTRUCTURE_GATE": "1"},
            )
            self.assertEqual(status.halt_reason, "cross_asset_dealer_quote_toxicity")
            self.assertIn("BTCc1_quote_imb=-0.75", str(status.detail))

    def test_funding_z_fade_pipeline_from_binance_indicators_and_series(self) -> None:
        # 1) Compute funding_z from a historical funding rate sequence with a deep negative tail
        history = [0.0001] * 25 + [-0.0012]
        fz = compute_funding_z_from_series(history)
        assert fz is not None
        self.assertLess(fz, -2.5)

        # 2) Verify binance_indicators stores and reads funding_z & open_interest_change_1h
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ind.db"
            conn = sqlite3.connect(str(db_path))
            ensure_schema(conn)
            upsert_indicator(
                conn,
                {
                    "symbol": "SOLUSDT",
                    "base_asset": "SOL",
                    "quote_asset": "USDT",
                    "status": "TRADING",
                    "interval": "5m",
                    "price_usd": 150.0,
                    "change_5m": -2.8,
                    "quote_volume_24h": 50_000_000.0,
                    "tech_updated_at": "2026-11-09T01:00:00Z",
                    "updated_at": "2026-11-09T01:00:00Z",
                },
            )
            conn.execute(
                """
                UPDATE binance_indicators
                SET funding_rate = -0.0015, funding_z = -2.85, open_interest_change_1h = 3.2
                WHERE symbol = 'SOLUSDT'
                """
            )
            conn.commit()
            hot = list_binance_hot_tokens(conn, fresh_sec=0)
            conn.close()

        self.assertEqual(len(hot), 1)
        self.assertAlmostEqual(hot[0]["funding_z"], -2.85, places=4)
        self.assertAlmostEqual(hot[0]["open_interest_change_1h"], 3.2, places=4)

        # 3) FundingZFadeStrategy now emits a BUY instead of skipping with funding_missing!
        strat = FundingZFadeStrategy()
        signals = strat.on_tick({"hot_tokens": hot})
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["token_address"], "SOLUSDT")
        self.assertAlmostEqual(signals[0]["funding_z"], -2.85, places=4)

        # 4) Optional OI collapse filter rejects when open_interest_change_1h <= -5.0%
        hot_collapse = [dict(hot[0], open_interest_change_1h=-7.5)]
        with patch.dict(os.environ, {"FUNDING_Z_MAX_OI_DROP_1H_PCT": "5.0"}, clear=False):
            ok, reason, _ = evaluate_funding_z_fade(hot_collapse[0])
        self.assertFalse(ok)
        self.assertEqual(reason, "funding_z_oi_collapsing")


if __name__ == "__main__":
    unittest.main()
