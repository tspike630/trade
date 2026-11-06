"""Operational prefilter after the live_bnb min-notional lift.

A neutral 0.025 on a book near 437.64 is lifted to just over 20 USDT and
kept. The prefilter drops a buy only when the quote is still under 20 after
that lift. A missing or unparseable book fails open. Other purposes are
unchanged. Multipliers and --position-pct stay as they are.
"""

from __future__ import annotations

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
from decision_engine.strategies.live_bnb_notional import ENTRY_MIN_NOTIONAL_USDT
from decision_engine.strategies.live_bnb_size_floor import (
    ENTRY_MIN_NOTIONAL_USDT as LIFT_ENTRY_MIN_NOTIONAL_USDT,
)
from decision_engine.strategies.tech_boll_fade import TechBollFadeStrategy
from execution.executors.binance_spot_protect import (
    ENTRY_MIN_NOTIONAL_USDT as EXECUTION_ENTRY_MIN_NOTIONAL_USDT,
)
from execution.executors.binance_spot_protect import resolve_entry_size

_BOOK = "BINANCE_SPOT_BOOK_USDT"
_LOGGER = "dexscreener.decision_engine.tech_boll_fade"
_PREFILTER = "entry_notional_below_min_prefilter"


def _row(alpha: float, ratio: float) -> dict:
    low = 100.0
    span = 10.0
    return {
        "binance_symbol": "BNBUSDT",
        "chain_id": "binance",
        "token_address": "BNBUSDT",
        "kline_low": low,
        "boll_mid": low + span,
        "price_usd": low + alpha * span,
        "trade_size_ratio": ratio,
        "change_5m": -0.5,
        "volume_strength": 0.1,
        "boll_lower": low,
        "liquidity_usd": 90_000.0,
    }


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


def _execution_quote(size_pct: float, book: str) -> tuple[str | None, str | None]:
    _extras, err, plan = resolve_entry_size(
        {"action": "buy", "size_pct": size_pct},
        {"order_type": "MARKET"},
        environ={_BOOK: book},
    )
    quote = None if plan is None else plan.get("quote_usdt")
    return err, None if quote is None else str(quote)


def _persist(signals: list[dict], purpose: str) -> list[sqlite3.Row]:
    with tempfile.TemporaryDirectory() as tmp:
        bus = SignalBus(Path(tmp) / "decision.db", purpose=purpose)
        try:
            bus.publish_many(signals)
            assert bus._rw is not None
            return list(bus._rw.execute("SELECT * FROM decision_log ORDER BY id"))
        finally:
            bus.close()


class LiveBnbNotionalPrefilterTests(unittest.TestCase):
    def test_floor_matches_execution(self) -> None:
        self.assertEqual(ENTRY_MIN_NOTIONAL_USDT, Decimal("20"))
        self.assertEqual(ENTRY_MIN_NOTIONAL_USDT, EXECUTION_ENTRY_MIN_NOTIONAL_USDT)
        self.assertEqual(ENTRY_MIN_NOTIONAL_USDT, LIFT_ENTRY_MIN_NOTIONAL_USDT)

    def setUp(self) -> None:
        self._prev = os.environ.get(_BOOK)

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop(_BOOK, None)
        else:
            os.environ[_BOOK] = self._prev

    def _book(self, value: str | None) -> None:
        if value is None:
            os.environ.pop(_BOOK, None)
        else:
            os.environ[_BOOK] = value

    def _live(self, rows: list[dict]) -> tuple[TechBollFadeStrategy, list[dict]]:
        strategy = TechBollFadeStrategy(position_pct=0.05)
        signals = strategy.on_tick(
            {"hot_tokens": rows, "purpose": PURPOSE_LIVE_BNB_BOLL_FADE}
        )
        return strategy, signals

    def test_lifted_neutral_on_the_prod_book_is_kept(self) -> None:
        # Neutral multiplier stays 0.5, so the planned fraction is 0.025
        # (~10.94 USDT). The lift raises it to just over 20 before this
        # prefilter, so the row is written. Large-trade 0.05 (~21.88) is
        # already clear and is not rewritten.
        self._book("437.64")
        strategy, signals = self._live([_row(0.12, 0.85), _row(0.08, 1.3)])
        self.assertEqual(strategy.position_pct, 0.05)
        self.assertEqual(strategy.rejected.get(_PREFILTER, 0), 0)
        self.assertEqual(len(signals), 2)
        neutral, large = signals
        self.assertEqual(neutral["action"], "buy")
        self.assertEqual(neutral["position_size_multiplier"], 0.5)
        self.assertAlmostEqual(neutral["size_pct_before_min_notional"], 0.025)
        self.assertTrue(neutral["min_notional_lift"])
        self.assertGreater(neutral["size_pct"], 0.025)
        self.assertLess(neutral["size_pct"], 0.05)
        err, quote = _execution_quote(neutral["size_pct"], "437.64")
        self.assertIsNone(err)
        assert quote is not None
        self.assertGreaterEqual(Decimal(quote), Decimal("20"))
        self.assertEqual(large["position_size_multiplier"], 1.0)
        self.assertAlmostEqual(large["size_pct"], 0.05)
        self.assertNotIn("min_notional_lift", large)
        rows = _persist(signals, PURPOSE_LIVE_BNB_BOLL_FADE)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["purpose"], PURPOSE_LIVE_BNB_BOLL_FADE)
        self.assertGreater(float(rows[0]["size_pct"]), 0.025)
        self.assertAlmostEqual(float(rows[1]["size_pct"]), 0.05)

    def test_at_and_above_floor_are_emitted(self) -> None:
        # 0.05 * 400 = 20 exactly. Execution refuses only quote < 20.
        self._book("400")
        strategy, at_floor = self._live([_row(0.08, 1.3)])
        self.assertEqual(strategy.rejected.get(_PREFILTER, 0), 0)
        self.assertEqual(len(at_floor), 1)
        self.assertAlmostEqual(at_floor[0]["size_pct"], 0.05)
        self.assertEqual(len(_persist(at_floor, PURPOSE_LIVE_BNB_BOLL_FADE)), 1)

        # Neutral 0.025 * 800 = 20. Still the 0.5 multiplier, not a size retune.
        self._book("800")
        strategy, neutral_at_floor = self._live([_row(0.12, 0.85)])
        self.assertEqual(len(neutral_at_floor), 1)
        self.assertAlmostEqual(neutral_at_floor[0]["size_pct"], 0.025)
        self.assertEqual(neutral_at_floor[0]["position_size_multiplier"], 0.5)
        self.assertNotIn("min_notional_lift", neutral_at_floor[0])
        self.assertEqual(strategy.rejected.get(_PREFILTER, 0), 0)

        # 0.05 * 399.99 = 19.9995. The lift clears 20, so the prefilter keeps it.
        self._book("399.99")
        strategy, under = self._live([_row(0.08, 1.3)])
        self.assertEqual(strategy.rejected.get(_PREFILTER, 0), 0)
        self.assertEqual(len(under), 1)
        self.assertEqual(under[0]["position_size_multiplier"], 1.0)
        self.assertTrue(under[0]["min_notional_lift"])
        self.assertGreater(under[0]["size_pct"], 0.05)
        err, quote = _execution_quote(under[0]["size_pct"], "399.99")
        self.assertIsNone(err)
        assert quote is not None
        self.assertGreaterEqual(Decimal(quote), Decimal("20"))
        self.assertEqual(len(_persist(under, PURPOSE_LIVE_BNB_BOLL_FADE)), 1)

        # Prod book, large-trade size, above the floor.
        self._book("437.64")
        _, above = self._live([_row(0.08, 1.3)])
        self.assertEqual(len(above), 1)
        self.assertAlmostEqual(above[0]["size_pct"], 0.05)

    def test_missing_or_unparseable_book_fails_open(self) -> None:
        neutral = [_row(0.12, 0.85), _row(0.20, 1.0)]
        for raw in (None, "", "   ", "not-a-book", "0", "-5", "4.3764e2"):
            with self.subTest(book=raw):
                self._book(raw)
                with self.assertLogs(_LOGGER, level="WARNING") as captured:
                    strategy, signals = self._live(neutral)
                self.assertEqual(len(signals), 2, raw)
                self.assertEqual(strategy.rejected.get(_PREFILTER, 0), 0)
                self.assertEqual(strategy.position_pct, 0.05)
                self.assertAlmostEqual(signals[0]["size_pct"], 0.025)
                self.assertEqual(signals[0]["position_size_multiplier"], 0.5)
                self.assertNotIn("min_notional_lift", signals[0])
                warnings = [line for line in captured.output if "WARNING" in line]
                self.assertEqual(len(warnings), 1)
                self.assertIn(_BOOK, warnings[0])
                rows = _persist(signals, PURPOSE_LIVE_BNB_BOLL_FADE)
                self.assertEqual(len(rows), 2)

        self._book("437.64")
        with self.assertNoLogs(_LOGGER, level="WARNING"):
            strategy, signals = self._live([_row(0.12, 0.85)])
        self.assertEqual(len(signals), 1)
        self.assertEqual(strategy.rejected.get(_PREFILTER, 0), 0)
        self.assertEqual(signals[0]["position_size_multiplier"], 0.5)
        self.assertTrue(signals[0]["min_notional_lift"])
        self.assertGreater(signals[0]["size_pct"], 0.025)

    def test_still_under_20_after_the_lift_is_dropped(self) -> None:
        # 1 * 10 = 10. The lift cannot invent a size_pct above 1, so both
        # the neutral 0.025 and the large-trade 0.05 stay under 20 and are
        # not written.
        self._book("10")
        strategy, signals = self._live([_row(0.12, 0.85), _row(0.08, 1.3)])
        self.assertEqual(strategy.position_pct, 0.05)
        self.assertEqual(signals, [])
        self.assertEqual(strategy.rejected.get(_PREFILTER), 2)
        self.assertEqual(len(_persist(signals, PURPOSE_LIVE_BNB_BOLL_FADE)), 0)
        err, _quote = _execution_quote(0.025, "10")
        self.assertEqual(err, "entry_notional_below_min")

    def test_other_purposes_ignore_the_book(self) -> None:
        # 0.05 * 100 = 5, under the live floor. Paper must still be written.
        self._book("100")
        strategy = TechBollFadeStrategy(position_pct=0.05)
        signals = strategy.on_tick(
            {"hot_tokens": [_paper_row()], "purpose": PURPOSE_RACE_TECH_BOLL_FADE}
        )
        self.assertEqual(len(signals), 1)
        self.assertAlmostEqual(signals[0]["size_pct"], 0.05)
        self.assertEqual(signals[0]["and_mode"], "tech_fade&boll_lower")
        self.assertEqual(strategy.rejected.get(_PREFILTER, 0), 0)
        rows = _persist(signals, PURPOSE_RACE_TECH_BOLL_FADE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["purpose"], PURPOSE_RACE_TECH_BOLL_FADE)

        untouched = strategy.on_tick({"hot_tokens": [_paper_row()]})
        self.assertEqual(len(untouched), 1)
        self.assertEqual(strategy.rejected.get(_PREFILTER, 0), 0)


if __name__ == "__main__":
    unittest.main()
