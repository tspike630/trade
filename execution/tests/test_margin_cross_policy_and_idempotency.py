"""Independent audit suite for cross-margin policy, 5-state gate, and idempotency."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import os
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from decision_engine.core.cityu_institutional_alpha import due_cftc_asof
from execution.margin_cross.api_semantics import make_client_id
from execution.margin_cross.errors import MarginExchangeError, MarginTransportTimeout
from execution.margin_cross.fixture import NOW_MS, client_from_snapshot, short_case
from execution.margin_cross.money import parse_decimal
from contextlib import contextmanager
import json
from pathlib import Path
from execution.margin_cross.policy import (
    APPROVED_CONFIG_PATH_ENV,
    APPROVED_CONFIG_SHA256_ENV,
    APPROVED_DRY_RUN_MODE,
    REQUIRED_APPROVAL_SCOPE,
    ENABLE_STATE2_LONG_ENV,
    ENABLE_STATE4_LONG_ENV,
    FILL_TP_FORMULA_B_ENV,
    MCP_FORBIDDEN_UID,
    PARAMETERS_APPROVED_ENV,
    PENDING_TRANSACTION_CODE,
    SUPPORTED_REGIME_STRATEGIES,
    env_flag_literal_one,
    parse_approved_risk_config,
)
from execution.margin_cross.proposal import unapproved_threshold_proposal
from execution.margin_cross.risk import parse_contract
from execution.margin_cross.strategy_gate import (
    UNRULED_KELLY_FRACTIONS,
    apply_formula_b_take_profit,
    build_approved_position_plan,
    check_data_freshness,
    evaluate_approved_regime_intent,
)
from execution.margin_cross.track import CrossMarginTrack



def _explicit_test_config_dict(
    *,
    uid: str = "88001122",
    now_ms: int = NOW_MS,
) -> dict[str, Any]:
    return {
        "approval_scope": REQUIRED_APPROVAL_SCOPE,
        "approved_by": "operator_test_fixture",
        "approved_account_uid": uid,
        "approved_at_ms": now_ms - 60_000,
        "expires_at_ms": now_ms + 86_400_000,
        "max_leverage": "3",
        "max_effective_leverage": "1.50",
        "max_total_liability_net_ratio": "0.50",
        "max_single_borrow_net_ratio": "0.24",
        "max_borrowable_pool_ratio": "0.80",
        "min_entry_margin_level": "2.50",
        "warn_margin_level": "2.00",
        "emergency_margin_level": "1.60",
        "min_order_notional_usdt": "20",
        "max_snapshot_age_ms": 5000,
        "interest_horizon_hours": "4",
        "base_position_pct": "0.24",
        "approved_strategies": sorted(SUPPORTED_REGIME_STRATEGIES),
    }


@contextmanager
def _approved_env_ctx(tmpdir: str, extra: dict[str, str] | None = None):
    cfg_path = os.path.join(tmpdir, "approved_cfg.json")
    raw_bytes = json.dumps(_explicit_test_config_dict()).encode("utf-8")
    Path(cfg_path).write_bytes(raw_bytes)
    cfg_sha = hashlib.sha256(raw_bytes).hexdigest()
    merged = {
        PARAMETERS_APPROVED_ENV: "1",
        APPROVED_CONFIG_PATH_ENV: cfg_path,
        APPROVED_CONFIG_SHA256_ENV: cfg_sha,
    }
    if extra:
        merged.update(extra)
    with patch.dict(os.environ, merged, clear=True):
        yield

def _fresh_clocks(now_ms: int = NOW_MS) -> dict[str, Any]:
    as_of_utc = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)
    cot_iso = due_cftc_asof(as_of_utc).isoformat()
    return {
        "snapshot_observed_at_ms": now_ms - 1_000,
        "indicators_ts_ms": now_ms - 60_000,
        "obi_top5_source_ts_ms": now_ms - 30_000,
        "perp_counterparty_ts_ms": now_ms - 60_000,
        "deribit_gamma_ts_ms": now_ms - 120_000,
        "volume_z_source_close_ms": now_ms - 300_000,
        "news_fetched_at_ms": now_ms - 600_000,
        "lseg_market_open": True,
        "lseg_cross_asset_ts_ms": now_ms - 300_000,
        "cot_asof_date": cot_iso,
    }


def _approved_snapshot(
    *,
    uid: str = "88001122",
    usdt_free: str = "10000",
    btc_free: str = "0",
    btc_borrowed: str = "0",
    btc_interest: str = "0",
    margin_level: str = "999",
    margin_level_status: str = "EXCESSIVE",
    account_type: str = "MARGIN_1",
    max_borrow_btc: str = "0.5",
    borrow_limit_btc: str = "1.0",
    now_ms: int = NOW_MS,
) -> dict[str, Any]:
    return {
        "uid": uid,
        "margin_mode": "CROSS",
        "account_type": account_type,
        "created": True,
        "trade_enabled": True,
        "borrow_enabled": True,
        "margin_level": margin_level,
        "margin_level_status": margin_level_status,
        "account_max_leverage": "3" if account_type == "MARGIN_1" else "5",
        "observed_at_ms": now_ms - 1_000,
        "assets": [
            {
                "asset": "BTC",
                "free": btc_free,
                "locked": "0",
                "borrowed": btc_borrowed,
                "interest": btc_interest,
            },
            {
                "asset": "USDT",
                "free": usdt_free,
                "locked": "0",
                "borrowed": "0",
                "interest": "0",
            },
        ],
        "max_borrowable": {"BTC": max_borrow_btc, "USDT": "20000"},
        "borrow_limit": {"BTC": borrow_limit_btc, "USDT": "50000"},
        "hourly_interest_rate": {"BTC": "0.000001", "USDT": "0.000002"},
        "bid_qty": "2",
        "ask_qty": "2",
        "reference_price": "100000",
        "filters": {
            "tick_size": "0.01",
            "step_size": "0.0001",
            "min_qty": "0.0001",
            "min_notional": "5",
        },
        "fee_rate": "0.001",
        "fee_asset": "USDT",
    }


def _state4_short_market_row() -> dict[str, Any]:
    return {
        "funding_z": 2.10,
        "funding_rate": 0.00040,
        "global_account_ls_ratio": 2.15,
        "whale_retail_divergence": 0.55,
        "xs_breadth_1h": 0.42,
        "xs_rank_pct_1h": 0.55,
        "obi_top5": -0.18,
        "taker_buy_ratio": 0.42,
        "volume_z": 1.10,
        "cot_regime": "cot_leveraged_long_crowded",
        "gamma_regime": "positive_gamma_pinned",
        "dvol_zscore": 0.40,
        "nvda_1h_ret_pct": -0.35,
        "smh_1h_ret_pct": -0.25,
        "lseg_market_open": True,
    }


def _state1_long_market_row() -> dict[str, Any]:
    return {
        "funding_z": 0.10,
        "funding_rate": 0.00005,
        "global_account_ls_ratio": 1.15,
        "whale_retail_divergence": 1.05,
        "xs_breadth_1h": 0.45,
        "xs_rank_pct_1h": 0.70,
        "obi_top5": 0.12,
        "taker_buy_ratio": 0.54,
        "volume_z": 0.90,
        "cot_regime": "cot_neutral",
        "gamma_regime": "positive_gamma_pinned",
        "dvol_zscore": 0.20,
        "nvda_1h_ret_pct": 0.25,
        "smh_1h_ret_pct": 0.20,
        "lseg_market_open": True,
    }


class RiskAndExceptionPolicyTest(unittest.TestCase):
    def test_env_flags_require_literal_one_and_default_fail_closed(self) -> None:
        for rejected in ("", "0", "true", "TRUE", "yes", "on", "1 ", " 1", "2"):
            self.assertFalse(
                env_flag_literal_one(PARAMETERS_APPROVED_ENV, {PARAMETERS_APPROVED_ENV: rejected})
            )
        self.assertTrue(
            env_flag_literal_one(PARAMETERS_APPROVED_ENV, {PARAMETERS_APPROVED_ENV: "1"})
        )

        plan_snap = _approved_snapshot()
        with patch.dict(os.environ, {}, clear=True):
            blocked_unapproved = build_approved_position_plan(
                intent_id="approved-plan-blocked",
                symbol="BTCUSDT",
                base_asset="BTC",
                quote_asset="USDT",
                requested_action="OPEN_SHORT",
                snapshot_raw=plan_snap,
                market_row=_state4_short_market_row(),
                clocks=_fresh_clocks(NOW_MS),
                now_ms=NOW_MS,
                planned_take_profit=Decimal("97000"),
                stop_price=Decimal("101500"),
                stop_limit_price=Decimal("101600"),
                risk_config=_explicit_test_config_dict(),
            )
            self.assertFalse(blocked_unapproved.allowed)
            self.assertEqual(blocked_unapproved.reasons, ("parameters_not_approved",))

        with patch.dict(os.environ, {PARAMETERS_APPROVED_ENV: "1"}, clear=True):
            blocked_no_cfg = build_approved_position_plan(
                intent_id="approved-plan-no-cfg",
                symbol="BTCUSDT",
                base_asset="BTC",
                quote_asset="USDT",
                requested_action="OPEN_SHORT",
                snapshot_raw=plan_snap,
                market_row=_state4_short_market_row(),
                clocks=_fresh_clocks(NOW_MS),
                now_ms=NOW_MS,
                planned_take_profit=Decimal("97000"),
                stop_price=Decimal("101500"),
                stop_limit_price=Decimal("101600"),
            )
            self.assertFalse(blocked_no_cfg.allowed)
            self.assertIn("approved_config_missing", blocked_no_cfg.reasons)

            plan = build_approved_position_plan(
                intent_id="approved-plan-1",
                symbol="BTCUSDT",
                base_asset="BTC",
                quote_asset="USDT",
                requested_action="OPEN_SHORT",
                snapshot_raw=plan_snap,
                market_row=_state4_short_market_row(),
                clocks=_fresh_clocks(NOW_MS),
                now_ms=NOW_MS,
                planned_take_profit=Decimal("97000"),
                stop_price=Decimal("101500"),
                stop_limit_price=Decimal("101600"),
                risk_config=_explicit_test_config_dict(),
            )
            self.assertTrue(plan.allowed)
            assert plan.contract is not None
            parsed_on, reasons_on = parse_contract(plan.contract)
            self.assertEqual(reasons_on, [])
            self.assertIsNotNone(parsed_on)

        with patch.dict(os.environ, {}, clear=True):
            parsed_off, reasons_off = parse_contract(plan.contract)
            self.assertIsNone(parsed_off)
            self.assertIn("parameters_approved_must_be_false", reasons_off)

    def test_effective_leverage_pool_cap_pro_account_mcp_uid_and_margin_ladder(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, _approved_env_ctx(tmpdir):
            base_snap = _approved_snapshot()
            plan = build_approved_position_plan(
                intent_id="ladder-1",
                symbol="BTCUSDT",
                base_asset="BTC",
                quote_asset="USDT",
                requested_action="OPEN_SHORT",
                snapshot_raw=base_snap,
                market_row=_state4_short_market_row(),
                clocks=_fresh_clocks(NOW_MS),
                now_ms=NOW_MS,
                planned_take_profit=Decimal("97000"),
                stop_price=Decimal("101500"),
                stop_limit_price=Decimal("101600"),
            )
            self.assertTrue(plan.allowed)
            assert plan.intent is not None and plan.contract is not None
            gate = {
                "emergency_stop": False,
                "now_ms": NOW_MS,
                "execution_mode": APPROVED_DRY_RUN_MODE,
            }

            over_lev_intent = {**plan.intent, "intent_id": "over-lev", "quantity": "0.0600"}
            over_lev_contract = {**plan.contract, "max_borrow_quantity": "0.1000"}
            t_lev = CrossMarginTrack(
                os.path.join(tmpdir, "lev.db"), client_from_snapshot(base_snap, Decimal(0))
            )
            r_lev = t_lev.open_position(over_lev_intent, base_snap, over_lev_contract, gate)
            self.assertEqual(r_lev.state, "locked")
            self.assertIn("effective_leverage_exceeded", r_lev.detail["reasons"])

            tight_pool_snap = _approved_snapshot(max_borrow_btc="0.0150")
            pool_intent = {**plan.intent, "intent_id": "over-pool", "quantity": "0.0130"}
            pool_contract = {**plan.contract, "max_borrow_quantity": "0.0200"}
            t_pool = CrossMarginTrack(
                os.path.join(tmpdir, "pool.db"),
                client_from_snapshot(tight_pool_snap, Decimal(0)),
            )
            r_pool = t_pool.open_position(pool_intent, tight_pool_snap, pool_contract, gate)
            self.assertEqual(r_pool.state, "locked")
            self.assertIn("asset_borrow_exceeds_pool_safety_cap", r_pool.detail["reasons"])

            pro_snap = _approved_snapshot(account_type="MARGIN_2")
            t_pro = CrossMarginTrack(
                os.path.join(tmpdir, "pro.db"), client_from_snapshot(pro_snap, Decimal(0))
            )
            r_pro = t_pro.open_position(plan.intent, pro_snap, plan.contract, gate)
            self.assertEqual(r_pro.state, "locked")
            self.assertIn("account_type_not_classic_cross_margin", r_pro.detail["reasons"])

            mcp_snap = _approved_snapshot(uid=MCP_FORBIDDEN_UID)
            mcp_intent = {**plan.intent, "account_uid": MCP_FORBIDDEN_UID}
            mcp_contract = {**plan.contract, "account_uid": MCP_FORBIDDEN_UID}
            t_mcp = CrossMarginTrack(
                os.path.join(tmpdir, "mcp.db"), client_from_snapshot(mcp_snap, Decimal(0))
            )
            r_mcp = t_mcp.open_position(mcp_intent, mcp_snap, mcp_contract, gate)
            self.assertEqual(r_mcp.state, "locked")
            self.assertIn("mcp_account_forbidden", r_mcp.detail["reasons"])

            tiny_snap = _approved_snapshot(usdt_free="120")
            tiny_plan = build_approved_position_plan(
                intent_id="tiny-1",
                symbol="BTCUSDT",
                base_asset="BTC",
                quote_asset="USDT",
                requested_action="OPEN_SHORT",
                snapshot_raw=tiny_snap,
                market_row=_state4_short_market_row(),
                clocks=_fresh_clocks(NOW_MS),
                now_ms=NOW_MS,
                planned_take_profit=Decimal("97000"),
                stop_price=Decimal("101500"),
                stop_limit_price=Decimal("101600"),
            )
            self.assertFalse(tiny_plan.allowed)
            self.assertIn("notional_below_approved_floor", tiny_plan.reasons)

            rest_snap = _approved_snapshot(margin_level="3.00")
            rest_client = client_from_snapshot(rest_snap, Decimal(0))
            rest_intent = {**plan.intent, "intent_id": "warn-rest", "limit_price": "100500"}
            t_warn = CrossMarginTrack(os.path.join(tmpdir, "warn.db"), rest_client)
            first = t_warn.open_position(rest_intent, rest_snap, plan.contract, gate)
            self.assertEqual(first.state, "entry_resting")
            rest_client.margin_level = Decimal("1.95")
            second = t_warn.open_position(rest_intent, rest_snap, plan.contract, gate)
            self.assertEqual(second.state, "emergency_unwound")
            self.assertTrue(second.account_flat)

    def test_minus_3007_pending_transaction_holds_without_duplicate_borrow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            case = short_case()
            case["client"].fail_next(
                "borrow",
                when="before",
                kind="exchange",
                code=PENDING_TRANSACTION_CODE,
                message="HAS_PENDING_TRANSACTION",
            )
            ledger_path = os.path.join(tmpdir, "p3007.db")
            track = CrossMarginTrack(ledger_path, case["client"])
            first = track.open_position(
                case["intent"], case["snapshot"], case["contract"], case["gate"]
            )
            self.assertEqual(first.state, "borrow_unknown")
            self.assertEqual(first.reason, "borrow_pending_transaction_lock")

            restarted = CrossMarginTrack(ledger_path, case["client"])
            second = restarted.open_position(
                case["intent"], case["snapshot"], case["contract"], case["gate"]
            )
            self.assertEqual(second.state, "borrow_unknown")
            borrow_writes = [m for m, _p in case["client"].write_log if m == "borrow"]
            self.assertEqual(len(borrow_writes), 1)


class StrategySizingAndFreshnessTest(unittest.TestCase):
    def test_all_nine_freshness_clocks_and_missing_classifier_inputs_fail_closed(self) -> None:
        base_clocks = _fresh_clocks(NOW_MS)
        self.assertEqual(check_data_freshness(base_clocks, now_ms=NOW_MS, max_snapshot_age_ms=5000), [])
        self.assertEqual(
            check_data_freshness(
                {**base_clocks, "volume_z_source_close_ms": NOW_MS - 3_600_000},
                now_ms=NOW_MS,
                max_snapshot_age_ms=5000,
            ),
            [],
        )

        stale_cases = [
            ("snapshot_observed_at_ms", NOW_MS - 5_001, "snapshot_stale_or_absent"),
            ("indicators_ts_ms", NOW_MS - 3_600_001, "indicators_stale_or_absent"),
            ("obi_top5_source_ts_ms", NOW_MS - 3_600_001, "obi_top5_source_stale_or_absent"),
            ("perp_counterparty_ts_ms", NOW_MS - 3_600_001, "perp_counterparty_stale_or_absent"),
            ("deribit_gamma_ts_ms", NOW_MS - 3_600_001, "deribit_gamma_stale_or_absent"),
            ("volume_z_source_close_ms", NOW_MS - 3_600_001, "volume_z_close_stale_or_absent"),
            ("news_fetched_at_ms", NOW_MS - 5_400_001, "news_fetched_at_stale_or_absent"),
            ("lseg_cross_asset_ts_ms", NOW_MS - 5_400_001, "lseg_cross_asset_bars_stale_or_absent"),
        ]
        for field, bad_val, expected_code in stale_cases:
            mutated = {**base_clocks, field: bad_val}
            reasons = check_data_freshness(mutated, now_ms=NOW_MS, max_snapshot_age_ms=5000)
            self.assertIn(expected_code, reasons)

        closed_lseg = {
            **base_clocks,
            "lseg_market_open": False,
            "lseg_cross_asset_ts_ms": None,
        }
        self.assertEqual(check_data_freshness(closed_lseg, now_ms=NOW_MS, max_snapshot_age_ms=5000), [])

        old_cot = (
            due_cftc_asof(datetime.fromtimestamp(NOW_MS / 1000.0, tz=timezone.utc))
            - timedelta(days=14)
        ).isoformat()
        self.assertIn(
            "cot_publication_stale_or_absent",
            check_data_freshness({**base_clocks, "cot_asof_date": old_cot}, now_ms=NOW_MS, max_snapshot_age_ms=5000),
        )

        incomplete_row = dict(_state1_long_market_row())
        incomplete_row.pop("gamma_regime")
        dec_missing = evaluate_approved_regime_intent(
            incomplete_row, symbol="BTCUSDT", requested_action="OPEN_LONG"
        )
        self.assertFalse(dec_missing.allowed)
        self.assertIn("regime_required_inputs_missing", dec_missing.reasons)
        self.assertIn("options_gamma_dvol_missing", dec_missing.reasons)

        for kelly in UNRULED_KELLY_FRACTIONS:
            row_with_kelly = {**_state4_short_market_row(), "size_pct": str(kelly)}
            dec_kelly = evaluate_approved_regime_intent(
                row_with_kelly, symbol="BTCUSDT", requested_action="OPEN_SHORT"
            )
            self.assertFalse(dec_kelly.allowed)
            self.assertIn("unruled_kelly_fraction_rejected", dec_kelly.reasons)

    def test_five_state_routing_anti_squeeze_veto_and_realtime_24pct_sizing(self) -> None:
        snap = _approved_snapshot(usdt_free="10000")
        clocks = _fresh_clocks(NOW_MS)

        squeeze_row = {**_state4_short_market_row(), "funding_z": -1.5}
        vetoed = evaluate_approved_regime_intent(
            squeeze_row, symbol="BTCUSDT", requested_action="OPEN_SHORT"
        )
        self.assertFalse(vetoed.allowed)
        self.assertEqual(vetoed.reasons, ("veto_negative_funding_z_squeeze_risk",))

        s1_short = evaluate_approved_regime_intent(
            _state1_long_market_row(), symbol="BTCUSDT", requested_action="OPEN_SHORT"
        )
        self.assertFalse(s1_short.allowed)
        self.assertIn("short_forbidden_in_bull_or_neutral_regime", s1_short.reasons)

        s1_long_plan = build_approved_position_plan(
            intent_id="s1-long",
            risk_config=_explicit_test_config_dict(),
            environ={PARAMETERS_APPROVED_ENV: "1"},
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            requested_action="OPEN_LONG",
            snapshot_raw=snap,
            market_row=_state1_long_market_row(),
            clocks=clocks,
            now_ms=NOW_MS,
            planned_take_profit=Decimal("102000"),
            stop_price=Decimal("98500"),
            stop_limit_price=Decimal("98400"),
        )
        self.assertTrue(s1_long_plan.allowed)
        self.assertEqual(s1_long_plan.quantity, Decimal("0.0240"))

        s4_short_plan = build_approved_position_plan(
            intent_id="s4-short",
            risk_config=_explicit_test_config_dict(),
            environ={PARAMETERS_APPROVED_ENV: "1"},
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            requested_action="OPEN_SHORT",
            snapshot_raw=snap,
            market_row=_state4_short_market_row(),
            clocks=clocks,
            now_ms=NOW_MS,
            planned_take_profit=Decimal("97000"),
            stop_price=Decimal("101500"),
            stop_limit_price=Decimal("101600"),
        )
        self.assertTrue(s4_short_plan.allowed)
        self.assertEqual(s4_short_plan.quantity, Decimal("0.0132"))

        crisis_row = {
            **_state4_short_market_row(),
            "liq_cascade_regime": "long_liquidation_waterfall",
            "taker_buy_ratio": 0.38,
        }
        s5_plan = build_approved_position_plan(
            intent_id="s5-short",
            risk_config=_explicit_test_config_dict(),
            environ={PARAMETERS_APPROVED_ENV: "1"},
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            requested_action="OPEN_SHORT",
            snapshot_raw=snap,
            market_row=crisis_row,
            clocks=clocks,
            now_ms=NOW_MS,
            planned_take_profit=Decimal("96000"),
            stop_price=Decimal("101500"),
            stop_limit_price=Decimal("101600"),
        )
        self.assertTrue(s5_plan.allowed)
        self.assertEqual(s5_plan.strategy_id, "regime5_crisis_contagion_short")
        self.assertEqual(s5_plan.quantity, Decimal("0.0120"))

        unlock_row = {
            **_state1_long_market_row(),
            "in_unlock_blackout": 1,
            "obi_top5": -0.22,
            "taker_buy_ratio": 0.40,
        }
        unlock_dec = evaluate_approved_regime_intent(
            unlock_row, symbol="ARBUSDT", requested_action="OPEN_SHORT"
        )
        self.assertTrue(unlock_dec.allowed)
        self.assertEqual(unlock_dec.strategy_id, "regime5_unlock_cliff_short")
        self.assertEqual(unlock_dec.size_multiplier, Decimal("0.35"))

        s2_row = {
            **_state1_long_market_row(),
            "funding_z": -2.2,
            "funding_rate": -0.0004,
        }
        self.assertFalse(
            evaluate_approved_regime_intent(
                s2_row, symbol="BTCUSDT", requested_action="OPEN_LONG", environ={}
            ).allowed
        )
        self.assertTrue(
            evaluate_approved_regime_intent(
                s2_row,
                symbol="BTCUSDT",
                requested_action="OPEN_LONG",
                environ={ENABLE_STATE2_LONG_ENV: "1"},
            ).allowed
        )
        self.assertFalse(
            evaluate_approved_regime_intent(
                _state4_short_market_row(),
                symbol="BTCUSDT",
                requested_action="OPEN_LONG",
                environ={},
            ).allowed
        )
        self.assertFalse(
            evaluate_approved_regime_intent(
                {**_state4_short_market_row(), "boll_z": -1.50},
                symbol="BTCUSDT",
                requested_action="OPEN_LONG",
                environ={ENABLE_STATE4_LONG_ENV: "1"},
            ).allowed
        )
        self.assertTrue(
            evaluate_approved_regime_intent(
                {**_state4_short_market_row(), "boll_z": -2.40, "alpha": 0.10, "obi_top5": -0.10, "taker_buy_ratio": 0.42},
                symbol="BTCUSDT",
                requested_action="OPEN_LONG",
                environ={ENABLE_STATE4_LONG_ENV: "1"},
            ).allowed
        )

    def test_formula_b_first_hang_take_profit_and_restart_persistence(self) -> None:
        short_tp = apply_formula_b_take_profit(
            action="OPEN_SHORT",
            planned_entry=Decimal("100000"),
            planned_tp=Decimal("99000"),
            actual_fill=Decimal("100200"),
            tick_size=Decimal("0.01"),
        )
        self.assertEqual(short_tp, Decimal("99198.00"))

        with tempfile.TemporaryDirectory() as tmpdir, _approved_env_ctx(
            tmpdir, {FILL_TP_FORMULA_B_ENV: "1"}
        ):
            snap = _approved_snapshot()
            plan = build_approved_position_plan(
                intent_id="fb-short-1",
                symbol="BTCUSDT",
                base_asset="BTC",
                quote_asset="USDT",
                requested_action="OPEN_SHORT",
                snapshot_raw=snap,
                market_row=_state4_short_market_row(),
                clocks=_fresh_clocks(NOW_MS),
                now_ms=NOW_MS,
                limit_price=Decimal("99800"),
                planned_take_profit=Decimal("98802"),
                stop_price=Decimal("101500"),
                stop_limit_price=Decimal("101600"),
            )
            self.assertTrue(plan.allowed)
            assert plan.intent is not None and plan.contract is not None
            client = client_from_snapshot(snap, Decimal(0))
            orig_new_order = client.new_order

            def fill_better_price(payload: dict[str, Any]) -> dict[str, Any]:
                res = orig_new_order(payload)
                oid = int(res["orderId"])
                client._trades[oid][0]["price"] = "100000"
                return res

            client.new_order = fill_better_price  # type: ignore[method-assign]
            client.fail_next("new_oco", when="after", kind="timeout")

            ledger_path = os.path.join(tmpdir, "fb.db")
            gate = {
                "emergency_stop": False,
                "now_ms": NOW_MS,
                "execution_mode": APPROVED_DRY_RUN_MODE,
            }
            track = CrossMarginTrack(ledger_path, client)
            first = track.open_position(plan.intent, snap, plan.contract, gate)
            self.assertEqual(first.state, "protected")
            self.assertEqual(first.detail["stop_price"], "101500")
            tp_used, _ = parse_decimal(first.detail["take_profit_price"], "tp")
            orig_tp, _ = parse_decimal(plan.intent["take_profit_price"], "orig_tp")
            assert tp_used is not None and orig_tp is not None
            self.assertGreater(tp_used, orig_tp)

            second = CrossMarginTrack(ledger_path, client).open_position(
                plan.intent, snap, plan.contract, gate
            )
            self.assertEqual(second.state, "protected")
            self.assertEqual(second.detail["take_profit_price"], first.detail["take_profit_price"])
            oco_writes = [m for m, _p in client.write_log if m == "new_oco"]
            self.assertEqual(len(oco_writes), 1)


class LifecycleIdempotencyAuditTest(unittest.TestCase):
    def test_cross_intent_unresolved_same_symbol_pyramiding_and_existing_liability_blocks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            case1 = short_case()
            case1["client"].fail_next("borrow", when="before", kind="timeout")
            ledger_u = os.path.join(tmpdir, "unresolved.db")
            t1 = CrossMarginTrack(ledger_u, case1["client"])
            r1 = t1.open_position(
                case1["intent"], case1["snapshot"], case1["contract"], case1["gate"]
            )
            self.assertEqual(r1.state, "borrow_unknown")

            intent2 = {**case1["intent"], "intent_id": "short-cycle-2"}
            r2 = t1.open_position(intent2, case1["snapshot"], case1["contract"], case1["gate"])
            self.assertEqual(r2.state, "locked")
            self.assertEqual(r2.reason, "unresolved_intent_blocks_new_risk")

            case_p = short_case()
            ledger_p = os.path.join(tmpdir, "pyramid.db")
            tp = CrossMarginTrack(ledger_p, case_p["client"])
            rp1 = tp.open_position(
                case_p["intent"], case_p["snapshot"], case_p["contract"], case_p["gate"]
            )
            self.assertEqual(rp1.state, "protected")
            rp2 = tp.open_position(
                {**case_p["intent"], "intent_id": "pyramid-attempt-2"},
                case_p["snapshot"],
                case_p["contract"],
                case_p["gate"],
            )
            self.assertEqual(rp2.state, "locked")
            self.assertEqual(rp2.reason, "same_symbol_position_open")

            liab_case = short_case()
            liab_case["snapshot"]["assets"][0]["borrowed"] = "0.0005"
            liab_case["client"].asset("BTC")["borrowed"] = Decimal("0.0005")
            tl = CrossMarginTrack(os.path.join(tmpdir, "liab.db"), liab_case["client"])
            rl = tl.open_position(
                liab_case["intent"],
                liab_case["snapshot"],
                liab_case["contract"],
                liab_case["gate"],
            )
            self.assertEqual(rl.state, "locked")
            self.assertEqual(rl.reason, "existing_liability_blocks_new_open")

    def test_historical_same_amount_manual_tx_never_binds_timed_out_borrow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            case = short_case()
            case["client"]._txs.append(
                {
                    "type": "BORROW",
                    "asset": "BTC",
                    "amount": "0.01",
                    "principal": "0.01",
                    "interest": "0",
                    "status": "CONFIRMED",
                    "txId": 77,
                    "isolatedSymbol": "",
                }
            )
            case["client"].fail_next("borrow", when="before", kind="timeout")
            track = CrossMarginTrack(os.path.join(tmpdir, "hist.db"), case["client"])
            res = track.open_position(
                case["intent"], case["snapshot"], case["contract"], case["gate"]
            )
            self.assertEqual(res.state, "borrow_unknown")
            self.assertEqual(res.reason, "borrow_unconfirmed")
            order_writes = [m for m, _p in case["client"].write_log if m == "new_order"]
            self.assertEqual(order_writes, [])

    def test_partial_fill_on_borrow_interest_repay_timeout_and_pending_restart_recovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            case = short_case()
            case["contract"]["interest_horizon_hours"] = "4"
            case["contract"]["max_base_over_liability"] = "0.0001"
            case["client"].partial_fill_qty = Decimal("0.0040")
            case["client"].fail_next("repay", when="after", kind="timeout")

            ledger_path = os.path.join(tmpdir, "partial-timeout.db")
            track = CrossMarginTrack(ledger_path, case["client"])
            orig_repay = case["client"].repay

            def repay_then_pending(payload: dict[str, Any]) -> dict[str, Any]:
                try:
                    return orig_repay(payload)
                finally:
                    if case["client"]._txs:
                        case["client"]._txs[-1]["status"] = "PENDING"
                        case["client"]._txs[-1]["type"] = "MANUAL"

            case["client"].repay = repay_then_pending  # type: ignore[method-assign]

            def qbr_with_manual(payload: dict[str, Any]) -> dict[str, Any]:
                wanted = str(payload.get("type") or "")
                asset = str(payload.get("asset") or "")
                rows = [
                    dict(r)
                    for r in case["client"]._txs
                    if r["asset"] == asset and r["type"] in {wanted, "MANUAL"}
                ]
                return {"rows": rows, "total": len(rows)}

            case["client"].query_borrow_repay = qbr_with_manual  # type: ignore[method-assign]
            first = track.open_position(
                case["intent"], case["snapshot"], case["contract"], case["gate"]
            )
            self.assertEqual(first.state, "repay_unknown")

            second = CrossMarginTrack(ledger_path, case["client"]).open_position(
                case["intent"], case["snapshot"], case["contract"], case["gate"]
            )
            self.assertEqual(second.state, "repay_unknown")
            self.assertEqual(len([m for m, _p in case["client"].write_log if m == "repay"]), 1)

            case["client"]._txs[-1]["status"] = "CONFIRMED"
            case["client"].repay = orig_repay  # type: ignore[method-assign]
            third = CrossMarginTrack(ledger_path, case["client"]).open_position(
                case["intent"], case["snapshot"], case["contract"], case["gate"]
            )
            self.assertEqual(third.state, "protected")
            self.assertEqual(third.detail["protection_qty"], "0.0042")

            list_id = make_client_id(str(case["intent"]["intent_id"]), "oco-list")
            case["client"].fill_oco_limit(list_id)
            CrossMarginTrack(ledger_path, case["client"]).complete_exit(
                str(case["intent"]["intent_id"]), case["gate"]
            )
            self.assertEqual(case["client"].asset("BTC")["borrowed"], Decimal("0"))
            self.assertEqual(case["client"].asset("BTC")["interest"], Decimal("0"))
            self.assertEqual(len([m for m, _p in case["client"].write_log if m == "new_oco"]), 1)

    def test_emergency_cancel_race_with_actual_fill_arms_oco_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            case = short_case()
            case["intent"]["limit_price"] = "100500"
            ledger_path = os.path.join(tmpdir, "cancel-race.db")
            track = CrossMarginTrack(ledger_path, case["client"])
            resting = track.open_position(
                case["intent"], case["snapshot"], case["contract"], case["gate"]
            )
            self.assertEqual(resting.state, "entry_resting")

            entry_cid = make_client_id(str(case["intent"]["intent_id"]), "entry")
            order_row = case["client"]._orders[entry_cid]
            case["client"]._unlock_remainder(order_row)
            case["client"].asset("BTC")["free"] -= Decimal("0.0040")
            case["client"].asset("USDT")["free"] += Decimal("402.0") * Decimal("0.999")
            order_row["executedQty"] = "0.004"
            order_row["status"] = "CANCELED"
            case["client"]._trades[int(order_row["orderId"])] = [
                {
                    "id": 901,
                    "orderId": order_row["orderId"],
                    "symbol": "BTCUSDT",
                    "price": "100500",
                    "qty": "0.004",
                    "commission": "0.402",
                    "commissionAsset": "USDT",
                    "isBuyer": False,
                    "isMaker": True,
                    "isIsolated": False,
                }
            ]
            case["client"].asset("BTC")["interest"] = Decimal("0")

            emerg_gate = {**case["gate"], "emergency_stop": True}
            recovered = track.open_position(
                case["intent"], case["snapshot"], case["contract"], emerg_gate
            )
            self.assertEqual(recovered.state, "protected")
            self.assertTrue(recovered.protection_confirmed)
            self.assertEqual(recovered.detail["protection_qty"], "0.004")

    def test_oco_reject_ioc_flatten_timeout_recovers_on_restart_without_duplicate_flatten(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            case = short_case()
            case["client"].interest_on_borrow = Decimal("0")
            case["client"].fail_next(
                "new_oco", when="before", kind="exchange", code=-2010, message="OCO rejected"
            )
            case["client"].fail_next("new_order", when="after", kind="timeout", on_call=2)
            orig_qo = case["client"].query_order
            flatten_cid = make_client_id(str(case["intent"]["intent_id"]), "flatten")
            first_flatten_q = [True]

            def qo_timeout_once(payload: dict[str, Any]) -> dict[str, Any]:
                if payload.get("origClientOrderId") == flatten_cid and first_flatten_q[0]:
                    first_flatten_q[0] = False
                    raise MarginTransportTimeout("query_order")
                return orig_qo(payload)

            case["client"].query_order = qo_timeout_once  # type: ignore[method-assign]
            ledger_path = os.path.join(tmpdir, "flatten-recover.db")
            track = CrossMarginTrack(ledger_path, case["client"])
            tick1 = track.open_position(
                case["intent"], case["snapshot"], case["contract"], case["gate"]
            )
            self.assertEqual(tick1.state, "protection_unknown")
            self.assertEqual(tick1.reason, "flatten_unconfirmed")

            tick2 = CrossMarginTrack(ledger_path, case["client"]).open_position(
                case["intent"], case["snapshot"], case["contract"], case["gate"]
            )
            self.assertEqual(tick2.state, "protection_failed_flattened")
            self.assertTrue(tick2.account_flat)
            flatten_orders = [
                p
                for m, p in case["client"].write_log
                if m == "new_order" and p.get("newClientOrderId") == flatten_cid
            ]
            self.assertEqual(len(flatten_orders), 1)

    def test_query_transport_timeouts_across_readback_paths_never_crash_or_duplicate(self) -> None:
        for read_method in (
            "query_account",
            "query_order",
            "query_trades",
            "query_hourly_interest",
            "query_oco",
        ):
            with tempfile.TemporaryDirectory() as tmpdir:
                case = short_case()
                orig_fn = getattr(case["client"], read_method)
                tripped = [False]

                def fail_once(
                    *args: Any,
                    _orig: Any = orig_fn,
                    _flag: list[bool] = tripped,
                    _m: str = read_method,
                    **kwargs: Any,
                ) -> Any:
                    if not _flag[0]:
                        _flag[0] = True
                        raise MarginTransportTimeout(_m)
                    return _orig(*args, **kwargs)

                setattr(case["client"], read_method, fail_once)
                ledger_path = os.path.join(tmpdir, f"{read_method}.db")
                track = CrossMarginTrack(ledger_path, case["client"])
                res = track.open_position(
                    case["intent"], case["snapshot"], case["contract"], case["gate"]
                )
                self.assertIsInstance(res.state, str)
                res2 = track.open_position(
                    case["intent"], case["snapshot"], case["contract"], case["gate"]
                )
                self.assertEqual(res2.state, "protected")
                self.assertEqual(len([m for m, _p in case["client"].write_log if m == "borrow"]), 1)
                self.assertEqual(
                    len([m for m, _p in case["client"].write_log if m == "new_order"]), 1
                )
                self.assertEqual(
                    len([m for m, _p in case["client"].write_log if m == "new_oco"]), 1
                )

    def test_zero_default_policy_proposal_and_contract_fields_missing_means_closed(self) -> None:
        # 1) proposal.py stays strictly unapproved even if CROSS_MARGIN_PARAMETERS_APPROVED=1
        with patch.dict(os.environ, {PARAMETERS_APPROVED_ENV: "1"}, clear=True):
            rows = unapproved_threshold_proposal()
            self.assertTrue(rows)
            self.assertTrue(all(r["approved"] is False for r in rows))

        # 2) policy.py, risk.py, strategy_gate.py contain zero default risk/sizing numbers
        pkg = Path(__file__).resolve().parents[1] / "margin_cross"
        for mod_name in ("policy.py", "risk.py", "strategy_gate.py"):
            src = (pkg / mod_name).read_text(encoding="utf-8")
            self.assertNotIn("0.24", src)
            self.assertNotIn("1.50", src)
            self.assertNotIn("2.50", src)

        # 3) Every single field of ApprovedRiskConfig is mandatory (missing -> closed)
        full_cfg = _explicit_test_config_dict()
        for field in list(full_cfg.keys()):
            partial = dict(full_cfg)
            partial.pop(field)
            parsed_cfg, cfg_reasons = parse_approved_risk_config(partial)
            self.assertIsNone(parsed_cfg, f"missing {field} must fail")
            self.assertTrue(cfg_reasons)

        # 4) Unapproved fixture contract leaves optional approved fields None (never 1.50/2.00/1.60)
        unapp, unapp_reasons = parse_contract(short_case()["contract"])
        self.assertEqual(unapp_reasons, [])
        assert unapp is not None
        self.assertIsNone(unapp.max_effective_leverage)
        self.assertIsNone(unapp.warn_margin_level)
        self.assertIsNone(unapp.emergency_margin_level)

        # 5) Approved contract missing ANY of max_effective_leverage / warn_margin_level / emergency_margin_level fails closed
        with patch.dict(os.environ, {PARAMETERS_APPROVED_ENV: "1"}, clear=True):
            plan = build_approved_position_plan(
                intent_id="req-fields-1",
                symbol="BTCUSDT",
                base_asset="BTC",
                quote_asset="USDT",
                requested_action="OPEN_SHORT",
                snapshot_raw=_approved_snapshot(),
                market_row=_state4_short_market_row(),
                clocks=_fresh_clocks(NOW_MS),
                now_ms=NOW_MS,
                planned_take_profit=Decimal("97000"),
                stop_price=Decimal("101500"),
                stop_limit_price=Decimal("101600"),
                risk_config=full_cfg,
            )
            assert plan.contract is not None
            for req_field in (
                "max_effective_leverage",
                "warn_margin_level",
                "emergency_margin_level",
                "max_total_liability_net_ratio",
                "max_borrowable_pool_ratio",
                "min_order_notional_usdt",
            ):
                broken = dict(plan.contract)
                broken.pop(req_field)
                c_out, c_reasons = parse_contract(broken)
                self.assertIsNone(c_out, f"missing {req_field} must fail parse_contract")
                self.assertIn(f"{req_field}_missing", c_reasons)

    def test_same_asset_external_borrow_and_preexisting_base_isolation_probes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Probe 1 (Exact Audit Blocker #3): external +0.005 BTC borrow & +0.005 free BTC
            case = short_case()
            t1 = CrossMarginTrack(os.path.join(tmpdir, "iso1.db"), case["client"])
            opened = t1.open_position(
                case["intent"], case["snapshot"], case["contract"], case["gate"]
            )
            self.assertEqual(opened.state, "protected")
            oco = next(p for name, p in case["client"].write_log if name == "new_oco")
            case["client"].fill_oco_limit(oco["listClientOrderId"])
            # External strategy borrows +0.005 BTC and leaves +0.005 BTC free on same account
            case["client"].asset("BTC")["borrowed"] += Decimal("0.005")
            case["client"].asset("BTC")["free"] += Decimal("0.005")

            closed = t1.complete_exit(case["intent"]["intent_id"], case["gate"])
            self.assertEqual(closed.state, "not_flat")
            self.assertEqual(closed.reason, "same_asset_external_liability_conflict")
            self.assertFalse(closed.phase_ok)
            self.assertFalse(closed.account_flat)
            self.assertEqual([n for n, _ in case["client"].write_log].count("repay"), 0)
            self.assertEqual(case["client"].asset("BTC")["borrowed"], Decimal("0.015"))
            self.assertEqual(case["client"].asset("BTC")["free"], Decimal("0.0151"))

            # Probe 2: Cross-asset ETH interest 0.01 -> repays ONLY BTC 0.0101, state is not_flat
            case_eth = short_case()
            t_eth = CrossMarginTrack(os.path.join(tmpdir, "eth.db"), case_eth["client"])
            t_eth.open_position(
                case_eth["intent"], case_eth["snapshot"], case_eth["contract"], case_eth["gate"]
            )
            oco_eth = next(p for name, p in case_eth["client"].write_log if name == "new_oco")
            case_eth["client"].fill_oco_limit(oco_eth["listClientOrderId"])
            case_eth["client"].asset("ETH")["interest"] = Decimal("0.01")
            closed_eth = t_eth.complete_exit(case_eth["intent"]["intent_id"], case_eth["gate"])
            repay_payloads = [p for n, p in case_eth["client"].write_log if n == "repay"]
            self.assertEqual(len(repay_payloads), 1)
            self.assertEqual(repay_payloads[0]["asset"], "BTC")
            self.assertEqual(repay_payloads[0]["amount"], "0.0101")
            self.assertEqual(closed_eth.state, "not_flat")
            self.assertFalse(closed_eth.account_flat)

            # Probe 3: Pre-existing baseline_base = 1.0 BTC is NEVER dipped into when interest grows
            case_base = short_case()
            case_base["snapshot"]["assets"][0]["free"] = "1.0"
            case_base["client"] = client_from_snapshot(case_base["snapshot"], Decimal("0.0001"))
            t_base = CrossMarginTrack(os.path.join(tmpdir, "base.db"), case_base["client"])
            o_base = t_base.open_position(
                case_base["intent"], case_base["snapshot"], case_base["contract"], case_base["gate"]
            )
            self.assertEqual(o_base.state, "protected")
            oco_base = next(p for name, p in case_base["client"].write_log if name == "new_oco")
            case_base["client"].asset("BTC")["interest"] += Decimal("0.0001")
            case_base["client"].fill_oco_limit(oco_base["listClientOrderId"])
            # Total BTC.free is 1.0101 >= 0.0102 debt, yet baseline 1.0 BTC must stay untouched!
            c_base = t_base.complete_exit(case_base["intent"]["intent_id"], case_base["gate"])
            self.assertEqual(c_base.state, "not_flat")
            self.assertEqual(c_base.reason, "base_short_of_liability")
            self.assertFalse(c_base.account_flat)
            self.assertEqual([n for n, _ in case_base["client"].write_log].count("repay"), 0)
            self.assertEqual(case_base["client"].asset("BTC")["free"], Decimal("1.0101"))

            # Probe 4: Same base_asset across a different symbol (BTCFDUSD while BTCUSDT protected) is blocked
            case_sym = short_case()
            t_sym = CrossMarginTrack(os.path.join(tmpdir, "sym.db"), case_sym["client"])
            self.assertEqual(
                t_sym.open_position(
                    case_sym["intent"], case_sym["snapshot"], case_sym["contract"], case_sym["gate"]
                ).state,
                "protected",
            )
            snap_fd = dict(case_sym["snapshot"])
            snap_fd["assets"] = [
                {"asset": "BTC", "free": "0", "locked": "0", "borrowed": "0", "interest": "0"},
                {"asset": "FDUSD", "free": "5000", "locked": "0", "borrowed": "0", "interest": "0"},
            ]
            snap_fd["max_borrowable"] = {"BTC": "1", "FDUSD": "10000"}
            snap_fd["borrow_limit"] = {"BTC": "2", "FDUSD": "20000"}
            snap_fd["hourly_interest_rate"] = {"BTC": "0.000001", "FDUSD": "0.000002"}
            snap_fd["fee_asset"] = "FDUSD"
            intent_fd = {
                **case_sym["intent"],
                "intent_id": "short-btcfdusd-2",
                "symbol": "BTCFDUSD",
                "quote_asset": "FDUSD",
            }
            contract_fd = {
                **case_sym["contract"],
                "symbol": "BTCFDUSD",
                "quote_asset": "FDUSD",
            }
            r_fd = t_sym.open_position(intent_fd, snap_fd, contract_fd, case_sym["gate"])
            self.assertEqual(r_fd.state, "locked")
            self.assertEqual(r_fd.reason, "same_base_asset_position_open")


    def test_interest_history_attribution_blocks_unattributed_or_shared_principal_interest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Case A: Legitimate PERIODIC interest accrued on intent principal (0.01) within horizon -> clean_flat!
            case_ok = short_case()
            case_ok["snapshot"]["hourly_interest_rate"]["BTC"] = "0.01"
            case_ok["client"].hourly_rate["BTC"] = Decimal("0.01")
            case_ok["contract"]["interest_horizon_hours"] = "1"
            case_ok["contract"]["max_base_over_liability"] = "0.0001"
            t_ok = CrossMarginTrack(os.path.join(tmpdir, "int_ok.db"), case_ok["client"])
            self.assertEqual(
                t_ok.open_position(
                    case_ok["intent"], case_ok["snapshot"], case_ok["contract"], case_ok["gate"]
                ).state,
                "protected",
            )
            oco_ok = next(p for name, p in case_ok["client"].write_log if name == "new_oco")
            # Cover bought 0.0102 BTC; accrue 0.0001 periodic interest via official interestHistory
            case_ok["client"].accrue_interest("BTC", Decimal("0.0001"))
            case_ok["client"].fill_oco_limit(oco_ok["listClientOrderId"])
            closed_ok = t_ok.complete_exit(case_ok["intent"]["intent_id"], case_ok["gate"])
            self.assertEqual(closed_ok.state, "clean_flat")
            self.assertTrue(closed_ok.account_flat)
            self.assertEqual([n for n, _ in case_ok["client"].write_log].count("repay"), 1)

            # Case B: Unattributed extra interest (+0.0001 without interestHistory row) even when cover & free BTC (0.0102 + 1.0) can easily pay it -> REFUSED!
            case_unattr = short_case()
            case_unattr["snapshot"]["hourly_interest_rate"]["BTC"] = "0.01"
            case_unattr["client"].hourly_rate["BTC"] = Decimal("0.01")
            case_unattr["contract"]["interest_horizon_hours"] = "1"
            case_unattr["contract"]["max_base_over_liability"] = "0.0001"
            t_unattr = CrossMarginTrack(
                os.path.join(tmpdir, "int_unattr.db"), case_unattr["client"]
            )
            self.assertEqual(
                t_unattr.open_position(
                    case_unattr["intent"],
                    case_unattr["snapshot"],
                    case_unattr["contract"],
                    case_unattr["gate"],
                ).state,
                "protected",
            )
            oco_unattr = next(
                p for name, p in case_unattr["client"].write_log if name == "new_oco"
            )
            case_unattr["client"].asset("BTC")["interest"] += Decimal("0.0001")
            case_unattr["client"].asset("BTC")["free"] += Decimal("1.0")
            case_unattr["client"].fill_oco_limit(oco_unattr["listClientOrderId"])
            closed_unattr = t_unattr.complete_exit(
                case_unattr["intent"]["intent_id"], case_unattr["gate"]
            )
            self.assertEqual(closed_unattr.state, "not_flat")
            self.assertEqual(closed_unattr.reason, "unattributed_interest_liability_conflict")
            self.assertFalse(closed_unattr.account_flat)
            self.assertEqual([n for n, _ in case_unattr["client"].write_log].count("repay"), 0)

            # Case C: Interest row in interestHistory accrued on external/larger principal (0.015 != 0.010) -> REFUSED!
            case_ext = short_case()
            case_ext["snapshot"]["hourly_interest_rate"]["BTC"] = "0.01"
            case_ext["client"].hourly_rate["BTC"] = Decimal("0.01")
            case_ext["contract"]["interest_horizon_hours"] = "1"
            case_ext["contract"]["max_base_over_liability"] = "0.0001"
            t_ext = CrossMarginTrack(os.path.join(tmpdir, "int_ext.db"), case_ext["client"])
            t_ext.open_position(
                case_ext["intent"], case_ext["snapshot"], case_ext["contract"], case_ext["gate"]
            )
            oco_ext = next(p for name, p in case_ext["client"].write_log if name == "new_oco")
            case_ext["client"].accrue_interest(
                "BTC", Decimal("0.0001"), principal=Decimal("0.015")
            )
            case_ext["client"].fill_oco_limit(oco_ext["listClientOrderId"])
            closed_ext = t_ext.complete_exit(case_ext["intent"]["intent_id"], case_ext["gate"])
            self.assertEqual(closed_ext.state, "not_flat")
            self.assertEqual(closed_ext.reason, "same_asset_external_interest_conflict")
            self.assertEqual([n for n, _ in case_ext["client"].write_log].count("repay"), 0)

    def test_wrong_200_claims_5xx_indeterminate_writes_and_429_readback_holds(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1) Wrong HTTP 200 on query_order (mismatched clientOrderId / symbol) -> fill_mismatch, NO OCO!
            case_w_ord = short_case()
            orig_qo = case_w_ord["client"].query_order
            case_w_ord["client"].query_order = lambda p: {  # type: ignore[method-assign]
                **orig_qo(p),
                "clientOrderId": "wrong-client-id",
            }
            t_wo = CrossMarginTrack(os.path.join(tmpdir, "wo.db"), case_w_ord["client"])
            r_wo = t_wo.open_position(
                case_w_ord["intent"],
                case_w_ord["snapshot"],
                case_w_ord["contract"],
                case_w_ord["gate"],
            )
            self.assertEqual(r_wo.state, "fill_mismatch")
            self.assertEqual(r_wo.reason, "entry_identity_mismatch")
            self.assertEqual([n for n, _ in case_w_ord["client"].write_log].count("new_oco"), 0)

            # 2) Wrong HTTP 200 on query_oco (mismatched listClientOrderId) -> protection_unknown, never claims protected!
            case_w_oco = short_case()
            orig_qoco = case_w_oco["client"].query_oco
            case_w_oco["client"].query_oco = lambda p: {  # type: ignore[method-assign]
                **orig_qoco(p),
                "listClientOrderId": "foreign-oco-list",
            }
            t_woco = CrossMarginTrack(os.path.join(tmpdir, "woco.db"), case_w_oco["client"])
            r_woco = t_woco.open_position(
                case_w_oco["intent"],
                case_w_oco["snapshot"],
                case_w_oco["contract"],
                case_w_oco["gate"],
            )
            self.assertEqual(r_woco.state, "protection_unknown")
            self.assertFalse(r_woco.protection_confirmed)

            # 3) Wrong HTTP 200 on query_trades (foreign orderId) -> fill_mismatch, NO OCO!
            case_w_tr = short_case()
            orig_qt = case_w_tr["client"].query_trades
            case_w_tr["client"].query_trades = lambda p: [  # type: ignore[method-assign]
                {**t, "orderId": 999999} for t in orig_qt(p)
            ]
            t_wtr = CrossMarginTrack(os.path.join(tmpdir, "wtr.db"), case_w_tr["client"])
            r_wtr = t_wtr.open_position(
                case_w_tr["intent"],
                case_w_tr["snapshot"],
                case_w_tr["contract"],
                case_w_tr["gate"],
            )
            self.assertEqual(r_wtr.state, "fill_mismatch")
            self.assertEqual([n for n, _ in case_w_tr["client"].write_log].count("new_oco"), 0)

            # 4) HTTP 500 / -1001 on new_order BEFORE fill + HTTP 429 on query_order -> holds in entry_unknown (NEVER unwinds/repays!)
            case_500 = short_case()
            orig_no = case_500["client"].new_order

            def no_500_before(_payload: dict[str, Any]) -> dict[str, Any]:
                case_500["client"].write_log.append(("new_order", dict(_payload)))
                raise MarginExchangeError(-1001, "Internal error", http_status=500)

            case_500["client"].new_order = no_500_before  # type: ignore[method-assign]
            case_500["client"].query_order = lambda _p: (_ for _ in ()).throw(  # type: ignore[method-assign]
                MarginExchangeError(-1003, "Too many requests", http_status=429)
            )
            db_500 = os.path.join(tmpdir, "indet500.db")
            t_500 = CrossMarginTrack(db_500, case_500["client"])
            r_500 = t_500.open_position(
                case_500["intent"], case_500["snapshot"], case_500["contract"], case_500["gate"]
            )
            self.assertEqual(r_500.state, "entry_unknown")
            self.assertIsNone(r_500.account_flat)
            self.assertEqual([n for n, _ in case_500["client"].write_log].count("repay"), 0)

            # Suppose that 500 entry order actually filled on the exchange! Next tick reconciles via query_order & arms OCO without duplicate new_order or borrow!
            entry_cid = make_client_id(str(case_500["intent"]["intent_id"]), "entry")
            case_500["client"].new_order = orig_no  # type: ignore[method-assign]
            orig_no(
                {
                    "symbol": "BTCUSDT",
                    "isIsolated": "FALSE",
                    "side": "SELL",
                    "type": "LIMIT",
                    "timeInForce": "GTC",
                    "quantity": "0.01",
                    "price": "100000",
                    "newClientOrderId": entry_cid,
                    "sideEffectType": "NO_SIDE_EFFECT",
                }
            )
            # Remove the synthetic write_log entry added by orig_no simulating server-side completion of the first call
            case_500["client"].write_log.pop()
            case_500["client"].query_order = short_case()["client"].query_order.__func__.__get__(  # type: ignore[method-assign]
                case_500["client"]
            )
            r_500_rec = CrossMarginTrack(db_500, case_500["client"]).open_position(
                case_500["intent"], case_500["snapshot"], case_500["contract"], case_500["gate"]
            )
            self.assertEqual(r_500_rec.state, "protected")
            self.assertTrue(r_500_rec.protection_confirmed)
            self.assertEqual([n for n, _ in case_500["client"].write_log].count("borrow"), 1)
            self.assertEqual([n for n, _ in case_500["client"].write_log].count("new_order"), 1)
            self.assertEqual([n for n, _ in case_500["client"].write_log].count("new_oco"), 1)

    def test_t341_ambiguous_oco_2018_never_flattens_and_external_interest_blocked_pre_oco(
        self,
    ) -> None:
        """t341 closure: HTTP 400/-2018 on new_oco holds in protection_unknown (0 flatten buyback), and external interest blocks before cover_buy_quantity."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1) Spot-shaped HTTP 400 / -2018 ('Order list does not exist.') or 429 on new_oco
            # MUST NOT extrapolate spot semantics or trigger an unverified _flatten buyback!
            for err_code, err_msg, http_st in (
                (-2018, "Order list does not exist.", 400),
                (-2013, "Order does not exist.", 400),
                (-1003, "Too many requests", 429),
            ):
                case_amb = short_case()
                orig_new_oco = case_amb["client"].new_oco

                def raise_amb_oco(
                    payload: dict[str, Any],
                    _c: int = err_code,
                    _m: str = err_msg,
                    _s: int = http_st,
                    _cl: Any = case_amb["client"],
                ) -> dict[str, Any]:
                    _cl.write_log.append(("new_oco", dict(payload)))
                    raise MarginExchangeError(_c, _m, http_status=_s)

                case_amb["client"].new_oco = raise_amb_oco  # type: ignore[method-assign]
                t_amb = CrossMarginTrack(
                    os.path.join(tmpdir, f"amb_{abs(err_code)}.db"), case_amb["client"]
                )
                r_amb = t_amb.open_position(
                    case_amb["intent"],
                    case_amb["snapshot"],
                    case_amb["contract"],
                    case_amb["gate"],
                )
                self.assertEqual(r_amb.state, "protection_unknown")
                self.assertEqual(r_amb.reason, "oco_unconfirmed")
                self.assertIsNone(r_amb.account_flat)
                # Zero flatten BUY orders and zero premature repays!
                order_sides = [
                    p.get("side") for n, p in case_amb["client"].write_log if n == "new_order"
                ]
                self.assertEqual(order_sides, ["SELL"])
                self.assertEqual(
                    [n for n, _ in case_amb["client"].write_log].count("repay"), 0
                )
                del orig_new_oco

            # 2) External-principal interest accrued while entry_resting is blocked BEFORE cover_buy_quantity / new_oco!
            case_ext_pre = short_case()
            case_ext_pre["client"].interest_on_borrow = Decimal("0")
            case_ext_pre["intent"]["limit_price"] = "100500"
            case_ext_pre["contract"]["max_base_over_liability"] = "0.0010"
            db_pre = os.path.join(tmpdir, "ext_pre_oco.db")
            t_pre = CrossMarginTrack(db_pre, case_ext_pre["client"])
            resting = t_pre.open_position(
                case_ext_pre["intent"],
                case_ext_pre["snapshot"],
                case_ext_pre["contract"],
                case_ext_pre["gate"],
            )
            self.assertEqual(resting.state, "entry_resting")
            # Simulate entry fill + external principal interest row (principal 0.02 != 0.01) while resting
            entry_cid = make_client_id(str(case_ext_pre["intent"]["intent_id"]), "entry")
            ord_row = case_ext_pre["client"]._orders[entry_cid]
            case_ext_pre["client"]._unlock_remainder(ord_row)
            case_ext_pre["client"]._fill(ord_row, "BTC", "USDT", Decimal("0.01"), Decimal("100500"), "SELL")
            case_ext_pre["client"].accrue_interest(
                "BTC", Decimal("0.0002"), principal=Decimal("0.02")
            )
            blocked_oco = t_pre.open_position(
                case_ext_pre["intent"],
                case_ext_pre["snapshot"],
                case_ext_pre["contract"],
                case_ext_pre["gate"],
            )
            self.assertEqual(blocked_oco.state, "protection_blocked")
            self.assertEqual(blocked_oco.reason, "same_asset_external_interest_conflict")
            self.assertEqual(
                [n for n, _ in case_ext_pre["client"].write_log].count("new_oco"), 0
            )


if __name__ == "__main__":
    unittest.main()
