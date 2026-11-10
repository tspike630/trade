"""Integration contracts for the uploaded five-state upgrade.

These checks keep the accepted news path and the published source fields.
They do not replace the uploaded module tests.
"""

from __future__ import annotations

import unittest

from decision_engine.core.news_shock_gate import NewsReadSnapshot, NewsShockStatus
from decision_engine.core.regime_strategy_router import (
    REGIME_3_CROSS_ASSET_BULL_PULLBACK,
    REGIME_4_CROWDED_LONG_DEFENSIVE,
    apply_five_state_regime_router,
)
from decision_engine.core.wall_mid_pricing import compute_wall_mid_from_book


def _snap(shock: NewsShockStatus | None) -> NewsReadSnapshot:
    return NewsReadSnapshot(
        mode="ro",
        availability="ok",
        detail=None,
        db_path=None,
        as_of="2026-11-10T08:00:00Z",
        window_min=90,
        shocks=() if shock is None else (shock,),
        context_count=1,
        scanned=1,
        suppressed=0,
        x_watch_availability="not_configured",
        x_watch_detail=None,
        x_points=(),
        feature_availability="not_requested",
        feature_detail=None,
        obi_points=(),
        taker_points=(),
        whale_points=(),
    )


class NewsAndSourcePassthroughTests(unittest.TestCase):
    def test_ordinary_buy_publishes_limit_maker_and_keeps_finite_source_fields(self) -> None:
        out, _counts = apply_five_state_regime_router(
            [{
                "action": "buy",
                "binance_symbol": "SOLUSDT",
                "size_pct": 0.05,
                "obi_top5": 0.40,
                "obi_top5_source_ts_ms": 50,
                "volume_z": 1.10,
            }],
            [{
                "binance_symbol": "SOLUSDT",
                "xs_breadth_1h": 0.80,
                "xs_rank_pct_1h": 0.90,
                "obi_top5": -0.10,
                "obi_top5_source_ts_ms": 999,
                "volume_z": 2.50,
                "orderbook_imbalance_20": -0.80,
            }],
        )
        self.assertEqual(len(out), 1)
        row = out[0]
        self.assertEqual(row["market_regime"], REGIME_3_CROSS_ASSET_BULL_PULLBACK)
        self.assertEqual(row["recommended_order_type"], "LIMIT_MAKER")
        self.assertEqual(row["obi_top5"], 0.40)
        self.assertEqual(row["obi_top5_source_ts_ms"], 50)
        self.assertEqual(row["volume_z"], 1.10)
        self.assertNotIn("orderbook_imbalance_20", row)

    def test_news_hawkish_still_routes_defensive_and_sell_is_unchanged(self) -> None:
        shock = NewsShockStatus(
            "hawkish",
            "inflation remains elevated",
            "inflation remains elevated",
            "fed_speeches",
            10.0,
        )
        sell = {"action": "sell", "binance_symbol": "ETHUSDT", "size_pct": 0.10}
        out, _counts = apply_five_state_regime_router(
            [
                {"action": "buy", "binance_symbol": "ETHUSDT", "size_pct": 0.10},
                sell,
            ],
            [{"binance_symbol": "ETHUSDT"}],
            news_snapshot=_snap(shock),
        )
        self.assertEqual([row["action"] for row in out], ["buy", "sell"])
        self.assertEqual(out[0]["market_regime"], REGIME_4_CROWDED_LONG_DEFENSIVE)
        self.assertEqual(out[0]["recommended_order_type"], "LIMIT_MAKER")
        self.assertEqual(out[1]["size_pct"], 0.10)
        self.assertEqual(out[1]["action"], "sell")
        self.assertNotIn("recommended_order_type", out[1])
        self.assertFalse(out[1]["news_shock_applied"])

    def test_missing_book_does_not_invent_a_wall_or_a_zero_price(self) -> None:
        out, _counts = apply_five_state_regime_router(
            [{"action": "buy", "binance_symbol": "BNBUSDT", "size_pct": 0.05}],
            [{"binance_symbol": "BNBUSDT"}],
        )
        self.assertEqual(len(out), 1)
        self.assertNotIn("wall_mid_price", out[0])
        self.assertNotIn("recommended_maker_buy_price", out[0])
        self.assertIsNone(compute_wall_mid_from_book([], [(1.0, 1.0)]))
        crossed = compute_wall_mid_from_book([(10.0, 1.0)], [(9.0, 1.0)], tick_size=0.01)
        self.assertIsNone(crossed)


class WallMidContractTests(unittest.TestCase):
    def test_maker_prices_stay_positive_finite_and_do_not_cross(self) -> None:
        bids = [(100.00, 0.1), (99.80, 500.0)]
        asks = [(100.02, 0.1), (100.40, 500.0)]
        quote = compute_wall_mid_from_book(bids, asks, tick_size=0.01)
        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertGreater(quote.maker_buy_price, 0.0)
        self.assertGreater(quote.maker_sell_price, 0.0)
        self.assertLess(quote.maker_buy_price, asks[0][0])
        self.assertGreater(quote.maker_sell_price, bids[0][0])
        self.assertAlmostEqual(round(quote.maker_buy_price / 0.01) * 0.01, quote.maker_buy_price, places=6)
        junk = compute_wall_mid_from_book([(0.0, 1.0), (-1.0, 2.0)], [(float("nan"), 1.0)], tick_size=0.01)
        self.assertIsNone(junk)


if __name__ == "__main__":
    unittest.main()
