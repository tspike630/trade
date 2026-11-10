"""Tests for Milestone 17: Multi-Horizon 1h/4h Trend & Volatility Regime Filter + Cross-Sectional Relative Strength Ranker."""

from __future__ import annotations

import os
import unittest
from typing import Any
from unittest.mock import patch

from decision_engine.core.live_bnb_turnover import cap_live_bnb_buys
from decision_engine.core.multi_horizon_and_xs_regime import (
    compute_composite_alpha_score,
    compute_cross_sectional_context,
    evaluate_multi_horizon_regime,
    stamp_cross_sectional_metrics,
)
from decision_engine.core.signal_bus import PURPOSE_LIVE_BNB_BOLL_FADE
from decision_engine.strategies.live_bnb_entry_zone import evaluate_entry_zone
from decision_engine.strategies.tech_boll_fade import TechBollFadeStrategy


def _base_entry_row(symbol: str = "SOLUSDT", **overrides: Any) -> dict[str, Any]:
    # L = 100, M = 110 -> S = 10, price = 102 -> alpha = 0.20, stop = 99
    # odds = (110 - 102) / (102 - 99) = 8 / 3 = 2.67
    row: dict[str, Any] = {
        "chain_id": "binance",
        "token_address": symbol,
        "binance_symbol": symbol,
        "primary_pair_address": f"{symbol[:-4]}/USDT",
        "kline_low": 100.0,
        "boll_lower": 101.0,
        "boll_mid": 110.0,
        "boll_upper": 119.0,
        "price_usd": 102.0,
        "trade_size_ratio": 1.6,
        "taker_buy_ratio": 0.52,
        "change_5m": -2.5,
        "change_1h": -1.2,
        "change_4h": 0.8,
        "volume_strength": 0.8,
    }
    row.update(overrides)
    return row


class TestMultiHorizonAndXsRegime(unittest.TestCase):
    def test_1h_and_4h_waterfall_gates_reject_catching_falling_knife(self) -> None:
        with patch.dict(os.environ, {"LIVE_BNB_MULTI_HORIZON_REGIME": "1"}, clear=False):
            row_1h = _base_entry_row("SOLUSDT", change_1h=-9.5, change_4h=-5.0)
            dec_1h = evaluate_entry_zone(row_1h)
            self.assertFalse(dec_1h.emit)
            self.assertEqual(dec_1h.reject_reason, "multi_horizon_1h_waterfall")
            self.assertEqual(dec_1h.horizon_regime, "waterfall_1h")

            row_4h = _base_entry_row("SOLUSDT", change_1h=-3.0, change_4h=-15.5)
            dec_4h = evaluate_entry_zone(row_4h)
            self.assertFalse(dec_4h.emit)
            self.assertEqual(dec_4h.reject_reason, "multi_horizon_4h_waterfall")
            self.assertEqual(dec_4h.horizon_regime, "waterfall_4h")

    def test_moderate_1h_downtrend_downgrades_large_trade_to_neutral_half_size(self) -> None:
        with patch.dict(os.environ, {"LIVE_BNB_MULTI_HORIZON_REGIME": "1"}, clear=False):
            row = _base_entry_row("SOLUSDT", change_1h=-6.2, change_4h=-4.0)
            dec = evaluate_entry_zone(row)
            self.assertTrue(dec.emit)
            self.assertEqual(dec.tier, "neutral")
            self.assertEqual(dec.position_size_multiplier, 0.5)
            self.assertEqual(dec.horizon_regime, "htf_downtrend_downgraded")
            assert dec.composite_alpha_score is not None
            self.assertGreater(dec.composite_alpha_score, 0.0)

    def test_bollinger_bandwidth_compression_rejects_ultra_tight_band(self) -> None:
        with patch.dict(os.environ, {"LIVE_BNB_MULTI_HORIZON_REGIME": "1"}, clear=False):
            # boll_upper=100.20, boll_lower=99.80, boll_mid=100.00 -> bw = 0.004 < 0.006
            row = _base_entry_row(
                "SOLUSDT",
                kline_low=99.0,
                boll_lower=99.80,
                boll_mid=100.00,
                boll_upper=100.20,
                price_usd=99.20,
            )
            dec = evaluate_entry_zone(row)
            self.assertFalse(dec.emit)
            self.assertEqual(dec.reject_reason, "volatility_compression_skip")
            self.assertEqual(dec.horizon_regime, "vol_compressed")

    def test_systemic_crash_breadth_halts_all_buys_in_tick(self) -> None:
        # 6 tokens all dumping > 4% in 1h -> breadth = 0.0 < 0.12, median <= -3.5%
        hot = [
            _base_entry_row("BTCUSDT", change_1h=-4.5),
            _base_entry_row("ETHUSDT", change_1h=-5.2),
            _base_entry_row("SOLUSDT", change_1h=-6.0),
            _base_entry_row("BNBUSDT", change_1h=-4.1),
            _base_entry_row("ADAUSDT", change_1h=-5.8),
            _base_entry_row("XRPUSDT", change_1h=-4.9),
        ]
        stamped, ctx = stamp_cross_sectional_metrics(hot)
        self.assertTrue(ctx.is_systemic_crash)
        self.assertEqual(ctx.breadth_1h_positive_ratio, 0.0)

        with patch.dict(os.environ, {"LIVE_BNB_MULTI_HORIZON_REGIME": "1"}, clear=False):
            strat = TechBollFadeStrategy()
            signals = strat.on_tick({"purpose": PURPOSE_LIVE_BNB_BOLL_FADE, "hot_tokens": hot})
            self.assertEqual(len(signals), 0)
            self.assertEqual(strat.rejected.get("xs_systemic_crash_breadth"), 6)

    def test_xs_extreme_laggard_bleed_rejected_while_resilient_leader_emits(self) -> None:
        hot = [
            _base_entry_row("BTCUSDT", change_1h=0.5),
            _base_entry_row("ETHUSDT", change_1h=0.2),
            _base_entry_row("BNBUSDT", change_1h=0.1),
            _base_entry_row("SOLUSDT", change_1h=-0.2),
            # Extreme idiosyncratic bleed: -6.8% when median is +0.05% -> xs_rel_1h = -6.85%
            _base_entry_row("WEAKUSDT", change_1h=-6.8),
        ]
        with patch.dict(os.environ, {"LIVE_BNB_MULTI_HORIZON_REGIME": "1"}, clear=False):
            strat = TechBollFadeStrategy()
            signals = strat.on_tick({"purpose": PURPOSE_LIVE_BNB_BOLL_FADE, "hot_tokens": hot})
            emitted_syms = [s["binance_symbol"] for s in signals]
            self.assertNotIn("WEAKUSDT", emitted_syms)
            self.assertIn("SOLUSDT", emitted_syms)
            self.assertEqual(strat.rejected.get("xs_extreme_laggard_bleed"), 1)

    def test_composite_alpha_score_ranks_top_buys_when_enabled(self) -> None:
        now_ms = 1_762_620_000_000
        asof_iso = "2025-11-08T16:35:00Z"
        # Signal A has slightly higher raw volume_strength (0.9 vs 0.8),
        # but Signal B has much higher composite_alpha_score (85.0 vs 52.0)
        sig_a = {
            "action": "buy",
            "binance_symbol": "AAAUSDT",
            "token_address": "AAAUSDT",
            "universe_member": True,
            "quote_turnover_24h_usdt": 10_000_000.0,
            "quote_turnover_24h_source": "ccxt_ticks.quote_volume_24h",
            "quote_turnover_24h_asof": asof_iso,
            "volume_strength": 0.90,
            "composite_alpha_score": 52.0,
        }
        sig_b = {
            "action": "buy",
            "binance_symbol": "BBBUSDT",
            "token_address": "BBBUSDT",
            "universe_member": True,
            "quote_turnover_24h_usdt": 10_000_000.0,
            "quote_turnover_24h_source": "ccxt_ticks.quote_volume_24h",
            "quote_turnover_24h_asof": asof_iso,
            "volume_strength": 0.80,
            "composite_alpha_score": 85.0,
        }
        with patch.dict(
            os.environ,
            {"LIVE_BNB_MULTI_HORIZON_REGIME": "1", "LIVE_BNB_TOP_BUYS": "1"},
            clear=False,
        ):
            kept, report = cap_live_bnb_buys([sig_a, sig_b], now_ms=now_ms)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["binance_symbol"], "BBBUSDT")
        self.assertEqual(report["ranked"], ["BBBUSDT", "AAAUSDT"])


if __name__ == "__main__":
    unittest.main()
