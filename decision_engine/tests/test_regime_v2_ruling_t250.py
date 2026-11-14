"""t250u draft contract. Defaults stay. Unimplemented rulings stay blockers."""

from __future__ import annotations

import ast
import os
import unittest
from pathlib import Path

from decision_engine.core.regime_strategy_router import (
    CONTRACT_ENV,
    _apply_v2_contract,
    apply_five_state_regime_router,
    evaluate_five_state_regime,
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
        self.assertEqual(env["REGIME_V2_CONTRACT"], "1")
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
        self.assertIn("state2_entry_module_not_live", agents)
        self.assertIn("blocked_behavior_mismatch", agents)


def _full(**extra: object) -> dict[str, object]:
    row: dict[str, object] = {
        "funding_rate": 0.0001,
        "funding_z": 0.1,
        "whale_retail_divergence": 1.0,
        "global_account_ls_ratio": 1.2,
        "gamma_regime": "positive_gamma",
        "dvol_zscore": 0.1,
        "liq_cascade_regime": "normal",
        "max_venue_dislocation_bps": 1.0,
        "cot_regime": "neutral",
        "nvda_1h_ret_pct": 0.1,
        "smh_1h_ret_pct": 0.1,
        "xs_breadth_1h": 0.40,
        "xs_rank_pct_1h": 0.80,
        "obi_top5": 0.1,
        "taker_buy_ratio": 0.55,
        "volume_z": 0.4,
        "in_unlock_blackout": 0,
        "xs_systemic_crash": False,
    }
    row.update(extra)
    return row


class ContractWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.get(CONTRACT_ENV)
        os.environ[CONTRACT_ENV] = "1"

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop(CONTRACT_ENV, None)
        else:
            os.environ[CONTRACT_ENV] = self._saved

    def test_state1_whale_branch_does_not_change_size_or_tp_price(self) -> None:
        bull = _buy(_full(whale_retail_divergence=1.40, funding_z=0.2))
        self.assertEqual(bull["regime_playbook"], "tech_boll_fade_standard")
        self.assertAlmostEqual(bull["size_pct"], 0.24, places=6)
        self.assertAlmostEqual(bull["regime_size_multiplier"], 1.0, places=6)
        self.assertAlmostEqual(bull["regime_tp_multiplier"], 1.0, places=6)
        self.assertAlmostEqual(bull["take_profit_price"], 110.0, places=6)
        self.assertFalse(bull["require_perp_beta_hedge"])
        self.assertFalse(bull["enable_trailing_ratchet"])
        self.assertEqual(bull["tp_price_policy"], "unchanged_until_fill_formula_approved")
        shallow = _buy(_full(whale_retail_divergence=0.60, boll_z=-1.0))
        self.assertEqual(shallow["regime_playbook"], "tech_boll_fade_standard")
        self.assertAlmostEqual(shallow["size_pct"], 0.24, places=6)

    def test_missing_inputs_do_not_default_to_a_state1_buy(self) -> None:
        out, _counts = apply_five_state_regime_router(
            [{
                "action": "buy",
                "strategy_id": "tech_boll_fade_v1",
                "binance_symbol": "SHELLUSDT",
                "size_pct": 0.24,
                "take_profit_price": 1.0,
                "price_usd": 1.0,
            }],
            [{"binance_symbol": "SHELLUSDT"}],
        )
        self.assertEqual(out, [])
        decision = _apply_v2_contract(
            evaluate_five_state_regime({}),
            {"missing": ["funding_z", "cot_regime"], "inputs": {}},
        )
        self.assertFalse(decision.allow_buy)
        self.assertEqual(decision.reject_reason, "regime_required_inputs_missing")

    def test_state3_uses_115_and_125_without_rewriting_tp_price(self) -> None:
        routed = _buy(_full(xs_breadth_1h=0.75, xs_rank_pct_1h=0.90))
        self.assertEqual(routed["market_regime"], "cross_asset_momentum_pullback")
        self.assertAlmostEqual(routed["size_pct"], 0.276, places=6)
        self.assertAlmostEqual(routed["regime_tp_multiplier"], 1.25, places=6)
        self.assertAlmostEqual(routed["take_profit_price"], 110.0, places=6)
        self.assertFalse(routed["require_perp_beta_hedge"])

    def test_state2_and_state4_routes_stay_closed(self) -> None:
        squeeze = evaluate_five_state_regime(_full(funding_z=-1.8))
        closed = _apply_v2_contract(squeeze, {"missing": [], "inputs": {}})
        self.assertFalse(closed.allow_buy)
        self.assertEqual(closed.reject_reason, "state2_entry_module_not_live")
        self.assertAlmostEqual(closed.size_multiplier, 0.0, places=6)
        self.assertAlmostEqual(closed.tp_multiplier, 1.35, places=6)
        crowded = evaluate_five_state_regime(
            _full(global_account_ls_ratio=2.5, whale_retail_divergence=0.50)
        )
        defensive = _apply_v2_contract(crowded, {"missing": [], "inputs": {}})
        self.assertFalse(defensive.allow_buy)
        self.assertEqual(defensive.reject_reason, "state4_deep_entry_undefined")
        self.assertAlmostEqual(defensive.size_multiplier, 0.0, places=6)
        self.assertAlmostEqual(defensive.tp_multiplier, 0.85, places=6)
        self.assertFalse(defensive.require_perp_beta_hedge)

    def test_state5_stays_flat(self) -> None:
        crisis = _apply_v2_contract(
            evaluate_five_state_regime(_full(max_venue_dislocation_bps=40)),
            {"missing": [], "inputs": {}},
        )
        self.assertFalse(crisis.allow_buy)
        self.assertAlmostEqual(crisis.size_multiplier, 0.0, places=6)
