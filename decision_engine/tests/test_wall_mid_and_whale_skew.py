import inspect
import unittest

from decision_engine.core.regime_strategy_router import (
    REGIME_1_POSITIVE_GAMMA_PINNED,
    apply_five_state_regime_router,
    evaluate_five_state_regime,
)
from decision_engine.core.wall_mid_pricing import (
    compute_wall_mid_from_book,
    extract_wall_mid_from_row,
)


class WallMidAndWhaleSkewTests(unittest.TestCase):
    def test_wall_mid_ignores_tiny_top_of_book_noise_and_quotes_1_tick_ahead_of_whale_wall(self) -> None:
        # Tiny 0.1-lot noise at 100.00 / 100.02, massive 500-lot whale walls at 99.80 and 100.40
        bids = [(100.00, 0.1), (99.95, 0.2), (99.90, 0.2), (99.80, 500.0), (99.70, 0.3)]
        asks = [(100.02, 0.1), (100.10, 0.2), (100.20, 0.2), (100.40, 500.0), (100.50, 0.3)]
        q = compute_wall_mid_from_book(bids, asks, tick_size=0.01)
        self.assertIsNotNone(q)
        assert q is not None
        self.assertTrue(q.used_true_bid_wall)
        self.assertTrue(q.used_true_ask_wall)
        self.assertAlmostEqual(q.bid_wall_price, 99.80, places=4)
        self.assertAlmostEqual(q.ask_wall_price, 100.40, places=4)
        # WallMid = (99.80 + 100.40) / 2 = 100.10 (vs raw mid 100.01)
        self.assertAlmostEqual(q.wall_mid_price, 100.10, places=4)
        # Maker buy sits 1 tick ahead of the 99.80 whale bid wall -> 99.81 (shielded by 500-lot wall!)
        self.assertAlmostEqual(q.maker_buy_price, 99.81, places=4)

    def test_asymmetric_whale_skew_in_state_1(self) -> None:
        # 1) Bullish whale accumulation -> wider TP (1.22x) and 1.08x size
        dec_bull = evaluate_five_state_regime({"symbol": "SOLUSDT", "whale_retail_divergence": 1.45, "funding_z": 0.1})
        self.assertEqual(dec_bull.regime, REGIME_1_POSITIVE_GAMMA_PINNED)
        self.assertEqual(dec_bull.playbook, "tech_boll_fade_whale_bull_skew")
        self.assertAlmostEqual(dec_bull.tp_multiplier, 1.22, places=2)
        self.assertAlmostEqual(dec_bull.size_multiplier, 1.08, places=2)

        # 2) Bearish whale distribution + shallow dip (boll_z = -1.9 > -2.25) -> skipped!
        dec_shallow = evaluate_five_state_regime(
            {"symbol": "SOLUSDT", "whale_retail_divergence": 0.60, "boll_z": -1.90}
        )
        self.assertFalse(dec_shallow.allow_buy)
        self.assertEqual(dec_shallow.reject_reason, "regime1_whale_bear_skew_shallow_dip")

        # 3) Bearish whale distribution + deep discount (boll_z = -2.45) -> allowed with fast exit (tp=0.72x)
        dec_deep = evaluate_five_state_regime(
            {"symbol": "SOLUSDT", "whale_retail_divergence": 0.60, "boll_z": -2.45}
        )
        self.assertTrue(dec_deep.allow_buy)
        self.assertEqual(dec_deep.playbook, "tech_boll_fade_whale_bear_skew")
        self.assertAlmostEqual(dec_deep.tp_multiplier, 0.72, places=2)
        self.assertAlmostEqual(dec_deep.size_multiplier, 0.85, places=2)

    def test_router_stamps_wall_mid_and_maker_buy_price(self) -> None:
        candidates = [{"action": "buy", "binance_symbol": "BNBUSDT", "size_pct": 0.10}]
        hot = [
            {
                "binance_symbol": "BNBUSDT",
                "l2_bids": [(600.0, 0.2), (599.5, 0.3), (598.0, 400.0)],
                "l2_asks": [(600.2, 0.2), (600.8, 0.3), (602.0, 400.0)],
                "tick_size": 0.1,
            }
        ]
        out, _ = apply_five_state_regime_router(candidates, hot)
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0]["wall_mid_price"], 600.0, places=4)
        self.assertAlmostEqual(out[0]["recommended_maker_buy_price"], 598.1, places=4)

    def test_unsorted_book_ranks_by_price_before_the_top_20_cut(self) -> None:
        real_bids = [(100.00, 0.1), (99.95, 0.2), (99.90, 0.2), (99.80, 500.0), (99.70, 0.3)]
        real_asks = [(100.02, 0.1), (100.10, 0.2), (100.20, 0.2), (100.40, 500.0), (100.50, 0.3)]
        # Worse prices occupy the first 25 raw slots. A cut before the sort
        # would never see the touch or the whale wall.
        bids = [(90.0 - i, 0.01) for i in range(25)] + list(real_bids)
        asks = [(200.0 + i, 0.01) for i in range(25)] + list(real_asks)
        quote = compute_wall_mid_from_book(bids, asks, tick_size=0.01)
        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertAlmostEqual(quote.bid_wall_price, 99.80, places=4)
        self.assertAlmostEqual(quote.ask_wall_price, 100.40, places=4)
        self.assertAlmostEqual(quote.raw_mid_price, 100.01, places=4)
        self.assertAlmostEqual(quote.maker_buy_price, 99.81, places=4)
        self.assertGreater(quote.maker_buy_price, 0.0)
        self.assertLess(quote.maker_buy_price, real_asks[0][0])
        self.assertGreater(quote.maker_sell_price, real_bids[0][0])

    def test_invalid_units_and_quote_size_do_not_become_a_book(self) -> None:
        quote = compute_wall_mid_from_book(
            [
                (0.0, 1.0),
                (-1.0, 2.0),
                (float("nan"), 1.0),
                (100.0, 0.0),
                (100.0, -2.0),
                {"price": 100.0, "notional_usd": 1_000_000.0},
                {"px": 99.0, "quote_qty": 50.0},
            ],
            [
                (101.0, -1.0),
                {"price": 101.0, "notional_usd": 1_000_000.0},
            ],
            tick_size=0.01,
        )
        self.assertIsNone(quote)
        self.assertIsNone(
            extract_wall_mid_from_row(
                {
                    "orderbook_imbalance_20": -0.80,
                    "bid_depth_20_usd": 1_000_000.0,
                    "ask_depth_20_usd": 1_000_000.0,
                    "obi_top5": -0.30,
                }
            )
        )

    def test_missing_wall_notional_stays_unset(self) -> None:
        quote = extract_wall_mid_from_row(
            {
                "bid_wall_price": 99.80,
                "ask_wall_price": 100.40,
                "best_bid": 100.00,
                "best_ask": 100.02,
                "tick_size": 0.01,
            }
        )
        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertIsNone(quote.bid_wall_notional_usd)
        self.assertIsNone(quote.ask_wall_notional_usd)
        self.assertAlmostEqual(quote.maker_buy_price, 99.81, places=4)

    def test_book_helper_has_no_freshness_clock(self) -> None:
        self.assertNotIn("now_ms", inspect.signature(compute_wall_mid_from_book).parameters)
        stale = compute_wall_mid_from_book(
            [
                {"price": 100.0, "qty": 1.0, "ts_ms": 0},
                {"price": 99.5, "qty": 1.0, "ts_ms": 0},
            ],
            [
                {"price": 100.2, "qty": 1.0, "ts_ms": 0},
                {"price": 100.8, "qty": 1.0, "ts_ms": 0},
            ],
            tick_size=0.01,
        )
        fresh = compute_wall_mid_from_book(
            [(100.0, 1.0), (99.5, 1.0)],
            [(100.2, 1.0), (100.8, 1.0)],
            tick_size=0.01,
        )
        self.assertEqual(stale, fresh)


if __name__ == "__main__":
    unittest.main()
