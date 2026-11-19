"""Rejection text, flat quantity, and the merged spot executor contract."""

from __future__ import annotations

import hashlib
from pathlib import Path
import unittest

from execution.executors.binance_spot import BinanceSpotAdapter
from execution.reservation_lifecycle import (
    _proven_exchange_rejection,
    position_is_definite_flat,
)

_ROOT = Path(__file__).resolve().parents[2]
_UNION_SPOT = "ff8124f906841d2d28238af662a6dc292c82dfeb5a410720a282eb97db97fa76"
_PRODUCTION_PACK = "87ef5ed7ed6566bab4f6b7ff0abd4fd33a96811f03cf8f7477a5eb03bede2f00"


def _posted(code: int, msg: str, *, status: int = 400) -> dict[str, object]:
    return {
        "order_post_attempted": True,
        "submitted": False,
        "exchange_order_id": None,
        "arm_position": None,
        "exchange_error": {"status": status, "code": code, "msg": msg},
    }


class RejectionTextTests(unittest.TestCase):
    def test_duplicate_wording_keeps_the_reservation_for_1013_and_2010(self) -> None:
        self.assertFalse(
            _proven_exchange_rejection(_posted(-1013, "Duplicate order sent."))
        )
        self.assertFalse(
            _proven_exchange_rejection(_posted(-2010, "Duplicate order sent."))
        )
        self.assertFalse(
            _proven_exchange_rejection(_posted(-1013, "ClientOrderId is already in use."))
        )
        self.assertFalse(_proven_exchange_rejection(_posted(-1013, "")))
        self.assertFalse(
            _proven_exchange_rejection(_posted(-1007, "Timeout waiting for response"))
        )
        self.assertFalse(
            _proven_exchange_rejection(
                {
                    "order_post_attempted": True,
                    "submitted": False,
                    "exchange_error": {"status": 408, "code": -1007, "msg": "timeout"},
                }
            )
        )

    def test_explicit_filter_and_balance_text_still_prove_no_order(self) -> None:
        self.assertTrue(
            _proven_exchange_rejection(_posted(-1013, "Filter failure: LOT_SIZE"))
        )
        self.assertTrue(
            _proven_exchange_rejection(
                _posted(-2010, "Account has insufficient balance for requested action.")
            )
        )

    def test_string_code_and_a_fill_do_not_prove_rejection(self) -> None:
        self.assertFalse(
            _proven_exchange_rejection(
                {
                    "order_post_attempted": True,
                    "submitted": False,
                    "exchange_error": {
                        "status": 400,
                        "code": "-1013",
                        "msg": "Duplicate order sent.",
                    },
                }
            )
        )
        self.assertFalse(
            _proven_exchange_rejection(
                {
                    "order_post_attempted": True,
                    "submitted": True,
                    "exchange_order_id": "1",
                    "exchange_error": {"status": 400, "code": -1013, "msg": "Filter failure: LOT_SIZE"},
                }
            )
        )

    def test_flat_needs_explicit_zero_quantity_and_residual(self) -> None:
        reservation = {
            "symbol": "BTCUSDT",
            "purpose": "live_bnb_boll_fade",
            "decision_log_id": "9",
        }
        base = {
            "status": "closed",
            "symbol": "BTCUSDT",
            "purpose": "live_bnb_boll_fade",
            "decision_log_id": "9",
        }
        self.assertFalse(
            position_is_definite_flat({**base, "quantity": "0", "residual_qty": None}, reservation)
        )
        self.assertFalse(
            position_is_definite_flat({**base, "quantity": "0.1", "residual_qty": "0"}, reservation)
        )
        self.assertTrue(
            position_is_definite_flat({**base, "quantity": "0", "residual_qty": "0"}, reservation)
        )


class MergedExecutorContractTests(unittest.TestCase):
    def test_union_hash_keeps_reservation_flag_and_oco_freeze(self) -> None:
        path = _ROOT / "execution" / "executors" / "binance_spot.py"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(digest, _UNION_SPOT)
        text = path.read_text(encoding="utf-8")
        body = text[text.index("def execute(") :]
        self.assertIn("self._order_post_attempted = False", body)
        self.assertLess(
            body.index("self._order_post_attempted = False"),
            body.index("self._order_post_attempted = True"),
        )
        self.assertLess(
            body.index("self._order_post_attempted = True"),
            body.index("client.new_order(signed, api_key=key)"),
        )
        adapter = BinanceSpotAdapter.__new__(BinanceSpotAdapter)
        adapter._gates = lambda: {}
        missing_side = adapter._side_conflict(
            {"raw_payload": {"orderReports": [{"origQty": "1"}, {"origQty": "1"}]}},
            require_explicit=True,
        )
        self.assertEqual(missing_side, "oco_readback_side_mismatch")
        saved: dict[str, object] = {}
        adapter._persist_readback_hold(
            {"last_error": "oco_rejected:filter", "quantity": "1.23"},
            saved.update,
            "oco_list_absent:-2018",
        )
        self.assertEqual(saved["last_error"], "oco_rejected:filter")
        self.assertEqual(saved["status"], "oco_unknown")
        pin = (_ROOT / "decision_engine" / "tests" / "test_regime_v2_cutover.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(_UNION_SPOT, pin)
        self.assertIn(_PRODUCTION_PACK, pin)
