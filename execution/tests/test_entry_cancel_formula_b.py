"""1A cancel readback, first-hang formula B, restart/lag/mismatch, and dust boundaries.

The new take-profit flag defaults off. These tests do not place a live order.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from execution.executors.binance_spot import (
    BinanceRequestError,
    BinanceSpotAdapter,
    size_exit_quantity,
)
from execution.executors.binance_spot_gate import (
    PURPOSE_LIVE_BNB_BOLL_FADE,
    REST_BOOK,
    buy_exposure_block,
    decision_unresolved_halt,
)
from execution.executors.binance_spot_protect import (
    dust_archive_fields,
    fill_tp_formula_b_enabled,
    formula_b_take_profit,
)
from execution.ledger import ExecutionLedger, initialize_spot_ledger_binding


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


def _snapshot(uid: int = 424242) -> dict[str, Any]:
    return {
        "uid": uid,
        "accountType": "SPOT",
        "canTrade": True,
        "permissions": ["SPOT"],
        "restrictions": {
            "enableSpotAndMarginTrading": True,
            "enableReading": True,
            "ipRestrict": True,
        },
        "balances": [{"asset": "USDT", "free": "2000.0"}],
    }


class _Client:
    def __init__(
        self,
        gets: list[dict],
        trades: list[dict],
        *,
        get_error: Exception | None = None,
        cancel_ok: bool = False,
        new_order_resp: dict[str, Any] | None = None,
        ticker_px: str = "101",
    ) -> None:
        self._gets = list(gets)
        self.trades = trades
        self.get_error = get_error
        self.cancel_ok = cancel_ok
        self.new_order_resp = new_order_resp
        self.ticker_px = ticker_px
        self.cancels = 0
        self.sells: list[str] = []
        self.buys: list[str] = []
        self.ocos: list[str] = []
        self.last_oco_resp: dict[str, Any] | None = None

    def exchange_info(self, symbol: str) -> dict:
        return _info(symbol)

    def ticker_price(self, symbol: str) -> dict:
        return {"symbol": symbol, "price": self.ticker_px}

    def order_book(self, symbol: str, *, limit: int = 20) -> dict:
        return {
            "bids": [["99.95", "100.0"]],
            "asks": [["100.10", "100.0"]],
        }

    def get_order(self, signed_query: str, *, api_key: str) -> dict:
        form = parse_qs(signed_query)
        oid = (form.get("orderId") or [""])[0]
        if self.last_oco_resp is not None and oid in {"501", "502"}:
            for rep in self.last_oco_resp.get("orderReports", []):
                if str(rep.get("orderId")) == oid:
                    return dict(rep)
        if self.get_error is not None and not self._gets:
            raise self.get_error
        if not self._gets:
            raise TimeoutError("entry get exhausted")
        return self._gets.pop(0)

    def cancel_order(self, signed_query: str, *, api_key: str) -> dict:
        self.cancels += 1
        if self.cancel_ok:
            return {"symbol": "SOLUSDT", "orderId": 91001, "status": "CANCELED"}
        raise BinanceRequestError(400, code=-2011, msg="Unknown order sent.")

    def my_trades(self, signed_query: str, *, api_key: str) -> list:
        form = parse_qs(signed_query)
        if form.get("fromId"):
            return []
        oid = (form.get("orderId") or ["91001"])[0]
        if oid in {"501", "502"}:
            return []
        return self.trades

    def new_order(self, signed_query: str, *, api_key: str) -> dict:
        form = parse_qs(signed_query)
        if (form.get("side") or [""])[0] == "BUY" and self.new_order_resp is not None:
            self.buys.append(signed_query)
            return dict(self.new_order_resp)
        self.sells.append(signed_query)
        raise AssertionError("dust, band fallback, and unknown cancel must not sell")

    def new_oco(self, signed_query: str, *, api_key: str) -> dict:
        self.ocos.append(signed_query)
        query = parse_qs(signed_query)
        qty = query.get("quantity", ["10"])[0]
        above_px = query.get("abovePrice", ["114.63"])[0]
        below_px = query.get("belowPrice", ["97.99"])[0]
        below_stop = query.get("belowStopPrice", ["98"])[0]
        list_cid = query.get("listClientOrderId", ["bnbfade-ol-170003"])[0]
        above_cid = query.get("aboveClientOrderId", ["bnbfade-tp-170003"])[0]
        below_cid = query.get("belowClientOrderId", ["bnbfade-sl-170003"])[0]
        resp = {
            "orderListId": 77,
            "listClientOrderId": list_cid,
            "listOrderStatus": "EXECUTING",
            "listStatusType": "EXEC_STARTED",
            "symbol": "SOLUSDT",
            "orders": [
                {"symbol": "SOLUSDT", "orderId": 501, "clientOrderId": above_cid},
                {"symbol": "SOLUSDT", "orderId": 502, "clientOrderId": below_cid},
            ],
            "orderReports": [
                {
                    "orderId": 501,
                    "clientOrderId": above_cid,
                    "symbol": "SOLUSDT",
                    "type": "LIMIT_MAKER",
                    "side": "SELL",
                    "status": "NEW",
                    "price": above_px,
                    "origQty": qty,
                    "executedQty": "0",
                },
                {
                    "orderId": 502,
                    "clientOrderId": below_cid,
                    "symbol": "SOLUSDT",
                    "type": "STOP_LOSS_LIMIT",
                    "side": "SELL",
                    "status": "NEW",
                    "price": below_px,
                    "stopPrice": below_stop,
                    "origQty": qty,
                    "executedQty": "0",
                },
            ],
        }
        self.last_oco_resp = resp
        return resp

    def get_order_list(self, signed_query: str, *, api_key: str) -> dict:
        if self.last_oco_resp is None:
            raise BinanceRequestError(400, code=-2018, msg="Order list does not exist.")
        return dict(self.last_oco_resp)


def _trades(qty_fee: list[tuple[str, str, str]], *, order_id: int = 91001) -> list[dict]:
    rows = []
    for index, (qty, fee, asset) in enumerate(qty_fee, start=1):
        rows.append(
            {
                "id": index,
                "symbol": "SOLUSDT",
                "orderId": order_id,
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

    def _adapter(self, client: _Client, **kwargs: Any) -> BinanceSpotAdapter:
        return BinanceSpotAdapter(
            client=client,
            kill_switch_path=Path("/tmp/no-such-kill-1a"),
            api_key="k",
            api_secret="s",
            entry_ttl_sec=180,
            **kwargs,
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

    def test_sqlite_restart_recovery_persists_regime_tp_multiplier_and_fill_avg(self) -> None:
        os.environ["BINANCE_SPOT_FILL_TP_FORMULA_B"] = "1"
        with tempfile.TemporaryDirectory(prefix="formula-b-restart-") as tmp:
            db_path = Path(tmp) / "ledger.db"
            arm_client = _Client(
                [],
                [],
                new_order_resp={
                    "symbol": "SOLUSDT",
                    "orderId": 91001,
                    "side": "BUY",
                    "status": "NEW",
                    "executedQty": "0",
                    "cummulativeQuoteQty": "0",
                    "price": "100",
                },
                ticker_px="100",
            )
            arm_adapter = self._adapter(
                arm_client,
                account_snapshot=_snapshot(),
                production_book=REST_BOOK,
                rest_expected={"account_type": "SPOT", "usdt_free": "2000"},
                auto_flatten_on_oco_fail=True,
                clock_ms=lambda: 1_700_000_000_000,
            )
            sig = {
                "decision_log_id": 170010,
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                "mode": "live_stub",
                "action": "buy",
                "token_address": "SOLUSDT",
                "extras": {
                    "binance_symbol": "SOLUSDT",
                    "order_type": "LIMIT_MAKER",
                    "limit_price": "100",
                    "price_usd": "100",
                    "quantity": "10",
                    "stop_price": "98",
                    "take_profit_price": "110",
                    "regime_tp_multiplier": 1.25,
                },
            }
            res = arm_adapter.execute(sig)
            self.assertEqual(res["error"], "maker_entry_resting")
            ledger1 = ExecutionLedger(db_path)
            armed = ledger1.arm_binance_spot_position(res["arm_position"])
            pid = int(armed["id"])
            ledger1.close()

            # Simulate process restart: reopen SQLite ledger & create a brand-new adapter.
            ledger2 = ExecutionLedger(db_path)
            persisted = ledger2.binance_spot_position_by_id(pid)
            assert persisted is not None
            self.assertEqual(str(persisted["tp_multiplier"]), "1.25")
            self.assertEqual(str(persisted["signal_entry_price"]), "100")
            self.assertEqual(str(persisted["signal_take_profit_price"]), "110")

            filled = {
                "symbol": "SOLUSDT",
                "orderId": 91001,
                "side": "BUY",
                "status": "FILLED",
                "executedQty": "10",
                "cummulativeQuoteQty": "990",
            }
            restart_client = _Client([filled], _trades([("10", "0.01", "USDT")]), ticker_px="100")
            restart_adapter = self._adapter(
                restart_client,
                account_snapshot=_snapshot(),
                production_book=REST_BOOK,
                rest_expected={"account_type": "SPOT", "usdt_free": "2000"},
                auto_flatten_on_oco_fail=True,
                clock_ms=lambda: 1_700_000_010_000,
            )
            out = restart_adapter.maintain_held_position(
                persisted,
                "100",
                persist=lambda fields: ledger2.update_binance_spot_position(pid, fields),
            )
            self.assertTrue(out["ok"], out)
            self.assertEqual(len(restart_client.ocos), 1)
            q = parse_qs(restart_client.ocos[0])
            # F=99, E=100, P=110, m=1.25 -> 99 * (1 + 0.10 * 1.25) = 111.375 -> tick 111.37
            self.assertEqual(q["abovePrice"], ["111.37"])
            self.assertEqual(q["belowStopPrice"], ["98"])
            final_row = ledger2.binance_spot_position_by_id(pid)
            assert final_row is not None
            self.assertEqual(final_row["entry_price"], "99")
            self.assertEqual(final_row["fill_tp_formula_b"], "applied")
            self.assertEqual(final_row["take_profit_price"], "111.375")
            self.assertEqual(final_row["stop_price"], "98")
            ledger2.close()

    def test_mytrades_lag_halts_then_next_tick_recovers_and_clears_unprotected_incident(self) -> None:
        os.environ["BINANCE_SPOT_FILL_TP_FORMULA_B"] = "1"
        with tempfile.TemporaryDirectory(prefix="formula-b-lag-") as tmp:
            ledger = ExecutionLedger(Path(tmp) / "ledger.db")
            initialize_spot_ledger_binding(ledger.path, "424242")
            armed = ledger.arm_binance_spot_position(
                {
                    "decision_log_id": 170003,
                    "symbol": "SOLUSDT",
                    "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                    "entry_order_id": "91001",
                    "status": "maker_resting",
                    "protection": "maker_resting",
                    "quantity": None,
                    "entry_order_qty": "10",
                    "entry_limit_price": "100",
                    "entry_price": "100",
                    "signal_entry_price": "100",
                    "signal_take_profit_price": "110",
                    "signal_stop_price": "98",
                    "stop_price": "98",
                    "take_profit_price": "110",
                    "tp_multiplier": "1.35",
                    "created_at_ms": "1700000000000",
                }
            )
            pid = int(armed["id"])
            partial = {
                "symbol": "SOLUSDT",
                "orderId": 91001,
                "side": "BUY",
                "status": "PARTIALLY_FILLED",
                "executedQty": "4",
                "cummulativeQuoteQty": "404",
            }
            canceled_partial = {
                "symbol": "SOLUSDT",
                "orderId": 91001,
                "side": "BUY",
                "status": "CANCELED",
                "executedQty": "4",
                "cummulativeQuoteQty": "404",
            }
            # Tick 1: cancel completes/requeries CANCELED with executedQty=4, but myTrades lags ([]).
            lag_client = _Client([partial, canceled_partial], [], cancel_ok=True)
            adapter1 = self._adapter(
                lag_client,
                account_snapshot=_snapshot(),
                production_book=REST_BOOK,
                rest_expected={"account_type": "SPOT", "usdt_free": "2000"},
                clock_ms=lambda: 1_700_000_005_000,
            )
            tick1 = adapter1.maintain_held_position(
                ledger.binance_spot_position_by_id(pid),  # type: ignore[arg-type]
                "101",
                persist=lambda fields: ledger.update_binance_spot_position(pid, fields),
            )
            self.assertEqual(tick1["error"], "fee_unverified")
            self.assertEqual(lag_client.ocos, [])
            self.assertEqual(lag_client.sells, [])
            after_lag = ledger.binance_spot_position_by_id(pid)
            assert after_lag is not None
            self.assertEqual(after_lag["status"], "maker_cancel_unknown")
            self.assertEqual(after_lag["unprotected_qty"], "4")
            self.assertEqual(
                buy_exposure_block(
                    ledger.list_binance_spot_buy_blocks(PURPOSE_LIVE_BNB_BOLL_FADE),
                    [],
                    purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
                    symbol="BTCUSDT",
                ),
                "maker_resting_blocks_buy",
            )

            # Tick 2: myTrades catches up (4 SOL filled, 0.04 SOL fee -> 3.96 net).
            caught_up_client = _Client(
                [canceled_partial, canceled_partial, canceled_partial],
                _trades([("4", "0.04", "SOL")]),
            )
            adapter2 = self._adapter(
                caught_up_client,
                account_snapshot=_snapshot(),
                production_book=REST_BOOK,
                rest_expected={"account_type": "SPOT", "usdt_free": "2000"},
                clock_ms=lambda: 1_700_000_010_000,
            )
            adapter2.record_entry_origin = ledger.record_exchange_entry_origin  # type: ignore[attr-defined]
            tick2 = adapter2.maintain_held_position(
                after_lag,
                "101",
                persist=lambda fields: ledger.update_binance_spot_position(pid, fields),
            )
            self.assertTrue(tick2["ok"], tick2)
            self.assertEqual(len(caught_up_client.ocos), 1)
            q = parse_qs(caught_up_client.ocos[0])
            self.assertEqual(q["quantity"], ["3.96"])
            self.assertEqual(q["abovePrice"], ["114.63"])
            self.assertEqual(q["belowStopPrice"], ["98"])

            recovered = ledger.binance_spot_position_by_id(pid)
            assert recovered is not None
            self.assertEqual(recovered["status"], "oco_working")
            self.assertIsNone(recovered["unprotected_qty"])
            self.assertIsNone(recovered["manual_action"])
            self.assertIsNone(recovered["last_error"])
            # POST /orderList/oco ack alone does not forge readback; the next
            # maintenance GET /api/v3/orderList persists verified readback.
            self.assertIsNone(recovered["oco_readback_status"])
            tick3 = adapter2.maintain_held_position(
                recovered,
                "101",
                persist=lambda fields: ledger.update_binance_spot_position(pid, fields),
            )
            self.assertTrue(tick3["ok"], tick3)
            self.assertEqual(len(caught_up_client.ocos), 1)
            verified_row = ledger.binance_spot_position_by_id(pid)
            assert verified_row is not None
            self.assertEqual(verified_row["oco_readback_status"], "verified")
            self.assertEqual(verified_row["oco_readback_covered_qty"], "3.96")

            blocks = ledger.list_binance_spot_buy_blocks(PURPOSE_LIVE_BNB_BOLL_FADE)
            self.assertIsNone(
                decision_unresolved_halt(
                    blocks,
                    expected_account_uid="424242",
                    now_ms=1_700_000_020_000,
                )
            )
            self.assertIsNone(
                buy_exposure_block(blocks, [], purpose=PURPOSE_LIVE_BNB_BOLL_FADE, symbol="BTCUSDT")
            )
            self.assertEqual(
                buy_exposure_block(blocks, [], purpose=PURPOSE_LIVE_BNB_BOLL_FADE, symbol="SOLUSDT"),
                "position_open_blocks_buy",
            )
            ledger.close()

    def test_http200_cancel_and_initial_get_reject_wrong_order_side_uid_absent_and_pending(self) -> None:
        partial = {
            "symbol": "SOLUSDT",
            "orderId": 91001,
            "side": "BUY",
            "status": "PARTIALLY_FILLED",
            "executedQty": "4",
            "cummulativeQuoteQty": "404",
        }
        bad_cases = [
            (dict(partial, orderId=99999, status="CANCELED"), "entry_requery_order_mismatch"),
            (dict(partial, symbol="BTCUSDT", status="CANCELED"), "entry_requery_symbol_mismatch"),
            (dict(partial, side="SELL", status="CANCELED"), "entry_requery_side_mismatch"),
            (dict(partial, uid=999999, status="CANCELED"), "entry_requery_account_mismatch"),
            (dict(partial, status=""), "entry_requery_no_status"),
            (dict(partial, status="PENDING_CANCEL", executedQty="0"), "maker_cancel_unknown"),
        ]
        for cancel_ok in (False, True):
            for bad_payload, expected_err in bad_cases:
                client = _Client(
                    [partial, bad_payload],
                    _trades([("4", "0", "USDT")]),
                    cancel_ok=cancel_ok,
                )
                adapter = self._adapter(client, account_snapshot=_snapshot(uid=424242))
                out = adapter.reconcile_resting_entry(_position(), mark="101")
                self.assertEqual(out["book"], "maker_cancel_unknown")
                self.assertEqual(out["error"], expected_err)
                self.assertEqual(client.ocos, [])
                self.assertEqual(client.sells, [])

            absent_client = _Client(
                [partial],
                [],
                get_error=BinanceRequestError(400, code=-2013, msg="Order does not exist."),
                cancel_ok=cancel_ok,
            )
            absent_out = self._adapter(absent_client).reconcile_resting_entry(_position(), mark="101")
            self.assertEqual(absent_out["book"], "maker_cancel_unknown")
            self.assertEqual(absent_out["error"], "entry_requery_absent")
            self.assertEqual(absent_client.ocos, [])

        # Initial GET mismatch in reconcile_resting_entry must not cancel or adopt.
        init_wrong = _Client([dict(partial, side="SELL")], [])
        init_out = self._adapter(init_wrong).reconcile_resting_entry(_position(), mark="101")
        self.assertEqual(init_out["book"], "unchanged")
        self.assertEqual(init_out["error"], "entry_requery_side_mismatch")
        self.assertEqual(init_wrong.cancels, 0)
        self.assertEqual(init_wrong.ocos, [])

    def test_immediate_market_and_instant_limit_maker_apply_formula_b_once(self) -> None:
        os.environ["BINANCE_SPOT_FILL_TP_FORMULA_B"] = "1"
        filled_ack = {
            "symbol": "SOLUSDT",
            "orderId": 91001,
            "side": "BUY",
            "status": "FILLED",
            "executedQty": "10",
            "cummulativeQuoteQty": "1010",
            "fills": [{"tradeId": 1, "qty": "10", "commission": "0.01", "commissionAsset": "USDT", "isBuyer": True}],
        }
        client = _Client(
            [filled_ack],
            _trades([("10", "0.01", "USDT")]),
            new_order_resp=filled_ack,
            ticker_px="101",
        )
        adapter = self._adapter(
            client,
            account_snapshot=_snapshot(),
            production_book=REST_BOOK,
            rest_expected={"account_type": "SPOT", "usdt_free": "2000"},
            auto_flatten_on_oco_fail=True,
        )
        sig = {
            "decision_log_id": 170003,
            "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
            "mode": "live_stub",
            "action": "buy",
            "token_address": "SOLUSDT",
            "extras": {
                "binance_symbol": "SOLUSDT",
                "order_type": "MARKET",
                "quote_order_qty": "1010",
                "price_usd": "100",
                "stop_price": "98",
                "take_profit_price": "110",
                "regime_tp_multiplier": 1.35,
            },
        }
        res = adapter.execute(sig)
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(client.ocos), 1)
        q = parse_qs(client.ocos[0])
        self.assertEqual(q["abovePrice"], ["114.63"])
        self.assertEqual(q["belowStopPrice"], ["98"])
        self.assertEqual(res["arm_position"]["fill_tp_formula_b"], "applied")
        self.assertEqual(res["arm_position"]["stop_price"], "98")

    def test_formula_b_widened_tp_prevents_false_auto_flatten_when_mark_crossed_signal_tp(self) -> None:
        os.environ["BINANCE_SPOT_FILL_TP_FORMULA_B"] = "1"
        filled = {
            "symbol": "SOLUSDT",
            "orderId": 91001,
            "side": "BUY",
            "status": "FILLED",
            "executedQty": "10",
            "cummulativeQuoteQty": "1000",
        }
        client = _Client([filled], _trades([("10", "0", "USDT")]), ticker_px="102.05")
        adapter = self._adapter(client, auto_flatten_on_oco_fail=True, reanchor_oco_on_fill=False)
        row = _position(
            signal_entry_price="100",
            signal_take_profit_price="102",
            take_profit_price="102",
            stop_price="98",
            tp_multiplier="1.35",
        )
        # Signal TP=102 < mark=102.05, but Formula B TP = 100*(1+0.02*1.35) = 102.70 > 102.05.
        out = adapter.reconcile_resting_entry(row, mark="102.05")
        self.assertTrue(out["ok"], out)
        self.assertEqual(client.sells, [])
        self.assertEqual(len(client.ocos), 1)
        q = parse_qs(client.ocos[0])
        self.assertEqual(q["abovePrice"], ["102.7"])
        self.assertEqual(q["belowStopPrice"], ["98"])

    def test_formula_b_band_rejection_falls_back_to_original_tp_without_moving_stop(self) -> None:
        os.environ["BINANCE_SPOT_FILL_TP_FORMULA_B"] = "1"
        filled = {
            "symbol": "SOLUSDT",
            "orderId": 91001,
            "side": "BUY",
            "status": "FILLED",
            "executedQty": "10",
            "cummulativeQuoteQty": "1010",
        }
        client = _Client([filled], _trades([("10", "0", "USDT")]), ticker_px="109.80")
        adapter = self._adapter(client, auto_flatten_on_oco_fail=True, reanchor_oco_on_fill=False)
        saved_row = _position(tp_multiplier="0.85")
        # m=0.85 -> 109.585 < mark=109.80 < original_tp=110: Formula B blocked, original TP=110 kept.
        out = adapter.reconcile_resting_entry(
            saved_row,
            mark="109.80",
            persist=lambda fields: (saved_row.update(fields) or dict(saved_row)),
        )
        self.assertTrue(out["ok"], out)
        self.assertEqual(saved_row["fill_tp_formula_b"], "blocked")
        self.assertEqual(saved_row["take_profit_price"], "110")
        self.assertEqual(saved_row["stop_price"], "98")
        self.assertEqual(client.sells, [])
        self.assertEqual(len(client.ocos), 1)
        q = parse_qs(client.ocos[0])
        self.assertEqual(q["abovePrice"], ["110"])
        self.assertEqual(q["belowStopPrice"], ["98"])

    def test_resting_dust_partial_fill_archives_closed_dust_and_blocks_cross_symbol_buy(self) -> None:
        os.environ["BINANCE_SPOT_FILL_TP_FORMULA_B"] = "1"
        with tempfile.TemporaryDirectory(prefix="formula-b-dust-") as tmp:
            ledger = ExecutionLedger(Path(tmp) / "ledger.db")
            armed = ledger.arm_binance_spot_position(_position(quantity=None))
            pid = int(armed["id"])
            partial_dust = {
                "symbol": "SOLUSDT",
                "orderId": 91001,
                "side": "BUY",
                "status": "PARTIALLY_FILLED",
                "executedQty": "0.05",
                "cummulativeQuoteQty": "5.05",
            }
            canceled_dust = dict(partial_dust, status="CANCELED")
            client = _Client([partial_dust, canceled_dust], _trades([("0.05", "0", "USDT")]), cancel_ok=True)
            adapter = self._adapter(client, auto_flatten_on_oco_fail=True)
            out = adapter.reconcile_resting_entry(
                armed,
                mark="101",
                persist=lambda fields: ledger.update_binance_spot_position(pid, fields),
            )
            self.assertEqual(out["book"], "closed_dust")
            self.assertEqual(out["error"], "dust_untradable")
            self.assertEqual(client.ocos, [])
            self.assertEqual(client.sells, [])
            stored = ledger.binance_spot_position_by_id(pid)
            assert stored is not None
            self.assertEqual(stored["status"], "closed_dust")
            self.assertEqual(stored["residual_qty"], "0.05")
            self.assertEqual(stored["unprotected_qty"], "0.05")
            self.assertEqual(
                stored["manual_action"],
                "residual_below_min_is_not_a_complete_exit",
            )
            blocks = ledger.list_binance_spot_buy_blocks(PURPOSE_LIVE_BNB_BOLL_FADE)
            self.assertEqual(
                buy_exposure_block(blocks, [], purpose=PURPOSE_LIVE_BNB_BOLL_FADE, symbol="BTCUSDT"),
                "unprotected_position_blocks_buy",
            )
            ledger.close()

    def test_formula_b_flag_accepts_only_literal_one(self) -> None:
        for raw in ("", "0", "2", "1.0", "nope", "true", "TRUE", "yes", "on"):
            self.assertFalse(
                fill_tp_formula_b_enabled({"BINANCE_SPOT_FILL_TP_FORMULA_B": raw}),
                raw,
            )
        self.assertTrue(fill_tp_formula_b_enabled({"BINANCE_SPOT_FILL_TP_FORMULA_B": "1"}))
        self.assertTrue(fill_tp_formula_b_enabled({"BINANCE_SPOT_FILL_TP_FORMULA_B": " 1 "}))

    def test_oco_leg_residual_dust_persists_unprotected_qty_and_manual_action(self) -> None:
        with tempfile.TemporaryDirectory(prefix="formula-b-remdust-") as tmp:
            ledger = ExecutionLedger(Path(tmp) / "ledger.db")
            armed = ledger.arm_binance_spot_position(
                {
                    "decision_log_id": 170091,
                    "symbol": "SOLUSDT",
                    "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                    "entry_order_id": "91001",
                    "status": "oco_partial",
                    "protection": "exchange_oco",
                    "quantity": "10.005",
                    "unprotected_qty": "0.005",
                    "manual_action": "residual_below_min_is_not_a_complete_exit",
                    "entry_price": "100",
                    "stop_price": "98",
                    "take_profit_price": "110",
                }
            )
            pid = int(armed["id"])
            armed = ledger.update_binance_spot_position(
                pid,
                {
                    "residual_qty": "0.005",
                    "oco_order_list_id": "77",
                    "oco_list_client_id": "bnbfade-ol-170091",
                    "oco_above_order_id": "501",
                    "oco_below_order_id": "502",
                },
            ) or armed
            entry_filled = {
                "symbol": "SOLUSDT",
                "orderId": 91001,
                "side": "BUY",
                "status": "FILLED",
                "executedQty": "10.005",
                "cummulativeQuoteQty": "1000.5",
            }
            done_list = {
                "orderListId": 77,
                "listClientOrderId": "bnbfade-ol-170091",
                "listOrderStatus": "ALL_DONE",
                "listStatusType": "ALL_DONE",
                "symbol": "SOLUSDT",
                "orders": [
                    {"symbol": "SOLUSDT", "orderId": 501, "clientOrderId": "bnbfade-tp-170091"},
                    {"symbol": "SOLUSDT", "orderId": 502, "clientOrderId": "bnbfade-sl-170091"},
                ],
                "orderReports": [
                    {
                        "orderId": 501,
                        "clientOrderId": "bnbfade-tp-170091",
                        "symbol": "SOLUSDT",
                        "type": "LIMIT_MAKER",
                        "side": "SELL",
                        "status": "FILLED",
                        "price": "110",
                        "origQty": "10",
                        "executedQty": "10",
                    },
                    {
                        "orderId": 502,
                        "clientOrderId": "bnbfade-sl-170091",
                        "symbol": "SOLUSDT",
                        "type": "STOP_LOSS_LIMIT",
                        "side": "SELL",
                        "status": "EXPIRED",
                        "price": "97.99",
                        "stopPrice": "98",
                        "origQty": "10",
                        "executedQty": "0",
                    },
                ],
            }

            class _ResidualClient(_Client):
                def my_trades(self, signed_query: str, *, api_key: str) -> list:
                    form = parse_qs(signed_query)
                    if form.get("fromId"):
                        return []
                    oid = (form.get("orderId") or [""])[0]
                    if oid == "91001":
                        return _trades([("10.005", "0", "USDT")], order_id=91001)
                    if oid == "501":
                        return [
                            {
                                "id": 10,
                                "symbol": "SOLUSDT",
                                "orderId": 501,
                                "qty": "10",
                                "commission": "0",
                                "commissionAsset": "USDT",
                                "isBuyer": False,
                            }
                        ]
                    return []

            rc = _ResidualClient([entry_filled], [])
            rc.last_oco_resp = done_list
            adapter = self._adapter(rc, auto_flatten_on_oco_fail=True)
            out = adapter.maintain_held_position(
                armed,
                "110",
                persist=lambda fields: ledger.update_binance_spot_position(pid, fields),
            )
            self.assertEqual(out["book"], "closed_dust", out)
            self.assertEqual(rc.sells, [])
            stored = ledger.binance_spot_position_by_id(pid)
            assert stored is not None
            self.assertEqual(stored["status"], "closed_dust")
            self.assertEqual(stored["unprotected_qty"], "0.005")
            self.assertEqual(stored["residual_qty"], "0.005")
            self.assertEqual(
                stored["manual_action"],
                "residual_below_min_is_not_a_complete_exit",
            )
            ledger.close()

    def test_oco_unknown_official_list_miss_does_not_repost_or_rewrite_prices(self) -> None:
        os.environ["BINANCE_SPOT_FILL_TP_FORMULA_B"] = "1"
        with tempfile.TemporaryDirectory(prefix="formula-b-absent-oco-") as tmp:
            ledger = ExecutionLedger(Path(tmp) / "ledger.db")
            armed = ledger.arm_binance_spot_position(
                _position(status="open", entry_price="101", quantity="10")
            )
            pid = int(armed["id"])

            class _TimeoutOnceOcoClient(_Client):
                def __init__(self) -> None:
                    super().__init__([], _trades([("10", "0", "USDT")]))
                    self.fail_first_oco = True

                def new_oco(self, signed_query: str, *, api_key: str) -> dict:
                    if self.fail_first_oco:
                        self.ocos.append(signed_query)
                        self.fail_first_oco = False
                        raise TimeoutError("oco post timed out")
                    return super().new_oco(signed_query, api_key=api_key)

            client = _TimeoutOnceOcoClient()
            adapter = self._adapter(client, auto_flatten_on_oco_fail=True)
            # Tick 1: POST /orderList/oco times out. GET /api/v3/orderList then
            # returns the official miss HTTP 400 {"code":-2018,"msg":"Order list does not exist."}.
            t1 = adapter.maintain_held_position(
                armed,
                "101",
                persist=lambda fields: ledger.update_binance_spot_position(pid, fields),
            )
            self.assertEqual(t1["book"], "oco_unknown", t1)
            self.assertEqual(len(client.ocos), 1)
            self.assertEqual(client.sells, [])
            after_t1 = ledger.binance_spot_position_by_id(pid)
            assert after_t1 is not None
            self.assertEqual(after_t1["oco_list_client_id"], "bnbfade-ol-170003")
            self.assertEqual(after_t1["fill_tp_formula_b"], "applied")
            self.assertEqual(after_t1["take_profit_price"], "114.635")
            self.assertEqual(after_t1["stop_price"], "98")

            # Tick 2: the same official -2018, still inside the band, must not POST again.
            t2 = adapter.maintain_held_position(
                after_t1,
                "101",
                persist=lambda fields: ledger.update_binance_spot_position(pid, fields),
            )
            self.assertFalse(t2["ok"], t2)
            self.assertFalse(t2["submitted"], t2)
            self.assertEqual(t2["book"], "oco_unknown", t2)
            self.assertEqual(t2["error"], "oco_list_absent:-2018")
            self.assertEqual(len(client.ocos), 1)
            self.assertEqual(client.sells, [])
            only = parse_qs(client.ocos[0])
            self.assertEqual(only["listClientOrderId"], ["bnbfade-ol-170003"])
            self.assertEqual(only["abovePrice"], ["114.63"])
            self.assertEqual(only["belowStopPrice"], ["98"])
            after_t2 = ledger.binance_spot_position_by_id(pid)
            assert after_t2 is not None
            self.assertEqual(after_t2["status"], "oco_unknown")
            self.assertEqual(after_t2["stop_price"], "98")
            self.assertEqual(after_t2["take_profit_price"], "114.635")
            self.assertEqual(after_t2["fill_tp_formula_b"], "applied")
            self.assertEqual(after_t2["oco_list_client_id"], "bnbfade-ol-170003")
            self.assertEqual(
                buy_exposure_block(
                    [after_t2],
                    [],
                    purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
                    symbol="SOLUSDT",
                ),
                "oco_failed_blocks_buy",
            )

            # A new process with the same row still does not POST.
            restarted_client = _TimeoutOnceOcoClient()
            restarted = self._adapter(restarted_client, auto_flatten_on_oco_fail=True)
            t3 = restarted.maintain_held_position(
                after_t2,
                "101",
                persist=lambda fields: ledger.update_binance_spot_position(pid, fields),
            )
            self.assertEqual(t3["book"], "oco_unknown", t3)
            self.assertEqual(t3["error"], "oco_list_absent:-2018")
            self.assertEqual(restarted_client.ocos, [])
            self.assertEqual(restarted_client.sells, [])
            after_restart = ledger.binance_spot_position_by_id(pid)
            assert after_restart is not None
            self.assertEqual(after_restart["status"], "oco_unknown")
            self.assertEqual(after_restart["stop_price"], "98")
            self.assertEqual(after_restart["take_profit_price"], "114.635")
            self.assertEqual(after_restart["fill_tp_formula_b"], "applied")
            self.assertEqual(after_restart["oco_list_client_id"], "bnbfade-ol-170003")
            ledger.close()


if __name__ == "__main__":
    unittest.main()
