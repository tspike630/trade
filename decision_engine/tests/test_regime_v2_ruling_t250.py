"""t250u draft contract. Defaults stay. Unimplemented rulings stay blockers."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from decision_engine.core.regime_strategy_router import (
    apply_five_state_regime_router,
)
from decision_engine.core.regime_v2_acceptance import parse_env_file, profile_blocks, profile_path
from decision_engine.strategies.funding_z_fade import FUNDING_Z_ABS_MIN, STRATEGY_ID

_ROOT = Path(__file__).resolve().parents[2]


def _buy(hot: dict[str, object]) -> dict:
    out, _counts = apply_five_state_regime_router(
        [
            {
                "action": "buy",
                "strategy_id": "tech_boll_fade_v1",
                "binance_symbol": "SOLUSDT",
                "size_pct": 0.24,
                "order_type": "MARKET",
                "price_usd": 100.0,
                "take_profit_price": 110.0,
                "stop_price": 90.0,
            }
        ],
        [{"binance_symbol": "SOLUSDT", **hot}],
    )
    return out[0]


class RulingDraftTests(unittest.TestCase):
    def test_draft_records_the_ruling_without_claiming_reanchor_compliance(self) -> None:
        env = parse_env_file(profile_path())
        self.assertEqual(profile_blocks(env), [])
        self.assertEqual(env["REGIME_V2_POSITION_PCT"], "0.24")
        self.assertEqual(env["REGIME_V2_POSITION_STATUS"], "live_cli_fact_not_a_new_approval")
        self.assertEqual(env["BINANCE_SPOT_TRAILING_RATCHET"], "0")
        self.assertEqual(env["BINANCE_SPOT_MAX_HOLD_SEC"], "0")
        self.assertEqual(env["BINANCE_SPOT_AUTO_FLATTEN_ON_OCO_FAIL"], "1")
        self.assertEqual(env["BINANCE_SPOT_REANCHOR_OCO_ON_FILL"], "0")
        self.assertEqual(env["REGIME_V2_REANCHOR_RULING"], "1")
        self.assertEqual(env["REGIME_V2_REANCHOR_STATUS"], "blocked_behavior_mismatch")
        self.assertEqual(env["ENABLE_PERP_SHORT_ENGINE"], "0")
        self.assertEqual(env["REGIME_V2_STATE4_SIZE_MULTIPLIER"], "0.30")
        self.assertEqual(env["REGIME_V2_STATE2_TP_MULTIPLIER"], "1.35")
        self.assertEqual(env["REGIME_V2_STATE3_TP_MULTIPLIER"], "1.25")
        self.assertEqual(env["REGIME_V2_STATE4_TP_MULTIPLIER"], "0.85")
        self.assertEqual(env["REGIME_V2_STATE2_ENTRY_STATUS"], "module_not_called")
        self.assertEqual(env["REGIME_V2_STATE4_ENTRY_STATUS"], "deep_tier_undefined")
        self.assertEqual(env["REGIME_V2_STALE_DATA_POLICY"], "fail_closed_not_implemented")
        self.assertNotIn("591.", "\n".join(f"{k}={v}" for k, v in env.items()))

    def test_router_does_not_read_the_draft_multipliers(self) -> None:
        source = (_ROOT / "decision_engine/core/regime_strategy_router.py").read_text(encoding="utf-8")
        self.assertNotIn("REGIME_V2_STATE4_SIZE_MULTIPLIER", source)
        tree = ast.parse(source)
        assigned = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == 0.55:
                assigned.append(node.lineno)
        self.assertTrue(assigned)

    def test_unset_env_keeps_state4_at_055_and_state1_whale_branch(self) -> None:
        quiet = {
            "funding_rate": 0.0001,
            "funding_z": 0.1,
            "whale_retail_divergence": 0.50,
            "global_account_ls_ratio": 2.5,
            "gamma_regime": "positive_gamma",
            "dvol_zscore": 0.1,
            "liq_cascade_regime": "normal",
            "max_venue_dislocation_bps": 1.0,
            "cot_regime": "neutral",
            "nvda_1h_ret_pct": 0.1,
            "smh_1h_ret_pct": 0.1,
            "xs_breadth_1h": 0.40,
            "in_unlock_blackout": 0,
            "xs_systemic_crash": False,
        }
        state4 = _buy(quiet)
        self.assertAlmostEqual(state4["size_pct"], 0.132, places=6)
        self.assertAlmostEqual(state4["regime_size_multiplier"], 0.55, places=6)
        self.assertAlmostEqual(state4["take_profit_price"], 110.0, places=6)
        self.assertAlmostEqual(state4["regime_tp_multiplier"], 0.80, places=6)
        whale = _buy({**quiet, "global_account_ls_ratio": 1.2, "whale_retail_divergence": 1.40})
        self.assertEqual(whale["regime_playbook"], "tech_boll_fade_whale_bull_skew")
        self.assertAlmostEqual(whale["size_pct"], 0.2592, places=6)
        self.assertAlmostEqual(whale["take_profit_price"], 110.0, places=6)

    def test_missing_features_still_fall_through_to_state1(self) -> None:
        row = _buy({})
        self.assertEqual(row["regime_playbook"], "tech_boll_fade_standard")
        self.assertTrue(row["regime_classification"]["state1_unconfirmed"])

    def test_funding_fade_module_is_not_the_live_emitter(self) -> None:
        self.assertEqual(STRATEGY_ID, "funding_z_fade_v1")
        self.assertEqual(FUNDING_Z_ABS_MIN, 2.0)
        script = (_ROOT / "scripts/regime_v2_kill_dry_run.sh").read_text(encoding="utf-8")
        self.assertIn("--strategy tech_boll_fade_v1", script)
        self.assertNotIn("funding_z_fade_v1", script)
        agents = (_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("module_not_called", agents)
        self.assertIn("blocked_behavior_mismatch", agents)
