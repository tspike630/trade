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
REANCHOR_OCO_ENV = "BINANCE_SPOT_REANCHOR_OCO_ON_FILL"
AUTO_FLATTEN_ENV = "BINANCE_SPOT_AUTO_FLATTEN_ON_OCO_FAIL"
ENTRY_MIN_NOTIONAL_USDT = Decimal("20")
BOOK_ENV = "BINANCE_SPOT_BOOK_USDT"
DEFAULT_TAKER_FEE_BPS = Decimal("10")
MIN_REANCHOR_STOP_GAP = Decimal("0.005")
OCO_PATH = "/api/v3/orderList/oco"
ORDER_LIST_PATH = "/api/v3/orderList"

PROTECTION_LOOP = "held_position_exit"
PROTECTION_PENDING = "oco_pending"
PROTECTION_EXCHANGE = "exchange_oco"
PROTECTION_FALLBACK = "loop_fallback"
PROTECTION_DUST = "dust_abandon"
PROTECTION_AUTO_FLATTEN = "auto_flatten"

# listOrderStatus / listStatusType values that mean the list is still live.
_OCO_WORKING = frozenset({"EXECUTING", "EXEC_STARTED"})
_TERMINAL_LEG = frozenset({"CANCELED", "CANCELLED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"})
_DUST_DETAILS = frozenset({"exit_qty_below_min", "exit_below_min_notional", "oco_qty_below_min"})


def block_sell_on_kill() -> bool:
    raw = os.environ.get(BLOCK_SELL_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


def reanchor_oco_on_fill_enabled() -> bool:
    raw = os.environ.get(REANCHOR_OCO_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


def auto_flatten_on_oco_fail_enabled() -> bool:
    raw = os.environ.get(AUTO_FLATTEN_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


def flatten_client_order_id(decision_id: object) -> str | None:
    """Deterministic client order id for an emergency naked-position flatten sell."""
    return _client_piece("bnbfade-fl-", decision_id)


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


def _client_piece(prefix: str, raw: object, *, ratchet_seq: int = 0) -> str | None:
    if isinstance(raw, bool) or raw is None:
        return None
    text = str(raw).strip()
    if not text.isdigit():
        return None
    seq = int(ratchet_seq) if isinstance(ratchet_seq, int) and not isinstance(ratchet_seq, bool) else 0
    suffix = f"-r{seq}" if seq > 0 else ""
    client_id = f"{prefix}{text}{suffix}"
    if len(client_id) > 36:
        return None
    return client_id


def oco_list_client_id(decision_id: object, *, ratchet_seq: int = 0) -> str | None:
    return _client_piece("bnbfade-ol-", decision_id, ratchet_seq=ratchet_seq)


def oco_above_client_id(decision_id: object, *, ratchet_seq: int = 0) -> str | None:
    return _client_piece("bnbfade-tp-", decision_id, ratchet_seq=ratchet_seq)


def oco_below_client_id(decision_id: object, *, ratchet_seq: int = 0) -> str | None:
    return _client_piece("bnbfade-sl-", decision_id, ratchet_seq=ratchet_seq)


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
    """Base left from an order ack that lists every fill's commission.

    A missing ``fills`` list, an empty list, or a fill with no commission
    is unknown, not a zero fee. Commission in another asset does not change
    the base quantity. This is the ack helper. A later buy uses ``myTrades``.
    """
    if not isinstance(placed, dict):
        return None
    executed = _to_decimal(placed.get("executedQty"))
    if executed is None or executed <= 0:
        return None
    fills = placed.get("fills")
    if not isinstance(fills, list) or not fills:
        return None
    base = symbol[: -len("USDT")] if symbol.endswith("USDT") else ""
    fee = Decimal("0")
    qty = Decimal("0")
    for fill in fills:
        if not isinstance(fill, dict):
            return None
        part_qty = _to_decimal(fill.get("qty"))
        if part_qty is None or part_qty <= 0:
            return None
        qty += part_qty
        if "commission" not in fill or "commissionAsset" not in fill:
            return None
        asset = str(fill.get("commissionAsset") or "").strip().upper()
        if not asset:
            return None
        try:
            part = Decimal(str(fill.get("commission")).strip())
        except Exception:
            return None
        if not part.is_finite() or part < 0:
            return None
        if asset == base:
            fee += part
    if qty != executed:
        return None
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


def buy_entry_oco_preflight(
    *,
    order_type: str,
    limit_price: object,
    stop_price: object,
    take_profit_price: object,
    mark: object,
    mark_source: object,
    filters: list[Any],
    resting_ceiling: object = None,
    ceiling_source: object = None,
) -> tuple[dict[str, Any], str | None]:
    """Choose the pre-submit reference for a long entry. Prices are not rewritten.

    A missing or non-positive mark is ``mark_unavailable``. ``MARKET`` must
    keep the fresh mark strictly inside the tick-aligned stop and take-profit.
    A resting buy is passive only when ``resting_ceiling`` is a fresh
    executable ask strictly above the tick-aligned limit. The last price is
    not that proof. A missing ask is ``ask_unavailable``. The order type
    string alone is not proof, and ``price_usd`` is not read here.
    """
    kind = str(order_type or "").strip().upper()
    source = None if mark_source in (None, "") else str(mark_source)
    diag: dict[str, Any] = {
        "order_type": kind or None,
        "reference": None,
        "reference_price": None,
        "mark": None,
        "mark_source": source,
        "ceiling_price": None,
        "ceiling_source": None,
        "aligned_stop": None,
        "aligned_take_profit": None,
        "aligned_limit": None,
        "planned_limit": None,
        "mark_inside_band": None,
    }
    mark_n = _to_decimal(mark)
    if mark_n is None or mark_n <= 0:
        return diag, "mark_unavailable"
    diag["mark"] = _decimal_text(mark_n)
    aligned, align_error = aligned_oco_prices(
        stop_price=str(stop_price or ""),
        take_profit_price=str(take_profit_price or ""),
        filters=filters,
        mark=None,
    )
    if align_error or aligned is None:
        return diag, align_error or "oco_prices_crossed"
    diag["aligned_stop"] = _decimal_text(aligned["below_stop"])
    diag["aligned_take_profit"] = _decimal_text(aligned["above"])
    diag["mark_inside_band"] = aligned["below_stop"] < mark_n < aligned["above"]
    if kind == "MARKET":
        diag["reference"] = "live_mark"
        diag["reference_price"] = diag["mark"]
        diag["ceiling_price"] = diag["mark"]
        diag["ceiling_source"] = "live_mark"
        if diag["mark_inside_band"] is not True:
            return diag, "oco_price_restriction"
        return diag, None
    if kind in {"LIMIT", "LIMIT_MAKER"}:
        ceiling_n = _to_decimal(resting_ceiling)
        if ceiling_n is None or ceiling_n <= 0:
            return diag, "ask_unavailable"
        ceiling_label = (
            str(ceiling_source).strip() if ceiling_source not in (None, "") else "best_ask"
        )
    else:
        return diag, "invalid_order_type"
    limit_n = _to_decimal(limit_price)
    if limit_n is None or limit_n <= 0:
        return diag, "missing_limit_price"
    diag["planned_limit"] = _decimal_text(limit_n)
    tick_n = _to_decimal(_tick_size(filters))
    if tick_n is None or tick_n <= 0:
        return diag, "oco_missing_tick"
    aligned_limit = _align(limit_n, tick_n, ROUND_DOWN)
    if aligned_limit <= 0:
        return diag, "invalid_limit_price"
    diag["aligned_limit"] = _decimal_text(aligned_limit)
    diag["ceiling_price"] = _decimal_text(ceiling_n)
    diag["ceiling_source"] = ceiling_label
    diag["reference"] = "planned_entry"
    diag["reference_price"] = diag["aligned_limit"]
    if aligned_limit >= ceiling_n:
        return diag, "limit_would_cross"
    if not (aligned["below_stop"] < aligned_limit < aligned["above"]):
        return diag, "oco_price_restriction"
    return diag, None


def reanchor_oco_prices_to_fill(
    *,
    signal_entry_price: object,
    stop_price: str,
    take_profit_price: str,
    fill_price: object,
    mark: object,
    filters: list[Any],
) -> tuple[dict[str, str] | None, str | None]:
    """Re-anchor OCO stop (and TP if crossed) to the actual fill/mark price.

    Preserves the signal's relative stop distance (floored at 50 bps) below
    ``min(fill_price, mark)`` so entry slippage does not leave a filled BUY
    unprotectable due to ``oco_price_restriction``.
    """
    tick = _tick_size(filters)
    if tick is None:
        return None, "oco_missing_tick"
    tick_n = _to_decimal(tick)
    stop_n = _to_decimal(stop_price)
    tp_n = _to_decimal(take_profit_price)
    sig_entry_n = _to_decimal(signal_entry_price)
    fill_n = _to_decimal(fill_price)
    mark_n = _to_decimal(mark)
    if tick_n is None or stop_n is None or tp_n is None:
        return None, "oco_prices_crossed"
    anchors = [p for p in (fill_n, mark_n) if p is not None and p > 0]
    if not anchors:
        return None, "mark_unavailable"
    ref_low = min(anchors)
    ref_high = max(anchors)

    if sig_entry_n is not None and sig_entry_n > stop_n > 0:
        orig_sl_gap = (sig_entry_n - stop_n) / sig_entry_n
    else:
        orig_sl_gap = MIN_REANCHOR_STOP_GAP
    sl_gap = max(orig_sl_gap, MIN_REANCHOR_STOP_GAP)

    new_stop = _align(ref_low * (Decimal("1") - sl_gap), tick_n, ROUND_DOWN)
    if new_stop >= ref_low:
        new_stop = ref_low - tick_n * Decimal("2")
    if new_stop <= 0:
        return None, "oco_prices_crossed"

    new_tp = _align(tp_n, tick_n, ROUND_DOWN)
    if new_tp <= ref_high:
        if sig_entry_n is not None and tp_n > sig_entry_n > 0:
            orig_tp_gap = (tp_n - sig_entry_n) / sig_entry_n
        else:
            orig_tp_gap = Decimal("0.01")
        tp_gap = max(orig_tp_gap, MIN_REANCHOR_STOP_GAP)
        new_tp = _align(ref_high * (Decimal("1") + tp_gap), tick_n, ROUND_DOWN)
        if new_tp <= ref_high:
            new_tp = ref_high + tick_n * Decimal("2")

    new_stop_str = _decimal_text(new_stop)
    new_tp_str = _decimal_text(new_tp)
    for candidate in anchors:
        err = protective_band_error(
            stop_price=new_stop_str,
            take_profit_price=new_tp_str,
            mark=candidate,
            filters=filters,
        )
        if err is not None:
            return None, err
    return {
        "stop_price": new_stop_str,
        "take_profit_price": new_tp_str,
    }, None


def fill_average_price(placed: dict[str, Any] | None) -> str | None:
    """Average fill from quote spent and base filled. A zero qty is not a price."""
    if not isinstance(placed, dict):
        return None
    quote = _to_decimal(placed.get("cummulativeQuoteQty"))
    qty = _to_decimal(placed.get("executedQty"))
    if quote is None or qty is None or qty <= 0:
        return None
    return _decimal_text(quote / qty)


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
    ratchet_seq: int = 0,
) -> tuple[dict[str, Any] | None, str | None]:
    """Form body for ``POST /api/v3/orderList/oco``. No network.

    Returns ``(spec, error)``. ``spec["pairs"]`` is the unsigned form.
    """
    list_id = oco_list_client_id(decision_id, ratchet_seq=ratchet_seq)
    above_id = oco_above_client_id(decision_id, ratchet_seq=ratchet_seq)
    below_id = oco_below_client_id(decision_id, ratchet_seq=ratchet_seq)
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
        "order_reports": _reports(payload),
    }
    executed = Decimal("0")
    filled_reason: str | None = None
    any_open = False
    order_qty: Decimal | None = None
    for row in _reports(payload):
        status = str(row.get("status") or "").upper()
        got = _to_decimal(row.get("executedQty")) or Decimal("0")
        executed += got
        orig = _to_decimal(row.get("origQty") or row.get("origQuantity"))
        if orig is not None:
            order_qty = orig if order_qty is None else max(order_qty, orig)
        if got > 0:
            leg = str(row.get("type") or "").upper()
            if leg in {"LIMIT_MAKER", "TAKE_PROFIT_LIMIT", "TAKE_PROFIT"}:
                filled_reason = "take_profit"
            elif leg in {"STOP_LOSS_LIMIT", "STOP_LOSS"}:
                filled_reason = "stop_loss"
        if status not in _TERMINAL_LEG and status not in {"FILLED", "PARTIALLY_FILLED"}:
            any_open = True
    held = _to_decimal(held_qty) or Decimal("0")
    basis = order_qty if order_qty is not None else held
    base["executed_qty"] = _decimal_text(executed) if executed > 0 else "0"
    if oco_is_working(payload):
        base["state"] = "partial_working" if executed > 0 else "working"
        if executed > 0 and basis > executed:
            base["remainder_qty"] = _decimal_text(basis - executed)
        return base
    if executed > 0 and (filled_reason or not any_open):
        if basis > executed:
            base["state"] = "partial"
            base["remainder_qty"] = _decimal_text(basis - executed)
            base["exit_reason"] = filled_reason or "partial_fill"
            return base
        base["state"] = "filled"
        base["exit_reason"] = filled_reason or "partial_fill"
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
    sized = dict(extras)
    if order_type in {"LIMIT", "LIMIT_MAKER"}:
        price = _to_decimal(extras.get("limit_price"))
        if price is None:
            price = _to_decimal(extras.get("price"))
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


ORDERBOOK_GATE_ENV = "BINANCE_SPOT_ORDERBOOK_GATE"
MAX_SPREAD_BPS_ENV = "BINANCE_SPOT_MAX_SPREAD_BPS"
MAX_IMPACT_BPS_ENV = "BINANCE_SPOT_MAX_IMPACT_BPS"
DEFAULT_MAX_SPREAD_BPS = Decimal("35")
DEFAULT_MAX_IMPACT_BPS = Decimal("50")


def orderbook_gate_enabled() -> bool:
    raw = os.environ.get(ORDERBOOK_GATE_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


def max_spread_bps() -> Decimal:
    val = _to_decimal(os.environ.get(MAX_SPREAD_BPS_ENV))
    if val is None or val <= 0:
        return DEFAULT_MAX_SPREAD_BPS
    return val


def max_impact_bps() -> Decimal:
    val = _to_decimal(os.environ.get(MAX_IMPACT_BPS_ENV))
    if val is None or val <= 0:
        return DEFAULT_MAX_IMPACT_BPS
    return val


def _parse_book_levels(raw: object) -> list[tuple[Decimal, Decimal]]:
    if not isinstance(raw, list):
        return []
    out: list[tuple[Decimal, Decimal]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        px = _to_decimal(item[0])
        qty = _to_decimal(item[1])
        if px is not None and qty is not None and px > 0 and qty > 0:
            out.append((px, qty))
    return out


def evaluate_orderbook_impact(
    depth: object,
    *,
    quote_usdt: object = None,
    quantity: object = None,
    spread_cap_bps: Decimal | None = None,
    impact_cap_bps: Decimal | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Evaluate L2 orderbook spread, top-5 imbalance (OBI), and market-buy VWAP impact.

    Returns ``(metrics, error)``. When ``error`` is non-None, a market BUY
    should be refused before ``new_order`` is sent.
    """
    if not isinstance(depth, dict):
        return None, "orderbook_unavailable"
    bids = sorted(_parse_book_levels(depth.get("bids")), key=lambda x: x[0], reverse=True)
    asks = sorted(_parse_book_levels(depth.get("asks")), key=lambda x: x[0])
    if not bids or not asks:
        return None, "orderbook_empty"
    best_bid = bids[0][0]
    best_ask = asks[0][0]
    if best_ask <= best_bid:
        return None, "orderbook_crossed"
    mid = (best_bid + best_ask) / Decimal("2")
    spread_bps = ((best_ask - best_bid) / mid) * Decimal("10000")

    bid_5 = sum((px * qty for px, qty in bids[:5]), Decimal("0"))
    ask_5 = sum((px * qty for px, qty in asks[:5]), Decimal("0"))
    total_5 = bid_5 + ask_5
    obi_5 = float((bid_5 - ask_5) / total_5) if total_5 > 0 else 0.0

    s_cap = max_spread_bps() if spread_cap_bps is None else spread_cap_bps
    i_cap = max_impact_bps() if impact_cap_bps is None else impact_cap_bps

    target_quote = _to_decimal(quote_usdt)
    target_qty = _to_decimal(quantity)

    vwap = best_ask
    impact_bps = ((best_ask - mid) / mid) * Decimal("10000")
    depth_covered = True

    if target_quote is not None and target_quote > 0:
        rem_quote = target_quote
        acc_qty = Decimal("0")
        acc_quote = Decimal("0")
        for px, qty in asks:
            lvl_quote = px * qty
            if lvl_quote >= rem_quote:
                take_qty = rem_quote / px
                acc_qty += take_qty
                acc_quote += rem_quote
                rem_quote = Decimal("0")
                break
            acc_qty += qty
            acc_quote += lvl_quote
            rem_quote -= lvl_quote
        if rem_quote > Decimal("0.00000001") or acc_qty <= 0:
            depth_covered = False
        else:
            vwap = acc_quote / acc_qty
            impact_bps = ((vwap - mid) / mid) * Decimal("10000")
    elif target_qty is not None and target_qty > 0:
        rem_qty = target_qty
        acc_qty = Decimal("0")
        acc_quote = Decimal("0")
        for px, qty in asks:
            if qty >= rem_qty:
                acc_qty += rem_qty
                acc_quote += rem_qty * px
                rem_qty = Decimal("0")
                break
            acc_qty += qty
            acc_quote += qty * px
            rem_qty -= qty
        if rem_qty > Decimal("0.00000001") or acc_qty <= 0:
            depth_covered = False
        else:
            vwap = acc_quote / acc_qty
            impact_bps = ((vwap - mid) / mid) * Decimal("10000")

    metrics: dict[str, Any] = {
        "best_bid": _decimal_text(best_bid),
        "best_ask": _decimal_text(best_ask),
        "mid_price": _decimal_text(mid),
        "spread_bps": round(float(spread_bps), 4),
        "expected_vwap": _decimal_text(vwap),
        "impact_bps": round(float(impact_bps), 4),
        "obi_top5": round(obi_5, 4),
        "depth_covered": depth_covered,
    }
    if not depth_covered:
        return metrics, "orderbook_depth_insufficient"
    if spread_bps > s_cap:
        return metrics, "orderbook_spread_too_wide"
    if impact_bps > i_cap:
        return metrics, "orderbook_impact_too_high"
    return metrics, None


POST_ONLY_LIMIT_ENV = "BINANCE_SPOT_POST_ONLY_LIMIT"
SOR_MAKER_FALLBACK_ENV = "BINANCE_SPOT_SOR_MAKER_FALLBACK"
ENTRY_TTL_SEC_ENV = "BINANCE_SPOT_ENTRY_TTL_SEC"
DEFAULT_ENTRY_TTL_SEC = 180


def post_only_limit_enabled() -> bool:
    raw = os.environ.get(POST_ONLY_LIMIT_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


def sor_maker_fallback_enabled() -> bool:
    raw = os.environ.get(SOR_MAKER_FALLBACK_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


def entry_ttl_sec() -> int:
    raw = os.environ.get(ENTRY_TTL_SEC_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_ENTRY_TTL_SEC
    try:
        val = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return DEFAULT_ENTRY_TTL_SEC
    return max(5, val)


def peg_maker_buy_price(
    limit_price: object,
    *,
    best_bid: object = None,
    best_ask: object = None,
    mark: object = None,
    stop_price: object = None,
    take_profit_price: object = None,
    filters: list[Any],
) -> tuple[str | None, bool, str | None]:
    """Clamp a BUY ``LIMIT_MAKER`` price below ``best_ask`` so it rests as Maker.

    Returns ``(pegged_price_str, was_clamped, error)``.
    """
    tick = _tick_size(filters)
    if tick is None:
        return None, False, "oco_missing_tick"
    tick_n = _to_decimal(tick)
    limit_n = _to_decimal(limit_price)
    if tick_n is None or limit_n is None:
        return None, False, "missing_limit_price"
    aligned = _align(limit_n, tick_n, ROUND_DOWN)
    if aligned <= 0:
        return None, False, "invalid_limit_price"

    bid_n = _to_decimal(best_bid)
    ask_n = _to_decimal(best_ask)
    if bid_n is None or ask_n is None:
        return None, False, "maker_book_unavailable"
    if ask_n <= bid_n:
        return None, False, "orderbook_crossed"
    ceiling = ask_n
    del mark

    was_clamped = False
    chosen = aligned
    if chosen >= ceiling:
        if bid_n is not None and bid_n < ceiling:
            candidate = _align(bid_n, tick_n, ROUND_DOWN)
        else:
            candidate = _align(ceiling - tick_n, tick_n, ROUND_DOWN)
        if candidate <= 0:
            return None, False, "maker_peg_below_zero"
        chosen = candidate
        was_clamped = True

    stop_n = _to_decimal(stop_price)
    tp_n = _to_decimal(take_profit_price)
    if stop_n is not None and chosen <= stop_n:
        return None, was_clamped, "maker_peg_crossed_stop"
    if tp_n is not None and chosen >= tp_n:
        return None, was_clamped, "maker_peg_crossed_tp"
    return _decimal_text(chosen), was_clamped, None


def sor_maker_fallback_spec(
    *,
    quote_usdt: object,
    best_bid: object,
    best_ask: object,
    stop_price: object,
    take_profit_price: object,
    filters: list[Any],
) -> tuple[dict[str, str] | None, str | None]:
    """Convert a blocked ``MARKET`` BUY into a passive ``LIMIT_MAKER`` order at ``best_bid``."""
    q_n = _to_decimal(quote_usdt)
    bid_n = _to_decimal(best_bid)
    if q_n is None or bid_n is None:
        return None, "sor_maker_missing_quote_or_bid"
    pegged_px, _clamped, peg_err = peg_maker_buy_price(
        bid_n,
        best_bid=best_bid,
        best_ask=best_ask,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        filters=filters,
    )
    if peg_err or pegged_px is None:
        return None, peg_err or "sor_maker_peg_failed"
    px_n = _to_decimal(pegged_px)
    lot, step = _limit_lot(filters)
    if px_n is None or lot is None or step is None:
        return None, "oco_missing_lot_filter"
    fee = taker_fee_rate() or Decimal("0")
    raw_qty = q_n / px_n
    floored_qty = floor_base_qty(_decimal_text(raw_qty), step)
    floored_n = _to_decimal(floored_qty) if floored_qty is not None else None
    min_qty = _to_decimal(lot.get("minQty"))
    if floored_qty is None or floored_n is None or (min_qty is not None and floored_n < min_qty):
        return None, "entry_not_viable:below_min_qty"
    net_n = floored_n * (Decimal("1") - fee)
    net_floored = floor_base_qty(_decimal_text(net_n), step) if net_n > 0 else None
    if net_floored is None or (min_qty is not None and (_to_decimal(net_floored) or Decimal("0")) < min_qty):
        return None, "entry_not_viable:below_min_qty"
    notion = floored_n * px_n
    min_notional = _min_notional(filters)
    if notion < ENTRY_MIN_NOTIONAL_USDT:
        return None, "entry_notional_below_min"
    if min_notional is not None and notion < min_notional:
        return None, "entry_not_viable:below_min_notional"
    return {
        "order_type": "LIMIT_MAKER",
        "price": pegged_px,
        "quantity": floored_qty,
    }, None


TRAILING_RATCHET_ENV = "BINANCE_SPOT_TRAILING_RATCHET"
RATCHET_TRIGGER_PCT_ENV = "BINANCE_SPOT_RATCHET_TRIGGER_PCT"
BREAKEVEN_BUFFER_BPS_ENV = "BINANCE_SPOT_BREAKEVEN_BUFFER_BPS"
TRAILING_DIST_PCT_ENV = "BINANCE_SPOT_TRAILING_DIST_PCT"
MAX_HOLD_SEC_ENV = "BINANCE_SPOT_MAX_HOLD_SEC"
DEFAULT_RATCHET_TRIGGER_PCT = Decimal("0.60")
DEFAULT_BREAKEVEN_BUFFER_BPS = Decimal("25")
DEFAULT_TRAILING_DIST_PCT = Decimal("0.015")
PROTECTION_TIME_STOP = "time_stop_exit"


def trailing_ratchet_enabled() -> bool:
    raw = os.environ.get(TRAILING_RATCHET_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


def max_hold_sec() -> int:
    raw = os.environ.get(MAX_HOLD_SEC_ENV)
    if raw is None or not str(raw).strip():
        return 0
    try:
        val = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return 0
    return max(0, val)


def compute_ratcheted_stop_price(
    row: dict[str, Any],
    *,
    mark: object,
    filters: list[Any],
    trigger_pct: Decimal | None = None,
    breakeven_buffer_bps: Decimal | None = None,
    trail_dist_pct: Decimal | None = None,
) -> tuple[str | None, str | None]:
    """Compute a ratcheted stop-loss price when mark progresses toward take-profit.

    - Triggers only when unrealized progress ``(mark - entry) / (tp - entry) >= trigger_pct``
      (default 0.60).
    - Sets candidate stop to ``max(entry * (1 + breakeven_buffer), mark * (1 - trail_dist))``
      floored to ``tickSize``.
    - Requires the new stop to improve on the current ``stop_price`` by at least 2 ticks
      and to pass ``protective_band_error`` against ``mark``.
    Returns ``(new_stop_price_str, ratchet_reason)`` or ``(None, None)``.
    """
    tick = _tick_size(filters)
    if tick is None:
        return None, None
    tick_n = _to_decimal(tick)
    entry = _to_decimal(row.get("entry_price") or row.get("signal_entry_price"))
    cur_stop = _to_decimal(row.get("stop_price"))
    tp = _to_decimal(row.get("take_profit_price"))
    mark_n = _to_decimal(mark)
    if (
        tick_n is None
        or entry is None
        or cur_stop is None
        or tp is None
        or mark_n is None
    ):
        return None, None
    if tp <= entry or mark_n <= entry or mark_n >= tp:
        return None, None

    progress = (mark_n - entry) / (tp - entry)
    trig = (
        trigger_pct
        if trigger_pct is not None
        else (_to_decimal(os.environ.get(RATCHET_TRIGGER_PCT_ENV)) or DEFAULT_RATCHET_TRIGGER_PCT)
    )
    if progress < trig:
        return None, None

    be_bps = (
        breakeven_buffer_bps
        if breakeven_buffer_bps is not None
        else (_to_decimal(os.environ.get(BREAKEVEN_BUFFER_BPS_ENV)) or DEFAULT_BREAKEVEN_BUFFER_BPS)
    )
    trail_pct = (
        trail_dist_pct
        if trail_dist_pct is not None
        else (_to_decimal(os.environ.get(TRAILING_DIST_PCT_ENV)) or DEFAULT_TRAILING_DIST_PCT)
    )

    be_stop = entry * (Decimal("1") + be_bps / Decimal("10000"))
    trail_stop = mark_n * (Decimal("1") - trail_pct)
    if trail_stop > be_stop:
        raw_target = trail_stop
        reason = "trailing_profit_ratchet"
    else:
        raw_target = be_stop
        reason = "breakeven_ratchet"

    aligned_stop = _align(raw_target, tick_n, ROUND_DOWN)
    max_allowed = _align(mark_n - tick_n * Decimal("2"), tick_n, ROUND_DOWN)
    if aligned_stop > max_allowed:
        aligned_stop = max_allowed
    if aligned_stop < cur_stop + tick_n * Decimal("2"):
        return None, None

    stop_str = _decimal_text(aligned_stop)
    tp_str = _decimal_text(tp)
    if protective_band_error(
        stop_price=stop_str,
        take_profit_price=tp_str,
        mark=mark_n,
        filters=filters,
    ) is not None:
        return None, None
    return stop_str, reason
