"""Integration contracts for the uploaded five-state upgrade.

These checks keep the accepted news path and the published source fields.
They do not replace the uploaded module tests.
"""

from __future__ import annotations

import math
import unittest
from datetime import datetime, timezone

from decision_engine.core.news_shock_gate import (
    OnchainCalibration,
    NewsReadSnapshot,
    NewsShockStatus,
    classify_news_row,
)
from decision_engine.core.regime_strategy_router import (
    REGIME_1_POSITIVE_GAMMA_PINNED,
    REGIME_2_SHORT_SQUEEZE_BREAKOUT,
    REGIME_3_CROSS_ASSET_BULL_PULLBACK,
    REGIME_4_CROWDED_LONG_DEFENSIVE,
    REGIME_5_CRISIS_LIQUIDATION_HALT,
    apply_five_state_regime_router,
    evaluate_five_state_regime,
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


_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
_AS_OF = "2026-06-01T12:00:00Z"
_PUBLISHED = "2026-06-01T11:50:00Z"


def _news_row(source: str, title: str) -> dict[str, str]:
    return {
        "source": source,
        "source_tier": "whitelist",
        "title": title,
        "summary": "",
        "published_at": _PUBLISHED,
        "fetched_at": _PUBLISHED,
        "url": "https://example.test/" + title,
    }


def _classified(source: str, title: str) -> NewsShockStatus:
    return classify_news_row(_news_row(source, title), now=_NOW)


def _router_snap(shock: NewsShockStatus | None) -> NewsReadSnapshot:
    return NewsReadSnapshot(
        mode="ro",
        availability="ok",
        detail=None,
        db_path=None,
        as_of=_AS_OF,
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


def _calibration() -> OnchainCalibration:
    return OnchainCalibration(
        min_heat_delta=5,
        min_obi_aligned=0.20,
        min_taker_aligned=0.55,
        min_whale_aligned=1.20,
        max_obi_divergent=-0.05,
        max_taker_divergent=0.60,
        max_whale_divergent=0.70,
        max_feature_age_min=90,
    )


def _bull(symbol: str = "SOLUSDT") -> dict[str, object]:
    return {
        "binance_symbol": symbol,
        "symbol": symbol,
        "nvda_1h_ret_pct": 1.60,
        "xs_breadth_1h": 0.75,
        "xs_rank_pct_1h": 0.88,
    }


class NewsPrecedenceTests(unittest.TestCase):
    def test_negated_crisis_does_not_halt_and_a_real_crisis_still_does(self) -> None:
        negated = _classified("coindesk", "Officials confirm no exchange hacked")
        self.assertEqual(negated.severity, "none")
        buy = {"action": "buy", "binance_symbol": "SOLUSDT", "size_pct": 0.10, **_bull()}
        kept, _counts = apply_five_state_regime_router(
            [buy],
            [],
            news_snapshot=_router_snap(negated),
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["market_regime"], REGIME_3_CROSS_ASSET_BULL_PULLBACK)
        self.assertFalse(kept[0]["news_shock_applied"])

        crisis = _classified("coindesk", "Exchange hacked overnight")
        self.assertEqual(crisis.severity, "crisis")
        self.assertEqual(crisis.matched_keyword, "exchange hacked")
        sell = {"action": "sell", "binance_symbol": "SOLUSDT", "size_pct": 1.0}
        out, counts = apply_five_state_regime_router(
            [buy, sell],
            [],
            news_snapshot=_router_snap(crisis),
        )
        self.assertEqual([row["action"] for row in out], ["sell"])
        self.assertEqual(out[0]["size_pct"], 1.0)
        self.assertFalse(out[0]["news_shock_applied"])
        self.assertEqual(out[0]["news_shock_severity"], "crisis")
        self.assertNotIn("recommended_order_type", out[0])
        self.assertEqual(counts[REGIME_5_CRISIS_LIQUIDATION_HALT], 1)

    def test_divergent_book_vetoes_only_an_open_state3_buy(self) -> None:
        features = {
            "obi_top5": -0.10,
            "taker_buy_ratio": 0.50,
            "whale_retail_divergence": 0.50,
            "mention_count_prev": 10,
            "mention_count_last": 30,
            "last_searched_at": _AS_OF,
        }
        sell = {"action": "sell", "binance_symbol": "SOLUSDT", "size_pct": 0.25}
        pre_news = evaluate_five_state_regime({**_bull(), **features})
        self.assertTrue(pre_news.allow_buy)
        self.assertEqual(pre_news.regime, REGIME_3_CROSS_ASSET_BULL_PULLBACK)
        out, counts = apply_five_state_regime_router(
            [
                {"action": "buy", "binance_symbol": "SOLUSDT", "size_pct": 0.10, **_bull(), **features},
                sell,
            ],
            [],
            news_snapshot=_router_snap(None),
            onchain_calibration=_calibration(),
        )
        self.assertEqual([row["action"] for row in out], ["sell"])
        self.assertEqual(out[0]["size_pct"], 0.25)
        self.assertFalse(out[0]["news_shock_applied"])
        self.assertNotIn("recommended_order_type", out[0])
        self.assertEqual(counts[REGIME_3_CROSS_ASSET_BULL_PULLBACK], 1)

        state1, _counts = apply_five_state_regime_router(
            [{
                "action": "buy",
                "binance_symbol": "SOLUSDT",
                "size_pct": 0.10,
                **features,
            }],
            [],
            news_snapshot=_router_snap(None),
            onchain_calibration=_calibration(),
        )
        self.assertEqual(len(state1), 1)
        self.assertEqual(state1[0]["market_regime"], REGIME_1_POSITIVE_GAMMA_PINNED)
        self.assertFalse(state1[0]["news_shock_applied"])

    def test_tighter_regime_keeps_precedence_over_a_later_news_label(self) -> None:
        crisis = _router_snap(_classified("coindesk", "Chapter 11 filing"))
        hawkish = _router_snap(_classified("fed_speeches", "Inflation remains elevated"))
        sell = {"action": "sell", "binance_symbol": "SOLUSDT", "size_pct": 1.0}

        already_crisis, counts = apply_five_state_regime_router(
            [
                {"action": "buy", "binance_symbol": "SOLUSDT", "size_pct": 0.10},
                sell,
            ],
            [{"binance_symbol": "SOLUSDT", "liq_cascade_regime": "long_liquidation_waterfall"}],
            news_snapshot=crisis,
        )
        self.assertEqual([row["action"] for row in already_crisis], ["sell"])
        self.assertEqual(already_crisis[0]["size_pct"], 1.0)
        self.assertFalse(already_crisis[0]["news_shock_applied"])
        self.assertEqual(counts[REGIME_5_CRISIS_LIQUIDATION_HALT], 1)

        squeeze_dropped, squeeze_counts = apply_five_state_regime_router(
            [{"action": "buy", "binance_symbol": "ETHUSDT", "size_pct": 0.10, "funding_z": -2.4}],
            [],
            news_snapshot=crisis,
        )
        self.assertEqual(squeeze_dropped, [])
        self.assertEqual(squeeze_counts[REGIME_5_CRISIS_LIQUIDATION_HALT], 1)

        crowded, _counts = apply_five_state_regime_router(
            [{"action": "buy", "binance_symbol": "SOLUSDT", "size_pct": 0.10, "funding_z": 2.0}],
            [],
            news_snapshot=hawkish,
        )
        self.assertEqual(len(crowded), 1)
        self.assertEqual(crowded[0]["market_regime"], REGIME_4_CROWDED_LONG_DEFENSIVE)
        self.assertEqual(crowded[0]["regime_playbook"], "defensive_deep_fade_with_beta_hedge")
        self.assertNotEqual(crowded[0]["regime_playbook"], "news_hawkish_defensive_fade")
        self.assertAlmostEqual(crowded[0]["size_pct"], 0.055)

        thin_stays_closed, _counts = apply_five_state_regime_router(
            [{
                "action": "buy",
                "binance_symbol": "SOLUSDT",
                "size_pct": 0.10,
                **_bull(),
                "obi_top5": -0.40,
                "taker_buy_ratio": 0.30,
            }],
            [],
            news_snapshot=hawkish,
        )
        self.assertEqual(thin_stays_closed, [])

        squeeze_kept, _counts = apply_five_state_regime_router(
            [{"action": "buy", "binance_symbol": "ETHUSDT", "size_pct": 0.10, "funding_z": -2.4}],
            [],
        )
        self.assertEqual(squeeze_kept[0]["market_regime"], REGIME_2_SHORT_SQUEEZE_BREAKOUT)


class PublishedWallPriceTests(unittest.TestCase):
    def test_signal_carries_the_maker_price_and_execution_does_not_read_it(self) -> None:
        bids = [(600.0, 0.2), (599.5, 0.3), (598.0, 400.0)]
        asks = [(600.2, 0.2), (600.8, 0.3), (602.0, 400.0)]
        out, _counts = apply_five_state_regime_router(
            [{"action": "buy", "binance_symbol": "BNBUSDT", "size_pct": 0.05}],
            [{
                "binance_symbol": "BNBUSDT",
                "l2_bids": bids,
                "l2_asks": asks,
                "tick_size": 0.1,
            }],
        )
        self.assertEqual(len(out), 1)
        row = out[0]
        self.assertEqual(row["recommended_order_type"], "LIMIT_MAKER")
        price = row["recommended_maker_buy_price"]
        wall = row["wall_mid_price"]
        self.assertTrue(math.isfinite(price) and price > 0.0)
        self.assertTrue(math.isfinite(wall) and wall > 0.0)
        self.assertAlmostEqual(price, 598.1, places=4)
        self.assertLess(price, asks[0][0])
        self.assertGreater(price, bids[-1][0])
        self.assertAlmostEqual(round(price / 0.1) * 0.1, price, places=6)
        self.assertNotIn("limit_price", row)
        self.assertNotIn("price", row)

        from decision_engine.core.signal_bus import normalize_signal
        from execution.executors.binance_spot import _chosen_limit_raw

        extras = normalize_signal(row)["_extras"]
        self.assertEqual(extras["recommended_maker_buy_price"], price)
        self.assertNotIn("limit_price", extras)
        chosen, error = _chosen_limit_raw(extras)
        self.assertIsNone(chosen)
        self.assertIsNone(error)


if __name__ == "__main__":
    unittest.main()
