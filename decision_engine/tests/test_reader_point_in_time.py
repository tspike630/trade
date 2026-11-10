"""Point-in-time reader checks. Fixtures are synthetic, not the installed DBs."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import decision_engine  # noqa: F401
from decision_engine.core.cityu_institutional_alpha import load_cityu_institutional_snapshot
from decision_engine.core.macro_circuit_breaker import evaluate_macro_gate
from decision_engine.core.perp_microstructure_and_beta_hedge import (
    attach_counterparty_context,
)

_HOUR_MS = 3_600_000
_NOW_MS = 1_763_000_000_000


class DerivativesFreshnessTests(unittest.TestCase):
    def _db(self, root: Path) -> Path:
        path = root / "public_derivatives.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE binance_perp_counterparty (
                symbol TEXT, ts_ms INTEGER, funding_rate REAL, funding_z REAL,
                perp_basis_bps REAL, top_position_ls_ratio REAL,
                global_account_ls_ratio REAL, whale_retail_divergence REAL,
                liq_cascade_regime TEXT
            );
            CREATE TABLE cross_exchange_spot_baseline (
                symbol TEXT, ts_ms INTEGER, max_venue_dislocation_bps REAL
            );
            CREATE TABLE deribit_options_gamma (
                currency TEXT, ts_ms INTEGER, gamma_regime TEXT, dvol_zscore REAL
            );
            CREATE TABLE token_unlock_schedule (
                symbol TEXT, next_unlock_utc TEXT, hours_to_unlock REAL,
                unlock_pct_circulating REAL, in_unlock_blackout INTEGER
            );
            CREATE TABLE binance_spot_microstructure (
                symbol TEXT, ts_ms INTEGER, orderbook_imbalance_20 REAL
            );
            """
        )
        stale = _NOW_MS - 19 * _HOUR_MS
        conn.execute(
            "INSERT INTO binance_perp_counterparty VALUES ('ETHUSDT', ?, 0.001, 3.0, 10, 1, 1, 1, 'long_liquidation_waterfall')",
            (stale,),
        )
        conn.execute(
            "INSERT INTO cross_exchange_spot_baseline VALUES ('ETHUSDT', ?, 80.0)",
            (stale,),
        )
        conn.execute(
            "INSERT INTO deribit_options_gamma VALUES ('BTC', ?, 'negative_gamma_slippery', 3.0)",
            (stale,),
        )
        conn.execute(
            """
            INSERT INTO token_unlock_schedule VALUES (
                'SUIUSDT', '2026-12-01T00:00:00Z', 517.26, 5.0, 1
            )
            """
        )
        conn.execute(
            """
            INSERT INTO token_unlock_schedule VALUES (
                'SOONUSDT', ?, 10.0, 3.0, 0
            )
            """,
            (self._iso(_NOW_MS + 10 * _HOUR_MS),),
        )
        conn.execute(
            "INSERT INTO binance_spot_microstructure VALUES ('ETHUSDT', ?, -0.90)",
            (_NOW_MS,),
        )
        conn.commit()
        conn.close()
        return path

    @staticmethod
    def _iso(ms: int) -> str:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    def test_stale_snapshot_is_not_stamped_and_unlock_is_recomputed(self) -> None:
        old = os.environ.get("BINANCE_INDICATORS_FRESH_SEC")
        os.environ["BINANCE_INDICATORS_FRESH_SEC"] = "3600"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db = self._db(Path(tmp))
                rows = [{"binance_symbol": "ETHUSDT"}, {"binance_symbol": "SUIUSDT"}, {"binance_symbol": "SOONUSDT"}]
                attach_counterparty_context(rows, db_path=db, now_ms=_NOW_MS)
        finally:
            if old is None:
                os.environ.pop("BINANCE_INDICATORS_FRESH_SEC", None)
            else:
                os.environ["BINANCE_INDICATORS_FRESH_SEC"] = old
        eth, sui, soon = rows
        self.assertIsNone(eth.get("funding_rate"))
        self.assertIsNone(eth.get("funding_z"))
        self.assertIsNone(eth.get("liq_cascade_regime"))
        self.assertIsNone(eth.get("max_venue_dislocation_bps"))
        self.assertIsNone(eth.get("gamma_regime"))
        self.assertIsNone(eth.get("dvol_zscore"))
        self.assertNotIn("obi_top5", eth)
        self.assertNotIn("orderbook_imbalance_20", eth)
        self.assertNotIn("volume_z", eth)
        self.assertEqual(sui.get("in_unlock_blackout"), 0)
        self.assertEqual(soon.get("in_unlock_blackout"), 1)

    def test_fresh_perp_row_is_still_stamped(self) -> None:
        old = os.environ.get("BINANCE_INDICATORS_FRESH_SEC")
        os.environ["BINANCE_INDICATORS_FRESH_SEC"] = "3600"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "p.db"
                conn = sqlite3.connect(path)
                conn.execute(
                    """
                    CREATE TABLE binance_perp_counterparty (
                        symbol TEXT, ts_ms INTEGER, funding_rate REAL, funding_z REAL,
                        perp_basis_bps REAL, top_position_ls_ratio REAL,
                        global_account_ls_ratio REAL, whale_retail_divergence REAL,
                        liq_cascade_regime TEXT
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO binance_perp_counterparty VALUES ('ETHUSDT', ?, 0.0002, 0.4, 1, 1, 1, 1, NULL)",
                    (_NOW_MS - 10 * 60 * 1000,),
                )
                conn.commit()
                conn.close()
                rows = [{"binance_symbol": "ETHUSDT"}]
                attach_counterparty_context(rows, db_path=path, now_ms=_NOW_MS)
        finally:
            if old is None:
                os.environ.pop("BINANCE_INDICATORS_FRESH_SEC", None)
            else:
                os.environ["BINANCE_INDICATORS_FRESH_SEC"] = old
        self.assertAlmostEqual(rows[0]["funding_rate"], 0.0002)


def _macro_db(path: Path, *, utc_ms: int, actual: float) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE central_bank_events (
            country TEXT, event_name TEXT, is_high_impact INTEGER,
            utc_ts TEXT, utc_ms INTEGER, local_time_hkt TEXT
        );
        CREATE TABLE macro_events (
            country TEXT, indicator_name TEXT, relevance INTEGER,
            local_time_hkt TEXT, utc_ts TEXT, utc_ms INTEGER,
            reuters_poll REAL, actual REAL, surprise REAL,
            predicted_surprise REAL, min_forecast REAL, max_forecast REAL
        );
        CREATE TABLE cross_asset_bars (
            ric TEXT, interval TEXT, utc_ts TEXT, utc_ms INTEGER, close REAL
        );
        """
    )
    conn.execute(
        """
        INSERT INTO macro_events (
            country, indicator_name, relevance, local_time_hkt,
            utc_ts, utc_ms, reuters_poll, actual, surprise,
            predicted_surprise, min_forecast, max_forecast
        ) VALUES (
            'United States', 'sample', 3, '13:00',
            'stamp', ?, 100.0, ?, ?,
            NULL, 90.0, 110.0
        )
        """,
        (utc_ms, actual, actual - 100.0),
    )
    conn.commit()
    conn.close()


class MacroFutureActualTests(unittest.TestCase):
    def test_future_actual_is_calendar_risk_not_a_released_shock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            future_db = root / "future.db"
            past_db = root / "past.db"
            _macro_db(future_db, utc_ms=_NOW_MS + 10 * 60 * 1000, actual=900.0)
            _macro_db(past_db, utc_ms=_NOW_MS - 5 * 60 * 1000, actual=140.0)
            env = {"DECISION_MACRO_SURPRISE_DYNAMIC": "1"}
            future = evaluate_macro_gate(future_db, now_ms=_NOW_MS, environ=env)
            past = evaluate_macro_gate(past_db, now_ms=_NOW_MS, environ=env)
        self.assertEqual(future.halt_reason, "macro_rel3_blackout")
        self.assertEqual(past.halt_reason, "macro_surprise_shock_blackout")


class CityUBarAgeTests(unittest.TestCase):
    def test_stale_cross_asset_bar_is_not_a_current_nvda_return(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lseg = Path(tmp) / "lseg.db"
            conn = sqlite3.connect(lseg)
            conn.execute(
                "CREATE TABLE cross_asset_bars (ric TEXT, interval TEXT, utc_ms INTEGER, close REAL)"
            )
            stale = _NOW_MS - 27 * _HOUR_MS
            for idx in range(13):
                conn.execute(
                    "INSERT INTO cross_asset_bars VALUES ('NVDA.O', '5min', ?, ?)",
                    (stale - idx * 300_000, 100.0 if idx else 80.0),
                )
            conn.commit()
            conn.close()
            snap = load_cityu_institutional_snapshot(None, lseg, now_ms=_NOW_MS)
        self.assertIsNone(snap.nvda_1h_ret_pct)


if __name__ == "__main__":
    unittest.main()
