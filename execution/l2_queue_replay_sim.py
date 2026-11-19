"""Event-Driven L2 100ms Depth + ``aggTrade`` Queue-Priority Replay Simulator (Step 3).

Eliminates optimistic K-line touch backtest illusions (``Low <= LimitPrice => Fill``)
by replaying recorded Binance WebSocket frames (``@depth20@100ms``, ``@bookTicker``,
and ``@aggTrade`` / ``@trade``) with realistic matching-engine microstructure:

1. **Gateway Network Latency (``gateway_latency_ms``)**:
   Orders submitted at ``t_submit`` reach the matching engine at
   ``t_arrive = t_submit + gateway_latency_ms`` and match against the order book
   active at ``t_arrive``—not the stale book observed at signal time.
2. **Post-Only Crossing Rejection (``LIMIT_MAKER`` -> ``EXPIRED_IN_MATCH``)**:
   A ``BUY`` ``LIMIT_MAKER`` arriving when ``limit_price >= best_ask`` (or ``SELL``
   when ``limit_price <= best_bid``) is immediately rejected with
   ``EXPIRED_IN_MATCH``, matching live Binance Spot behavior.
3. **Conservative FIFO Queue Ahead-Quantity (``queue_ahead_qty``)**:
   - Resting a passive order behind existing L2 depth sets ``queue_ahead_qty`` to
     the full visible base liquidity at ``limit_price`` (or ``0`` when pennying
     strictly inside the spread via ``WallMid``).
   - Passive fills at ``limit_price`` occur ONLY after cumulative contra-side taker
     trades at ``limit_price`` drain ``queue_ahead_qty`` (``contra_vol > queue_ahead_qty``),
     or when trades/asks sweep strictly through ``limit_price``.
   - Cancellations on L2 depth updates conservatively assume orders *behind* us
     cancel first; ``queue_ahead_qty`` shrinks only when total visible depth falls
     below ``queue_ahead_qty``.
4. **Integrated 5s / 60s Markout Adverse-Selection Diagnostics**:
   Tracks post-fill mid-price at +5s and +60s (and optionally persists to
   ``MarkoutTcaStore``) alongside a naive K-line touch shadow flag so strategy
   authors directly measure how many "touch-without-penetration" false fills
   were filtered out.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import json
from typing import Any

from decision_engine.core.wall_mid_pricing import WallMidQuote, compute_wall_mid_from_book
from execution.binance_ws_stream import L1BookTick, L2DepthSnapshot, parse_ws_message
from execution.markout_tca import HORIZON_5S_MS, HORIZON_60S_MS, MarkoutTcaStore, compute_markout_bps


def _pos_dec(raw: object) -> Decimal | None:
    if isinstance(raw, bool) or raw is None:
        return None
    txt = str(raw).strip()
    if not txt:
        return None
    try:
        val = Decimal(txt)
    except InvalidOperation:
        return None
    if not val.is_finite() or val <= 0:
        return None
    return val


def _dec_str(val: Decimal) -> str:
    txt = format(val, "f")
    if "." in txt:
        txt = txt.rstrip("0").rstrip(".")
    return txt or "0"


@dataclass(frozen=True)
class AggTradeTick:
    symbol: str
    trade_id: int
    price: Decimal
    quantity: Decimal
    is_buyer_maker: bool
    event_time_ms: int


def parse_agg_trade_message(raw: str | bytes | Mapping[str, Any]) -> AggTradeTick | None:
    """Parse a Binance ``@aggTrade`` or ``@trade`` WebSocket frame."""
    if isinstance(raw, (str, bytes)):
        try:
            outer = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
    elif isinstance(raw, Mapping):
        outer = raw
    else:
        return None
    if not isinstance(outer, Mapping):
        return None
    data = outer.get("data") if isinstance(outer.get("data"), Mapping) else outer
    if not isinstance(data, Mapping):
        return None
    etype = str(data.get("e") or "").strip()
    stream_name = str(outer.get("stream") or "").strip()
    if etype not in {"aggTrade", "trade"} and "@aggTrade" not in stream_name and "@trade" not in stream_name:
        return None
    sym = str(data.get("s") or "").strip().upper()
    if not sym and "@" in stream_name:
        sym = stream_name.partition("@")[0].strip().upper()
    px = _pos_dec(data.get("p"))
    qty = _pos_dec(data.get("q"))
    ibm = data.get("m")
    ev_ms = data.get("T") or data.get("E")
    tid_raw = data.get("a", data.get("t", 0))
    if (
        not sym
        or px is None
        or qty is None
        or not isinstance(ibm, bool)
        or isinstance(ev_ms, bool)
        or not isinstance(ev_ms, int)
        or ev_ms <= 0
    ):
        return None
    tid = int(tid_raw) if isinstance(tid_raw, int) and not isinstance(tid_raw, bool) else 0
    return AggTradeTick(
        symbol=sym,
        trade_id=tid,
        price=px,
        quantity=qty,
        is_buyer_maker=ibm,
        event_time_ms=ev_ms,
    )


@dataclass
class ReplayOrder:
    order_id: str
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    limit_price: Decimal | None
    submit_ts_ms: int
    arrival_ts_ms: int
    ttl_ms: int | None = None
    status: str = "PENDING_ARRIVAL"
    initial_queue_ahead_qty: Decimal = Decimal("0")
    queue_ahead_qty: Decimal = Decimal("0")
    contra_volume_at_level: Decimal = Decimal("0")
    filled_qty: Decimal = Decimal("0")
    cum_quote_qty: Decimal = Decimal("0")
    first_fill_ts_ms: int | None = None
    last_fill_ts_ms: int | None = None
    naive_touch_would_fill: bool = False
    mid_at_fill: float | None = None
    mid_5s: float | None = None
    mid_60s: float | None = None
    markout_5s_bps: float | None = None
    markout_60s_bps: float | None = None
    reject_reason: str | None = None
    fills: list[dict[str, Any]] = field(default_factory=list)

    @property
    def remaining_qty(self) -> Decimal:
        return max(Decimal("0"), self.quantity - self.filled_qty)

    @property
    def avg_fill_price(self) -> Decimal | None:
        if self.filled_qty <= 0:
            return None
        return self.cum_quote_qty / self.filled_qty


class L2QueueReplaySimulator:
    """Deterministic single-threaded event-driven L2 + ``aggTrade`` matching engine."""

    def __init__(
        self,
        *,
        gateway_latency_ms: int = 25,
        maker_fee_bps: float = 0.0,
        taker_fee_bps: float = 10.0,
        markout_store: MarkoutTcaStore | None = None,
    ) -> None:
        self.gateway_latency_ms = max(0, int(gateway_latency_ms))
        self.maker_fee_bps = float(maker_fee_bps)
        self.taker_fee_bps = float(taker_fee_bps)
        self.markout_store = markout_store
        self._now_ms: int = 0
        self._l2_books: dict[str, L2DepthSnapshot] = {}
        self._best_bid_ask: dict[str, tuple[Decimal, Decimal]] = {}
        self._orders: dict[str, ReplayOrder] = {}
        self._order_seq = 0

    def latest_wall_mid(self, symbol: str, *, tick_size: float | None = None) -> WallMidQuote | None:
        snap = self._l2_books.get(symbol.strip().upper())
        if snap is None:
            return None
        return compute_wall_mid_from_book(snap.bids, snap.asks, tick_size=tick_size)

    def submit_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity: str | Decimal,
        limit_price: str | Decimal | None = None,
        submit_ts_ms: int | None = None,
        ttl_ms: int | None = None,
        order_id: str | None = None,
    ) -> ReplayOrder:
        clean_sym = str(symbol or "").strip().upper()
        clean_side = str(side or "").strip().upper()
        clean_type = str(order_type or "").strip().upper()
        qty = _pos_dec(quantity)
        if not clean_sym or clean_side not in {"BUY", "SELL"} or qty is None:
            raise ValueError("invalid_replay_order_spec")
        if clean_type not in {"LIMIT_MAKER", "LIMIT", "MARKET"}:
            raise ValueError(f"unsupported_replay_order_type:{clean_type}")
        lim_px = _pos_dec(limit_price) if limit_price is not None else None
        if clean_type in {"LIMIT_MAKER", "LIMIT"} and lim_px is None:
            raise ValueError("limit_price_required")

        self._order_seq += 1
        oid = str(order_id).strip() if order_id else f"sim-{self._order_seq}"
        sub_ms = self._now_ms if submit_ts_ms is None else int(submit_ts_ms)
        arr_ms = sub_ms + self.gateway_latency_ms
        order = ReplayOrder(
            order_id=oid,
            symbol=clean_sym,
            side=clean_side,
            order_type=clean_type,
            quantity=qty,
            limit_price=lim_px,
            submit_ts_ms=sub_ms,
            arrival_ts_ms=arr_ms,
            ttl_ms=int(ttl_ms) if ttl_ms is not None and int(ttl_ms) > 0 else None,
        )
        self._orders[oid] = order
        if arr_ms <= self._now_ms:
            self._activate_arrived_order(order, ts_ms=self._now_ms)
        return order

    def submit_wall_mid_maker_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: str | Decimal,
        tick_size: float | None = None,
        submit_ts_ms: int | None = None,
        ttl_ms: int | None = None,
        order_id: str | None = None,
    ) -> ReplayOrder:
        quote = self.latest_wall_mid(symbol, tick_size=tick_size)
        if quote is None:
            raise RuntimeError(f"wall_mid_book_unavailable:{symbol}")
        clean_side = str(side or "").strip().upper()
        px = quote.maker_buy_price if clean_side == "BUY" else quote.maker_sell_price
        return self.submit_order(
            symbol=symbol,
            side=clean_side,
            order_type="LIMIT_MAKER",
            quantity=quantity,
            limit_price=f"{px:.8f}",
            submit_ts_ms=submit_ts_ms,
            ttl_ms=ttl_ms,
            order_id=order_id,
        )

    def get_order(self, order_id: str) -> ReplayOrder:
        return self._orders[order_id]

    def replay_events(self, events: Iterable[str | bytes | Mapping[str, Any]]) -> dict[str, Any]:
        for raw in events:
            self.step_raw_event(raw)
        return self.summary()

    def step_raw_event(self, raw: str | bytes | Mapping[str, Any]) -> None:
        trade = parse_agg_trade_message(raw)
        if trade is not None:
            self._advance_clock(trade.event_time_ms)
            self._on_agg_trade(trade)
            return

        parsed = parse_ws_message(raw, now_mono_ms=self._now_ms, now_epoch_ms=self._now_ms)
        if isinstance(parsed, L2DepthSnapshot):
            self._advance_clock(parsed.event_time_ms)
            self._on_l2_depth(parsed)
        elif isinstance(parsed, L1BookTick):
            self._advance_clock(parsed.event_time_ms)
            self._on_l1_tick(parsed)

    def _advance_clock(self, event_ts_ms: int) -> None:
        if event_ts_ms > self._now_ms:
            self._now_ms = event_ts_ms
        # Activate any orders whose gateway latency has elapsed, and expire TTLs
        for order in list(self._orders.values()):
            if order.status == "PENDING_ARRIVAL" and order.arrival_ts_ms <= self._now_ms:
                self._activate_arrived_order(order, ts_ms=order.arrival_ts_ms)
            if (
                order.status in {"NEW", "PARTIALLY_FILLED"}
                and order.ttl_ms is not None
                and self._now_ms >= (order.arrival_ts_ms + order.ttl_ms)
            ):
                order.status = "CANCELED"
                order.reject_reason = "entry_ttl_expired"

    def _visible_same_side_depth_at_price(self, symbol: str, side: str, price: Decimal) -> Decimal:
        snap = self._l2_books.get(symbol)
        if snap is None:
            return Decimal("0")
        levels = snap.bids if side == "BUY" else snap.asks
        for lv in levels:
            px = _pos_dec(lv[0])
            qty = _pos_dec(lv[1])
            if px is not None and qty is not None and px == price:
                return qty
        return Decimal("0")

    def _current_mid(self, symbol: str) -> float | None:
        bbo = self._best_bid_ask.get(symbol)
        if bbo is None:
            return None
        return float((bbo[0] + bbo[1]) / Decimal("2"))

    def _activate_arrived_order(self, order: ReplayOrder, *, ts_ms: int) -> None:
        bbo = self._best_bid_ask.get(order.symbol)
        snap = self._l2_books.get(order.symbol)
        if bbo is None:
            order.status = "REJECTED"
            order.reject_reason = "orderbook_unavailable_at_arrival"
            return

        best_bid, best_ask = bbo
        if order.order_type == "MARKET":
            self._sweep_book_immediately(order, snap=snap, best_bid=best_bid, best_ask=best_ask, ts_ms=ts_ms)
            return

        assert order.limit_price is not None
        crosses = (
            order.limit_price >= best_ask
            if order.side == "BUY"
            else order.limit_price <= best_bid
        )
        if crosses:
            if order.order_type == "LIMIT_MAKER":
                order.status = "EXPIRED_IN_MATCH"
                order.reject_reason = "post_only_would_cross_spread"
                return
            # Standard GTC LIMIT sweeps available contra levels <= limit_price, rests remainder
            self._sweep_book_immediately(
                order,
                snap=snap,
                best_bid=best_bid,
                best_ask=best_ask,
                ts_ms=ts_ms,
                limit_cap=order.limit_price,
            )
            if order.remaining_qty <= 0:
                return

        ahead = self._visible_same_side_depth_at_price(order.symbol, order.side, order.limit_price)
        order.initial_queue_ahead_qty = ahead
        order.queue_ahead_qty = ahead
        order.status = "PARTIALLY_FILLED" if order.filled_qty > 0 else "NEW"

    def _sweep_book_immediately(
        self,
        order: ReplayOrder,
        *,
        snap: L2DepthSnapshot | None,
        best_bid: Decimal,
        best_ask: Decimal,
        ts_ms: int,
        limit_cap: Decimal | None = None,
    ) -> None:
        if snap is not None:
            contra_levels = snap.asks if order.side == "BUY" else snap.bids
            for lv in contra_levels:
                if order.remaining_qty <= 0:
                    break
                px = _pos_dec(lv[0])
                avail = _pos_dec(lv[1])
                if px is None or avail is None:
                    continue
                if limit_cap is not None:
                    if order.side == "BUY" and px > limit_cap:
                        break
                    if order.side == "SELL" and px < limit_cap:
                        break
                take = min(order.remaining_qty, avail)
                self._record_fill_slice(order, fill_px=px, fill_qty=take, ts_ms=ts_ms)
        else:
            px = best_ask if order.side == "BUY" else best_bid
            self._record_fill_slice(order, fill_px=px, fill_qty=order.remaining_qty, ts_ms=ts_ms)

        if order.remaining_qty <= 0:
            order.status = "FILLED"
        elif order.order_type == "MARKET":
            order.status = "EXPIRED"
            order.reject_reason = "insufficient_l2_depth"

    def _record_fill_slice(
        self,
        order: ReplayOrder,
        *,
        fill_px: Decimal,
        fill_qty: Decimal,
        ts_ms: int,
    ) -> None:
        if fill_qty <= 0:
            return
        mid_now = self._current_mid(order.symbol)
        order.filled_qty += fill_qty
        order.cum_quote_qty += fill_qty * fill_px
        if order.first_fill_ts_ms is None:
            order.first_fill_ts_ms = ts_ms
            order.mid_at_fill = mid_now
        order.last_fill_ts_ms = ts_ms
        order.fills.append(
            {
                "price": _dec_str(fill_px),
                "qty": _dec_str(fill_qty),
                "ts_ms": ts_ms,
            }
        )
        if order.remaining_qty <= 0:
            order.status = "FILLED"
            avg_px = order.avg_fill_price
            if self.markout_store is not None and avg_px is not None:
                self.markout_store.record_fill(
                    symbol=order.symbol,
                    order_id=order.order_id,
                    side=order.side,
                    order_type=order.order_type,
                    fill_price=float(avg_px),
                    fill_qty=float(order.filled_qty),
                    fill_ts_ms=ts_ms,
                    mid_at_fill=order.mid_at_fill,
                )
        else:
            order.status = "PARTIALLY_FILLED"

    def _on_l1_tick(self, tick: L1BookTick) -> None:
        bid = _pos_dec(tick.best_bid)
        ask = _pos_dec(tick.best_ask)
        if bid is None or ask is None or ask < bid:
            return
        self._best_bid_ask[tick.symbol] = (bid, ask)
        mid = float((bid + ask) / Decimal("2"))
        self._update_markouts(tick.symbol, mid, tick.event_time_ms)
        self._check_book_sweep(tick.symbol, best_bid=bid, best_ask=ask, ts_ms=tick.event_time_ms)

    def _on_l2_depth(self, snap: L2DepthSnapshot) -> None:
        self._l2_books[snap.symbol] = snap
        bid = _pos_dec(snap.bids[0][0]) if snap.bids else None
        ask = _pos_dec(snap.asks[0][0]) if snap.asks else None
        if bid is None or ask is None or ask < bid:
            return
        self._best_bid_ask[snap.symbol] = (bid, ask)
        mid = float((bid + ask) / Decimal("2"))
        self._update_markouts(snap.symbol, mid, snap.event_time_ms)

        # Conservative queue shrinkage: cancellations remove back-of-queue first
        for order in self._orders.values():
            if order.symbol != snap.symbol or order.status not in {"NEW", "PARTIALLY_FILLED"}:
                continue
            if order.limit_price is None:
                continue
            visible = self._visible_same_side_depth_at_price(
                snap.symbol, order.side, order.limit_price
            )
            # Only if the entire visible level shrank below our queue_ahead_qty can our queue_ahead shrink
            if visible < order.queue_ahead_qty:
                order.queue_ahead_qty = visible

        self._check_book_sweep(snap.symbol, best_bid=bid, best_ask=ask, ts_ms=snap.event_time_ms)

    def _check_book_sweep(
        self,
        symbol: str,
        *,
        best_bid: Decimal,
        best_ask: Decimal,
        ts_ms: int,
    ) -> None:
        """If the contra quote moved strictly past our resting limit, the entire level cleared."""
        for order in self._orders.values():
            if order.symbol != symbol or order.status not in {"NEW", "PARTIALLY_FILLED"}:
                continue
            if order.limit_price is None:
                continue
            if order.side == "BUY" and best_ask <= order.limit_price:
                order.naive_touch_would_fill = True
                if best_ask < order.limit_price:
                    order.queue_ahead_qty = Decimal("0")
                    self._record_fill_slice(
                        order,
                        fill_px=order.limit_price,
                        fill_qty=order.remaining_qty,
                        ts_ms=ts_ms,
                    )
            elif order.side == "SELL" and best_bid >= order.limit_price:
                order.naive_touch_would_fill = True
                if best_bid > order.limit_price:
                    order.queue_ahead_qty = Decimal("0")
                    self._record_fill_slice(
                        order,
                        fill_px=order.limit_price,
                        fill_qty=order.remaining_qty,
                        ts_ms=ts_ms,
                    )

    def _on_agg_trade(self, trade: AggTradeTick) -> None:
        for order in self._orders.values():
            if order.symbol != trade.symbol or order.status not in {"NEW", "PARTIALLY_FILLED"}:
                continue
            if order.limit_price is None:
                continue

            if order.side == "BUY":
                if trade.price <= order.limit_price:
                    order.naive_touch_would_fill = True
                # Only seller-initiated trades (`is_buyer_maker=True`) hit resting bids!
                if not trade.is_buyer_maker:
                    continue
                if trade.price < order.limit_price:
                    # Trade-through strictly below our bid guarantees full queue clearance
                    order.queue_ahead_qty = Decimal("0")
                    self._record_fill_slice(
                        order,
                        fill_px=order.limit_price,
                        fill_qty=order.remaining_qty,
                        ts_ms=trade.event_time_ms,
                    )
                elif trade.price == order.limit_price:
                    order.contra_volume_at_level += trade.quantity
                    if trade.quantity <= order.queue_ahead_qty:
                        order.queue_ahead_qty -= trade.quantity
                    else:
                        penetrated = trade.quantity - order.queue_ahead_qty
                        order.queue_ahead_qty = Decimal("0")
                        fill_slice = min(order.remaining_qty, penetrated)
                        self._record_fill_slice(
                            order,
                            fill_px=order.limit_price,
                            fill_qty=fill_slice,
                            ts_ms=trade.event_time_ms,
                        )

            elif order.side == "SELL":
                if trade.price >= order.limit_price:
                    order.naive_touch_would_fill = True
                # Only buyer-initiated trades (`is_buyer_maker=False`) lift resting asks!
                if trade.is_buyer_maker:
                    continue
                if trade.price > order.limit_price:
                    order.queue_ahead_qty = Decimal("0")
                    self._record_fill_slice(
                        order,
                        fill_px=order.limit_price,
                        fill_qty=order.remaining_qty,
                        ts_ms=trade.event_time_ms,
                    )
                elif trade.price == order.limit_price:
                    order.contra_volume_at_level += trade.quantity
                    if trade.quantity <= order.queue_ahead_qty:
                        order.queue_ahead_qty -= trade.quantity
                    else:
                        penetrated = trade.quantity - order.queue_ahead_qty
                        order.queue_ahead_qty = Decimal("0")
                        fill_slice = min(order.remaining_qty, penetrated)
                        self._record_fill_slice(
                            order,
                            fill_px=order.limit_price,
                            fill_qty=fill_slice,
                            ts_ms=trade.event_time_ms,
                        )

    def _update_markouts(self, symbol: str, mid_price: float, ts_ms: int) -> None:
        if self.markout_store is not None:
            self.markout_store.observe_mid_price(symbol=symbol, mid_price=mid_price, ts_ms=ts_ms)
        for order in self._orders.values():
            if order.symbol != symbol or order.last_fill_ts_ms is None:
                continue
            avg_px = order.avg_fill_price
            if avg_px is None:
                continue
            elapsed = ts_ms - order.last_fill_ts_ms
            if order.mid_5s is None and elapsed >= HORIZON_5S_MS:
                order.mid_5s = mid_price
                order.markout_5s_bps = compute_markout_bps(
                    side=order.side, fill_price=float(avg_px), future_mid_price=mid_price
                )
            if order.mid_60s is None and elapsed >= HORIZON_60S_MS:
                order.mid_60s = mid_price
                order.markout_60s_bps = compute_markout_bps(
                    side=order.side, fill_price=float(avg_px), future_mid_price=mid_price
                )

    def summary(self) -> dict[str, Any]:
        orders = list(self._orders.values())
        filled = [o for o in orders if o.status == "FILLED"]
        expired_in_match = [o for o in orders if o.status == "EXPIRED_IN_MATCH"]
        false_alpha = [
            o
            for o in orders
            if o.naive_touch_would_fill and o.filled_qty == 0
        ]
        m5 = [o.markout_5s_bps for o in filled if o.markout_5s_bps is not None]
        m60 = [o.markout_60s_bps for o in filled if o.markout_60s_bps is not None]
        return {
            "orders_total": len(orders),
            "filled_count": len(filled),
            "expired_in_match_count": len(expired_in_match),
            "naive_touch_count": sum(1 for o in orders if o.naive_touch_would_fill or o.filled_qty > 0),
            "false_alpha_filtered_count": len(false_alpha),
            "mean_markout_5s_bps": round(sum(m5) / len(m5), 3) if m5 else None,
            "mean_markout_60s_bps": round(sum(m60) / len(m60), 3) if m60 else None,
        }
