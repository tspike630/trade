"""Binance spot USDT adapter for ``live_bnb_boll_fade`` and ``live_bnb_boll_classic``.

Any other purpose is refused with ``purpose_not_live_bnb_boll_fade``. Classic
reads the signal's absolute stop and take-profit, or the
``classic_fixed_tp_sl`` shell (TP 10% / SL 5%) when those prices are absent.
The fade entry-zone recipe is unchanged.

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

A live buy is not a completed consumer event until ``POST /api/v3/orderList/oco``
returns a working list: ``orderListId >= 0`` and both leg order ids ``>= 0``.
``spot.newOrder`` ``MARKET`` / ``STOP_LOSS`` / ``TAKE_PROFIT`` is not that
list. Dry-run never calls HTTP. This module does not build an HTTP client
for the entry; tests inject one. No mark-price loop sells a position.

``LIVE_TRADING_DISABLED`` blocks a new buy only. A ledger-tracked protective
sell still goes out unless ``BINANCE_SPOT_BLOCK_SELL_ON_KILL=1``.
``BINANCE_SPOT_LIVE_ENABLED`` must also be on. Neither flag is enabled here.
The regular API-key book is not MCP uid ``1289917075``.
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
from datetime import datetime, timezone
import urllib.parse
import urllib.request
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Literal

from logging_setup import get_logger

from execution.executors.binance_spot_gate import (
    PURPOSE_LIVE_BNB_BOLL_CLASSIC,
    PURPOSE_LIVE_BNB_BOLL_FADE,
    is_live_bnb_spot_purpose,
)

log = get_logger("execution.binance_spot")

EXECUTOR_NAME = "binance_spot"
ENABLE_ENV = "BINANCE_SPOT_LIVE_ENABLED"
KILL_SWITCH_ENV = "LIVE_KILL_SWITCH_PATH"
DEFAULT_KILL_SWITCH_NAME = "LIVE_TRADING_DISABLED"
API_KEY_ENV = ("BINANCE_API_KEY", "LIVE_EXCHANGE_API_KEY")
API_SECRET_ENV = ("BINANCE_API_SECRET", "LIVE_EXCHANGE_API_SECRET")
DEFAULT_BASE_URL = "https://api.binance.com"
ACCOUNT_PATH = "/api/v3/account"
API_RESTRICTIONS_PATH = "/sapi/v1/account/apiRestrictions"
SPOT_PATH = "/api/v3/order"
TRADES_PATH = "/api/v3/myTrades"
MY_TRADES_PATH = TRADES_PATH
EXCHANGE_INFO_PATH = "/api/v3/exchangeInfo"
TICKER_PATH = "/api/v3/ticker/price"
OCO_PATH = "/api/v3/orderList/oco"
ORDER_LIST_PATH = "/api/v3/orderList"
RECV_WINDOW_MS = "5000"
LIVE_MODE = "live_stub"
DRY_MODE = "dry_run"
HELD_EXIT_PROTECTION = "held_position_exit"
_EXIT_OPEN_STATUS = frozenset({"NEW", "PARTIALLY_FILLED", "PENDING_NEW"})
_EXIT_FILLED_STATUS = frozenset({"FILLED"})
_EXIT_FAIL_STATUS = frozenset(
    {"REJECTED", "EXPIRED", "EXPIRED_IN_MATCH", "CANCELED", "CANCELLED"}
)
_ExitReason = Literal["stop_loss", "take_profit"]

# Same base shape as decision_engine.core.bnb_spot_map, but the order id must
# already be the concatenated BASEUSDT form. Slash / colon names are rejected.
_BASE_MIN = 2
_BASE_MAX = 20
_NOT_A_BASE = frozenset(
    {"USDT", "USD", "USDC", "FDUSD", "BUSD", "TUSD", "DAI", "USDP"}
)
_DECIMAL = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d+)?$")
_OrderType = Literal["MARKET", "LIMIT", "LIMIT_MAKER"]
_Side = Literal["BUY", "SELL"]

Opener = Callable[[urllib.request.Request, float], tuple[int, bytes]]


_DIAGNOSTIC_LIMIT = 160
_BODY_PREFIX = 4096
_CODE_MIN = -1_000_000
_CODE_MAX = 1_000_000
_SAFE_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 .,;:_%+=()/'\"*-"
)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_LABELED_SECRET_RE = re.compile(
    r"(?i)\b(?:signature|api[_-]?key|api[_-]?secret|secret|x-mbx-apikey|authorization|bearer|token)\b\s*[:=]\s*\S+"
)
_QUERY_RE = re.compile(
    r"(?i)(?:[?&])(?:signature|timestamp|recvwindow|apikey|api_key|api-key)=[^&\s]*"
)
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9]{32,}\b")
# Leftover request material after the labeled-value scrub. Bare words such as
# "API-key" and "Signature for this request is not valid." are Binance msgs.
_UNSAFE_MARKER_RE = re.compile(r"(?i)(https?://|x-mbx-apikey|authorization\s*:|bearer\s+\S)")


def _valid_exchange_code(raw: object) -> bool:
    return isinstance(raw, int) and not isinstance(raw, bool) and _CODE_MIN <= raw <= _CODE_MAX


def _scrub_diagnostic(text: object, secrets: tuple[str, ...] = ()) -> str | None:
    """Bound a Binance ``msg`` or fallback excerpt. Drop URLs, queries, and secrets."""
    if not isinstance(text, str) or not text:
        return None
    cleaned = text
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        cleaned = cleaned.replace(secret, " ")
    cleaned = _URL_RE.sub(" ", cleaned)
    cleaned = _QUERY_RE.sub(" ", cleaned)
    cleaned = _LABELED_SECRET_RE.sub(" ", cleaned)
    cleaned = _LONG_TOKEN_RE.sub(" ", cleaned)
    cleaned = "".join(ch if ch in _SAFE_CHARS else " " for ch in cleaned)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > _DIAGNOSTIC_LIMIT:
        cleaned = cleaned[:_DIAGNOSTIC_LIMIT].rstrip()
    if not cleaned or not any(ch.isalnum() for ch in cleaned):
        return None
    if _UNSAFE_MARKER_RE.search(cleaned):
        return None
    return cleaned


def _format_exchange_error(
    status: int,
    code: int | None,
    message: str | None,
    detail: str | None,
) -> str:
    base = f"binance_http_error:{status}"
    if code is not None and message:
        return f"{base}:{code}:{message}"
    if code is not None:
        return f"{base}:{code}"
    if message:
        return f"{base}:{message}"
    if detail:
        return f"{base}:detail:{detail}"
    return base


class BinanceRequestError(Exception):
    """HTTP failure. Status is always kept. Code and msg are optional and scrubbed.

    A body-less error stays ``binance_http_error:<status>``. The request URL,
    query, signature, and API key are not part of the message. ``msg`` is the
    keyword order-list checks already pass; ``message`` is the body-decoder alias.
    """

    def __init__(
        self,
        status: int,
        *,
        code: int | None = None,
        msg: str | None = None,
        message: str | None = None,
        detail: str | None = None,
        secrets: tuple[str, ...] = (),
    ) -> None:
        self.status = int(status)
        self.code = code if _valid_exchange_code(code) else None
        raw_message = message if message is not None else msg
        self.exchange_message = _scrub_diagnostic(raw_message, secrets)
        self.msg = self.exchange_message
        self.detail = _scrub_diagnostic(detail, secrets)
        super().__init__(
            _format_exchange_error(
                self.status,
                self.code,
                self.exchange_message,
                self.detail,
            )
        )

    def public_view(self, secrets: tuple[str, ...] = ()) -> dict[str, Any]:
        """Status plus a scrubbed code/msg/detail. Other response fields are dropped."""
        msg = _scrub_diagnostic(self.exchange_message, secrets) if self.exchange_message else None
        detail = _scrub_diagnostic(self.detail, secrets) if self.detail else None
        payload: dict[str, Any] = {"status": self.status}
        if self.code is not None:
            payload["code"] = self.code
        if msg:
            payload["msg"] = msg
        if detail:
            payload["detail"] = detail
        return payload

    def redacted_text(self, secrets: tuple[str, ...] = ()) -> str:
        view = self.public_view(secrets)
        code = view.get("code")
        msg = view.get("msg")
        detail = view.get("detail")
        return _format_exchange_error(
            self.status,
            code if _valid_exchange_code(code) else None,
            msg if isinstance(msg, str) else None,
            detail if isinstance(detail, str) else None,
        )


def public_exchange_error(raw: object) -> dict[str, Any] | None:
    """Receipt copy of an exchange rejection. Only status, code, msg, and detail."""
    if not isinstance(raw, dict):
        return None
    status = raw.get("status")
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        return None
    out: dict[str, Any] = {"status": status}
    code = raw.get("code")
    if _valid_exchange_code(code):
        out["code"] = code
    msg = _scrub_diagnostic(raw.get("msg"))
    if msg:
        out["msg"] = msg
    detail = _scrub_diagnostic(raw.get("detail"))
    if detail:
        out["detail"] = detail
    return out


# GET /api/v3/orderList only. -2018 is "Order list does not exist."
# -2013 is GET /api/v3/order ("Order does not exist") for one leg.
# -2011 is cancel ("Unknown order sent"). Neither means the list is gone.
_ORDER_LIST_ABSENT_CODE = -2018
_SINGLE_ORDER_ABSENT_CODES = frozenset({-2011, -2013})


def order_list_absent(exc: BinanceRequestError) -> bool:
    """True only for a definitive miss on ``GET /api/v3/orderList``.

    A bare 400, 401, 418, 429, ``-1003``, ``-2015``, ``-2013``, or ``-2011``
    is not absence. Those must not start a second OCO.
    """
    return exc.status in {400, 404} and exc.code == _ORDER_LIST_ABSENT_CODE


def single_order_absent(exc: BinanceRequestError) -> bool:
    """True when ``GET /api/v3/order`` says that one leg id is gone.

    This does not say the OCO list is gone. A working list stays working.
    """
    return exc.status in {400, 404} and exc.code in _SINGLE_ORDER_ABSENT_CODES


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


def _maker_entry_lifecycle_ready(client: Any) -> bool:
    """A resting buy can be submitted only when the fill can be canceled and protected."""
    return all(
        callable(getattr(client, name, None))
        for name in ("get_order", "cancel_order", "new_oco")
    )


def _apply_recommended_order_type(extras: dict[str, Any], action: object) -> dict[str, Any]:
    """Apply the published recommendation over the strategy's default market type.

    ``LIMIT`` already chosen on the signal stays. ``NONE`` and a missing
    recommendation leave the signal's own type, which defaults to market.
    A recommendation that needs a limit and has none uses ``price_usd``.
    """
    if str(action or "").strip().lower() != "buy":
        return extras
    recommended = str(extras.get("recommended_order_type") or "").strip().upper()
    if recommended in {"", "NONE"} or recommended not in {"LIMIT", "LIMIT_MAKER"}:
        return extras
    explicit = extras.get("order_type", extras.get("binance_order_type"))
    explicit_text = "" if explicit is None else str(explicit).strip().upper()
    if explicit_text == "LIMIT":
        return extras
    copied = dict(extras)
    copied["order_type"] = recommended
    chosen, _price_error = _chosen_limit_raw(copied)
    if chosen in (None, ""):
        for key in ("price_usd", "entry_price"):
            if _format_positive(extras.get(key)):
                copied["price"] = extras.get(key)
                break
    return copied


def _parse_order_type(raw: object) -> _OrderType | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "MARKET"
    text = str(raw).strip().upper()
    if text == "MARKET":
        return "MARKET"
    if text == "LIMIT":
        return "LIMIT"
    if text == "LIMIT_MAKER":
        return "LIMIT_MAKER"
    return None


def _row_epoch_ms(row: dict[str, Any]) -> int | None:
    """Entry clock in milliseconds. Ledger ``created_at`` is the restart fallback."""
    direct = _finite_number(row.get("created_at_ms"))
    if direct is not None and direct > 0:
        return int(direct)
    text = row.get("created_at")
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        parsed = datetime.strptime(text.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(parsed.timestamp() * 1000)


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
    list after the fill. It does not market-sell from a watched mark.
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
    if order_type in ("LIMIT", "LIMIT_MAKER"):
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


def _protection_incident(state: str) -> dict[str, Any]:
    """Local evidence only. Nothing is sent to an operator or an outside service.

    The durable copy is the position row plus the gateway ``order_events``
    ``rejected`` detail. A log line is not that record.
    """
    return {
        "state": state,
        "store": "binance_spot_positions",
        "event": "rejected",
        "delivered": False,
        "transport": "none",
        "integrations_required": (
            "Read binance_spot_positions and order_events. "
            "No webhook, email, or chat message is sent."
        ),
    }


def _trade_proof_fields(trade: dict[str, Any]) -> tuple[str, str, str, object] | None:
    """Qty, commission, asset, and side. None when the row cannot be compared."""
    qty = _nonneg_decimal(trade.get("qty"))
    fee = _nonneg_decimal(trade.get("commission"))
    asset = str(trade.get("commissionAsset") or "").strip().upper()
    if qty is None or qty <= 0 or fee is None or not asset:
        return None
    return (format(qty, "f"), format(fee, "f"), asset, trade.get("isBuyer"))


def _fill_trade_id(fill: dict[str, Any]) -> str | None:
    raw = fill.get("tradeId")
    if raw is None:
        raw = fill.get("id")
    if isinstance(raw, bool) or raw is None:
        return None
    text = str(raw).strip()
    if not text.isdigit() or int(text) <= 0:
        return None
    return str(int(text))


def _receipt_fills_match(fills: object, trades: list[dict[str, Any]], *, side: str) -> bool:
    """Order ``fills`` and ``myTrades`` name the same trades, or there is no receipt.

    An empty or missing fill list is not proof and does not skip ``myTrades``.
    A non-empty list must use the same numeric ids and the same qty, fee, and
    asset, and a side on either copy must match.
    """
    if not isinstance(fills, list) or not fills:
        return True
    wanted = side.strip().upper()
    receipt: dict[str, tuple[str, str, str]] = {}
    for fill in fills:
        if not isinstance(fill, dict):
            return False
        trade_id = _fill_trade_id(fill)
        qty = _nonneg_decimal(fill.get("qty"))
        fee = _nonneg_decimal(fill.get("commission"))
        asset = str(fill.get("commissionAsset") or "").strip().upper()
        if trade_id is None or qty is None or qty <= 0 or fee is None or not asset:
            return False
        if "isBuyer" in fill and not isinstance(fill.get("isBuyer"), bool):
            return False
        if isinstance(fill.get("isBuyer"), bool):
            buyer = fill["isBuyer"] is True
            if wanted == "BUY" and not buyer:
                return False
            if wanted == "SELL" and buyer:
                return False
        if "side" in fill and str(fill.get("side") or "").strip().upper() != wanted:
            return False
        signature = (format(qty, "f"), format(fee, "f"), asset)
        if trade_id in receipt and receipt[trade_id] != signature:
            return False
        receipt[trade_id] = signature
    exchange: dict[str, tuple[str, str, str]] = {}
    for trade in trades:
        if not isinstance(trade, dict):
            return False
        trade_id = _fill_trade_id({"id": trade.get("id")})
        proof = _trade_proof_fields(trade)
        if trade_id is None or proof is None:
            return False
        exchange[trade_id] = proof[:3]
    return receipt == exchange


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


CLASSIC_EXIT_SHELL = "classic_fixed_tp_sl"
_CLASSIC_TP_PCT = Decimal("10")
_CLASSIC_SL_PCT = Decimal("5")


def apply_classic_exit_shell(extras: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Fill classic stop/take from the 10/5 shell when the signal omitted them.

    Absolute prices already on the signal win. Fade never calls this. Returns
    the extras to plan with, and whether the shell supplied a missing price.
    """
    if str(extras.get("exit_shell") or "") != CLASSIC_EXIT_SHELL:
        return extras, False
    stop_missing = extras.get("stop_price") in (None, "")
    tp_missing = extras.get("take_profit_price") in (None, "")
    if not stop_missing and not tp_missing:
        return extras, False
    price = None
    for key in ("price_usd", "entry_price", "price"):
        if extras.get(key) in (None, ""):
            continue
        price = _to_decimal(extras.get(key))
        if price is not None:
            break
    if price is None:
        return extras, False
    filled = dict(extras)
    if stop_missing:
        filled["stop_price"] = _decimal_text(price * (Decimal(1) - _CLASSIC_SL_PCT / Decimal(100)))
    if tp_missing:
        filled["take_profit_price"] = _decimal_text(
            price * (Decimal(1) + _CLASSIC_TP_PCT / Decimal(100))
        )
    if filled.get("order_type") in (None, "") and filled.get("binance_order_type") in (None, ""):
        filled["order_type"] = "MARKET"
    return filled, True


# USDT quoteAssetPrecision. More fraction digits on quoteOrderQty is Binance -1111.
EXCHANGE_DECIMAL_PLACES = 8


def floor_decimal_places(number: object, places: int = EXCHANGE_DECIMAL_PLACES) -> str | None:
    """Floor to ``places`` fraction digits. Never rounds up and never uses float.

    A digit string that already fits is returned unchanged. Longer values are
    truncated the same way ``floor_base_qty`` truncates onto a lot step, so a
    ``size_pct * book`` product such as ``21.8819049515`` becomes ``21.88190495``.
    """
    if isinstance(number, str):
        text = number.strip()
        if _positive_decimal(text) is None:
            return None
        if len(text.partition(".")[2]) <= places:
            return text
        raw: object = text
    else:
        raw = number
    qty = _to_decimal(raw)
    if qty is None or places < 0:
        return None
    quantum = Decimal(1).scaleb(-places)
    floored = qty.quantize(quantum, rounding=ROUND_DOWN)
    if floored <= 0 or floored > qty:
        return None
    return _decimal_text(floored)


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

    Dry-run and unfilled acks return None. Base size comes from
    ``sellable_qty``, the verified fee-net. Gross ``filled_qty`` is not a
    substitute. Missing size is ``qty_unconfirmed`` and is not sellable.
    """
    arm = result.get("arm_position")
    if isinstance(arm, dict):
        return arm
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
        "purpose": str(sig.get("purpose") or "").strip() or PURPOSE_LIVE_BNB_BOLL_FADE,
    }


def _exit_order_id_text(placed: dict[str, Any]) -> str | None:
    raw = placed.get("orderId")
    if isinstance(raw, bool) or raw is None:
        return None
    text = str(raw).strip()
    if not text.isdigit() or int(text) <= 0:
        return None
    return str(int(text))


def _exit_order_status(placed: dict[str, Any]) -> str:
    return str(placed.get("status") or "").strip().upper()


def _exit_order_accepted(placed: dict[str, Any]) -> str | None:
    """Order id only for an open sell or a full fill.

    A missing, blank, or unknown status is unresolved. A rejected, expired,
    or canceled order is terminal and is not accepted here.
    """
    status = _exit_order_status(placed)
    if status not in _EXIT_OPEN_STATUS and status not in _EXIT_FILLED_STATUS:
        return None
    return _exit_order_id_text(placed)


def _exit_order_identity(
    placed: dict[str, Any],
    *,
    client_id: str,
    entry_order_id: object = None,
    symbol: object = None,
) -> bool:
    """True when this body is the flatten sell for this position."""
    ord_id = _exit_order_id_text(placed)
    if ord_id is None:
        return False
    entry_id = str(entry_order_id or "").strip()
    if entry_id and ord_id == entry_id:
        return False
    side = str(placed.get("side") or "").strip().upper()
    if side and side != "SELL":
        return False
    for key in ("clientOrderId", "origClientOrderId"):
        reported = str(placed.get(key) or "").strip()
        if reported and reported != client_id:
            return False
    reported_symbol = str(placed.get("symbol") or "").strip().upper()
    wanted = str(symbol or "").strip().upper()
    if reported_symbol and wanted and reported_symbol != wanted:
        return False
    return True


def _exit_order_matches_lookup(
    placed: dict[str, Any],
    *,
    client_id: str,
    entry_order_id: object = None,
    symbol: object = None,
) -> bool:
    if not _exit_order_identity(
        placed,
        client_id=client_id,
        entry_order_id=entry_order_id,
        symbol=symbol,
    ):
        return False
    return _exit_order_accepted(placed) is not None


def _terminal_fail_sell(
    placed: dict[str, Any],
    *,
    client_id: str,
    entry_order_id: object = None,
    symbol: object = None,
) -> bool:
    """Identity-matched terminal sell that already carries ``executedQty``.

    Missing quantity stays on the settle path. A present but illegal quantity
    is still this shape; the recorder fail-closes it and does not treat it as zero.
    """
    if "executedQty" not in placed:
        return False
    if _exit_order_status(placed) not in _EXIT_FAIL_STATUS:
        return False
    if str(placed.get("side") or "").strip().upper() != "SELL":
        return False
    reported_symbol = str(placed.get("symbol") or "").strip().upper()
    wanted = str(symbol or "").strip().upper()
    if not reported_symbol or not wanted or reported_symbol != wanted:
        return False
    reported_client = str(
        placed.get("clientOrderId") or placed.get("origClientOrderId") or ""
    ).strip()
    if not reported_client or reported_client != client_id:
        return False
    return _exit_order_identity(
        placed,
        client_id=client_id,
        entry_order_id=entry_order_id,
        symbol=symbol,
    )


def _entry_intent(
    extras: dict[str, Any],
    *,
    side: str,
    order_type: _OrderType,
    order: dict[str, str] | None,
) -> dict[str, Any]:
    limit_raw, _error = _chosen_limit_raw(extras)
    limit_text = _format_positive(limit_raw) if order_type in ("LIMIT", "LIMIT_MAKER") else None
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

    def get_account(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        """Signed read of ``GET /api/v3/account``. Does not place an order."""
        request = urllib.request.Request(
            f"{self.base_url}{ACCOUNT_PATH}?{signed_query}",
            method="GET",
            headers={
                "User-Agent": "trade-binance-spot",
                "X-MBX-APIKEY": api_key,
            },
        )
        status, body = self._opener(request, self.timeout)
        return _decode_ok(status, body, secrets=(api_key,))

    def get_api_restrictions(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        """Signed read of ``GET /sapi/v1/account/apiRestrictions``. Not an order."""
        request = urllib.request.Request(
            f"{self.base_url}{API_RESTRICTIONS_PATH}?{signed_query}",
            method="GET",
            headers={
                "User-Agent": "trade-binance-spot",
                "X-MBX-APIKEY": api_key,
            },
        )
        status, body = self._opener(request, self.timeout)
        return _decode_ok(status, body, secrets=(api_key,))

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
        return _decode_ok(status, body, secrets=(api_key,))

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

    def order_book(self, symbol: str, *, limit: int = 20) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            (("symbol", symbol), ("limit", str(int(limit)))),
            quote_via=urllib.parse.quote,
            safe="",
        )
        request = urllib.request.Request(
            f"{self.base_url}/api/v3/depth?{query}",
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
        return _decode_ok(status, body, secrets=(api_key,))

    def my_trades(self, signed_query: str, *, api_key: str) -> Any:
        """One page of ``GET /api/v3/myTrades``. The caller paginates."""
        request = urllib.request.Request(
            f"{self.base_url}{MY_TRADES_PATH}?{signed_query}",
            method="GET",
            headers={
                "User-Agent": "trade-binance-spot",
                "X-MBX-APIKEY": api_key,
            },
        )
        status, body = self._opener(request, self.timeout)
        return _decode_ok_list(status, body, secrets=(api_key,))

    def cancel_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}{SPOT_PATH}?{signed_query}",
            method="DELETE",
            headers={
                "User-Agent": "trade-binance-spot",
                "X-MBX-APIKEY": api_key,
            },
        )
        status, body = self._opener(request, self.timeout)
        return _decode_ok(status, body, secrets=(api_key,))

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
        return _decode_ok(status, body, secrets=(api_key,))

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
        return _decode_ok(status, body, secrets=(api_key,))

    def cancel_order_list(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}{ORDER_LIST_PATH}?{signed_query}",
            method="DELETE",
            headers={
                "User-Agent": "trade-binance-spot",
                "X-MBX-APIKEY": api_key,
            },
        )
        status, body = self._opener(request, self.timeout)
        return _decode_ok(status, body, secrets=(api_key,))


def _decode_body_prefix(body: bytes) -> str | None:
    """UTF-8 prefix only. Invalid bytes contribute no text."""
    if not body:
        return None
    chunk = bytes(body[:_BODY_PREFIX])
    try:
        return chunk.decode("utf-8")
    except UnicodeDecodeError as exc:
        if len(body) > _BODY_PREFIX and exc.start > 0:
            try:
                return chunk[: exc.start].decode("utf-8")
            except UnicodeDecodeError:
                return None
        return None


def _rejection_from_body(
    status: int,
    body: object,
    secrets: tuple[str, ...],
) -> BinanceRequestError:
    if not isinstance(body, (bytes, bytearray)) or not body:
        return BinanceRequestError(status)
    text = _decode_body_prefix(bytes(body))
    if text is None:
        return BinanceRequestError(status)
    stripped = text.lstrip("\ufeff").strip()
    if not stripped:
        return BinanceRequestError(status)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return BinanceRequestError(status, detail=stripped, secrets=secrets)
    if not isinstance(parsed, dict):
        return BinanceRequestError(status)
    code = parsed.get("code")
    msg = parsed.get("msg")
    safe_code = code if _valid_exchange_code(code) else None
    safe_msg = msg if isinstance(msg, str) else None
    if safe_code is None and safe_msg is None:
        return BinanceRequestError(status)
    return BinanceRequestError(
        status,
        code=safe_code,
        message=safe_msg,
        secrets=secrets,
    )


def _decode_ok_list(
    status: int,
    body: bytes,
    *,
    secrets: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Decode ``GET /api/v3/myTrades``. The body is a JSON array, not a dict.

    Status ``>= 400`` uses the same rejection parser as the other routes.
    Any other body that is not a list of objects raises ``BinanceRequestError``.
    """
    if status >= 400:
        raise _rejection_from_body(status, body, secrets)
    try:
        parsed = json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BinanceRequestError(status) from exc
    if not isinstance(parsed, list) or any(not isinstance(item, dict) for item in parsed):
        raise BinanceRequestError(status)
    return parsed


def _decode_ok(
    status: int,
    body: bytes,
    *,
    secrets: tuple[str, ...] = (),
) -> dict[str, Any]:
    if status >= 400:
        raise _rejection_from_body(status, body, secrets)
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
                sent_quote = floor_decimal_places(quote_qty)
                if sent_quote is None:
                    return None, "invalid_order_qty"
                pairs.append(("quoteOrderQty", sent_quote))
            elif quantity:
                sent_qty = floor_decimal_places(quantity)
                if sent_qty is None:
                    return None, "invalid_order_qty"
                pairs.append(("quantity", sent_qty))
            else:
                return None, "missing_order_qty"
        elif side == "SELL":
            if quote_qty:
                return None, "ambiguous_order_qty"
            if not quantity:
                return None, "missing_order_qty"
            sent_qty = floor_decimal_places(quantity)
            if sent_qty is None:
                return None, "invalid_order_qty"
            pairs.append(("quantity", sent_qty))
        else:
            unexpected_side: never = side
            return None, f"invalid_side:{unexpected_side}"
    elif order_type in ("LIMIT", "LIMIT_MAKER"):
        if quote_qty:
            return None, "limit_rejects_quote_order_qty"
        if not quantity:
            return None, "missing_order_qty"
        if not price:
            return None, "missing_limit_price"
        sent_qty = floor_decimal_places(quantity)
        if sent_qty is None:
            return None, "invalid_order_qty"
        if order_type == "LIMIT":
            pairs.append(("timeInForce", "GTC"))
        pairs.extend(
            (
                ("quantity", sent_qty),
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
    consumer_allowed: bool | None = None,
    arm_position: dict[str, Any] | None = None,
    oco_fields: dict[str, Any] | None = None,
    exchange_error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
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
        "consumer_allowed": consumer_allowed is True,
    }
    if arm_position is not None:
        body["arm_position"] = arm_position
    if oco_fields is not None:
        body["oco_fields"] = oco_fields
    if exchange_error is not None:
        body["exchange_error"] = exchange_error
    return body


from execution.binance_ws_stream import default_max_age_ms  # noqa: E402
from execution.executors.binance_spot_gate import (  # noqa: E402
    MCP_POSITION_UID,
    OCO_READBACK_COLUMNS,
    OCO_READBACK_VERIFIED_STATUS,
    REST_BOOK,
    account_book_block,
    _FINAL_ORDER_STATUS,
    _nonneg_decimal,
    _nonneg_id,
    account_order_base,
    buy_exposure_block,
    final_executed_qty,
    hard_unresolved_halt,
    oco_readback_covers,
    row_unresolved_reason,
    cross_arm_symbol_block,
    funding_after_reservation,
    reserved_quote_usdt,
    native_oco_verified,
    replay_block,
    snapshot_usdt_free,
    spot_account_uid,
)
from execution.executors.binance_spot_protect import (  # noqa: E402
    PROTECTION_AUTO_FLATTEN,
    PROTECTION_DUST,
    PROTECTION_EXCHANGE,
    PROTECTION_FALLBACK,
    PROTECTION_PENDING,
    PROTECTION_TIME_STOP,
    _limit_lot,
    _tick_size,
    auto_flatten_on_oco_fail_enabled,
    block_sell_on_kill,
    build_spot_oco,
    _reports,
    classify_oco,
    compute_ratcheted_stop_price,
    dust_archive_fields,
    dust_detail,
    entry_notional_floor_error,
    entry_viability_error,
    entry_ttl_sec,
    evaluate_orderbook_impact,
    fill_average_price,
    flatten_client_order_id,
    MAX_HOLD_SEC_ENV,
    max_hold_sec,
    orderbook_gate_enabled,
    peg_maker_buy_price,
    post_only_limit_enabled,
    sor_maker_fallback_enabled,
    sor_maker_fallback_spec,
    trailing_ratchet_enabled,
    protective_band_error,
    buy_entry_oco_preflight,
    is_request_timeout,
    ENTRY_MIN_NOTIONAL_USDT,
    reanchor_oco_on_fill_enabled,
    reanchor_oco_prices_to_fill,
    resolve_entry_size,
    oco_is_working,
    oco_leg_ids,
    protective_order_block,
    sellable_base_qty,
    tracked_exit,
)


def _recon_payload(recon: dict[str, Any]) -> dict[str, Any]:
    return {
        "orderListId": recon.get("order_list_id"),
        "listOrderStatus": "EXECUTING",
        "listStatusType": "EXEC_STARTED",
        "orders": [
            {"orderId": recon.get("above_order_id")},
            {"orderId": recon.get("below_order_id")},
        ],
    }


def _oco_filters_ready(info: dict[str, Any], symbol: str) -> str | None:
    """Lot and tick must exist before a BUY is sent. No order is built here."""
    filters = _symbol_filters(info, symbol)
    if not filters:
        return "oco_missing_lot_filter"
    lot, step = _limit_lot(filters)
    if lot is None or step is None:
        return "oco_missing_lot_filter"
    if _tick_size(filters) is None:
        return "oco_missing_tick"
    return None


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


def _exit_order_ids(row: dict[str, Any]) -> list[str]:
    """Every exit order id recorded on the row. The latest id is not the only one."""
    found: list[str] = []
    raw = row.get("exit_order_ids")
    parsed: object = None
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
    elif isinstance(raw, list):
        parsed = raw
    if isinstance(parsed, list):
        for item in parsed:
            text = str(item or "").strip()
            if text and text not in found:
                found.append(text)
    one = str(row.get("exit_order_id") or "").strip()
    if one and one not in found:
        found.append(one)
    return found


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
        account_snapshot: dict[str, Any] | None = None,
        production_book: str | None = None,
        rest_expected: dict[str, Any] | None = None,
        snapshot_error: str | None = None,
        allow_rest_http: bool = False,
        reanchor_oco_on_fill: bool | None = None,
        auto_flatten_on_oco_fail: bool | None = None,
        orderbook_gate: bool | None = None,
        ws_cache: Any | None = None,
        post_only_limit: bool | None = None,
        sor_maker_fallback: bool | None = None,
        entry_ttl_sec: int | None = None,
        trailing_ratchet: bool | None = None,
        max_hold_sec: int | None = None,
    ) -> None:
        self.client = client
        self.kill_switch_path = kill_switch_path
        self.api_key = api_key
        self.api_secret = api_secret
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.dry_run = dry_run
        self.account_snapshot = account_snapshot
        self.production_book = production_book
        self.rest_expected = rest_expected
        self.snapshot_error = snapshot_error
        self.allow_rest_http = bool(allow_rest_http)
        self.reanchor_oco_on_fill = reanchor_oco_on_fill
        self.auto_flatten_on_oco_fail = auto_flatten_on_oco_fail
        self.orderbook_gate = orderbook_gate
        self.ws_cache = ws_cache
        self.post_only_limit = post_only_limit
        self.sor_maker_fallback = sor_maker_fallback
        self.entry_ttl_sec = entry_ttl_sec
        self.trailing_ratchet = trailing_ratchet
        self.max_hold_sec = max_hold_sec
        self.buy_blocks: list[dict[str, Any]] = []
        self.peer_buy_blocks: list[dict[str, Any]] = []
        self.peer_book_error: str | None = None
        self.canonical_rows: list[dict[str, Any]] = []
        self._size_plan: dict[str, Any] | None = None
        self._entry_mark: str | None = None
        self._orderbook_metrics: dict[str, Any] | None = None
        self._mark_telemetry: dict[str, Any] | None = None
        self._oco_preflight: dict[str, Any] | None = None
        self._resting_ceiling: str | None = None
        self._resting_ceiling_source: str | None = None
        self._resting_ask_age_ms: int | None = None

    def _active_ws_cache(self) -> Any | None:
        if self.ws_cache is not None:
            return self.ws_cache
        from execution.binance_ws_stream import get_global_ws_cache, ws_stream_enabled
        if ws_stream_enabled():
            return get_global_ws_cache()
        return None

    def _reanchor_enabled(self) -> bool:
        if self.reanchor_oco_on_fill is not None:
            return bool(self.reanchor_oco_on_fill)
        return reanchor_oco_on_fill_enabled()

    def _auto_flatten_enabled(self) -> bool:
        if self.auto_flatten_on_oco_fail is not None:
            return bool(self.auto_flatten_on_oco_fail)
        return auto_flatten_on_oco_fail_enabled()

    def _orderbook_gate_enabled(self) -> bool:
        if self.orderbook_gate is not None:
            return bool(self.orderbook_gate)
        return orderbook_gate_enabled()

    def _post_only_limit_enabled(self) -> bool:
        if self.post_only_limit is not None:
            return bool(self.post_only_limit)
        return post_only_limit_enabled()

    def _sor_maker_fallback_enabled(self) -> bool:
        if self.sor_maker_fallback is not None:
            return bool(self.sor_maker_fallback)
        return sor_maker_fallback_enabled()

    def _entry_ttl_sec(self) -> int:
        if self.entry_ttl_sec is not None:
            return max(5, int(self.entry_ttl_sec))
        return entry_ttl_sec()

    def _trailing_ratchet_enabled(self) -> bool:
        if self.trailing_ratchet is not None:
            return bool(self.trailing_ratchet)
        return trailing_ratchet_enabled()

    def _env_max_hold_mode(self) -> tuple[str, int]:
        """``disabled`` / ``unset`` / ``invalid`` / ``enabled``.

        Explicit ``0`` is a freeze. A missing value is not a freeze, and a
        value that does not parse is not a freeze either. Both leave a
        positive inline hold available. A positive number enables that many
        seconds when the row has no inline hold.
        """
        raw = os.environ.get(MAX_HOLD_SEC_ENV)
        if raw is None or not str(raw).strip():
            return "unset", 0
        try:
            number = float(str(raw).strip())
        except (TypeError, ValueError):
            return "invalid", 0
        if not math.isfinite(number):
            return "invalid", 0
        try:
            val = int(number)
        except (OverflowError, TypeError, ValueError):
            return "invalid", 0
        if val == 0:
            return "disabled", 0
        if val < 0:
            return "invalid", 0
        return "enabled", val

    def _max_hold_freeze_active(self) -> bool:
        """Explicit runtime or adapter disable. A positive adapter value stays enabled."""
        if self.max_hold_sec is not None:
            return int(self.max_hold_sec) <= 0
        return self._env_max_hold_mode()[0] == "disabled"

    def _apply_hold_passthrough(self, target: dict[str, Any], source: dict[str, Any]) -> None:
        """Copy a positive inline hold, or drop it when the freeze is explicit."""
        if self._max_hold_freeze_active():
            target.pop("max_hold_sec", None)
            target.pop("max_hold_min", None)
            return
        for key in ("max_hold_sec", "max_hold_min"):
            number = _finite_number(source.get(key))
            if number is not None and number > 0:
                target[key] = source.get(key)

    def _effective_max_hold_sec(self, row: dict[str, Any]) -> int:
        if self.max_hold_sec is not None:
            return max(0, int(self.max_hold_sec))
        if self._max_hold_freeze_active():
            return 0
        row_sec = _finite_number(row.get("max_hold_sec"))
        if row_sec is not None and row_sec > 0:
            return int(row_sec)
        row_min = _finite_number(row.get("max_hold_min"))
        if row_min is not None and row_min > 0:
            return int(row_min * 60)
        mode, seconds = self._env_max_hold_mode()
        if mode == "enabled":
            return seconds
        return 0

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

    def _account_block(self) -> str | None:
        """REST book uses the declared account type and USDT free balance.

        ``BINANCE_SPOT_ACCOUNT_UID`` is not read. A missing uid on the spot
        account payload does not become the MCP uid.
        """
        if self.snapshot_error:
            return self.snapshot_error
        book = self.production_book
        if book is None:
            book = os.environ.get("BINANCE_SPOT_PRODUCTION_BOOK", "").strip() or None
        expected = self.rest_expected
        if expected is None and book == REST_BOOK:
            usdt = os.environ.get("BINANCE_SPOT_REST_USDT_FREE", "").strip()
            account_type = os.environ.get("BINANCE_SPOT_REST_ACCOUNT_TYPE", "SPOT").strip() or "SPOT"
            expected = {"account_type": account_type, "usdt_free": usdt or None}
            rest_uid = os.environ.get("BINANCE_SPOT_REST_UID", "").strip()
            if rest_uid:
                expected["uid"] = rest_uid
        return account_book_block(self.account_snapshot, book, expected)

    def _trade_client(self) -> Any:
        """HTTP client for orders.

        A restarted process builds ``default_live_bnb_adapter`` with no client.
        The buy path installs one before ``new_order`` only when REST trading
        is already allowed. Order-list reads use ``_order_list_reader``, which
        can query without handing that client to a later buy.
        """
        if self.client is not None:
            return self.client
        if self.allow_rest_http:
            self.client = BinanceSpotHttpClient()
        return self.client

    def _rest_book_selected(self) -> bool:
        book = self.production_book
        if book is None:
            book = os.environ.get("BINANCE_SPOT_PRODUCTION_BOOK", "").strip() or None
        return book == REST_BOOK

    def _order_list_reader(self) -> Any:
        """Client that can GET an official order list.

        ``_trade_client`` installs nothing when the adapter was built with no
        client and ``allow_rest_http`` is false. A live ``execution.run``
        loop is that adapter: a stale account snapshot keeps REST trading
        off, ``get_order_list`` is missing, and every maintenance tick
        rewrites ``last_error`` to ``oco_requery_unsupported`` while the row
        stays ``oco_working``. An injected client that cannot query does the
        same thing when REST trading is already allowed.

        The read uses ``BinanceSpotHttpClient`` in those two cases. It is
        stored only when this adapter has no client and REST trading is
        already allowed. A snapshot-blocked buy must not inherit the reader
        and skip that gate. An injected order client stays installed.
        """
        current = self.client
        if callable(getattr(current, "get_order_list", None)):
            return current
        if not self.allow_rest_http and not (current is None and self._rest_book_selected()):
            return current
        http = BinanceSpotHttpClient()
        if current is None and self.allow_rest_http:
            self.client = http
        return http

    def _maker_top_of_book(
        self, client: Any, symbol: str
    ) -> tuple[str | None, str | None, str | None]:
        """Fresh uncrossed bid/ask. A stale WebSocket quote does not beat REST.

        Returns ``(bid, ask, error)``. Missing or crossed books do not invent
        a price from the mark, and a maker order is not rewritten as MARKET.
        """
        saw_crossed = False
        ws_cache = self._active_ws_cache()
        if ws_cache is not None and callable(getattr(ws_cache, "get_ticker_price", None)):
            ws_tick = ws_cache.get_ticker_price(symbol)
            fresh = self._fresh_bbo(ws_tick)
            if fresh == "crossed":
                saw_crossed = True
            elif isinstance(fresh, tuple):
                self._resting_ceiling_source = "ws_bbo"
                self._resting_ask_age_ms = self._ws_age_ms(ws_tick) if isinstance(ws_tick, dict) else None
                return fresh[0], fresh[1], None
        depth_fn = getattr(client, "order_book", None) if client is not None else None
        if callable(depth_fn):
            try:
                depth = depth_fn(symbol, limit=5)
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, AssertionError):
                    raise
                depth = None
            if isinstance(depth, dict):
                bids = depth.get("bids") or []
                asks = depth.get("asks") or []
                if (
                    bids
                    and asks
                    and isinstance(bids[0], (list, tuple))
                    and isinstance(asks[0], (list, tuple))
                ):
                    fresh = self._fresh_bbo(
                        {"best_bid": bids[0][0], "best_ask": asks[0][0]},
                        require_age=False,
                    )
                    if isinstance(fresh, tuple):
                        self._resting_ceiling_source = "rest_book"
                        self._resting_ask_age_ms = None
                        return fresh[0], fresh[1], None
                    if fresh == "crossed":
                        return None, None, "orderbook_crossed"
        if saw_crossed:
            return None, None, "orderbook_crossed"
        return None, None, "maker_book_unavailable"

    @staticmethod
    def _ws_age_ms(payload: dict) -> int | None:
        """Milliseconds of a proved-fresh websocket quote, or None.

        A missing, blank, non-numeric, non-finite, boolean, negative, or
        over-age value is not proof that the quote is fresh. Callers then
        use REST. This does not raise.
        """
        if "ws_age_ms" not in payload:
            return None
        age = payload["ws_age_ms"]
        if isinstance(age, bool) or age is None:
            return None
        try:
            if isinstance(age, str):
                text = age.strip()
                if not text:
                    return None
                number = Decimal(text)
            elif isinstance(age, Decimal):
                number = age
            elif isinstance(age, (int, float)):
                number = Decimal(str(age))
            else:
                return None
            if not number.is_finite() or number != number.to_integral_value():
                return None
            age_ms = int(number)
        except (ArithmeticError, ValueError, TypeError, InvalidOperation):
            return None
        if age_ms < 0 or age_ms > default_max_age_ms():
            return None
        return age_ms

    @staticmethod
    def _fresh_bbo(payload: object, *, require_age: bool = True) -> tuple[str, str] | str | None:
        if not isinstance(payload, dict):
            return None
        if require_age and BinanceSpotAdapter._ws_age_ms(payload) is None:
            return None
        bid = _to_decimal(payload.get("best_bid"))
        ask = _to_decimal(payload.get("best_ask"))
        if bid is None or ask is None:
            return None
        if ask <= bid:
            return "crossed"
        return str(payload.get("best_bid")).strip(), str(payload.get("best_ask")).strip()

    def _read_entry_mark(
        self, client: Any, symbol: str
    ) -> tuple[str | None, str | None]:
        """Live last price before a BUY. Checks WebSocket L1 cache first, then REST fallback.

        No ticker method, a transport error, or a non-positive price is
        ``mark_unavailable``. The live BUY stops before ``new_order``.
        """
        ws_cache = self._active_ws_cache()
        if ws_cache is not None and callable(getattr(ws_cache, "get_ticker_price", None)):
            try:
                ws_tick = ws_cache.get_ticker_price(symbol)
            except AssertionError:
                raise
            except Exception:  # noqa: BLE001 — one bad quote is not a price
                ws_tick = None
            age_ms = self._ws_age_ms(ws_tick) if isinstance(ws_tick, dict) else None
            if isinstance(ws_tick, dict) and age_ms is not None and _to_decimal(ws_tick.get("price")) is not None:
                self._mark_telemetry = {
                    "mark_source": str(ws_tick.get("source") or "ws_l1"),
                    "mark_ws_age_ms": age_ms,
                }
                return str(ws_tick["price"]).strip(), None

        ticker = getattr(client, "ticker_price", None) if client is not None else None
        if not callable(ticker):
            return None, "mark_unavailable"
        try:
            payload = ticker(symbol)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, AssertionError):
                raise
            return None, "mark_unavailable"
        mark = payload.get("price") if isinstance(payload, dict) else None
        if _to_decimal(mark) is None:
            return None, "mark_unavailable"
        self._mark_telemetry = {"mark_source": "rest_fallback"}
        return str(mark).strip(), None

    def fresh_maintenance_mark(self, symbol: str) -> tuple[str | None, str | None]:
        """One new last price for this maintenance tick.

        Same WebSocket-then-REST reader as a new buy. The stored entry mark,
        the fill average, and the signal price are not read.
        """
        self._mark_telemetry = None
        client = self.client if self.client is not None else self._trade_client()
        mark, err = self._read_entry_mark(client, symbol)
        if err is not None:
            self._mark_telemetry = {"mark_source": None, "error": err}
        return mark, err

    def _refuse_protected_buy(
        self,
        *,
        error: str,
        symbol: str,
        gates: dict[str, Any],
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        """Stop before ``new_order``. The receipt keeps the preflight, not a position."""
        return self._finish(
            _result(
                ok=False,
                dry_run=False,
                executed=0,
                submitted=False,
                error=error,
                symbol=symbol,
                order=None,
                exchange_order_id=None,
                gates=gates,
                exit_plan=plan,
                consumer_allowed=False,
            )
        )

    def _copy_exit_onto_arm(
        self,
        arm: dict[str, Any],
        row: dict[str, Any],
        oco_fields: dict[str, Any] | None = None,
    ) -> None:
        keys = (
            "status",
            "protection",
            "quantity",
            "exit_order_id",
            "exit_reason",
            "exit_client_order_id",
            "exit_filled_qty",
            "exit_order_ids",
            "unprotected_qty",
            "manual_action",
            "residual_qty",
            "residual_quote",
            "last_error",
        )
        for key in keys:
            if row.get(key) is not None:
                arm[key] = row[key]
                if oco_fields is not None:
                    oco_fields[key] = row[key]

    def _label_fill_protection(self, result: dict[str, Any], *, qty: str) -> dict[str, Any]:
        """Name a filled buy that is not a verified OCO and not a filled exit.

        Blocking another buy is not protection. An exchange reject can still
        leave the coins; this only makes that state explicit.
        """
        if _to_decimal(qty) is None:
            return result
        arm = result.get("arm_position") if isinstance(result.get("arm_position"), dict) else {}
        status = str(arm.get("status") or "")
        protection = str(arm.get("protection") or "")
        if status == "oco_working" and protection == PROTECTION_EXCHANGE:
            result["protection_state"] = "oco_working"
            return result
        if status == "closed_dust" or protection == PROTECTION_DUST:
            result["protection_state"] = "dust_residual"
            result["manual_action"] = "residual_below_min_is_not_a_complete_exit"
            result["protection_incident"] = _protection_incident("dust_residual")
            return result
        residual = _to_decimal(arm.get("residual_qty"))
        if status == "closed" and protection == PROTECTION_AUTO_FLATTEN and (
            residual is None or residual <= Decimal("0.00000001")
        ):
            result["protection_state"] = "exited"
            return result
        result["protection_state"] = "unprotected"
        result["protection_incident"] = _protection_incident("unprotected")
        result["manual_action"] = "halt_new_buys_and_manually_close_or_protect_the_fill"
        log.error(
            "binance_spot_unprotected symbol=%s qty=%s status=%s error=%s",
            result.get("symbol"),
            qty,
            status,
            result.get("error"),
        )
        return result

    def _resting_limit_preflight(
        self,
        *,
        sig: dict[str, Any],
        extras: dict[str, Any],
        plan: dict[str, Any],
        pairs: list[tuple[str, str]],
        info: dict[str, Any],
        symbol: str,
        gates: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], list[tuple[str, str]], dict[str, Any] | None]:
        """Bracket a resting buy by its planned entry. A refusal is already finished."""
        order_type = _parse_order_type(
            extras.get("order_type", extras.get("binance_order_type"))
        )
        raw_limit, price_error = _chosen_limit_raw(extras)
        tel = getattr(self, "_mark_telemetry", None)
        mark_source = tel.get("mark_source") if isinstance(tel, dict) else None
        filters = _symbol_filters(info, symbol) or []
        if price_error:
            diag = {
                "order_type": order_type,
                "reference": None,
                "reference_price": None,
                "mark": self._entry_mark,
                "mark_source": mark_source,
                "aligned_stop": None,
                "aligned_take_profit": None,
                "aligned_limit": None,
                "planned_limit": None,
                "mark_inside_band": None,
            }
            self._oco_preflight = diag
            return extras, plan, pairs, self._refuse_protected_buy(
                error=price_error,
                symbol=symbol,
                gates=gates,
                plan=plan,
            )
        if order_type in {"LIMIT", "LIMIT_MAKER"} and not self._resting_ceiling:
            _bid, ask, book_err = self._maker_top_of_book(self.client, symbol)
            if book_err or not ask:
                self._oco_preflight = {
                    "order_type": order_type,
                    "reference": None,
                    "mark": self._entry_mark,
                    "mark_source": mark_source,
                    "ceiling_source": None,
                    "mark_inside_band": None,
                }
                return extras, plan, pairs, self._refuse_protected_buy(
                    error=book_err or "ask_unavailable",
                    symbol=symbol,
                    gates=gates,
                    plan=plan,
                )
            self._resting_ceiling = ask
        diag, pre_err = buy_entry_oco_preflight(
            order_type=order_type or "",
            limit_price=raw_limit,
            stop_price=plan.get("stop_price"),
            take_profit_price=plan.get("take_profit_price"),
            mark=self._entry_mark,
            mark_source=mark_source,
            filters=filters,
            resting_ceiling=self._resting_ceiling,
            ceiling_source=self._resting_ceiling_source,
        )
        if isinstance(diag, dict):
            diag["ask_age_ms"] = self._resting_ask_age_ms
        self._oco_preflight = diag
        if pre_err is None and not self._auto_flatten_enabled():
            # Every resting buy can fill after the mark has left the original
            # band, or the OCO can be rejected. Deployed defaults do not
            # flatten that fill, and blocking the next buy does not close it.
            # Do not send LIMIT or LIMIT_MAKER unless the existing
            # auto-flatten switch is already on. Reanchor is not that switch.
            # This does not turn the switch on. Resting entries stay
            # unavailable until an operator makes that risk decision.
            # MARKET is not decided here.
            return extras, plan, pairs, self._refuse_protected_buy(
                error="resting_protection_unconfigured",
                symbol=symbol,
                gates=gates,
                plan=plan,
            )
        if pre_err:
            return extras, plan, pairs, self._refuse_protected_buy(
                error=pre_err,
                symbol=symbol,
                gates=gates,
                plan=plan,
            )
        aligned = diag.get("aligned_limit")
        current = dict(pairs).get("price")
        aligned_n = _to_decimal(aligned)
        if aligned_n is not None and _to_decimal(current) != aligned_n:
            stamped = dict(extras)
            if stamped.get("price") not in (None, ""):
                stamped["price"] = aligned
            if stamped.get("limit_price") not in (None, "") or stamped.get("price") in (None, ""):
                stamped["limit_price"] = aligned
            floor_error = entry_notional_floor_error(stamped)
            if floor_error:
                return extras, plan, pairs, self._refuse_protected_buy(
                    error=floor_error,
                    symbol=symbol,
                    gates=gates,
                    plan=plan,
                )
            rebuilt, qty_error = self._preview_pairs(sig, stamped, symbol)
            if qty_error or rebuilt is None:
                return extras, plan, pairs, self._refuse_protected_buy(
                    error=qty_error or "missing_order_qty",
                    symbol=symbol,
                    gates=gates,
                    plan=plan,
                )
            extras = stamped
            pairs = rebuilt
        return extras, plan, pairs, None

    def _repost_band(self, row: dict[str, Any], mark: object) -> tuple[bool, str | None]:
        """True only when the original stop and take-profit still bracket ``mark``."""
        band = self._held_band_error(row, mark)
        return band is None, band

    def _held_band_error(self, row: dict[str, Any], mark: object) -> str | None:
        """Whether this held row can still host the original stop and take-profit."""
        if _to_decimal(mark) is None:
            return "mark_unavailable"
        info, info_error = self._load_symbol_info(row, self._gates())
        if info_error or not isinstance(info, dict):
            return info_error or "oco_missing_lot_filter"
        filters = _symbol_filters(info, str(row.get("symbol") or ""))
        if not filters:
            return "oco_missing_lot_filter"
        return protective_band_error(
            stop_price=str(row.get("stop_price") or ""),
            take_profit_price=str(row.get("take_profit_price") or ""),
            mark=mark,
            filters=filters,
        )

    def _live_buy_block(
        self, sig: dict[str, Any], plan: dict[str, Any] | None, symbol: str
    ) -> str | None:
        """Reasons a live BUY must not be sent. None means the later checks still apply."""
        if plan is None:
            return "missing_exit_prices"
        blocked = replay_block(sig.get("decision_log_id"), _client_order_id(sig))
        if blocked:
            return blocked
        blocked = self._account_block()
        if blocked:
            return blocked
        decision = str(sig.get("decision_log_id") or "").strip()
        if not decision.isdigit():
            return "invalid_oco_client_id"
        purpose = str(sig.get("purpose") or "")
        if self.peer_book_error:
            return self.peer_book_error
        shared = list(self.buy_blocks) + list(self.peer_buy_blocks)
        held_rows = shared + list(self.canonical_rows)
        unresolved = hard_unresolved_halt(held_rows)
        if unresolved:
            return unresolved
        held = buy_exposure_block(
            self.buy_blocks,
            self.canonical_rows,
            purpose=purpose,
            symbol=symbol,
        )
        if held:
            return held
        other_arm = cross_arm_symbol_block(shared, purpose=purpose, symbol=symbol)
        if other_arm:
            return other_arm
        planned = None
        if isinstance(self._size_plan, dict):
            planned = self._size_plan.get("quote_usdt")
        if planned in (None, ""):
            planned = sig.get("extras", {}).get("quote_order_qty") if isinstance(sig.get("extras"), dict) else None
            if planned in (None, "") and isinstance(sig.get("extras"), dict):
                planned = sig["extras"].get("quoteOrderQty")
        funding = funding_after_reservation(
            snapshot_usdt_free(self.account_snapshot),
            ENTRY_MIN_NOTIONAL_USDT,
            planned,
            reserved_quote_usdt(shared),
        )
        if funding:
            return funding
        return self._exchange_evidence_halt(held_rows)

    def _exchange_evidence_halt(self, rows: list[dict[str, Any]]) -> str | None:
        """Clear a working list or a zeroed exit only from this call's reads.

        The entry net is the fee-net on the entry order. A local quantity is
        not that net. A local ``exit_filled_qty`` is not a sell. Each OCO leg
        must still cover whatever the entry read shows is unsold.
        """
        for row in rows:
            if not isinstance(row, dict):
                continue
            purpose = str(row.get("purpose") or "").strip()
            if purpose and purpose not in {
                PURPOSE_LIVE_BNB_BOLL_FADE,
                PURPOSE_LIVE_BNB_BOLL_CLASSIC,
            }:
                continue
            reason = row_unresolved_reason(row)
            if reason not in {"oco_readback_unverified", "exit_readback_unverified"}:
                continue
            entry_net = self._live_order_net(
                row,
                order_id=str(row.get("entry_order_id") or ""),
                side="BUY",
            )
            if entry_net is None:
                return "entry_origin_unverified"
            exit_ids = _exit_order_ids(row)
            exit_net = Decimal("0")
            for exit_id in exit_ids:
                one = self._live_order_net(row, order_id=exit_id, side="SELL")
                if one is None:
                    return "exit_readback_unverified"
                exit_net += one
            still = entry_net - exit_net
            if still <= 0:
                continue
            if not self._current_list_covers(row, still):
                return reason
        return None

    def _live_order_net(self, row: dict[str, Any], *, order_id: str, side: str) -> Decimal | None:
        symbol = str(row.get("symbol") or "").strip()
        order_id = order_id.strip()
        if not symbol or not order_id:
            return None
        client = self._trade_client()
        query = getattr(client, "get_order", None) if client is not None else None
        if not callable(query):
            return None
        key, secret = self._credentials()
        if not key or not secret:
            return None
        pairs = [
            ("symbol", symbol),
            ("orderId", order_id),
            ("recvWindow", RECV_WINDOW_MS),
            ("timestamp", str(int(self.clock_ms()))),
        ]
        try:
            payload = query(sign_query(pairs, secret), api_key=key)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, AssertionError):
                raise
            return None
        if not isinstance(payload, dict):
            return None
        if str(payload.get("symbol") or "").strip().upper() != symbol.upper():
            return None
        if str(payload.get("orderId") or "").strip() != order_id:
            return None
        if str(payload.get("side") or "").strip().upper() != side:
            return None
        text = self._verified_order_base(payload, symbol=symbol, side=side)
        if text is None:
            return None
        number = Decimal(text)
        if not number.is_finite() or number <= 0:
            return None
        return number

    def _order_trades(self, *, symbol: str, order_id: str) -> list[dict[str, Any]] | None:
        """Every trade for this order.

        Binance ``GET /api/v3/myTrades`` takes ``symbol``, ``orderId``,
        ``limit`` (1000 here), and inclusive ``fromId`` (trades whose id is
        greater than or equal to that id). The next page asks for
        ``lastId + 1`` so the boundary id is not requested again. A short
        page ends the read. A full page that adds no id, a non-numeric id,
        or a repeated id whose qty, fee, asset, or side differs fails closed.
        An identical repeated id is kept once. Fifty pages is the cap.
        A row with no order id, or another order's id, is left out of this
        order. It is not treated as ours. A full page that adds nothing fails.
        """
        client = self._trade_client()
        query = getattr(client, "my_trades", None) if client is not None else None
        if not callable(query):
            return None
        key, secret = self._credentials()
        if not key or not secret:
            return None
        seen: dict[str, dict[str, Any]] = {}
        from_id: int | None = None
        for _page in range(50):
            pairs = [
                ("symbol", symbol),
                ("orderId", order_id),
                ("limit", "1000"),
                ("recvWindow", RECV_WINDOW_MS),
                ("timestamp", str(int(self.clock_ms()))),
            ]
            if from_id is not None:
                pairs.append(("fromId", str(from_id)))
            try:
                page = query(sign_query(pairs, secret), api_key=key)
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, AssertionError):
                    raise
                return None
            if not isinstance(page, list):
                return None
            if not page:
                break
            max_tid: int | None = None
            fresh = 0
            for trade in page:
                if not isinstance(trade, dict):
                    return None
                raw_id = trade.get("id")
                if isinstance(raw_id, bool) or raw_id is None:
                    return None
                trade_id = str(raw_id).strip()
                if not trade_id.isdigit() or int(trade_id) <= 0:
                    return None
                number = int(trade_id)
                max_tid = number if max_tid is None else max(max_tid, number)
                owner = str(trade.get("orderId") or "").strip()
                if not owner or owner != order_id:
                    continue
                if str(trade.get("symbol") or "").strip().upper() != symbol.upper():
                    return None
                if not isinstance(trade.get("isBuyer"), bool):
                    return None
                proof = _trade_proof_fields(trade)
                if proof is None:
                    return None
                prior = seen.get(str(number))
                if prior is None:
                    seen[str(number)] = trade
                    fresh += 1
                elif _trade_proof_fields(prior) != proof:
                    return None
            if len(page) < 1000:
                break
            if fresh == 0 or max_tid is None:
                return None
            from_id = max_tid + 1
        else:
            return None
        return list(seen.values())

    def _verified_order_base(
        self,
        payload: dict[str, Any],
        *,
        symbol: str,
        side: str,
        allow_open: bool = False,
        order_id_override: str | None = None,
    ) -> str | None:
        """Buy net or sell reduction from ``myTrades``. The ack ``fills`` list is not evidence.

        A FULL ack often has fills and no ``tradeId``. That list is not
        checked and does not block the trade read. The order body is checked
        only for symbol, side, order id, and a finite non-negative
        ``executedQty``. Executed quantity zero with an empty trade page is
        zero. A positive executed quantity must match the trade qty sum.
        """
        self._proved_fills = None
        if not isinstance(payload, dict):
            return None
        wanted = str(side or "").strip().upper()
        if wanted not in {"BUY", "SELL"}:
            return None
        reported_side = str(payload.get("side") or "").strip().upper()
        if reported_side and reported_side != wanted:
            return None
        order_id = str(order_id_override if order_id_override is not None else payload.get("orderId") or "").strip()
        reported_id = str(payload.get("orderId") or "").strip()
        if not order_id.isdigit() or (reported_id and reported_id != order_id):
            return None
        executed_qty = _nonneg_decimal(payload.get("executedQty"))
        if executed_qty is None or not order_id:
            return None
        status = str(payload.get("status") or "").strip().upper()
        if not allow_open and status not in _FINAL_ORDER_STATUS:
            return None
        if allow_open and status not in _FINAL_ORDER_STATUS | {"NEW", "PARTIALLY_FILLED", "PENDING_NEW"}:
            return None
        reported = str(payload.get("symbol") or "").strip().upper()
        if reported and reported != symbol.upper():
            return None
        trades_page = self._order_trades(symbol=symbol, order_id=order_id)
        if trades_page is None:
            return None
        if executed_qty == 0:
            if trades_page:
                return None
            return "0"
        effect = account_order_base(
            trades_page,
            symbol=symbol,
            order_id=order_id,
            side=wanted,
            executed_qty=executed_qty,
        )
        if effect is None:
            return None
        self._remember_proved_fills(order_id, trades_page)
        return _decimal_text(effect)

    def _remember_proved_fills(self, order_id: str, trades: list[dict[str, Any]]) -> None:
        """Keep the rows that proved this order so the receipt uses those fees."""
        fills: list[dict[str, Any]] = []
        for trade in trades:
            if not isinstance(trade, dict):
                return
            fills.append(
                {
                    "tradeId": trade.get("id"),
                    "qty": trade.get("qty"),
                    "commission": trade.get("commission"),
                    "commissionAsset": trade.get("commissionAsset"),
                }
            )
        self._proved_fills = (order_id, fills)

    def _with_proved_fills(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Attach the proved trades when the ack itself has no fill list."""
        proved = getattr(self, "_proved_fills", None)
        order_id = str(payload.get("orderId") or "").strip()
        if not isinstance(proved, tuple) or len(proved) != 2 or proved[0] != order_id:
            return payload
        existing = payload.get("fills")
        if isinstance(existing, list) and existing:
            return payload
        evidenced = dict(payload)
        evidenced["fills"] = [dict(item) for item in proved[1]]
        return evidenced

    def _read_order(self, *, symbol: str, order_id: str) -> dict[str, Any] | None:
        if not symbol or not order_id:
            return None
        client = self._trade_client()
        query = getattr(client, "get_order", None) if client is not None else None
        if not callable(query):
            return None
        key, secret = self._credentials()
        if not key or not secret:
            return None
        pairs = [
            ("symbol", symbol),
            ("orderId", order_id),
            ("recvWindow", RECV_WINDOW_MS),
            ("timestamp", str(int(self.clock_ms()))),
        ]
        try:
            payload = query(sign_query(pairs, secret), api_key=key)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, AssertionError):
                raise
            return None
        return payload if isinstance(payload, dict) else None

    def _verified_order_base_for_exit(
        self,
        row: dict[str, Any],
        *,
        entry_proof: dict[str, Any] | None = None,
    ) -> str | None:
        """Size a reduce sell from the entry ``get_order`` and proved sells.

        ``NEW``, ``PARTIALLY_FILLED``, ``PENDING_NEW``, and any status outside
        the final set do not sell. The size is the proved BUY net minus every
        recorded SELL that ``myTrades`` can prove, including base-asset
        commission. Stored ``quantity`` and ``exit_filled_qty`` are not a
        size and are not a credit.
        """
        order_id = str(row.get("entry_order_id") or "").strip()
        symbol = str(row.get("symbol") or "").strip()
        stored = _positive_decimal(row.get("quantity"))
        self._exit_fee_block = None
        if not order_id:
            if stored is None:
                self._exit_fee_block = "exit_qty_unconfirmed"
            else:
                self._exit_fee_block = "manual_intervention_required:fee_unverified"
            return None
        payload = entry_proof if isinstance(entry_proof, dict) else None
        if payload is None:
            payload = getattr(self, "_same_call_entry_proof", None)
            if isinstance(payload, dict):
                payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else None
            else:
                payload = None
        if isinstance(entry_proof, dict) or (
            isinstance(getattr(self, "_same_call_entry_proof", None), dict)
            and payload is not None
        ):
            bound = entry_proof if isinstance(entry_proof, dict) else self._same_call_entry_proof
            same_symbol = str(bound.get("symbol") or "").strip().upper() == symbol.upper()
            same_order = str(bound.get("orderId") or "").strip() == order_id
            proof_status = str(bound.get("status") or payload.get("status") or "").strip().upper()
            same_side = str(bound.get("side") or "").strip().upper() == "BUY"
            if (
                not same_symbol
                or not same_order
                or not same_side
                or proof_status not in _FINAL_ORDER_STATUS
                or _positive_decimal(bound.get("net")) is None
            ):
                self._exit_fee_block = "manual_intervention_required:fee_unverified"
                return None
        elif payload is None:
            payload = self._read_order(symbol=symbol, order_id=order_id)
        status = str(payload.get("status") or "").strip().upper() if isinstance(payload, dict) else ""
        if (
            not isinstance(payload, dict)
            or status in {"NEW", "PARTIALLY_FILLED", "PENDING_NEW"}
            or status not in _FINAL_ORDER_STATUS
        ):
            self._exit_fee_block = "manual_intervention_required:fee_unverified"
            return None
        verified = self._verified_order_base(payload, symbol=symbol, side="BUY")
        if verified is None:
            self._exit_fee_block = "manual_intervention_required:fee_unverified"
            return None
        sold = self._proved_recorded_sell_base(row)
        if sold is None:
            self._exit_fee_block = "manual_intervention_required:fee_unverified"
            return None
        remaining = Decimal(verified) - sold
        if remaining <= 0:
            self._exit_fee_block = "manual_intervention_required:fee_unverified"
            return None
        return _decimal_text(remaining)

    def _proved_recorded_sell_base(self, row: dict[str, Any]) -> Decimal | None:
        """Sum of proved SELL effects for exit ids already stored on the row.

        No recorded id is zero, not a license to ignore a local
        ``exit_filled_qty``. An id that cannot be read, or whose status is
        outside the open and final sets, keeps the sell unproved.
        """
        symbol = str(row.get("symbol") or "").strip()
        total = Decimal("0")
        for order_id in _exit_order_ids(row):
            payload = self._read_order(symbol=symbol, order_id=order_id)
            if not isinstance(payload, dict):
                return None
            status = str(payload.get("status") or "").strip().upper()
            still_open = status in {"NEW", "PARTIALLY_FILLED", "PENDING_NEW"}
            if not still_open and status not in _FINAL_ORDER_STATUS:
                return None
            text = self._verified_order_base(
                payload,
                symbol=symbol,
                side="SELL",
                allow_open=still_open,
            )
            if text is None:
                return None
            total += Decimal(text)
        return total

    def _verified_order_base_text(self, row: dict[str, Any], *, order_id: str, side: str) -> str | None:
        net = self._live_order_net(row, order_id=order_id, side=side)
        if net is None:
            return None
        return _decimal_text(net)

    def _current_list_covers(self, row: dict[str, Any], held: Decimal) -> bool:
        list_id = str(row.get("oco_order_list_id") or "").strip()
        symbol = str(row.get("symbol") or "").strip()
        if not list_id or not symbol or held <= 0:
            return False
        client = self._trade_client()
        query = getattr(client, "get_order_list", None) if client is not None else None
        if not callable(query):
            return False
        key, secret = self._credentials()
        if not key or not secret:
            return False
        pairs = [
            ("symbol", symbol),
            ("orderListId", list_id),
            ("recvWindow", RECV_WINDOW_MS),
            ("timestamp", str(int(self.clock_ms()))),
        ]
        try:
            payload = query(sign_query(pairs, secret), api_key=key)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, AssertionError):
                raise
            return False
        return oco_readback_covers(
            payload,
            symbol=symbol,
            held_net=held,
            list_id=list_id,
            above_id=row.get("oco_above_order_id"),
            below_id=row.get("oco_below_order_id"),
        )

    def _readback_account_identity(self) -> str | None:
        if self._account_block() is not None:
            return None
        uid = spot_account_uid(self.account_snapshot)
        if uid is not None:
            if uid == MCP_POSITION_UID:
                return None
            return uid
        if (
            isinstance(self.account_snapshot, dict)
            and self.account_snapshot.get("canTrade") is True
            and str(self.account_snapshot.get("accountType") or "").strip() == "SPOT"
        ):
            return REST_BOOK
        return None

    def _verified_oco_readback_fields(
        self,
        row: dict[str, Any],
        *,
        raw_list_payload: dict[str, Any] | None,
    ) -> dict[str, str] | None:
        """Build persisted OCO readback evidence from a live ``GET /api/v3/orderList`` read.

        Requires proved entry fee-net (minus proved SELL reductions) and live
        coverage on both OCO legs. A ``POST /api/v3/orderList/oco`` ack is never
        accepted here.
        """
        if not isinstance(raw_list_payload, dict):
            return None
        if str(row.get("status") or "") != "oco_working":
            return None
        if str(row.get("protection") or "") != PROTECTION_EXCHANGE:
            return None
        res_val, res_state = self._explicit_hold(row.get("residual_qty"))
        if res_state == "invalid" or (res_val is not None and res_val > 0):
            return None
        unp_val, unp_state = self._explicit_hold(row.get("unprotected_qty"))
        if unp_state == "invalid" or (unp_val is not None and unp_val > 0):
            return None
        symbol = str(row.get("symbol") or "").strip()
        entry_order_id = str(row.get("entry_order_id") or "").strip()
        list_id = _nonneg_id(row.get("oco_order_list_id"))
        above_id = _nonneg_id(row.get("oco_above_order_id"))
        below_id = _nonneg_id(row.get("oco_below_order_id"))
        stored_text = _positive_decimal(row.get("quantity"))
        if (
            not is_spot_usdt_symbol(symbol)
            or _nonneg_id(entry_order_id) is None
            or list_id is None
            or above_id is None
            or below_id is None
            or above_id == below_id
            or stored_text is None
        ):
            return None
        stored_qty = Decimal(stored_text)
        if stored_qty <= 0:
            return None
        account_id = self._readback_account_identity()
        if account_id is None:
            return None
        now_ms = int(self.clock_ms())
        if now_ms <= 0:
            return None
        if not oco_readback_covers(
            raw_list_payload,
            symbol=symbol,
            held_net=stored_qty,
            list_id=list_id,
            above_id=above_id,
            below_id=below_id,
        ):
            return None
        exit_ids = _exit_order_ids(row)
        proof = getattr(self, "_same_call_entry_proof", None)
        entry_net: Decimal | None = None
        if (
            isinstance(proof, dict)
            and not exit_ids
            and str(proof.get("symbol") or "").strip().upper() == symbol.upper()
            and str(proof.get("orderId") or "").strip() == entry_order_id
            and str(proof.get("side") or "").strip().upper() == "BUY"
            and str(proof.get("status") or "").strip().upper() in _FINAL_ORDER_STATUS
        ):
            net_txt = _positive_decimal(proof.get("net"))
            if net_txt is not None:
                entry_net = Decimal(net_txt)
        if entry_net is None:
            client = self._trade_client()
            if client is None or not (
                callable(getattr(client, "get_order", None))
                and callable(getattr(client, "my_trades", None))
            ):
                return None
            entry_net = self._live_order_net(row, order_id=entry_order_id, side="BUY")
        if entry_net is None or entry_net <= 0:
            return None
        sold_net = Decimal("0")
        for exit_id in exit_ids:
            one = self._live_order_net(row, order_id=exit_id, side="SELL")
            if one is None:
                return None
            sold_net += one
        still = entry_net - sold_net
        if still > stored_qty:
            for report in _reports(raw_list_payload):
                leg_id = str(report.get("orderId") or "").strip()
                if leg_id not in {str(above_id), str(below_id)} or leg_id in exit_ids:
                    continue
                leg_exec = _nonneg_decimal(report.get("executedQty"))
                if leg_exec is None or leg_exec <= 0:
                    continue
                proved_leg = self._verified_order_base(
                    report,
                    symbol=symbol,
                    side="SELL",
                    allow_open=True,
                    order_id_override=leg_id,
                )
                if proved_leg is None:
                    return None
                still -= Decimal(proved_leg)
        if still <= 0 or still < stored_qty:
            return None
        if not oco_readback_covers(
            raw_list_payload,
            symbol=symbol,
            held_net=still,
            list_id=list_id,
            above_id=above_id,
            below_id=below_id,
        ):
            return None
        return {
            "oco_readback_status": OCO_READBACK_VERIFIED_STATUS,
            "oco_readback_account_uid": account_id,
            "oco_readback_symbol": symbol.upper(),
            "oco_readback_entry_order_id": entry_order_id,
            "oco_readback_list_id": str(list_id),
            "oco_readback_above_id": str(above_id),
            "oco_readback_below_id": str(below_id),
            "oco_readback_covered_qty": _decimal_text(still),
            "oco_readback_at_ms": str(now_ms),
        }

    def _flatten_resume_held(self, row: dict[str, Any], current_order_id: str) -> str | None:
        """Held size for one more pass over the same flatten sell.

        ``E`` is the proved entry net minus proved sells of every other exit
        id. ``A`` is ``exit_filled_qty`` only when it belongs to this order.
        The caller passes ``max(E-A, 0)`` so a later read cannot add ``A``
        back onto the remainder.
        """
        symbol = str(row.get("symbol") or "").strip()
        entry_id = str(row.get("entry_order_id") or "").strip()
        if not symbol or not entry_id:
            return None
        proof = getattr(self, "_same_call_entry_proof", None)
        if isinstance(proof, dict) and proof:
            same_symbol = str(proof.get("symbol") or "").strip().upper() == symbol.upper()
            same_order = str(proof.get("orderId") or "").strip() == entry_id
            same_side = str(proof.get("side") or "").strip().upper() == "BUY"
            proof_status = str(proof.get("status") or "").strip().upper()
            net_txt = _positive_decimal(proof.get("net"))
            if not (
                same_symbol
                and same_order
                and same_side
                and proof_status in _FINAL_ORDER_STATUS
                and net_txt is not None
            ):
                return None
            entry_net = net_txt
        else:
            payload = self._read_order(symbol=symbol, order_id=entry_id)
            if not isinstance(payload, dict):
                return None
            entry_net = self._verified_order_base(payload, symbol=symbol, side="BUY")
            if entry_net is None:
                return None
        others = Decimal("0")
        for exit_id in _exit_order_ids(row):
            if exit_id == current_order_id:
                continue
            other = self._read_order(symbol=symbol, order_id=exit_id)
            if not isinstance(other, dict):
                return None
            other_status = _exit_order_status(other)
            text = self._verified_order_base(
                other,
                symbol=symbol,
                side="SELL",
                allow_open=other_status in _EXIT_OPEN_STATUS,
            )
            if text is None:
                return None
            others += Decimal(text)
        net = Decimal(entry_net) - others
        accounted = Decimal("0")
        if str(row.get("exit_order_id") or "").strip() == current_order_id:
            accounted = _to_decimal(row.get("exit_filled_qty")) or Decimal("0")
        return _decimal_text(max(net - accounted, Decimal("0")))

    def _record_terminal_flatten(
        self,
        row: dict[str, Any],
        save: Callable[[dict[str, Any]], None],
        placed: dict[str, Any],
        *,
        symbol: str,
        mark: object,
    ) -> dict[str, Any]:
        """A matched sell that is already rejected, expired, or canceled."""
        status = _exit_order_status(placed)
        order_id = _exit_order_id_text(placed)
        held = str(row.get("quantity") or "")
        label = f"flatten_order_{status.lower()}"
        if "executedQty" not in placed:
            executed = None
        else:
            executed = _nonneg_decimal(placed.get("executedQty"))
        if executed is None:
            save(
                {
                    "status": "oco_failed",
                    "protection": PROTECTION_PENDING,
                    "last_error": "manual_intervention_required:fee_unverified",
                    "manual_action": "halt_new_buys_and_manually_close_or_protect_the_fill",
                    "unprotected_qty": held,
                }
            )
            return self._coverage(
                row,
                book="oco_failed",
                submitted=False,
                error="manual_intervention_required:fee_unverified",
                protection=PROTECTION_PENDING,
                quantity=held or None,
            )
        if order_id is None:
            return self._coverage(
                row,
                book="oco_failed",
                submitted=False,
                error="flatten_submit_unknown",
                protection=PROTECTION_PENDING,
            )

        def _book_terminal(fields: dict[str, Any]) -> dict[str, Any]:
            client_text = str(
                placed.get("clientOrderId") or placed.get("origClientOrderId") or ""
            ).strip()
            if client_text:
                fields["exit_client_order_id"] = client_text
            found = _exit_order_ids(row)
            if order_id not in found:
                found.append(order_id)
            fields["exit_order_ids"] = json.dumps(found)
            return fields

        if executed == 0:
            save(
                _book_terminal(
                    {
                        "status": "oco_failed",
                        "protection": PROTECTION_PENDING,
                        "unprotected_qty": held,
                        "manual_action": "halt_new_buys_and_manually_close_or_protect_the_fill",
                        "exit_order_id": order_id,
                        "exit_filled_qty": "0",
                        "last_error": label,
                    }
                )
            )
            return self._coverage(
                row,
                book="oco_failed",
                submitted=False,
                error=label,
                protection=PROTECTION_PENDING,
                quantity=held or None,
            )
        proved = self._verified_order_base(
            placed, symbol=symbol, side="SELL", allow_open=False
        )
        if proved is None:
            save(
                {
                    "status": "oco_failed",
                    "protection": PROTECTION_PENDING,
                    "last_error": "manual_intervention_required:fee_unverified",
                    "manual_action": "halt_new_buys_and_manually_close_or_protect_the_fill",
                    "unprotected_qty": held,
                }
            )
            return self._coverage(
                row,
                book="oco_failed",
                submitted=False,
                error="manual_intervention_required:fee_unverified",
                protection=PROTECTION_PENDING,
                quantity=held or None,
            )
        held_now = self._flatten_resume_held(row, order_id)
        if held_now is None:
            save(
                {
                    "status": "oco_failed",
                    "protection": PROTECTION_PENDING,
                    "last_error": "manual_intervention_required:fee_unverified",
                    "manual_action": "halt_new_buys_and_manually_close_or_protect_the_fill",
                    "unprotected_qty": held,
                }
            )
            return self._coverage(
                row,
                book="oco_failed",
                submitted=False,
                error="manual_intervention_required:fee_unverified",
                protection=PROTECTION_PENDING,
                quantity=held or None,
            )
        base = Decimal(held_now)
        accounted = Decimal("0")
        if str(row.get("exit_order_id") or "").strip() == order_id:
            accounted = _to_decimal(row.get("exit_filled_qty")) or Decimal("0")
        sold = Decimal(proved)
        remainder = base - max(sold - accounted, Decimal("0"))
        if remainder < 0:
            remainder = Decimal("0")
        dust_line = Decimal("0.00000001")
        if remainder <= dust_line:
            stored_residual = _to_decimal(row.get("residual_qty"))
            if stored_residual is not None and stored_residual > dust_line:
                residual_text = _decimal_text(stored_residual)
                fields = dust_archive_fields(
                    quantity=residual_text or "0",
                    mark=mark if mark is not None else row.get("entry_price"),
                    stop=row.get("stop_price"),
                    detail="exit_qty_below_min",
                )
                fields["exit_order_id"] = order_id
                fields["exit_filled_qty"] = _decimal_text(sold)
                save(_book_terminal(fields))
                return self._coverage(
                    row,
                    book="closed_dust",
                    submitted=False,
                    error="dust_untradable",
                    protection=PROTECTION_DUST,
                    quantity=residual_text,
                )
            save(
                _book_terminal(
                    {
                        "status": "closed",
                        "protection": PROTECTION_AUTO_FLATTEN,
                        "quantity": _decimal_text(sold),
                        "exit_order_id": order_id,
                        "exit_reason": "oco_failed_auto_flatten",
                        "exit_filled_qty": _decimal_text(sold),
                        "oco_list_status": "ALL_DONE",
                        "last_error": None,
                    }
                )
            )
            return self._coverage(
                row,
                book="closed",
                submitted=False,
                ok=True,
                error=None,
                protection=PROTECTION_AUTO_FLATTEN,
                quantity=_decimal_text(sold),
            )
        kept = _decimal_text(remainder)
        if kept is None:
            return self._coverage(
                row,
                book="oco_failed",
                submitted=False,
                error="manual_intervention_required:fee_unverified",
                protection=PROTECTION_PENDING,
            )
        info, _info_err = self._load_symbol_info(row, self._gates())
        dust = False
        if isinstance(info, dict):
            _sized, size_error = size_exit_quantity(info, symbol, kept, mark)
            dust = bool(dust_detail(size_error) or size_error == "exit_qty_below_min")
        if dust:
            fields = dust_archive_fields(
                quantity=kept,
                mark=mark if mark is not None else row.get("entry_price"),
                stop=row.get("stop_price"),
                detail="exit_qty_below_min",
            )
            fields["exit_order_id"] = order_id
            fields["exit_filled_qty"] = _decimal_text(sold)
            save(_book_terminal(fields))
            return self._coverage(
                row,
                book="closed_dust",
                submitted=False,
                error="dust_untradable",
                protection=PROTECTION_DUST,
                quantity=kept,
            )
        save(
            _book_terminal(
                {
                    "status": "oco_failed",
                    "protection": PROTECTION_PENDING,
                    "quantity": kept,
                    "unprotected_qty": kept,
                    "exit_order_id": order_id,
                    "exit_filled_qty": _decimal_text(sold),
                    "manual_action": "halt_new_buys_and_manually_close_or_protect_the_fill",
                    "last_error": label,
                }
            )
        )
        return self._coverage(
            row,
            book="oco_failed",
            submitted=False,
            error=label,
            protection=PROTECTION_PENDING,
            quantity=kept,
        )

    def _resume_unknown_flatten(
        self,
        row: dict[str, Any],
        mark: object,
        save: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        """Finish a flatten whose POST result was not known.

        A confirmed fill closes the row. A live partial keeps the remainder.
        A terminal cancel, reject, or expiry is not a timeout. A timeout stays
        unknown. None of these posts a new sell or a new OCO.
        """
        client_id = str(row.get("exit_client_order_id") or "").strip()
        symbol = str(row.get("symbol") or "")
        key, secret = self._credentials()
        client = self._trade_client()
        entry_oid = row.get("entry_order_id")
        lookup = getattr(client, "get_order", None) if client is not None else None
        if (
            client is None
            or not callable(lookup)
            or not client_id
            or not key
            or not secret
            or not is_spot_usdt_symbol(symbol)
        ):
            return self._coverage(
                row,
                book="oco_failed",
                submitted=False,
                error="flatten_submit_unknown",
                protection=PROTECTION_PENDING,
            )
        pairs = [
            ("symbol", symbol),
            ("origClientOrderId", client_id),
            ("recvWindow", RECV_WINDOW_MS),
            ("timestamp", str(int(self.clock_ms()))),
        ]
        try:
            placed = lookup(sign_query(pairs, secret), api_key=key)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, AssertionError):
                raise
            if isinstance(exc, BinanceRequestError) and single_order_absent(exc):
                return self._oco_failed(row, save, "flatten_absent")
            return self._coverage(
                row,
                book="oco_failed",
                submitted=False,
                error="flatten_submit_unknown",
                protection=PROTECTION_PENDING,
            )
        if not isinstance(placed, dict):
            return self._coverage(
                row,
                book="oco_failed",
                submitted=False,
                error="flatten_submit_unknown",
                protection=PROTECTION_PENDING,
            )
        identity = _exit_order_identity(
            placed,
            client_id=client_id,
            entry_order_id=entry_oid,
            symbol=symbol,
        )
        status = _exit_order_status(placed)
        if not identity:
            if status in _EXIT_OPEN_STATUS or status in _EXIT_FILLED_STATUS or status in _EXIT_FAIL_STATUS:
                save(
                    {
                        "status": "oco_failed",
                        "protection": PROTECTION_PENDING,
                        "last_error": "flatten_lookup_wrong_order",
                        "manual_action": "halt_new_buys_and_manually_close_or_protect_the_fill",
                    }
                )
                return self._coverage(
                    row,
                    book="oco_failed",
                    submitted=False,
                    error="flatten_lookup_wrong_order",
                    protection=PROTECTION_PENDING,
                )
            return self._coverage(
                row,
                book="oco_failed",
                submitted=False,
                error="flatten_submit_unknown",
                protection=PROTECTION_PENDING,
            )
        if status in _EXIT_FAIL_STATUS:
            return self._record_terminal_flatten(
                row, save, placed, symbol=symbol, mark=mark
            )
        if status not in _EXIT_OPEN_STATUS and status not in _EXIT_FILLED_STATUS:
            return self._coverage(
                row,
                book="oco_failed",
                submitted=False,
                error="flatten_submit_unknown",
                protection=PROTECTION_PENDING,
            )
        order_id = _exit_order_id_text(placed) or ""
        held = self._flatten_resume_held(row, order_id)
        if held is None:
            save(
                {
                    "status": "oco_failed",
                    "protection": PROTECTION_PENDING,
                    "last_error": "manual_intervention_required:fee_unverified",
                    "manual_action": "halt_new_buys_and_manually_close_or_protect_the_fill",
                }
            )
            return self._coverage(
                row,
                book="oco_failed",
                submitted=False,
                error="manual_intervention_required:fee_unverified",
                protection=PROTECTION_PENDING,
            )
        info, _info_error = self._load_symbol_info(row, self._gates())
        return self._settle_reduce_sell(
            row,
            save,
            placed,
            client_id,
            reason="oco_failed_auto_flatten",
            protection=PROTECTION_AUTO_FLATTEN,
            error_prefix=f"oco_failed_auto_flattened:{row.get('last_error') or 'flatten_submit_unknown'}",
            mark=mark,
            info=info if isinstance(info, dict) else None,
            proved_held=held,
        )

    def _auto_flatten_naked_position(
        self,
        row: dict[str, Any],
        *,
        info: dict[str, Any] | None,
        mark: object,
        oco_error: str,
        save: Callable[[dict[str, Any]], None],
        exchange_error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Emergency reduce-only market sell when a placement book is ``oco_failed``.

        ``closed_dust`` and ``oco_partial`` never enter. A second call while
        one flatten is already running persists the failure and does not sell.
        """
        status_now = str(row.get("status") or "")
        if status_now in {"closed_dust", "oco_partial"} or getattr(self, "_flatten_guard", False):
            keep = row.get("quantity")
            save(
                {
                    "status": "oco_failed",
                    "protection": PROTECTION_PENDING,
                    "last_error": oco_error,
                    "manual_action": "halt_new_buys_and_manually_close_or_protect_the_fill",
                    "quantity": keep,
                    "unprotected_qty": keep,
                }
            )
            return self._coverage(
                row,
                book="oco_failed",
                submitted=False,
                error=oco_error,
                protection=PROTECTION_PENDING,
                quantity=str(keep) if keep is not None else None,
                exchange_error=exchange_error,
            )
        self._flatten_guard = True
        try:
            return self._auto_flatten_body(
                row,
                info=info,
                mark=mark,
                oco_error=oco_error,
                save=save,
                exchange_error=exchange_error,
            )
        finally:
            self._flatten_guard = False

    def _auto_flatten_body(
        self,
        row: dict[str, Any],
        *,
        info: dict[str, Any] | None,
        mark: object,
        oco_error: str,
        save: Callable[[dict[str, Any]], None],
        exchange_error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        gates = self._gates()
        blocked = protective_order_block(gates)
        if blocked:
            return self._oco_failed(
                row,
                save,
                f"{oco_error}|flatten_blocked:{blocked}",
                exchange_error=exchange_error,
            )
        symbol = str(row.get("symbol") or "")
        if not isinstance(info, dict):
            loaded, _load_error = self._load_symbol_info(row, gates)
            if isinstance(loaded, dict):
                info = loaded
        qty = _positive_decimal(self._verified_order_base_for_exit(row))
        client_id = flatten_client_order_id(row.get("decision_log_id"))
        if qty is None:
            keep = row.get("quantity")
            save(
                {
                    "status": "oco_failed",
                    "protection": PROTECTION_PENDING,
                    "quantity": keep,
                    "unprotected_qty": keep,
                    "manual_action": "halt_new_buys_and_manually_close_or_protect_the_fill",
                    "last_error": "manual_intervention_required:fee_unverified",
                }
            )
            failed = self._coverage(
                row,
                book="oco_failed",
                submitted=False,
                error="fee_unverified",
                protection=PROTECTION_PENDING,
                quantity=str(keep) if keep is not None else None,
                exchange_error=exchange_error,
            )
            failed["manual_action"] = "halt_new_buys_and_manually_close_or_protect_the_fill"
            failed["protection_incident"] = _protection_incident("unprotected")
            return failed
        if not is_spot_usdt_symbol(symbol) or client_id is None:
            return self._oco_failed(
                row,
                save,
                f"{oco_error}|flatten_invalid_spec",
                exchange_error=exchange_error,
            )
        key, secret = self._credentials()
        if not key or not secret:
            return self._oco_failed(
                row,
                save,
                f"{oco_error}|flatten_missing_credentials",
                exchange_error=exchange_error,
            )
        client = self._trade_client()
        if client is None or not callable(getattr(client, "new_order", None)):
            return self._oco_failed(
                row,
                save,
                f"{oco_error}|flatten_client_unsupported",
                exchange_error=exchange_error,
            )
        sym_info = info
        if not isinstance(sym_info, dict):
            sym_info, info_err = self._load_symbol_info(row, gates)
            if info_err or not isinstance(sym_info, dict):
                return self._oco_failed(
                    row,
                    save,
                    f"{oco_error}|flatten_info_failed:{info_err or 'missing'}",
                    exchange_error=exchange_error,
                )
        ref_mark = mark if _to_decimal(mark) is not None else row.get("entry_price")
        sized, size_error = size_exit_quantity(sym_info, symbol, qty, ref_mark)
        if size_error or sized is None:
            detail = dust_detail(size_error)
            if detail is not None:
                fields = dust_archive_fields(
                    quantity=qty,
                    mark=ref_mark,
                    stop=row.get("stop_price"),
                    detail=detail,
                )
                save(fields)
                return self._coverage(
                    row,
                    book="closed_dust",
                    submitted=False,
                    error="dust_untradable",
                    protection=PROTECTION_DUST,
                    reason="dust_abandon",
                    exchange_error=exchange_error,
                )
            return self._oco_failed(
                row,
                save,
                f"{oco_error}|flatten_size_error:{size_error or 'invalid'}",
                exchange_error=exchange_error,
            )
        pairs, qty_error = _order_pairs(
            symbol=symbol,
            side="SELL",
            order_type="MARKET",
            extras={"quantity": sized},
            client_id=client_id,
            timestamp_ms=int(self.clock_ms()),
        )
        if qty_error or pairs is None:
            return self._oco_failed(
                row,
                save,
                f"{oco_error}|flatten_pairs_error:{qty_error or 'missing'}",
                exchange_error=exchange_error,
            )
        signed = sign_query(pairs, secret)
        entry_oid = row.get("entry_order_id")
        existing = self._lookup_exit_order(
            client, symbol, client_id, key, secret, entry_order_id=entry_oid
        )
        if existing is not None:
            return self._settle_reduce_sell(
                row,
                save,
                existing,
                client_id,
                reason="oco_failed_auto_flatten",
                protection=PROTECTION_AUTO_FLATTEN,
                error_prefix=f"oco_failed_auto_flattened:{oco_error}",
                exchange_error=exchange_error,
                mark=mark,
                info=sym_info if isinstance(sym_info, dict) else None,
                proved_held=qty,
            )
        if str(row.get("last_error") or "").startswith("flatten_submit_unknown"):
            return self._oco_failed(
                row,
                save,
                f"{oco_error}|flatten_submit_unknown",
                exchange_error=exchange_error,
            )
        try:
            placed = client.new_order(signed, api_key=key)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, AssertionError):
                raise
            self._last_exit_lookup_payload = None
            looked = self._exit_lookup_state(
                client, symbol, client_id, key, secret, entry_order_id=entry_oid
            )
            sell_err = _public_error(exc, key, secret)
            if looked == "terminal":
                cached = getattr(self, "_last_exit_lookup_payload", None)
                terminal_placed = cached if isinstance(cached, dict) else None
                if not isinstance(terminal_placed, dict):
                    terminal_placed = self._lookup_terminal_exit_order(
                        client,
                        symbol,
                        client_id,
                        key,
                        secret,
                        entry_order_id=entry_oid,
                    )
                if isinstance(terminal_placed, dict):
                    return self._record_terminal_flatten(
                        row,
                        save,
                        terminal_placed,
                        symbol=symbol,
                        mark=mark,
                    )
                save(
                    {
                        "status": "oco_failed",
                        "protection": PROTECTION_PENDING,
                        "exit_client_order_id": client_id,
                        "last_error": "flatten_submit_unknown",
                    }
                )
                return self._coverage(
                    row,
                    book="oco_failed",
                    submitted=False,
                    error=f"{oco_error}|flatten_submit_unknown:{sell_err}",
                    protection=PROTECTION_PENDING,
                    exchange_error=exchange_error,
                )
            recovered = self._lookup_exit_order(
                client, symbol, client_id, key, secret, entry_order_id=entry_oid
            )
            if recovered is not None:
                placed = recovered
            elif callable(getattr(client, "get_order", None)) and looked in {"unknown", "mismatch"}:
                save(
                    {
                        "status": "oco_failed",
                        "protection": PROTECTION_PENDING,
                        "exit_client_order_id": client_id,
                        "last_error": "flatten_submit_unknown",
                    }
                )
                return self._coverage(
                    row,
                    book="oco_failed",
                    submitted=False,
                    error=f"{oco_error}|flatten_submit_unknown:{sell_err}",
                    protection=PROTECTION_PENDING,
                    exchange_error=exchange_error,
                )
            else:
                return self._oco_failed(
                    row,
                    save,
                    f"{oco_error}|flatten_failed:{sell_err}",
                    exchange_error=exchange_error,
                )
        if not isinstance(placed, dict):
            return self._oco_failed(
                row,
                save,
                f"{oco_error}|flatten_rejected",
                exchange_error=exchange_error,
            )
        if _terminal_fail_sell(
            placed,
            client_id=client_id,
            entry_order_id=entry_oid,
            symbol=symbol,
        ):
            return self._record_terminal_flatten(
                row,
                save,
                placed,
                symbol=symbol,
                mark=mark,
            )
        return self._settle_reduce_sell(
            row,
            save,
            placed,
            client_id,
            reason="oco_failed_auto_flatten",
            protection=PROTECTION_AUTO_FLATTEN,
            error_prefix=f"oco_failed_auto_flattened:{oco_error}",
            exchange_error=exchange_error,
            mark=mark,
            info=sym_info if isinstance(sym_info, dict) else None,
            proved_held=qty,
        )

    def _buy_with_native_oco(
        self,
        sig: dict[str, Any],
        *,
        plan: dict[str, Any],
        gates: dict[str, Any],
        intent: dict[str, Any] | None,
        pairs: list[tuple[str, str]],
        order_id: str,
        filled: str | None,
        sellable: str | None,
        info: dict[str, Any],
        symbol: str,
        placed: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Arm the fill only after a verified native OCO."""
        info_obj = info if isinstance(info, dict) else None
        filters = _symbol_filters(info_obj, symbol) if info_obj is not None else None
        decision_id = int(str(sig.get("decision_log_id")).strip())
        qty = sellable
        arm: dict[str, Any] = {
            "decision_log_id": decision_id,
            "symbol": symbol,
            "quantity": qty,
            "stop_price": plan["stop_price"],
            "take_profit_price": plan["take_profit_price"],
            "entry_price": plan.get("entry_price"),
            "entry_order_id": order_id,
            "status": "qty_unconfirmed",
            "protection": HELD_EXIT_PROTECTION,
            "purpose": str(sig.get("purpose") or "").strip() or PURPOSE_LIVE_BNB_BOLL_FADE,
        }
        signal_entry = plan.get("entry_price")
        if signal_entry not in (None, ""):
            arm["signal_entry_price"] = signal_entry
        fill_price = fill_average_price(placed)
        if fill_price:
            # Ledger entry is the exchange average. The signal price stays on
            # exit_plan and on signal_entry_price. Existing rows are not rewritten.
            arm["entry_price"] = fill_price
        common = dict(
            symbol=symbol,
            order=dict(pairs),
            exchange_order_id=order_id,
            gates=gates,
            intent=intent,
            filled_qty=filled,
            sellable_qty=sellable,
        )
        if qty is None and not filled:
            arm["status"] = "qty_unconfirmed"
            return _result(
                ok=False,
                dry_run=False,
                executed=0,
                submitted=False,
                error="parent_fill_incomplete",
                exit_plan=plan,
                arm_position=arm,
                consumer_allowed=False,
                **common,
            )
        if qty is None:
            arm["status"] = "oco_failed"
            arm["protection"] = PROTECTION_PENDING
            arm["quantity"] = filled
            arm["unprotected_qty"] = filled
            arm["manual_action"] = "halt_new_buys_and_manually_close_or_protect_the_fill"
            arm["last_error"] = "fee_unverified"
            return self._label_fill_protection(
                _result(
                    ok=False,
                    dry_run=False,
                    executed=0,
                    submitted=True,
                    error="fee_unverified",
                    exit_plan=plan,
                    arm_position=arm,
                    consumer_allowed=False,
                    **common,
                ),
                qty=str(filled or ""),
            )
        row: dict[str, Any] = {
            "id": 1,
            "decision_log_id": decision_id,
            "symbol": symbol,
            "status": "open",
            "quantity": qty,
            "stop_price": plan["stop_price"],
            "take_profit_price": plan["take_profit_price"],
            "entry_price": arm.get("entry_price"),
            "entry_order_id": order_id,
            "protection": HELD_EXIT_PROTECTION,
        }
        extras = sig.get("extras") if isinstance(sig.get("extras"), dict) else {}
        self._apply_hold_passthrough(arm, extras)
        self._apply_hold_passthrough(row, extras)

        def save(fields: dict[str, Any]) -> None:
            row.update(fields)

        pre = getattr(self, "_oco_preflight", None)
        strict_fill = (
            isinstance(pre, dict)
            and pre.get("reference") == "planned_entry"
            and pre.get("mark_inside_band") is False
        )
        protect_mark = getattr(self, "_entry_mark", None)
        anchors: tuple[object, ...] = (protect_mark, fill_price)
        anchor_block: str | None = None
        if strict_fill and isinstance(pre, dict):
            # The pre-submit mark was allowed to sit outside the band. The
            # fill is protected against the fill average and a fresh mark.
            client_now = self.client if self.client is not None else self._trade_client()
            if client_now is None:
                fresh, fresh_err = None, "mark_unavailable"
            else:
                fresh, fresh_err = self._read_entry_mark(client_now, symbol)
            noted = dict(pre)
            noted["fill_mark"] = fresh
            noted["fill_average"] = fill_price
            if fresh_err:
                noted["fill_mark_error"] = fresh_err
            self._oco_preflight = noted
            if fresh_err or _to_decimal(fresh) is None:
                anchor_block = fresh_err or "mark_unavailable"
            elif _to_decimal(fill_price) is None:
                anchor_block = "fill_price_unavailable"
            else:
                anchors = (fresh, fill_price)
                protect_mark = fresh

        band_error: str | None = anchor_block
        if band_error is None:
            for candidate in anchors:
                if not filters or _to_decimal(candidate) is None:
                    continue
                band_error = protective_band_error(
                    stop_price=str(row.get("stop_price") or ""),
                    take_profit_price=str(row.get("take_profit_price") or ""),
                    mark=candidate,
                    filters=filters,
                )
                if band_error:
                    break

        if band_error and anchor_block is None and self._reanchor_enabled() and filters:
            reanchored, reanchor_err = reanchor_oco_prices_to_fill(
                signal_entry_price=signal_entry,
                stop_price=str(plan.get("stop_price") or ""),
                take_profit_price=str(plan.get("take_profit_price") or ""),
                fill_price=fill_price,
                mark=protect_mark,
                filters=filters,
            )
            if reanchored is not None and reanchor_err is None:
                arm["signal_stop_price"] = plan["stop_price"]
                arm["signal_take_profit_price"] = plan["take_profit_price"]
                arm["oco_reanchored"] = True
                arm["stop_price"] = reanchored["stop_price"]
                arm["take_profit_price"] = reanchored["take_profit_price"]
                row["stop_price"] = reanchored["stop_price"]
                row["take_profit_price"] = reanchored["take_profit_price"]
                band_error = None

        if band_error:
            detail = f"oco_unprotectable:{band_error}"
            row["status"] = "oco_failed"
            row["protection"] = PROTECTION_PENDING
            row["last_error"] = detail
            if self._auto_flatten_enabled():
                flat = self._auto_flatten_naked_position(
                    row,
                    info=info_obj,
                    mark=fill_price or protect_mark,
                    oco_error=detail,
                    save=save,
                )
                self._copy_exit_onto_arm(arm, row)
                return self._label_fill_protection(
                    _result(
                        ok=False,
                        dry_run=False,
                        executed=0,
                        submitted=False,
                        error=str(flat.get("error") or detail),
                        exit_plan=plan,
                        arm_position=arm,
                        consumer_allowed=False,
                        **common,
                    ),
                    qty=str(arm.get("quantity") or ""),
                )
            arm["status"] = "oco_failed"
            arm["protection"] = PROTECTION_PENDING
            row["status"] = "oco_failed"
            row["protection"] = PROTECTION_PENDING
            row["last_error"] = detail
            return self._label_fill_protection(
                _result(
                    ok=False,
                    dry_run=False,
                    executed=0,
                    submitted=False,
                    error=detail,
                    exit_plan=plan,
                    arm_position=arm,
                    consumer_allowed=False,
                    **common,
                ),
                qty=str(arm.get("quantity") or ""),
            )

        if strict_fill:
            oco_mark = protect_mark or fill_price
        else:
            oco_mark = fill_price or protect_mark or plan.get("entry_price")
        outcome = self._try_place_oco(row, oco_mark, save)
        oco_fields = {
            key: row.get(key)
            for key in (
                "oco_list_client_id",
                "oco_order_list_id",
                "oco_list_status",
                "oco_above_order_id",
                "oco_below_order_id",
                "oco_above_client_id",
                "oco_below_client_id",
                "residual_qty",
                "last_error",
                *OCO_READBACK_COLUMNS,
            )
            if row.get(key) not in (None, "")
        }
        verified = (
            isinstance(outcome, dict)
            and outcome.get("book") in {"oco_working", "oco_partial"}
            and str(row.get("status") or "") in {"oco_working", "oco_partial"}
            and native_oco_verified(
                {
                    "orderListId": row.get("oco_order_list_id"),
                    "listOrderStatus": "EXECUTING",
                    "listStatusType": "EXEC_STARTED",
                    "orders": [
                        {"orderId": row.get("oco_above_order_id")},
                        {"orderId": row.get("oco_below_order_id")},
                    ],
                }
            )[0]
            is not None
        )
        if verified:
            residual = _to_decimal(row.get("residual_qty"))
            if residual is not None and residual > Decimal("0.00000001"):
                arm["status"] = "oco_partial"
                arm["protection"] = PROTECTION_EXCHANGE
                arm["quantity"] = qty
                arm["residual_qty"] = row.get("residual_qty")
                arm["unprotected_qty"] = row.get("residual_qty")
                arm["manual_action"] = "residual_below_min_is_not_a_complete_exit"
                arm["last_error"] = row.get("last_error") or f"oco_residual_unprotected:{row.get('residual_qty')}"
                save(
                    {
                        "status": "oco_partial",
                        "quantity": qty,
                        "unprotected_qty": row.get("residual_qty"),
                        "manual_action": arm["manual_action"],
                        "last_error": arm["last_error"],
                    }
                )
                partial = _result(
                    ok=False,
                    dry_run=False,
                    executed=1,
                    submitted=True,
                    error="oco_lot_residual",
                    exit_plan=plan,
                    arm_position=arm,
                    oco_fields=oco_fields,
                    consumer_allowed=False,
                    **common,
                )
                partial["protection_state"] = "dust_residual"
                partial["manual_action"] = arm["manual_action"]
                partial["protection_incident"] = _protection_incident("dust_residual")
                return partial
            covered = dict(plan)
            covered["stop_price"] = row["stop_price"]
            covered["take_profit_price"] = row["take_profit_price"]
            covered["protective_order"] = PROTECTION_EXCHANGE
            covered["submitted"] = True
            arm["status"] = "oco_working"
            arm["protection"] = PROTECTION_EXCHANGE
            arm["quantity"] = row.get("quantity") or qty
            covered_result = _result(
                ok=True,
                dry_run=False,
                executed=1,
                submitted=True,
                error=None,
                exit_plan=covered,
                arm_position=arm,
                oco_fields=oco_fields,
                consumer_allowed=True,
                **common,
            )
            covered_result["protection_state"] = "oco_working"
            return covered_result
        status = str(row.get("status") or "")
        if status in {"", "open"}:
            status = "oco_failed"
        arm["status"] = status
        arm["protection"] = str(row.get("protection") or PROTECTION_PENDING)
        arm["quantity"] = row.get("quantity") or qty
        error = None
        exch_err = None
        if isinstance(outcome, dict):
            error = outcome.get("error")
            exch_err = outcome.get("exchange_error")
        if not error:
            error = row.get("last_error") or "native_oco_unverified"
        placed_book = str(outcome.get("book") or "") if isinstance(outcome, dict) else ""
        if (
            self._auto_flatten_enabled()
            and placed_book == "oco_failed"
            and status not in {"closed_dust", "oco_partial"}
        ):
            flat = self._auto_flatten_naked_position(
                row,
                info=info_obj,
                mark=oco_mark,
                oco_error=str(error),
                save=save,
                exchange_error=exch_err,
            )
            self._copy_exit_onto_arm(arm, row, oco_fields)
            return self._label_fill_protection(
                _result(
                    ok=False,
                    dry_run=False,
                    executed=0,
                    submitted=False,
                    error=str(flat.get("error") or error),
                    exit_plan=plan,
                    arm_position=arm,
                    oco_fields=oco_fields or None,
                    exchange_error=exch_err,
                    consumer_allowed=False,
                    **common,
                ),
                qty=str(arm.get("quantity") or ""),
            )
        return self._label_fill_protection(
            _result(
                ok=False,
                dry_run=False,
                executed=0,
                submitted=False,
                error=str(error),
                exit_plan=plan,
                arm_position=arm,
                oco_fields=oco_fields or None,
                exchange_error=exch_err,
                consumer_allowed=False,
                **common,
            ),
            qty=str(arm.get("quantity") or ""),
        )

    def _resting_maker_ack(
        self,
        sig: dict[str, Any],
        *,
        plan: dict[str, Any] | None,
        gates: dict[str, Any],
        intent: dict[str, Any] | None,
        pairs: list[tuple[str, str]],
        order_id: str,
        placed: dict[str, Any],
        symbol: str,
    ) -> dict[str, Any]:
        """Record a live post-only entry that is not a protected buy yet.

        OCO waits until the entry is terminal. Until then the original order
        quantity stays reserved and the row blocks another buy. A zero-fill
        cancel is ``closed_canceled`` and does not keep the lock.
        """
        order = dict(pairs)
        ack = str(placed.get("status") or "").upper()
        terminal = ack in {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}
        base_filled = filled_base_qty(placed)
        if plan is None:
            return _result(
                ok=False,
                dry_run=False,
                executed=0,
                submitted=True,
                error="maker_entry_missing_exit_plan",
                symbol=symbol,
                order=order,
                exchange_order_id=order_id,
                gates=gates,
                intent=intent,
                consumer_allowed=False,
            )
        if terminal and base_filled:
            filled = self._verified_order_base(placed, symbol=symbol, side="BUY")
            if not filled:
                arm = {
                    "decision_log_id": int(str(sig.get("decision_log_id")).strip()),
                    "symbol": symbol,
                    "quantity": base_filled,
                    "unprotected_qty": base_filled,
                    "entry_order_id": order_id,
                    "stop_price": plan["stop_price"],
                    "take_profit_price": plan["take_profit_price"],
                    "status": "oco_failed",
                    "protection": PROTECTION_PENDING,
                    "last_error": "fee_unverified",
                    "manual_action": "halt_new_buys_and_manually_close_or_protect_the_fill",
                    "purpose": str(sig.get("purpose") or "").strip() or PURPOSE_LIVE_BNB_BOLL_FADE,
                }
                labeled = _result(
                    ok=False,
                    dry_run=False,
                    executed=0,
                    submitted=True,
                    error="fee_unverified",
                    symbol=symbol,
                    order=order,
                    exchange_order_id=order_id,
                    gates=gates,
                    intent=intent,
                    exit_plan=plan,
                    filled_qty=base_filled,
                    sellable_qty=None,
                    arm_position=arm,
                    consumer_allowed=False,
                )
                return self._label_fill_protection(labeled, qty=str(base_filled))
        else:
            filled = base_filled
        if terminal and not filled:
            return _result(
                ok=False,
                dry_run=False,
                executed=0,
                submitted=True,
                error="maker_entry_not_filled",
                symbol=symbol,
                order=order,
                exchange_order_id=order_id,
                gates=gates,
                intent=intent,
                exit_plan=plan,
                consumer_allowed=False,
            )
        avg = fill_average_price(placed)
        arm: dict[str, Any] = {
            "decision_log_id": int(str(sig.get("decision_log_id")).strip()),
            "symbol": symbol,
            "quantity": filled,
            "entry_order_qty": order.get("quantity"),
            "entry_limit_price": order.get("price"),
            "entry_price": avg or order.get("price"),
            "entry_order_id": order_id,
            "stop_price": plan["stop_price"],
            "take_profit_price": plan["take_profit_price"],
            "status": "maker_resting",
            "protection": "maker_resting",
            "purpose": str(sig.get("purpose") or "").strip() or PURPOSE_LIVE_BNB_BOLL_FADE,
            "created_at_ms": str(int(self.clock_ms())),
        }
        extras = sig.get("extras") if isinstance(sig.get("extras"), dict) else {}
        self._apply_hold_passthrough(arm, extras)
        return _result(
            ok=False,
            dry_run=False,
            executed=0,
            submitted=True,
            error="maker_entry_resting",
            symbol=symbol,
            order=order,
            exchange_order_id=order_id,
            gates=gates,
            intent=intent,
            exit_plan=plan,
            filled_qty=filled,
            sellable_qty=filled if terminal else None,
            arm_position=arm,
            consumer_allowed=False,
        )

    def execute(self, sig: dict[str, Any]) -> dict[str, Any]:
        self._size_plan = None
        self._entry_mark = None
        self._orderbook_metrics = None
        self._mark_telemetry = None
        self._oco_preflight = None
        self._resting_ceiling = None
        self._resting_ceiling_source = None
        self._resting_ask_age_ms = None
        gates = self._gates()
        purpose = str(sig.get("purpose") or "").strip()
        if not is_live_bnb_spot_purpose(purpose):
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
        shell_filled = False
        if purpose == PURPOSE_LIVE_BNB_BOLL_CLASSIC:
            extras, shell_filled = apply_classic_exit_shell(extras)
        extras = _apply_recommended_order_type(extras, sig.get("action"))
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
        if isinstance(plan, dict) and shell_filled:
            plan["price_source"] = CLASSIC_EXIT_SHELL
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

        side_now = _parse_side(sig.get("action"))
        if gates["kill_switch_active"] and side_now == "SELL":
            # The kill file blocks a new buy only. An inbound signal is not a
            # ledger-tracked protective sell, so it is not sent.
            return self._finish(
                _result(
                    ok=False,
                    dry_run=False,
                    executed=0,
                    submitted=False,
                    error="inbound_sell_not_protective",
                    symbol=symbol,
                    order=None,
                    exchange_order_id=None,
                    gates=gates,
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
        if side_now == "BUY":
            buy_block = self._live_buy_block(sig, plan, symbol)
            if buy_block:
                return self._finish(
                    _result(
                        ok=False,
                        dry_run=False,
                        executed=0,
                        submitted=False,
                        error=buy_block,
                        symbol=symbol,
                        order=None,
                        exchange_order_id=None,
                        gates=gates,
                        exit_plan=plan,
                        consumer_allowed=False,
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
        if self.client is None:
            if not self.allow_rest_http:
                return self._finish(
                    _result(
                        ok=False,
                        dry_run=False,
                        executed=0,
                        submitted=False,
                        error="mcp_gate_required",
                        symbol=symbol,
                        order=None,
                        exchange_order_id=None,
                        gates=gates,
                        exit_plan=plan,
                        consumer_allowed=False,
                    )
                )
            self.client = BinanceSpotHttpClient()
        if side_now == "BUY" and not callable(getattr(self.client, "new_oco", None)):
            return self._finish(
                _result(
                    ok=False,
                    dry_run=False,
                    executed=0,
                    submitted=False,
                    error="native_oco_required",
                    symbol=symbol,
                    order=None,
                    exchange_order_id=None,
                    gates=gates,
                    exit_plan=plan,
                    consumer_allowed=False,
                )
            )

        client = self.client
        try:
            info = client.exchange_info(symbol)
        except Exception as exc:  # noqa: BLE001 — surface a redacted refusal, no retry
            error, exchange_error = _exchange_failure(exc, key, secret)
            return self._finish(
                _result(
                    ok=False,
                    dry_run=False,
                    executed=0,
                    submitted=False,
                    error=error,
                    symbol=symbol,
                    order=dict(pairs),
                    exchange_order_id=None,
                    gates=gates,
                    exit_plan=plan,
                    exchange_error=exchange_error,
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
            filter_error = _oco_filters_ready(info, symbol)
            if filter_error:
                return self._finish(
                    _result(
                        ok=False,
                        dry_run=False,
                        executed=0,
                        submitted=False,
                        error=filter_error,
                        symbol=symbol,
                        order=None,
                        exchange_order_id=None,
                        gates=gates,
                        exit_plan=plan,
                        consumer_allowed=False,
                    )
                )
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

        if side_now == "BUY" and isinstance(plan, dict):
            mark, mark_error = self._read_entry_mark(client, symbol)
            order_type_now = _parse_order_type(
                extras.get("order_type", extras.get("binance_order_type"))
            )
            tel = getattr(self, "_mark_telemetry", None)
            mark_source = tel.get("mark_source") if isinstance(tel, dict) else None
            if mark_error or mark is None:
                self._oco_preflight = {
                    "order_type": order_type_now,
                    "reference": None,
                    "reference_price": None,
                    "mark": None,
                    "mark_source": mark_source,
                    "ceiling_price": None,
                    "ceiling_source": None,
                    "aligned_stop": None,
                    "aligned_take_profit": None,
                    "aligned_limit": None,
                    "planned_limit": None,
                    "mark_inside_band": None,
                }
                return self._finish(
                    _result(
                        ok=False,
                        dry_run=False,
                        executed=0,
                        submitted=False,
                        error=mark_error or "mark_unavailable",
                        symbol=symbol,
                        order=None,
                        exchange_order_id=None,
                        gates=gates,
                        exit_plan=plan,
                        consumer_allowed=False,
                    )
                )
            self._entry_mark = mark
            # MARKET keeps the live-mark band. A resting limit is checked
            # after pegging, against its own planned entry.
            if order_type_now == "MARKET":
                filters = _symbol_filters(info, symbol) or []
                diag, pre_err = buy_entry_oco_preflight(
                    order_type="MARKET",
                    limit_price=None,
                    stop_price=plan.get("stop_price"),
                    take_profit_price=plan.get("take_profit_price"),
                    mark=mark,
                    mark_source=mark_source,
                    filters=filters,
                )
                self._oco_preflight = diag
                if pre_err:
                    return self._finish(
                        _result(
                            ok=False,
                            dry_run=False,
                            executed=0,
                            submitted=False,
                            error=pre_err,
                            symbol=symbol,
                            order=None,
                            exchange_order_id=None,
                            gates=gates,
                            exit_plan=plan,
                            consumer_allowed=False,
                        )
                    )
                if not self._auto_flatten_enabled():
                    # A MARKET fill can still be left naked if the OCO is
                    # rejected. Deployed defaults do not sell that fill.
                    # Do not send the buy. This does not enable auto-flatten.
                    diag["protection_disposition"] = "unconfigured"
                    self._oco_preflight = diag
                    return self._finish(
                        _result(
                            ok=False,
                            dry_run=False,
                            executed=0,
                            submitted=False,
                            error="buy_protection_unconfigured",
                            symbol=symbol,
                            order=None,
                            exchange_order_id=None,
                            gates=gates,
                            exit_plan=plan,
                            consumer_allowed=False,
                        )
                    )
            if order_type_now == "LIMIT" and (
                self._post_only_limit_enabled() or extras.get("post_only") is True
            ):
                extras = dict(extras)
                extras["order_type"] = "LIMIT_MAKER"
                order_type_now = "LIMIT_MAKER"

            if order_type_now == "LIMIT_MAKER":
                best_bid_px, best_ask_px, book_error = self._maker_top_of_book(client, symbol)
                if book_error:
                    return self._finish(
                        _result(
                            ok=False,
                            dry_run=False,
                            executed=0,
                            submitted=False,
                            error=book_error,
                            symbol=symbol,
                            order=None,
                            exchange_order_id=None,
                            gates=gates,
                            exit_plan=plan,
                            consumer_allowed=False,
                        )
                    )
                self._resting_ceiling = best_ask_px
                raw_lim, _ = _chosen_limit_raw(extras)
                filters = _symbol_filters(info, symbol) or []
                pegged_px, was_clamped, peg_err = peg_maker_buy_price(
                    raw_lim,
                    best_bid=best_bid_px,
                    best_ask=best_ask_px,
                    mark=self._entry_mark,
                    stop_price=plan.get("stop_price"),
                    take_profit_price=plan.get("take_profit_price"),
                    filters=filters,
                )
                if peg_err or pegged_px is None:
                    return self._finish(
                        _result(
                            ok=False,
                            dry_run=False,
                            executed=0,
                            submitted=False,
                            error=peg_err or "maker_peg_failed",
                            symbol=symbol,
                            order=None,
                            exchange_order_id=None,
                            gates=gates,
                            exit_plan=plan,
                            consumer_allowed=False,
                        )
                    )
                extras = dict(extras)
                extras["order_type"] = "LIMIT_MAKER"
                extras["limit_price"] = pegged_px
                if extras.get("price") not in (None, ""):
                    extras["price"] = pegged_px
                plan = dict(plan)
                plan["entry_price"] = pegged_px
                if was_clamped:
                    plan["maker_pegged"] = True
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

            if order_type_now == "MARKET" and self._orderbook_gate_enabled():
                depth_payload = None
                ob_source = "rest_fallback"
                ob_age_ms: int | None = None
                ws_cache = self._active_ws_cache()
                if ws_cache is not None and callable(getattr(ws_cache, "get_order_book", None)):
                    ws_book = ws_cache.get_order_book(symbol)
                    if isinstance(ws_book, dict):
                        depth_payload = ws_book
                        ob_source = str(ws_book.get("source") or "ws_l2")
                        ob_age_ms = int(ws_book.get("ws_age_ms") or 0)
                if depth_payload is None:
                    depth_fn = getattr(client, "order_book", None)
                    if callable(depth_fn):
                        try:
                            depth_payload = depth_fn(symbol, limit=20)
                        except Exception:  # noqa: BLE001
                            depth_payload = None
                if isinstance(depth_payload, dict):
                    order_map = dict(pairs)
                    ob_metrics, ob_error = evaluate_orderbook_impact(
                        depth_payload,
                        quote_usdt=order_map.get("quoteOrderQty"),
                        quantity=order_map.get("quantity"),
                        spread_cap_bps=_to_decimal(extras.get("max_spread_bps")),
                        impact_cap_bps=_to_decimal(extras.get("max_impact_bps")),
                    )
                    if isinstance(ob_metrics, dict):
                        ob_metrics["market_data_source"] = ob_source
                        if ob_age_ms is not None:
                            ob_metrics["ws_age_ms"] = ob_age_ms
                    self._orderbook_metrics = ob_metrics
                    if ob_error:
                        sor_converted = False
                        if self._sor_maker_fallback_enabled() and isinstance(ob_metrics, dict):
                            filters = _symbol_filters(info, symbol) or []
                            sor_spec, _sor_err = sor_maker_fallback_spec(
                                quote_usdt=order_map.get("quoteOrderQty"),
                                best_bid=ob_metrics.get("best_bid"),
                                best_ask=ob_metrics.get("best_ask"),
                                stop_price=plan.get("stop_price"),
                                take_profit_price=plan.get("take_profit_price"),
                                filters=filters,
                            )
                            if sor_spec is not None:
                                extras = dict(extras)
                                extras.pop("quote_order_qty", None)
                                extras.pop("quoteOrderQty", None)
                                extras["order_type"] = "LIMIT_MAKER"
                                extras["limit_price"] = sor_spec["price"]
                                extras["quantity"] = sor_spec["quantity"]
                                new_pairs, new_err = self._preview_pairs(sig, extras, symbol)
                                if new_pairs is not None and new_err is None:
                                    pairs = new_pairs
                                    plan = dict(plan)
                                    plan["entry_price"] = sor_spec["price"]
                                    plan["sor_routed"] = "limit_maker_at_best_bid"
                                    self._resting_ceiling = str(ob_metrics.get("best_ask") or "")
                                    self._resting_ceiling_source = "rest_book"
                                    ob_metrics["sor_routed"] = "limit_maker_at_best_bid"
                                    ob_metrics["sor_original_block"] = ob_error
                                    ob_metrics["sor_maker_price"] = sor_spec["price"]
                                    ob_metrics["sor_maker_quantity"] = sor_spec["quantity"]
                                    sor_converted = True
                        if not sor_converted:
                            return self._finish(
                                _result(
                                    ok=False,
                                    dry_run=False,
                                    executed=0,
                                    submitted=False,
                                    error=ob_error,
                                    symbol=symbol,
                                    order=None,
                                    exchange_order_id=None,
                                    gates=gates,
                                    exit_plan=plan,
                                    consumer_allowed=False,
                                )
                            )

        if side_now == "BUY" and isinstance(plan, dict):
            final_type = _parse_order_type(
                extras.get("order_type", extras.get("binance_order_type"))
            )
            if final_type in {"LIMIT", "LIMIT_MAKER"}:
                extras, plan, pairs, refused = self._resting_limit_preflight(
                    sig=sig,
                    extras=extras,
                    plan=plan,
                    pairs=pairs,
                    info=info,
                    symbol=symbol,
                    gates=gates,
                )
                if refused is not None:
                    return refused

        posted = {key_name: value for key_name, value in pairs}
        if posted.get("side") == "BUY" and posted.get("type") in {"LIMIT", "LIMIT_MAKER"}:
            if not _maker_entry_lifecycle_ready(client):
                return self._finish(
                    _result(
                        ok=False,
                        dry_run=False,
                        executed=0,
                        submitted=False,
                        error="maker_lifecycle_unsupported",
                        symbol=symbol,
                        order=None,
                        exchange_order_id=None,
                        gates=gates,
                        exit_plan=plan,
                        consumer_allowed=False,
                    )
                )
        signed = sign_query(pairs, secret)
        try:
            placed = client.new_order(signed, api_key=key)
        except Exception as exc:  # noqa: BLE001 — rejection is final; do not post again
            error, exchange_error = _exchange_failure(exc, key, secret)
            return self._finish(
                _result(
                    ok=False,
                    dry_run=False,
                    executed=0,
                    submitted=False,
                    error=error,
                    symbol=symbol,
                    order=dict(pairs),
                    exchange_order_id=None,
                    gates=gates,
                    exit_plan=plan,
                    exchange_error=exchange_error,
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
        filled = filled_base_qty(placed)
        sellable = (
            self._verified_order_base(placed, symbol=symbol, side="BUY")
            if isinstance(placed, dict)
            else None
        )
        entry_evidence = self._with_proved_fills(placed) if isinstance(placed, dict) else None
        ack_status = str(placed.get("status") or "").upper()
        self._same_call_entry_proof = None
        if (
            sellable
            and ack_status in _FINAL_ORDER_STATUS
            and isinstance(placed, dict)
        ):
            self._same_call_entry_proof = {
                "symbol": symbol,
                "orderId": str(order_id),
                "side": "BUY",
                "status": ack_status,
                "executedQty": placed.get("executedQty"),
                "net": sellable,
                "payload": placed,
            }
        if side_now == "BUY" and order_type in {"LIMIT", "LIMIT_MAKER"} and ack_status != "FILLED":
            return self._finish(
                self._resting_maker_ack(
                    sig,
                    plan=plan if isinstance(plan, dict) else None,
                    gates=gates,
                    intent=intent,
                    pairs=pairs,
                    order_id=str(order_id),
                    placed=placed,
                    symbol=symbol,
                )
            )
        if side_now == "BUY":
            try:
                bought = self._buy_with_native_oco(
                    sig,
                    plan=plan,
                    gates=gates,
                    intent=intent,
                    pairs=pairs,
                    order_id=str(order_id),
                    filled=filled,
                    sellable=sellable,
                    info=info,
                    symbol=symbol,
                    placed=placed,
                )
            finally:
                self._same_call_entry_proof = None
            if isinstance(entry_evidence, dict):
                bought["entry_fill"] = entry_evidence
            return self._finish(bought)
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
                filled_qty=filled,
                sellable_qty=sellable,
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
        if order_type in ("LIMIT", "LIMIT_MAKER") and _format_positive(limit_raw) is None:
            if limit_raw in (None, ""):
                return None, "missing_limit_price"
            return None, "invalid_limit_price"
        if order_type in ("LIMIT", "LIMIT_MAKER") and not has_qty:
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

    def refresh_filled_qty(self, position: dict[str, Any]) -> tuple[str | None, str | None]:
        """Verified entry net, plus ``fee_unverified`` when a final fill has no commission.

        A still-open order is ``(None, None)``. Gross ``executedQty`` is not returned.
        """
        gates = self._gates()
        if not gates["live_enabled"]:
            return None, None
        symbol = position.get("symbol")
        order_id = position.get("entry_order_id")
        if not isinstance(symbol, str) or not is_spot_usdt_symbol(symbol):
            return None, None
        if order_id is None or not str(order_id).strip():
            return None, None
        key, secret = self._credentials()
        if not key or not secret:
            return None, None
        client = self.client if self.client is not None else BinanceSpotHttpClient()
        lookup = getattr(client, "get_order", None)
        if not callable(lookup):
            return None, None
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
            return None, None
        if not isinstance(placed, dict):
            return None, None
        if final_executed_qty(placed) is None:
            return None, None
        net = self._verified_order_base(placed, symbol=symbol, side="BUY")
        if net is None:
            return None, "fee_unverified:" + str(_positive_decimal(placed.get("executedQty")) or "")
        return net, None

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
        qty = self._verified_order_base_for_exit(position)
        if qty is None:
            block = getattr(self, "_exit_fee_block", None) or "exit_qty_unconfirmed"
            stored = _positive_decimal(position.get("quantity"))
            dust_client = self._trade_client()
            if block != "exit_qty_unconfirmed" and stored is not None and isinstance(symbol, str) and dust_client is not None:
                try:
                    dust_info = dust_client.exchange_info(symbol)
                except Exception as exc:  # noqa: BLE001
                    if isinstance(exc, AssertionError):
                        raise
                    dust_info = None
                if isinstance(dust_info, dict):
                    _dust_sized, dust_error = size_exit_quantity(dust_info, symbol, stored, mark)
                    if dust_detail(dust_error):
                        outcome["error"] = "dust_untradable"
                        outcome["quantity"] = stored
                        outcome["book"] = "closed_dust"
                        outcome["protection"] = "dust_abandon"
                        return self._log_exit(outcome)
            outcome["error"] = "fee_unverified" if str(block).startswith("manual_intervention_required") else block
            outcome["quantity"] = position.get("quantity")
            outcome["book"] = "error_only" if block == "exit_qty_unconfirmed" else str(position.get("status") or "open")
            if str(block).startswith("manual_intervention_required"):
                outcome["manual_action"] = "halt_new_buys_and_manually_close_or_protect_the_fill"
                outcome["protection_incident"] = _protection_incident("unprotected")
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
                client,
                symbol,
                client_id,
                key,
                secret,
                entry_order_id=position.get("entry_order_id"),
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
        if status in {"maker_resting", "maker_cancel_unknown"} or row.get("reconcile_resting_entry") is True:
            return self.reconcile_resting_entry(row, mark=mark, persist=persist)
        if status == "time_stop_unknown":
            return self._resume_time_stop(row, mark=mark, save=save)
        if status == "qty_unconfirmed":
            return self._coverage(
                row,
                book="unchanged",
                error="exit_qty_unconfirmed",
                submitted=False,
            )
        err = str(row.get("last_error") or "")
        if (
            err.startswith("flatten_order_")
            or err.startswith("manual_intervention_required:")
            or err in {"flatten_absent", "flatten_filled_partial", "flatten_lookup_wrong_order"}
        ):
            return self._coverage(
                row,
                book="oco_failed",
                submitted=False,
                error=err,
                protection=PROTECTION_PENDING,
            )
        if (
            err.startswith("flatten_submit_unknown")
            or err == "flatten_sell_open"
            or (
                err.startswith("oco_failed_auto_flattened:")
                and err.endswith("_unconfirmed")
            )
        ):
            return self._resume_unknown_flatten(row, mark, save)
        if err == "fee_unverified":
            held = self._coverage(
                row,
                book="oco_failed",
                submitted=False,
                error="fee_unverified",
                protection=PROTECTION_PENDING,
                quantity=str(row.get("quantity") or "") or None,
            )
            held["manual_action"] = (
                row.get("manual_action")
                or "halt_new_buys_and_manually_close_or_protect_the_fill"
            )
            held["protection_incident"] = _protection_incident("unprotected")
            held["unprotected_qty"] = row.get("unprotected_qty") or row.get("quantity")
            return held
        if status in {"closed", "closed_dust", "exit_submitted", "oco_filled"}:
            return self._coverage(
                row,
                book="unchanged",
                error="position_not_open",
                submitted=False,
                ok=True,
            )

        protection = str(row.get("protection") or "")
        retry_place = False
        if status in {"oco_unknown", "oco_working", "oco_partial"} or protection in {
            PROTECTION_PENDING,
            PROTECTION_EXCHANGE,
        }:
            recon = self._reconcile_oco(row)
            state = str(recon.get("state") or "")
            if state in {"working", "partial_working"}:
                if native_oco_verified(_recon_payload(recon))[0] is None:
                    save(
                        {
                            "status": "oco_unknown",
                            "protection": PROTECTION_PENDING,
                            "last_error": "native_oco_unverified",
                        }
                    )
                    return self._coverage(
                        row,
                        book="oco_unknown",
                        submitted=False,
                        error="native_oco_unverified",
                        protection=PROTECTION_PENDING,
                    )
                close_state = self._account_oco_execution(row, recon, save)
                mapped = self._coverage_from_account(row, recon, close_state)
                if mapped is not None:
                    return mapped
                held_book = self._finalize_placed_oco(
                    row,
                    recon,
                    save,
                    residual_qty=row.get("residual_qty"),
                    submitted=False,
                )
                hold_limit_sec = self._effective_max_hold_sec(row)
                if hold_limit_sec > 0:
                    created_ms = _row_epoch_ms(row)
                    now_ms = int(self.clock_ms())
                    if created_ms is not None and (now_ms - int(created_ms)) >= hold_limit_sec * 1000:
                        ts_out = self._time_stop_working_oco(row, mark=mark, save=save)
                        if ts_out is not None:
                            return ts_out
                if str(row.get("status") or "") != "oco_working":
                    return held_book
                if (
                    str(row.get("status") or "") == "oco_working"
                    and self._trailing_ratchet_enabled()
                    and _to_decimal(mark) is not None
                ):
                    ratcheted = self._maybe_ratchet_working_oco(row, mark=mark, save=save)
                    if ratcheted is not None:
                        return ratcheted
                residual_left = _to_decimal(row.get("residual_qty")) or _to_decimal(row.get("unprotected_qty"))
                uncovered = residual_left is not None and residual_left > Decimal("0.00000001")
                return self._coverage(
                    row,
                    book="oco_working",
                    submitted=False,
                    ok=not uncovered,
                    protection=PROTECTION_EXCHANGE,
                    error="oco_lot_residual" if uncovered else None,
                )
            if state == "filled":
                fields = self._official_oco_fill_fields(row, recon)
                if fields is not None:
                    save(fields)
                    return self._coverage(
                        row,
                        book="oco_filled",
                        submitted=False,
                        ok=True,
                        protection=PROTECTION_EXCHANGE,
                        error=None,
                        reason=recon.get("exit_reason"),
                    )
                close_state = self._account_oco_execution(row, recon, save)
                mapped = self._coverage_from_account(row, recon, close_state)
                if mapped is not None:
                    return mapped
                state = "partial"
            if state == "requery_failed":
                error = str(recon.get("error") or "oco_requery_failed")
                prior = str(row.get("last_error") or "")
                clear_ev = (
                    {col: None for col in OCO_READBACK_COLUMNS}
                    if any(row.get(col) not in (None, "") for col in OCO_READBACK_COLUMNS)
                    else {}
                )
                # Keep the rejection that explains the naked fill. This tick
                # still reports the query failure and does not post again.
                if not (
                    prior.startswith("oco_rejected:") or prior.startswith("oco_unprotectable:")
                ):
                    save({"last_error": error, **clear_ev})
                elif clear_ev:
                    save(clear_ev)
                if status == "oco_failed":
                    book = "oco_failed"
                elif status == "oco_working":
                    book = "oco_working"
                else:
                    book = "oco_unknown"
                return self._coverage(
                    row,
                    book=book,
                    submitted=False,
                    error=error,
                    protection=row.get("protection") or PROTECTION_PENDING,
                )
            if state == "absent":
                allowed, band = self._repost_band(row, mark)
                if not allowed:
                    prior = str(row.get("last_error") or "")
                    if prior.startswith("oco_rejected:") or prior.startswith("oco_unprotectable:"):
                        stored = prior
                    elif band in {"oco_price_restriction", "oco_prices_crossed"}:
                        stored = f"oco_unprotectable:{band}"
                    else:
                        stored = prior or band or "oco_list_absent"
                    save(
                        {
                            "status": "oco_failed",
                            "protection": PROTECTION_PENDING,
                            "last_error": stored,
                            "manual_action": "halt_new_buys_and_manually_close_or_protect_the_fill",
                        }
                    )
                    if self._auto_flatten_enabled() and str(row.get("status") or "") not in {
                        "closed_dust",
                        "oco_partial",
                    }:
                        return self._auto_flatten_naked_position(
                            row,
                            info=None,
                            mark=mark,
                            oco_error=stored,
                            save=save,
                        )
                    save(
                        {
                            "status": "oco_failed",
                            "protection": PROTECTION_PENDING,
                            "last_error": stored,
                        }
                    )
                    return self._coverage(
                        row,
                        book="oco_failed",
                        submitted=False,
                        error=stored,
                        protection=PROTECTION_PENDING,
                    )
                retry_place = True
            if state in {"unfilled", "partial"}:
                close_state = self._account_oco_execution(row, recon, save)
                mapped = self._coverage_from_account(row, recon, close_state)
                if mapped is not None:
                    return mapped
                if close_state == "unproved":
                    return self._coverage(
                        row,
                        book="oco_failed",
                        submitted=False,
                        ok=False,
                        error="fee_unverified",
                        protection=PROTECTION_PENDING,
                    )
                remainder = row.get("quantity")
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
                if remainder is not None:
                    row["quantity"] = str(remainder)
                if self._auto_flatten_enabled() and str(row.get("status") or "") not in {
                    "closed_dust",
                    "oco_partial",
                }:
                    return self._auto_flatten_naked_position(
                        row,
                        info=None,
                        mark=mark,
                        oco_error=detail,
                        save=save,
                    )
                save(
                    {
                        "status": "oco_failed",
                        "protection": PROTECTION_PENDING,
                        "quantity": str(remainder) if remainder is not None else row.get("quantity"),
                        "oco_list_status": recon.get("list_status"),
                        "last_error": detail,
                    }
                )
                return self._coverage(
                    row,
                    book="oco_failed",
                    submitted=False,
                    error=detail,
                    protection=PROTECTION_PENDING,
                )
            elif state and state != "absent":
                save({"last_error": recon.get("error") or "oco_requery_failed"})
                return self._coverage(
                    row,
                    book="oco_unknown",
                    submitted=False,
                    error=recon.get("error") or "oco_requery_failed",
                    protection=PROTECTION_PENDING,
                )

        if retry_place or str(row.get("status") or "") in {"open", "oco_failed"}:
            placed = self._try_place_oco(row, mark, save)
            if placed is not None:
                if (
                    self._auto_flatten_enabled()
                    and isinstance(placed, dict)
                    and placed.get("book") == "oco_failed"
                ):
                    return self._auto_flatten_naked_position(
                        row,
                        info=None,
                        mark=mark,
                        oco_error=str(placed.get("error") or "oco_failed"),
                        save=save,
                        exchange_error=placed.get("exchange_error"),
                    )
                return placed

        return self._coverage(
            row,
            book="oco_failed",
            submitted=False,
            error=str(row.get("last_error") or "native_oco_unverified"),
            protection=PROTECTION_PENDING,
        )

    def _fill_left_original_band(
        self,
        row: dict[str, Any],
        mark: object,
        fill: object,
    ) -> str | None:
        """Error when the current mark or the fill average is outside the original band."""
        if _to_decimal(mark) is None:
            return "mark_unavailable"
        info, err = self._load_symbol_info(row, self._gates())
        if err or not isinstance(info, dict):
            return err or "oco_missing_lot_filter"
        filters = _symbol_filters(info, str(row.get("symbol") or ""))
        if not filters:
            return "oco_missing_lot_filter"
        band = protective_band_error(
            stop_price=str(row.get("stop_price") or ""),
            take_profit_price=str(row.get("take_profit_price") or ""),
            mark=mark,
            filters=filters,
        )
        if band:
            return band
        if _to_decimal(fill) is None:
            return None
        return protective_band_error(
            stop_price=str(row.get("stop_price") or ""),
            take_profit_price=str(row.get("take_profit_price") or ""),
            mark=fill,
            filters=filters,
        )

    def _reanchor_open_row(
        self,
        row: dict[str, Any],
        save: Callable[[dict[str, Any]], None],
        *,
        mark: object,
        fill_price: object,
        signal_entry: object,
    ) -> bool:
        """Move stop and take-profit to the actual fill and mark. False leaves them unchanged."""
        info, err = self._load_symbol_info(row, self._gates())
        if err or not isinstance(info, dict):
            return False
        filters = _symbol_filters(info, str(row.get("symbol") or ""))
        if not filters:
            return False
        reanchored, re_err = reanchor_oco_prices_to_fill(
            signal_entry_price=signal_entry,
            stop_price=str(row.get("stop_price") or ""),
            take_profit_price=str(row.get("take_profit_price") or ""),
            fill_price=fill_price,
            mark=mark,
            filters=filters,
        )
        if reanchored is None or re_err:
            return False
        save(
            {
                "stop_price": reanchored["stop_price"],
                "take_profit_price": reanchored["take_profit_price"],
            }
        )
        row["stop_price"] = reanchored["stop_price"]
        row["take_profit_price"] = reanchored["take_profit_price"]
        row["oco_reanchored"] = True
        return True

    def _adopt_terminal_entry(
        self,
        row: dict[str, Any],
        save: Callable[[dict[str, Any]], None],
        payload: dict[str, Any],
        mark: object,
        persist: Callable[[dict[str, Any]], dict[str, Any] | None] | None,
        filled_qty: str | None,
        avg_px: str | None,
    ) -> dict[str, Any]:
        """OCO only the base the entry order actually filled. Zero is terminal."""
        gross_filled = filled_base_qty(payload)
        existing_qty = _positive_decimal(row.get("quantity"))
        verified_net_qty = _positive_decimal(filled_qty)
        has_exposure = gross_filled is not None or existing_qty is not None
        if has_exposure and verified_net_qty is None:
            keep_qty = existing_qty or gross_filled
            save(
                {
                    "status": "maker_cancel_unknown",
                    "last_error": "manual_intervention_required:maker_fee_unverified",
                    "quantity": keep_qty,
                    "unprotected_qty": keep_qty,
                    "manual_action": "halt_new_buys_and_manually_close_or_protect_the_fill",
                }
            )
            held = self._coverage(
                row,
                book="maker_cancel_unknown",
                submitted=False,
                error="fee_unverified",
                protection="maker_resting",
                quantity=keep_qty,
            )
            held["manual_action"] = "halt_new_buys_and_manually_close_or_protect_the_fill"
            held["protection_incident"] = _protection_incident("unprotected")
            return held
        if verified_net_qty is None:
            filled_qty = None
        status = str(payload.get("status") or "").upper()
        final_qty = final_executed_qty(payload)
        terminal_fill = status == "FILLED" or (
            status in {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}
            and final_qty is not None
        )
        if terminal_fill:
            if not filled_qty:
                if final_executed_qty(payload) is not None:
                    save(
                        {
                            "status": "oco_failed",
                            "protection": PROTECTION_PENDING,
                            "last_error": "fee_unverified",
                            "quantity": str(final_qty),
                            "unprotected_qty": str(final_qty),
                            "manual_action": "halt_new_buys_and_manually_close_or_protect_the_fill",
                        }
                    )
                    return self._coverage(
                        row,
                        book="oco_failed",
                        submitted=False,
                        error="fee_unverified",
                        protection=PROTECTION_PENDING,
                    )
                save(
                    {
                        "status": "maker_cancel_unknown",
                        "last_error": "maker_fill_unconfirmed",
                    }
                )
                return self._coverage(
                    row,
                    book="maker_cancel_unknown",
                    submitted=False,
                    error="maker_fill_unconfirmed",
                    protection="maker_resting",
                )
            signal_entry = row.get("entry_limit_price") or row.get("entry_price")
            upd: dict[str, Any] = {"status": "open", "quantity": filled_qty}
            if avg_px:
                upd["entry_price"] = avg_px
            save(upd)
            recorder = getattr(self, "record_entry_origin", None)
            if callable(recorder) and row.get("id") is not None:
                recorder(
                    int(row["id"]),
                    symbol=str(row.get("symbol") or ""),
                    placed=self._with_proved_fills(payload if isinstance(payload, dict) else {}),
                )
            prior_proof = getattr(self, "_same_call_entry_proof", None)
            if isinstance(payload, dict) and status in _FINAL_ORDER_STATUS and filled_qty:
                self._same_call_entry_proof = {
                    "symbol": str(row.get("symbol") or ""),
                    "orderId": str(row.get("entry_order_id") or payload.get("orderId") or ""),
                    "side": "BUY",
                    "status": status,
                    "executedQty": payload.get("executedQty"),
                    "net": filled_qty,
                    "payload": payload,
                }
            try:
                if _to_decimal(mark) is None:
                    detail = "oco_unprotectable:mark_unavailable"
                    save(
                        {
                            "status": "oco_failed",
                            "protection": PROTECTION_PENDING,
                            "last_error": detail,
                        }
                    )
                    if self._auto_flatten_enabled():
                        return self._auto_flatten_naked_position(
                            row,
                            info=None,
                            mark=avg_px or signal_entry,
                            oco_error=detail,
                            save=save,
                        )
                    return self._coverage(
                        row,
                        book="oco_failed",
                        submitted=False,
                        error=detail,
                        protection=PROTECTION_PENDING,
                    )
                left = self._fill_left_original_band(row, mark, avg_px)
                if left and self._reanchor_enabled():
                    if self._reanchor_open_row(
                        row,
                        save,
                        mark=mark,
                        fill_price=avg_px,
                        signal_entry=signal_entry,
                    ):
                        left = None
                if left and self._auto_flatten_enabled() and not self._reanchor_enabled():
                    detail = f"oco_unprotectable:{left}"
                    save(
                        {
                            "status": "oco_failed",
                            "protection": PROTECTION_PENDING,
                            "last_error": detail,
                        }
                    )
                    return self._auto_flatten_naked_position(
                        row,
                        info=None,
                        mark=mark,
                        oco_error=detail,
                        save=save,
                    )
                return self.maintain_held_position(row, mark, persist=persist)
            finally:
                self._same_call_entry_proof = prior_proof
        save(
            {
                "status": "closed_canceled",
                "exit_reason": "entry_ttl_expired",
                "last_error": "entry_ttl_expired",
            }
        )
        return self._coverage(
            row,
            book="closed_canceled",
            submitted=True,
            ok=True,
            error=None,
            reason="entry_ttl_expired",
        )

    def _cancel_resting_entry(
        self,
        row: dict[str, Any],
        save: Callable[[dict[str, Any]], None],
        lookup: Any,
        symbol: str,
        entry_order_id: str,
        key: str,
        secret: str,
        mark: object,
        persist: Callable[[dict[str, Any]], dict[str, Any] | None] | None,
        clock_now: int,
    ) -> dict[str, Any]:
        """Cancel the open remainder, then read the fill once more before any OCO."""
        client = self._trade_client()
        cancel_fn = getattr(client, "cancel_order", None) if client is not None else None
        save(
            {
                "status": "maker_cancel_unknown",
                "last_error": "maker_cancel_pending",
            }
        )
        if not callable(cancel_fn):
            save({"last_error": "maker_cancel_unsupported"})
            return self._coverage(
                row,
                book="maker_cancel_unknown",
                submitted=False,
                error="maker_cancel_unsupported",
                protection="maker_resting",
            )
        pairs = [
            ("symbol", symbol),
            ("orderId", entry_order_id),
            ("recvWindow", RECV_WINDOW_MS),
            ("timestamp", str(clock_now)),
        ]
        try:
            cancel_fn(sign_query(pairs, secret), api_key=key)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, AssertionError):
                raise
            save({"last_error": "maker_cancel_unknown"})
            return self._coverage(
                row,
                book="maker_cancel_unknown",
                submitted=False,
                error="maker_cancel_unknown",
                protection="maker_resting",
            )
        try:
            fresh = lookup(sign_query(pairs, secret), api_key=key)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, AssertionError):
                raise
            save({"last_error": "maker_cancel_unknown"})
            return self._coverage(
                row,
                book="maker_cancel_unknown",
                submitted=False,
                error="maker_cancel_unknown",
                protection="maker_resting",
            )
        if not isinstance(fresh, dict):
            save({"last_error": "maker_cancel_unknown"})
            return self._coverage(
                row,
                book="maker_cancel_unknown",
                submitted=False,
                error="maker_cancel_unknown",
                protection="maker_resting",
            )
        fresh_status = str(fresh.get("status") or "").upper()
        if fresh_status in {"NEW", "PARTIALLY_FILLED", "PENDING_NEW"}:
            save({"last_error": "maker_cancel_unknown"})
            return self._coverage(
                row,
                book="maker_cancel_unknown",
                submitted=False,
                error="maker_cancel_unknown",
                protection="maker_resting",
            )
        filled_qty = self._verified_order_base(fresh, symbol=symbol, side="BUY")
        return self._adopt_terminal_entry(
            row,
            save,
            fresh,
            mark,
            persist,
            filled_qty,
            fill_average_price(fresh),
        )

    def reconcile_resting_entry(
        self,
        position: dict[str, Any],
        *,
        mark: object = None,
        now_ms: int | None = None,
        ttl_sec: int | None = None,
        persist: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        """Reconcile a resting ``LIMIT`` / ``LIMIT_MAKER`` entry order.

        A partial fill cancels the open remainder on that tick. The confirming
        read, not the cancel body, is the sellable quantity. Native OCO covers
        only that quantity. A cancel or read that does not finish stays
        ``maker_cancel_unknown``: the quote stays reserved and no OCO is sent.
        A zero-fill order is canceled only after ``ttl_sec`` (default 180s).
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

        gates = self._gates()
        if not gates.get("live_enabled"):
            return self._coverage(row, book="unchanged", submitted=False, error="binance_spot_live_disabled")
        symbol = str(row.get("symbol") or "")
        entry_order_id = str(row.get("entry_order_id") or "").strip()
        if not is_spot_usdt_symbol(symbol) or not entry_order_id:
            return self._coverage(row, book="unchanged", submitted=False, error="missing_entry_order_id")
        key, secret = self._credentials()
        if not key or not secret:
            return self._coverage(row, book="unchanged", submitted=False, error="missing_api_credentials")
        client = self._trade_client()
        lookup = getattr(client, "get_order", None) if client is not None else None
        if not callable(lookup):
            return self._coverage(row, book="unchanged", submitted=False, error="entry_requery_unsupported")
        clock_now = int(self.clock_ms()) if now_ms is None else int(now_ms)
        q_pairs = [
            ("symbol", symbol),
            ("orderId", entry_order_id),
            ("recvWindow", RECV_WINDOW_MS),
            ("timestamp", str(clock_now)),
        ]
        try:
            order_payload = lookup(sign_query(q_pairs, secret), api_key=key)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, AssertionError):
                raise
            return self._coverage(row, book="unchanged", submitted=False, error=_public_error(exc, key, secret))
        if not isinstance(order_payload, dict):
            return self._coverage(row, book="unchanged", submitted=False, error="entry_requery_failed")

        ord_status = str(order_payload.get("status") or "").upper()
        filled_qty = self._verified_order_base(order_payload, symbol=symbol, side="BUY")
        avg_px = fill_average_price(order_payload)
        created_ms = _row_epoch_ms(row)
        if created_ms is None:
            created_ms = _finite_number(order_payload.get("time"))
            created_ms = None if created_ms is None else int(created_ms)
        limit_ttl_sec = self._entry_ttl_sec() if ttl_sec is None else max(5, int(ttl_sec))
        expired = created_ms is not None and (clock_now - int(created_ms)) >= limit_ttl_sec * 1000

        terminal = ord_status in {
            "FILLED",
            "CANCELED",
            "CANCELLED",
            "EXPIRED",
            "REJECTED",
            "EXPIRED_IN_MATCH",
        }
        # A partial fill is inventory. Cancel the resting remainder immediately
        # and protect only the quantity the next read still shows. Do not wait
        # out the TTL with an unprotected fill.
        if ord_status == "PARTIALLY_FILLED" or (ord_status in {"NEW", "PARTIALLY_FILLED"} and expired):
            return self._cancel_resting_entry(
                row,
                save,
                lookup,
                symbol,
                entry_order_id,
                key,
                secret,
                mark,
                persist,
                clock_now,
            )
        if terminal:
            return self._adopt_terminal_entry(
                row, save, order_payload, mark, persist, filled_qty, avg_px
            )

        return self._coverage(
            row,
            book="entry_resting",
            submitted=False,
            ok=True,
            error=None,
        )

    def _oco_failed(
        self,
        row: dict[str, Any],
        save: Callable[[dict[str, Any]], None],
        error: str,
        *,
        exchange_error: dict[str, Any] | None = None,
        **extra: object,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "status": "oco_failed",
            "protection": PROTECTION_PENDING,
            "last_error": error,
        }
        fields.update(extra)
        save(fields)
        return self._coverage(
            row,
            book="oco_failed",
            submitted=False,
            error=error,
            protection=PROTECTION_PENDING,
            exchange_error=exchange_error,
        )

    def _cancel_working_oco(
        self,
        row: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Cancel a working OCO order list via ``DELETE /api/v3/orderList``."""
        gates = self._gates()
        blocked = protective_order_block(gates)
        if blocked:
            return None, blocked
        client = self._trade_client()
        cancel_fn = getattr(client, "cancel_order_list", None) if client is not None else None
        if not callable(cancel_fn):
            return None, "oco_cancel_unsupported"
        key, secret = self._credentials()
        if not key or not secret:
            return None, "missing_api_credentials"
        symbol = str(row.get("symbol") or "")
        list_order = row.get("oco_order_list_id")
        list_client = row.get("oco_list_client_id")
        if list_order not in (None, ""):
            ident = ("orderListId", str(list_order))
        elif list_client not in (None, ""):
            ident = ("listClientOrderId", str(list_client))
        else:
            return None, "oco_cancel_missing_id"
        pairs = [
            ("symbol", symbol),
            ident,
            ("recvWindow", RECV_WINDOW_MS),
            ("timestamp", str(int(self.clock_ms()))),
        ]
        try:
            payload = cancel_fn(sign_query(pairs, secret), api_key=key)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, AssertionError):
                raise
            return None, _public_error(exc, key, secret)
        if not isinstance(payload, dict):
            return None, "oco_cancel_invalid_response"
        return payload, None

    def _time_stop_working_oco(
        self,
        row: dict[str, Any],
        *,
        mark: object,
        save: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any] | None:
        """Hand a held row to the idempotent time-stop seller.

        The unknown state is stored before the market POST. A later tick must
        not place another OCO while that sell is unresolved.
        """
        client_id = row.get("exit_client_order_id") or exit_client_order_id(
            row.get("decision_log_id")
        )
        if client_id is None:
            return None
        save(
            {
                "status": "time_stop_unknown",
                "protection": PROTECTION_TIME_STOP,
                "exit_reason": "time_stop_max_hold",
                "exit_client_order_id": client_id,
                "last_error": "time_stop_pending",
            }
        )
        return self._resume_time_stop(row, mark=mark, save=save)

    def _resume_time_stop(
        self,
        row: dict[str, Any],
        *,
        mark: object,
        save: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        """Finish one max-hold sell. Never opens a new OCO from this state."""
        client_id = str(row.get("exit_client_order_id") or "")
        symbol = str(row.get("symbol") or "")
        key, secret = self._credentials()
        client = self._trade_client()
        if not client_id or not is_spot_usdt_symbol(symbol) or not key or not secret or client is None:
            return self._coverage(
                row,
                book="time_stop_unknown",
                submitted=False,
                error="time_stop_submit_unknown",
                protection=PROTECTION_TIME_STOP,
                reason="time_stop_max_hold",
            )
        first_attempt = str(row.get("last_error") or "") == "time_stop_pending"
        entry_oid = row.get("entry_order_id")
        found = self._lookup_exit_order(
            client, symbol, client_id, key, secret, entry_order_id=entry_oid
        )
        if found is not None:
            return self._close_time_stop(row, save, found, client_id, None)
        looked = self._exit_lookup_state(
            client, symbol, client_id, key, secret, entry_order_id=entry_oid
        )
        # A found sell is already settled above. Only a confirmed absence may
        # post. Unknown on the first attempt may post once. Mismatch and a
        # terminal partial must not post again.
        if looked not in {"absent"} and not (looked == "unknown" and first_attempt):
            save({"last_error": "time_stop_submit_unknown"})
            return self._coverage(
                row,
                book="time_stop_unknown",
                submitted=False,
                error="time_stop_submit_unknown",
                protection=PROTECTION_TIME_STOP,
                reason="time_stop_max_hold",
            )
        if self._max_hold_freeze_active():
            return self._coverage(
                row,
                book="time_stop_unknown",
                submitted=False,
                error="max_hold_disabled",
                protection=PROTECTION_TIME_STOP,
                reason="time_stop_max_hold",
            )
        recon = self._reconcile_oco(row)
        state = str(recon.get("state") or "")
        if state in {"working", "partial_working"}:
            canceled, cancel_err = self._cancel_working_oco(row)
            if cancel_err or canceled is None:
                save({"last_error": cancel_err or "time_stop_cancel_unconfirmed"})
                return self._coverage(
                    row,
                    book="time_stop_unknown",
                    submitted=False,
                    error=cancel_err or "time_stop_cancel_unconfirmed",
                    protection=PROTECTION_TIME_STOP,
                    reason="time_stop_max_hold",
                )
            classified = classify_oco(canceled, held_qty=str(row.get("quantity") or ""))
            if self._account_oco_execution(row, classified, save) == "closed":
                save(
                    {
                        "status": "oco_filled",
                        "protection": PROTECTION_EXCHANGE,
                        "exit_reason": classified.get("exit_reason"),
                        "oco_list_status": classified.get("list_status"),
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
                    reason=classified.get("exit_reason"),
                )
            if classified.get("state") not in {"unfilled", "partial", "absent"}:
                save({"last_error": "time_stop_cancel_unconfirmed"})
                return self._coverage(
                    row,
                    book="time_stop_unknown",
                    submitted=False,
                    error="time_stop_cancel_unconfirmed",
                    protection=PROTECTION_TIME_STOP,
                    reason="time_stop_max_hold",
                )
        elif state in {"requery_failed", "unknown", ""}:
            save({"last_error": str(recon.get("error") or "time_stop_list_unknown")})
            return self._coverage(
                row,
                book="time_stop_unknown",
                submitted=False,
                error=str(recon.get("error") or "time_stop_list_unknown"),
                protection=PROTECTION_TIME_STOP,
                reason="time_stop_max_hold",
            )
        elif state == "filled":
            save(
                {
                    "status": "oco_filled",
                    "protection": PROTECTION_EXCHANGE,
                    "exit_reason": recon.get("exit_reason"),
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
        return self._submit_time_stop_sell(row, mark, save, client_id)

    def _submit_time_stop_sell(
        self,
        row: dict[str, Any],
        mark: object,
        save: Callable[[dict[str, Any]], None],
        client_id: str,
    ) -> dict[str, Any]:
        gates = self._gates()
        symbol = str(row.get("symbol") or "")
        qty = _positive_decimal(self._verified_order_base_for_exit(row))
        key, secret = self._credentials()
        client = self._trade_client()
        if qty is None or client is None or not key or not secret:
            if qty is None:
                keep = row.get("quantity")
                save(
                    {
                        "last_error": "manual_intervention_required:fee_unverified",
                        "manual_action": "halt_new_buys_and_manually_close_or_protect_the_fill",
                        "unprotected_qty": keep,
                    }
                )
                held = self._coverage(
                    row,
                    book=str(row.get("status") or "time_stop_unknown"),
                    submitted=False,
                    error="fee_unverified",
                    protection=row.get("protection") or PROTECTION_TIME_STOP,
                    reason="time_stop_max_hold",
                    quantity=str(keep) if keep is not None else None,
                )
                held["manual_action"] = "halt_new_buys_and_manually_close_or_protect_the_fill"
                held["protection_incident"] = _protection_incident("unprotected")
                return held
            save({"last_error": "time_stop_submit_unknown"})
            return self._coverage(
                row,
                book="time_stop_unknown",
                submitted=False,
                error="time_stop_submit_unknown",
                protection=PROTECTION_TIME_STOP,
                reason="time_stop_max_hold",
            )
        info, info_err = self._load_symbol_info(row, gates)
        if info_err or not isinstance(info, dict):
            save({"last_error": info_err or "time_stop_submit_unknown"})
            return self._coverage(
                row,
                book="time_stop_unknown",
                submitted=False,
                error=info_err or "time_stop_submit_unknown",
                protection=PROTECTION_TIME_STOP,
                reason="time_stop_max_hold",
            )
        ref_mark = mark if _to_decimal(mark) is not None else row.get("entry_price")
        sized, size_err = size_exit_quantity(info, symbol, qty, ref_mark)
        if size_err or sized is None:
            save({"last_error": size_err or "time_stop_submit_unknown"})
            return self._coverage(
                row,
                book="time_stop_unknown",
                submitted=False,
                error=size_err or "time_stop_submit_unknown",
                protection=PROTECTION_TIME_STOP,
                reason="time_stop_max_hold",
            )
        pairs, qty_err = _order_pairs(
            symbol=symbol,
            side="SELL",
            order_type="MARKET",
            extras={"quantity": sized},
            client_id=client_id,
            timestamp_ms=int(self.clock_ms()),
        )
        if qty_err or pairs is None:
            save({"last_error": qty_err or "time_stop_submit_unknown"})
            return self._coverage(
                row,
                book="time_stop_unknown",
                submitted=False,
                error=qty_err or "time_stop_submit_unknown",
                protection=PROTECTION_TIME_STOP,
                reason="time_stop_max_hold",
            )
        try:
            placed = client.new_order(sign_query(pairs, secret), api_key=key)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, AssertionError):
                raise
            found = self._lookup_exit_order(
                client,
                symbol,
                client_id,
                key,
                secret,
                entry_order_id=row.get("entry_order_id"),
            )
            if found is None:
                save({"last_error": "time_stop_submit_unknown"})
                return self._coverage(
                    row,
                    book="time_stop_unknown",
                    submitted=False,
                    error="time_stop_submit_unknown",
                    protection=PROTECTION_TIME_STOP,
                    reason="time_stop_max_hold",
                    order=dict(pairs),
                    quantity=sized,
                )
            placed = found
        return self._settle_reduce_sell(
            row,
            save,
            placed if isinstance(placed, dict) else {},
            client_id,
            reason="time_stop_max_hold",
            protection=PROTECTION_TIME_STOP,
            error_prefix="time_stop_max_hold",
            order=dict(pairs),
            proved_held=qty,
        )

    def _proved_sell_reduction(
        self,
        row: dict[str, Any],
        order_ids: list[str],
    ) -> Decimal | None:
        """Sum fee-proved SELL reductions. None when a filled leg has no proof."""
        symbol = str(row.get("symbol") or "").strip()
        total = Decimal("0")
        saw_fill = False
        for order_id in order_ids:
            if not order_id:
                continue
            payload = self._read_order(symbol=symbol, order_id=order_id)
            if not isinstance(payload, dict):
                continue
            gross = _to_decimal(filled_base_qty(payload))
            if gross is None:
                continue
            saw_fill = True
            proved = self._verified_order_base(
                payload, symbol=symbol, side="SELL", allow_open=True
            )
            if proved is None:
                return None
            total += Decimal(proved)
        if not saw_fill:
            return None
        return total

    def _explicit_hold(self, raw: object) -> tuple[Decimal | None, str]:
        """``(value, 'ok'|'missing'|'invalid')``. Illegal numbers are not zero."""
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None, "missing"
        number = _nonneg_decimal(raw)
        if number is None:
            return None, "invalid"
        return number, "ok"

    def _full_net_and_slice(
        self,
        row: dict[str, Any],
        *,
        leg_orig: Decimal | None = None,
    ) -> tuple[Decimal | None, Decimal | None]:
        """Full fee-net and the uncovered slice of that same net.

        The placement writer stores the full net in ``quantity`` and the
        uncovered slice in ``residual_qty``. Adding them would count the
        slice twice. A leg origQty equal to ``quantity`` means an older row
        stored only the OCO size, and the slice is then added once.
        """
        qty, qty_state = self._explicit_hold(row.get("quantity"))
        if qty_state != "ok" or qty is None:
            return None, None
        residual, residual_state = self._explicit_hold(row.get("residual_qty"))
        if residual_state == "invalid":
            return None, None
        if residual_state == "missing" or residual is None or residual == 0:
            return qty, Decimal("0")
        eps = Decimal("0.00000001")
        if leg_orig is not None:
            if abs(leg_orig - qty) <= eps:
                return qty + residual, residual
            if qty >= residual and abs(leg_orig - (qty - residual)) <= eps:
                return qty, residual
            return None, None
        if residual > qty:
            return None, None
        return qty, residual

    def _list_watermark(self, row: dict[str, Any], recon: dict[str, Any]) -> Decimal | None:
        """Proved-sell watermark for this list only. A new list starts at zero.

        The stored number is not itself a fill. An illegal value fails closed
        instead of being read as zero. A different list id does not subtract
        the previous list again.
        """
        number, state = self._explicit_hold(row.get("exit_filled_qty"))
        if state == "invalid":
            return None
        if state == "missing" or number is None:
            number = Decimal("0")
        list_id = str(recon.get("order_list_id") or "").strip()
        stored = str(row.get("oco_order_list_id") or "").strip()
        if list_id and stored and list_id != stored:
            return Decimal("0")
        return number

    def _save_oco_unverified(
        self,
        row: dict[str, Any],
        save: Callable[[dict[str, Any]], None],
        recon: dict[str, Any],
        *,
        working: bool,
    ) -> None:
        keep = row.get("quantity")
        fields: dict[str, Any] = {
            "last_error": "manual_intervention_required:fee_unverified",
            "manual_action": "halt_new_buys_and_manually_close_or_protect_the_fill",
            "unprotected_qty": keep,
        }
        if not working:
            fields["status"] = "oco_unknown"
            fields["protection"] = PROTECTION_PENDING
        save(fields)

    def _verify_oco_executed_legs(
        self,
        row: dict[str, Any],
        recon: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Prove both legs of this read. A stored report from an earlier call is not used."""
        symbol = str(row.get("symbol") or "").strip()

        def _same_leg(recon_id: object, row_id: object) -> str | None:
            left = str(recon_id or "").strip()
            right = str(row_id or "").strip()
            if left and right and left != right:
                return None
            chosen = left or right
            if not chosen.isdigit():
                return None
            return chosen

        above = _same_leg(recon.get("above_order_id"), row.get("oco_above_order_id"))
        below = _same_leg(recon.get("below_order_id"), row.get("oco_below_order_id"))
        if above is None or below is None or above == below:
            return None
        reports = recon.get("order_reports")
        if not isinstance(reports, list):
            reports = []
        sold = Decimal("0")
        filled_ids: list[str] = []
        reason: str | None = None
        leg_orig: Decimal | None = None
        for order_id in (above, below):
            fresh = self._read_order(symbol=symbol, order_id=order_id)
            report = None
            for item in reports:
                if isinstance(item, dict) and str(item.get("orderId") or "").strip() == order_id:
                    report = item
                    break
            payload = fresh if isinstance(fresh, dict) else report
            if not isinstance(payload, dict):
                return None
            if str(payload.get("orderId") or "").strip() not in {"", order_id}:
                return None
            side = str(payload.get("side") or "").strip().upper()
            if side and side != "SELL":
                return None
            executed = _nonneg_decimal(payload.get("executedQty"))
            original = _nonneg_decimal(payload.get("origQty") or payload.get("origQuantity"))
            if executed is None or original is None:
                return None
            if leg_orig is None:
                leg_orig = original
            if executed > 0:
                text = self._verified_order_base(
                    payload,
                    symbol=symbol,
                    side="SELL",
                    allow_open=True,
                    order_id_override=order_id,
                )
                if text is None:
                    return None
                sold += Decimal(text)
                filled_ids.append(order_id)
                leg = str(payload.get("type") or "").upper()
                if leg in {"LIMIT_MAKER", "TAKE_PROFIT_LIMIT", "TAKE_PROFIT"}:
                    reason = "take_profit"
                elif leg in {"STOP_LOSS_LIMIT", "STOP_LOSS"}:
                    reason = "stop_loss"
        return {"sold": sold, "filled_ids": filled_ids, "reason": reason, "leg_orig": leg_orig}

    def _account_oco_execution(
        self,
        row: dict[str, Any],
        recon: dict[str, Any],
        save: Callable[[dict[str, Any]], None],
    ) -> str:
        """Apply one list's proved SELL reduction. Returns closed, closed_dust, open, or unverified.

        ``quantity`` is the full fee-net. ``residual_qty`` is the slice of that
        net the list does not cover, not a second pile of coins. A new list id
        starts its own watermark at zero because the previous list's reduction
        is already in ``quantity``. ``exit_filled_qty`` is only that watermark.
        It is not fill proof. ``1e-8`` absorbs binary dust and does not erase
        a real positive remainder or hide an oversell.
        """
        eps = Decimal("0.00000001")
        state = str(recon.get("state") or "")
        reported = _nonneg_decimal(recon.get("executed_qty"))
        if reported is None and recon.get("executed_qty") not in (None, "", "0", "0.0", "0.00"):
            self._save_oco_unverified(row, save, recon, working=state in {"working", "partial_working"})
            return "unverified"
        if (reported is None or reported == 0) and state != "filled":
            return "open"
        proved = self._verify_oco_executed_legs(row, recon)
        if proved is None:
            self._save_oco_unverified(row, save, recon, working=state in {"working", "partial_working"})
            return "unverified"
        sold = proved["sold"]
        held, residual = self._full_net_and_slice(row, leg_orig=proved.get("leg_orig"))
        if held is None or residual is None:
            self._save_oco_unverified(row, save, recon, working=state in {"working", "partial_working"})
            return "unverified"
        accounted = self._list_watermark(row, recon)
        if accounted is None:
            self._save_oco_unverified(row, save, recon, working=state in {"working", "partial_working"})
            return "unverified"
        if sold + eps < accounted:
            self._save_oco_unverified(row, save, recon, working=state in {"working", "partial_working"})
            return "unverified"
        delta = sold - accounted
        if delta < 0:
            delta = Decimal("0")
        oco_rem = held - delta
        if oco_rem < -eps:
            self._save_oco_unverified(row, save, recon, working=state in {"working", "partial_working"})
            return "unverified"
        if abs(oco_rem) <= eps:
            oco_rem = Decimal("0")
        total_rem = oco_rem
        if total_rem < -eps:
            self._save_oco_unverified(row, save, recon, working=state in {"working", "partial_working"})
            return "unverified"
        list_id = str(recon.get("order_list_id") or row.get("oco_order_list_id") or "")
        if state in {"working", "partial_working"} and sold <= eps:
            return "open"
        covered = held - residual
        if covered < -eps:
            self._save_oco_unverified(row, save, recon, working=False)
            return "unverified"
        oco_done = sold + eps >= covered and state in {"filled", "partial", "unfilled"}
        if oco_done and residual <= eps and oco_rem <= eps:
            save(
                {
                    "status": "oco_filled",
                    "protection": PROTECTION_EXCHANGE,
                    "quantity": _decimal_text(held),
                    "residual_qty": "0",
                    "exit_reason": proved.get("reason") or recon.get("exit_reason"),
                    "exit_filled_qty": _decimal_text(sold),
                    "oco_list_status": recon.get("list_status"),
                    "oco_order_list_id": list_id,
                    "last_error": None,
                }
            )
            return "closed"
        if oco_done and residual > eps:
            if self._remainder_is_dust(row, _decimal_text(residual), row.get("entry_price")):
                save(
                    {
                        "status": "closed_dust",
                        "protection": PROTECTION_DUST,
                        "quantity": _decimal_text(residual),
                        "residual_qty": _decimal_text(residual),
                        "unprotected_qty": _decimal_text(residual),
                        "manual_action": "residual_below_min_is_not_a_complete_exit",
                        "last_error": f"oco_residual_unprotected:{_decimal_text(residual)}",
                        "exit_filled_qty": _decimal_text(sold),
                        "exit_reason": proved.get("reason") or recon.get("exit_reason"),
                        "oco_order_list_id": list_id,
                    }
                )
                return "closed_dust"
            save(
                {
                    "status": "oco_failed",
                    "protection": PROTECTION_PENDING,
                    "quantity": _decimal_text(residual),
                    "residual_qty": "",
                    "unprotected_qty": _decimal_text(residual),
                    "manual_action": "halt_new_buys_and_manually_close_or_protect_the_fill",
                    "last_error": f"oco_residual_unprotected:{_decimal_text(residual)}",
                    "exit_filled_qty": _decimal_text(sold),
                    "oco_order_list_id": list_id,
                }
            )
            return "open"
        if delta > eps or (state in {"partial", "unfilled"} and total_rem > eps):
            save(
                {
                    "quantity": _decimal_text(total_rem if total_rem > eps else residual),
                    "exit_filled_qty": _decimal_text(sold),
                    "oco_order_list_id": list_id,
                    "residual_qty": _decimal_text(residual) if residual > eps else "0",
                }
            )
        return "open"

    def _settle_reduce_sell(
        self,
        row: dict[str, Any],
        save: Callable[[dict[str, Any]], None],
        placed: dict[str, Any],
        client_id: str,
        *,
        reason: str,
        protection: str,
        error_prefix: str,
        order: dict[str, str] | None = None,
        exchange_error: dict[str, Any] | None = None,
        mark: object = None,
        info: dict[str, Any] | None = None,
        proved_held: str | None = None,
    ) -> dict[str, Any]:
        """Close only a sell that filled the whole remaining qty. A zero or partial fill stays open."""
        order_id = _exit_order_accepted(placed)
        if order_id is None:
            book = "time_stop_unknown" if reason == "time_stop_max_hold" else "oco_failed"
            save({"last_error": f"{error_prefix}_unconfirmed", "exit_client_order_id": client_id})
            return self._coverage(
                row,
                book=book,
                submitted=False,
                error=f"{error_prefix}_unconfirmed",
                protection=protection,
                reason=reason,
                order=order,
                exchange_error=exchange_error,
            )
        stored_held = _to_decimal(row.get("quantity")) or Decimal("0")
        proved_dec = _to_decimal(proved_held)
        held = proved_dec if proved_dec is not None else stored_held
        gross = _nonneg_decimal(placed.get("executedQty"))
        symbol = str(row.get("symbol") or "")
        fallback_book = "time_stop_unknown" if reason == "time_stop_max_hold" else "oco_failed"
        cur_status = str(row.get("status") or "")
        unverified_book = fallback_book if cur_status in {"", "open"} else cur_status
        if gross is None:
            exposure = _decimal_text(held) if held > 0 else row.get("quantity")
            upd_unverified: dict[str, Any] = {
                "manual_action": "halt_new_buys_and_manually_close_or_protect_the_fill",
                "unprotected_qty": exposure,
                "last_error": "manual_intervention_required:fee_unverified",
                "exit_order_id": order_id,
                "exit_client_order_id": client_id,
            }
            if cur_status in {"", "open"}:
                upd_unverified["status"] = unverified_book
                upd_unverified["protection"] = (
                    PROTECTION_TIME_STOP if reason == "time_stop_max_hold" else PROTECTION_PENDING
                )
            save(upd_unverified)
            out = self._coverage(
                row,
                book=unverified_book,
                submitted=False,
                error="fee_unverified",
                protection=row.get("protection") or protection,
                reason=reason,
                order=order,
                quantity=str(exposure) if exposure is not None else None,
                exchange_error=exchange_error,
            )
            out["manual_action"] = "halt_new_buys_and_manually_close_or_protect_the_fill"
            out["protection_incident"] = _protection_incident("unprotected")
            out["exchange_order_id"] = order_id
            out["exit_client_order_id"] = client_id
            return out
        proved_text = (
            self._verified_order_base(placed, symbol=symbol, side="SELL", allow_open=True)
            if gross > 0
            else "0"
        )
        if proved_text is None:
            exposure = _decimal_text(held) if held > 0 else _decimal_text(gross)
            upd_unproved: dict[str, Any] = {
                "quantity": exposure,
                "unprotected_qty": exposure,
                "manual_action": "halt_new_buys_and_manually_close_or_protect_the_fill",
                "last_error": "manual_intervention_required:fee_unverified",
                "exit_order_id": order_id,
                "exit_client_order_id": client_id,
            }
            if cur_status in {"", "open"}:
                upd_unproved["status"] = unverified_book
                upd_unproved["protection"] = (
                    PROTECTION_TIME_STOP if reason == "time_stop_max_hold" else PROTECTION_PENDING
                )
            save(upd_unproved)
            out = self._coverage(
                row,
                book=unverified_book,
                submitted=False,
                error="fee_unverified",
                protection=row.get("protection") or protection,
                reason=reason,
                order=order,
                quantity=exposure,
                exchange_error=exchange_error,
            )
            out["manual_action"] = "halt_new_buys_and_manually_close_or_protect_the_fill"
            out["protection_incident"] = _protection_incident("unprotected")
            out["exchange_order_id"] = order_id
            out["exit_client_order_id"] = client_id
            return out
        executed = Decimal(proved_text)
        prior_id = str(row.get("exit_order_id") or "")
        accounted = (
            _to_decimal(row.get("exit_filled_qty")) or Decimal("0")
            if prior_id == str(order_id)
            else Decimal("0")
        )
        # The sell's origQty is the size of that order. Completing it does
        # not close a larger hold. The remainder is held minus executedQty.
        delta = executed - accounted
        if delta < 0:
            delta = Decimal("0")
        remainder = held - delta
        sell_final = str(placed.get("status") or "").upper() in _FINAL_ORDER_STATUS
        # A non-final sell can reduce the hold. It cannot close the book.
        full = sell_final and executed > 0 and remainder <= Decimal("0.00000001")
        lot_residual = _to_decimal(row.get("residual_qty"))
        if full and lot_residual is not None and lot_residual > Decimal("0.00000001"):
            # The sell filled its own quantity. A lot remainder was stored
            # aside and was not part of that order. That is not a flat close.
            residual_text = _decimal_text(lot_residual)
            fields = dust_archive_fields(
                quantity=residual_text,
                mark=mark if mark is not None else row.get("entry_price"),
                stop=row.get("stop_price"),
                detail="oco_lot_residual",
            )
            fields["quantity"] = residual_text
            fields["exit_order_id"] = order_id
            fields["exit_client_order_id"] = client_id
            fields["exit_filled_qty"] = _decimal_text(delta)
            save(fields)
            out = self._coverage(
                row,
                book="closed_dust",
                submitted=False,
                error="dust_untradable",
                protection=PROTECTION_DUST,
                reason="dust_abandon",
                order=order,
                quantity=residual_text,
                exchange_error=exchange_error,
            )
            out["exchange_order_id"] = order_id
            out["exit_client_order_id"] = client_id
            return out
        if full:
            sold = _decimal_text(executed)
            save(
                {
                    "status": "closed",
                    "protection": protection,
                    "quantity": sold,
                    "exit_order_id": order_id,
                    "exit_reason": reason,
                    "exit_client_order_id": client_id,
                    "exit_filled_qty": sold,
                    "oco_list_status": "ALL_DONE",
                    "last_error": None if reason == "time_stop_max_hold" else error_prefix,
                }
            )
            out = self._coverage(
                row,
                book="closed",
                submitted=True,
                ok=reason == "time_stop_max_hold",
                error=None if reason == "time_stop_max_hold" else error_prefix,
                protection=protection,
                reason=reason,
                order=order,
                quantity=sold,
                exchange_error=exchange_error,
            )
            out["exchange_order_id"] = order_id
            out["exit_client_order_id"] = client_id
            return out
        kept = _decimal_text(held if remainder < 0 else remainder)
        sell_status = str(placed.get("status") or "").upper()
        if (
            sell_status == "FILLED"
            and isinstance(info, dict)
            and remainder > Decimal("0.00000001")
            and kept is not None
        ):
            _sized, size_error = size_exit_quantity(
                info, str(row.get("symbol") or ""), kept, mark
            )
            if dust_detail(size_error) or size_error == "exit_qty_below_min":
                fields = dust_archive_fields(
                    quantity=kept,
                    mark=mark,
                    stop=row.get("stop_price"),
                    detail="exit_qty_below_min",
                )
                fields["quantity"] = kept
                fields["exit_order_id"] = order_id
                fields["exit_client_order_id"] = client_id
                fields["exit_filled_qty"] = _decimal_text(executed)
                save(fields)
                dust_error = None if reason == "time_stop_max_hold" else "dust_untradable"
                dust_reason = reason if reason == "time_stop_max_hold" else "dust_abandon"
                out = self._coverage(
                    row,
                    book="closed_dust",
                    submitted=False,
                    ok=False,
                    error=dust_error,
                    protection=PROTECTION_DUST,
                    reason=dust_reason,
                    order=order,
                    quantity=kept,
                    exchange_error=exchange_error,
                )
                out["manual_action"] = "residual_below_min_is_not_a_complete_exit"
                out["protection_incident"] = _protection_incident("dust_residual")
                out["exchange_order_id"] = order_id
                out["exit_client_order_id"] = client_id
                return out
        book = "time_stop_unknown" if reason == "time_stop_max_hold" else "oco_failed"
        open_protection = PROTECTION_TIME_STOP if reason == "time_stop_max_hold" else PROTECTION_PENDING
        if reason == "time_stop_max_hold":
            open_error = "time_stop_sell_open"
        elif sell_status == "FILLED":
            open_error = "flatten_filled_partial"
        else:
            open_error = "flatten_sell_open"
        save(
            {
                "status": book,
                "protection": open_protection,
                "quantity": kept,
                "exit_order_id": order_id,
                "exit_client_order_id": client_id,
                "exit_filled_qty": _decimal_text(executed) if executed > 0 else "0",
                "last_error": open_error,
            }
        )
        return self._coverage(
            row,
            book=book,
            submitted=False,
            error=open_error,
            protection=protection,
            reason=reason,
            order=order,
            quantity=kept,
            exchange_error=exchange_error,
        )

    def _close_time_stop(
        self,
        row: dict[str, Any],
        save: Callable[[dict[str, Any]], None],
        placed: dict[str, Any],
        client_id: str,
        order: dict[str, str] | None,
    ) -> dict[str, Any]:
        return self._settle_reduce_sell(
            row,
            save,
            placed,
            client_id,
            reason="time_stop_max_hold",
            protection=PROTECTION_TIME_STOP,
            error_prefix="time_stop_max_hold",
            order=order,
        )

    def _exit_lookup_state(
        self,
        client: Any,
        symbol: str,
        client_id: str,
        key: str,
        secret: str,
        *,
        entry_order_id: object = None,
    ) -> str:
        """``found``, ``absent``, ``terminal``, ``mismatch``, or ``unknown``.

        Unknown must not place another order. A verified ``found`` or
        ``terminal`` body is cached so a later recorder does not consume
        another ``get_order``.
        """
        self._last_exit_lookup_payload = None
        lookup = getattr(client, "get_order", None)
        if not callable(lookup):
            return "unknown"
        pairs = [
            ("symbol", symbol),
            ("origClientOrderId", client_id),
            ("recvWindow", RECV_WINDOW_MS),
            ("timestamp", str(int(self.clock_ms()))),
        ]
        try:
            placed = lookup(sign_query(pairs, secret), api_key=key)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, AssertionError):
                raise
            if isinstance(exc, BinanceRequestError) and single_order_absent(exc):
                return "absent"
            return "unknown"
        if isinstance(placed, dict):
            identity = _exit_order_identity(
                placed,
                client_id=client_id,
                entry_order_id=entry_order_id,
                symbol=symbol,
            )
            status = _exit_order_status(placed)
            if identity and status in _EXIT_FAIL_STATUS:
                self._last_exit_lookup_payload = placed
                return "terminal"
            if _exit_order_matches_lookup(
                placed,
                client_id=client_id,
                entry_order_id=entry_order_id,
                symbol=symbol,
            ):
                self._last_exit_lookup_payload = placed
                return "found"
            if status in _EXIT_OPEN_STATUS or status in _EXIT_FILLED_STATUS or status in _EXIT_FAIL_STATUS:
                return "mismatch"
        return "unknown"

    def _maybe_ratchet_working_oco(
        self,
        row: dict[str, Any],
        *,
        mark: object,
        save: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any] | None:
        """Ratchet working OCO stop-loss up to breakeven or trailing floor when in profit."""
        gates = self._gates()
        info, info_err = self._load_symbol_info(row, gates)
        if info_err or not isinstance(info, dict):
            return None
        filters = _symbol_filters(info, str(row.get("symbol") or ""))
        if not filters:
            return None
        new_stop, ratchet_reason = compute_ratcheted_stop_price(
            row,
            mark=mark,
            filters=filters,
        )
        if new_stop is None or ratchet_reason is None:
            return None
        canceled, cancel_err = self._cancel_working_oco(row)
        if cancel_err or canceled is None:
            return None
        classified = classify_oco(canceled, held_qty=str(row.get("quantity") or ""))
        if self._account_oco_execution(row, classified, save) == "closed":
            save(
                {
                    "status": "oco_filled",
                    "protection": PROTECTION_EXCHANGE,
                    "exit_reason": classified.get("exit_reason"),
                    "oco_list_status": classified.get("list_status"),
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
                reason=classified.get("exit_reason"),
            )
        if classified.get("state") not in {"unfilled", "partial", "absent"}:
            save({"last_error": "oco_cancel_unconfirmed"})
            return self._coverage(
                row,
                book="oco_unknown",
                submitted=False,
                error="oco_cancel_unconfirmed",
                protection=PROTECTION_PENDING,
            )
        prev_stop = str(row.get("stop_price") or "")
        next_seq = int(row.get("oco_ratchet_count") or 0) + 1
        upd_fields: dict[str, Any] = {
            "stop_price": new_stop,
            "oco_ratchet_count": next_seq,
            "oco_ratchet_reason": ratchet_reason,
        }
        if not row.get("initial_stop_price") and prev_stop:
            upd_fields["initial_stop_price"] = prev_stop
        save(upd_fields)
        outcome = self._try_place_oco(row, mark, save, ratchet_seq=next_seq)
        if isinstance(outcome, dict):
            outcome["oco_ratcheted"] = True
            outcome["oco_ratchet_reason"] = ratchet_reason
            outcome["previous_stop_price"] = prev_stop
            outcome["new_stop_price"] = new_stop
            outcome["oco_ratchet_count"] = next_seq
        return outcome

    def _try_place_oco(
        self,
        row: dict[str, Any],
        mark: object,
        save: Callable[[dict[str, Any]], None],
        *,
        ratchet_seq: int | None = None,
    ) -> dict[str, Any] | None:
        """Place the native list. A failure stays on the book unless auto-flatten is on."""
        gates = self._gates()
        client = self._trade_client()
        if client is None or not callable(getattr(client, "new_oco", None)):
            return self._oco_failed(row, save, "oco_client_unsupported")
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
            return self._oco_failed(row, save, info_error or "oco_missing_lot_filter")
        filters = _symbol_filters(info, str(row.get("symbol") or ""))
        if not filters:
            return self._oco_failed(row, save, "oco_missing_lot_filter")
        if _to_decimal(mark) is None:
            return self._oco_failed(row, save, "mark_unavailable")
        seq = int(row.get("oco_ratchet_count") or 0) if ratchet_seq is None else int(ratchet_seq)
        spec, spec_error = build_spot_oco(
            symbol=str(row.get("symbol") or ""),
            quantity=str(row.get("quantity") or ""),
            stop_price=str(row.get("stop_price") or ""),
            take_profit_price=str(row.get("take_profit_price") or ""),
            decision_id=row.get("decision_log_id"),
            filters=filters,
            mark=mark,
            timestamp_ms=int(self.clock_ms()),
            ratchet_seq=seq,
        )
        if spec_error in {"oco_qty_below_min"} or dust_detail(spec_error):
            kept = str(row.get("quantity") or "")
            fields = dust_archive_fields(
                quantity=kept,
                mark=mark,
                stop=row.get("stop_price"),
                detail=spec_error or "oco_qty_below_min",
            )
            save(fields)
            held = self._coverage(
                row,
                book="closed_dust",
                submitted=False,
                error="dust_untradable",
                protection=PROTECTION_DUST,
                reason="dust_abandon",
                quantity=kept,
            )
            held["manual_action"] = "residual_below_min_is_not_a_complete_exit"
            held["protection_incident"] = _protection_incident("dust_residual")
            return held
        if spec is None or spec_error:
            return self._oco_failed(row, save, spec_error or "oco_rejected")
        key, secret = self._credentials()
        if not key or not secret:
            return self._coverage(
                row,
                book="blocked",
                submitted=False,
                error="missing_api_credentials",
            )
        total_held = str(row.get("quantity") or "")
        save(
            {
                "status": "oco_unknown",
                "protection": PROTECTION_PENDING,
                "oco_list_client_id": spec["list_client_id"],
                "oco_order_list_id": "",
                "oco_above_order_id": "",
                "oco_below_order_id": "",
                "oco_above_client_id": spec["above_client_id"],
                "oco_below_client_id": spec["below_client_id"],
                "quantity": total_held,
                "residual_qty": spec.get("residual_qty"),
                "unprotected_qty": spec.get("residual_qty"),
                "manual_action": "residual_below_min_is_not_a_complete_exit"
                if spec.get("residual_qty")
                else None,
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
            claimed = self._claim_reconciled_oco(
                row,
                found,
                save,
                residual_qty=spec.get("residual_qty"),
                order=dict(spec["pairs"]),
            )
            if claimed is not None:
                return claimed
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
            err_str, exch_err = _exchange_failure(exc, key, secret)
            return self._oco_failed(
                row,
                save,
                f"oco_rejected:{err_str}",
                exchange_error=exch_err,
                quantity=total_held,
                residual_qty="",
                unprotected_qty="",
            )
        verified, verify_error = native_oco_verified(placed)
        if verified is None:
            # A 200 body that does not prove the list can still be the live list.
            # Flatten only after a later requery says it is gone.
            found = self._reconcile_oco(row)
            claimed = self._claim_reconciled_oco(
                row,
                found,
                save,
                residual_qty=spec.get("residual_qty"),
                order=dict(spec["pairs"]),
            )
            if claimed is not None:
                return claimed
            if str(found.get("state") or "") == "absent":
                return self._oco_failed(row, save, verify_error or "native_oco_unverified")
            save(
                {
                    "status": "oco_unknown",
                    "protection": PROTECTION_PENDING,
                    "last_error": verify_error or "native_oco_unverified",
                }
            )
            return self._coverage(
                row,
                book="oco_unknown",
                submitted=False,
                error=verify_error or "native_oco_unverified",
                protection=PROTECTION_PENDING,
            )
        classified = classify_oco(placed, held_qty=str(spec["quantity"]))
        if isinstance(placed, dict):
            classified["order_reports"] = _reports(placed)
        if classified.get("state") not in {"working", "partial_working", "filled"}:
            return self._oco_failed(
                row,
                save,
                "oco_rejected:not_working",
                oco_order_list_id=classified.get("order_list_id"),
                oco_list_status=classified.get("list_status"),
                quantity=total_held,
                residual_qty="",
                unprotected_qty="",
            )
        claimed = self._claim_reconciled_oco(
            row,
            classified,
            save,
            residual_qty=spec.get("residual_qty"),
            order=dict(spec["pairs"]),
        )
        if claimed is not None:
            return claimed
        return self._oco_failed(
            row,
            save,
            "oco_rejected:not_working",
            quantity=total_held,
            residual_qty="",
            unprotected_qty="",
        )

    def _official_oco_fill_fields(
        self, row: dict[str, Any], recon: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Terminal close for a proved official fill. Missing qty is not a fill."""
        executed = _to_decimal(recon.get("executed_qty"))
        reason = recon.get("exit_reason")
        if executed is None or not reason:
            return None
        fields: dict[str, Any] = {
            "status": "oco_filled",
            "protection": PROTECTION_EXCHANGE,
            "exit_reason": reason,
            "exit_order_id": recon.get("filled_order_id"),
            "exit_filled_qty": _decimal_text(executed),
            "oco_list_status": recon.get("list_status"),
            "oco_order_list_id": recon.get("order_list_id"),
            "oco_above_order_id": recon.get("above_order_id"),
            "oco_below_order_id": recon.get("below_order_id"),
            "last_error": None,
        }
        held = _to_decimal(row.get("quantity"))
        if held is not None:
            remainder = held - executed
            if remainder <= Decimal("0.00000001"):
                fields["residual_qty"] = "0"
            else:
                fields["residual_qty"] = _decimal_text(remainder)
        return fields

    def _reconcile_oco(self, row: dict[str, Any]) -> dict[str, Any]:
        gates = self._gates()
        if not gates.get("live_enabled"):
            return {"state": "requery_failed", "error": "binance_spot_live_disabled"}
        client = self._order_list_reader()
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
            if isinstance(exc, BinanceRequestError) and order_list_absent(exc):
                return {"state": "absent", "error": f"oco_list_absent:{exc.code}"}
            if is_request_timeout(exc):
                return {"state": "requery_failed", "error": "oco_requery_transport"}
            return {"state": "requery_failed", "error": _public_error(exc, key, secret)}
        if not isinstance(payload, dict):
            return {"state": "requery_failed", "error": "oco_requery_failed"}
        if not payload.get("orderReports"):
            legs = self._load_oco_legs(payload, symbol, key, secret, client)
            # A missing leg (-2013/-2011) or a leg-query failure does not
            # cancel a list the list endpoint still says is working.
            if legs is None and not oco_is_working(payload):
                return {"state": "requery_failed", "error": "oco_leg_requery_failed"}
            if legs:
                payload = dict(payload)
                payload["orderReports"] = legs
        classified = classify_oco(payload, held_qty=str(row.get("quantity") or ""))
        classified["order_reports"] = _reports(payload)
        classified["raw_payload"] = payload
        classified["error"] = None
        return classified

    def _load_oco_legs(
        self,
        payload: dict[str, Any],
        symbol: str,
        key: str,
        secret: str,
        client: Any | None = None,
    ) -> list[dict[str, Any]] | None:
        if client is None:
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
                # -2013/-2011 name one order. They are not a list deletion.
                # Any other leg error is also not proof the list is gone.
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

    def _oco_working_fields(
        self,
        row: dict[str, Any],
        recon: dict[str, Any],
        residual_qty: object = None,
    ) -> dict[str, Any]:
        """Working list fields. A positive residual is ``oco_partial``, not a clean working book.

        A missing residual is not written as zero. An illegal residual is not
        dropped by ``_to_decimal``. Both stay a partial halt when the hold is
        still a positive net.
        """
        raw = row.get("residual_qty") if residual_qty is None else residual_qty
        residual, state = self._explicit_hold(raw)
        positive = state == "ok" and residual is not None and residual > 0
        illegal = state == "invalid"
        fields: dict[str, Any] = {
            "status": "oco_partial" if positive or illegal else "oco_working",
            "protection": PROTECTION_EXCHANGE,
            "oco_list_status": recon.get("list_status") or "EXECUTING",
            "last_error": None,
        }
        if positive and residual is not None:
            text = _decimal_text(residual)
            fields["residual_qty"] = text
            fields["unprotected_qty"] = text
            fields["manual_action"] = "residual_below_min_is_not_a_complete_exit"
            fields["last_error"] = f"oco_residual_unprotected:{text}"
        elif illegal:
            fields["manual_action"] = "halt_new_buys_and_manually_close_or_protect_the_fill"
            fields["last_error"] = "oco_residual_unprotected:invalid"
        elif state == "ok" and residual is not None:
            fields["residual_qty"] = "0"
        if recon.get("order_list_id") is not None:
            fields["oco_order_list_id"] = recon.get("order_list_id")
        if recon.get("above_order_id") is not None:
            fields["oco_above_order_id"] = recon.get("above_order_id")
        if recon.get("below_order_id") is not None:
            fields["oco_below_order_id"] = recon.get("below_order_id")
        for key in (
            "oco_list_client_id",
            "oco_above_client_id",
            "oco_below_client_id",
        ):
            if row.get(key):
                fields[key] = row.get(key)
        return fields

    def _finalize_placed_oco(
        self,
        row: dict[str, Any],
        recon: dict[str, Any],
        save: Callable[[dict[str, Any]], None],
        *,
        residual_qty: object = None,
        submitted: bool,
        order: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        fields = self._oco_working_fields(row, recon, residual_qty=residual_qty)
        raw_list_payload = (
            recon.get("raw_payload") if isinstance(recon.get("raw_payload"), dict) else None
        )
        if fields["status"] == "oco_working" and raw_list_payload is not None:
            candidate = {**row, **fields}
            ev_fields = self._verified_oco_readback_fields(
                candidate,
                raw_list_payload=raw_list_payload,
            )
            if ev_fields is not None:
                fields.update(ev_fields)
            elif any(row.get(col) not in (None, "") for col in OCO_READBACK_COLUMNS):
                fields.update({col: None for col in OCO_READBACK_COLUMNS})
        elif any(row.get(col) not in (None, "") for col in OCO_READBACK_COLUMNS):
            fields.update({col: None for col in OCO_READBACK_COLUMNS})
        save(fields)
        row.update(fields)
        partial = fields["status"] == "oco_partial"
        out = self._coverage(
            row,
            book="oco_partial" if partial else "oco_working",
            submitted=submitted,
            ok=not partial,
            error="oco_lot_residual" if partial else None,
            protection=PROTECTION_EXCHANGE,
            order=order,
            quantity=str(row.get("quantity") or "") or None,
        )
        if partial:
            out["manual_action"] = fields.get("manual_action")
            out["consumer_allowed"] = False
            out["protection_incident"] = _protection_incident("dust_residual")
        return out

    def _coverage_from_account(
        self,
        row: dict[str, Any],
        recon: dict[str, Any],
        close_state: str,
    ) -> dict[str, Any] | None:
        if close_state == "closed":
            return self._coverage(
                row,
                book="oco_filled",
                submitted=False,
                ok=True,
                protection=PROTECTION_EXCHANGE,
                error=None,
                reason=recon.get("exit_reason"),
            )
        if close_state == "closed_dust":
            out = self._coverage(
                row,
                book="closed_dust",
                submitted=False,
                ok=False,
                error="dust_untradable",
                protection=PROTECTION_DUST,
                reason="dust_halt",
                quantity=str(row.get("quantity") or "") or None,
            )
            out["manual_action"] = row.get("manual_action")
            out["protection_incident"] = _protection_incident("dust_residual")
            out["consumer_allowed"] = False
            return out
        if close_state in {"unverified", "unproved"}:
            out = self._coverage(
                row,
                book=str(row.get("status") or "oco_unknown"),
                submitted=False,
                ok=False,
                error="manual_intervention_required:fee_unverified",
                protection=row.get("protection") or PROTECTION_PENDING,
                quantity=str(row.get("quantity") or "") or None,
            )
            out["manual_action"] = "halt_new_buys_and_manually_close_or_protect_the_fill"
            out["protection_incident"] = _protection_incident("unprotected")
            out["last_error"] = "manual_intervention_required:fee_unverified"
            return out
        return None

    def _claim_reconciled_oco(
        self,
        row: dict[str, Any],
        found: dict[str, Any],
        save: Callable[[dict[str, Any]], None],
        *,
        residual_qty: object = None,
        order: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        state = str(found.get("state") or "")
        if state in {"filled", "partial"}:
            close_state = self._account_oco_execution(row, found, save)
            mapped = self._coverage_from_account(row, found, close_state)
            if mapped is not None:
                return mapped
            return self._coverage(
                row,
                book="oco_failed",
                submitted=False,
                ok=False,
                error="oco_lot_residual",
                protection=PROTECTION_PENDING,
                quantity=str(row.get("quantity") or "") or None,
            )
        if state in {"working", "partial_working"} and native_oco_verified(_recon_payload(found))[0] is not None:
            executed = _nonneg_decimal(found.get("executed_qty"))
            if executed is not None and executed > 0:
                close_state = self._account_oco_execution(row, found, save)
                mapped = self._coverage_from_account(row, found, close_state)
                if mapped is not None:
                    return mapped
            return self._finalize_placed_oco(
                row,
                found,
                save,
                residual_qty=residual_qty,
                submitted=True,
                order=order,
            )
        return None

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
        exchange_error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {
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
        if exchange_error is not None:
            out["exchange_error"] = exchange_error
        return out

    def _lookup_exit_order(
        self,
        client: Any,
        symbol: str,
        client_id: str,
        key: str,
        secret: str,
        *,
        entry_order_id: object = None,
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
        if not isinstance(placed, dict) or not _exit_order_matches_lookup(
            placed,
            client_id=client_id,
            entry_order_id=entry_order_id,
            symbol=symbol,
        ):
            return None
        return placed

    def _lookup_terminal_exit_order(
        self,
        client: Any,
        symbol: str,
        client_id: str,
        key: str,
        secret: str,
        *,
        entry_order_id: object = None,
    ) -> dict[str, Any] | None:
        """One ``get_order`` for a terminal flatten when the state cache is empty."""
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
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, AssertionError):
                raise
            return None
        if not isinstance(placed, dict):
            return None
        if _exit_order_status(placed) not in _EXIT_FAIL_STATUS:
            return None
        if not _exit_order_identity(
            placed,
            client_id=client_id,
            entry_order_id=entry_order_id,
            symbol=symbol,
        ):
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
        ob_metrics = getattr(self, "_orderbook_metrics", None)
        if isinstance(ob_metrics, dict):
            result["orderbook_metrics"] = dict(ob_metrics)
        mark_tel = getattr(self, "_mark_telemetry", None)
        if isinstance(mark_tel, dict):
            result["mark_telemetry"] = dict(mark_tel)
        preflight = getattr(self, "_oco_preflight", None)
        if isinstance(preflight, dict):
            result["oco_preflight"] = dict(preflight)
        logged = result.get("oco_preflight") if isinstance(result.get("oco_preflight"), dict) else {}
        log.info(
            "binance_spot symbol=%s dry_run=%s ok=%s submitted=%s error=%s reference=%s mark=%s mark_source=%s aligned_stop=%s aligned_tp=%s aligned_limit=%s",
            result.get("symbol"),
            result.get("dry_run"),
            result.get("ok"),
            result.get("submitted"),
            result.get("error"),
            logged.get("reference"),
            logged.get("mark"),
            logged.get("mark_source"),
            logged.get("aligned_stop"),
            logged.get("aligned_take_profit"),
            logged.get("aligned_limit"),
        )
        return result


def _public_error(exc: BaseException, key: str, secret: str) -> str:
    if isinstance(exc, BinanceRequestError):
        return _redact(exc.redacted_text((key, secret)), (key, secret))
    text = _redact(type(exc).__name__, (key, secret))
    return f"binance_http_error:{text}"


def _exchange_failure(
    exc: BaseException,
    key: str,
    secret: str,
) -> tuple[str, dict[str, Any] | None]:
    """Public error string plus a receipt-safe code/msg. No second order."""
    error = _public_error(exc, key, secret)
    if not isinstance(exc, BinanceRequestError):
        return error, None
    return error, public_exchange_error(exc.public_view((key, secret)))
