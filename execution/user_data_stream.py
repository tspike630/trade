"""Real-Time Binance Spot & Cross Margin User Data Stream Processor (Step 1 upgrade).

Eliminates blind REST polling windows between maintenance ticks by consuming push
events from Binance User Data Streams (WebSocket API ``userDataStream.subscribe`` or
``listenKey`` stream) while preserving the SQLite idempotent ledger as the durable
source of truth:

- ``executionReport`` — Real-time order lifecycle, fill price/quantity, commission,
  and ``orderListId`` leg transitions.
- ``listStatus`` — Real-time OCO list state transitions (``EXEC_STARTED`` / ``EXECUTING``
  vs ``ALL_DONE``).
- ``outboundAccountPosition`` & ``balanceUpdate`` — Real-time free/locked balance
  snapshots with monotonic ``u`` (last account update) ordering.

Safety invariants:
1. Default OFF via ``BINANCE_USER_DATA_STREAM_ENABLED`` (``0`` by default).
2. Strict out-of-order replay rejection using per-order ``(transaction_time_ms, event_time_ms)``
   watermarks and account ``last_update_ms`` watermarks.
3. Explicit ``side == "SELL"`` verification on OCO claims—a ``BUY`` OCO or mismatched
   symbol event never clears ``unprotected_qty`` or marks a position protected.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import os
import sqlite3
import threading
import time
from typing import Any

USER_DATA_STREAM_ENABLED_ENV = "BINANCE_USER_DATA_STREAM_ENABLED"
DEFAULT_MAX_STREAM_SILENCE_MS = 30_000


def user_data_stream_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    raw = str(env.get(USER_DATA_STREAM_ENABLED_ENV) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _now_mono_ms() -> int:
    return int(time.monotonic() * 1000)


def _dec_or_none(raw: object) -> Decimal | None:
    if isinstance(raw, bool) or raw is None:
        return None
    txt = str(raw).strip()
    if not txt:
        return None
    try:
        val = Decimal(txt)
    except InvalidOperation:
        return None
    if not val.is_finite():
        return None
    return val


def _dec_str(val: Decimal) -> str:
    txt = format(val, "f")
    if "." in txt:
        txt = txt.rstrip("0").rstrip(".")
    return txt or "0"


@dataclass(frozen=True)
class ExecutionReportEvent:
    symbol: str
    order_id: str
    client_order_id: str
    orig_client_order_id: str
    side: str
    order_type: str
    exec_type: str
    order_status: str
    order_list_id: int
    order_qty: Decimal
    cum_filled_qty: Decimal
    cum_quote_qty: Decimal
    last_fill_qty: Decimal
    last_fill_price: Decimal
    commission: Decimal
    commission_asset: str
    trade_id: int
    event_time_ms: int
    transaction_time_ms: int
    received_mono_ms: int


@dataclass(frozen=True)
class ListStatusOrderItem:
    symbol: str
    order_id: str
    client_order_id: str


@dataclass(frozen=True)
class ListStatusEvent:
    symbol: str
    order_list_id: str
    contingency_type: str
    list_status_type: str
    list_order_status: str
    list_client_order_id: str
    transaction_time_ms: int
    event_time_ms: int
    orders: tuple[ListStatusOrderItem, ...]
    received_mono_ms: int


@dataclass(frozen=True)
class AccountBalanceItem:
    asset: str
    free: Decimal
    locked: Decimal


@dataclass(frozen=True)
class AccountPositionEvent:
    event_time_ms: int
    last_account_update_ms: int
    balances: dict[str, AccountBalanceItem]
    received_mono_ms: int


UserDataEvent = ExecutionReportEvent | ListStatusEvent | AccountPositionEvent


def parse_user_data_event(
    raw: str | bytes | Mapping[str, Any],
    *,
    now_mono_ms: int | None = None,
) -> UserDataEvent | None:
    """Parse a Binance Spot / Margin user-data WebSocket payload into a typed event."""
    mono_ms = _now_mono_ms() if now_mono_ms is None else int(now_mono_ms)
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
    # Unwrap WebSocket API `{"event": {...}}` or combined stream `{"data": {...}}`
    data: Mapping[str, Any] = outer
    if isinstance(outer.get("event"), Mapping):
        data = outer["event"]
    elif isinstance(outer.get("data"), Mapping):
        data = outer["data"]

    etype = str(data.get("e") or "").strip()
    ev_ms_raw = data.get("E")
    if isinstance(ev_ms_raw, bool) or not isinstance(ev_ms_raw, int) or ev_ms_raw <= 0:
        return None

    if etype == "executionReport":
        sym = str(data.get("s") or "").strip().upper()
        oid_raw = data.get("i")
        if not sym or isinstance(oid_raw, bool) or not isinstance(oid_raw, int) or oid_raw <= 0:
            return None
        side = str(data.get("S") or "").strip().upper()
        otype = str(data.get("o") or "").strip().upper()
        xtype = str(data.get("x") or "").strip().upper()
        ostatus = str(data.get("X") or "").strip().upper()
        if side not in {"BUY", "SELL"} or not otype or not ostatus:
            return None
        olid_raw = data.get("g", -1)
        olid = int(olid_raw) if isinstance(olid_raw, int) and not isinstance(olid_raw, bool) else -1
        oqty = _dec_or_none(data.get("q"))
        cum_qty = _dec_or_none(data.get("z"))
        cum_quote = _dec_or_none(data.get("Z", "0"))
        last_qty = _dec_or_none(data.get("l", "0"))
        last_px = _dec_or_none(data.get("L", "0"))
        comm = _dec_or_none(data.get("n", "0"))
        if (
            oqty is None
            or oqty <= 0
            or cum_qty is None
            or cum_qty < 0
            or cum_quote is None
            or cum_quote < 0
            or last_qty is None
            or last_qty < 0
            or last_px is None
            or last_px < 0
            or comm is None
            or comm < 0
        ):
            return None
        tx_ms = data.get("T", ev_ms_raw)
        if isinstance(tx_ms, bool) or not isinstance(tx_ms, int) or tx_ms <= 0:
            tx_ms = ev_ms_raw
        tid_raw = data.get("t", -1)
        tid = int(tid_raw) if isinstance(tid_raw, int) and not isinstance(tid_raw, bool) else -1
        return ExecutionReportEvent(
            symbol=sym,
            order_id=str(oid_raw),
            client_order_id=str(data.get("c") or "").strip(),
            orig_client_order_id=str(data.get("C") or "").strip(),
            side=side,
            order_type=otype,
            exec_type=xtype,
            order_status=ostatus,
            order_list_id=olid,
            order_qty=oqty,
            cum_filled_qty=cum_qty,
            cum_quote_qty=cum_quote,
            last_fill_qty=last_qty,
            last_fill_price=last_px,
            commission=comm,
            commission_asset=str(data.get("N") or "").strip().upper(),
            trade_id=tid,
            event_time_ms=ev_ms_raw,
            transaction_time_ms=tx_ms,
            received_mono_ms=mono_ms,
        )

    if etype == "listStatus":
        sym = str(data.get("s") or "").strip().upper()
        olid_raw = data.get("g")
        if not sym or isinstance(olid_raw, bool) or not isinstance(olid_raw, int) or olid_raw < 0:
            return None
        raw_orders = data.get("O")
        if not isinstance(raw_orders, list) or not raw_orders:
            return None
        items: list[ListStatusOrderItem] = []
        for item in raw_orders:
            if not isinstance(item, Mapping):
                return None
            isym = str(item.get("s") or sym).strip().upper()
            ioid = item.get("i")
            if isinstance(ioid, bool) or not isinstance(ioid, int) or ioid <= 0:
                return None
            items.append(
                ListStatusOrderItem(
                    symbol=isym,
                    order_id=str(ioid),
                    client_order_id=str(item.get("c") or "").strip(),
                )
            )
        tx_ms = data.get("T", ev_ms_raw)
        if isinstance(tx_ms, bool) or not isinstance(tx_ms, int) or tx_ms <= 0:
            tx_ms = ev_ms_raw
        return ListStatusEvent(
            symbol=sym,
            order_list_id=str(olid_raw),
            contingency_type=str(data.get("c") or "").strip().upper(),
            list_status_type=str(data.get("l") or "").strip().upper(),
            list_order_status=str(data.get("L") or "").strip().upper(),
            list_client_order_id=str(data.get("C") or "").strip(),
            transaction_time_ms=tx_ms,
            event_time_ms=ev_ms_raw,
            orders=tuple(items),
            received_mono_ms=mono_ms,
        )

    if etype == "outboundAccountPosition":
        upd_ms = data.get("u", ev_ms_raw)
        if isinstance(upd_ms, bool) or not isinstance(upd_ms, int) or upd_ms <= 0:
            return None
        raw_bals = data.get("B")
        if not isinstance(raw_bals, list):
            return None
        balances: dict[str, AccountBalanceItem] = {}
        for row in raw_bals:
            if not isinstance(row, Mapping):
                continue
            asset = str(row.get("a") or "").strip().upper()
            free = _dec_or_none(row.get("f"))
            locked = _dec_or_none(row.get("l"))
            if not asset or free is None or free < 0 or locked is None or locked < 0:
                continue
            balances[asset] = AccountBalanceItem(asset=asset, free=free, locked=locked)
        return AccountPositionEvent(
            event_time_ms=ev_ms_raw,
            last_account_update_ms=upd_ms,
            balances=balances,
            received_mono_ms=mono_ms,
        )

    return None


class SpotUserDataStreamProcessor:
    """In-memory + SQLite-reconciling processor for Spot & Margin User Data Streams."""

    def __init__(
        self,
        *,
        ledger_path: os.PathLike[str] | str | None = None,
        max_silence_ms: int = DEFAULT_MAX_STREAM_SILENCE_MS,
        on_event: Callable[[UserDataEvent], None] | None = None,
    ) -> None:
        self.ledger_path = os.fspath(ledger_path) if ledger_path is not None else None
        self.max_silence_ms = max(1000, int(max_silence_ms))
        self._on_event = on_event
        self._lock = threading.RLock()
        self._last_received_mono_ms: int | None = None
        self._account_watermark_ms: int = 0
        self._order_watermarks: dict[tuple[str, str], tuple[int, int]] = {}
        self._list_watermarks: dict[tuple[str, str], tuple[int, int]] = {}
        self._balances: dict[str, AccountBalanceItem] = {}
        self._latest_orders: dict[tuple[str, str], ExecutionReportEvent] = {}
        self._latest_lists: dict[tuple[str, str], ListStatusEvent] = {}
        self._stats: dict[str, int] = {
            "events_accepted": 0,
            "events_stale_dropped": 0,
            "events_malformed": 0,
            "ledger_rows_updated": 0,
        }

    def is_healthy(self, *, now_mono_ms: int | None = None) -> bool:
        mono = _now_mono_ms() if now_mono_ms is None else int(now_mono_ms)
        with self._lock:
            if self._last_received_mono_ms is None:
                return False
            return max(0, mono - self._last_received_mono_ms) <= self.max_silence_ms

    def stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._stats)

    def get_balance(
        self,
        asset: str,
        *,
        now_mono_ms: int | None = None,
    ) -> AccountBalanceItem | None:
        if not self.is_healthy(now_mono_ms=now_mono_ms):
            return None
        with self._lock:
            return self._balances.get(str(asset or "").strip().upper())

    def get_order_state(self, symbol: str, order_id: str) -> ExecutionReportEvent | None:
        with self._lock:
            return self._latest_orders.get((symbol.strip().upper(), str(order_id).strip()))

    def ingest_raw(
        self,
        raw: str | bytes | Mapping[str, Any],
        *,
        now_mono_ms: int | None = None,
    ) -> dict[str, Any]:
        ev = parse_user_data_event(raw, now_mono_ms=now_mono_ms)
        if ev is None:
            with self._lock:
                self._stats["events_malformed"] += 1
            return {"accepted": False, "reason": "malformed_or_unsupported"}

        updated_rows = 0
        with self._lock:
            if isinstance(ev, AccountPositionEvent):
                if ev.last_account_update_ms < self._account_watermark_ms:
                    self._stats["events_stale_dropped"] += 1
                    return {"accepted": False, "reason": "stale_account_event"}
                self._account_watermark_ms = ev.last_account_update_ms
                self._balances.update(ev.balances)

            elif isinstance(ev, ExecutionReportEvent):
                key = (ev.symbol, ev.order_id)
                stamp = (ev.transaction_time_ms, ev.event_time_ms)
                prev = self._order_watermarks.get(key)
                if prev is not None and stamp < prev:
                    self._stats["events_stale_dropped"] += 1
                    return {"accepted": False, "reason": "stale_order_event"}
                self._order_watermarks[key] = stamp
                self._latest_orders[key] = ev
                if self.ledger_path is not None:
                    updated_rows += self._reconcile_execution_report_sqlite(ev)

            elif isinstance(ev, ListStatusEvent):
                key = (ev.symbol, ev.order_list_id)
                stamp = (ev.transaction_time_ms, ev.event_time_ms)
                prev = self._list_watermarks.get(key)
                if prev is not None and stamp < prev:
                    self._stats["events_stale_dropped"] += 1
                    return {"accepted": False, "reason": "stale_list_event"}
                self._list_watermarks[key] = stamp
                self._latest_lists[key] = ev
                if self.ledger_path is not None:
                    updated_rows += self._reconcile_list_status_sqlite(ev)

            self._last_received_mono_ms = ev.received_mono_ms
            self._stats["events_accepted"] += 1
            self._stats["ledger_rows_updated"] += updated_rows

        if self._on_event is not None:
            self._on_event(ev)

        return {
            "accepted": True,
            "event_type": type(ev).__name__,
            "ledger_rows_updated": updated_rows,
        }

    def _reconcile_execution_report_sqlite(self, ev: ExecutionReportEvent) -> int:
        assert self.ledger_path is not None
        path_obj = os.fspath(self.ledger_path)
        if not os.path.isfile(path_obj):
            return 0
        conn = sqlite3.connect(path_obj, timeout=5.0)
        conn.row_factory = sqlite3.Row
        changed = 0
        try:
            tables = {
                str(r[0])
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "binance_spot_positions" not in tables:
                return 0
            cols = {
                str(r[1])
                for r in conn.execute("PRAGMA table_info(binance_spot_positions)").fetchall()
            }

            # 1. Reconcile resting BUY entry (`maker_resting`)
            if ev.side == "BUY":
                rows = conn.execute(
                    """
                    SELECT * FROM binance_spot_positions
                    WHERE symbol = ? AND entry_order_id = ?
                    """,
                    (ev.symbol, ev.order_id),
                ).fetchall()
                for row in rows:
                    status = str(row["status"] or "")
                    if status != "maker_resting":
                        continue
                    if ev.order_status == "FILLED" and ev.cum_filled_qty > 0:
                        base_asset = ev.symbol[:-4] if ev.symbol.endswith("USDT") else ""
                        net_qty = ev.cum_filled_qty
                        if ev.commission > 0 and ev.commission_asset == base_asset:
                            net_qty = max(Decimal("0"), net_qty - ev.commission)
                        if net_qty <= 0:
                            continue
                        avg_px = (
                            _dec_str(ev.cum_quote_qty / ev.cum_filled_qty)
                            if ev.cum_quote_qty > 0
                            else str(row["entry_price"] or "")
                        )
                        conn.execute(
                            """
                            UPDATE binance_spot_positions
                            SET status = 'open',
                                quantity = ?,
                                entry_price = ?,
                                last_error = NULL
                            WHERE id = ?
                            """,
                            (_dec_str(net_qty), avg_px, row["id"]),
                        )
                        changed += 1
                    elif (
                        ev.order_status in {"CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}
                        and ev.cum_filled_qty == 0
                    ):
                        conn.execute(
                            """
                            UPDATE binance_spot_positions
                            SET status = 'closed_canceled',
                                exit_reason = 'ws_entry_canceled_zero_fill',
                                last_error = NULL
                            WHERE id = ?
                            """,
                            (row["id"],),
                        )
                        changed += 1

            # 2. Reconcile SELL OCO leg fill (`oco_working` / `oco_unknown` -> `oco_filled`)
            elif ev.side == "SELL" and ev.order_list_id >= 0 and ev.order_status == "FILLED":
                list_id_str = str(ev.order_list_id)
                rows = conn.execute(
                    """
                    SELECT * FROM binance_spot_positions
                    WHERE symbol = ? AND oco_order_list_id = ?
                    """,
                    (ev.symbol, list_id_str),
                ).fetchall()
                for row in rows:
                    status = str(row["status"] or "")
                    if status not in {"oco_working", "oco_unknown", "oco_partial"}:
                        continue
                    held_qty = _dec_or_none(row["quantity"])
                    if held_qty is None or ev.cum_filled_qty < held_qty:
                        continue
                    exit_reason = (
                        "take_profit"
                        if ev.order_type in {"LIMIT_MAKER", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT"}
                        else "stop_loss"
                    )
                    upd_cols: dict[str, Any] = {
                        "status": "oco_filled",
                        "protection": "exchange_oco",
                        "exit_order_id": ev.order_id,
                        "exit_reason": exit_reason,
                        "oco_list_status": "ALL_DONE",
                        "last_error": None,
                    }
                    if "exit_filled_qty" in cols:
                        upd_cols["exit_filled_qty"] = _dec_str(ev.cum_filled_qty)
                    if "exit_price" in cols and ev.cum_quote_qty > 0 and ev.cum_filled_qty > 0:
                        upd_cols["exit_price"] = _dec_str(ev.cum_quote_qty / ev.cum_filled_qty)
                    if "unprotected_qty" in cols:
                        upd_cols["unprotected_qty"] = None
                    if "manual_action" in cols:
                        upd_cols["manual_action"] = None
                    assign_sql = ", ".join(f"{c} = ?" for c in upd_cols)
                    conn.execute(
                        f"UPDATE binance_spot_positions SET {assign_sql} WHERE id = ?",
                        [*upd_cols.values(), row["id"]],
                    )
                    changed += 1

            if changed:
                conn.commit()
        finally:
            conn.close()
        return changed

    def _reconcile_list_status_sqlite(self, ev: ListStatusEvent) -> int:
        """Restore `oco_unknown` -> `oco_working` ONLY when matching SELL leg reports exist."""
        assert self.ledger_path is not None
        if ev.list_status_type != "EXEC_STARTED" or ev.list_order_status != "EXECUTING":
            return 0
        if len(ev.orders) != 2:
            return 0
        # Require both legs to have been observed in executionReport with explicit side == SELL
        leg_events: list[ExecutionReportEvent] = []
        for leg in ev.orders:
            rep = self._latest_orders.get((ev.symbol, leg.order_id))
            if rep is None or rep.side != "SELL" or rep.symbol != ev.symbol:
                return 0
            if rep.order_status not in {"NEW", "PARTIALLY_FILLED"}:
                return 0
            leg_events.append(rep)

        path_obj = os.fspath(self.ledger_path)
        if not os.path.isfile(path_obj):
            return 0
        conn = sqlite3.connect(path_obj, timeout=5.0)
        conn.row_factory = sqlite3.Row
        changed = 0
        try:
            tables = {
                str(r[0])
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "binance_spot_positions" not in tables:
                return 0
            cols = {
                str(r[1])
                for r in conn.execute("PRAGMA table_info(binance_spot_positions)").fetchall()
            }
            rows = conn.execute(
                """
                SELECT * FROM binance_spot_positions
                WHERE symbol = ? AND status = 'oco_unknown'
                  AND (oco_order_list_id = ? OR oco_list_client_id = ?)
                """,
                (ev.symbol, ev.order_list_id, ev.list_client_order_id),
            ).fetchall()
            for row in rows:
                held = _dec_or_none(row["quantity"])
                if held is None or held <= 0:
                    continue
                if any((rep.order_qty - rep.cum_filled_qty) < held for rep in leg_events):
                    continue
                upd: dict[str, Any] = {
                    "status": "oco_working",
                    "protection": "exchange_oco",
                    "oco_order_list_id": ev.order_list_id,
                    "oco_list_status": "EXECUTING",
                    "oco_above_order_id": ev.orders[0].order_id,
                    "oco_below_order_id": ev.orders[1].order_id,
                    "last_error": None,
                }
                if "unprotected_qty" in cols:
                    upd["unprotected_qty"] = None
                if "manual_action" in cols:
                    upd["manual_action"] = None
                assign_sql = ", ".join(f"{c} = ?" for c in upd)
                conn.execute(
                    f"UPDATE binance_spot_positions SET {assign_sql} WHERE id = ?",
                    [*upd.values(), row["id"]],
                )
                changed += 1
            if changed:
                conn.commit()
        finally:
            conn.close()
        return changed
