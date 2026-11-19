"""Decision signal to the existing cross-margin entry. Offline fixture client only."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from decision_engine.core.execution_account_route import (
    preserve_explicit_account_domain,
    route_request_from_signal,
)
from execution.account_dispatch import dispatch_account_route
from execution.margin_cross.fixture import short_case
from execution.margin_cross.policy import SUPPORTED_SHORT_STRATEGIES
from execution.margin_cross.risk import OFFLINE_FIXTURE_STRATEGY_ID


class AccountDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.ledger = str(Path(self._dir.name) / "margin.db")
        self._saved = {
            key: os.environ.get(key)
            for key in (
                "CROSS_MARGIN_PARAMETERS_APPROVED",
                "CROSS_MARGIN_LIVE_ENABLED",
            )
        }
        os.environ.pop("CROSS_MARGIN_PARAMETERS_APPROVED", None)
        os.environ.pop("CROSS_MARGIN_LIVE_ENABLED", None)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._dir.cleanup()

    def test_missing_domain_stays_on_spot_and_does_not_call_margin(self) -> None:
        seen: list[str] = []
        signal = preserve_explicit_account_domain({"action": "buy", "symbol": "BTCUSDT"})
        request = route_request_from_signal(signal)
        result = dispatch_account_route(
            request,
            ledger_path=self.ledger,
            spot_submit=lambda _sig: seen.append("spot") or {"ok": True},
        )
        self.assertEqual(result["venue"], "binance_spot")
        self.assertTrue(result["spot_called"])
        self.assertFalse(result["margin_called"])
        self.assertEqual(seen, ["spot"])
        self.assertNotIn("account_domain", signal)

    def test_offline_signal_reaches_track_and_restart_does_not_borrow_again(self) -> None:
        case = short_case()
        signal = {
            "account_domain": "binance_cross_margin",
            "spot_book_full": False,
            "strategy_id": OFFLINE_FIXTURE_STRATEGY_ID,
            "execution_mode": "offline_fixture",
            "intent": case["intent"],
            "snapshot": case["snapshot"],
            "contract": case["contract"],
            "gate": case["gate"],
            "client": case["client"],
        }
        request = route_request_from_signal(signal)
        first = dispatch_account_route(request, ledger_path=self.ledger, spot_submit=lambda _sig: {})
        self.assertTrue(first["margin_called"])
        self.assertEqual(first["entry"], "execution.margin_cross.entry.open_offline_routed")
        self.assertFalse(first["spot_called"])
        self.assertEqual(first["writes"].count("borrow"), 1)
        second = dispatch_account_route(request, ledger_path=self.ledger, spot_submit=lambda _sig: {})
        self.assertTrue(second["margin_called"])
        self.assertEqual(second["writes"].count("borrow"), 1)

    def test_spot_full_rejects_a_long_strategy_before_the_track(self) -> None:
        case = short_case()
        signal = {
            "account_domain": "binance_cross_margin",
            "spot_book_full": True,
            "strategy_id": "regime1_positive_gamma_fade",
            "execution_mode": "offline_fixture",
            "intent": case["intent"],
            "snapshot": case["snapshot"],
            "contract": case["contract"],
            "gate": case["gate"],
            "client": case["client"],
        }
        result = dispatch_account_route(
            route_request_from_signal(signal),
            ledger_path=self.ledger,
            spot_submit=lambda _sig: {"should_not": True},
        )
        self.assertEqual(result["reason"], "spot_full_only_three_shorts")
        self.assertFalse(result["margin_called"])
        self.assertFalse(result["spot_called"])
        self.assertEqual(case["client"].write_log, [])

    def test_spot_full_allows_only_the_three_short_ids(self) -> None:
        self.assertEqual(
            SUPPORTED_SHORT_STRATEGIES,
            {
                "regime4_crowded_long_carry_short",
                "regime5_crisis_contagion_short",
                "regime5_unlock_cliff_short",
            },
        )

    def test_both_venues_and_missing_topology_do_not_submit(self) -> None:
        both = dispatch_account_route(
            {
                "account_domain": "binance_cross_margin",
                "submit_spot": True,
                "spot_book_full": False,
                "strategy_id": OFFLINE_FIXTURE_STRATEGY_ID,
            },
            ledger_path=self.ledger,
            spot_submit=lambda _sig: {"spot": True},
        )
        self.assertEqual(both["reason"], "duplicate_spot_and_margin")
        self.assertFalse(both["margin_called"])
        self.assertFalse(both["spot_called"])
        missing = dispatch_account_route(
            {
                "account_domain": "binance_cross_margin",
                "strategy_id": OFFLINE_FIXTURE_STRATEGY_ID,
                "execution_mode": "offline_fixture",
            },
            ledger_path=self.ledger,
        )
        self.assertEqual(missing["reason"], "topology_unspecified")
        self.assertFalse(missing["margin_called"])

    def test_uid_mismatch_and_unapproved_live_plan_do_not_call_the_track(self) -> None:
        case = short_case()
        intent = dict(case["intent"])
        intent["account_uid"] = "other-uid"
        mismatched = dispatch_account_route(
            {
                "account_domain": "binance_cross_margin",
                "spot_book_full": False,
                "strategy_id": OFFLINE_FIXTURE_STRATEGY_ID,
                "execution_mode": "offline_fixture",
                "intent": intent,
                "snapshot": case["snapshot"],
                "contract": case["contract"],
                "gate": case["gate"],
                "client": case["client"],
            },
            ledger_path=self.ledger,
        )
        self.assertEqual(mismatched["reason"], "account_uid_mismatch")
        self.assertFalse(mismatched["margin_called"])
        self.assertEqual(case["client"].write_log, [])
        denied = dispatch_account_route(
            {
                "account_domain": "binance_cross_margin",
                "spot_book_full": False,
                "strategy_id": "regime5_crisis_contagion_short",
                "execution_mode": "approved_dry_run",
                "symbol": "BTCUSDT",
                "base_asset": "BTC",
                "requested_action": "OPEN_SHORT",
                "now_ms": 1_700_000_000_000,
            },
            ledger_path=self.ledger,
            environ={},
        )
        self.assertEqual(denied["reason"], "parameters_not_approved")
        self.assertFalse(denied["margin_called"])
        self.assertEqual(
            denied["entry"],
            "execution.margin_cross.strategy_gate.build_approved_position_plan",
        )
