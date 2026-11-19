"""In-process cross-margin stand-in. It never opens a socket.

Balances, fills, interest-first repayment, and error codes are a test model.
They are not an exchange acceptance record. Interest is repaid before principal
because that is the usual margin repayment order; the track still confirms
flatness from the liability readback rather than from this model.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from execution.margin_cross.errors import MarginExchangeError, MarginTransportTimeout
from execution.margin_cross.money import api_amount, parse_decimal


@dataclass
class _Script:
    method: str
    when: str
    kind: str
    code: int = -2010
    message: str = "rejected"
    on_call: int = 0


class OfflineFixtureClient:
    performs_network_io = False

    def __init__(self) -> None:
        self.account_uid = ""
        self.margin_mode = "CROSS"
        self.account_type = "MARGIN_1"
        self.trade_enabled = True
        self.borrow_enabled = True
        self.created = True
        self.margin_level = Decimal("1")
        self.market_price = Decimal("1")
        self.bid_qty = Decimal("0")
        self.ask_qty = Decimal("0")
        self.fee_rate = Decimal("0")
        self.fee_asset = "USDT"
        self.balances: dict[str, dict[str, Decimal]] = {}
        self.max_borrow: dict[str, Decimal] = {}
        self.borrow_limit: dict[str, Decimal] = {}
        self.hourly_rate: dict[str, Decimal] = {}
        self.interest_on_borrow = Decimal("0")
        self.after_borrow_margin_level: Decimal | None = None
        self.partial_fill_qty: Decimal | None = None
        self.force_isolated_response = False
        self.margin_buy_borrow_amount: Decimal | None = None
        self.repay_ack_without_apply = False
        self.min_repay_residual: Decimal | None = None
        self.query_order_misses = 0
        self.query_trades_empty = 0
        self.trade_qty_override: Decimal | None = None
        self.interrupt_after: str | None = None
        self._scripts: list[_Script] = []
        self._call_counts: dict[str, int] = {}
        self._orders: dict[str, dict[str, Any]] = {}
        self._lists: dict[str, dict[str, Any]] = {}
        self._trades: dict[int, list[dict[str, Any]]] = {}
        self._txs: list[dict[str, Any]] = []
        self._interest_rows: list[dict[str, Any]] = []
        self._seq = 0
        self.write_log: list[tuple[str, dict[str, Any]]] = []
        self.read_log: list[tuple[str, dict[str, Any]]] = []

    def fail_next(
        self,
        method: str,
        *,
        when: str = "before",
        kind: str = "timeout",
        code: int = -2010,
        message: str = "rejected",
        on_call: int = 0,
    ) -> None:
        self._scripts.append(_Script(method, when, kind, code, message, on_call))

    def asset(self, name: str) -> dict[str, Decimal]:
        row = self.balances.get(name)
        if row is None:
            row = {
                "free": Decimal("0"),
                "locked": Decimal("0"),
                "borrowed": Decimal("0"),
                "interest": Decimal("0"),
            }
            self.balances[name] = row
        return row

    def _next_id(self) -> int:
        self._seq += 1
        return self._seq

    def _record_write(self, method: str, payload: dict[str, Any]) -> None:
        self.write_log.append((method, dict(payload)))

    def _fire(self, method: str, when: str) -> None:
        if when == "before":
            self._call_counts[method] = self._call_counts.get(method, 0) + 1
        count = self._call_counts.get(method, 0)
        for index, script in enumerate(self._scripts):
            if script.method != method or script.when != when:
                continue
            if script.on_call not in (0, count):
                continue
            self._scripts.pop(index)
            if script.kind == "timeout":
                raise MarginTransportTimeout(method)
            raise MarginExchangeError(script.code, script.message)

    def _dec(self, payload: dict[str, Any], field: str) -> Decimal:
        number, problem = parse_decimal(payload.get(field), field)
        if problem or number is None:
            raise MarginExchangeError(-1102, problem or field)
        return number

    def _require_cross(self, payload: dict[str, Any]) -> None:
        if payload.get("isIsolated") != "FALSE":
            raise MarginExchangeError(-1102, "isIsolated must be FALSE for this track")
        if "symbol" in payload and payload.get("type") in {"BORROW", "REPAY"}:
            raise MarginExchangeError(-1102, "symbol is isolated-only")

    def _read(self, method: str, payload: dict[str, Any] | None = None) -> None:
        self.read_log.append((method, {} if payload is None else dict(payload)))

    def query_account(self) -> dict[str, Any]:
        self._read("query_account")
        assets = []
        for name in sorted(self.balances):
            row = self.balances[name]
            net = row["free"] + row["locked"] - row["borrowed"] - row["interest"]
            assets.append(
                {
                    "asset": name,
                    "free": api_amount(row["free"]),
                    "locked": api_amount(row["locked"]),
                    "borrowed": api_amount(row["borrowed"]),
                    "interest": api_amount(row["interest"]),
                    "netAsset": api_amount(net),
                }
            )
        return {
            "created": self.created,
            "borrowEnabled": self.borrow_enabled,
            "tradeEnabled": self.trade_enabled,
            "marginLevel": api_amount(self.margin_level),
            "accountType": self.account_type,
            "userAssets": assets,
        }

    def query_max_borrow(self, payload: dict[str, Any]) -> dict[str, str]:
        self._read("query_max_borrow", payload)
        if "isolatedSymbol" in payload:
            raise MarginExchangeError(-1102, "isolatedSymbol is not cross")
        asset = str(payload.get("asset") or "")
        if asset not in self.max_borrow:
            raise MarginExchangeError(-3012, "ASSET_ADMIN_BAN_BORROW")
        return {
            "amount": api_amount(self.max_borrow[asset]),
            "borrowLimit": api_amount(self.borrow_limit.get(asset, Decimal("0"))),
        }

    def query_hourly_interest(self, payload: dict[str, Any]) -> list[dict[str, str]]:
        self._read("query_hourly_interest", payload)
        if payload.get("isIsolated") != "FALSE":
            raise MarginExchangeError(-1102, "isIsolated must be FALSE")
        asset = str(payload.get("assets") or "")
        if asset not in self.hourly_rate:
            raise MarginExchangeError(-1121, "invalid asset")
        return [
            {
                "asset": asset,
                "nextHourlyInterestRate": api_amount(self.hourly_rate[asset]),
            }
        ]

    def accrue_interest(
        self,
        asset: str,
        interest_amount: Decimal,
        *,
        principal: Decimal | None = None,
        accrued_type: str = "PERIODIC",
        isolated_symbol: str = "",
        accrued_time_ms: int = 1_700_000_001_000,
    ) -> int:
        row = self.asset(asset)
        eff_principal = row["borrowed"] if principal is None else principal
        row["interest"] += interest_amount
        tx_id = self._next_id()
        self._interest_rows.append(
            {
                "txId": tx_id,
                "interestAccruedTime": int(accrued_time_ms),
                "asset": asset,
                "rawAsset": asset,
                "principal": api_amount(eff_principal),
                "interest": api_amount(interest_amount),
                "interestRate": api_amount(self.hourly_rate.get(asset, Decimal("0"))),
                "type": accrued_type,
                "isolatedSymbol": isolated_symbol,
            }
        )
        return tx_id

    def query_interest_history(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._read("query_interest_history", payload)
        self._fire("query_interest_history", "before")
        if "isolatedSymbol" in payload:
            raise MarginExchangeError(-1102, "isolatedSymbol is not cross")
        asset = str(payload.get("asset") or "")
        rows = [
            dict(item)
            for item in self._interest_rows
            if not asset or item.get("asset") == asset
        ]
        self._fire("query_interest_history", "after")
        return {"rows": rows, "total": len(rows)}

    def query_borrow_repay(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._read("query_borrow_repay", payload)
        wanted = str(payload.get("type") or "")
        asset = str(payload.get("asset") or "")
        rows = [
            dict(row)
            for row in self._txs
            if row["type"] == wanted and row["asset"] == asset
        ]
        return {"rows": rows, "total": len(rows)}

    def query_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._read("query_order", payload)
        if self.query_order_misses > 0:
            self.query_order_misses -= 1
            raise MarginExchangeError(-2013, "Order does not exist")
        client_id = str(payload.get("origClientOrderId") or "")
        order = self._orders.get(client_id)
        if order is None:
            raise MarginExchangeError(-2013, "Order does not exist")
        return dict(order)

    def query_oco(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._read("query_oco", payload)
        client_id = str(payload.get("origClientOrderId") or "")
        row = self._lists.get(client_id)
        if row is None:
            raise MarginExchangeError(-2011, "Unknown order sent")
        public = dict(row)
        public.pop("lock", None)
        return public

    def query_open_orders(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        self._read("query_open_orders", payload)
        symbol = str(payload.get("symbol") or "")
        return [
            dict(order)
            for order in self._orders.values()
            if order["symbol"] == symbol
            and order["status"] in {"NEW", "PARTIALLY_FILLED", "PENDING_NEW"}
        ]

    def query_trades(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        self._read("query_trades", payload)
        if self.query_trades_empty > 0:
            self.query_trades_empty -= 1
            return []
        order_id = int(payload.get("orderId") or 0)
        trades = [dict(row) for row in self._trades.get(order_id, [])]
        if self.trade_qty_override is not None and trades:
            trades[0]["qty"] = api_amount(self.trade_qty_override)
        return trades

    def borrow(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._record_write("borrow", payload)
        self._require_cross(payload)
        if payload.get("type") != "BORROW":
            raise MarginExchangeError(-1102, "type")
        self._fire("borrow", "before")
        asset = str(payload["asset"])
        amount = self._dec(payload, "amount")
        available = self.max_borrow.get(asset)
        if available is None:
            raise MarginExchangeError(-3012, "ASSET_ADMIN_BAN_BORROW")
        if amount > available:
            raise MarginExchangeError(-3006, "EXCEED_MAX_BORROWABLE")
        row = self.asset(asset)
        row["free"] += amount
        row["borrowed"] += amount
        row["interest"] += self.interest_on_borrow
        self.max_borrow[asset] = available - amount
        if self.after_borrow_margin_level is not None:
            self.margin_level = self.after_borrow_margin_level
        tx_id = self._next_id()
        self._txs.append(
            {
                "type": "BORROW",
                "asset": asset,
                "amount": api_amount(amount),
                "principal": api_amount(amount),
                "interest": api_amount(self.interest_on_borrow),
                "status": "CONFIRMED",
                "txId": tx_id,
                "isolatedSymbol": "",
            }
        )
        if self.interest_on_borrow > 0:
            self._interest_rows.append(
                {
                    "txId": self._next_id(),
                    "interestAccruedTime": 1_700_000_000_000,
                    "asset": asset,
                    "rawAsset": asset,
                    "principal": api_amount(amount),
                    "interest": api_amount(self.interest_on_borrow),
                    "interestRate": api_amount(self.hourly_rate.get(asset, Decimal("0"))),
                    "type": "ON_BORROW",
                    "isolatedSymbol": "",
                }
            )
        self._fire("borrow", "after")
        return {"tranId": tx_id}

    def repay(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._record_write("repay", payload)
        self._require_cross(payload)
        if payload.get("type") != "REPAY":
            raise MarginExchangeError(-1102, "type")
        self._fire("repay", "before")
        asset = str(payload["asset"])
        amount = self._dec(payload, "amount")
        row = self.asset(asset)
        liability = row["borrowed"] + row["interest"]
        if amount > liability:
            raise MarginExchangeError(-3015, "REPAY_EXCEED_LIABILITY")
        if self.min_repay_residual is not None:
            remaining = liability - amount
            if Decimal(0) < remaining < self.min_repay_residual:
                raise MarginExchangeError(-3015, "REPAY_EXCEED_LIABILITY")
        if row["free"] < amount:
            raise MarginExchangeError(-2010, "insufficient balance")
        tx_id = self._next_id()
        if self.repay_ack_without_apply:
            self._fire("repay", "after")
            return {"tranId": tx_id}
        pay_interest = min(row["interest"], amount)
        pay_principal = amount - pay_interest
        row["interest"] -= pay_interest
        row["borrowed"] -= pay_principal
        row["free"] -= amount
        self._txs.append(
            {
                "type": "REPAY",
                "asset": asset,
                "amount": api_amount(amount),
                "principal": api_amount(pay_principal),
                "interest": api_amount(pay_interest),
                "status": "CONFIRMED",
                "txId": tx_id,
                "isolatedSymbol": "",
            }
        )
        self._fire("repay", "after")
        return {"tranId": tx_id}

    def new_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._record_write("new_order", payload)
        self._require_cross(payload)
        if payload.get("sideEffectType") != "NO_SIDE_EFFECT":
            raise MarginExchangeError(-1102, "sideEffectType")
        if payload.get("type") != "LIMIT":
            raise MarginExchangeError(-1116, "order type not used by this fixture")
        self._fire("new_order", "before")
        client_id = str(payload["newClientOrderId"])
        if client_id in self._orders:
            raise MarginExchangeError(-2010, "duplicate client order id")
        quantity = self._dec(payload, "quantity")
        price = self._dec(payload, "price")
        side = str(payload["side"])
        order = self._new_order_row(payload, client_id, quantity, price, side)
        self._apply_order_placement(order, quantity, price, side)
        self._fire("new_order", "after")
        return dict(order)

    def _new_order_row(
        self,
        payload: dict[str, Any],
        client_id: str,
        quantity: Decimal,
        price: Decimal,
        side: str,
    ) -> dict[str, Any]:
        order_id = self._next_id()
        isolated = "TRUE" if self.force_isolated_response else "FALSE"
        borrow_amount = self.margin_buy_borrow_amount
        order = {
            "symbol": payload["symbol"],
            "orderId": order_id,
            "clientOrderId": client_id,
            "status": "NEW",
            "side": side,
            "type": "LIMIT",
            "origQty": api_amount(quantity),
            "executedQty": api_amount(Decimal("0")),
            "price": api_amount(price),
            "isIsolated": isolated,
            "sideEffectType": "NO_SIDE_EFFECT",
            "timeInForce": payload.get("timeInForce", "GTC"),
        }
        if borrow_amount is not None:
            order["marginBuyBorrowAmount"] = api_amount(borrow_amount)
            order["marginBuyBorrowAsset"] = "USDT"
        self._orders[client_id] = order
        return order

    def _apply_order_placement(
        self, order: dict[str, Any], quantity: Decimal, price: Decimal, side: str
    ) -> None:
        base, quote = _split_symbol(str(order["symbol"]))
        marketable = (side == "SELL" and price <= self.market_price) or (
            side == "BUY" and price >= self.market_price
        )
        if side == "SELL":
            self._place_sell(order, base, quote, quantity, price, marketable)
        else:
            self._place_buy(order, base, quote, quantity, price, marketable)

    def _place_sell(
        self,
        order: dict[str, Any],
        base: str,
        quote: str,
        quantity: Decimal,
        price: Decimal,
        marketable: bool,
    ) -> None:
        row = self.asset(base)
        if row["free"] < quantity:
            self._reject_local(order)
            raise MarginExchangeError(-2010, "insufficient balance")
        fill_qty = quantity
        if marketable and self.partial_fill_qty is not None:
            fill_qty = self.partial_fill_qty
            if fill_qty <= 0 or fill_qty >= quantity:
                raise MarginExchangeError(-1013, "partial fixture qty")
        if not marketable:
            row["free"] -= quantity
            row["locked"] += quantity
            if order["timeInForce"] == "IOC":
                row["locked"] -= quantity
                row["free"] += quantity
                order["status"] = "EXPIRED"
            return
        self._fill(order, base, quote, fill_qty, price, "SELL")
        if fill_qty < quantity:
            remainder = quantity - fill_qty
            row["free"] -= remainder
            row["locked"] += remainder
            order["status"] = "PARTIALLY_FILLED"

    def _place_buy(
        self,
        order: dict[str, Any],
        base: str,
        quote: str,
        quantity: Decimal,
        price: Decimal,
        marketable: bool,
    ) -> None:
        cost = _quote_cost(quantity, price, self.fee_rate, self.fee_asset, quote)
        quote_row = self.asset(quote)
        if quote_row["free"] < cost:
            self._reject_local(order)
            raise MarginExchangeError(-2010, "insufficient balance")
        if not marketable:
            quote_row["free"] -= cost
            quote_row["locked"] += cost
            if order["timeInForce"] == "IOC":
                quote_row["locked"] -= cost
                quote_row["free"] += cost
                order["status"] = "EXPIRED"
            return
        self._fill(order, base, quote, quantity, price, "BUY")

    def _fill(
        self,
        order: dict[str, Any],
        base: str,
        quote: str,
        quantity: Decimal,
        price: Decimal,
        side: str,
    ) -> None:
        notional = quantity * price
        fee = notional * self.fee_rate
        if side == "SELL":
            base_row = self.asset(base)
            if order["status"] == "NEW" and base_row["free"] >= quantity:
                base_row["free"] -= quantity
            elif base_row["locked"] >= quantity:
                base_row["locked"] -= quantity
            else:
                raise MarginExchangeError(-2010, "insufficient balance")
            self.asset(quote)["free"] += notional - fee
        else:
            cost = notional + fee
            quote_row = self.asset(quote)
            if quote_row["locked"] >= cost:
                quote_row["locked"] -= cost
            elif quote_row["free"] >= cost:
                quote_row["free"] -= cost
            else:
                raise MarginExchangeError(-2010, "insufficient balance")
            self.asset(base)["free"] += quantity
        previous, _ = parse_decimal(order["executedQty"], "executedQty")
        executed = (previous or Decimal(0)) + quantity
        order["executedQty"] = api_amount(executed)
        order["status"] = "FILLED"
        trade = {
            "id": self._next_id(),
            "orderId": order["orderId"],
            "symbol": order["symbol"],
            "price": api_amount(price),
            "qty": api_amount(quantity),
            "commission": api_amount(fee),
            "commissionAsset": self.fee_asset,
            "isBuyer": side == "BUY",
            "isMaker": False,
            "isIsolated": False,
        }
        self._trades.setdefault(int(order["orderId"]), []).append(trade)

    def _reject_local(self, order: dict[str, Any]) -> None:
        order["status"] = "REJECTED"
        self._orders.pop(str(order["clientOrderId"]), None)

    def cancel_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._record_write("cancel_order", payload)
        self._require_cross(payload)
        self._fire("cancel_order", "before")
        client_id = str(payload.get("origClientOrderId") or "")
        order = self._orders.get(client_id)
        if order is None or order["status"] in {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}:
            raise MarginExchangeError(-2011, "Unknown order sent")
        self._unlock_remainder(order)
        order["status"] = "CANCELED"
        self._fire("cancel_order", "after")
        return dict(order)

    def _unlock_remainder(self, order: dict[str, Any]) -> None:
        base, quote = _split_symbol(str(order["symbol"]))
        orig, _ = parse_decimal(order["origQty"], "origQty")
        executed, _ = parse_decimal(order["executedQty"], "executedQty")
        price, _ = parse_decimal(order["price"], "price")
        remainder = (orig or Decimal(0)) - (executed or Decimal(0))
        if remainder <= 0 or price is None:
            return
        if order["side"] == "SELL":
            row = self.asset(base)
            row["locked"] -= remainder
            row["free"] += remainder
        else:
            cost = _quote_cost(remainder, price, self.fee_rate, self.fee_asset, quote)
            row = self.asset(quote)
            row["locked"] -= cost
            row["free"] += cost

    def new_oco(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._record_write("new_oco", payload)
        self._require_cross(payload)
        if payload.get("sideEffectType") != "NO_SIDE_EFFECT":
            raise MarginExchangeError(-1102, "sideEffectType")
        self._fire("new_oco", "before")
        list_id = str(payload["listClientOrderId"])
        if list_id in self._lists:
            raise MarginExchangeError(-2010, "duplicate list client id")
        quantity = self._dec(payload, "quantity")
        limit_price = self._dec(payload, "price")
        stop_price = self._dec(payload, "stopPrice")
        stop_limit = self._dec(payload, "stopLimitPrice")
        side = str(payload["side"])
        lock = self._lock_oco(str(payload["symbol"]), side, quantity, limit_price, stop_limit)
        limit_order = self._child_order(
            payload, str(payload["limitClientOrderId"]), quantity, limit_price, side, "LIMIT"
        )
        stop_order = self._child_order(
            payload,
            str(payload["stopClientOrderId"]),
            quantity,
            stop_limit,
            side,
            "STOP_LOSS_LIMIT",
        )
        stop_order["stopPrice"] = api_amount(stop_price)
        listed = {
            "orderListId": self._next_id(),
            "contingencyType": "OCO",
            "listStatusType": "EXEC_STARTED",
            "listOrderStatus": "EXECUTING",
            "listClientOrderId": list_id,
            "symbol": payload["symbol"],
            "isIsolated": False,
            "orders": [
                {
                    "symbol": payload["symbol"],
                    "orderId": limit_order["orderId"],
                    "clientOrderId": limit_order["clientOrderId"],
                },
                {
                    "symbol": payload["symbol"],
                    "orderId": stop_order["orderId"],
                    "clientOrderId": stop_order["clientOrderId"],
                },
            ],
            "lock": lock,
        }
        self._lists[list_id] = listed
        self._fire("new_oco", "after")
        public = dict(listed)
        public.pop("lock", None)
        return public

    def _child_order(
        self,
        payload: dict[str, Any],
        client_id: str,
        quantity: Decimal,
        price: Decimal,
        side: str,
        order_type: str,
    ) -> dict[str, Any]:
        order = {
            "symbol": payload["symbol"],
            "orderId": self._next_id(),
            "clientOrderId": client_id,
            "status": "NEW",
            "side": side,
            "type": order_type,
            "origQty": api_amount(quantity),
            "executedQty": api_amount(Decimal("0")),
            "price": api_amount(price),
            "isIsolated": "FALSE",
            "sideEffectType": "NO_SIDE_EFFECT",
            "timeInForce": "GTC",
        }
        self._orders[client_id] = order
        return order

    def _lock_oco(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        limit_price: Decimal,
        stop_limit: Decimal,
    ) -> dict[str, Any]:
        base, quote = _split_symbol(symbol)
        if side == "BUY":
            worst = max(
                _quote_cost(quantity, limit_price, self.fee_rate, self.fee_asset, quote),
                _quote_cost(quantity, stop_limit, self.fee_rate, self.fee_asset, quote),
            )
            row = self.asset(quote)
            if row["free"] < worst:
                raise MarginExchangeError(-2010, "insufficient balance")
            row["free"] -= worst
            row["locked"] += worst
            return {"asset": quote, "amount": api_amount(worst)}
        row = self.asset(base)
        if row["free"] < quantity:
            raise MarginExchangeError(-2010, "insufficient balance")
        row["free"] -= quantity
        row["locked"] += quantity
        return {"asset": base, "amount": api_amount(quantity)}

    def fill_oco_limit(self, list_client_order_id: str) -> None:
        listed = self._lists.get(list_client_order_id)
        if listed is None or listed["listOrderStatus"] != "EXECUTING":
            raise RuntimeError("oco is not working")
        limit_id = str(listed["orders"][0]["clientOrderId"])
        stop_id = str(listed["orders"][1]["clientOrderId"])
        limit_order = self._orders[limit_id]
        stop_order = self._orders[stop_id]
        quantity, _ = parse_decimal(limit_order["origQty"], "origQty")
        price, _ = parse_decimal(limit_order["price"], "price")
        if quantity is None or price is None:
            raise RuntimeError("oco qty")
        lock = listed.get("lock")
        if isinstance(lock, dict):
            amount, problem = parse_decimal(lock.get("amount"), "amount")
            if problem or amount is None:
                raise RuntimeError("oco lock")
            row = self.asset(str(lock["asset"]))
            row["locked"] -= amount
            row["free"] += amount
        self._fill(
            limit_order,
            *_split_symbol(str(limit_order["symbol"])),
            quantity,
            price,
            str(limit_order["side"]),
        )
        stop_order["status"] = "EXPIRED"
        listed["listStatusType"] = "ALL_DONE"
        listed["listOrderStatus"] = "ALL_DONE"


def _split_symbol(symbol: str) -> tuple[str, str]:
    if not symbol.endswith("USDT"):
        raise MarginExchangeError(-1121, "fixture symbols are *USDT only")
    return symbol[: -len("USDT")], "USDT"


def _quote_cost(
    quantity: Decimal, price: Decimal, fee_rate: Decimal, fee_asset: str, quote: str
) -> Decimal:
    notional = quantity * price
    if fee_asset == quote:
        return notional + (notional * fee_rate)
    return notional
