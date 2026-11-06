"""Live neutral buys clear 20 USDT. Entry-zone bands and the 0.5 multiplier stay.

The half size is the zone's risk choice. This lift runs after it, only for
``live_bnb_boll_fade``, and only when ``BINANCE_SPOT_BOOK_USDT`` parses.
Paper purposes, including ``race_tech_boll_fade``, are not resized.
"""

from __future__ import annotations

import math
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from decision_engine.core.signal_bus import (
    PURPOSE_LIVE_BNB_BOLL_FADE,
    PURPOSE_RACE_TECH_BOLL_FADE,
    SignalBus,
)
from decision_engine.strategies.live_bnb_entry_zone import (
    ALPHA_CAP,
    ALPHA_FLOOR,
    LARGE_MIN_ODDS,
    LARGE_MIN_RATIO,
    NEUTRAL_ALPHA_MAX,
    NEUTRAL_ALPHA_MIN,
    NEUTRAL_MIN_ODDS,
    NEUTRAL_MIN_RATIO,
    evaluate_entry_zone,
)
from decision_engine.strategies.live_bnb_size_floor import (
    BOOK_ENV,
    ENTRY_MIN_NOTIONAL_USDT,
    lift_size_pct_for_entry_min,
)
from decision_engine.strategies.tech_boll_fade import TechBollFadeStrategy
from decision_engine.tests.test_live_bnb_entry_zone import _row
from execution.executors.binance_spot_protect import (
    ENTRY_MIN_NOTIONAL_USDT as EXECUTION_ENTRY_MIN_NOTIONAL_USDT,
)
from execution.executors.binance_spot_protect import resolve_entry_size

_PROD_BOOK = "437.63809903"


def _paper_row() -> dict:
    return {
        "chain_id": "solana",
        "token_address": "MintA",
        "change_5m": -3.0,
        "volume_strength": 0.9,
        "boll_lower": 1.0,
        "price_usd": 0.9,
        "liquidity_usd": 90_000.0,
    }


def _persist(signals: list[dict], purpose: str) -> list[sqlite3.Row]:
    with tempfile.TemporaryDirectory() as tmp:
        bus = SignalBus(Path(tmp) / "decision.db", purpose=purpose)
        try:
            bus.publish_many(signals)
            assert bus._rw is not None
            return list(bus._rw.execute("SELECT * FROM decision_log ORDER BY id"))
        finally:
            bus.close()


def _execution_quote(size_pct: float, book: str) -> tuple[str | None, str | None]:
    """``(error, quote_usdt)`` the way the live arm sizes a MARKET buy."""
    _extras, err, plan = resolve_entry_size(
        {"action": "buy", "size_pct": size_pct},
        {"order_type": "MARKET"},
        environ={BOOK_ENV: book},
    )
    quote = None if plan is None else plan.get("quote_usdt")
    return err, None if quote is None else str(quote)


class SizeFloorMathTests(unittest.TestCase):
    def test_floor_constant_matches_execution_and_is_not_lowered(self) -> None:
        self.assertEqual(ENTRY_MIN_NOTIONAL_USDT, Decimal("20"))
        self.assertEqual(ENTRY_MIN_NOTIONAL_USDT, EXECUTION_ENTRY_MIN_NOTIONAL_USDT)

    def test_smallest_float_clears_and_the_previous_ulp_does_not(self) -> None:
        lifted, did = lift_size_pct_for_entry_min(
            0.025, environ={BOOK_ENV: _PROD_BOOK}
        )
        self.assertTrue(did)
        self.assertGreater(lifted, 0.025)
        self.assertLess(lifted, 0.05)
        err, quote = _execution_quote(lifted, _PROD_BOOK)
        self.assertIsNone(err)
        assert quote is not None
        self.assertGreaterEqual(Decimal(quote), ENTRY_MIN_NOTIONAL_USDT)
        self.assertLess(Decimal(quote), Decimal("20.01"))
        again, again_lifted = lift_size_pct_for_entry_min(
            lifted, environ={BOOK_ENV: _PROD_BOOK}
        )
        self.assertFalse(again_lifted)
        self.assertEqual(again, lifted)
        below = math.nextafter(lifted, 0.0)
        bumped, bumped_lifted = lift_size_pct_for_entry_min(
            below, environ={BOOK_ENV: _PROD_BOOK}
        )
        self.assertTrue(bumped_lifted)
        self.assertEqual(bumped, lifted)
        # Operator book cited with the order: 0.025 * 437.64 ≈ 10.94 → just over 20,
        # still under the large-trade 0.05 (≈ 21.88).
        rounded, rounded_lifted = lift_size_pct_for_entry_min(
            0.025, environ={BOOK_ENV: "437.64"}
        )
        self.assertTrue(rounded_lifted)
        self.assertLess(rounded, 0.05)
        err, quote = _execution_quote(rounded, "437.64")
        self.assertIsNone(err)
        assert quote is not None
        self.assertGreaterEqual(Decimal(quote), ENTRY_MIN_NOTIONAL_USDT)
        self.assertLess(Decimal(quote), Decimal("20.01"))

    def test_already_clear_missing_book_and_impossible_book_stay_put(self) -> None:
        # 0.05 * 437.64 = 21.882. Large-trade size is not rewritten.
        kept, lifted = lift_size_pct_for_entry_min(0.05, environ={BOOK_ENV: "437.64"})
        self.assertFalse(lifted)
        self.assertEqual(kept, 0.05)
        # 0.025 * 800 = 20 exactly. Strict `<` is the execution check.
        at_floor, at_lifted = lift_size_pct_for_entry_min(
            0.025, environ={BOOK_ENV: "800"}
        )
        self.assertFalse(at_lifted)
        self.assertEqual(at_floor, 0.025)
        for raw in (None, "", "   ", "not-a-book", "0", "-5", "4.3764e2"):
            book = {} if raw is None else {BOOK_ENV: raw}
            unchanged, did = lift_size_pct_for_entry_min(0.025, environ=book)
            self.assertFalse(did, raw)
            self.assertEqual(unchanged, 0.025, raw)
        # 1 * 10 = 10, still under 20. Do not clamp to a size that cannot clear.
        impossible, did_imp = lift_size_pct_for_entry_min(
            0.025, environ={BOOK_ENV: "10"}
        )
        self.assertFalse(did_imp)
        self.assertEqual(impossible, 0.025)
        # Passed environ wins over a process book that would lift.
        os.environ[BOOK_ENV] = "100"
        try:
            explicit, explicit_lifted = lift_size_pct_for_entry_min(
                0.05, environ={BOOK_ENV: "1000"}
            )
        finally:
            os.environ.pop(BOOK_ENV, None)
        self.assertFalse(explicit_lifted)
        self.assertEqual(explicit, 0.05)


class LiveEmitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = os.environ.get(BOOK_ENV)

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop(BOOK_ENV, None)
        else:
            os.environ[BOOK_ENV] = self._prev

    def _book(self, value: str | None) -> None:
        if value is None:
            os.environ.pop(BOOK_ENV, None)
        else:
            os.environ[BOOK_ENV] = value

    def _live(
        self, rows: list[dict], position_pct: float = 0.05
    ) -> tuple[TechBollFadeStrategy, list[dict]]:
        strategy = TechBollFadeStrategy(position_pct=position_pct)
        signals = strategy.on_tick(
            {"hot_tokens": rows, "purpose": PURPOSE_LIVE_BNB_BOLL_FADE}
        )
        return strategy, signals

    def test_entry_zone_bands_and_reject_reasons_are_unchanged(self) -> None:
        self.assertEqual(ALPHA_FLOOR, 0.08)
        self.assertEqual(ALPHA_CAP, 0.40)
        self.assertEqual(NEUTRAL_ALPHA_MIN, 0.12)
        self.assertEqual(NEUTRAL_ALPHA_MAX, 0.28)
        self.assertEqual(LARGE_MIN_ODDS, 1.5)
        self.assertEqual(NEUTRAL_MIN_ODDS, 2.5)
        self.assertEqual(LARGE_MIN_RATIO, 1.3)
        self.assertEqual(NEUTRAL_MIN_RATIO, 0.85)
        # A book that would lift a neutral buy must not retune the zone.
        self._book("100")
        neutral = evaluate_entry_zone(_row(0.12, 0.85))
        self.assertTrue(neutral.emit, neutral.reject_reason)
        self.assertEqual(neutral.position_size_multiplier, 0.5)
        self.assertEqual(neutral.entry_reason, "neutral_market")
        self.assertAlmostEqual(neutral.entry_zone_min or 0.0, 101.2)
        self.assertAlmostEqual(neutral.entry_zone_max or 0.0, 102.8)
        large = evaluate_entry_zone(_row(0.08, 1.3))
        self.assertEqual(large.position_size_multiplier, 1.0)
        self.assertEqual(large.entry_reason, "large_trade_market")
        self.assertAlmostEqual(large.entry_zone_min or 0.0, 100.8)
        self.assertAlmostEqual(large.entry_zone_max or 0.0, 104.0)
        rejects = (
            (0.079, 2.0, "price_below_floor"),
            (0.40, 2.0, "odds_below_min"),
            (0.10, 1.0, "neutral_below_band"),
            (0.20, 0.5, "retail_crowded_skip"),
        )
        for alpha, ratio, reason in rejects:
            decision = evaluate_entry_zone(_row(alpha, ratio))
            self.assertFalse(decision.emit, reason)
            self.assertEqual(decision.reject_reason, reason)
            strategy, signals = self._live([_row(alpha, ratio)])
            self.assertEqual(signals, [], reason)
            self.assertEqual(strategy.rejected.get(reason), 1, reason)

    def test_neutral_on_the_prod_book_clears_20_and_large_stays_at_005(self) -> None:
        self._book(_PROD_BOOK)
        strategy, signals = self._live([_row(0.12, 0.85), _row(0.08, 1.3)])
        self.assertEqual(strategy.position_pct, 0.05)
        self.assertEqual(len(signals), 2)
        neutral, large = signals
        self.assertEqual(neutral["position_size_multiplier"], 0.5)
        self.assertEqual(
            neutral["position_size_note"],
            "half_planned_size_no_inverse_stop_width_sizer",
        )
        self.assertEqual(neutral["entry_reason"], "neutral_market")
        self.assertAlmostEqual(neutral["alpha"], 0.12)
        self.assertAlmostEqual(neutral["size_pct_before_min_notional"], 0.025)
        self.assertTrue(neutral["min_notional_lift"])
        self.assertEqual(neutral["min_notional_usdt"], "20")
        self.assertAlmostEqual(neutral["planned_size_pct"], 0.05)
        err, quote = _execution_quote(neutral["size_pct"], _PROD_BOOK)
        self.assertIsNone(err)
        assert quote is not None
        self.assertGreaterEqual(Decimal(quote), Decimal("20"))
        self.assertLess(neutral["size_pct"], large["size_pct"])
        self.assertEqual(large["position_size_multiplier"], 1.0)
        self.assertEqual(large["entry_reason"], "large_trade_market")
        self.assertAlmostEqual(large["size_pct"], 0.05)
        self.assertNotIn("min_notional_lift", large)
        large_err, large_quote = _execution_quote(large["size_pct"], _PROD_BOOK)
        self.assertIsNone(large_err)
        self.assertEqual(large_quote, "21.88190495")
        rows = _persist(signals, PURPOSE_LIVE_BNB_BOLL_FADE)
        self.assertEqual(len(rows), 2)
        stored = float(rows[0]["size_pct"])
        stored_err, stored_quote = _execution_quote(stored, _PROD_BOOK)
        self.assertIsNone(stored_err)
        assert stored_quote is not None
        self.assertGreaterEqual(Decimal(stored_quote), Decimal("20"))

    def test_above_floor_neutral_keeps_the_half_size(self) -> None:
        self._book("800")
        _strategy, signals = self._live([_row(0.12, 1.0)])
        self.assertEqual(len(signals), 1)
        self.assertAlmostEqual(signals[0]["size_pct"], 0.025)
        self.assertEqual(signals[0]["position_size_multiplier"], 0.5)
        self.assertNotIn("min_notional_lift", signals[0])
        err, quote = _execution_quote(signals[0]["size_pct"], "800")
        self.assertIsNone(err)
        self.assertEqual(quote, "20")

        self._book("1000")
        _strategy, richer = self._live([_row(0.20, 1.0)])
        self.assertAlmostEqual(richer[0]["size_pct"], 0.025)
        self.assertNotIn("min_notional_lift", richer[0])
        err, quote = _execution_quote(richer[0]["size_pct"], "1000")
        self.assertIsNone(err)
        self.assertEqual(quote, "25")

    def test_any_under_floor_path_is_lifted_including_large_trade(self) -> None:
        # 0.05 * 300 = 15. Large is already multiplier 1 and still under 20.
        self._book("300")
        _strategy, signals = self._live([_row(0.12, 0.85), _row(0.08, 1.3)])
        self.assertEqual(len(signals), 2)
        neutral, large = signals
        self.assertEqual(neutral["position_size_multiplier"], 0.5)
        self.assertEqual(large["position_size_multiplier"], 1.0)
        self.assertTrue(neutral["min_notional_lift"])
        self.assertTrue(large["min_notional_lift"])
        self.assertAlmostEqual(large["size_pct_before_min_notional"], 0.05)
        self.assertEqual(neutral["size_pct"], large["size_pct"])
        for signal in signals:
            err, quote = _execution_quote(signal["size_pct"], "300")
            self.assertIsNone(err, signal["entry_reason"])
            assert quote is not None
            self.assertGreaterEqual(Decimal(quote), Decimal("20"))
        # A neutral limit is the same half size, so it is lifted too.
        _strategy, limit = self._live([_row(0.28, 0.85)])
        self.assertEqual(limit[0]["order_type"], "LIMIT")
        self.assertEqual(limit[0]["entry_reason"], "neutral_limit")
        self.assertEqual(limit[0]["position_size_multiplier"], 0.5)
        self.assertAlmostEqual(limit[0]["limit_price"], 102.0)
        self.assertTrue(limit[0]["min_notional_lift"])
        err, quote = _execution_quote(limit[0]["size_pct"], "300")
        self.assertIsNone(err)
        assert quote is not None
        self.assertGreaterEqual(Decimal(quote), Decimal("20"))

    def test_missing_book_and_impossible_book_do_not_invent_a_size(self) -> None:
        self._book(None)
        _strategy, missing = self._live([_row(0.12, 0.85)])
        self.assertAlmostEqual(missing[0]["size_pct"], 0.025)
        self.assertEqual(missing[0]["position_size_multiplier"], 0.5)
        self.assertNotIn("min_notional_lift", missing[0])
        self._book("10")
        _strategy, tiny = self._live([_row(0.12, 0.85)])
        self.assertAlmostEqual(tiny[0]["size_pct"], 0.025)
        self.assertNotIn("min_notional_lift", tiny[0])
        err, _quote = _execution_quote(tiny[0]["size_pct"], "10")
        self.assertEqual(err, "entry_notional_below_min")

    def test_paper_purpose_keeps_its_size_when_the_book_is_under_the_floor(self) -> None:
        # 0.05 * 100 = 5. A live half-size would be lifted or refused. Paper is neither.
        self._book("100")
        strategy = TechBollFadeStrategy(position_pct=0.05)
        paper = strategy.on_tick(
            {"hot_tokens": [_paper_row()], "purpose": PURPOSE_RACE_TECH_BOLL_FADE}
        )
        self.assertEqual(len(paper), 1)
        self.assertAlmostEqual(paper[0]["size_pct"], 0.05)
        self.assertEqual(paper[0]["and_mode"], "tech_fade&boll_lower")
        self.assertNotIn("min_notional_lift", paper[0])
        self.assertNotIn("entry_reason", paper[0])
        self.assertNotIn("position_size_multiplier", paper[0])
        untouched = strategy.on_tick({"hot_tokens": [_paper_row()]})
        self.assertEqual(len(untouched), 1)
        self.assertAlmostEqual(untouched[0]["size_pct"], 0.05)
        self.assertNotIn("min_notional_lift", untouched[0])
        rows = _persist(paper, PURPOSE_RACE_TECH_BOLL_FADE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["purpose"], PURPOSE_RACE_TECH_BOLL_FADE)
        self.assertAlmostEqual(float(rows[0]["size_pct"]), 0.05)


if __name__ == "__main__":
    unittest.main()
