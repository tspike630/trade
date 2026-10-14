"""G-13 high advertised APY is a risk tag, not an opportunity.

Synthetic fixtures only. No live keys, network, or production DBs.
The tag is observe-only: it does not count toward strategy validity.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import risk as risk_mod
from daily_check import G13_CHECK_NAME, CheckReport, check_g13_high_apy
from history import extract_metrics
from risk import (
    G13_HIGH_APY_DEDUCTION,
    G13_HIGH_APY_MIN_PCT,
    G13_HIGH_APY_TAG,
    HeuristicRiskScanner,
    apply_risk_to_score,
    count_g13_tagged_tokens,
    detect_g13_high_apy,
    format_g13_operator_line,
)
from scoring import score_token_from_store
from store import Store
from tools.grok_context import build_context, to_markdown


def _quiet_pair(**overrides: object) -> dict:
    pair: dict = {
        "baseToken": {"address": "mint", "name": "BONK", "symbol": "BONK"},
        "quoteToken": {"address": "sol", "name": "Solana", "symbol": "SOL"},
        "labels": ["v3"],
        "liquidity": {"usd": 120_000},
        "volume": {"h24": 40_000},
        "txns": {
            "h1": {"buys": 40, "sells": 35},
            "h24": {"buys": 80, "sells": 70},
        },
        "priceUsd": "0.01",
        "priceChange": {"h24": 1500},
        "boosts": {"active": 5000},
    }
    pair.update(overrides)
    return pair


class DetectorTests(unittest.TestCase):
    def test_threshold_constant(self) -> None:
        self.assertEqual(G13_HIGH_APY_MIN_PCT, 1000.0)
        self.assertEqual(G13_HIGH_APY_TAG, "risk_g13_high_apy")
        self.assertLess(G13_HIGH_APY_DEDUCTION, 20)

    def test_boundaries_and_claim_shapes(self) -> None:
        cases = {
            "APY 999%": None,
            "APY 1000%": 1000.0,
            "APY 1000.0%": 1000.0,
            "apr: 80%": None,
            "12% APR": None,
            "earn 5,000% APR": 5000.0,
            "1,000,000% APY": 1_000_000.0,
            "年化收益 2000%": 2000.0,
            "年化收益2000％": 2000.0,
            "挖矿年化 5000%": 5000.0,
            "质押 8000%": 8000.0,
            "stake to earn 2,500%": 2500.0,
            "staking rewards: 1500%": 1500.0,
            "10x APY": 1000.0,
            "9x APY": None,
            "2x yield": None,
            "APY 5000": 5000.0,
            "APR 2024 roadmap": None,
            "APY 2000": None,
        }
        for text, expected in cases.items():
            hit = detect_g13_high_apy(None, {"description": text})
            if expected is None:
                self.assertIsNone(hit, text)
            else:
                self.assertIsNotNone(hit, text)
                assert hit is not None
                self.assertEqual(hit["apy_pct"], expected, text)
                self.assertEqual(hit["tag"], G13_HIGH_APY_TAG)

    def test_unlimited_claim(self) -> None:
        hit = detect_g13_high_apy(None, {"description": "unlimited APY if you stake"})
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertIsNone(hit["apy_pct"])
        self.assertTrue(hit["unlimited"])
        self.assertIn("unlimited", hit["evidence"].lower())

    def test_normal_pair_fields_do_not_match(self) -> None:
        pair = _quiet_pair()
        pair["info"] = {
            "description": "Solana meme coin for the community",
            "websites": [{"url": "https://example.com/apy-5000", "label": "Website"}],
            "socials": [{"type": "twitter", "url": "https://x.com/5000apy"}],
        }
        pair["baseToken"]["name"] = "1000X"
        pair["baseToken"]["symbol"] = "MOON"
        self.assertIsNone(detect_g13_high_apy(pair, {"name": "APR", "description": "about 5000 holders"}))
        self.assertIsNone(detect_g13_high_apy(pair, {"description": "up 1000% this week"}))
        self.assertIsNone(detect_g13_high_apy(pair, {"description": "farm members grew 1000%"}))
        self.assertIsNone(detect_g13_high_apy(pair, {"description": "1000 holders farming the community"}))
        self.assertIsNone(detect_g13_high_apy(pair, {"description": "1000% higher APY is 12%"}))
        split = _quiet_pair(labels=["farm"], info={"description": "up 1000% this week"})
        split["baseToken"]["name"] = "Super Farm"
        self.assertIsNone(detect_g13_high_apy(split, None))
        self.assertIsNone(detect_g13_high_apy({}, {}))
        self.assertIsNone(detect_g13_high_apy(None, None))

    def test_social_label_matches_url_does_not(self) -> None:
        quiet = _quiet_pair(
            info={
                "socials": [
                    {"type": "twitter", "url": "https://x.com/foo", "label": "Earn 8000% APR"}
                ]
            }
        )
        hit = detect_g13_high_apy(quiet, None)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit["apy_pct"], 8000.0)

    def test_apy_copy_does_not_invent_liquidity(self) -> None:
        pair = {"info": {"description": "pool advertises 5000% APY"}, "priceUsd": "1"}
        self.assertIsNone(extract_metrics(pair)["liquidity_usd"])
        hit = detect_g13_high_apy(pair, None)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit["apy_pct"], 5000.0)
        self.assertNotIn("liquidity", hit)


class ScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scanner = HeuristicRiskScanner()

    def test_quiet_pair_has_no_g13_tag(self) -> None:
        result = self.scanner.scan("solana", "mint", pair=_quiet_pair(), token_raw={})
        self.assertNotIn(G13_HIGH_APY_TAG, result.tags)
        self.assertEqual(result.deduction, 0.0)
        self.assertNotIn("g13_high_apy", result.flags)

    def test_soft_deduction_does_not_elevate_alone(self) -> None:
        pair = _quiet_pair(info={"description": "Advertised APY 5000%"})
        result = self.scanner.scan("solana", "mint", pair=pair, token_raw={})
        self.assertIn(G13_HIGH_APY_TAG, result.tags)
        self.assertEqual(result.deduction, G13_HIGH_APY_DEDUCTION)
        self.assertEqual(result.flags["g13_apy_pct"], 5000.0)
        self.assertNotIn("risk_elevated", result.tags)
        score, tags = apply_risk_to_score(80.0, ["high_liq"], result)
        self.assertEqual(score, 70.0)
        self.assertIn(G13_HIGH_APY_TAG, tags)
        self.assertNotIn("risk_elevated", tags)

    def test_token_description_only(self) -> None:
        result = self.scanner.scan(
            "solana",
            "mint",
            pair=_quiet_pair(),
            token_raw={"description": "Stake and earn 4,000% APY"},
        )
        self.assertIn(G13_HIGH_APY_TAG, result.tags)
        self.assertEqual(result.flags["g13_apy_pct"], 4000.0)

    def test_tax_claim_stays_a_separate_tag(self) -> None:
        result = self.scanner.scan(
            "solana",
            "mint",
            pair=_quiet_pair(),
            token_raw={"description": "100% tax and 5000% APY"},
        )
        self.assertIn("risk_high_tax", result.tags)
        self.assertIn(G13_HIGH_APY_TAG, result.tags)


class ScorePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = risk_mod._scanner
        risk_mod._scanner = HeuristicRiskScanner()

    def tearDown(self) -> None:
        risk_mod._scanner = self._prev

    def test_tag_lands_on_token_quality(self) -> None:
        self.assertTrue(risk_mod.RISK_ENABLED)
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            calm = "So1anaMintCalm111111111111111111111111111"
            hot = "So1anaMintHotApy1111111111111111111111111"
            store.upsert_token("solana", calm, {"description": "meme coin"}, "dexscreener")
            store.upsert_token(
                "solana",
                hot,
                {"description": "listed yesterday"},
                "dexscreener",
            )
            store.upsert_pair("solana", "paircalm", _quiet_pair(), "dexscreener", calm)
            store.upsert_pair(
                "solana",
                "pairhot",
                _quiet_pair(info={"description": "farm pool 5000% APY"}),
                "dexscreener",
                hot,
            )
            calm_score = score_token_from_store(store, "solana", calm)
            hot_score = score_token_from_store(store, "solana", hot)
            stored = store.get_token_quality("solana", hot)
            store.close()

        self.assertNotIn(G13_HIGH_APY_TAG, calm_score["tags"])
        self.assertEqual(calm_score["risk"]["deduction"], 0.0)
        self.assertIn(G13_HIGH_APY_TAG, hot_score["tags"])
        self.assertEqual(hot_score["risk"]["deduction"], G13_HIGH_APY_DEDUCTION)
        self.assertEqual(
            hot_score["score"],
            round(calm_score["score"] - G13_HIGH_APY_DEDUCTION, 1),
        )
        self.assertNotIn("risk_elevated", hot_score["tags"])
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertIn(G13_HIGH_APY_TAG, stored["quality_tags"])
        self.assertEqual(stored["quality_score"], hot_score["score"])


class ObserveMetricTests(unittest.TestCase):
    def test_count_is_exact_tag_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            store.upsert_token("solana", "tagged", {"description": "x"}, "dexscreener")
            store.upsert_token("solana", "other", {"description": "y"}, "dexscreener")
            store.upsert_token("solana", "near", {"description": "z"}, "dexscreener")
            store.upsert_token("solana", "bad", {"description": "w"}, "dexscreener")
            store.set_token_quality("solana", "tagged", 70, ["high_liq", G13_HIGH_APY_TAG])
            store.set_token_quality("solana", "other", 80, ["high_liq"])
            store.set_token_quality("solana", "near", 60, ["risk_g13_high_apy_extra"])
            store._conn.execute(
                "UPDATE tokens SET quality_tags = ? WHERE token_address = ?",
                ("risk_g13_high_apy not-json", "bad"),
            )
            store._conn.commit()
            self.assertEqual(count_g13_tagged_tokens(store._conn), 1)
            report = CheckReport()
            check_g13_high_apy(store, report)
            empty = Store(Path(tmp) / "empty.db")
            empty_report = CheckReport()
            check_g13_high_apy(empty, empty_report)
            ctx = build_context(store, top_n=5, alerts_n=1, history_pairs=1)
            store.close()
            empty.close()

        self.assertEqual(report.checks[0]["name"], G13_CHECK_NAME)
        self.assertEqual(report.checks[0]["level"], "PASS")
        self.assertEqual(report.checks[0]["metrics"]["tagged_n"], 1)
        self.assertFalse(report.checks[0]["metrics"]["counts_toward_strategy_validity"])
        self.assertTrue(report.checks[0]["metrics"]["observe_only"])
        self.assertEqual(empty_report.checks[0]["level"], "PASS")
        self.assertEqual(empty_report.checks[0]["metrics"]["tagged_n"], 0)
        self.assertEqual(ctx["stats"]["g13_high_apy_n"], 1)

    def test_hourly_line_is_observe_only(self) -> None:
        ctx = {
            "generated_at": "2026-10-14T00:00:00Z",
            "enabled_chains": ["solana"],
            "health": {"ok": True, "message": "OK"},
            "stats": {"pending_enrich": 1, "g13_high_apy_n": 4},
            "hot_liquidity_1h": {"n": 1, "missing_rate": 0.0, "below_min_rate": 0.0, "ge_min_rate": 1.0},
            "top_scores": [],
            "low_scores": [],
            "history_samples": [],
            "recent_alerts": [],
        }
        md = to_markdown(ctx)
        self.assertLess(md.index("Health:"), md.index("Hot liq 1h"))
        self.assertLess(md.index("Hot liq 1h"), md.index("G-13 high APY"))
        self.assertLess(md.index("G-13 high APY"), md.index("## Stats"))
        self.assertIn("tag=risk_g13_high_apy n=4", md)
        self.assertIn("不计有效性", md)
        self.assertIn('"g13_high_apy_n": 4', md)
        missing = dict(ctx)
        missing["stats"] = {}
        self.assertIn("n=n/a", to_markdown(missing))
        self.assertIn("n=n/a", format_g13_operator_line(None))


if __name__ == "__main__":
    unittest.main()
