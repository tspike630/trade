"""Entry zone for live_bnb_boll_fade. Paper race_tech_boll_fade stays on the old legs."""

from __future__ import annotations

import unittest

from decision_engine.core.signal_bus import (
    PURPOSE_LIVE_BNB_BOLL_FADE,
    PURPOSE_RACE_TECH_BOLL_FADE,
)
from decision_engine.strategies.entry_sizing import buy_size_fields
from decision_engine.strategies.live_bnb_entry_zone import (
    ZoneBar,
    evaluate_entry_zone,
    reward_risk,
    trade_size_ratio_from_bars,
)
from decision_engine.strategies.tech_boll_fade import TechBollFadeStrategy

_LOW = 100.0
_SPAN = 10.0
_MID = _LOW + _SPAN
_STEP = 5 * 60 * 1000
_DAY = 24 * 60 * 60 * 1000


def _price(alpha: float, *, low: float = _LOW, span: float = _SPAN) -> float:
    return low + alpha * span


def _row(
    alpha: float,
    ratio: float,
    *,
    low: float = _LOW,
    span: float = _SPAN,
    price: float | None = None,
    **overrides: object,
) -> dict:
    mid = low + span
    row = {
        "binance_symbol": "BNBUSDT",
        "chain_id": "binance",
        "token_address": "BNBUSDT",
        "kline_low": low,
        "boll_mid": mid,
        "price_usd": _price(alpha, low=low, span=span) if price is None else price,
        "trade_size_ratio": ratio,
        "change_5m": -0.5,
        "volume_strength": 0.1,
        "boll_lower": low,
        "liquidity_usd": 90_000.0,
    }
    row.update(overrides)
    return row


def _bar(
    open_ms: int,
    *,
    quote: float,
    trades: float | None,
    low: float = 96.0,
    close: float = 97.0,
) -> ZoneBar:
    return ZoneBar(
        open_ms=open_ms,
        close_ms=open_ms + _STEP,
        low=low,
        close=close,
        quote_volume=quote,
        trades=trades,
    )


class BoundaryTests(unittest.TestCase):
    def test_large_includes_alpha_0_08_and_ratio_1_3(self) -> None:
        decision = evaluate_entry_zone(_row(0.08, 1.3))
        self.assertTrue(decision.emit, decision.reject_reason)
        self.assertEqual(decision.tier, "large_trade")
        self.assertEqual(decision.entry_reason, "large_trade_market")
        self.assertEqual(decision.order_type, "MARKET")
        self.assertIsNone(decision.limit_price)
        self.assertEqual(decision.position_size_multiplier, 1.0)
        self.assertAlmostEqual(decision.stop_price or 0.0, _LOW - 0.1 * _SPAN)
        self.assertAlmostEqual(decision.take_profit_price or 0.0, _MID)
        self.assertAlmostEqual(decision.entry_zone_min or 0.0, _price(0.08))
        self.assertAlmostEqual(decision.entry_zone_max or 0.0, _price(0.40))
        odds = reward_risk(_MID, _price(0.08), _LOW - 0.1 * _SPAN)
        self.assertIsNotNone(odds)
        assert odds is not None
        self.assertGreaterEqual(odds, 1.5)
        self.assertFalse(decision.legacy_change_5m_pass)
        self.assertFalse(decision.legacy_volume_strength_pass)

    def test_alpha_0_40_is_inside_the_band_and_fails_odds(self) -> None:
        decision = evaluate_entry_zone(_row(0.40, 2.0))
        self.assertFalse(decision.emit)
        self.assertEqual(decision.reject_reason, "odds_below_min")
        self.assertEqual(decision.tier, "large_trade")
        odds = reward_risk(_MID, _price(0.40), _LOW - 0.1 * _SPAN)
        self.assertIsNotNone(odds)
        assert odds is not None
        self.assertLess(odds, 1.5)

    def test_ratio_just_under_1_3_is_neutral(self) -> None:
        decision = evaluate_entry_zone(_row(0.12, 1.299999))
        self.assertTrue(decision.emit, decision.reject_reason)
        self.assertEqual(decision.tier, "neutral")
        self.assertEqual(decision.entry_reason, "neutral_market")

    def test_neutral_alpha_bounds_0_12_and_0_28(self) -> None:
        at_floor = evaluate_entry_zone(_row(0.12, 0.85))
        self.assertTrue(at_floor.emit, at_floor.reject_reason)
        self.assertEqual(at_floor.entry_reason, "neutral_market")
        self.assertEqual(at_floor.order_type, "MARKET")
        self.assertEqual(at_floor.position_size_multiplier, 0.5)
        self.assertAlmostEqual(at_floor.entry_zone_min or 0.0, _price(0.12))
        self.assertAlmostEqual(at_floor.entry_zone_max or 0.0, _price(0.28))
        at_cap = evaluate_entry_zone(_row(0.28, 0.85))
        self.assertTrue(at_cap.emit, at_cap.reject_reason)
        self.assertEqual(at_cap.entry_reason, "neutral_limit")
        self.assertEqual(at_cap.order_type, "LIMIT")
        self.assertAlmostEqual(at_cap.limit_price or 0.0, _price(0.20))
        self.assertAlmostEqual(at_cap.take_profit_price or 0.0, _MID)
        self.assertLess(at_cap.limit_price or 0.0, _price(0.28))

    def test_neutral_alpha_0_20_is_a_market_not_a_chase(self) -> None:
        decision = evaluate_entry_zone(_row(0.20, 1.0))
        self.assertEqual(decision.entry_reason, "neutral_market")
        self.assertEqual(decision.order_type, "MARKET")
        self.assertIsNone(decision.limit_price)

    def test_price_below_floor_and_above_mid(self) -> None:
        below = evaluate_entry_zone(_row(0.079, 2.0))
        self.assertEqual(below.reject_reason, "price_below_floor")
        self.assertFalse(below.emit)
        above = evaluate_entry_zone(_row(0.0, 2.0, price=_MID + 1.0))
        self.assertEqual(above.reject_reason, "price_above_mid")
        self.assertFalse(above.emit)
        at_mid = evaluate_entry_zone(_row(0.0, 2.0, price=_MID))
        self.assertEqual(at_mid.reject_reason, "price_above_mid")

    def test_neutral_above_mid_rests_a_limit_and_does_not_cross(self) -> None:
        decision = evaluate_entry_zone(_row(0.0, 1.0, price=_MID + 5.0))
        self.assertTrue(decision.emit, decision.reject_reason)
        self.assertEqual(decision.entry_reason, "neutral_limit")
        self.assertAlmostEqual(decision.limit_price or 0.0, _price(0.20))
        self.assertLess(decision.limit_price or 0.0, _MID + 5.0)
        self.assertEqual(decision.position_size_multiplier, 0.5)

    def test_neutral_below_0_12_does_not_chase_with_a_higher_limit(self) -> None:
        decision = evaluate_entry_zone(_row(0.10, 1.0))
        self.assertFalse(decision.emit)
        self.assertEqual(decision.reject_reason, "neutral_below_band")
        self.assertIsNone(decision.limit_price)

    def test_span_not_positive_and_stop_not_below_price(self) -> None:
        flat = evaluate_entry_zone(_row(0.2, 2.0, span=0.0))
        self.assertEqual(flat.reject_reason, "span_not_positive")
        inverted = evaluate_entry_zone(_row(0.2, 2.0, low=110.0, span=-5.0))
        self.assertEqual(inverted.reject_reason, "span_not_positive")
        # Stop is L-0.1*S. A close at or under that stop has no odds.
        buried = evaluate_entry_zone(_row(-0.1, 2.0))
        self.assertEqual(buried.reject_reason, "stop_not_below_price")
        self.assertFalse(buried.emit)
        stop = _LOW - 0.1 * _SPAN
        self.assertIsNone(reward_risk(_MID, stop, stop))
        self.assertIsNone(reward_risk(_MID, stop - 0.01, stop))

    def test_missing_coordinates_fail_closed_without_the_old_legs(self) -> None:
        classic = _row(0.2, 2.0)
        classic["change_5m"] = -4.0
        classic["volume_strength"] = 0.9
        classic["boll_lower"] = classic["price_usd"] + 1.0
        for key in ("kline_low", "trade_size_ratio"):
            broken = dict(classic)
            broken.pop(key)
            decision = evaluate_entry_zone(broken)
            self.assertFalse(decision.emit, key)
            self.assertIn(
                decision.reject_reason,
                {"kline_low_missing", "trade_size_ratio_missing"},
            )
        no_mid = dict(classic)
        no_mid.pop("boll_mid")
        self.assertEqual(evaluate_entry_zone(no_mid).reject_reason, "boll_mid_missing")
        bad_ratio = dict(classic)
        bad_ratio["trade_size_ratio"] = -1.0
        self.assertEqual(
            evaluate_entry_zone(bad_ratio).reject_reason,
            "trade_size_ratio_invalid",
        )

    def test_ratio_from_quote_and_trades_excludes_the_current_bar_and_older_than_24h(self) -> None:
        current_open = 1_700_000_000_000
        stale = _bar(current_open - _DAY - _STEP, quote=1_000.0, trades=1.0)
        baseline = _bar(current_open - _STEP, quote=1_000.0, trades=100.0)
        current = _bar(current_open, quote=1_000.0, trades=50.0)
        ratio, count = trade_size_ratio_from_bars([stale, baseline, current])
        self.assertEqual(count, 1)
        self.assertAlmostEqual(ratio or 0.0, 2.0)
        edge = _bar(current_open + _STEP - _DAY - _STEP, quote=500.0, trades=50.0)
        # close of `edge` is exactly 24h before the current close, so it counts.
        ratio_edge, count_edge = trade_size_ratio_from_bars([edge, current])
        self.assertEqual(count_edge, 1)
        self.assertAlmostEqual(ratio_edge or 0.0, (1000.0 / 50.0) / (500.0 / 50.0))
        missing, _n = trade_size_ratio_from_bars(
            [baseline, _bar(current_open, quote=1_000.0, trades=None)]
        )
        self.assertIsNone(missing)

    def test_avg_trade_size_over_baseline_when_ratio_column_is_absent(self) -> None:
        row = _row(0.20, 0.0)
        row.pop("trade_size_ratio")
        row["avg_trade_size"] = 20.0
        row["baseline_quote_volume"] = 1_000.0
        row["baseline_trades"] = 100.0
        decision = evaluate_entry_zone(row)
        self.assertTrue(decision.emit, decision.reject_reason)
        self.assertAlmostEqual(decision.trade_size_ratio or 0.0, 2.0)
        self.assertEqual(decision.trade_size_ratio_source, "avg_trade_size_over_kline_baseline")

    def test_retail_crowded_skips_and_records_the_deep_price(self) -> None:
        decision = evaluate_entry_zone(_row(0.20, 0.849999))
        self.assertFalse(decision.emit)
        self.assertEqual(decision.reject_reason, "retail_crowded_skip")
        self.assertEqual(decision.tier, "retail_crowded")
        self.assertIsNone(decision.order_type)
        self.assertAlmostEqual(decision.deep_zone_price or 0.0, _LOW - 0.5 * _SPAN)
        at_edge = evaluate_entry_zone(_row(0.20, 0.85))
        self.assertEqual(at_edge.tier, "neutral")

    def test_half_size_is_half_the_planned_name_and_leaves_the_sizer_alone(self) -> None:
        row = _row(0.12, 1.0)
        planned = buy_size_fields(row, floor_pct=0.2)
        strategy = TechBollFadeStrategy(position_pct=0.2)
        signals = strategy.on_tick(
            {"hot_tokens": [row], "purpose": PURPOSE_LIVE_BNB_BOLL_FADE}
        )
        self.assertEqual(len(signals), 1)
        self.assertAlmostEqual(signals[0]["planned_size_pct"], planned["size_pct"])
        self.assertAlmostEqual(signals[0]["size_pct"], planned["size_pct"] * 0.5)
        self.assertEqual(signals[0]["position_size_multiplier"], 0.5)
        self.assertEqual(
            signals[0]["position_size_note"],
            "half_planned_size_no_inverse_stop_width_sizer",
        )
        large = strategy.on_tick(
            {
                "hot_tokens": [_row(0.20, 1.5)],
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
            }
        )
        self.assertAlmostEqual(large[0]["size_pct"], planned["size_pct"])
        self.assertEqual(large[0]["position_size_multiplier"], 1.0)

    def test_paper_purpose_keeps_the_old_legs(self) -> None:
        legacy = {
            "chain_id": "solana",
            "token_address": "MintA",
            "change_5m": -3.0,
            "volume_strength": 0.9,
            "boll_lower": 1.0,
            "price_usd": 0.9,
            "liquidity_usd": 90_000.0,
        }
        strategy = TechBollFadeStrategy()
        paper = strategy.on_tick(
            {"hot_tokens": [legacy], "purpose": PURPOSE_RACE_TECH_BOLL_FADE}
        )
        self.assertEqual(len(paper), 1)
        self.assertEqual(paper[0]["and_mode"], "tech_fade&boll_lower")
        self.assertNotIn("entry_reason", paper[0])
        untouched = strategy.on_tick({"hot_tokens": [legacy]})
        self.assertEqual(len(untouched), 1)
        self.assertEqual(untouched[0]["and_mode"], "tech_fade&boll_lower")
        live = strategy.on_tick(
            {"hot_tokens": [legacy], "purpose": PURPOSE_LIVE_BNB_BOLL_FADE}
        )
        self.assertEqual(live, [])
        self.assertGreater(strategy.rejected.get("binance_symbol_missing", 0), 0)

    def test_live_emit_extras_and_old_legs_do_not_block(self) -> None:
        row = _row(0.20, 1.6)
        row["change_5m"] = 0.0
        row["volume_strength"] = 0.0
        row["boll_lower"] = 1.0
        signals = TechBollFadeStrategy().on_tick(
            {"hot_tokens": [row], "purpose": PURPOSE_LIVE_BNB_BOLL_FADE}
        )
        self.assertEqual(len(signals), 1)
        got = signals[0]
        self.assertEqual(got["binance_symbol"], "BNBUSDT")
        self.assertEqual(got["and_mode"], "entry_zone")
        self.assertEqual(got["legacy_gate"], "diagnostic_only")
        self.assertFalse(got["legacy_change_5m_pass"])
        self.assertFalse(got["legacy_volume_strength_pass"])
        self.assertFalse(got["legacy_boll_lower_break"])
        for key in (
            "entry_zone_min",
            "entry_zone_max",
            "alpha",
            "trade_size_ratio",
            "stop_price",
            "take_profit_price",
            "order_type",
            "limit_price",
            "position_size_multiplier",
            "entry_reason",
        ):
            self.assertIn(key, got)
        self.assertEqual(got["order_type"], "MARKET")
        self.assertIsNone(got["limit_price"])
        self.assertEqual(got["entry_reason"], "large_trade_market")
        self.assertAlmostEqual(got["take_profit_price"], _MID)
        limit = TechBollFadeStrategy().on_tick(
            {
                "hot_tokens": [_row(0.28, 1.0)],
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
            }
        )
        self.assertEqual(limit[0]["order_type"], "LIMIT")
        self.assertAlmostEqual(limit[0]["limit_price"], _price(0.20))
        crowded_strategy = TechBollFadeStrategy()
        crowded = crowded_strategy.on_tick(
            {
                "hot_tokens": [_row(0.20, 0.5)],
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
            }
        )
        self.assertEqual(crowded, [])
        self.assertEqual(crowded_strategy.rejected.get("retail_crowded_skip"), 1)


if __name__ == "__main__":
    unittest.main()
