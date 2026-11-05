"""Binance spot USDT adapter for purpose ``live_bnb_boll_fade`` only.

Live ``POST /api/v3/order`` stays off unless every gate below is open:

- kill-switch file ``LIVE_TRADING_DISABLED`` (or ``LIVE_KILL_SWITCH_PATH``) is absent
- ``BINANCE_SPOT_LIVE_ENABLED`` is explicitly on
- the decision ``mode`` is ``live_stub`` (``dry_run`` never submits)
- ``extras.binance_symbol`` is a spot ``BASEUSDT`` id
- exchangeInfo for that symbol says ``TRADING``
- API key and secret come from the environment

This module does not call ``LiveExecutor`` and does not apply a per-order
notional cap. Tests inject a client. The HTTP client is built only after the
gates pass and only when no client was injected.

A live buy does not attach the OCO to the entry ``newOrder``. The entry
receipt keeps ``protective_order=not_supported_not_sent``. After the fill,
``maintain_held_position`` posts ``POST /api/v3/orderList/oco`` (take-profit
``LIMIT_MAKER`` and stop ``STOP_LOSS_LIMIT``) sized from ``executedQty``.
That list is the exchange-side cover if this process is down. The loop
market sell is only the fallback after a positive OCO rejection or a
terminal unfilled list. Dry-run never calls HTTP.

``LIVE_TRADING_DISABLED`` blocks new buys and inbound sell signals. A
ledger-tracked protective sell still goes out unless
``BINANCE_SPOT_BLOCK_SELL_ON_KILL=1``. ``BINANCE_SPOT_LIVE_ENABLED`` must
also be on. Neither flag is enabled by this module.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any, Callable, Literal

from logging_setup import get_logger

log = get_logger("execution.binance_spot")

PURPOSE_LIVE_BNB_BOLL_FADE = "live_bnb_boll_fade"
EXECUTOR_NAME = "binance_spot"
ENABLE_ENV = "BINANCE_SPOT_LIVE_ENABLED"
KILL_SWITCH_ENV = "LIVE_KILL_SWITCH_PATH"
DEFAULT_KILL_SWITCH_NAME = "LIVE_TRADING_DISABLED"
API_KEY_ENV = ("BINANCE_API_KEY", "LIVE_EXCHANGE_API_KEY")
API_SECRET_ENV = ("BINANCE_API_SECRET", "LIVE_EXCHANGE_API_SECRET")
DEFAULT_BASE_URL = "https://api.binance.com"
SPOT_PATH = "/api/v3/order"
EXCHANGE_INFO_PATH = "/api/v3/exchangeInfo"
TICKER_PATH = "/api/v3/ticker/price"
OCO_PATH = "/api/v3/orderList/oco"
ORDER_LIST_PATH = "/api/v3/orderList"
RECV_WINDOW_MS = "5000"
LIVE_MODE = "live_stub"
DRY_MODE = "dry_run"
HELD_EXIT_PROTECTION = "held_position_exit"
_EXIT_FAIL_STATUS = frozenset({"REJECTED", "EXPIRED", "CANCELED", "CANCELLED"})
_ExitReason = Literal["stop_loss", "take_profit"]

# Same base shape as decision_engine.core.bnb_spot_map, but the order id must
# already be the concatenated BASEUSDT form. Slash / colon names are rejected.
_BASE_MIN = 2
_BASE_MAX = 20
_NOT_A_BASE = frozenset(
    {"USDT", "USD", "USDC", "FDUSD", "BUSD", "TUSD", "DAI", "USDP"}
)
_DECIMAL = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d+)?$")
_OrderType = Literal["MARKET", "LIMIT"]
_Side = Literal["BUY", "SELL"]

Opener = Callable[[urllib.request.Request, float], tuple[int, bytes]]


class BinanceRequestError(Exception):
    """HTTP failure. The message is only a status code, never a body or secret."""

    def __init__(self, status: int) -> None:
        self.status = int(status)
        super().__init__(f"binance_http_error:{self.status}")


def _env_flag(name: str) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_secret(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _repo_root() -> Path:
    # execution/executors/binance_spot.py → repo root
    return Path(__file__).resolve().parents[2]


def kill_switch_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Path consulted for the kill switch.

    An explicit argument or ``LIVE_KILL_SWITCH_PATH`` wins. With neither set,
    this returns the cwd file; ``kill_switch_active`` also looks at the repo
    root and ``crawler/`` so a process started elsewhere still sees the switch.
    """
    if explicit is not None and str(explicit).strip():
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return path
    raw = os.environ.get(KILL_SWITCH_ENV, "").strip()
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return path
    return Path.cwd() / DEFAULT_KILL_SWITCH_NAME


def kill_switch_active(explicit: str | os.PathLike[str] | None = None) -> tuple[bool, str]:
    """Return ``(active, display_name)``.

    When the caller or the env names a path, only that path is consulted.
    When neither is set, the switch is active if any default location exists.
    """
    if explicit is not None or os.environ.get(KILL_SWITCH_ENV, "").strip():
        path = kill_switch_path(explicit)
        return path.is_file(), path.name
    root = _repo_root()
    candidates = (
        Path.cwd() / DEFAULT_KILL_SWITCH_NAME,
        root / DEFAULT_KILL_SWITCH_NAME,
        root / "crawler" / DEFAULT_KILL_SWITCH_NAME,
    )
    for path in candidates:
        if path.is_file():
            return True, path.name
    return False, DEFAULT_KILL_SWITCH_NAME


def is_spot_usdt_symbol(symbol: object) -> bool:
    """True for a canonical spot id such as ``BTCUSDT``.

    Rejects quote assets other than USDT, CCXT names (``BTC/USDT``),
    derivatives (``:``), lowercase, and stable-coin bases.
    """
    if not isinstance(symbol, str) or not symbol or symbol != symbol.strip():
        return False
    if symbol != symbol.upper():
        return False
    if any(mark in symbol for mark in ("/", ":", "-", "_", " ")):
        return False
    if not symbol.endswith("USDT") or symbol == "USDT":
        return False
    base = symbol[: -len("USDT")]
    if len(base) < _BASE_MIN or len(base) > _BASE_MAX:
        return False
    if base in _NOT_A_BASE or not base.isalnum():
        return False
    return True


def symbol_status_trading(payload: dict[str, Any], symbol: str) -> bool:
    """True when ``payload`` is exchangeInfo (or one symbol row) and it is spot TRADING."""
    row = payload
    listed = payload.get("symbols")
    if isinstance(listed, list):
        found: dict[str, Any] | None = None
        for item in listed:
            if isinstance(item, dict) and str(item.get("symbol") or "").upper() == symbol:
                found = item
                break
        if found is None:
            return False
        row = found
    listed_symbol = str(row.get("symbol") or "").upper()
    if listed_symbol and listed_symbol != symbol:
        return False
    if str(row.get("status") or "").upper() != "TRADING":
        return False
    quote = row.get("quoteAsset")
    if quote is not None and str(quote).upper() != "USDT":
        return False
    if row.get("isSpotTradingAllowed") is False:
        return False
    return True


def _positive_decimal(raw: object) -> str | None:
    """Decimal string Binance can take as quantity. Floats are rejected."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        if raw <= 0:
            return None
        return str(raw)
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not _DECIMAL.fullmatch(text):
        return None
    if all(ch in "0." for ch in text):
        return None
    return text


def _parse_order_type(raw: object) -> _OrderType | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "MARKET"
    text = str(raw).strip().upper()
    if text == "MARKET":
        return "MARKET"
    if text == "LIMIT":
        return "LIMIT"
    return None


def _finite_number(raw: object) -> float | None:
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _format_positive(raw: object) -> str | None:
    """Decimal text for an intent price. Floats are accepted; blank and <= 0 are not.

    Exchange quantity stays on ``_positive_decimal`` (digit strings only).
    """
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, str):
        return _positive_decimal(raw.strip())
    if isinstance(raw, int):
        return _positive_decimal(raw)
    if isinstance(raw, float):
        if not math.isfinite(raw) or raw <= 0:
            return None
        text = format(raw, ".12f").rstrip("0").rstrip(".")
        return text if _positive_decimal(text) else None
    return None


def _chosen_limit_raw(extras: dict[str, Any]) -> tuple[object, str | None]:
    raw_price = extras.get("price")
    raw_limit = extras.get("limit_price")
    price_set = raw_price not in (None, "")
    limit_set = raw_limit not in (None, "")
    if price_set and limit_set:
        if _format_positive(raw_price) != _format_positive(raw_limit):
            return None, "ambiguous_limit_price"
    if price_set:
        return raw_price, None
    if limit_set:
        return raw_limit, None
    return None, None


def build_exit_plan(
    sig: dict[str, Any],
    extras: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Absolute stop and take-profit from the signal. No 5% / 10% fill-in.

    Absent on a legacy signal (no ``entry_reason`` and no prices) so an old
    dry-run still acks. Present prices are the decision-time ``boll_mid`` and
    the stop reference; a later mid does not rewrite them. The entry order
    itself still does not rest an OCO. ``maintain_held_position`` posts that
    list after the fill, and ``exit_held_position`` is only the fallback.
    """
    stop_raw = extras.get("stop_price")
    tp_raw = extras.get("take_profit_price")
    reason = extras.get("entry_reason")
    if all(value in (None, "") for value in (stop_raw, tp_raw, reason)):
        return None, None
    action = str(sig.get("action") or "").strip().lower()
    if action != "buy":
        return None, "exit_plan_buy_only"
    stop = _format_positive(stop_raw)
    take_profit = _format_positive(tp_raw)
    if stop is None or take_profit is None:
        return None, "missing_exit_prices"
    order_type = _parse_order_type(extras.get("order_type", extras.get("binance_order_type")))
    if order_type is None:
        return None, "invalid_order_type"
    if order_type == "LIMIT":
        entry_raw, price_error = _chosen_limit_raw(extras)
        if price_error:
            return None, price_error
        entry = _format_positive(entry_raw)
        if entry is None:
            return None, "missing_limit_price"
    elif order_type == "MARKET":
        entry = _format_positive(extras.get("price_usd"))
        if entry is None:
            entry = _format_positive(extras.get("entry_price"))
        if entry is None:
            return None, "missing_entry_price"
    else:
        unexpected_type: never = order_type
        return None, f"invalid_order_type:{unexpected_type}"
    stop_n = float(stop)
    entry_n = float(entry)
    tp_n = float(take_profit)
    if stop_n >= entry_n:
        return None, "stop_not_below_entry"
    if tp_n <= entry_n:
        return None, "take_profit_not_above_entry"
    return {
        "stop_price": stop,
        "take_profit_price": take_profit,
        "entry_price": entry,
        "submitted": False,
        "protective_order": "not_supported_not_sent",
        "defaults_applied": False,
        "price_source": "signal_absolute",
    }, None


def protective_exit_reason(
    exit_plan: dict[str, Any] | None, mark: object
) -> _ExitReason | None:
    """Compare a mark to the absolute plan. Percent stops are not applied."""
    if not isinstance(exit_plan, dict):
        return None
    stop = _finite_number(exit_plan.get("stop_price"))
    take_profit = _finite_number(exit_plan.get("take_profit_price"))
    price = _finite_number(mark)
    if stop is None or take_profit is None or price is None:
        return None
    if price <= stop:
        return "stop_loss"
    if price >= take_profit:
        return "take_profit"
    return None


def filled_base_qty(placed: dict[str, Any] | None) -> str | None:
    """Base qty the exchange says filled. Quote notional is not converted."""
    if not isinstance(placed, dict):
        return None
    return _positive_decimal(placed.get("executedQty"))


def exit_client_order_id(raw: object) -> str | None:
    """Stable id so a retried full-position sell cannot double-fill."""
    if isinstance(raw, bool) or raw is None:
        return None
    text = str(raw).strip()
    if not text.isdigit():
        return None
    client_id = f"bnbfade-x-{text}"
    if len(client_id) > 36:
        return None
    return client_id


def _to_decimal(raw: object) -> Decimal | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, Decimal):
        number = raw
    elif isinstance(raw, int):
        number = Decimal(raw)
    elif isinstance(raw, float):
        if not math.isfinite(raw) or raw <= 0:
            return None
        number = Decimal(str(raw))
    elif isinstance(raw, str):
        text = raw.strip()
        if not _DECIMAL.fullmatch(text):
            return None
        number = Decimal(text)
    else:
        return None
    if not number.is_finite() or number <= 0:
        return None
    return number


def _decimal_text(number: Decimal) -> str:
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def floor_base_qty(quantity: object, step: object) -> str | None:
    """Floor ``quantity`` to ``step``. Never rounds up and never exceeds it."""
    qty = _to_decimal(quantity)
    step_n = _to_decimal(step)
    if qty is None or step_n is None:
        return None
    units = (qty / step_n).to_integral_value(rounding=ROUND_DOWN)
    floored = units * step_n
    if floored <= 0 or floored > qty:
        return None
    return _decimal_text(floored)


def _symbol_row(payload: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    listed = payload.get("symbols")
    if isinstance(listed, list):
        for item in listed:
            if isinstance(item, dict) and str(item.get("symbol") or "").upper() == symbol:
                return item
        return None
    listed_symbol = str(payload.get("symbol") or "").upper()
    if listed_symbol and listed_symbol != symbol:
        return None
    return payload


def _filter_row(filters: list[Any], name: str) -> dict[str, Any] | None:
    for item in filters:
        if isinstance(item, dict) and str(item.get("filterType") or "") == name:
            return item
    return None


def _symbol_filters(payload: dict[str, Any], symbol: str) -> list[Any] | None:
    row = _symbol_row(payload, symbol)
    if row is None:
        return None
    filters = row.get("filters")
    if not isinstance(filters, list):
        return None
    return filters


def _market_lot(filters: list[Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Prefer a positive MARKET_LOT_SIZE step, else LOT_SIZE."""
    market = _filter_row(filters, "MARKET_LOT_SIZE")
    lot = _filter_row(filters, "LOT_SIZE")
    market_step = None if market is None else _to_decimal(market.get("stepSize"))
    if market is not None and market_step is not None:
        return market, _decimal_text(market_step)
    lot_step = None if lot is None else _to_decimal(lot.get("stepSize"))
    if lot is not None and lot_step is not None:
        return lot, _decimal_text(lot_step)
    return None, None


def _min_notional(filters: list[Any]) -> Decimal | None:
    for name in ("NOTIONAL", "MIN_NOTIONAL"):
        row = _filter_row(filters, name)
        if row is None:
            continue
        if name == "NOTIONAL" and row.get("applyMinToMarket") is False:
            continue
        floor = _to_decimal(row.get("minNotional"))
        if floor is not None:
            return floor
    return None


def size_exit_quantity(
    payload: dict[str, Any],
    symbol: str,
    quantity: str,
    mark: object,
) -> tuple[str | None, str | None]:
    """Full-position sell size. Dust below the step or min is not sent."""
    filters = _symbol_filters(payload, symbol)
    if filters is None:
        return None, "missing_lot_filter"
    lot, step = _market_lot(filters)
    if lot is None or step is None:
        return None, "missing_lot_filter"
    floored = floor_base_qty(quantity, step)
    if floored is None:
        return None, "exit_qty_below_min"
    floored_n = _to_decimal(floored)
    if floored_n is None:
        return None, "exit_qty_below_min"
    min_qty = _to_decimal(lot.get("minQty"))
    if min_qty is not None and floored_n < min_qty:
        return None, "exit_qty_below_min"
    max_qty = _to_decimal(lot.get("maxQty"))
    if max_qty is not None and floored_n > max_qty:
        return None, "exit_qty_above_max"
    mark_n = _to_decimal(mark)
    notional_floor = _min_notional(filters)
    if notional_floor is not None:
        if mark_n is None:
            return None, "mark_unavailable"
        if floored_n * mark_n < notional_floor:
            return None, "exit_below_min_notional"
    return floored, None


def held_position_spec(
    sig: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any] | None:
    """Ledger row for a live buy that already has absolute stop and take-profit.

    Dry-run and unfilled acks return None. Base size comes only from
    ``filled_qty`` (exchange ``executedQty``). Missing size is recorded as
    ``qty_unconfirmed`` and is not sellable.
    """
    if result.get("dry_run") or not result.get("submitted"):
        return None
    if int(result.get("executed") or 0) != 1:
        return None
    if str(sig.get("action") or "").strip().lower() != "buy":
        return None
    plan = result.get("exit_plan")
    if not isinstance(plan, dict):
        return None
    symbol = result.get("symbol")
    if not isinstance(symbol, str) or not is_spot_usdt_symbol(symbol):
        return None
    decision_id = exit_client_order_id(sig.get("decision_log_id"))
    if decision_id is None:
        return None
    stop = _positive_decimal(plan.get("stop_price"))
    take_profit = _positive_decimal(plan.get("take_profit_price"))
    if stop is None or take_profit is None:
        return None
    raw_sellable = result.get("sellable_qty")
    qty = (
        _positive_decimal(raw_sellable)
        if raw_sellable not in (None, "")
        else None
    )
    if qty is None:
        qty = _positive_decimal(result.get("filled_qty"))
    return {
        "decision_log_id": int(str(sig.get("decision_log_id")).strip()),
        "symbol": symbol,
        "quantity": qty,
        "stop_price": stop,
        "take_profit_price": take_profit,
        "entry_price": _positive_decimal(plan.get("entry_price")),
        "entry_order_id": (
            None
            if result.get("exchange_order_id") is None
            else str(result.get("exchange_order_id"))
        ),
        "status": "open" if qty else "qty_unconfirmed",
        "protection": HELD_EXIT_PROTECTION,
    }


def _exit_order_accepted(placed: dict[str, Any]) -> str | None:
    status = str(placed.get("status") or "").upper()
    if status in _EXIT_FAIL_STATUS:
        return None
    order_id = placed.get("orderId")
    if order_id is None or not str(order_id).strip():
        return None
    return str(order_id).strip()


def _entry_intent(
    extras: dict[str, Any],
    *,
    side: str,
    order_type: _OrderType,
    order: dict[str, str] | None,
) -> dict[str, Any]:
    limit_raw, _error = _chosen_limit_raw(extras)
    limit_text = _format_positive(limit_raw) if order_type == "LIMIT" else None
    return {
        "side": side,
        "order_type": order_type,
        "limit_price": limit_text,
        "exchange_ready": order is not None,
        "quantity_status": "exchange_ready" if order is not None else "deferred_size_pct",
        "protective_order": "not_supported_not_sent",
    }


def _parse_side(action: object) -> _Side | None:
    text = str(action or "").strip().lower()
    if text == "buy":
        return "BUY"
    if text == "sell":
        return "SELL"
    return None


def _client_order_id(sig: dict[str, Any]) -> str | None:
    raw = sig.get("decision_log_id")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text.isdigit():
        return None
    client_id = f"bnbfade-{text}"
    if len(client_id) > 36:
        return None
    return client_id


def sign_query(pairs: list[tuple[str, str]], secret: str) -> str:
    """HMAC-SHA256 of the form body, as Binance spot ``newOrder`` expects."""
    query = urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote, safe="")
    signature = hmac.new(
        secret.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{query}&signature={signature}"


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    return out


def _default_opener(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return int(exc.code), body


class BinanceSpotHttpClient:
    """Signed spot REST client. Constructed only after the live gates pass."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = 10.0,
        opener: Opener | None = None,
    ) -> None:
        raw = (
            base_url
            or os.environ.get("BINANCE_SPOT_API_BASE", "").strip()
            or DEFAULT_BASE_URL
        )
        self.base_url = raw.rstrip("/")
        self.timeout = float(timeout)
        self._opener = opener or _default_opener

    def exchange_info(self, symbol: str) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            (("symbol", symbol),),
            quote_via=urllib.parse.quote,
            safe="",
        )
        request = urllib.request.Request(
            f"{self.base_url}{EXCHANGE_INFO_PATH}?{query}",
            method="GET",
            headers={"User-Agent": "trade-binance-spot"},
        )
        status, body = self._opener(request, self.timeout)
        return _decode_ok(status, body)

    def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}{SPOT_PATH}",
            data=signed_query.encode("utf-8"),
            method="POST",
            headers={
                "User-Agent": "trade-binance-spot",
                "X-MBX-APIKEY": api_key,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        status, body = self._opener(request, self.timeout)
        return _decode_ok(status, body)

    def ticker_price(self, symbol: str) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            (("symbol", symbol),),
            quote_via=urllib.parse.quote,
            safe="",
        )
        request = urllib.request.Request(
            f"{self.base_url}{TICKER_PATH}?{query}",
            method="GET",
            headers={"User-Agent": "trade-binance-spot"},
        )
        status, body = self._opener(request, self.timeout)
        return _decode_ok(status, body)

    def get_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}{SPOT_PATH}?{signed_query}",
            method="GET",
            headers={
                "User-Agent": "trade-binance-spot",
                "X-MBX-APIKEY": api_key,
            },
        )
        status, body = self._opener(request, self.timeout)
        return _decode_ok(status, body)

    def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}{OCO_PATH}",
            data=signed_query.encode("utf-8"),
            method="POST",
            headers={
                "User-Agent": "trade-binance-spot",
                "X-MBX-APIKEY": api_key,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        status, body = self._opener(request, self.timeout)
        return _decode_ok(status, body)

    def get_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}{ORDER_LIST_PATH}?{signed_query}",
            method="GET",
            headers={
                "User-Agent": "trade-binance-spot",
                "X-MBX-APIKEY": api_key,
            },
        )
        status, body = self._opener(request, self.timeout)
        return _decode_ok(status, body)


def _decode_ok(status: int, body: bytes) -> dict[str, Any]:
    if status >= 400:
        raise BinanceRequestError(status)
    try:
        parsed = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BinanceRequestError(status) from exc
    if not isinstance(parsed, dict):
        raise BinanceRequestError(status)
    return parsed


def _order_pairs(
    *,
    symbol: str,
    side: _Side,
    order_type: _OrderType,
    extras: dict[str, Any],
    client_id: str | None,
    timestamp_ms: int,
) -> tuple[list[tuple[str, str]] | None, str | None]:
    quote_qty = _positive_decimal(
        extras.get("quote_order_qty", extras.get("quoteOrderQty"))
    )
    quantity = _positive_decimal(extras.get("quantity"))
    raw_quote = extras.get("quote_order_qty", extras.get("quoteOrderQty"))
    raw_qty = extras.get("quantity")
    raw_price, price_error = _chosen_limit_raw(extras)
    if price_error:
        return None, price_error
    price = _format_positive(raw_price)
    if raw_quote not in (None, "") and quote_qty is None:
        return None, "invalid_order_qty"
    if raw_qty not in (None, "") and quantity is None:
        return None, "invalid_order_qty"
    if raw_price not in (None, "") and price is None:
        return None, "invalid_order_qty"

    pairs: list[tuple[str, str]] = [
        ("symbol", symbol),
        ("side", side),
        ("type", order_type),
    ]
    if order_type == "MARKET":
        if side == "BUY":
            if quote_qty and quantity:
                return None, "ambiguous_order_qty"
            if quote_qty:
                pairs.append(("quoteOrderQty", quote_qty))
            elif quantity:
                pairs.append(("quantity", quantity))
            else:
                return None, "missing_order_qty"
        elif side == "SELL":
            if quote_qty:
                return None, "ambiguous_order_qty"
            if not quantity:
                return None, "missing_order_qty"
            pairs.append(("quantity", quantity))
        else:
            unexpected_side: never = side
            return None, f"invalid_side:{unexpected_side}"
    elif order_type == "LIMIT":
        if quote_qty:
            return None, "limit_rejects_quote_order_qty"
        if not quantity:
            return None, "missing_order_qty"
        if not price:
            return None, "missing_limit_price"
        pairs.extend(
            (
                ("timeInForce", "GTC"),
                ("quantity", quantity),
                ("price", price),
            )
        )
    else:
        unexpected_type: never = order_type
        return None, f"invalid_order_type:{unexpected_type}"

    if client_id:
        pairs.append(("newClientOrderId", client_id))
    pairs.append(("recvWindow", RECV_WINDOW_MS))
    pairs.append(("timestamp", str(int(timestamp_ms))))
    return pairs, None


def _result(
    *,
    ok: bool,
    dry_run: bool,
    executed: int,
    submitted: bool,
    error: str | None,
    symbol: str | None,
    order: dict[str, str] | None,
    exchange_order_id: str | None,
    gates: dict[str, Any],
    intent: dict[str, Any] | None = None,
    exit_plan: dict[str, Any] | None = None,
    filled_qty: str | None = None,
    sellable_qty: str | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "dry_run": dry_run,
        "executed": executed,
        "submitted": submitted,
        "error": error,
        "executor": EXECUTOR_NAME,
        "symbol": symbol,
        "order": order,
        "exchange_order_id": exchange_order_id,
        "gates": gates,
        "intent": intent,
        "exit_plan": exit_plan,
        "filled_qty": filled_qty,
        "sellable_qty": sellable_qty,
    }


from execution.executors.binance_spot_protect import (  # noqa: E402
    PROTECTION_DUST,
    PROTECTION_EXCHANGE,
    PROTECTION_FALLBACK,
    PROTECTION_PENDING,
    block_sell_on_kill,
    build_spot_oco,
    classify_oco,
    dust_archive_fields,
    dust_detail,
    entry_notional_floor_error,
    entry_viability_error,
    is_request_timeout,
    ENTRY_MIN_NOTIONAL_USDT,
    resolve_entry_size,
    oco_is_working,
    oco_leg_ids,
    protective_order_block,
    sellable_base_qty,
    tracked_exit,
)


def _floor_limit_quantity(
    info: dict[str, Any],
    symbol: str,
    extras: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Floor a size_pct limit buy onto LOT_SIZE. Does not read a balance."""
    filters = _symbol_filters(info, symbol)
    if not filters:
        return None, "entry_not_viable:missing_lot_filter"
    lot = _filter_row(filters, "LOT_SIZE")
    step = None if lot is None else _to_decimal(lot.get("stepSize"))
    if lot is None or step is None:
        return None, "entry_not_viable:missing_lot_filter"
    floored = floor_base_qty(extras.get("quantity"), _decimal_text(step))
    if floored is None:
        return None, "entry_not_viable:below_min_qty"
    floored_n = _to_decimal(floored)
    min_qty = _to_decimal(lot.get("minQty"))
    if floored_n is None or (min_qty is not None and floored_n < min_qty):
        return None, "entry_not_viable:below_min_qty"
    price = _to_decimal(extras.get("limit_price"))
    if price is None:
        price = _to_decimal(extras.get("price"))
    min_notional = _min_notional(filters)
    if floored_n is not None and price is not None:
        notion = floored_n * price
        if notion < ENTRY_MIN_NOTIONAL_USDT:
            return None, "entry_notional_below_min"
        if min_notional is not None and notion < min_notional:
            return None, "entry_not_viable:below_min_notional"
    return floored, None


class BinanceSpotAdapter:
    """Turn one ``live_bnb_boll_fade`` decision into a spot order, or refuse it."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        kill_switch_path: str | os.PathLike[str] | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
        clock_ms: Callable[[], int] | None = None,
        dry_run: bool | None = None,
    ) -> None:
        self.client = client
        self.kill_switch_path = kill_switch_path
        self.api_key = api_key
        self.api_secret = api_secret
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.dry_run = dry_run
        self._size_plan: dict[str, Any] | None = None

    def _credentials(self) -> tuple[str, str]:
        key = (self.api_key if self.api_key is not None else _env_secret(API_KEY_ENV)).strip()
        secret = (
            self.api_secret if self.api_secret is not None else _env_secret(API_SECRET_ENV)
        ).strip()
        return key, secret

    def _gates(self) -> dict[str, Any]:
        active, name = kill_switch_active(self.kill_switch_path)
        return {
            "kill_switch_active": active,
            "kill_switch": name,
            "live_enabled": _env_flag(ENABLE_ENV),
            "block_sell_on_kill": block_sell_on_kill(),
        }

    def execute(self, sig: dict[str, Any]) -> dict[str, Any]:
        self._size_plan = None
        gates = self._gates()
        purpose = str(sig.get("purpose") or "").strip()
        if purpose != PURPOSE_LIVE_BNB_BOLL_FADE:
            return self._finish(
                _result(
                    ok=False,
                    dry_run=False,
                    executed=0,
                    submitted=False,
                    error="purpose_not_live_bnb_boll_fade",
                    symbol=None,
                    order=None,
                    exchange_order_id=None,
                    gates=gates,
                )
            )

        extras = sig.get("extras")
        if not isinstance(extras, dict):
            extras = {}
        mode = str(sig.get("mode") or "").strip().lower()
        raw_symbol = extras.get("binance_symbol")
        if raw_symbol is None or (isinstance(raw_symbol, str) and not raw_symbol.strip()):
            return self._finish(
                _result(
                    ok=False,
                    dry_run=False,
                    executed=0,
                    submitted=False,
                    error="missing_binance_symbol",
                    symbol=None,
                    order=None,
                    exchange_order_id=None,
                    gates=gates,
                )
            )
        if not isinstance(raw_symbol, str) or not is_spot_usdt_symbol(raw_symbol.strip()):
            shown = raw_symbol.strip() if isinstance(raw_symbol, str) else None
            return self._finish(
                _result(
                    ok=False,
                    dry_run=False,
                    executed=0,
                    submitted=False,
                    error="invalid_binance_symbol",
                    symbol=shown,
                    order=None,
                    exchange_order_id=None,
                    gates=gates,
                )
            )
        symbol = raw_symbol.strip()
        token = sig.get("token_address")
        if isinstance(token, str) and token.strip() and token.strip() != symbol:
            return self._finish(
                _result(
                    ok=False,
                    dry_run=False,
                    executed=0,
                    submitted=False,
                    error="binance_symbol_mismatch",
                    symbol=symbol,
                    order=None,
                    exchange_order_id=None,
                    gates=gates,
                )
            )

        plan, plan_error = build_exit_plan(sig, extras)
        if plan_error:
            return self._finish(
                _result(
                    ok=False,
                    dry_run=mode == DRY_MODE or self.dry_run is True,
                    executed=0,
                    submitted=False,
                    error=plan_error,
                    symbol=symbol,
                    order=None,
                    exchange_order_id=None,
                    gates=gates,
                )
            )
        forced_dry = self.dry_run is True or mode == DRY_MODE
        extras, size_error, self._size_plan = resolve_entry_size(
            sig, extras, require_book=not forced_dry
        )
        if size_error:
            return self._finish(
                _result(
                    ok=False,
                    dry_run=forced_dry,
                    executed=0,
                    submitted=False,
                    error=size_error,
                    symbol=symbol,
                    order=None,
                    exchange_order_id=None,
                    gates=gates,
                    exit_plan=plan,
                )
            )
        if forced_dry:
            if str(sig.get("action") or "").strip().lower() == "buy":
                floor_error = entry_notional_floor_error(extras)
                if floor_error:
                    return self._finish(
                        _result(
                            ok=False,
                            dry_run=True,
                            executed=0,
                            submitted=False,
                            error=floor_error,
                            symbol=symbol,
                            order=None,
                            exchange_order_id=None,
                            gates=gates,
                            exit_plan=plan,
                        )
                    )
            preview, preview_error = self._preview(sig, extras, symbol)
            side = _parse_side(sig.get("action"))
            order_type = _parse_order_type(
                extras.get("order_type", extras.get("binance_order_type"))
            )
            intent = None
            if preview_error is None and side is not None and order_type is not None:
                intent = _entry_intent(
                    extras, side=side, order_type=order_type, order=preview
                )
            return self._finish(
                _result(
                    ok=preview_error is None,
                    dry_run=True,
                    executed=0,
                    submitted=False,
                    error=preview_error,
                    symbol=symbol,
                    order=preview,
                    exchange_order_id=None,
                    gates=gates,
                    intent=intent,
                    exit_plan=plan,
                )
            )

        if gates["kill_switch_active"]:
            return self._finish(
                _result(
                    ok=False,
                    dry_run=False,
                    executed=0,
                    submitted=False,
                    error=f"kill_switch:{gates['kill_switch']}",
                    symbol=symbol,
                    order=None,
                    exchange_order_id=None,
                    gates=gates,
                    exit_plan=plan,
                )
            )
        if not gates["live_enabled"]:
            return self._finish(
                _result(
                    ok=False,
                    dry_run=False,
                    executed=0,
                    submitted=False,
                    error="binance_spot_live_disabled",
                    symbol=symbol,
                    order=None,
                    exchange_order_id=None,
                    gates=gates,
                    exit_plan=plan,
                )
            )
        if mode != LIVE_MODE:
            return self._finish(
                _result(
                    ok=False,
                    dry_run=False,
                    executed=0,
                    submitted=False,
                    error="mode_not_live_stub",
                    symbol=symbol,
                    order=None,
                    exchange_order_id=None,
                    gates=gates,
                    exit_plan=plan,
                )
            )

        pairs, qty_error = self._preview_pairs(sig, extras, symbol)
        if qty_error or pairs is None:
            return self._finish(
                _result(
                    ok=False,
                    dry_run=False,
                    executed=0,
                    submitted=False,
                    error=qty_error or "missing_order_qty",
                    symbol=symbol,
                    order=None,
                    exchange_order_id=None,
                    gates=gates,
                    exit_plan=plan,
                )
            )

        if _parse_side(sig.get("action")) == "BUY":
            floor_error = entry_notional_floor_error(extras)
            if floor_error:
                return self._finish(
                    _result(
                        ok=False,
                        dry_run=False,
                        executed=0,
                        submitted=False,
                        error=floor_error,
                        symbol=symbol,
                        order=dict(pairs),
                        exchange_order_id=None,
                        gates=gates,
                        exit_plan=plan,
                    )
                )

        key, secret = self._credentials()
        if not key or not secret:
            return self._finish(
                _result(
                    ok=False,
                    dry_run=False,
                    executed=0,
                    submitted=False,
                    error="missing_api_credentials",
                    symbol=symbol,
                    order=dict(pairs),
                    exchange_order_id=None,
                    gates=gates,
                    exit_plan=plan,
                )
            )

        client = self.client if self.client is not None else BinanceSpotHttpClient()
        try:
            info = client.exchange_info(symbol)
        except Exception as exc:  # noqa: BLE001 — surface a redacted refusal, no retry
            return self._finish(
                _result(
                    ok=False,
                    dry_run=False,
                    executed=0,
                    submitted=False,
                    error=_public_error(exc, key, secret),
                    symbol=symbol,
                    order=dict(pairs),
                    exchange_order_id=None,
                    gates=gates,
                    exit_plan=plan,
                )
            )
        if not isinstance(info, dict) or not symbol_status_trading(info, symbol):
            return self._finish(
                _result(
                    ok=False,
                    dry_run=False,
                    executed=0,
                    submitted=False,
                    error="symbol_not_trading",
                    symbol=symbol,
                    order=dict(pairs),
                    exchange_order_id=None,
                    gates=gates,
                    exit_plan=plan,
                )
            )
        if _parse_side(sig.get("action")) == "BUY":
            viable_error = entry_viability_error(extras, info, symbol)
            if viable_error:
                return self._finish(
                    _result(
                        ok=False,
                        dry_run=False,
                        executed=0,
                        submitted=False,
                        error=viable_error,
                        symbol=symbol,
                        order=None,
                        exchange_order_id=None,
                        gates=gates,
                        exit_plan=plan,
                    )
                )
            if isinstance(self._size_plan, dict) and self._size_plan.get("needs_lot_floor"):
                floored, floor_err = _floor_limit_quantity(info, symbol, extras)
                if floor_err or floored is None:
                    return self._finish(
                        _result(
                            ok=False,
                            dry_run=False,
                            executed=0,
                            submitted=False,
                            error=floor_err or "entry_not_viable:below_min_qty",
                            symbol=symbol,
                            order=None,
                            exchange_order_id=None,
                            gates=gates,
                            exit_plan=plan,
                        )
                    )
                extras = dict(extras)
                extras["quantity"] = floored
                self._size_plan = dict(self._size_plan)
                self._size_plan["quantity"] = floored
                pairs, qty_error = self._preview_pairs(sig, extras, symbol)
                if qty_error or pairs is None:
                    return self._finish(
                        _result(
                            ok=False,
                            dry_run=False,
                            executed=0,
                            submitted=False,
                            error=qty_error or "missing_order_qty",
                            symbol=symbol,
                            order=None,
                            exchange_order_id=None,
                            gates=gates,
                            exit_plan=plan,
                        )
                    )

        signed = sign_query(pairs, secret)
        try:
            placed = client.new_order(signed, api_key=key)
        except Exception as exc:  # noqa: BLE001
            return self._finish(
                _result(
                    ok=False,
                    dry_run=False,
                    executed=0,
                    submitted=False,
                    error=_public_error(exc, key, secret),
                    symbol=symbol,
                    order=dict(pairs),
                    exchange_order_id=None,
                    gates=gates,
                    exit_plan=plan,
                )
            )
        if not isinstance(placed, dict):
            return self._finish(
                _result(
                    ok=False,
                    dry_run=False,
                    executed=0,
                    submitted=False,
                    error="binance_order_rejected",
                    symbol=symbol,
                    order=dict(pairs),
                    exchange_order_id=None,
                    gates=gates,
                    exit_plan=plan,
                )
            )
        order_id = placed.get("orderId")
        if order_id is None or not str(order_id).strip():
            return self._finish(
                _result(
                    ok=False,
                    dry_run=False,
                    executed=0,
                    submitted=False,
                    error="binance_order_rejected",
                    symbol=symbol,
                    order=dict(pairs),
                    exchange_order_id=None,
                    gates=gates,
                    exit_plan=plan,
                )
            )
        side = _parse_side(sig.get("action"))
        order_type = _parse_order_type(
            extras.get("order_type", extras.get("binance_order_type"))
        )
        intent = None
        if side is not None and order_type is not None:
            intent = _entry_intent(
                extras, side=side, order_type=order_type, order=dict(pairs)
            )
        return self._finish(
            _result(
                ok=True,
                dry_run=False,
                executed=1,
                submitted=True,
                error=None,
                symbol=symbol,
                order=dict(pairs),
                exchange_order_id=str(order_id),
                gates=gates,
                intent=intent,
                exit_plan=plan,
                filled_qty=filled_base_qty(placed),
                sellable_qty=sellable_base_qty(placed, symbol) or filled_base_qty(placed),
            )
        )

    def _preview(
        self,
        sig: dict[str, Any],
        extras: dict[str, Any],
        symbol: str,
    ) -> tuple[dict[str, str] | None, str | None]:
        """Dry-run preview. A missing size is still an accept: nothing is sent.

        A present but unusable size is a refusal, including on dry-run, so a
        bad quantity is not acknowledged as ready.
        """
        side = _parse_side(sig.get("action"))
        order_type = _parse_order_type(
            extras.get("order_type", extras.get("binance_order_type"))
        )
        if side is None:
            return None, "invalid_side"
        if order_type is None:
            return None, "invalid_order_type"
        raw_quote = extras.get("quote_order_qty", extras.get("quoteOrderQty"))
        raw_qty = extras.get("quantity")
        limit_raw, price_error = _chosen_limit_raw(extras)
        if price_error:
            return None, price_error
        has_qty = any(value not in (None, "") for value in (raw_quote, raw_qty))
        if order_type == "LIMIT" and _format_positive(limit_raw) is None:
            if limit_raw in (None, ""):
                return None, "missing_limit_price"
            return None, "invalid_limit_price"
        if order_type == "LIMIT" and not has_qty:
            # Price is an intent. Base size is still size_pct, not a fake quantity.
            return None, None
        if not has_qty:
            return None, None
        pairs, error = _order_pairs(
            symbol=symbol,
            side=side,
            order_type=order_type,
            extras=extras,
            client_id=_client_order_id(sig),
            timestamp_ms=int(self.clock_ms()),
        )
        if error or pairs is None:
            return None, error or "missing_order_qty"
        return dict(pairs), None

    def _preview_pairs(
        self,
        sig: dict[str, Any],
        extras: dict[str, Any],
        symbol: str,
    ) -> tuple[list[tuple[str, str]] | None, str | None]:
        side = _parse_side(sig.get("action"))
        order_type = _parse_order_type(
            extras.get("order_type", extras.get("binance_order_type"))
        )
        if side is None:
            return None, "invalid_side"
        if order_type is None:
            return None, "invalid_order_type"
        return _order_pairs(
            symbol=symbol,
            side=side,
            order_type=order_type,
            extras=extras,
            client_id=_client_order_id(sig),
            timestamp_ms=int(self.clock_ms()),
        )

    def refresh_filled_qty(self, position: dict[str, Any]) -> str | None:
        """Read the entry order's base fill. No request while the live gates are shut."""
        gates = self._gates()
        if not gates["live_enabled"]:
            return None
        symbol = position.get("symbol")
        order_id = position.get("entry_order_id")
        if not isinstance(symbol, str) or not is_spot_usdt_symbol(symbol):
            return None
        if order_id is None or not str(order_id).strip():
            return None
        key, secret = self._credentials()
        if not key or not secret:
            return None
        client = self.client if self.client is not None else BinanceSpotHttpClient()
        lookup = getattr(client, "get_order", None)
        if not callable(lookup):
            return None
        pairs = [
            ("symbol", symbol),
            ("orderId", str(order_id).strip()),
            ("recvWindow", RECV_WINDOW_MS),
            ("timestamp", str(int(self.clock_ms()))),
        ]
        try:
            placed = lookup(sign_query(pairs, secret), api_key=key)
        except Exception as exc:  # noqa: BLE001 — a missed read must not submit
            if isinstance(exc, AssertionError):
                raise
            return None
        if not isinstance(placed, dict):
            return None
        return filled_base_qty(placed)

    def mark_price(self, symbol: str) -> object | None:
        """Public last price. No client is built while the live gates are shut."""
        gates = self._gates()
        if not gates["live_enabled"]:
            return None
        if not is_spot_usdt_symbol(symbol):
            return None
        client = self.client if self.client is not None else BinanceSpotHttpClient()
        ticker = getattr(client, "ticker_price", None)
        if not callable(ticker):
            return None
        try:
            payload = ticker(symbol)
        except Exception as exc:  # noqa: BLE001 — a missing mark must not submit
            if isinstance(exc, AssertionError):
                raise
            return None
        if not isinstance(payload, dict):
            return None
        return payload.get("price")

    def exit_held_position(
        self,
        position: dict[str, Any],
        mark: object,
    ) -> dict[str, Any]:
        """Sell the whole held base qty when the absolute plan is touched.

        ``protective_exit_reason`` is the only trigger. Percent stops are not
        applied. A refusal leaves the caller to keep the row open.
        """
        gates = self._gates()
        symbol = position.get("symbol")
        shown = symbol if isinstance(symbol, str) else None
        plan = {
            "stop_price": position.get("stop_price"),
            "take_profit_price": position.get("take_profit_price"),
        }
        reason = protective_exit_reason(plan, mark)
        client_id = exit_client_order_id(position.get("decision_log_id"))
        outcome: dict[str, Any] = {
            "ok": False,
            "dry_run": False,
            "executed": 0,
            "submitted": False,
            "error": None,
            "reason": reason,
            "decision_log_id": position.get("decision_log_id"),
            "executor": EXECUTOR_NAME,
            "symbol": shown,
            "order": None,
            "exchange_order_id": None,
            "gates": gates,
            "quantity": None,
            "exit_client_order_id": client_id,
            "protection": position.get("protection") or HELD_EXIT_PROTECTION,
            "book": "unchanged",
        }
        if not tracked_exit(position):
            outcome["error"] = "untracked_exit"
            outcome["book"] = "unchanged"
            return self._log_exit(outcome)
        status = str(position.get("status") or "")
        if status == "qty_unconfirmed":
            outcome["error"] = "exit_qty_unconfirmed"
            return self._log_exit(outcome)
        if status != "open":
            outcome["error"] = "position_not_open"
            outcome["ok"] = status in {"exit_submitted", "closed"}
            return self._log_exit(outcome)
        if reason is None:
            if _finite_number(mark) is None:
                outcome["error"] = "mark_unavailable"
            else:
                outcome["ok"] = True
            return self._log_exit(outcome)
        if reason == "stop_loss":
            pass
        elif reason == "take_profit":
            pass
        else:
            unexpected_reason: never = reason
            outcome["error"] = f"invalid_exit_reason:{unexpected_reason}"
            return self._log_exit(outcome)
        blocked = protective_order_block(gates)
        if blocked:
            outcome["error"] = blocked
            outcome["book"] = "blocked"
            return self._log_exit(outcome)
        if not isinstance(symbol, str) or not is_spot_usdt_symbol(symbol):
            outcome["error"] = "invalid_binance_symbol"
            return self._log_exit(outcome)
        qty = _positive_decimal(position.get("quantity"))
        if qty is None:
            outcome["error"] = "exit_qty_unconfirmed"
            return self._log_exit(outcome)
        if client_id is None:
            outcome["error"] = "invalid_exit_client_id"
            return self._log_exit(outcome)
        key, secret = self._credentials()
        if not key or not secret:
            outcome["error"] = "missing_api_credentials"
            return self._log_exit(outcome)

        client = self.client if self.client is not None else BinanceSpotHttpClient()
        try:
            info = client.exchange_info(symbol)
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = _public_error(exc, key, secret)
            return self._log_exit(outcome)
        if not isinstance(info, dict) or not symbol_status_trading(info, symbol):
            outcome["error"] = "symbol_not_trading"
            return self._log_exit(outcome)
        sized, size_error = size_exit_quantity(info, symbol, qty, mark)
        if size_error or sized is None:
            detail = dust_detail(size_error)
            if detail is not None:
                archived = dust_archive_fields(
                    quantity=qty,
                    mark=mark,
                    stop=position.get("stop_price"),
                    detail=detail,
                )
                outcome["error"] = "dust_untradable"
                outcome["dust_detail"] = detail
                outcome["residual_qty"] = archived["residual_qty"]
                outcome["residual_quote"] = archived["residual_quote"]
                outcome["book"] = "closed_dust"
            else:
                outcome["error"] = size_error or "exit_qty_invalid"
                outcome["book"] = "error_only"
            return self._log_exit(outcome)
        pairs, qty_error = _order_pairs(
            symbol=symbol,
            side="SELL",
            order_type="MARKET",
            extras={"quantity": sized},
            client_id=client_id,
            timestamp_ms=int(self.clock_ms()),
        )
        if qty_error or pairs is None:
            outcome["error"] = qty_error or "missing_order_qty"
            return self._log_exit(outcome)
        outcome["order"] = dict(pairs)
        outcome["quantity"] = sized
        signed = sign_query(pairs, secret)
        try:
            placed = client.new_order(signed, api_key=key)
        except Exception as exc:  # noqa: BLE001
            recovered = self._lookup_exit_order(
                client, symbol, client_id, key, secret
            )
            if recovered is None:
                outcome["error"] = _public_error(exc, key, secret)
                return self._log_exit(outcome)
            placed = recovered
        if not isinstance(placed, dict):
            outcome["error"] = "binance_order_rejected"
            return self._log_exit(outcome)
        order_id = _exit_order_accepted(placed)
        if order_id is None:
            outcome["error"] = "binance_order_rejected"
            return self._log_exit(outcome)
        outcome["ok"] = True
        outcome["submitted"] = True
        outcome["executed"] = 1
        outcome["exchange_order_id"] = order_id
        outcome["book"] = "exit_submitted"
        return self._log_exit(outcome)

    def maintain_held_position(
        self,
        position: dict[str, Any],
        mark: object,
        *,
        persist: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        """Requery or place the spot OCO, else fall back to the market exit.

        ``persist`` runs before the OCO POST so a crash still has the client
        id to requery. Exchange coverage is recorded only after a working
        list is observed. A timeout does not market-sell.
        """
        row = dict(position)

        def save(fields: dict[str, Any]) -> None:
            if persist is None:
                row.update(fields)
                return
            saved = persist(fields)
            if isinstance(saved, dict):
                row.clear()
                row.update(saved)
            else:
                row.update(fields)

        if not tracked_exit(row):
            return self._coverage(
                row,
                book="unchanged",
                error="untracked_exit",
                submitted=False,
            )
        status = str(row.get("status") or "")
        if status == "qty_unconfirmed":
            return self._coverage(
                row,
                book="unchanged",
                error="exit_qty_unconfirmed",
                submitted=False,
            )
        if status in {"closed_dust", "exit_submitted", "oco_filled"}:
            return self._coverage(
                row,
                book="unchanged",
                error="position_not_open",
                submitted=False,
                ok=True,
            )

        protection = str(row.get("protection") or "")
        if status in {"oco_unknown", "oco_working"} or protection in {
            PROTECTION_PENDING,
            PROTECTION_EXCHANGE,
        }:
            recon = self._reconcile_oco(row)
            state = str(recon.get("state") or "")
            if state == "working":
                save(self._oco_working_fields(row, recon))
                return self._coverage(
                    row,
                    book="oco_working",
                    submitted=False,
                    ok=True,
                    protection=PROTECTION_EXCHANGE,
                    error=None,
                )
            if state == "filled":
                save(
                    {
                        "status": "oco_filled",
                        "protection": PROTECTION_EXCHANGE,
                        "exit_reason": recon.get("exit_reason"),
                        "exit_order_id": recon.get("filled_order_id"),
                        "oco_list_status": recon.get("list_status"),
                        "oco_order_list_id": recon.get("order_list_id"),
                        "oco_above_order_id": recon.get("above_order_id"),
                        "oco_below_order_id": recon.get("below_order_id"),
                        "last_error": None,
                    }
                )
                return self._coverage(
                    row,
                    book="oco_filled",
                    submitted=False,
                    ok=True,
                    protection=PROTECTION_EXCHANGE,
                    error=None,
                    reason=recon.get("exit_reason"),
                )
            if state == "requery_failed":
                save({"last_error": recon.get("error") or "oco_requery_failed"})
                return self._coverage(
                    row,
                    book="oco_unknown",
                    submitted=False,
                    error=recon.get("error") or "oco_requery_failed",
                    protection=row.get("protection") or PROTECTION_PENDING,
                )
            if state == "absent" and status == "oco_working":
                save({"last_error": "oco_requery_missing"})
                return self._coverage(
                    row,
                    book="oco_working",
                    submitted=False,
                    error="oco_requery_missing",
                    protection=PROTECTION_EXCHANGE,
                )
            if state == "absent" and status != "oco_unknown":
                save({"last_error": "oco_requery_missing"})
                return self._coverage(
                    row,
                    book="oco_unknown",
                    submitted=False,
                    error="oco_requery_missing",
                    protection=PROTECTION_PENDING,
                )
            if state in {"unfilled", "partial"}:
                remainder = recon.get("remainder_qty") or row.get("quantity")
                detail = (
                    "oco_partial_remainder" if state == "partial" else "oco_terminal_unfilled"
                )
                if state == "partial" and dust_detail("oco_qty_below_min") and self._remainder_is_dust(
                    row, str(remainder), mark
                ):
                    fields = dust_archive_fields(
                        quantity=str(remainder),
                        mark=mark,
                        stop=row.get("stop_price"),
                        detail="oco_partial_remainder",
                    )
                    save(fields)
                    return self._coverage(
                        row,
                        book="closed_dust",
                        submitted=False,
                        error="dust_untradable",
                        protection=PROTECTION_DUST,
                        reason="dust_abandon",
                    )
                save(
                    {
                        "status": "open",
                        "protection": PROTECTION_FALLBACK,
                        "quantity": str(remainder) if remainder is not None else row.get("quantity"),
                        "oco_list_status": recon.get("list_status"),
                        "last_error": detail,
                    }
                )
            elif state == "absent":
                # oco_unknown and the list is not on the book. Place below.
                pass
            elif state:
                save({"last_error": recon.get("error") or "oco_requery_failed"})
                return self._coverage(
                    row,
                    book="oco_unknown",
                    submitted=False,
                    error=recon.get("error") or "oco_requery_failed",
                    protection=PROTECTION_PENDING,
                )

        if str(row.get("protection") or "") != PROTECTION_FALLBACK and str(row.get("status") or "") == "open":
            placed = self._try_place_oco(row, mark, save)
            if placed is not None:
                return placed

        outcome = self.exit_held_position(row, mark)
        if outcome.get("book") == "closed_dust":
            fields = dust_archive_fields(
                quantity=str(outcome.get("residual_qty") or row.get("quantity") or ""),
                mark=mark,
                stop=row.get("stop_price"),
                detail=str(outcome.get("dust_detail") or "exit_qty_below_min"),
            )
            save(fields)
            outcome["protection"] = PROTECTION_DUST
            outcome["reason"] = "dust_abandon"
            return outcome
        if outcome.get("book") == "error_only" or (
            outcome.get("error") and outcome.get("book") == "unchanged"
        ):
            outcome["book"] = "error_only"
        return outcome

    def _try_place_oco(
        self,
        row: dict[str, Any],
        mark: object,
        save: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any] | None:
        """Place the list, or return None so the loop fallback can run.

        A returned dict is terminal for this tick (resting, unknown, blocked).
        """
        gates = self._gates()
        client = self.client
        if client is None or not callable(getattr(client, "new_oco", None)):
            save(
                {
                    "status": "open",
                    "protection": PROTECTION_FALLBACK,
                    "last_error": "oco_client_unsupported",
                }
            )
            return None
        blocked = protective_order_block(gates)
        if blocked:
            return self._coverage(
                row,
                book="blocked",
                submitted=False,
                error=blocked,
                protection=row.get("protection"),
            )
        info, info_error = self._load_symbol_info(row, gates)
        if info_error or not isinstance(info, dict):
            save(
                {
                    "status": "open",
                    "protection": PROTECTION_FALLBACK,
                    "last_error": info_error or "oco_missing_lot_filter",
                }
            )
            return None
        filters = _symbol_filters(info, str(row.get("symbol") or ""))
        if not filters:
            save(
                {
                    "status": "open",
                    "protection": PROTECTION_FALLBACK,
                    "last_error": "oco_missing_lot_filter",
                }
            )
            return None
        spec, spec_error = build_spot_oco(
            symbol=str(row.get("symbol") or ""),
            quantity=str(row.get("quantity") or ""),
            stop_price=str(row.get("stop_price") or ""),
            take_profit_price=str(row.get("take_profit_price") or ""),
            decision_id=row.get("decision_log_id"),
            filters=filters,
            mark=mark,
            timestamp_ms=int(self.clock_ms()),
        )
        if spec_error in {"oco_qty_below_min"} or dust_detail(spec_error):
            fields = dust_archive_fields(
                quantity=str(row.get("quantity") or ""),
                mark=mark,
                stop=row.get("stop_price"),
                detail=spec_error or "oco_qty_below_min",
            )
            save(fields)
            return self._coverage(
                row,
                book="closed_dust",
                submitted=False,
                error="dust_untradable",
                protection=PROTECTION_DUST,
                reason="dust_abandon",
            )
        if spec is None or spec_error:
            save(
                {
                    "status": "open",
                    "protection": PROTECTION_FALLBACK,
                    "last_error": spec_error or "oco_rejected",
                }
            )
            return None
        key, secret = self._credentials()
        if not key or not secret:
            return self._coverage(
                row,
                book="blocked",
                submitted=False,
                error="missing_api_credentials",
            )
        save(
            {
                "status": "oco_unknown",
                "protection": PROTECTION_PENDING,
                "oco_list_client_id": spec["list_client_id"],
                "oco_above_client_id": spec["above_client_id"],
                "oco_below_client_id": spec["below_client_id"],
                "quantity": spec["quantity"],
                "residual_qty": spec.get("residual_qty"),
                "residual_quote": None
                if not spec.get("residual_qty")
                else dust_archive_fields(
                    quantity=str(spec["residual_qty"]),
                    mark=mark,
                    stop=row.get("stop_price"),
                    detail="oco_lot_residual",
                )["residual_quote"],
                "last_error": None,
            }
        )
        signed = sign_query(spec["pairs"], secret)
        try:
            placed = client.new_oco(signed, api_key=key)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, AssertionError):
                raise
            found = self._reconcile_oco(row)
            if found.get("state") == "working":
                save(self._oco_working_fields(row, found))
                return self._coverage(
                    row,
                    book="oco_working",
                    submitted=True,
                    ok=True,
                    protection=PROTECTION_EXCHANGE,
                    error=None,
                )
            if is_request_timeout(exc) or not isinstance(exc, BinanceRequestError):
                save(
                    {
                        "status": "oco_unknown",
                        "protection": PROTECTION_PENDING,
                        "last_error": "oco_submit_timeout",
                    }
                )
                return self._coverage(
                    row,
                    book="oco_unknown",
                    submitted=False,
                    error="oco_submit_timeout",
                    protection=PROTECTION_PENDING,
                )
            save(
                {
                    "status": "open",
                    "protection": PROTECTION_FALLBACK,
                    "last_error": f"oco_rejected:{_public_error(exc, key, secret)}",
                }
            )
            return None
        if not isinstance(placed, dict) or placed.get("orderListId") is None:
            save(
                {
                    "status": "open",
                    "protection": PROTECTION_FALLBACK,
                    "last_error": "oco_rejected:missing_order_list_id",
                }
            )
            return None
        classified = classify_oco(placed, held_qty=str(spec["quantity"]))
        if classified.get("state") not in {"working", "partial_working", "filled"}:
            save(
                {
                    "status": "open",
                    "protection": PROTECTION_FALLBACK,
                    "oco_order_list_id": classified.get("order_list_id"),
                    "oco_list_status": classified.get("list_status"),
                    "last_error": "oco_rejected:not_working",
                }
            )
            return None
        fields = self._oco_working_fields(row, classified)
        fields["quantity"] = spec["quantity"]
        if spec.get("residual_qty"):
            fields["residual_qty"] = spec["residual_qty"]
        save(fields)
        return self._coverage(
            row,
            book="oco_working",
            submitted=True,
            ok=True,
            protection=PROTECTION_EXCHANGE,
            error=None,
            order=dict(spec["pairs"]),
            quantity=spec["quantity"],
        )

    def _reconcile_oco(self, row: dict[str, Any]) -> dict[str, Any]:
        gates = self._gates()
        if not gates.get("live_enabled"):
            return {"state": "requery_failed", "error": "binance_spot_live_disabled"}
        client = self.client
        query = getattr(client, "get_order_list", None) if client is not None else None
        if not callable(query):
            return {"state": "requery_failed", "error": "oco_requery_unsupported"}
        key, secret = self._credentials()
        if not key or not secret:
            return {"state": "requery_failed", "error": "missing_api_credentials"}
        symbol = str(row.get("symbol") or "")
        list_client = row.get("oco_list_client_id")
        list_order = row.get("oco_order_list_id")
        if list_order not in (None, ""):
            ident = ("orderListId", str(list_order))
        elif list_client not in (None, ""):
            ident = ("origClientOrderId", str(list_client))
        else:
            return {"state": "absent", "error": "oco_requery_missing"}
        pairs = [
            ident,
            ("recvWindow", RECV_WINDOW_MS),
            ("timestamp", str(int(self.clock_ms()))),
        ]
        try:
            payload = query(sign_query(pairs, secret), api_key=key)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, AssertionError):
                raise
            if isinstance(exc, BinanceRequestError) and exc.status in {400, 404}:
                return {"state": "absent", "error": "oco_not_found"}
            return {"state": "requery_failed", "error": _public_error(exc, key, secret)}
        if not isinstance(payload, dict):
            return {"state": "requery_failed", "error": "oco_requery_failed"}
        if not payload.get("orderReports"):
            legs = self._load_oco_legs(payload, symbol, key, secret)
            if legs is None and not oco_is_working(payload):
                return {"state": "requery_failed", "error": "oco_leg_requery_failed"}
            if legs:
                payload = dict(payload)
                payload["orderReports"] = legs
        classified = classify_oco(payload, held_qty=str(row.get("quantity") or ""))
        classified["error"] = None
        return classified

    def _load_oco_legs(
        self,
        payload: dict[str, Any],
        symbol: str,
        key: str,
        secret: str,
    ) -> list[dict[str, Any]] | None:
        client = self.client
        lookup = getattr(client, "get_order", None) if client is not None else None
        if not callable(lookup):
            return None
        above_id, below_id = oco_leg_ids(payload)
        legs: list[dict[str, Any]] = []
        for order_id in (above_id, below_id):
            if not order_id:
                continue
            pairs = [
                ("symbol", symbol),
                ("orderId", order_id),
                ("recvWindow", RECV_WINDOW_MS),
                ("timestamp", str(int(self.clock_ms()))),
            ]
            try:
                leg = lookup(sign_query(pairs, secret), api_key=key)
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, AssertionError):
                    raise
                return None
            if not isinstance(leg, dict):
                return None
            legs.append(leg)
        return legs or None

    def _load_symbol_info(
        self,
        row: dict[str, Any],
        gates: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        blocked = protective_order_block(gates)
        if blocked:
            return None, blocked
        symbol = str(row.get("symbol") or "")
        client = self.client if self.client is not None else BinanceSpotHttpClient()
        try:
            info = client.exchange_info(symbol)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, AssertionError):
                raise
            key, secret = self._credentials()
            return None, _public_error(exc, key, secret)
        if not isinstance(info, dict) or not symbol_status_trading(info, symbol):
            return None, "symbol_not_trading"
        return info, None

    def _remainder_is_dust(self, row: dict[str, Any], quantity: str, mark: object) -> bool:
        info, error = self._load_symbol_info(row, self._gates())
        if error or not isinstance(info, dict):
            return False
        _sized, size_error = size_exit_quantity(info, str(row.get("symbol") or ""), quantity, mark)
        return dust_detail(size_error) is not None

    def _oco_working_fields(self, row: dict[str, Any], recon: dict[str, Any]) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "status": "oco_working",
            "protection": PROTECTION_EXCHANGE,
            "oco_list_status": recon.get("list_status") or "EXECUTING",
            "last_error": None,
        }
        if recon.get("order_list_id"):
            fields["oco_order_list_id"] = recon.get("order_list_id")
        if recon.get("above_order_id"):
            fields["oco_above_order_id"] = recon.get("above_order_id")
        if recon.get("below_order_id"):
            fields["oco_below_order_id"] = recon.get("below_order_id")
        if row.get("oco_list_client_id"):
            fields["oco_list_client_id"] = row.get("oco_list_client_id")
        return fields

    def _coverage(
        self,
        row: dict[str, Any],
        *,
        book: str,
        submitted: bool,
        error: str | None,
        ok: bool = False,
        protection: object = None,
        reason: object = None,
        order: dict[str, str] | None = None,
        quantity: str | None = None,
    ) -> dict[str, Any]:
        return {
            "ok": ok,
            "dry_run": False,
            "executed": 0,
            "submitted": submitted,
            "error": error,
            "reason": reason,
            "decision_log_id": row.get("decision_log_id"),
            "executor": EXECUTOR_NAME,
            "symbol": row.get("symbol"),
            "order": order,
            "exchange_order_id": None,
            "gates": self._gates(),
            "quantity": quantity if quantity is not None else row.get("quantity"),
            "protection": protection if protection is not None else row.get("protection"),
            "book": book,
            "coverage": protection if protection is not None else row.get("protection"),
        }

    def _lookup_exit_order(
        self,
        client: Any,
        symbol: str,
        client_id: str,
        key: str,
        secret: str,
    ) -> dict[str, Any] | None:
        """After a failed POST, adopt the order if this client id already exists."""
        lookup = getattr(client, "get_order", None)
        if not callable(lookup):
            return None
        pairs = [
            ("symbol", symbol),
            ("origClientOrderId", client_id),
            ("recvWindow", RECV_WINDOW_MS),
            ("timestamp", str(int(self.clock_ms()))),
        ]
        try:
            placed = lookup(sign_query(pairs, secret), api_key=key)
        except Exception as exc:  # noqa: BLE001 — lookup failure leaves the sell unconfirmed
            if isinstance(exc, AssertionError):
                raise
            return None
        if not isinstance(placed, dict) or _exit_order_accepted(placed) is None:
            return None
        return placed

    def _log_exit(self, outcome: dict[str, Any]) -> dict[str, Any]:
        log.info(
            "binance_spot_exit symbol=%s reason=%s submitted=%s error=%s qty=%s",
            outcome.get("symbol"),
            outcome.get("reason"),
            outcome.get("submitted"),
            outcome.get("error"),
            outcome.get("quantity"),
        )
        return outcome

    def _finish(self, result: dict[str, Any]) -> dict[str, Any]:
        plan = getattr(self, "_size_plan", None)
        if isinstance(plan, dict):
            result["size_plan"] = {
                key: value
                for key, value in plan.items()
                if key != "needs_lot_floor"
            }
        log.info(
            "binance_spot symbol=%s dry_run=%s ok=%s submitted=%s error=%s",
            result.get("symbol"),
            result.get("dry_run"),
            result.get("ok"),
            result.get("submitted"),
            result.get("error"),
        )
        return result


def _public_error(exc: BaseException, key: str, secret: str) -> str:
    if isinstance(exc, BinanceRequestError):
        return _redact(str(exc), (key, secret))
    text = _redact(f"{type(exc).__name__}", (key, secret))
    return f"binance_http_error:{text}"
