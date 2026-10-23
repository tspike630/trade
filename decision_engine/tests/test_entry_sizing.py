"""Thick-pool buy size: 1.0 only when every condition holds, else <= 0.05."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_CRAWLER = Path(__file__).resolve().parents[2] / "crawler"
if str(_CRAWLER) not in sys.path:
    sys.path.insert(0, str(_CRAWLER))

from indicators.shadow_stop_prone import eval_r3

from decision_engine import config as de_config
from decision_engine.strategies import get_strategy
from decision_engine.strategies.entry_sizing import (
    ThickPoolThresholds,
    buy_size_fields,
    pool_age_days,
    r3_reject,
    resolve_entry_size,
    thresholds_from_config,
)
from decision_engine.strategies.factor_strategy import FactorStrategy
from decision_engine.strategies.jev_strategy import JevStrengthStrategy, jev_age_min

NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)
DAY_MS = 86_400_000
CREATED_MS = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


def _thick(**overrides: object) -> dict:
    row = {
        "chain_id": "solana",
        "token_address": "ThickMint",
        "primary_pair_address": "pair",
        "liquidity_usd": 150_000.0,
        "pair_created_at": CREATED_MS,
        "volatility_24h": 1.0,
        "change_1h": 0.5,
        "rsi_14": 50.0,
        "jev_strength": 0.15,
        "jev_signal": "bullish",
        "jev_score": 70.0,
        "jev_stance": "BULLISH",
        "jev_updated_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    row.update(overrides)
    return row


def _strategy_thick(**overrides: object) -> dict:
    """Same row as ``_thick``, with a JEV stamp fresh against ``jev_gate``'s wall clock.

    ResolveTests keep the frozen ``NOW`` stamp. Strategy ``on_tick`` calls
    ``jev_gate``, which treats ``jev_updated_at`` older than ``max_age_min``
    (1440) as ``jev_stale``.
    """
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    merged: dict[str, object] = {"jev_updated_at": fresh}
    merged.update(overrides)
    return _thick(**merged)


class ThresholdTests(unittest.TestCase):
    def test_config_wires_draft_defaults(self) -> None:
        th = thresholds_from_config()
        self.assertEqual(th.min_liq_usd, de_config.THICK_MIN_LIQ_USD)
        self.assertEqual(th.min_pool_age_days, de_config.THICK_MIN_POOL_AGE_DAYS)
        self.assertEqual(th.min_jev_strength, de_config.THICK_MIN_JEV_STRENGTH)
        self.assertEqual(th.full_size_pct, de_config.THICK_POOL_SIZE_PCT)
        self.assertEqual(th.ordinary_size_pct, de_config.ORDINARY_SIZE_PCT)
        self.assertEqual(th.strict_min_liq_usd, de_config.THICK_STRICT_MIN_LIQ_USD)
        drafted = ThickPoolThresholds()
        self.assertEqual(drafted.min_liq_usd, 150_000.0)
        self.assertEqual(drafted.min_pool_age_days, 7.0)
        self.assertEqual(drafted.r3_vol_thr, 2.5)
        self.assertEqual(drafted.r3_rsi_lo, 30.0)
        self.assertEqual(drafted.r3_rsi_hi, 70.0)
        self.assertEqual(drafted.min_jev_strength, 0.15)
        self.assertEqual(drafted.full_size_pct, 1.0)
        self.assertEqual(drafted.ordinary_size_pct, 0.05)
        self.assertIsNone(drafted.strict_min_liq_usd)

    def test_r3_matches_shadow_eval(self) -> None:
        cases = [
            (None, -1.0, 10.0),
            (2.5, -1.0, 80.0),
            (2.5000001, -0.01, 50.0),
            (3.0, 0.0, 50.0),
            (3.0, None, None),
            (3.0, 1.0, 30.0),
            (3.0, 1.0, 29.999),
            (3.0, 1.0, 70.0),
            (3.0, 1.0, 69.999),
            (3.0, -0.01, None),
            (3.0, None, 80.0),
        ]
        for vol, chg, rsi in cases:
            self.assertEqual(
                r3_reject(vol, chg, rsi),
                eval_r3(vol, chg, rsi),
                (vol, chg, rsi),
            )


class ResolveTests(unittest.TestCase):
    def test_all_conditions_are_full_size(self) -> None:
        decision = resolve_entry_size(_thick(), floor_pct=0.05, now=NOW)
        self.assertAlmostEqual(decision.size_pct, 1.0)
        self.assertEqual(decision.tier, "thick_pool")
        self.assertEqual(decision.r3, 0)
        self.assertEqual(decision.blockers, ())

    def test_boundaries_hold(self) -> None:
        created = int((NOW - timedelta(days=7)).timestamp() * 1000)
        decision = resolve_entry_size(
            _thick(liquidity_usd=150_000, pair_created_at=created, jev_strength=0.15, volatility_24h=2.5),
            floor_pct=0.05,
            now=NOW,
        )
        self.assertEqual(decision.tier, "thick_pool")
        self.assertAlmostEqual(pool_age_days({"pair_created_at": created}, NOW) or -1, 7.0, places=6)

    def test_each_miss_stays_at_floor(self) -> None:
        misses = {
            "liq_below_thick": _thick(liquidity_usd=149_999.99),
            "liq_missing": _thick(liquidity_usd=None),
            "pool_age_below": _thick(pool_age_days=6.999),
            "pool_age_missing": _thick(),
            "r3": _thick(volatility_24h=2.51, change_1h=-0.1, rsi_14=50),
            "jev_below": _thick(jev_strength=0.149),
            "jev_missing": _thick(jev_strength=None),
        }
        # pool_age_missing: drop the creation stamp after _thick set it.
        misses["pool_age_missing"].pop("pair_created_at")
        for name, row in misses.items():
            decision = resolve_entry_size(row, floor_pct=0.05, now=NOW)
            self.assertAlmostEqual(decision.size_pct, 0.05, msg=name)
            self.assertEqual(decision.tier, "ordinary", name)
            self.assertIn(name, decision.blockers, name)
            self.assertLessEqual(decision.size_pct, 0.05)

    def test_ordinary_is_not_raised_to_quarter_or_full(self) -> None:
        decision = resolve_entry_size(_thick(liquidity_usd=80_000), floor_pct=0.25, now=NOW)
        self.assertAlmostEqual(decision.size_pct, 0.05)
        self.assertNotAlmostEqual(decision.size_pct, 0.25)
        self.assertNotAlmostEqual(decision.size_pct, 1.0)
        smaller = resolve_entry_size(_thick(liquidity_usd=80_000), floor_pct=0.02, now=NOW)
        self.assertAlmostEqual(smaller.size_pct, 0.02)

    def test_rsi_and_vol_edges(self) -> None:
        # rsi == 30 is not an extreme; vol == 2.5 does not clear the strict >.
        hold = resolve_entry_size(
            _thick(volatility_24h=3.0, change_1h=0.0, rsi_14=30.0),
            floor_pct=0.05,
            now=NOW,
        )
        self.assertEqual(hold.tier, "thick_pool")
        fired = resolve_entry_size(
            _thick(volatility_24h=3.0, change_1h=1.0, rsi_14=70.0),
            floor_pct=0.05,
            now=NOW,
        )
        self.assertEqual(fired.tier, "ordinary")
        self.assertEqual(fired.r3, 1)
        self.assertIn("r3", fired.blockers)

    def test_aliases_and_strict_tier(self) -> None:
        row = _thick()
        row.pop("liquidity_usd")
        row.pop("volatility_24h")
        row.pop("change_1h")
        row.pop("rsi_14")
        row.pop("pair_created_at")
        row.update(
            {
                "liq_usd": 200_000,
                "vol_24h": 1.0,
                "chg_1h": 0.2,
                "rsi": 45,
                "pool_age_min": 8 * 24 * 60,
            }
        )
        decision = resolve_entry_size(row, floor_pct=0.05, now=NOW)
        self.assertEqual(decision.tier, "thick_pool")

        strict = ThickPoolThresholds(strict_min_liq_usd=500_000.0)
        below = resolve_entry_size(_thick(liquidity_usd=200_000), floor_pct=0.05, now=NOW, thresholds=strict)
        self.assertEqual(below.tier, "ordinary")
        self.assertIn("liq_below_strict", below.blockers)
        at = resolve_entry_size(_thick(liquidity_usd=500_000), floor_pct=0.05, now=NOW, thresholds=strict)
        self.assertEqual(at.tier, "thick_pool")

    def test_supply_gaps_loader_is_not_wired(self) -> None:
        src = Path(buy_size_fields.__code__.co_filename).read_text(encoding="utf-8")
        self.assertIn("supply_gaps", src)
        self.assertNotIn("load_supply_gaps", src)
        self.assertIn("step2_blocked", src)


class StrategyOutputTests(unittest.TestCase):
    def test_fixture_stamp_stays_inside_jev_window(self) -> None:
        age = jev_age_min(_strategy_thick())
        self.assertIsNotNone(age)
        assert age is not None
        self.assertLess(age, 2.0)

    def test_jev_full_only_on_thick_pool(self) -> None:
        strategy = JevStrengthStrategy(min_jev_strength=0.15, position_pct=0.05)
        full = strategy.on_tick({"hot_tokens": [_strategy_thick(jev_strength=0.4)]})
        self.assertEqual(len(full), 1)
        self.assertAlmostEqual(full[0]["size_pct"], 1.0)
        self.assertEqual(full[0]["size_tier"], "thick_pool")
        self.assertEqual(full[0]["entry_r3"], 0)

        ordinary = strategy.on_tick({"hot_tokens": [_strategy_thick(jev_strength=0.4, liquidity_usd=60_000)]})
        self.assertEqual(len(ordinary), 1)
        self.assertAlmostEqual(ordinary[0]["size_pct"], 0.05)
        self.assertLessEqual(ordinary[0]["size_pct"], 0.05)
        self.assertEqual(ordinary[0]["size_tier"], "ordinary")

        capped = JevStrengthStrategy(min_jev_strength=0.15, position_pct=0.25)
        raised = capped.on_tick({"hot_tokens": [_strategy_thick(jev_strength=0.4, pool_age_days=1)]})
        self.assertAlmostEqual(raised[0]["size_pct"], 0.05)

    def test_stale_jev_still_emits_nothing(self) -> None:
        strategy = JevStrengthStrategy(min_jev_strength=0.15, max_age_min=1440, require_bullish_stance=True)
        row = _strategy_thick(jev_strength=0.9, jev_age_min=None)
        self.assertEqual(strategy.on_tick({"hot_tokens": [row]}), [])
        self.assertEqual(strategy.rejected["jev_stale"], 1)

    def test_factor_twin_and_tech(self) -> None:
        factor = get_strategy("factor_jev_strength_v1", min_jev_strength=0.15, position_pct=0.05)
        out = factor.on_tick({"hot_tokens": [_strategy_thick(jev_strength=0.4)]})
        self.assertAlmostEqual(out[0]["size_pct"], 1.0)
        thin = factor.on_tick({"hot_tokens": [_strategy_thick(jev_strength=0.4, liquidity_usd=None)]})
        self.assertAlmostEqual(thin[0]["size_pct"], 0.05)

        tech = get_strategy("tech_momentum_v1", position_pct=0.05)
        tech_row = _strategy_thick(change_5m=3.0, volume_strength=0.8, jev_strength=None)
        missed = tech.on_tick({"hot_tokens": [tech_row]})
        self.assertEqual(len(missed), 1)
        self.assertAlmostEqual(missed[0]["size_pct"], 0.05)
        self.assertIn("jev_missing", missed[0]["size_blockers"])
        tech_full = tech.on_tick(
            {"hot_tokens": [_strategy_thick(change_5m=3.0, volume_strength=0.8, jev_strength=0.2)]}
        )
        self.assertAlmostEqual(tech_full[0]["size_pct"], 1.0)

    def test_sell_size_is_not_resized(self) -> None:
        strat = FactorStrategy(
            strategy_id="factor_test_sell",
            buy_factors=["jev_strength"],
            sell_factors=["time_exit"],
            sell_params={"time_exit": {"max_hold_seconds": 10}},
        )
        out = strat.on_tick(
            {
                "hot_tokens": [],
                "now_ts": 1_000,
                "open_positions": [
                    {
                        "chain_id": "solana",
                        "token_address": "tok",
                        "pair_address": "pair",
                        "opened_ts": 0,
                        "size_pct": 1.0,
                    }
                ],
            }
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["action"], "sell")
        self.assertAlmostEqual(out[0]["size_pct"], 1.0)
        self.assertNotIn("size_tier", out[0])


if __name__ == "__main__":
    unittest.main()
