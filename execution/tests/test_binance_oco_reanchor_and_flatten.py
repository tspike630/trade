"""Tests for P0 OCO fill-price re-anchoring, HTTP reject code/msg, and naked auto-flatten.

    python -m unittest execution.tests.test_binance_oco_reanchor_and_flatten
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "crawler"))

from execution.executors.binance_spot import (  # noqa: E402
    PURPOSE_LIVE_BNB_BOLL_FADE,
    BinanceRequestError,
    BinanceSpotAdapter,
)
from execution.executors.binance_spot_gate import (  # noqa: E402
    REST_BOOK,
    buy_exposure_block,
)
from execution.executors.binance_spot_protect import (  # noqa: E402
    PROTECTION_AUTO_FLATTEN,
    flatten_client_order_id,
    reanchor_oco_prices_to_fill,
)
from execution.ledger import ExecutionLedger  # noqa: E402

_DASH_FILTERS = [
    {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "900000", "stepSize": "0.001"},
    {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "maxQty": "900000", "stepSize": "0.001"},
    {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "100000", "tickSize": "0.01"},
    {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": True},
]
_DASH_INFO = {
    "symbol": "DASHUSDT",
    "status": "TRADING",
    "quoteAsset": "USDT",
    "isSpotTradingAllowed": True,
    "filters": _DASH_FILTERS,
}
_BOUND = {
    "accountType": "SPOT",
    "canTrade": True,
    "permissions": ["SPOT"],
    "restrictions": {
        "enableSpotAndMarginTrading": True,
        "enableReading": True,
        "ipRestrict": True,
    },
    "balances": [{"asset": "USDT", "free": "1000.00", "locked": "0"}],
}
_EXPECTED = {"account_type": "SPOT", "usdt_free": "1000"}


def _working_oco() -> dict[str, Any]:
    return {
        "orderListId": 88,
        "listOrderStatus": "EXECUTING",
        "listStatusType": "EXEC_STARTED",
        "orderReports": [
            {"orderId": 201, "type": "LIMIT_MAKER", "status": "NEW", "executedQty": "0"},
            {"orderId": 202, "type": "STOP_LOSS_LIMIT", "status": "NEW", "executedQty": "0"},
        ],
    }


class _SlippageAndFlattenClient:
    def __init__(
        self,
        *,
        mark: str = "48.10",
        oco_exc: Exception | None = None,
        sell_exc: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.mark = mark
        self.oco_exc = oco_exc
        self.sell_exc = sell_exc

    def exchange_info(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("exchange_info", symbol))
        return _DASH_INFO

    def ticker_price(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("ticker_price", symbol))
        return {"symbol": symbol, "price": self.mark}

    def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("new_order", signed_query, api_key))
        if "side=SELL" in signed_query:
            if self.sell_exc is not None:
                raise self.sell_exc
            return {"orderId": "7701", "status": "FILLED", "executedQty": "1.082"}
        # Buy fills at 48.02 average (slipped below original stop 48.02605)
        return {
            "orderId": "2953042815",
            "status": "FILLED",
            "executedQty": "1.082",
            "cummulativeQuoteQty": "51.957640",
            "symbol": "DASHUSDT",
        }

    def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("new_oco", signed_query, api_key))
        if self.oco_exc is not None:
            raise self.oco_exc
        return _working_oco()

    def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("get_order_list", signed_query, api_key))
        raise BinanceRequestError(400, code=-2018, msg="Order list does not exist.")


def _dash_buy(decision_id: int = 148592) -> dict[str, Any]:
    return {
        "decision_log_id": decision_id,
        "strategy_id": "tech_boll_fade_v1",
        "action": "buy",
        "chain_id": "binance",
        "token_address": "DASHUSDT",
        "pair_address": "DASH/USDT",
        "size_pct": 0.05,
        "mode": "live_stub",
        "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
        "extras": {
            "binance_symbol": "DASHUSDT",
            "order_type": "MARKET",
            "quote_order_qty": "51.95",
            "price_usd": "48.10",
            "stop_price": "48.02605",
            "take_profit_price": "48.2895",
            "entry_reason": "large_trade_market",
        },
    }


class OcoReanchorAndAutoFlattenTests(unittest.TestCase):
    def setUp(self) -> None:
        self._net = mock.patch(
            "execution.executors.binance_spot.urllib.request.urlopen",
            side_effect=AssertionError("live binance HTTP is disabled"),
        )
        self._net.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._net.stop()
        self._tmp.cleanup()

    def _env(self, **overrides: str) -> dict[str, str]:
        base = {
            "BINANCE_SPOT_LIVE_ENABLED": "1",
            "LIVE_KILL_SWITCH_PATH": str(self.root / "absent-kill"),
            "BINANCE_API_KEY": "",
            "BINANCE_API_SECRET": "",
            "BINANCE_SPOT_REANCHOR_OCO_ON_FILL": "0",
            "BINANCE_SPOT_AUTO_FLATTEN_ON_OCO_FAIL": "0",
        }
        base.update(overrides)
        return base

    def _adapter(self, client: Any, **kwargs: Any) -> BinanceSpotAdapter:
        return BinanceSpotAdapter(
            client=client,
            kill_switch_path=self.root / "absent-kill",
            api_key="test-key",
            api_secret="test-secret",
            clock_ms=lambda: 1_700_000_000_000,
            account_snapshot=_BOUND,
            production_book=REST_BOOK,
            rest_expected=_EXPECTED,
            **kwargs,
        )

    def test_reanchor_oco_on_fill_rescues_slipped_market_buy(self) -> None:
        reanchored, err = reanchor_oco_prices_to_fill(
            signal_entry_price="48.10",
            stop_price="48.02605",
            take_profit_price="48.2895",
            fill_price="48.02",
            mark="48.10",
            filters=_DASH_FILTERS,
        )
        self.assertIsNone(err)
        assert reanchored is not None
        # 48.02 * (1 - 0.005) = 47.7799 -> floored to 47.77
        self.assertEqual(reanchored["stop_price"], "47.77")
        self.assertEqual(reanchored["take_profit_price"], "48.28")

        client = _SlippageAndFlattenClient(mark="48.10")
        adapter = self._adapter(client)
        with mock.patch.dict(
            os.environ,
            self._env(BINANCE_SPOT_REANCHOR_OCO_ON_FILL="1"),
        ):
            res = self._adapter(client, auto_flatten_on_oco_fail=True).execute(_dash_buy())
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["executed"], 1)
        self.assertTrue(res["consumer_allowed"])
        arm = res["arm_position"]
        self.assertEqual(arm["status"], "oco_working")
        self.assertTrue(arm["oco_reanchored"])
        self.assertEqual(arm["signal_stop_price"], "48.02605")
        self.assertEqual(arm["stop_price"], "47.77")
        oco_call = next(c for c in client.calls if c[0] == "new_oco")
        oco_form = parse_qs(oco_call[1])
        self.assertEqual(oco_form["belowStopPrice"], ["47.77"])
        self.assertEqual(oco_form["belowPrice"], ["47.76"])

    def test_oco_http_rejection_keeps_code_and_msg_and_auto_flattens_when_enabled(self) -> None:
        oco_err = BinanceRequestError(
            400,
            code=-1013,
            msg="Filter failure: PRICE_FILTER",
        )
        client = _SlippageAndFlattenClient(
            mark="48.10",
            oco_exc=oco_err,
        )
        adapter = self._adapter(client)
        sig = _dash_buy(148600)
        sig["extras"]["stop_price"] = "47.50"  # valid band, but exchange rejects new_oco
        with mock.patch.dict(
            os.environ,
            self._env(BINANCE_SPOT_AUTO_FLATTEN_ON_OCO_FAIL="1"),
        ):
            res = adapter.execute(sig)

        self.assertFalse(res["ok"])
        self.assertEqual(res["executed"], 0)
        self.assertEqual(
            res["exchange_error"],
            {"status": 400, "code": -1013, "msg": "Filter failure: PRICE_FILTER"},
        )
        self.assertIn("oco_failed_auto_flattened:", str(res["error"]))
        self.assertIn("-1013:Filter failure: PRICE_FILTER", str(res["error"]))
        arm = res["arm_position"]
        self.assertEqual(arm["status"], "closed")
        self.assertEqual(arm["protection"], PROTECTION_AUTO_FLATTEN)
        self.assertEqual(arm["exit_reason"], "oco_failed_auto_flatten")
        self.assertEqual(arm["exit_order_id"], "7701")
        self.assertEqual(arm["exit_client_order_id"], flatten_client_order_id(148600))

        # Verify a second MARKET SELL order was sent with bnbfade-fl-148600
        order_calls = [c for c in client.calls if c[0] == "new_order"]
        self.assertEqual(len(order_calls), 2)
        sell_form = parse_qs(order_calls[1][1])
        self.assertEqual(sell_form["side"], ["SELL"])
        self.assertEqual(sell_form["type"], ["MARKET"])
        self.assertEqual(sell_form["quantity"], ["1.082"])
        self.assertEqual(sell_form["newClientOrderId"], ["bnbfade-fl-148600"])

        # Because the naked position was flattened and closed, it no longer blocks the arm!
        self.assertIsNone(
            buy_exposure_block([arm], [], purpose=PURPOSE_LIVE_BNB_BOLL_FADE, symbol="BNBUSDT")
        )

    def test_auto_flatten_in_maintain_held_position_closes_ledger_row(self) -> None:
        client = _SlippageAndFlattenClient(
            mark="47.79",
            oco_exc=BinanceRequestError(400, code=-2010, msg="Account has insufficient balance"),
        )
        adapter = self._adapter(client)
        ledger = ExecutionLedger(self.root / "exec_ledger.db")
        try:
            armed = ledger.arm_binance_spot_position(
                {
                    "decision_log_id": 148601,
                    "symbol": "DASHUSDT",
                    "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                    "status": "oco_failed",
                    "quantity": "1.082",
                    "stop_price": "48.02605",
                    "take_profit_price": "48.2895",
                    "entry_price": "48.02",
                    "entry_order_id": "2953042815",
                    "protection": "oco_pending",
                }
            )
            pos_id = int(armed["id"])
            with mock.patch.dict(
                os.environ,
                self._env(BINANCE_SPOT_AUTO_FLATTEN_ON_OCO_FAIL="1"),
            ):
                outcome = adapter.maintain_held_position(
                    armed,
                    "47.79",
                    persist=lambda fields: ledger.update_binance_spot_position(pos_id, fields),
                )
            self.assertEqual(outcome["book"], "closed")
            self.assertTrue(outcome["submitted"])
            self.assertEqual(outcome["reason"], "oco_failed_auto_flatten")
            updated = ledger.binance_spot_position_by_id(pos_id)
            assert updated is not None
            self.assertEqual(updated["status"], "closed")
            self.assertEqual(updated["protection"], PROTECTION_AUTO_FLATTEN)
            self.assertEqual(updated["exit_order_id"], "7701")
            self.assertEqual(ledger.list_binance_spot_buy_blocks(PURPOSE_LIVE_BNB_BOLL_FADE), [])
        finally:
            ledger.close()

    def test_failed_flatten_stays_oco_failed_and_blocks_new_buys(self) -> None:
        client = _SlippageAndFlattenClient(
            mark="48.10",
            sell_exc=BinanceRequestError(500, code=-1001, msg="Internal error"),
        )
        adapter = self._adapter(client)
        with mock.patch.dict(
            os.environ,
            self._env(BINANCE_SPOT_AUTO_FLATTEN_ON_OCO_FAIL="1"),
        ):
            res = adapter.execute(_dash_buy(148602))
        self.assertEqual(res["arm_position"]["status"], "oco_failed")
        self.assertIn("flatten_failed:binance_http_error:500:-1001:Internal error", str(res["error"]))
        self.assertEqual(
            buy_exposure_block(
                [res["arm_position"]],
                [],
                purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
                symbol="BNBUSDT",
            ),
            "oco_failed_blocks_buy",
        )


if __name__ == "__main__":
    unittest.main()
