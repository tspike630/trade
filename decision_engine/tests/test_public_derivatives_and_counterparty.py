"""Unit tests for Milestone 21: Categories 6, 7, 8, and 9 Public Derivatives, Spot L2/aggTrades, Cross-Exchange Baseline, Deribit Options Gamma Wall, Token Cliff Unlocks & Fed Net Liquidity."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
import tempfile
import unittest
from pathlib import Path

from decision_engine.core.perp_microstructure_and_beta_hedge import (
    evaluate_perp_microstructure,
)
from scripts.fetch_public_derivatives_and_counterparty import (
    compute_cross_exchange_dislocation,
    compute_deribit_option_walls_and_max_pain,
    compute_fed_net_liquidity,
    compute_spot_orderbook_and_aggtrades_metrics,
    compute_whale_retail_divergence,
    ensure_counterparty_schema,
    estimate_liquidation_cascade,
    evaluate_token_unlock_window,
)


class TestPublicDerivativesAndCounterparty(unittest.TestCase):
    def test_whale_vs_retail_divergence_identifies_retail_long_trap_and_whale_accumulation(self) -> None:
        # 1) Retail overwhelmingly long (2.5) while whales are net short (0.85) -> retail_long_trap_cascade
        div, regime = compute_whale_retail_divergence(0.85, 2.50, funding_rate=0.0002)
        assert div is not None
        self.assertAlmostEqual(div, 0.34, places=3)
        self.assertEqual(regime, "retail_long_trap_cascade")

        verdict_trap = evaluate_perp_microstructure(
            {
                "symbol": "SOLUSDT",
                "funding_rate": 0.0001,
                "top_position_ls_ratio": 0.85,
                "global_account_ls_ratio": 2.50,
            }
        )
        self.assertFalse(verdict_trap.allow)
        self.assertEqual(verdict_trap.reject_reason, "perp_retail_long_trap")
        self.assertEqual(verdict_trap.perp_regime, "retail_long_trap_cascade")

        # 2) Whales accumulating (top=1.80, retail=1.10) with negative funding -> whale_accumulation_favorable
        div2, regime2 = compute_whale_retail_divergence(1.80, 1.10, funding_rate=-0.0001)
        assert div2 is not None
        self.assertGreater(div2, 1.35)
        self.assertEqual(regime2, "whale_accumulation_favorable")

        verdict_acc = evaluate_perp_microstructure(
            {
                "symbol": "SOLUSDT",
                "funding_rate": -0.0001,
                "top_position_ls_ratio": 1.80,
                "global_account_ls_ratio": 1.10,
            }
        )
        self.assertTrue(verdict_acc.allow)
        self.assertEqual(verdict_acc.perp_regime, "whale_accumulation_favorable")
        self.assertAlmostEqual(verdict_acc.size_readjustment_multiplier, 1.15, places=4)

    def test_category_6_liquidation_cascade_estimation_and_gate(self) -> None:
        # OI drops -4.2% while taker sell pressure dominates (0.65)
        liq_usd, liq_regime = estimate_liquidation_cascade(
            oi_change_1h_pct=-4.2,
            open_interest_usd=500_000_000.0,
            perp_taker_buy_sell_ratio=0.65,
        )
        assert liq_usd is not None
        self.assertGreater(liq_usd, 0.0)
        self.assertEqual(liq_regime, "long_liquidation_waterfall")

        verdict_liq = evaluate_perp_microstructure(
            {
                "symbol": "ETHUSDT",
                "funding_rate": 0.0001,
                "liq_cascade_regime": liq_regime,
            }
        )
        self.assertFalse(verdict_liq.allow)
        self.assertEqual(verdict_liq.reject_reason, "perp_long_liquidation_waterfall")

    def test_category_7_spot_orderbook_depth_and_aggtrades_whale_sweep(self) -> None:
        depth = {
            "bids": [["100.00", "250.0"], ["99.90", "300.0"]],
            "asks": [["100.10", "80.0"], ["100.20", "120.0"]],
        }
        agg_trades = [
            {"p": "100.10", "q": "300.0", "m": False},  # $30,030 aggressive buyer whale sweep
            {"p": "100.00", "q": "50.0", "m": True},    # $5,000 small sell
        ]
        metrics = compute_spot_orderbook_and_aggtrades_metrics(
            depth,
            agg_trades,
            whale_threshold_usd=15_000.0,
        )
        self.assertGreater(metrics["orderbook_imbalance_20"] or 0.0, 0.40)
        self.assertAlmostEqual(metrics["whale_net_buy_usd"] or 0.0, 30030.0, places=1)
        self.assertEqual(metrics["aggtrade_whale_buy_ratio"], 1.0)
        self.assertIsNotNone(metrics["impact_10k_bps"])

    def test_category_8_cross_exchange_spot_dislocation_and_deribit_gamma_wall(self) -> None:
        # 8A: Cross-exchange flash wick manipulation (>= 35 bps dislocation)
        diff_bps, regime = compute_cross_exchange_dislocation(
            binance_px=100.0,
            okx_px=100.45,
            bybit_px=100.42,
        )
        assert diff_bps is not None
        self.assertGreater(diff_bps, 35.0)
        self.assertEqual(regime, "cross_exchange_wick_divergence")

        verdict_disloc = evaluate_perp_microstructure(
            {
                "symbol": "SOLUSDT",
                "funding_rate": 0.0001,
                "max_venue_dislocation_bps": diff_bps,
            }
        )
        self.assertFalse(verdict_disloc.allow)
        self.assertEqual(verdict_disloc.reject_reason, "cross_exchange_wick_dislocation")

        # 8B: Deribit Option Gamma Walls & Max Pain
        book_summaries = [
            {"instrument_name": "BTC-27DEC26-85000-P", "open_interest": 4500.0},
            {"instrument_name": "BTC-27DEC26-90000-P", "open_interest": 1200.0},
            {"instrument_name": "BTC-27DEC26-90000-C", "open_interest": 1500.0},
            {"instrument_name": "BTC-27DEC26-100000-C", "open_interest": 6000.0},
        ]
        dvol_series = [48.0] * 24 + [65.0]
        stats_neg = compute_deribit_option_walls_and_max_pain(
            book_summaries,
            spot_price=82000.0,
            dvol_series=dvol_series,
        )
        self.assertEqual(stats_neg["gamma_regime"], "negative_gamma_slippery")
        assert stats_neg["dvol_zscore"] is not None
        self.assertGreater(stats_neg["dvol_zscore"], 2.0)

        verdict_gamma = evaluate_perp_microstructure(
            {
                "symbol": "BTCUSDT",
                "funding_rate": 0.0001,
                "gamma_regime": stats_neg["gamma_regime"],
                "dvol_zscore": stats_neg["dvol_zscore"],
            }
        )
        self.assertFalse(verdict_gamma.allow)
        self.assertEqual(verdict_gamma.reject_reason, "options_negative_gamma_crash")

    def test_category_9_token_unlock_blackout_and_all_8_sqlite_tables(self) -> None:
        # 9A: Token unlock blackout inside [-72h, +48h] (ARBUSDT unlocks on 16th of month)
        now_dt = datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc)
        arb_row = evaluate_token_unlock_window("ARBUSDT", now_ms=int(now_dt.timestamp() * 1000))
        assert arb_row is not None
        self.assertEqual(arb_row["in_unlock_blackout"], 1)

        verdict_unlock = evaluate_perp_microstructure(
            {
                "symbol": "ARBUSDT",
                "funding_rate": 0.0001,
                "in_unlock_blackout": 1,
            }
        )
        self.assertFalse(verdict_unlock.allow)
        self.assertEqual(verdict_unlock.reject_reason, "token_unlock_cliff_blackout")

        # 9B: Fed Net Liquidity
        net_liq = compute_fed_net_liquidity(6_800_000.0, 800_000.0, 200.0)
        self.assertAlmostEqual(net_liq or 0.0, 5800.0, places=3)

        # Verify all 8 SQLite tables across Categories 6, 7, 8, and 9
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "public_derivatives.db"
            conn = sqlite3.connect(str(db_path))
            ensure_counterparty_schema(conn)
            tables = {
                str(r[0])
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            conn.close()

        expected_tables = {
            "binance_perp_counterparty",
            "binance_spot_microstructure",
            "cross_exchange_spot_baseline",
            "deribit_options_gamma",
            "token_unlock_schedule",
            "macro_net_liquidity",
            "stablecoin_supply",
            "onchain_holder_bot_summary",
        }
        self.assertTrue(expected_tables.issubset(tables))


if __name__ == "__main__":
    unittest.main()
