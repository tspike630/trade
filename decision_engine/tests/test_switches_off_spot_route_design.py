"""Design lock for the spot route when the four new switches are off.

The 2026-11-12 review compared the PR206 router (f87789d) with the installed
router (4d58d398 / 56a2df45) and found admission and size changes while
ENABLE_PERP_SHORT_ENGINE, BINANCE_PERP_SHORT_LIVE_ENABLED,
USE_OPTIMAL_KELLY_MATRIX, and EXPAND_REGIME_OCO_TP were unset. Those changes
are the five-state design committed in 8cf2eea, not a switch leak.

Sources, all in decision_engine/core/regime_strategy_router.py:

- State 3 thin book is an AND. Lines 305-308 reject only when obi_top5 is
  present and <= -0.25 and taker is missing or <= 0.45. A strong taker stays.
- State 3 climax is an AND. Lines 320-322 reject volume_z at or above the
  cold-start 3.8 only when whale_retail_divergence is present and <= 0.65.
- State 1 whale skew. Lines 349-390: whale_div >= 1.35 sizes at 1.08;
  0.45 < whale_div <= 0.72 drops a shallow dip (boll_z > -2.25).
- Switches off keep the multiplier path. Lines 632-642 replace size_pct
  only when Kelly is enabled. Lines 720-724 expand take-profit only when
  Kelly is on or EXPAND_REGIME_OCO_TP is truthy.

杨于 2026-11-12 13:08 HKT 裁定保留这套当前实现，并覆盖 AGENTS.md
原先第 12、14 行的无条件写法。缺 `obi` 或缺 `volume_z` 不因该项拒绝。
巨量但 `whale_retail_divergence` 缺失仍放行。熊向门槛以源码为准：
`0.45 < whale_div <= 0.72`，浅跌是 `boll_z > -2.25`。
"""

from __future__ import annotations

import hashlib
import math
import os
import tempfile
import unittest
from pathlib import Path

from decision_engine.core.news_shock_gate import NewsReadSnapshot, NewsShockStatus
from decision_engine.core.perp_short_alpha_engine import (
    evaluate_directional_short_candidates,
    is_perp_short_engine_enabled,
    resolve_perp_book_usdt,
)
from decision_engine.core.regime_strategy_router import (
    EXPAND_OCO_TP_ENV,
    OPTIMAL_KELLY_ENV,
    REGIME_1_POSITIVE_GAMMA_PINNED,
    REGIME_2_SHORT_SQUEEZE_BREAKOUT,
    REGIME_3_CROSS_ASSET_BULL_PULLBACK,
    REGIME_5_CRISIS_LIQUIDATION_HALT,
    apply_five_state_regime_router,
    evaluate_five_state_regime,
)
from execution.executors.binance_perp_short import BinancePerpShortAdapter
from execution.executors.binance_spot_protect import resolve_entry_size

_ROOT = Path(__file__).resolve().parents[2]
_SWITCHES = (
    "ENABLE_PERP_SHORT_ENGINE",
    "BINANCE_PERP_SHORT_LIVE_ENABLED",
    OPTIMAL_KELLY_ENV,
    EXPAND_OCO_TP_ENV,
    "ENFORCE_REAL_DEMAND_WHITELIST",
    "BINANCE_SPOT_BOOK_USDT",
    "LIVE_TRADING_DISABLED",
)
_OCO = {
    "execution/executors/binance_spot.py": "d8c31acabf25b7b564c86dc56d60bc2d41cf0877e93e6d1552b23b68a41659bf",
    "execution/executors/binance_spot_gate.py": "f7e2e3c63d42ad606516c9fc73bc822b868eba8686874248192671f03595cf38",
    "execution/executors/binance_spot_protect.py": "6686b214b2cc2b028f6b17c686107368d16a13462f61ad8953b211231874aa1f",
}
_NEWS = "41f94998d779b036be36363e831abac3d4c3a7c6997d272d3c281e22742b92ee"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bull(**extra: object) -> dict[str, object]:
    row: dict[str, object] = {"xs_breadth_1h": 0.75}
    row.update(extra)
    return row


def _route(hot: dict[str, object], *, size: float = 0.05, take_profit: float | None = None) -> list[dict]:
    signal: dict[str, object] = {
        "action": "buy",
        "binance_symbol": "SOLUSDT",
        "size_pct": size,
        "order_type": "MARKET",
        "price_usd": 100.0,
    }
    if take_profit is not None:
        signal["take_profit_price"] = take_profit
    out, _counts = apply_five_state_regime_router([signal], [{"binance_symbol": "SOLUSDT", **hot}])
    return out


class SwitchesOffSpotRouteDesignTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in _SWITCHES}
        for key in _SWITCHES:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_four_switches_off_matches_the_five_state_comments(self) -> None:
        ordinary = _route(_bull())
        self.assertEqual(len(ordinary), 1)
        self.assertEqual(ordinary[0]["market_regime"], REGIME_3_CROSS_ASSET_BULL_PULLBACK)
        self.assertEqual(ordinary[0]["recommended_order_type"], "LIMIT_MAKER")
        self.assertAlmostEqual(ordinary[0]["size_pct"], 0.0575, places=6)
        self.assertNotIn("regime_tp_expanded", ordinary[0])

        strong_taker = _route(_bull(obi_top5=-0.40, taker_buy_ratio=0.60))
        self.assertEqual(len(strong_taker), 1)
        self.assertEqual(strong_taker[0]["recommended_order_type"], "LIMIT_MAKER")
        self.assertAlmostEqual(strong_taker[0]["size_pct"], 0.0575, places=6)

        accumulating = _route(_bull(volume_z=4.2, whale_retail_divergence=1.0))
        self.assertEqual(len(accumulating), 1)
        self.assertEqual(accumulating[0]["recommended_order_type"], "LIMIT_MAKER")

        distributing = _route(_bull(volume_z=4.2, whale_retail_divergence=0.40))
        self.assertEqual(distributing, [])

        whale_bull = _route({"whale_retail_divergence": 1.50})
        self.assertEqual(len(whale_bull), 1)
        self.assertEqual(whale_bull[0]["market_regime"], REGIME_1_POSITIVE_GAMMA_PINNED)
        self.assertEqual(whale_bull[0]["regime_playbook"], "tech_boll_fade_whale_bull_skew")
        self.assertAlmostEqual(whale_bull[0]["size_pct"], 0.054, places=6)
        self.assertEqual(whale_bull[0]["recommended_order_type"], "LIMIT_MAKER")

        shallow = _route({"whale_retail_divergence": 0.60, "boll_z": -1.0})
        self.assertEqual(shallow, [])

    def test_yang_2026_11_12_1308_hkt_boundaries_with_switches_off(self) -> None:
        for key in (
            "ENABLE_PERP_SHORT_ENGINE",
            "BINANCE_PERP_SHORT_LIVE_ENABLED",
            OPTIMAL_KELLY_ENV,
            EXPAND_OCO_TP_ENV,
        ):
            self.assertNotIn(key, os.environ)
        bull = {"xs_breadth_1h": 0.75, "nvda_1h_ret_pct": 1.60, "xs_rank_pct_1h": 0.85}
        above_obi = math.nextafter(-0.25, 0.0)
        above_taker = math.nextafter(0.45, 1.0)
        below_z = math.nextafter(3.8, 0.0)
        above_whale = math.nextafter(0.65, 1.0)
        thin_cases = (
            ({"obi_top5": -0.25}, False),
            ({"obi_top5": -0.25, "taker_buy_ratio": 0.45}, False),
            ({"obi_top5": -0.25, "taker_buy_ratio": above_taker}, True),
            ({"obi_top5": above_obi, "taker_buy_ratio": 0.45}, True),
            ({"taker_buy_ratio": 0.20}, True),
        )
        for extra, allow in thin_cases:
            decision = evaluate_five_state_regime({**bull, **extra})
            self.assertEqual(decision.allow_buy, allow, extra)
        volume_cases = (
            ({"volume_z": 3.8}, True),
            ({"volume_z": 4.2}, True),
            ({"volume_z": 3.8, "whale_retail_divergence": 0.65}, False),
            ({"volume_z": 4.2, "whale_retail_divergence": 0.65}, False),
            ({"volume_z": 3.8, "whale_retail_divergence": above_whale}, True),
            ({"volume_z": below_z, "whale_retail_divergence": 0.40}, True),
            ({"whale_retail_divergence": 0.40}, True),
        )
        for extra, allow in volume_cases:
            decision = evaluate_five_state_regime({**bull, **extra})
            self.assertEqual(decision.allow_buy, allow, extra)

        just_under_bull = math.nextafter(1.35, 0.0)
        just_over_bear_low = math.nextafter(0.45, 1.0)
        just_over_bear_high = math.nextafter(0.72, 1.0)
        just_shallower = math.nextafter(-2.25, 0.0)
        bull_row = _route({"whale_retail_divergence": 1.35})
        self.assertEqual(bull_row[0]["regime_playbook"], "tech_boll_fade_whale_bull_skew")
        self.assertAlmostEqual(bull_row[0]["size_pct"], 0.054, places=6)
        self.assertAlmostEqual(bull_row[0]["regime_size_multiplier"], 1.08, places=6)
        capped = _route({"whale_retail_divergence": 1.35, "funding_z": 0.80})
        self.assertEqual(capped[0]["regime_playbook"], "tech_boll_fade_whale_bull_skew")
        hotter = _route({"whale_retail_divergence": 1.35, "funding_z": math.nextafter(0.80, 1.0)})
        self.assertEqual(hotter[0]["regime_playbook"], "tech_boll_fade_standard")
        self.assertAlmostEqual(hotter[0]["size_pct"], 0.05, places=6)
        plain = _route({"whale_retail_divergence": just_under_bull})
        self.assertEqual(plain[0]["regime_playbook"], "tech_boll_fade_standard")

        at_low = _route({"whale_retail_divergence": 0.45, "boll_z": -1.0})
        self.assertEqual(at_low[0]["regime_playbook"], "tech_boll_fade_standard")
        self.assertEqual(_route({"whale_retail_divergence": just_over_bear_low, "boll_z": just_shallower}), [])
        self.assertEqual(_route({"whale_retail_divergence": 0.72, "boll_z": -1.0}), [])
        self.assertEqual(
            _route({"whale_retail_divergence": just_over_bear_high, "boll_z": -1.0})[0]["regime_playbook"],
            "tech_boll_fade_standard",
        )
        on_line = _route({"whale_retail_divergence": 0.60, "boll_z": -2.25})
        self.assertEqual(on_line[0]["regime_playbook"], "tech_boll_fade_whale_bear_skew")
        self.assertAlmostEqual(on_line[0]["size_pct"], 0.0425, places=6)
        self.assertAlmostEqual(on_line[0]["regime_size_multiplier"], 0.85, places=6)
        missing_boll = _route({"whale_retail_divergence": 0.60})
        self.assertEqual(missing_boll[0]["regime_playbook"], "tech_boll_fade_whale_bear_skew")
        self.assertAlmostEqual(missing_boll[0]["size_pct"], 0.0425, places=6)
        deep = _route({"whale_retail_divergence": 0.60, "boll_z": -2.45})
        self.assertAlmostEqual(deep[0]["size_pct"], 0.0425, places=6)

        kept = _route(_bull(volume_z=4.2), take_profit=103.0)
        self.assertAlmostEqual(kept[0]["size_pct"], 0.0575, places=6)
        self.assertEqual(kept[0]["take_profit_price"], 103.0)
        self.assertNotIn("regime_tp_expanded", kept[0])
        _extras, reason, _plan = resolve_entry_size(
            {"action": "buy", "size_pct": 0.05},
            {},
            environ={},
        )
        self.assertEqual(reason, "missing_book_usdt")

    def test_each_switch_changes_only_its_own_boundary(self) -> None:
        os.environ[OPTIMAL_KELLY_ENV] = "1"
        kelly = _route({"funding_z": -2.1}, take_profit=103.0)
        self.assertEqual(kelly[0]["market_regime"], REGIME_2_SHORT_SQUEEZE_BREAKOUT)
        self.assertAlmostEqual(kelly[0]["size_pct"], 0.45, places=6)
        self.assertTrue(kelly[0]["regime_tp_expanded"])
        os.environ.pop(OPTIMAL_KELLY_ENV, None)

        os.environ[EXPAND_OCO_TP_ENV] = "1"
        expanded = _route(_bull(), take_profit=103.0)
        self.assertAlmostEqual(expanded[0]["size_pct"], 0.0575, places=6)
        self.assertTrue(expanded[0]["regime_tp_expanded"])
        self.assertGreater(float(expanded[0]["take_profit_price"]), 103.0)
        os.environ.pop(EXPAND_OCO_TP_ENV, None)

        self.assertFalse(is_perp_short_engine_enabled())
        os.environ["ENABLE_PERP_SHORT_ENGINE"] = "1"
        self.assertTrue(is_perp_short_engine_enabled())
        os.environ.pop("ENABLE_PERP_SHORT_ENGINE", None)

        self.assertIsNone(resolve_perp_book_usdt(None))
        self.assertEqual(
            evaluate_directional_short_candidates(
                [{"binance_symbol": "FETUSDT", "price_usd": 1.5, "nvda_1h_ret_pct": -3.0, "taker_buy_ratio": 0.2}],
                book_usdt=None,
            ),
            [],
        )
        with tempfile.TemporaryDirectory() as tmp:
            blocked = BinancePerpShortAdapter(Path(tmp) / "ledger.db", dry_run=False, book_usdt=None)
            self.assertTrue(blocked.dry_run)
            os.environ["BINANCE_PERP_SHORT_LIVE_ENABLED"] = "1"
            os.environ["LIVE_TRADING_DISABLED"] = "0"
            opened = BinancePerpShortAdapter(Path(tmp) / "live.db", dry_run=False, book_usdt=320.0)
            self.assertFalse(opened.dry_run)

    def test_whitelist_off_keeps_speculative_state1_and_news_crisis_still_blocks(self) -> None:
        speculative = _route({})
        self.assertEqual(len(speculative), 1)
        self.assertEqual(speculative[0]["conviction_tier"], "S")
        meme, _counts = apply_five_state_regime_router(
            [{"action": "buy", "binance_symbol": "PEPEUSDT", "size_pct": 0.05, "price_usd": 0.01}],
            [{"binance_symbol": "PEPEUSDT"}],
        )
        self.assertEqual(len(meme), 1)
        self.assertEqual(meme[0]["conviction_tier"], "SPECULATIVE")
        self.assertAlmostEqual(meme[0]["size_pct"], 0.05, places=6)

        os.environ["ENFORCE_REAL_DEMAND_WHITELIST"] = "1"
        blocked, _counts = apply_five_state_regime_router(
            [{"action": "buy", "binance_symbol": "PEPEUSDT", "size_pct": 0.05, "price_usd": 0.01}],
            [{"binance_symbol": "PEPEUSDT"}],
        )
        self.assertEqual(blocked, [])

        snap = NewsReadSnapshot(
            mode="ro",
            availability="ok",
            detail=None,
            db_path=None,
            as_of="2026-11-12T04:00:00Z",
            window_min=90,
            shocks=(
                NewsShockStatus(
                    severity="crisis",
                    matched_keyword="default",
                    headline="exchange halt",
                    source="test",
                    age_min=1.0,
                ),
            ),
            context_count=0,
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
        halted, counts = apply_five_state_regime_router(
            [{"action": "buy", "binance_symbol": "SOLUSDT", "size_pct": 0.05}],
            [{"binance_symbol": "SOLUSDT", "xs_breadth_1h": 0.75}],
            news_snapshot=snap,
        )
        self.assertEqual(halted, [])
        self.assertGreater(counts.get(REGIME_5_CRISIS_LIQUIDATION_HALT, 0), 0)

    def test_spot_oco_and_news_bytes_stay_on_the_accepted_hashes(self) -> None:
        for rel, expect in _OCO.items():
            path = _ROOT / rel
            self.assertTrue(path.is_file(), rel)
            self.assertEqual(_sha(path), expect, rel)
            self.assertNotIn(b"wall_mid_price", path.read_bytes())
            self.assertNotIn(b"recommended_maker_buy_price", path.read_bytes())
        news = _ROOT / "decision_engine/core/news_shock_gate.py"
        self.assertEqual(_sha(news), _NEWS)


if __name__ == "__main__":
    unittest.main()
