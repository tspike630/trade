"""Reduce-only kill, spot OCO, and dust rules for ``live_bnb_boll_fade``.

The entry order is still a normal spot order. After the exchange reports a
base fill, protection is a resting ``POST /api/v3/orderList/oco`` sell:

- above leg ``LIMIT_MAKER`` at the decision-time take-profit (tick-aligned)
- below leg ``STOP_LOSS_LIMIT`` triggered at the absolute stop, limit one
  tick below that stop so the stop can fill

That OCO stays on Binance if this process is down. A timeout is not treated
as rejection. This process does not watch a mark and market-sell in its place.

``LIVE_TRADING_DISABLED`` blocks a new buy only. It does not block a
ledger-tracked protective sell unless ``BINANCE_SPOT_BLOCK_SELL_ON_KILL=1``.
``BINANCE_SPOT_LIVE_ENABLED`` still has to be on before any order or signed
read. Dry-run never calls HTTP.
"""

from __future__ import annotations

import os
from decimal import ROUND_DOWN, Decimal
from typing import Any

from execution.executors.binance_spot import (
    _decimal_text,
    _filter_row,
    _min_notional,
    _positive_decimal,
    _symbol_filters,
    _to_decimal,
    floor_base_qty,
    floor_decimal_places,
)

BLOCK_SELL_ENV = "BINANCE_SPOT_BLOCK_SELL_ON_KILL"
FEE_BPS_ENV = "BINANCE_SPOT_TAKER_FEE_BPS"
ENTRY_MIN_NOTIONAL_USDT = Decimal("20")
BOOK_ENV = "BINANCE_SPOT_BOOK_USDT"
DEFAULT_TAKER_FEE_BPS = Decimal("10")
OCO_PATH = "/api/v3/orderList/oco"
ORDER_LIST_PATH = "/api/v3/orderList"

PROTECTION_LOOP = "held_position_exit"
PROTECTION_PENDING = "oco_pending"
PROTECTION_EXCHANGE = "exchange_oco"
PROTECTION_FALLBACK = "loop_fallback"
PROTECTION_DUST = "dust_abandon"

# listOrderStatus / listStatusType values that mean the list is still live.
_OCO_WORKING = frozenset({"EXECUTING", "EXEC_STARTED"})
_TERMINAL_LEG = frozenset({"CANCELED", "CANCELLED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"})
_DUST_DETAILS = frozenset({"exit_qty_below_min", "exit_below_min_notional", "oco_qty_below_min"})


def block_sell_on_kill() -> bool:
    raw = os.environ.get(BLOCK_SELL_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


def taker_fee_rate() -> Decimal | None:
    """Fraction of base assumed lost to the taker fee. Unset means 10 bps.

    This is only a pre-trade viability haircut. It is not subtracted from an
    order quantity and it is not a balance.
    """
    raw = os.environ.get(FEE_BPS_ENV)
    if raw is None or not str(raw).strip():
        bps = DEFAULT_TAKER_FEE_BPS
    else:
        text = str(raw).strip()
        if not text.isdigit():
            return None
        bps = Decimal(text)
    if bps < 0 or bps >= 10000:
        return None
    return bps / Decimal("10000")


def protective_order_block(gates: dict[str, Any]) -> str | None:
    """Why a ledger-tracked protective order must not be sent.

    The kill switch alone is reduce-only: it does not return an error here.
    ``BINANCE_SPOT_BLOCK_SELL_ON_KILL`` is the emergency halt for those sells.
    The live-enable flag still gates every order.
    """
    if not gates.get("live_enabled"):
        return "binance_spot_live_disabled"
    if gates.get("kill_switch_active") and (
        gates.get("block_sell_on_kill") or block_sell_on_kill()
    ):
        name = gates.get("kill_switch") or "LIVE_TRADING_DISABLED"
        return f"kill_switch_block_sell:{name}"
    return None


def _client_piece(prefix: str, raw: object) -> str | None:
    if isinstance(raw, bool) or raw is None:
        return None
    text = str(raw).strip()
    if not text.isdigit():
        return None
    client_id = f"{prefix}{text}"
    if len(client_id) > 36:
        return None
    return client_id


def oco_list_client_id(decision_id: object) -> str | None:
    return _client_piece("bnbfade-ol-", decision_id)


def oco_above_client_id(decision_id: object) -> str | None:
    return _client_piece("bnbfade-tp-", decision_id)


def oco_below_client_id(decision_id: object) -> str | None:
    return _client_piece("bnbfade-sl-", decision_id)


def reference_price(extras: dict[str, Any]) -> Decimal | None:
    for key in ("price", "limit_price", "price_usd", "entry_price"):
        number = _to_decimal(extras.get(key))
        if number is not None:
            return number
    return None


def planned_buy_notional(extras: dict[str, Any]) -> Decimal | None:
    """Quote we plan to spend. A base qty is valued only with a signal price."""
    quote = _to_decimal(extras.get("quote_order_qty", extras.get("quoteOrderQty")))
    if quote is not None:
        return quote
    qty = _to_decimal(extras.get("quantity"))
    price = reference_price(extras)
    if qty is not None and price is not None:
        return qty * price
    return None


def _has_order_size(extras: dict[str, Any]) -> bool:
    for key in ("quote_order_qty", "quoteOrderQty", "quantity"):
        if extras.get(key) not in (None, ""):
            return True
    return False


def entry_notional_floor_error(extras: dict[str, Any]) -> str | None:
    """20 USDT planned-entry floor. No exchange read. Not a daily cap.

    A buy with no size is unchanged (the adapter already defers that). A buy
    that names a size but cannot be valued, or is under 20 USDT, is refused.
    """
    if not _has_order_size(extras):
        return None
    notional = planned_buy_notional(extras)
    if notional is None:
        return "entry_notional_unknown"
    if notional < ENTRY_MIN_NOTIONAL_USDT:
        return "entry_notional_below_min"
    return None


def _limit_lot(filters: list[Any]) -> tuple[dict[str, Any] | None, str | None]:
    lot = _filter_row(filters, "LOT_SIZE")
    if lot is None:
        return None, None
    step = _to_decimal(lot.get("stepSize"))
    if step is None:
        return None, None
    return lot, _decimal_text(step)


def _tick_size(filters: list[Any]) -> str | None:
    price_filter = _filter_row(filters, "PRICE_FILTER")
    if price_filter is None:
        return None
    tick = _to_decimal(price_filter.get("tickSize"))
    if tick is None:
        return None
    return _decimal_text(tick)


def _align(price: Decimal, tick: Decimal, rounding: str) -> Decimal:
    units = (price / tick).to_integral_value(rounding=rounding)
    return units * tick


def entry_viability_error(
    extras: dict[str, Any],
    info: dict[str, Any],
    symbol: str,
) -> str | None:
    """Lot, minNotional, and a fee haircut. Does not change the order qty.

    The haircut assumes the taker fee is taken in the base asset. That can
    refuse a buy that would actually clear if the fee is paid in BNB. It
    never rewrites ``quoteOrderQty`` or ``quantity`` from a wallet balance.
    """
    floor_error = entry_notional_floor_error(extras)
    if floor_error:
        return floor_error
    filters = _symbol_filters(info, symbol)
    if not filters:
        return "entry_not_viable:missing_lot_filter"
    notional = planned_buy_notional(extras)
    if notional is None:
        return "entry_notional_unknown"
    min_notional = _min_notional(filters)
    if min_notional is not None and notional < min_notional:
        return "entry_not_viable:below_min_notional"
    price = reference_price(extras)
    if price is None:
        # Quote size cleared the floors. Base lot is checked again on the fill.
        return None
    lot, step = _limit_lot(filters)
    if lot is None or step is None:
        return "entry_not_viable:missing_lot_filter"
    fee = taker_fee_rate()
    if fee is None:
        return "entry_not_viable:bad_fee_bps"
    quote = _to_decimal(extras.get("quote_order_qty", extras.get("quoteOrderQty")))
    qty = _to_decimal(extras.get("quantity"))
    if quote is not None:
        gross = quote / price
    elif qty is not None:
        gross = qty
    else:
        return "entry_notional_unknown"
    net = gross * (Decimal("1") - fee)
    if net <= 0:
        return "entry_not_viable:below_min_qty"
    floored = floor_base_qty(_decimal_text(net), step)
    if floored is None:
        return "entry_not_viable:below_min_qty"
    floored_n = _to_decimal(floored)
    if floored_n is None:
        return "entry_not_viable:below_min_qty"
    min_qty = _to_decimal(lot.get("minQty"))
    if min_qty is not None and floored_n < min_qty:
        return "entry_not_viable:below_min_qty"
    if min_notional is not None and floored_n * price < min_notional:
        return "entry_not_viable:below_min_notional"
    return None


def sellable_base_qty(placed: dict[str, Any] | None, symbol: str) -> str | None:
    """``executedQty`` minus base-asset commission shown on this fill.

    Commission in any other asset is ignored. A missing ``fills`` array does
    not invent a fee.
    """
    if not isinstance(placed, dict):
        return None
    executed = _to_decimal(placed.get("executedQty"))
    if executed is None:
        return None
    base = symbol[: -len("USDT")] if symbol.endswith("USDT") else ""
    fee = Decimal("0")
    fills = placed.get("fills")
    if isinstance(fills, list) and base:
        for fill in fills:
            if not isinstance(fill, dict):
                continue
            asset = str(fill.get("commissionAsset") or "").upper()
            if asset != base:
                continue
            part = _to_decimal(fill.get("commission"))
            if part is not None:
                fee += part
    net = executed - fee
    if net <= 0:
        return None
    return _decimal_text(net)


def _quote_value(qty: str, mark: object, stop: object) -> tuple[str | None, str]:
    qty_n = _to_decimal(qty)
    if qty_n is None:
        return None, "none"
    mark_n = _to_decimal(mark)
    if mark_n is not None:
        return _decimal_text(qty_n * mark_n), "mark"
    stop_n = _to_decimal(stop)
    if stop_n is not None:
        return _decimal_text(qty_n * stop_n), "stop_price"
    return None, "none"


def dust_archive_fields(
    *,
    quantity: str,
    mark: object,
    stop: object,
    detail: str,
) -> dict[str, Any]:
    quote, source = _quote_value(quantity, mark, stop)
    return {
        "status": "closed_dust",
        "protection": PROTECTION_DUST,
        "exit_reason": "dust_abandon",
        "residual_qty": quantity,
        "residual_quote": quote,
        "last_error": f"dust_untradable:{detail}:{source}",
    }


def aligned_oco_prices(
    *,
    stop_price: str,
    take_profit_price: str,
    filters: list[Any],
    mark: object = None,
) -> tuple[dict[str, Decimal] | None, str | None]:
    """Tick-align a long's stop and take-profit. Prices are not rewritten to fit.

    ``mark``, when present, must sit strictly between the aligned stop trigger
    and the aligned take-profit. That is the same relation a native sell OCO
    needs against the exchange last price. A missing mark skips that check;
    ``protective_band_error`` is the caller that refuses a missing price.
    """
    tick = _tick_size(filters)
    if tick is None:
        return None, "oco_missing_tick"
    stop_n = _to_decimal(stop_price)
    tp_n = _to_decimal(take_profit_price)
    tick_n = _to_decimal(tick)
    if stop_n is None or tp_n is None or tick_n is None:
        return None, "oco_prices_crossed"
    above = _align(tp_n, tick_n, ROUND_DOWN)
    below_stop = _align(stop_n, tick_n, ROUND_DOWN)
    below_limit = below_stop - tick_n
    min_price = None
    price_filter = _filter_row(filters, "PRICE_FILTER")
    if price_filter is not None:
        min_price = _to_decimal(price_filter.get("minPrice"))
    if below_limit <= 0 or (min_price is not None and below_limit < min_price):
        below_limit = below_stop
    if not (below_limit > 0 and below_stop > below_limit and above > below_stop):
        if not (above > below_stop > 0 and below_limit == below_stop):
            return None, "oco_prices_crossed"
    mark_n = _to_decimal(mark)
    if mark_n is not None and not (below_stop < mark_n < above):
        return None, "oco_price_restriction"
    return {
        "above": above,
        "below_stop": below_stop,
        "below_limit": below_limit,
    }, None


def protective_band_error(
    *,
    stop_price: str,
    take_profit_price: str,
    mark: object,
    filters: list[Any],
) -> str | None:
    """Refuse a protective band that cannot be sent at ``mark``.

    Does not move the stop or the take-profit. A missing mark is
    ``mark_unavailable``, not a pass.
    """
    if _to_decimal(mark) is None:
        return "mark_unavailable"
    _aligned, error = aligned_oco_prices(
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        filters=filters,
        mark=mark,
    )
    return error


def fill_average_price(placed: dict[str, Any] | None) -> str | None:
    """Average fill from quote spent and base filled. A limit price is not a fill.

    ``cummulativeQuoteQty / executedQty`` is the only average. An unfilled
    order's ``price`` is not used, even when that field is present.
    """
    if not isinstance(placed, dict):
        return None
    quote = _to_decimal(placed.get("cummulativeQuoteQty"))
    qty = _to_decimal(placed.get("executedQty"))
    if quote is None or qty is None or qty <= 0:
        return None
    return _decimal_text(quote / qty)


# Numerical reference only. Live OCO keeps the signal's absolute stop and
# take-profit and the one-tick stop limit. These percents are not applied,
# and a stop-limit price is not a guaranteed sell. The 0.2% fee figure is not
# a substitute for the commission on the fill.
REFERENCE_TP_PCT = Decimal("10")
REFERENCE_SL_PCT = Decimal("5")
REFERENCE_SL_LIMIT_BUFFER_PCT = Decimal("0.3")
REFERENCE_FEE_BUFFER_PCT = Decimal("0.2")


def reference_protection_prices(avg_fill: str) -> dict[str, str] | None:
    """Illustration of 10% / 5% / 0.3% on an average fill. Not used for orders."""
    avg = _to_decimal(avg_fill)
    if avg is None:
        return None
    take_profit = avg * (Decimal(1) + REFERENCE_TP_PCT / Decimal(100))
    stop = avg * (Decimal(1) - REFERENCE_SL_PCT / Decimal(100))
    limit = stop - (avg * REFERENCE_SL_LIMIT_BUFFER_PCT / Decimal(100))
    if limit <= 0 or not (stop < avg < take_profit):
        return None
    return {
        "entry": _decimal_text(avg),
        "take_profit_price": _decimal_text(take_profit),
        "stop_price": _decimal_text(stop),
        "stop_limit_price": _decimal_text(limit),
        "fee_buffer_pct": _decimal_text(REFERENCE_FEE_BUFFER_PCT),
        "applied_to_live_orders": "no",
    }


def build_spot_oco(
    *,
    symbol: str,
    quantity: str,
    stop_price: str,
    take_profit_price: str,
    decision_id: object,
    filters: list[Any],
    mark: object,
    timestamp_ms: int,
) -> tuple[dict[str, Any] | None, str | None]:
    """Form body for ``POST /api/v3/orderList/oco``. No network.

    Returns ``(spec, error)``. ``spec["pairs"]`` is the unsigned form.
    """
    list_id = oco_list_client_id(decision_id)
    above_id = oco_above_client_id(decision_id)
    below_id = oco_below_client_id(decision_id)
    if list_id is None or above_id is None or below_id is None:
        return None, "invalid_oco_client_id"
    lot, step = _limit_lot(filters)
    if lot is None or step is None:
        return None, "oco_missing_lot_filter"
    if _tick_size(filters) is None:
        return None, "oco_missing_tick"
    sized = floor_base_qty(quantity, step)
    if sized is None:
        return None, "oco_qty_below_min"
    sized_n = _to_decimal(sized)
    min_qty = _to_decimal(lot.get("minQty"))
    if sized_n is None or (min_qty is not None and sized_n < min_qty):
        return None, "oco_qty_below_min"
    aligned, align_error = aligned_oco_prices(
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        filters=filters,
        mark=mark,
    )
    if align_error or aligned is None:
        return None, align_error or "oco_prices_crossed"
    above = aligned["above"]
    below_stop = aligned["below_stop"]
    below_limit = aligned["below_limit"]
    mark_n = _to_decimal(mark)
    min_notional = _min_notional(filters)
    if min_notional is not None:
        value_px = mark_n if mark_n is not None else below_stop
        if sized_n * value_px < min_notional:
            return None, "oco_qty_below_min"
    held = _to_decimal(quantity)
    residual = None
    if held is not None and held > sized_n:
        residual = _decimal_text(held - sized_n)
    pairs = [
        ("symbol", symbol),
        ("side", "SELL"),
        ("quantity", sized),
        ("listClientOrderId", list_id),
        ("aboveType", "LIMIT_MAKER"),
        ("aboveClientOrderId", above_id),
        ("abovePrice", _decimal_text(above)),
        ("belowType", "STOP_LOSS_LIMIT"),
        ("belowClientOrderId", below_id),
        ("belowPrice", _decimal_text(below_limit)),
        ("belowStopPrice", _decimal_text(below_stop)),
        ("belowTimeInForce", "GTC"),
        ("newOrderRespType", "FULL"),
        ("recvWindow", "5000"),
        ("timestamp", str(int(timestamp_ms))),
    ]
    return {
        "pairs": pairs,
        "quantity": sized,
        "residual_qty": residual,
        "list_client_id": list_id,
        "above_client_id": above_id,
        "below_client_id": below_id,
        "above_price": _decimal_text(above),
        "below_stop_price": _decimal_text(below_stop),
        "below_price": _decimal_text(below_limit),
    }, None


def _reports(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("orderReports")
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    return []


def _leg_by_type(payload: dict[str, Any], order_type: str) -> dict[str, Any] | None:
    for row in _reports(payload):
        if str(row.get("type") or "").upper() == order_type:
            return row
    return None


def oco_is_working(payload: dict[str, Any]) -> bool:
    status = str(payload.get("listOrderStatus") or "").upper()
    if status == "EXECUTING":
        return True
    if status in {"ALL_DONE", "REJECT"}:
        return False
    kind = str(payload.get("listStatusType") or "").upper()
    return kind in _OCO_WORKING


def oco_leg_ids(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """``(above_order_id, below_order_id)`` from reports, else the orders array."""
    above = _leg_by_type(payload, "LIMIT_MAKER")
    if above is None:
        above = _leg_by_type(payload, "TAKE_PROFIT_LIMIT")
    below = _leg_by_type(payload, "STOP_LOSS_LIMIT")
    if below is None:
        below = _leg_by_type(payload, "STOP_LOSS")
    above_id = None if above is None else above.get("orderId")
    below_id = None if below is None else below.get("orderId")
    if above_id is None or below_id is None:
        orders = payload.get("orders")
        if isinstance(orders, list):
            ids = [
                str(item.get("orderId")).strip()
                for item in orders
                if isinstance(item, dict) and item.get("orderId") is not None
            ]
            if above_id is None and len(ids) >= 1:
                above_id = ids[0]
            if below_id is None and len(ids) >= 2:
                below_id = ids[1]
    return (
        None if above_id is None else str(above_id).strip() or None,
        None if below_id is None else str(below_id).strip() or None,
    )


def classify_oco(payload: dict[str, Any], *, held_qty: str) -> dict[str, Any]:
    """Turn a list plus its leg reports into one book instruction.

    ``working`` — exchange still has the list; do not market-sell.
    ``filled`` — a leg filled; the list is done.
    ``partial`` — list finished with a remainder still held.
    ``unfilled`` — canceled, expired, or rejected with nothing executed.
    """
    list_status = str(payload.get("listOrderStatus") or "").upper()
    list_id = payload.get("orderListId")
    above_id, below_id = oco_leg_ids(payload)
    base = {
        "list_status": list_status or str(payload.get("listStatusType") or ""),
        "order_list_id": None if list_id is None else str(list_id),
        "above_order_id": above_id,
        "below_order_id": below_id,
    }
    if oco_is_working(payload):
        partial = False
        for row in _reports(payload):
            if str(row.get("status") or "").upper() == "PARTIALLY_FILLED":
                partial = True
        base["state"] = "partial_working" if partial else "working"
        return base
    executed = Decimal("0")
    filled_reason: str | None = None
    any_open = False
    for row in _reports(payload):
        status = str(row.get("status") or "").upper()
        got = _to_decimal(row.get("executedQty")) or Decimal("0")
        executed += got
        if status == "FILLED" or (status == "PARTIALLY_FILLED" and got > 0):
            leg = str(row.get("type") or "").upper()
            if leg in {"LIMIT_MAKER", "TAKE_PROFIT_LIMIT", "TAKE_PROFIT"}:
                filled_reason = "take_profit"
            elif leg in {"STOP_LOSS_LIMIT", "STOP_LOSS"}:
                filled_reason = "stop_loss"
        if status not in _TERMINAL_LEG and status != "FILLED":
            any_open = True
    held = _to_decimal(held_qty) or Decimal("0")
    base["executed_qty"] = _decimal_text(executed) if executed > 0 else "0"
    if filled_reason and executed > 0 and not any_open:
        if held > 0 and executed + Decimal("0.0000000000001") < held:
            remainder = held - executed
            base["state"] = "partial"
            base["remainder_qty"] = _decimal_text(remainder)
            base["exit_reason"] = filled_reason
            return base
        base["state"] = "filled"
        base["exit_reason"] = filled_reason
        return base
    if list_status in {"ALL_DONE", "REJECT"} or (
        _reports(payload) and not any_open and executed <= 0
    ):
        base["state"] = "unfilled"
        return base
    if not _reports(payload) and list_status in {"ALL_DONE", "REJECT"}:
        base["state"] = "unfilled"
        return base
    base["state"] = "unknown"
    return base


def is_request_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    return "timeout" in type(exc).__name__.lower()


def dust_detail(error: str | None) -> str | None:
    if error in _DUST_DETAILS:
        return error
    return None


def _explicit_order_size(extras: dict[str, Any]) -> bool:
    for key in ("quote_order_qty", "quoteOrderQty", "quantity"):
        if extras.get(key) not in (None, ""):
            return True
    return False


def book_usdt(environ: Any | None = None) -> Decimal | None:
    """Operator book for ``size_pct``. Not a wallet balance and not invented."""
    env = os.environ if environ is None else environ
    raw = env.get(BOOK_ENV) if hasattr(env, "get") else None
    if raw is None or not str(raw).strip():
        return None
    return _to_decimal(str(raw).strip())


def resolve_entry_size(
    sig: dict[str, Any],
    extras: dict[str, Any],
    *,
    require_book: bool = True,
    environ: Any | None = None,
) -> tuple[dict[str, Any], str | None, dict[str, Any] | None]:
    """Turn ``size_pct`` into a spot order size. Explicit qty/quote wins.

    ``quote = size_pct * BINANCE_SPOT_BOOK_USDT``. Below 20 USDT is refused.
    A missing book is ``missing_book_usdt``; nothing is guessed. MARKET buys
    get ``quoteOrderQty``. LIMIT buys get base ``quantity = quote / limit``.
    """
    if str(sig.get("action") or "").strip().lower() != "buy":
        return extras, None, None
    if _explicit_order_size(extras):
        return extras, None, {"source": "explicit"}
    if sig.get("size_pct") in (None, ""):
        return extras, None, None
    pct = _to_decimal(sig.get("size_pct"))
    if pct is None or pct <= 0 or pct > 1:
        return extras, "invalid_size_pct", None
    book = book_usdt(environ)
    if book is None:
        if not require_book:
            return extras, None, None
        return extras, "missing_book_usdt", None
    quote = pct * book
    plan: dict[str, Any] = {
        "source": "size_pct",
        "size_pct": _decimal_text(pct),
        "book_usdt": _decimal_text(book),
        "quote_usdt": _decimal_text(quote),
    }
    if quote < ENTRY_MIN_NOTIONAL_USDT:
        return extras, "entry_notional_below_min", plan
    # Floor only after the 20 USDT check so a half-size book still refuses.
    quote_text = floor_decimal_places(quote)
    sent_quote = None if quote_text is None else _to_decimal(quote_text)
    if quote_text is None or sent_quote is None or sent_quote < ENTRY_MIN_NOTIONAL_USDT:
        return extras, "entry_notional_below_min", plan
    plan["quote_usdt"] = quote_text
    order_type = str(extras.get("order_type") or extras.get("binance_order_type") or "MARKET").strip().upper()
    if order_type == "MAKER":
        order_type = "LIMIT_MAKER"
    sized = dict(extras)
    if order_type in {"LIMIT", "LIMIT_MAKER"}:
        price = _to_decimal(extras.get("limit_price"))
        if price is None:
            price = _to_decimal(extras.get("price"))
        if price is None and order_type == "LIMIT_MAKER":
            sized["quote_order_qty"] = quote_text
            plan["order_type"] = "LIMIT_MAKER"
            plan["needs_book_price"] = True
            return sized, None, plan
        if price is None:
            return extras, "missing_limit_price", plan
        qty = sent_quote / price
        if qty <= 0:
            return extras, "entry_not_viable:below_min_qty", plan
        qty_text = floor_decimal_places(qty)
        if qty_text is None:
            return extras, "entry_not_viable:below_min_qty", plan
        sized["quantity"] = qty_text
        plan["order_type"] = order_type
        plan["needs_lot_floor"] = True
        return sized, None, plan
    if order_type not in {"MARKET", ""}:
        return extras, "invalid_order_type", plan
    sized["quote_order_qty"] = quote_text
    plan["order_type"] = "MARKET"
    return sized, None, plan


def tracked_exit(position: dict[str, Any]) -> bool:
    """True only for a ledger row, not an inbound decision signal."""
    row_id = position.get("id")
    if isinstance(row_id, bool) or row_id is None:
        return False
    if isinstance(row_id, int):
        return row_id > 0
    if isinstance(row_id, str) and row_id.strip().isdigit():
        return int(row_id.strip()) > 0
    return False
