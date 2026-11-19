"""Independent audit suite for cross-margin policy, 5-state gate, and idempotency."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from decision_engine.core.cityu_institutional_alpha import due_cftc_asof
from execution.margin_cross.api_semantics import make_client_id
from execution.margin_cross.errors import MarginTransportTimeout
from execution.margin_cross.fixture import NOW_MS, client_from_snapshot, short_case
from execution.margin_cross.money import parse_decimal
from execution.margin_cross.policy import (
    APPROVED_DRY_RUN_MODE,
    ENABLE_STATE2_LONG_ENV,
    ENABLE_STATE4_LONG_ENV,
    FILL_TP_FORMULA_B_ENV,
    MCP_FORBIDDEN_UID,
    PARAMETERS_APPROVED_ENV,
    PENDING_TRANSACTION_CODE,
    env_flag_literal_one,
)
from execution.margin_cross.risk import parse_contract
from execution.margin_cross.strategy_gate import (
    UNRULED_KELLY_FRACTIONS,
    apply_formula_b_take_profit,
    build_approved_position_plan,
    check_data_freshness,
    evaluate_approved_regime_intent,
)
from execution.margin_cross.track import CrossMarginTrack


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
            )
            self.assertTrue(plan.allowed)
            assert plan.contract is not None
            parsed_off, reasons_off = parse_contract(plan.contract)
            self.assertIsNone(parsed_off)
            self.assertIn("parameters_approved_must_be_false", reasons_off)

        with patch.dict(os.environ, {PARAMETERS_APPROVED_ENV: "1"}, clear=True):
            parsed_on, reasons_on = parse_contract(plan.contract)
            self.assertEqual(reasons_on, [])
            self.assertIsNotNone(parsed_on)

    def test_effective_leverage_pool_cap_pro_account_mcp_uid_and_margin_ladder(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ, {PARAMETERS_APPROVED_ENV: "1"}, clear=True
        ):
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
            self.assertIn("notional_below_20_usdt_floor", tiny_plan.reasons)

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
        self.assertEqual(check_data_freshness(base_clocks, now_ms=NOW_MS), [])
        self.assertEqual(
            check_data_freshness(
                {**base_clocks, "volume_z_source_close_ms": NOW_MS - 3_600_000},
                now_ms=NOW_MS,
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
            reasons = check_data_freshness(mutated, now_ms=NOW_MS)
            self.assertIn(expected_code, reasons)

        closed_lseg = {
            **base_clocks,
            "lseg_market_open": False,
            "lseg_cross_asset_ts_ms": None,
        }
        self.assertEqual(check_data_freshness(closed_lseg, now_ms=NOW_MS), [])

        old_cot = (
            due_cftc_asof(datetime.fromtimestamp(NOW_MS / 1000.0, tz=timezone.utc))
            - timedelta(days=14)
        ).isoformat()
        self.assertIn(
            "cot_publication_stale_or_absent",
            check_data_freshness({**base_clocks, "cot_asof_date": old_cot}, now_ms=NOW_MS),
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

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {PARAMETERS_APPROVED_ENV: "1", FILL_TP_FORMULA_B_ENV: "1"},
            clear=True,
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


if __name__ == "__main__":
    unittest.main()
