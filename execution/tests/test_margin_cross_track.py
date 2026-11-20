"""Offline cross-margin track. Fake client and a temp SQLite file only.

The exchange is not contacted. A passing test is mock coverage, not a live fill.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from execution.margin_cross.accounting import cover_buy_quantity  # noqa: E402
from execution.margin_cross.api_semantics import (  # noqa: E402
    build_borrow_payload,
    make_client_id,
    validate_oto_payload,
    validate_otoco_payload,
)
from execution.margin_cross.errors import LiveNetworkDisabled  # noqa: E402
from execution.margin_cross.fixture import NOW_MS, client_from_snapshot, long_case, short_case  # noqa: E402
from execution.margin_cross.offline_client import OfflineFixtureClient  # noqa: E402
from execution.margin_cross.risk import parse_intent  # noqa: E402
from execution.margin_cross.track import CrossMarginTrack  # noqa: E402
from execution.tests.isolated_env import isolated_environ  # noqa: E402


def _names(client: OfflineFixtureClient) -> list[str]:
    return [name for name, _payload in client.write_log]


class MarginCrossTrackTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._ledgers = 0

    def _open(self, case: dict[str, object]) -> tuple[CrossMarginTrack, object]:
        self._ledgers += 1
        track = CrossMarginTrack(
            str(Path(self._tmp.name) / f"{self._ledgers}.db"), case["client"]
        )
        result = track.open_position(
            case["intent"], case["snapshot"], case["contract"], case["gate"]
        )
        return track, result

    def test_short_cycle_reaches_zero_liability_readback(self) -> None:
        case = short_case()
        track, opened = self._open(case)
        self.assertEqual(opened.state, "protected")
        self.assertTrue(opened.phase_ok)
        self.assertFalse(opened.account_flat)
        self.assertTrue(opened.protection_confirmed)
        self.assertTrue(opened.live_writes_blocked)
        self.assertEqual(opened.detail["protection_side"], "BUY")
        self.assertEqual(opened.detail["protection_qty"], "0.0101")
        oco = [payload for name, payload in case["client"].write_log if name == "new_oco"]
        self.assertEqual(oco[0]["side"], "BUY")
        self.assertEqual(oco[0]["sideEffectType"], "NO_SIDE_EFFECT")
        self.assertEqual(oco[0]["isIsolated"], "FALSE")
        self.assertNotIn("autoRepayAtCancel", oco[0])
        self.assertNotIn("symbol", [payload for name, payload in case["client"].write_log if name == "borrow"][0])
        borrow = [payload for name, payload in case["client"].write_log if name == "borrow"][0]
        self.assertEqual(borrow["type"], "BORROW")
        self.assertEqual(borrow["asset"], "BTC")
        self.assertNotIn("symbol", borrow)
        case["client"].fill_oco_limit(oco[0]["listClientOrderId"])
        closed = track.complete_exit(case["intent"]["intent_id"], case["gate"])
        self.assertEqual(closed.state, "clean_flat")
        self.assertTrue(closed.phase_ok)
        self.assertTrue(closed.account_flat)
        self.assertEqual(closed.borrowed, "0")
        self.assertEqual(closed.interest, "0")
        self.assertEqual(closed.base_free, "0")
        self.assertEqual(case["client"].asset("USDT")["locked"], Decimal("0"))
        self.assertEqual(_names(case["client"]).count("borrow"), 1)
        self.assertEqual(_names(case["client"]).count("new_order"), 1)
        self.assertEqual(_names(case["client"]).count("new_oco"), 1)
        self.assertEqual(_names(case["client"]).count("repay"), 1)
        again = CrossMarginTrack(str(track.ledger.path), case["client"])
        again.complete_exit(case["intent"]["intent_id"], case["gate"])
        self.assertEqual(_names(case["client"]).count("repay"), 1)
        self.assertEqual(_names(case["client"]).count("borrow"), 1)

    def test_restart_does_not_duplicate_writes(self) -> None:
        case = short_case()
        _track, opened = self._open(case)
        self.assertEqual(opened.state, "protected")
        before = list(_names(case["client"]))
        resumed = CrossMarginTrack(str(_track.ledger.path), case["client"])
        second = resumed.open_position(
            case["intent"], case["snapshot"], case["contract"], case["gate"]
        )
        self.assertEqual(second.state, "protected")
        self.assertTrue(second.protection_confirmed)
        self.assertEqual(_names(case["client"]), before)
        self.assertNotIn("cancel_order", _names(case["client"]))

    def test_borrow_timeout_after_apply_binds_once(self) -> None:
        case = short_case()
        case["client"].fail_next("borrow", when="after", kind="timeout")
        _track, opened = self._open(case)
        self.assertEqual(opened.state, "protected")
        self.assertEqual(_names(case["client"]).count("borrow"), 1)

    def test_borrow_timeout_before_apply_does_not_retry(self) -> None:
        case = short_case()
        case["client"].fail_next("borrow", when="before", kind="timeout")
        track, opened = self._open(case)
        self.assertEqual(opened.state, "borrow_unknown")
        self.assertIsNone(opened.account_flat)
        second = track.open_position(
            case["intent"], case["snapshot"], case["contract"], case["gate"]
        )
        self.assertEqual(second.state, "borrow_unknown")
        self.assertEqual(_names(case["client"]).count("borrow"), 1)
        self.assertNotIn("new_order", _names(case["client"]))

    def test_borrow_reject_does_not_sell(self) -> None:
        case = short_case()
        case["client"].fail_next("borrow", kind="reject", code=-3006, message="EXCEED_MAX_BORROWABLE")
        track, opened = self._open(case)
        self.assertEqual(opened.state, "borrow_rejected")
        self.assertFalse(opened.phase_ok)
        self.assertNotIn("new_order", _names(case["client"]))
        track.open_position(case["intent"], case["snapshot"], case["contract"], case["gate"])
        self.assertEqual(_names(case["client"]).count("borrow"), 1)

    def test_sell_reject_repays_without_claiming_an_open(self) -> None:
        case = short_case()
        case["client"].interest_on_borrow = Decimal("0")
        case["client"].fail_next("new_order", kind="reject", code=-2010, message="rejected")
        _track, opened = self._open(case)
        self.assertEqual(opened.state, "entry_rejected_unwound")
        self.assertFalse(opened.phase_ok)
        self.assertTrue(opened.account_flat)
        self.assertEqual(opened.borrowed, "0")
        self.assertEqual(_names(case["client"]).count("repay"), 1)
        self.assertNotIn("new_oco", _names(case["client"]))

    def test_sell_reject_with_interest_is_not_flat(self) -> None:
        case = short_case()
        case["client"].fail_next("new_order", kind="reject", code=-2010, message="rejected")
        _track, opened = self._open(case)
        self.assertEqual(opened.state, "residual_liability_after_unwind")
        self.assertFalse(opened.account_flat)
        self.assertNotEqual(opened.borrowed, "0")

    def test_sell_timeout_does_not_retry_or_repay(self) -> None:
        case = short_case()
        case["client"].fail_next("new_order", when="before", kind="timeout")
        track, opened = self._open(case)
        self.assertEqual(opened.state, "entry_unknown")
        self.assertNotIn("repay", _names(case["client"]))
        track.open_position(case["intent"], case["snapshot"], case["contract"], case["gate"])
        self.assertEqual(_names(case["client"]).count("new_order"), 1)
        self.assertNotIn("repay", _names(case["client"]))

    def test_partial_cancel_timeout_places_no_oco(self) -> None:
        case = short_case()
        case["client"].interest_on_borrow = Decimal("0")
        case["client"].partial_fill_qty = Decimal("0.004")
        case["client"].fail_next("cancel_order", when="before", kind="timeout")
        track, opened = self._open(case)
        self.assertEqual(opened.state, "partial_cancel_unknown")
        self.assertNotIn("new_oco", _names(case["client"]))
        track.open_position(case["intent"], case["snapshot"], case["contract"], case["gate"])
        self.assertEqual(_names(case["client"]).count("new_order"), 1)
        self.assertEqual(_names(case["client"]).count("cancel_order"), 1)

    def test_partial_fill_repays_unsold_and_protects_remainder(self) -> None:
        case = short_case()
        case["client"].interest_on_borrow = Decimal("0")
        case["client"].partial_fill_qty = Decimal("0.004")
        _track, opened = self._open(case)
        self.assertEqual(opened.state, "protected")
        self.assertEqual(opened.detail["protection_qty"], "0.004")
        self.assertEqual(opened.detail["protection_side"], "BUY")
        self.assertEqual(_names(case["client"]).count("repay"), 1)

    def test_delayed_trades_block_oco_until_they_match(self) -> None:
        case = short_case()
        case["client"].query_trades_empty = 1
        track, opened = self._open(case)
        self.assertEqual(opened.state, "fill_unconfirmed")
        self.assertNotIn("new_oco", _names(case["client"]))
        second = track.open_position(
            case["intent"], case["snapshot"], case["contract"], case["gate"]
        )
        self.assertEqual(second.state, "protected")
        self.assertEqual(_names(case["client"]).count("new_order"), 1)
        self.assertEqual(_names(case["client"]).count("new_oco"), 1)

    def test_trade_qty_mismatch_blocks_protection(self) -> None:
        case = short_case()
        case["client"].trade_qty_override = Decimal("0.009")
        _track, opened = self._open(case)
        self.assertEqual(opened.state, "fill_mismatch")
        self.assertNotIn("new_oco", _names(case["client"]))

    def test_order_ack_missing_on_query_is_not_filled(self) -> None:
        case = short_case()
        case["client"].query_order_misses = 1
        track, opened = self._open(case)
        self.assertEqual(opened.state, "fill_unconfirmed")
        self.assertNotIn("new_oco", _names(case["client"]))
        second = track.open_position(
            case["intent"], case["snapshot"], case["contract"], case["gate"]
        )
        self.assertEqual(second.state, "protected")
        self.assertEqual(_names(case["client"]).count("new_order"), 1)

    def test_oco_reject_does_not_flatten_and_restart_does_not_buy_back(self) -> None:
        case = short_case()
        case["client"].fail_next("new_oco", kind="reject", code=-2010, message="oco rejected")
        track, opened = self._open(case)
        self.assertEqual(opened.state, "protection_unknown")
        self.assertFalse(opened.phase_ok)
        self.assertIsNone(opened.account_flat)
        self.assertNotIn("repay", _names(case["client"]))
        flatten_id = make_client_id(str(case["intent"]["intent_id"]), "flatten")
        self.assertFalse(
            any(payload.get("newClientOrderId") == flatten_id for _name, payload in case["client"].write_log)
        )
        before = list(_names(case["client"]))
        second = track.open_position(
            case["intent"], case["snapshot"], case["contract"], case["gate"]
        )
        self.assertEqual(second.state, "protection_unknown")
        self.assertEqual(_names(case["client"]), before)

    def test_oco_timeout_unknown_does_not_resubmit(self) -> None:
        case = short_case()
        case["client"].fail_next("new_oco", when="before", kind="timeout")
        track, opened = self._open(case)
        self.assertEqual(opened.state, "protection_unknown")
        self.assertIsNone(opened.account_flat)
        track.open_position(case["intent"], case["snapshot"], case["contract"], case["gate"])
        self.assertEqual(_names(case["client"]).count("new_oco"), 1)
        self.assertNotIn("cancel_order", _names(case["client"]))

    def test_oco_timeout_after_apply_binds(self) -> None:
        case = short_case()
        case["client"].fail_next("new_oco", when="after", kind="timeout")
        _track, opened = self._open(case)
        self.assertEqual(opened.state, "protected")
        self.assertEqual(_names(case["client"]).count("new_oco"), 1)

    def test_repay_ack_without_liability_change_is_not_flat(self) -> None:
        case = short_case()
        track, opened = self._open(case)
        self.assertEqual(opened.state, "protected")
        oco = next(payload for name, payload in case["client"].write_log if name == "new_oco")
        case["client"].fill_oco_limit(oco["listClientOrderId"])
        case["client"].repay_ack_without_apply = True
        closed = track.complete_exit(case["intent"]["intent_id"], case["gate"])
        self.assertEqual(closed.state, "repay_unconfirmed")
        self.assertFalse(closed.account_flat)
        self.assertGreater(Decimal(closed.borrowed) + Decimal(closed.interest), 0)
        track.complete_exit(case["intent"]["intent_id"], case["gate"])
        self.assertEqual(_names(case["client"]).count("repay"), 1)

    def test_repay_reject_does_not_retry(self) -> None:
        case = short_case()
        track, _opened = self._open(case)
        oco = next(payload for name, payload in case["client"].write_log if name == "new_oco")
        case["client"].fill_oco_limit(oco["listClientOrderId"])
        case["client"].fail_next("repay", kind="reject", code=-3015, message="REPAY_EXCEED_LIABILITY")
        closed = track.complete_exit(case["intent"]["intent_id"], case["gate"])
        self.assertEqual(closed.state, "repay_rejected")
        self.assertFalse(closed.phase_ok)
        track.complete_exit(case["intent"]["intent_id"], case["gate"])
        self.assertEqual(_names(case["client"]).count("repay"), 1)

    def test_interest_growth_does_not_report_flat_or_send_a_second_buy(self) -> None:
        case = short_case()
        track, _opened = self._open(case)
        oco = next(payload for name, payload in case["client"].write_log if name == "new_oco")
        case["client"].asset("BTC")["interest"] += Decimal("0.0001")
        case["client"].fill_oco_limit(oco["listClientOrderId"])
        closed = track.complete_exit(case["intent"]["intent_id"], case["gate"])
        self.assertEqual(closed.state, "not_flat")
        self.assertEqual(closed.reason, "base_short_of_liability")
        self.assertNotIn("repay", _names(case["client"]))
        self.assertEqual(_names(case["client"]).count("new_order"), 1)

    def test_horizon_reserve_is_not_a_clean_flat(self) -> None:
        case = short_case()
        case["snapshot"]["hourly_interest_rate"]["BTC"] = "0.01"
        case["client"].hourly_rate["BTC"] = Decimal("0.01")
        case["contract"]["interest_horizon_hours"] = "1"
        case["contract"]["max_base_over_liability"] = "0.0001"
        track, opened = self._open(case)
        self.assertEqual(opened.state, "protected")
        self.assertEqual(opened.detail["protection_qty"], "0.0102")
        oco = next(payload for name, payload in case["client"].write_log if name == "new_oco")
        case["client"].fill_oco_limit(oco["listClientOrderId"])
        closed = track.complete_exit(case["intent"]["intent_id"], case["gate"])
        self.assertEqual(closed.state, "not_flat")
        self.assertFalse(closed.account_flat)
        self.assertEqual(closed.base_free, "0.0001")

    def test_horizon_above_max_blocks_the_oco(self) -> None:
        case = short_case()
        case["snapshot"]["hourly_interest_rate"]["BTC"] = "0.01"
        case["client"].hourly_rate["BTC"] = Decimal("0.01")
        case["contract"]["interest_horizon_hours"] = "1"
        _track, opened = self._open(case)
        self.assertEqual(opened.state, "protection_blocked")
        self.assertEqual(opened.reason, "cover_exceeds_max_base_over_liability")
        self.assertFalse(opened.phase_ok)
        self.assertNotIn("new_oco", _names(case["client"]))

    def test_btc_borrow_limit_is_not_taken_from_usdt(self) -> None:
        case = short_case()
        case["snapshot"]["max_borrowable"]["BTC"] = "0.001"
        case["snapshot"]["max_borrowable"]["USDT"] = "10000"
        case["client"].max_borrow["BTC"] = Decimal("0.001")
        _track, opened = self._open(case)
        self.assertEqual(opened.reason, "asset_borrow_insufficient")
        self.assertEqual(case["client"].write_log, [])
        case = short_case()
        case["client"].max_borrow["BTC"] = Decimal("0.001")
        _track, opened = self._open(case)
        self.assertEqual(opened.reason, "asset_borrow_readback_mismatch")
        assets = [payload.get("asset") for name, payload in case["client"].read_log if name == "query_max_borrow"]
        self.assertEqual(assets, ["BTC"])

    def test_filters_liquidity_identity_and_mode_lock(self) -> None:
        case = short_case()
        case["intent"]["limit_price"] = "100000.001"
        self.assertEqual(self._open(case)[1].reason, "limit_price_off_tick")
        case = short_case()
        case["snapshot"]["filters"]["min_notional"] = "1000000"
        case["client"] = client_from_snapshot(case["snapshot"], Decimal("0.0001"))
        self.assertEqual(self._open(case)[1].reason, "entry_notional_below_min")
        case = short_case()
        case["snapshot"]["bid_qty"] = "0.001"
        case["client"] = client_from_snapshot(case["snapshot"], Decimal("0.0001"))
        self.assertEqual(self._open(case)[1].reason, "sell_liquidity_insufficient")
        case = short_case()
        case["snapshot"]["margin_level"] = "NaN"
        self.assertEqual(self._open(case)[1].state, "locked")
        case = short_case()
        case["snapshot"]["observed_at_ms"] = NOW_MS - 60000
        self.assertEqual(self._open(case)[1].reason, "snapshot_stale_or_future")
        case = short_case()
        case["intent"]["account_uid"] = "other"
        self.assertEqual(self._open(case)[1].reason, "uid_mismatch")
        case = short_case()
        case["client"].account_uid = "someone-else"
        self.assertEqual(self._open(case)[1].reason, "uid_not_bound_on_client")
        self.assertEqual(case["client"].write_log, [])
        case = short_case()
        case["client"].margin_mode = "ISOLATED"
        self.assertEqual(self._open(case)[1].reason, "client_mode_not_cross")
        case = short_case()
        case["intent"]["mode"] = "ISOLATED"
        self.assertEqual(self._open(case)[1].reason, "mode_not_cross")
        case = short_case()
        case["snapshot"]["account_type"] = "ISOLATED_MARGIN"
        self.assertEqual(self._open(case)[1].reason, "isolated_account_rejected")
        case = short_case()
        case["intent"]["quantity"] = 0.01
        self.assertEqual(self._open(case)[1].reason, "quantity_invalid")
        for result_case, reason in (
            (self._strategy("regime_2_short_squeeze_breakout"), "strategy_not_in_offline_fixture_allowlist"),
            (self._purpose("live_bnb_boll_fade"), "purpose_not_cross_margin_track"),
        ):
            self.assertEqual(self._open(result_case)[1].reason, reason)
            self.assertEqual(result_case["client"].write_log, [])

    def test_risk_drop_unwinds_and_blocks_a_new_borrow(self) -> None:
        case = short_case()
        case["client"].interest_on_borrow = Decimal("0")
        case["client"].after_borrow_margin_level = Decimal("1")
        _track, opened = self._open(case)
        self.assertEqual(opened.state, "risk_dropped_unwound")
        self.assertTrue(opened.account_flat)
        self.assertNotIn("new_order", _names(case["client"]))
        fresh = short_case()
        fresh["snapshot"]["margin_level"] = "1"
        fresh["client"].margin_level = Decimal("1")
        _track, blocked = self._open(fresh)
        self.assertEqual(blocked.reason, "margin_level_below_contract")
        self.assertEqual(fresh["client"].write_log, [])

    def test_emergency_stop_blocks_new_risk_and_keeps_working_protection(self) -> None:
        case = short_case()
        case["gate"]["emergency_stop"] = True
        _track, opened = self._open(case)
        self.assertEqual(opened.state, "emergency_stop_blocks_new_risk")
        self.assertEqual(case["client"].write_log, [])
        case = short_case()
        case["client"].interest_on_borrow = Decimal("0")
        case["client"].interrupt_after = "borrow"
        track, paused = self._open(case)
        self.assertEqual(paused.state, "interrupted_after_borrow")
        case["client"].interrupt_after = None
        case["gate"]["emergency_stop"] = True
        unwound = track.open_position(
            case["intent"], case["snapshot"], case["contract"], case["gate"]
        )
        self.assertEqual(unwound.state, "emergency_unwound")
        self.assertTrue(unwound.account_flat)
        self.assertNotIn("new_order", _names(case["client"]))
        self.assertIn("repay", _names(case["client"]))
        protected = short_case()
        track, opened = self._open(protected)
        self.assertEqual(opened.state, "protected")
        before = len(protected["client"].write_log)
        protected["gate"]["emergency_stop"] = True
        again = track.open_position(
            protected["intent"], protected["snapshot"], protected["contract"], protected["gate"]
        )
        self.assertEqual(again.state, "protected")
        self.assertEqual(len(protected["client"].write_log), before)
        resting = track.complete_exit(protected["intent"]["intent_id"], protected["gate"])
        self.assertEqual(resting.state, "protection_still_working")
        self.assertEqual(len(protected["client"].write_log), before)

    def test_side_effect_and_isolated_response_stop_the_oco(self) -> None:
        case = short_case()
        case["client"].margin_buy_borrow_amount = Decimal("1")
        _track, opened = self._open(case)
        self.assertEqual(opened.state, "side_effect_violation")
        self.assertNotIn("new_oco", _names(case["client"]))
        case = short_case()
        case["client"].force_isolated_response = True
        _track, opened = self._open(case)
        self.assertEqual(opened.state, "side_effect_violation")
        self.assertNotIn("new_oco", _names(case["client"]))

    def test_long_protection_is_sell_and_closes_without_borrow(self) -> None:
        case = long_case()
        track, opened = self._open(case)
        self.assertEqual(opened.state, "protected")
        self.assertEqual(opened.detail["protection_side"], "SELL")
        self.assertNotIn("borrow", _names(case["client"]))
        oco = next(payload for name, payload in case["client"].write_log if name == "new_oco")
        self.assertEqual(oco["side"], "SELL")
        case["client"].fill_oco_limit(oco["listClientOrderId"])
        closed = track.complete_exit(case["intent"]["intent_id"], case["gate"])
        self.assertEqual(closed.state, "clean_flat")
        self.assertNotIn("repay", _names(case["client"]))
        self.assertNotIn("borrow", _names(case["client"]))

    def test_base_fee_long_fill_places_one_oco_for_the_net_base(self) -> None:
        case = long_case()
        case["client"].fee_rate = Decimal("0.0000001")
        case["client"].fee_asset = "BTC"
        track, opened = self._open(case)
        self.assertEqual(opened.state, "protected", opened.reason)
        self.assertTrue(opened.protection_confirmed)
        oco = [payload for name, payload in case["client"].write_log if name == "new_oco"]
        self.assertEqual(len(oco), 1)
        self.assertEqual(oco[0]["side"], "SELL")
        self.assertEqual(oco[0]["quantity"], "0.0099")
        orders = [name for name in _names(case["client"]) if name == "new_order"]
        again = track.open_position(
            case["intent"], case["snapshot"], case["contract"], case["gate"]
        )
        self.assertEqual(again.state, "protected")
        self.assertEqual(_names(case["client"]).count("new_order"), len(orders))
        self.assertEqual(_names(case["client"]).count("new_oco"), 1)

    def test_resting_long_is_resumed_after_fill_without_a_second_entry(self) -> None:
        case = long_case()
        case["intent"]["limit_price"] = "99000"
        case["client"].market_price = Decimal("100000")
        track, opened = self._open(case)
        self.assertEqual(opened.state, "entry_resting")
        self.assertNotIn("new_oco", _names(case["client"]))
        entry_key = make_client_id(case["intent"]["intent_id"], "entry")
        order = case["client"]._orders[entry_key]
        self.assertEqual(order["status"], "NEW")
        order["status"] = "FILLED"
        order["executedQty"] = order["origQty"]
        case["client"]._trades[int(order["orderId"])] = [
            {
                "id": 1,
                "orderId": order["orderId"],
                "symbol": order["symbol"],
                "price": order["price"],
                "qty": order["origQty"],
                "commission": "0",
                "commissionAsset": "USDT",
                "isBuyer": True,
                "isMaker": True,
                "isIsolated": False,
            }
        ]
        quote = case["client"].asset("USDT")
        quote["locked"] = Decimal("0")
        quote["free"] = Decimal("4010")
        case["client"].asset("BTC")["free"] = Decimal(order["origQty"])
        resumed = track.open_position(
            case["intent"], case["snapshot"], case["contract"], case["gate"]
        )
        self.assertEqual(resumed.state, "protected", resumed.reason)
        self.assertTrue(resumed.protection_confirmed)
        self.assertEqual(_names(case["client"]).count("new_order"), 1)
        self.assertEqual(_names(case["client"]).count("new_oco"), 1)
        repeated = track.open_position(
            case["intent"], case["snapshot"], case["contract"], case["gate"]
        )
        self.assertEqual(repeated.state, "protected")
        self.assertEqual(_names(case["client"]).count("new_order"), 1)
        self.assertEqual(_names(case["client"]).count("new_oco"), 1)

    def test_dual_base_fee_floors_the_oco_and_keeps_the_residual(self) -> None:
        case = long_case()
        case["intent"]["quantity"] = "13828.6"
        case["intent"]["limit_price"] = "0.02070"
        case["intent"]["take_profit_price"] = "0.02152"
        case["intent"]["stop_price"] = "0.02056"
        case["intent"]["stop_limit_price"] = "0.02051"
        case["snapshot"]["reference_price"] = "0.02078"
        case["snapshot"]["bid_qty"] = "100000000"
        case["snapshot"]["ask_qty"] = "100000000"
        case["snapshot"]["filters"]["step_size"] = "0.1"
        case["snapshot"]["filters"]["tick_size"] = "0.00001"
        case["client"].market_price = Decimal("0.02078")
        case["client"].bid_qty = Decimal("100000000")
        case["client"].ask_qty = Decimal("100000000")
        track, opened = self._open(case)
        self.assertEqual(opened.state, "entry_resting", opened.reason)
        entry_key = make_client_id(case["intent"]["intent_id"], "entry")
        order = case["client"]._orders[entry_key]
        order["status"] = "FILLED"
        order["executedQty"] = "13828.6"
        case["client"]._trades[int(order["orderId"])] = [
            {
                "id": 1,
                "orderId": order["orderId"],
                "symbol": "BTCUSDT",
                "price": "0.02078",
                "qty": "692",
                "commission": "0.692",
                "commissionAsset": "BTC",
                "isBuyer": True,
                "isMaker": True,
                "isIsolated": False,
            },
            {
                "id": 2,
                "orderId": order["orderId"],
                "symbol": "BTCUSDT",
                "price": "0.02078",
                "qty": "13136.6",
                "commission": "13.1366",
                "commissionAsset": "BTC",
                "isBuyer": True,
                "isMaker": True,
                "isIsolated": False,
            },
        ]
        case["client"].asset("USDT")["locked"] = Decimal("0")
        case["client"].asset("USDT")["free"] = Decimal("0.37305035")
        case["client"].asset("BTC")["free"] = Decimal("13814.7714")
        resumed = track.open_position(
            case["intent"], case["snapshot"], case["contract"], case["gate"]
        )
        self.assertEqual(resumed.state, "protected", resumed.reason)
        self.assertEqual(resumed.detail["oco_lot_residual_qty"], "0.0714")
        oco = [payload for name, payload in case["client"].write_log if name == "new_oco"]
        self.assertEqual(len(oco), 1)
        self.assertEqual(oco[0]["quantity"], "13814.7")
        self.assertNotIn("oco_lot_residual_qty", oco[0])
        again = track.open_position(
            case["intent"], case["snapshot"], case["contract"], case["gate"]
        )
        self.assertEqual(again.state, "protected")
        self.assertEqual(again.detail["oco_lot_residual_qty"], "0.0714")
        self.assertEqual(_names(case["client"]).count("new_order"), 1)
        self.assertEqual(_names(case["client"]).count("new_oco"), 1)
        for order in case["client"]._orders.values():
            order["status"] = "FILLED"
        case["client"].asset("BTC")["locked"] = Decimal("0")
        case["client"].asset("USDT")["locked"] = Decimal("0")
        case["client"].asset("BTC")["free"] = Decimal("0.0714")
        parsed, reasons = parse_intent(case["intent"])
        self.assertEqual(reasons, [])
        assert parsed is not None
        self.assertTrue(track._is_flat(parsed))
        case["client"].asset("BTC")["free"] = Decimal("0.1")
        self.assertFalse(track._is_flat(parsed))

    def test_free_equal_to_floor_is_enough_and_dust_alone_is_not(self) -> None:
        case = long_case()
        case["intent"]["quantity"] = "10"
        case["intent"]["limit_price"] = "100"
        case["snapshot"]["reference_price"] = "100"
        case["snapshot"]["bid_qty"] = "100"
        case["snapshot"]["ask_qty"] = "100"
        case["snapshot"]["filters"]["step_size"] = "0.1"
        case["snapshot"]["filters"]["tick_size"] = "0.01"
        case["snapshot"]["filters"]["min_notional"] = "5"
        case["intent"]["take_profit_price"] = "101"
        case["intent"]["stop_price"] = "99"
        case["intent"]["stop_limit_price"] = "98.9"
        case["client"].market_price = Decimal("101")
        case["client"].bid_qty = Decimal("100")
        case["client"].ask_qty = Decimal("100")
        track, opened = self._open(case)
        self.assertEqual(opened.state, "entry_resting")
        entry_key = make_client_id(case["intent"]["intent_id"], "entry")
        order = case["client"]._orders[entry_key]
        order["status"] = "FILLED"
        order["executedQty"] = "10"
        case["client"]._trades[int(order["orderId"])] = [
            {
                "id": 1,
                "orderId": order["orderId"],
                "symbol": "BTCUSDT",
                "price": "100",
                "qty": "10",
                "commission": "0.05",
                "commissionAsset": "BTC",
                "isBuyer": True,
                "isMaker": True,
                "isIsolated": False,
            }
        ]
        case["client"].asset("USDT")["locked"] = Decimal("0")
        case["client"].asset("BTC")["free"] = Decimal("9.9")
        exact = track.open_position(
            case["intent"], case["snapshot"], case["contract"], case["gate"]
        )
        self.assertEqual(exact.state, "protected", exact.reason)
        self.assertEqual(
            next(payload["quantity"] for name, payload in case["client"].write_log if name == "new_oco"),
            "9.9",
        )

    def test_quote_or_bnb_fee_does_not_shrink_the_base_lot(self) -> None:
        for asset in ("USDT", "BNB"):
            case = long_case()
            case["client"].fee_rate = Decimal("0.001")
            case["client"].fee_asset = asset
            track, opened = self._open(case)
            self.assertEqual(opened.state, "protected", opened.reason)
            oco = next(payload for name, payload in case["client"].write_log if name == "new_oco")
            self.assertEqual(oco["quantity"], "0.01")

    def test_bid_qty_still_blocks_a_floored_long(self) -> None:
        case = long_case()
        case["intent"]["quantity"] = "13828.6"
        case["intent"]["limit_price"] = "0.02070"
        case["intent"]["take_profit_price"] = "0.02152"
        case["intent"]["stop_price"] = "0.02056"
        case["intent"]["stop_limit_price"] = "0.02051"
        case["snapshot"]["reference_price"] = "0.02078"
        case["snapshot"]["bid_qty"] = "5042.1"
        case["snapshot"]["ask_qty"] = "100000000"
        case["snapshot"]["filters"]["step_size"] = "0.1"
        case["snapshot"]["filters"]["tick_size"] = "0.00001"
        case["client"].market_price = Decimal("0.02078")
        case["client"].bid_qty = Decimal("5042.1")
        case["client"].ask_qty = Decimal("100000000")
        track, opened = self._open(case)
        self.assertEqual(opened.state, "entry_resting")
        entry_key = make_client_id(case["intent"]["intent_id"], "entry")
        order = case["client"]._orders[entry_key]
        order["status"] = "FILLED"
        order["executedQty"] = "13828.6"
        case["client"]._trades[int(order["orderId"])] = [
            {
                "id": 1,
                "orderId": order["orderId"],
                "symbol": "BTCUSDT",
                "price": "0.02078",
                "qty": "13828.6",
                "commission": "13.8286",
                "commissionAsset": "BTC",
                "isBuyer": True,
                "isMaker": True,
                "isIsolated": False,
            }
        ]
        case["client"].asset("USDT")["locked"] = Decimal("0")
        case["client"].asset("BTC")["free"] = Decimal("13814.7714")
        blocked = track.open_position(
            case["intent"], case["snapshot"], case["contract"], case["gate"]
        )
        self.assertEqual(blocked.state, "protection_blocked")
        self.assertEqual(blocked.reason, "sell_liquidity_insufficient")
        self.assertFalse(blocked.protection_confirmed)
        self.assertNotIn("new_oco", _names(case["client"]))

    def test_resting_entry_is_cancelled_on_emergency_not_while_working(self) -> None:
        case = short_case()
        case["client"].interest_on_borrow = Decimal("0")
        case["intent"]["limit_price"] = "100100"
        case["snapshot"]["reference_price"] = "100000"
        track, opened = self._open(case)
        self.assertEqual(opened.state, "entry_resting")
        self.assertNotIn("cancel_order", _names(case["client"]))
        case["gate"]["emergency_stop"] = True
        stopped = track.open_position(
            case["intent"], case["snapshot"], case["contract"], case["gate"]
        )
        self.assertEqual(stopped.state, "emergency_unwound")
        self.assertIn("cancel_order", _names(case["client"]))
        self.assertTrue(stopped.account_flat)

    def test_live_mode_and_network_client_are_refused(self) -> None:
        case = short_case()
        case["gate"]["execution_mode"] = "live"
        _track, opened = self._open(case)
        self.assertEqual(opened.state, "live_network_disabled")
        self.assertEqual(case["client"].write_log, [])
        case["client"].performs_network_io = True
        with self.assertRaises(LiveNetworkDisabled):
            CrossMarginTrack(str(Path(self._tmp.name) / "live.db"), case["client"])

    def test_otoco_is_not_submitted_and_validators_match_the_sdk(self) -> None:
        case = short_case()
        track = CrossMarginTrack(str(Path(self._tmp.name) / "otoco.db"), case["client"])
        refused = track.submit_otoco(
            {
                "symbol": "BTCUSDT",
                "workingType": "LIMIT",
                "workingSide": "SELL",
                "workingPrice": "100000",
                "workingQuantity": "0.01",
                "workingTimeInForce": "GTC",
                "pendingSide": "BUY",
                "pendingQuantity": "0.01",
                "pendingAboveType": "STOP_LOSS_LIMIT",
                "pendingAbovePrice": "100500",
                "pendingAboveStopPrice": "100400",
                "pendingAboveTimeInForce": "GTC",
                "pendingBelowType": "LIMIT_MAKER",
                "pendingBelowPrice": "99500",
                "sideEffectType": "NO_SIDE_EFFECT",
                "isIsolated": "FALSE",
            }
        )
        self.assertEqual(refused.reason, "otoco_not_submitted_qty_coupling_unverified")
        self.assertEqual(case["client"].write_log, [])
        self.assertEqual(refused.detail["validation"], [])
        self.assertIn("auto_repay_not_in_otoco_enum", validate_otoco_payload({"sideEffectType": "AUTO_REPAY"}))
        self.assertIn("workingTimeInForce_required", validate_oto_payload({"workingType": "LIMIT", "pendingType": "LIMIT"}))
        self.assertIn(
            "side_effect_forbidden_explicit_borrow_repay_only",
            validate_oto_payload({"workingType": "LIMIT_MAKER", "pendingType": "STOP_LOSS", "pendingStopPrice": "1", "sideEffectType": "MARGIN_BUY", "symbol": "BTCUSDT", "workingSide": "BUY", "workingPrice": "1", "workingQuantity": "1", "pendingSide": "SELL", "pendingQuantity": "1"}),
        )
        payload = build_borrow_payload("BTC", Decimal("0.01"))
        self.assertEqual(payload["isIsolated"], "FALSE")
        self.assertNotIn("symbol", payload)

    def test_cover_math_and_package_boundaries(self) -> None:
        quantity, reason = cover_buy_quantity(
            borrowed=Decimal("0.01"),
            interest=Decimal("0.0001"),
            hourly_rate=Decimal("0"),
            horizon_hours=Decimal("0"),
            step=Decimal("0.0001"),
            fee_rate=Decimal("0.001"),
            fee_on_base=True,
            max_base_over_liability=Decimal("0"),
        )
        self.assertIsNone(quantity)
        self.assertEqual(reason, "cover_exceeds_max_base_over_liability")
        package = ROOT / "execution" / "margin_cross"
        for path in package.glob("*.py"):
            if path.name in {"proposal.py", "fixture.py", "entry.py"}:
                continue
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    self.assertNotIn("executors", module)
                    self.assertNotIn("binance_spot", module)
                    self.assertNotIn("binance_perp_short", module)
                    self.assertNotIn("proposal", module)
                    self.assertEqual(node.col_offset, 0)
                elif isinstance(node, ast.Import):
                    self.assertEqual(node.col_offset, 0)
        risk = (package / "risk.py").read_text(encoding="utf-8")
        self.assertNotIn("0.24", risk)
        self.assertNotIn("24%", risk)

    def test_dry_run_command_and_live_flag(self) -> None:
        ledger = str(Path(self._tmp.name) / "command.db")
        env = isolated_environ({"PYTHONPATH": str(ROOT)})
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "execution.margin_cross.entry",
                "--dry-run",
                "--fixture",
                "short-cycle",
                "--ledger",
                ledger,
            ],
            cwd=str(ROOT),
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["exchange_empirical"])
        self.assertTrue(report["mock_coverage"])
        self.assertFalse(report["parameters_approved"])
        self.assertTrue(report["production_halt_unchanged"])
        self.assertEqual(report["close"]["state"], "clean_flat")
        self.assertEqual(report["writes"].count("borrow"), 1)
        refused = subprocess.run(
            [sys.executable, "-m", "execution.margin_cross.entry", "--live"],
            cwd=str(ROOT),
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(refused.returncode, 2)
        self.assertIn("disabled", refused.stderr)
        proposal = subprocess.run(
            [sys.executable, "-m", "execution.margin_cross.entry", "--show-proposal"],
            cwd=str(ROOT),
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proposal.returncode, 0, proposal.stderr)
        rows = json.loads(proposal.stdout)
        self.assertTrue(rows)
        self.assertTrue(all(row["approved"] is False for row in rows))

    def _strategy(self, strategy_id: str) -> dict[str, object]:
        case = short_case()
        case["intent"]["strategy_id"] = strategy_id
        case["contract"]["strategy_id"] = strategy_id
        return case

    def _purpose(self, purpose: str) -> dict[str, object]:
        case = short_case()
        case["intent"]["purpose"] = purpose
        return case


if __name__ == "__main__":
    unittest.main()
