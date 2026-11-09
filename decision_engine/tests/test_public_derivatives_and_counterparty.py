"""Unit tests for Milestone 21: Public Derivatives, Whale-vs-Retail Divergence, Deribit Options Gamma Wall & Fed Net Liquidity."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from decision_engine.core.perp_microstructure_and_beta_hedge import (
    evaluate_perp_microstructure,
)
from scripts.fetch_public_derivatives_and_counterparty import (
    compute_deribit_option_walls_and_max_pain,
    compute_fed_net_liquidity,
    compute_whale_retail_divergence,
    ensure_counterparty_schema,
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

    def test_deribit_option_walls_max_pain_and_negative_gamma_gate(self) -> None:
        book_summaries = [
            {"instrument_name": "BTC-27DEC26-85000-P", "open_interest": 4500.0},
            {"instrument_name": "BTC-27DEC26-90000-P", "open_interest": 1200.0},
            {"instrument_name": "BTC-27DEC26-90000-C", "open_interest": 1500.0},
            {"instrument_name": "BTC-27DEC26-100000-C", "open_interest": 6000.0},
        ]
        dvol_series = [48.0] * 24 + [65.0]

        # Spot = 92,000 inside [Put Wall 85k, Call Wall 100k]
        stats_pinned = compute_deribit_option_walls_and_max_pain(
            book_summaries,
            spot_price=92000.0,
            dvol_series=[50.0] * 25,
        )
        self.assertEqual(stats_pinned["top_put_wall_strike"], 85000.0)
        self.assertEqual(stats_pinned["top_call_wall_strike"], 100000.0)
        self.assertEqual(stats_pinned["max_pain_strike"], 90000.0)
        self.assertEqual(stats_pinned["gamma_regime"], "positive_gamma_pinned")

        # Spot = 82,000 below Put Wall 85k with DVOL spike -> negative_gamma_slippery
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

    def test_fed_net_liquidity_and_counterparty_sqlite_schema(self) -> None:
        # WALCL = 6,800,000 ($M) = $6,800B; TGA = 800,000 ($M) = $800B; ON_RRP = $200B -> Net Liq = $5,800B
        net_liq = compute_fed_net_liquidity(6_800_000.0, 800_000.0, 200.0)
        self.assertAlmostEqual(net_liq or 0.0, 5800.0, places=3)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "public_derivatives.db"
            conn = sqlite3.connect(str(db_path))
            ensure_counterparty_schema(conn)
            tables = {
                str(r[0])
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            conn.close()

        self.assertTrue(
            {
                "binance_perp_counterparty",
                "deribit_options_gamma",
                "macro_net_liquidity",
                "stablecoin_supply",
            }.issubset(tables)
        )


if __name__ == "__main__":
    unittest.main()
