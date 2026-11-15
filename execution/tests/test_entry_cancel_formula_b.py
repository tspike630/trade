"""1A cancel readback, first-hang formula B, and dust boundaries.

The new take-profit flag defaults off. These tests do not place a live order.
"""

from __future__ import annotations

import os
import unittest
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs

from execution.executors.binance_spot import (
    BinanceRequestError,
    BinanceSpotAdapter,
    size_exit_quantity,
)
from execution.executors.binance_spot_gate import (
    PURPOSE_LIVE_BNB_BOLL_FADE,
    buy_exposure_block,
)
from execution.executors.binance_spot_protect import (
    dust_archive_fields,
    fill_tp_formula_b_enabled,
    formula_b_take_profit,
)


def _info(symbol: str = "SOLUSDT") -> dict:
    return {
        "symbols": [
            {
                "symbol": symbol,
                "status": "TRADING",
                "quoteAsset": "USDT",
                "isSpotTradingAllowed": True,
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01", "minPrice": "0.01"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "100000"},
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "100000"},
                    {
                        "filterType": "NOTIONAL",
                        "minNotional": "10",
                        "applyMinToMarket": True,
                        "maxNotional": "1000000",
                        "applyMaxToMarket": False,
                    },
                ],
            }
        ]
    }


def _filters() -> list:
    return _info()["symbols"][0]["filters"]


class _Client:
    def __init__(self, gets: list[dict], trades: list[dict], *, get_error: Exception | None = None) -> None:
        self._gets = list(gets)
        self.trades = trades
        self.get_error = get_error
        self.cancels = 0
        self.sells: list[str] = []
        self.ocos: list[str] = []

    def exchange_info(self, symbol: str) -> dict:
        return _info(symbol)

    def get_order(self, signed_query: str, *, api_key: str) -> dict:
        if self.get_error is not None and not self._gets:
            raise self.get_error
        if not self._gets:
            raise TimeoutError("entry get exhausted")
        return self._gets.pop(0)

    def cancel_order(self, signed_query: str, *, api_key: str) -> dict:
        self.cancels += 1
        raise BinanceRequestError(400, code=-2011, msg="Unknown order sent.")

    def my_trades(self, signed_query: str, *, api_key: str) -> list:
        return self.trades

    def new_order(self, signed_query: str, *, api_key: str) -> dict:
        self.sells.append(signed_query)
        raise AssertionError("dust and unknown cancel must not sell")

    def new_oco(self, signed_query: str, *, api_key: str) -> dict:
        self.ocos.append(signed_query)
        query = parse_qs(signed_query)
        return {
            "orderListId": 77,
            "listOrderStatus": "EXECUTING",
            "listStatusType": "EXEC_STARTED",
            "symbol": "SOLUSDT",
            "orders": [
                {"orderId": 501},
                {"orderId": 502},
            ],
            "orderReports": [
                {
                    "orderId": 501,
                    "symbol": "SOLUSDT",
                    "type": "LIMIT_MAKER",
                    "side": "SELL",
                    "status": "NEW",
                    "price": query.get("abovePrice", ["114.63"])[0],
                    "origQty": query.get("quantity", ["10"])[0],
                    "executedQty": "0",
                },
                {
                    "orderId": 502,
                    "symbol": "SOLUSDT",
                    "type": "STOP_LOSS_LIMIT",
                    "side": "SELL",
                    "status": "NEW",
                    "price": query.get("belowPrice", ["97.99"])[0],
                    "stopPrice": query.get("belowStopPrice", ["98"])[0],
                    "origQty": query.get("quantity", ["10"])[0],
                    "executedQty": "0",
                },
            ],
        }


def _trades(qty_fee: list[tuple[str, str, str]]) -> list[dict]:
    rows = []
    for index, (qty, fee, asset) in enumerate(qty_fee, start=1):
        rows.append(
            {
                "id": index,
                "symbol": "SOLUSDT",
                "orderId": 91001,
                "qty": qty,
                "commission": fee,
                "commissionAsset": asset,
                "isBuyer": True,
            }
        )
    return rows


def _position(**extra: object) -> dict:
    row = {
        "id": 1,
        "decision_log_id": 170003,
        "symbol": "SOLUSDT",
        "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
        "entry_order_id": "91001",
        "status": "maker_resting",
        "protection": "maker_resting",
        "quantity": "4",
        "entry_price": "100",
        "entry_limit_price": "100",
        "signal_entry_price": "100",
        "signal_take_profit_price": "110",
        "signal_stop_price": "98",
        "stop_price": "98",
        "take_profit_price": "110",
        "tp_multiplier": "1.35",
        "created_at_ms": 1_700_000_000_000,
    }
    row.update(extra)
    return row


class EntryCancelFormulaBTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = os.environ.copy()
        os.environ["BINANCE_SPOT_LIVE_ENABLED"] = "1"
        os.environ.pop("BINANCE_SPOT_FILL_TP_FORMULA_B", None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def _adapter(self, client: _Client) -> BinanceSpotAdapter:
        return BinanceSpotAdapter(
            client=client,
            kill_switch_path=Path("/tmp/no-such-kill-1a"),
            api_key="k",
            api_secret="s",
            entry_ttl_sec=180,
        )

    def test_formula_b_values_and_default_off(self) -> None:
        self.assertFalse(fill_tp_formula_b_enabled())
        self.assertEqual(
            formula_b_take_profit(
                fill_average="101",
                signal_entry="100",
                signal_take_profit="110",
                tp_multiplier="1.35",
            ),
            "114.635",
        )
        self.assertEqual(
            formula_b_take_profit(
                fill_average="101",
                signal_entry="100",
                signal_take_profit="110",
                tp_multiplier="0.85",
            ),
            "109.585",
        )
        for multiplier in ("1", "1.25", "1.35", "0.85"):
            self.assertIsNotNone(
                formula_b_take_profit(
                    fill_average="101",
                    signal_entry="100",
                    signal_take_profit="110",
                    tp_multiplier=multiplier,
                )
            )
        self.assertIsNone(
            formula_b_take_profit(
                fill_average="101",
                signal_entry="100",
                signal_take_profit="110",
                tp_multiplier="1.15",
            )
        )

    def test_cancel_unknown_then_filled_places_one_net_oco(self) -> None:
        os.environ["BINANCE_SPOT_FILL_TP_FORMULA_B"] = "1"
        partial = {
            "symbol": "SOLUSDT",
            "orderId": 91001,
            "side": "BUY",
            "status": "PARTIALLY_FILLED",
            "executedQty": "4",
            "cummulativeQuoteQty": "404",
        }
        filled = {
            "symbol": "SOLUSDT",
            "orderId": 91001,
            "side": "BUY",
            "status": "FILLED",
            "executedQty": "10",
            "cummulativeQuoteQty": "1010",
        }
        client = _Client([partial, filled], _trades([("4", "0.01", "USDT"), ("6", "0.01", "USDT")]))
        out = self._adapter(client).reconcile_resting_entry(_position(), mark="101")
        self.assertEqual(client.cancels, 1)
        self.assertEqual(len(client.ocos), 1, out)
        self.assertEqual(client.sells, [])
        query = parse_qs(client.ocos[0])
        self.assertEqual(query["quantity"], ["10"])
        self.assertEqual(query["abovePrice"], ["114.63"])
        self.assertEqual(query["belowStopPrice"], ["98"])
        self.assertNotIn("1.15", query["abovePrice"][0])

    def test_base_fee_reduces_oco_qty_quote_and_bnb_do_not(self) -> None:
        os.environ["BINANCE_SPOT_FILL_TP_FORMULA_B"] = "1"
        filled = {
            "symbol": "SOLUSDT",
            "orderId": 91001,
            "side": "BUY",
            "status": "FILLED",
            "executedQty": "10",
            "cummulativeQuoteQty": "1010",
        }
        partial = dict(filled, status="PARTIALLY_FILLED", executedQty="4", cummulativeQuoteQty="404")
        client = _Client(
            [partial, filled],
            _trades([("10", "0.25", "SOL")]),
        )
        # The partial read is not final, so myTrades is read on the FILLED get.
        client.trades = _trades([("4", "0.1", "SOL"), ("6", "0.15", "SOL")])
        out = self._adapter(client).reconcile_resting_entry(_position(tp_multiplier="1"), mark="101")
        self.assertEqual(len(client.ocos), 1, out)
        self.assertEqual(parse_qs(client.ocos[0])["quantity"], ["9.75"])
        self.assertEqual(parse_qs(client.ocos[0])["belowStopPrice"], ["98"])
        bnb = _Client(
            [partial, filled],
            _trades([("4", "0.0001", "BNB"), ("6", "0.0001", "BNB")]),
        )
        out = self._adapter(bnb).reconcile_resting_entry(_position(tp_multiplier="1"), mark="101")
        self.assertEqual(len(bnb.ocos), 1, out)
        self.assertEqual(parse_qs(bnb.ocos[0])["quantity"], ["10"])
        self.assertEqual(parse_qs(bnb.ocos[0])["belowStopPrice"], ["98"])

    def test_cancel_unknown_still_open_or_timeout_does_not_sell_or_place(self) -> None:
        partial = {
            "symbol": "SOLUSDT",
            "orderId": 91001,
            "side": "BUY",
            "status": "PARTIALLY_FILLED",
            "executedQty": "4",
            "cummulativeQuoteQty": "404",
        }
        still = dict(partial)
        client = _Client([partial, still], [])
        out = self._adapter(client).reconcile_resting_entry(_position(), mark="101")
        self.assertEqual(out["book"], "maker_cancel_unknown")
        self.assertEqual(client.cancels, 1)
        self.assertEqual(client.ocos, [])
        self.assertEqual(client.sells, [])

        client = _Client([partial], [], get_error=TimeoutError("slow"))
        out = self._adapter(client).reconcile_resting_entry(_position(), mark="101")
        self.assertEqual(out["error"], "entry_requery_timeout")
        self.assertEqual(client.ocos, [])
        self.assertEqual(client.sells, [])

        wrong = dict(partial, symbol="BTCUSDT", status="FILLED", executedQty="10")
        client = _Client([partial, wrong], _trades([("10", "0", "USDT")]))
        out = self._adapter(client).reconcile_resting_entry(_position(), mark="101")
        self.assertEqual(out["error"], "entry_requery_symbol_mismatch")
        self.assertEqual(client.ocos, [])

    def test_formula_off_keeps_tp_and_sl_and_applied_flag_is_once(self) -> None:
        row = _position(entry_price="101")
        saved: list[dict] = []

        def save(fields: dict) -> None:
            row.update(fields)
            saved.append(dict(fields))

        adapter = self._adapter(_Client([], []))
        adapter._maybe_apply_fill_tp_formula_b(row, save, mark="101", filters=_filters())
        self.assertEqual(saved, [])
        self.assertEqual(row["take_profit_price"], "110")
        self.assertEqual(row["stop_price"], "98")
        os.environ["BINANCE_SPOT_FILL_TP_FORMULA_B"] = "1"
        adapter._maybe_apply_fill_tp_formula_b(row, save, mark="101", filters=_filters())
        self.assertEqual(row["take_profit_price"], "114.635")
        self.assertEqual(row["stop_price"], "98")
        self.assertEqual(row["fill_tp_formula_b"], "applied")
        row["entry_price"] = "114.635"
        adapter._maybe_apply_fill_tp_formula_b(row, save, mark="101", filters=_filters())
        self.assertEqual(row["take_profit_price"], "114.635")
        self.assertEqual(row["stop_price"], "98")

    def test_existing_oco_id_is_not_rewritten(self) -> None:
        os.environ["BINANCE_SPOT_FILL_TP_FORMULA_B"] = "1"
        row = _position(oco_order_list_id="77", status="oco_working", fill_tp_formula_b="")
        adapter = self._adapter(_Client([], []))
        adapter._maybe_apply_fill_tp_formula_b(
            row, lambda fields: row.update(fields), mark="101", filters=_filters()
        )
        self.assertEqual(row["take_profit_price"], "110")
        self.assertEqual(row["oco_order_list_id"], "77")
        self.assertEqual(row["stop_price"], "98")

    def test_closed_dust_blocks_next_symbol_and_does_not_sell(self) -> None:
        small, error = size_exit_quantity(_info(), "SOLUSDT", "0.01", "100")
        self.assertIsNone(small)
        self.assertEqual(error, "exit_below_min_notional")
        fields = dust_archive_fields(quantity="0.01", mark="100", stop="98", detail=error)
        self.assertEqual(fields["status"], "closed_dust")
        self.assertNotEqual(fields["status"], "closed")
        dust = {
            "symbol": "SOLUSDT",
            "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
            "status": "closed_dust",
            "quantity": "0.01",
            "unprotected_qty": "0.01",
            "manual_action": "residual_below_min_is_not_a_complete_exit",
            "entry_order_id": "9",
            "residual_qty": "0.01",
        }
        blocked = buy_exposure_block(
            [dust],
            [],
            purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
            symbol="BTCUSDT",
        )
        self.assertEqual(blocked, "unprotected_position_blocks_buy")
        self.assertEqual(Decimal(dust["quantity"]), Decimal("0.01"))


if __name__ == "__main__":
    unittest.main()
