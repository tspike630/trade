"""Signed Binance Cross Margin REST HTTP client (stdlib only, zero SDK dependency).

Endpoints wired against official Binance Margin REST API documentation:
- ``GET  /sapi/v1/margin/account``
- ``GET  /sapi/v1/margin/maxBorrowable``
- ``GET  /sapi/v1/margin/next-hourly-interest-rate``
- ``GET  /sapi/v1/margin/interestHistory``
- ``GET  /sapi/v1/margin/borrow-repay`` (guarantees ``txId`` or ``startTime``)
- ``POST /sapi/v1/margin/borrow-repay`` (``isIsolated=FALSE``, >=110ms spacing)
- ``POST /sapi/v1/margin/order`` (``isIsolated=FALSE``, ``sideEffectType=NO_SIDE_EFFECT``)
- ``GET  /sapi/v1/margin/order``
- ``DELETE /sapi/v1/margin/order``
- ``POST /sapi/v1/margin/order/oco`` (``isIsolated=FALSE``, ``sideEffectType=NO_SIDE_EFFECT``)
- ``GET  /sapi/v1/margin/orderList`` (omits ``symbol`` on Cross Margin per spec)
- ``DELETE /sapi/v1/margin/orderList``
- ``GET  /sapi/v1/margin/openOrders``
- ``GET  /sapi/v1/margin/myTrades``
- ``GET  /sapi/v1/margin/crossMarginData``
- ``GET  /sapi/v1/account/apiRestrictions`` & ``GET /api/v3/account``
- ``GET  /api/v3/exchangeInfo``, ``GET /api/v3/depth``, ``GET /api/v3/ticker/price``

Removed legacy endpoints (``/sapi/v1/margin/loan``, ``/sapi/v1/margin/repay``) and
leverage mutation (``POST /sapi/v1/margin/max-leverage``) are never called.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
import hashlib
import hmac
import http.client
import json
import os
import re
import socket
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from execution.margin_cross.api_semantics import (
    CLIENT_ID_RE,
    CROSS_ISOLATED_FLAG,
    NO_SIDE_EFFECT,
    ORDER_SIDES,
    ORDER_TYPES,
    TIME_IN_FORCE,
)
from execution.margin_cross.errors import (
    LiveNetworkDisabled,
    MarginExchangeError,
    MarginTransportTimeout,
)
from execution.margin_cross.money import api_amount, parse_decimal
from execution.margin_cross.policy import (
    BORROW_REPAY_MIN_SPACING_SEC,
    HARD_CLASSIC_CROSS_LEVERAGE_CEILING,
    MCP_FORBIDDEN_UID,
    ApprovedRiskConfig,
    live_gate_reasons,
)

DEFAULT_BASE_URL = "https://api.binance.com"
DEFAULT_RECV_WINDOW_MS = "5000"
API_KEY_ENV_NAMES = ("BINANCE_MARGIN_API_KEY", "BINANCE_API_KEY")
API_SECRET_ENV_NAMES = ("BINANCE_MARGIN_API_SECRET", "BINANCE_API_SECRET")

MARGIN_ACCOUNT_PATH = "/sapi/v1/margin/account"
MARGIN_MAX_BORROW_PATH = "/sapi/v1/margin/maxBorrowable"
MARGIN_NEXT_HOURLY_RATE_PATH = "/sapi/v1/margin/next-hourly-interest-rate"
MARGIN_INTEREST_HISTORY_PATH = "/sapi/v1/margin/interestHistory"
MARGIN_BORROW_REPAY_PATH = "/sapi/v1/margin/borrow-repay"
MARGIN_ORDER_PATH = "/sapi/v1/margin/order"
MARGIN_OCO_PATH = "/sapi/v1/margin/order/oco"
MARGIN_ORDER_LIST_PATH = "/sapi/v1/margin/orderList"
MARGIN_OPEN_ORDERS_PATH = "/sapi/v1/margin/openOrders"
MARGIN_MY_TRADES_PATH = "/sapi/v1/margin/myTrades"
MARGIN_CROSS_DATA_PATH = "/sapi/v1/margin/crossMarginData"
API_RESTRICTIONS_PATH = "/sapi/v1/account/apiRestrictions"
SPOT_ACCOUNT_PATH = "/api/v3/account"
EXCHANGE_INFO_PATH = "/api/v3/exchangeInfo"
DEPTH_PATH = "/api/v3/depth"
TICKER_PRICE_PATH = "/api/v3/ticker/price"

Opener = Callable[[urllib.request.Request, float], tuple[int, bytes]]

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_SECRET_KV_RE = re.compile(
    r"(?i)\b(?:signature|api[_-]?key|api[_-]?secret|secret|x-mbx-apikey|authorization|token)\b\s*[:=]\s*\S+"
)
_SENSITIVE_PARAM_RE = re.compile(
    r"(?i)(?:[?&])(?:signature|timestamp|recvwindow|apikey|api_key)=[^&\s]*"
)
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9]{32,}\b")
_SAFE_MSG_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 .,;:_%+=()/'\"*-"
)
_DIAGNOSTIC_MAX_LEN = 160
_DEFAULT_BORROW_LOOKBACK_MS = 86_400_000


def resolve_api_credentials(environ: Mapping[str, str] | None = None) -> tuple[str, str]:
    env = os.environ if environ is None else environ
    key = ""
    secret = ""
    for name in API_KEY_ENV_NAMES:
        val = str(env.get(name, "")).strip()
        if val:
            key = val
            break
    for name in API_SECRET_ENV_NAMES:
        val = str(env.get(name, "")).strip()
        if val:
            secret = val
            break
    return key, secret


def sign_margin_query(pairs: Sequence[tuple[str, str]], secret: str) -> str:
    """URL-encode parameter pairs in order and append HMAC-SHA256 ``signature``."""
    if not secret:
        raise ValueError("api_secret_empty")
    query = urllib.parse.urlencode(list(pairs), quote_via=urllib.parse.quote, safe="")
    digest = hmac.new(
        secret.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{query}&signature={digest}"


def scrub_exchange_message(text: object, secrets: tuple[str, ...] = ()) -> str:
    if not isinstance(text, str) or not text:
        return "exchange_error"
    cleaned = text
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        cleaned = cleaned.replace(secret, " ")
    cleaned = _URL_RE.sub(" ", cleaned)
    cleaned = _SENSITIVE_PARAM_RE.sub(" ", cleaned)
    cleaned = _SECRET_KV_RE.sub(" ", cleaned)
    cleaned = _LONG_TOKEN_RE.sub(" ", cleaned)
    cleaned = "".join(ch if ch in _SAFE_MSG_CHARS else " " for ch in cleaned)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > _DIAGNOSTIC_MAX_LEN:
        cleaned = cleaned[:_DIAGNOSTIC_MAX_LEN].rstrip()
    return cleaned or "exchange_error"


def _default_opener(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()


class BinanceCrossMarginHttpClient:
    """Signed Cross Margin REST client compatible with ``CrossMarginTrack``."""

    performs_network_io = True

    def __init__(
        self,
        *,
        api_key: str = "",
        api_secret: str = "",
        account_uid: str = "",
        base_url: str | None = None,
        timeout: float = 10.0,
        recv_window_ms: int = 5000,
        opener: Opener | None = None,
        clock_ms: Callable[[], int] | None = None,
        monotonic_sec: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        environ: Mapping[str, str] | None = None,
        kill_switch_path: str | os.PathLike[str] | None = None,
        approved_config: ApprovedRiskConfig | None = None,
        signer: Any | None = None,
    ) -> None:
        clean_key = api_key.strip()
        clean_secret = api_secret.strip()
        if signer is not None:
            if not clean_key:
                clean_key = str(getattr(signer, "api_key", "") or "").strip()
            if not clean_key:
                raise ValueError("missing_api_credentials")
            if clean_secret:
                raise ValueError("in_process_secret_forbidden_with_external_signer")
        elif not clean_key or not clean_secret:
            raise ValueError("missing_api_credentials")
        if recv_window_ms <= 0 or recv_window_ms > 60_000:
            raise ValueError("recv_window_out_of_range")
        if account_uid.strip() == MCP_FORBIDDEN_UID:
            raise ValueError("mcp_account_forbidden")
        resolved_base = (
            base_url
            or os.environ.get("BINANCE_MARGIN_API_BASE", "").strip()
            or os.environ.get("BINANCE_SPOT_API_BASE", "").strip()
            or DEFAULT_BASE_URL
        )
        self._api_key = clean_key
        self._api_secret = clean_secret
        self._signer = signer
        self.account_uid = account_uid.strip()
        self.margin_mode = "CROSS"
        self.base_url = resolved_base.rstrip("/")
        self.timeout = float(timeout)
        self.recv_window_ms = str(int(recv_window_ms))
        self.bid_qty = Decimal("0")
        self.ask_qty = Decimal("0")
        self._opener = opener or _default_opener
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._monotonic = monotonic_sec or time.monotonic
        self._sleep = sleep_fn or time.sleep
        self._last_borrow_repay_mono: float | None = None
        self._environ = environ
        self._kill_switch_path = kill_switch_path
        self._approved_config = approved_config

    def is_signer_attested(self, *, now_ms: int | None = None) -> bool:
        if self._signer is None:
            return False
        env = os.environ if self._environ is None else self._environ
        cfg_sha = str(env.get("CROSS_MARGIN_APPROVED_CONFIG_SHA256") or "").strip().lower()
        if not cfg_sha or not self.account_uid:
            return False
        ts = int(self._clock_ms()) if now_ms is None else int(now_ms)
        try:
            return bool(
                self._signer.attest_approval(
                    config_sha256=cfg_sha,
                    account_uid=self.account_uid,
                    now_ms=ts,
                )
            )
        except Exception:
            return False

    def _require_live_network_gate(
        self,
        *,
        for_protective_or_repay: bool,
        label: str = "read",
    ) -> None:
        now_val = int(self._clock_ms())
        reasons = live_gate_reasons(
            allow_live_network=True,
            for_protective_or_repay=for_protective_or_repay,
            explicit_kill_path=self._kill_switch_path,
            environ=self._environ,
            approved_config=self._approved_config,
            now_ms=now_val,
            expected_account_uid=self.account_uid,
            require_bound_uid=True,
            signer_attested=self.is_signer_attested(now_ms=now_val),
        )
        if reasons:
            raise LiveNetworkDisabled(f"live_{label}_blocked:{reasons[0]}")

    def _require_live_write_gate(self, *, for_protective_or_repay: bool) -> None:
        self._require_live_network_gate(
            for_protective_or_repay=for_protective_or_repay,
            label="write",
        )

    def _secrets(self) -> tuple[str, ...]:
        if self._api_secret:
            return (self._api_key, self._api_secret)
        return (self._api_key,)

    def _signed_pairs(self, payload: Mapping[str, Any]) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for key, value in payload.items():
            if value is None or key in {"timestamp", "signature", "recvWindow"}:
                continue
            pairs.append((str(key), str(value)))
        pairs.append(("recvWindow", self.recv_window_ms))
        pairs.append(("timestamp", str(int(self._clock_ms()))))
        return pairs

    def _request_json(
        self,
        *,
        method_label: str,
        http_method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        signed: bool = True,
        for_protective_or_repay: bool = True,
    ) -> Any:
        self._require_live_network_gate(
            for_protective_or_repay=for_protective_or_repay,
            label="read" if http_method == "GET" else "write",
        )
        raw_params = dict(params or {})
        headers = {"User-Agent": "trade-margin-cross"}
        data_bytes: bytes | None = None
        if signed:
            headers["X-MBX-APIKEY"] = self._api_key
            pairs_to_sign = self._signed_pairs(raw_params)
            if self._signer is not None:
                try:
                    query = str(
                        self._signer.sign_request(
                            venue="binance_cross_margin",
                            method=http_method,
                            path=path,
                            pairs=pairs_to_sign,
                            account_uid=self.account_uid,
                            for_protective_or_repay=for_protective_or_repay,
                        )
                    )
                except Exception as exc:
                    raise LiveNetworkDisabled(f"signer_rejected:{exc}") from exc
            else:
                query = sign_margin_query(pairs_to_sign, self._api_secret)
            if http_method == "POST":
                url = f"{self.base_url}{path}"
                data_bytes = query.encode("utf-8")
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                url = f"{self.base_url}{path}?{query}"
        else:
            pairs = [(str(k), str(v)) for k, v in raw_params.items() if v is not None]
            query = urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote, safe="")
            url = f"{self.base_url}{path}?{query}" if query else f"{self.base_url}{path}"

        req = urllib.request.Request(url, data=data_bytes, method=http_method, headers=headers)
        try:
            status, body = self._opener(req, self.timeout)
        except (
            TimeoutError,
            socket.timeout,
            ConnectionError,
            http.client.HTTPException,
            urllib.error.URLError,
        ) as exc:
            raise MarginTransportTimeout(method_label) from exc

        if status >= 400:
            err = self._decode_error(status, body)
            err.method = method_label
            raise err
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MarginExchangeError(-status, "malformed_json_response") from exc

    def _decode_error(self, status: int, body: bytes) -> MarginExchangeError:
        code = -int(status)
        message = f"http_{status}"
        if body:
            try:
                decoded = body[:4096].decode("utf-8", errors="ignore").strip()
                parsed = json.loads(decoded)
            except json.JSONDecodeError:
                message = scrub_exchange_message(
                    body[:512].decode("utf-8", errors="ignore"), self._secrets()
                )
            else:
                if isinstance(parsed, dict):
                    raw_code = parsed.get("code")
                    if isinstance(raw_code, int) and not isinstance(raw_code, bool):
                        code = raw_code
                    raw_msg = parsed.get("msg")
                    if isinstance(raw_msg, str):
                        message = scrub_exchange_message(raw_msg, self._secrets())
        return MarginExchangeError(code, message, http_status=int(status))

    def _space_borrow_repay(self) -> None:
        now = float(self._monotonic())
        min_gap = float(BORROW_REPAY_MIN_SPACING_SEC)
        if self._last_borrow_repay_mono is not None:
            elapsed = now - self._last_borrow_repay_mono
            if 0 <= elapsed < min_gap:
                self._sleep(min_gap - elapsed)
                now = float(self._monotonic())
        self._last_borrow_repay_mono = now

    @staticmethod
    def _require_cross_payload(payload: Mapping[str, Any], *, forbid_symbol: bool = False) -> None:
        if not isinstance(payload, Mapping):
            raise MarginExchangeError(-1102, "payload_must_be_mapping")
        if payload.get("isIsolated") != CROSS_ISOLATED_FLAG:
            raise MarginExchangeError(-1102, "isIsolated must be FALSE for cross margin")
        if "isolatedSymbol" in payload:
            raise MarginExchangeError(-1102, "isolatedSymbol forbidden on cross margin")
        if forbid_symbol and "symbol" in payload:
            raise MarginExchangeError(-1102, "symbol is isolated-only")

    @staticmethod
    def _require_no_side_effect(payload: Mapping[str, Any]) -> None:
        if payload.get("sideEffectType") != NO_SIDE_EFFECT:
            raise MarginExchangeError(-1102, "sideEffectType must be NO_SIDE_EFFECT")
        if "autoRepayAtCancel" in payload:
            raise MarginExchangeError(-1102, "autoRepayAtCancel forbidden on explicit track")

    @staticmethod
    def _require_token(raw_val: object, field: str) -> str:
        if not isinstance(raw_val, str) or not raw_val.strip():
            raise MarginExchangeError(-1102, f"{field}_required")
        cleaned = raw_val.strip()
        if not cleaned.isalnum() or len(cleaned) > 30:
            raise MarginExchangeError(-1102, f"{field}_invalid")
        return cleaned

    @staticmethod
    def _require_positive_amount(payload: Mapping[str, Any], field: str) -> Decimal:
        raw_val = payload.get(field)
        if isinstance(raw_val, bool) or raw_val is None or raw_val == "":
            raise MarginExchangeError(-1102, f"{field}_required")
        val, err = parse_decimal(raw_val, field)
        if err or val is None or val <= 0:
            raise MarginExchangeError(-1102, f"positive_{field}_required")
        return val

    @staticmethod
    def _require_client_id_field(payload: Mapping[str, Any], field: str) -> str:
        raw_val = payload.get(field)
        if not isinstance(raw_val, str) or CLIENT_ID_RE.fullmatch(raw_val) is None:
            raise MarginExchangeError(-1102, f"{field}_invalid")
        return raw_val

    def query_account(self) -> dict[str, Any]:
        self._require_live_network_gate(for_protective_or_repay=True, label="read")
        raw = self._request_json(
            method_label="query_account",
            http_method="GET",
            path=MARGIN_ACCOUNT_PATH,
        )
        if not isinstance(raw, dict):
            raise MarginExchangeError(-1000, "account_payload_invalid")
        out = dict(raw)
        level, level_err = parse_decimal(out.get("marginLevel"), "marginLevel")
        total_liab, liab_err = parse_decimal(
            out.get("totalLiabilityOfBtc", "0"), "totalLiabilityOfBtc"
        )
        status_txt = str(out.get("marginLevelStatus") or "EXCESSIVE").upper()
        if (
            not level_err
            and level == Decimal(0)
            and not liab_err
            and total_liab == Decimal(0)
            and status_txt == "EXCESSIVE"
            and _zero_asset_liabilities(out.get("userAssets"))
        ):
            out["rawMarginLevel"] = out.get("marginLevel")
            out["marginLevel"] = "999"
        return out

    def query_max_borrow(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._require_live_network_gate(for_protective_or_repay=True, label="read")
        if not isinstance(payload, Mapping):
            raise MarginExchangeError(-1102, "payload_must_be_mapping")
        if "isolatedSymbol" in payload:
            raise MarginExchangeError(-1102, "isolatedSymbol forbidden on cross margin")
        asset = self._require_token(payload.get("asset"), "asset")
        raw = self._request_json(
            method_label="query_max_borrow",
            http_method="GET",
            path=MARGIN_MAX_BORROW_PATH,
            params={"asset": asset},
        )
        if not isinstance(raw, dict):
            raise MarginExchangeError(-1000, "max_borrow_payload_invalid")
        return raw

    def query_hourly_interest(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        self._require_live_network_gate(for_protective_or_repay=True, label="read")
        self._require_cross_payload(payload)
        assets = str(payload.get("assets") or "").strip()
        if not assets:
            raise MarginExchangeError(-1102, "assets_required")
        raw = self._request_json(
            method_label="query_hourly_interest",
            http_method="GET",
            path=MARGIN_NEXT_HOURLY_RATE_PATH,
            params={"assets": assets, "isIsolated": CROSS_ISOLATED_FLAG},
        )
        if not isinstance(raw, list):
            raise MarginExchangeError(-1000, "hourly_interest_payload_invalid")
        return [dict(item) for item in raw if isinstance(item, Mapping)]

    def query_interest_history(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._require_live_network_gate(for_protective_or_repay=True, label="read")
        if not isinstance(payload, Mapping):
            raise MarginExchangeError(-1102, "payload_must_be_mapping")
        if "isolatedSymbol" in payload:
            raise MarginExchangeError(-1102, "isolatedSymbol forbidden on cross margin")
        params: dict[str, Any] = {}
        if payload.get("asset") not in (None, ""):
            params["asset"] = self._require_token(payload.get("asset"), "asset")
        for key in ("startTime", "endTime", "current", "size"):
            if payload.get(key) not in (None, ""):
                params[key] = str(payload[key]).strip()
        raw = self._request_json(
            method_label="query_interest_history",
            http_method="GET",
            path=MARGIN_INTEREST_HISTORY_PATH,
            params=params,
        )
        if not isinstance(raw, dict):
            raise MarginExchangeError(-1000, "interest_history_payload_invalid")
        return raw

    def query_borrow_repay(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._require_live_network_gate(for_protective_or_repay=True, label="read")
        if not isinstance(payload, Mapping):
            raise MarginExchangeError(-1102, "payload_must_be_mapping")
        if "isolatedSymbol" in payload:
            raise MarginExchangeError(-1102, "isolatedSymbol forbidden on cross margin")
        kind = str(payload.get("type") or "").strip()
        if kind not in {"BORROW", "REPAY"}:
            raise MarginExchangeError(-1102, "borrow_repay_type_invalid")
        params: dict[str, Any] = {"type": kind}
        if payload.get("asset") not in (None, ""):
            params["asset"] = self._require_token(payload.get("asset"), "asset")
        if payload.get("txId") not in (None, ""):
            params["txId"] = str(payload["txId"]).strip()
        elif payload.get("startTime") not in (None, ""):
            params["startTime"] = str(payload["startTime"]).strip()
        else:
            params["startTime"] = str(max(0, int(self._clock_ms()) - _DEFAULT_BORROW_LOOKBACK_MS))
        for opt in ("endTime", "current", "size"):
            if payload.get(opt) not in (None, ""):
                params[opt] = str(payload[opt]).strip()
        raw = self._request_json(
            method_label="query_borrow_repay",
            http_method="GET",
            path=MARGIN_BORROW_REPAY_PATH,
            params=params,
        )
        if not isinstance(raw, dict):
            raise MarginExchangeError(-1000, "borrow_repay_query_invalid")
        return raw

    def query_order(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._require_live_network_gate(for_protective_or_repay=True, label="read")
        self._require_cross_payload(payload)
        symbol = self._require_token(payload.get("symbol"), "symbol")
        params: dict[str, Any] = {
            "symbol": symbol,
            "isIsolated": CROSS_ISOLATED_FLAG,
        }
        if payload.get("orderId") not in (None, ""):
            params["orderId"] = str(payload["orderId"]).strip()
        elif payload.get("origClientOrderId") not in (None, ""):
            params["origClientOrderId"] = self._require_client_id_field(
                payload, "origClientOrderId"
            )
        else:
            raise MarginExchangeError(-1102, "order_identifier_required")
        raw = self._request_json(
            method_label="query_order",
            http_method="GET",
            path=MARGIN_ORDER_PATH,
            params=params,
        )
        if not isinstance(raw, dict):
            raise MarginExchangeError(-1000, "order_payload_invalid")
        return raw

    def query_oco(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._require_live_network_gate(for_protective_or_repay=True, label="read")
        if not isinstance(payload, Mapping):
            raise MarginExchangeError(-1102, "payload_must_be_mapping")
        if "isolatedSymbol" in payload or "symbol" in payload:
            raise MarginExchangeError(-1102, "symbol_forbidden_on_cross_margin_query_oco")
        is_iso = str(payload.get("isIsolated") or CROSS_ISOLATED_FLAG)
        if is_iso != CROSS_ISOLATED_FLAG:
            raise MarginExchangeError(-1102, "isIsolated must be FALSE for cross margin")
        params: dict[str, Any] = {"isIsolated": CROSS_ISOLATED_FLAG}
        if payload.get("orderListId") not in (None, ""):
            params["orderListId"] = str(payload["orderListId"]).strip()
        elif payload.get("origClientOrderId") not in (None, ""):
            params["origClientOrderId"] = self._require_client_id_field(
                payload, "origClientOrderId"
            )
        else:
            raise MarginExchangeError(-1102, "order_list_identifier_required")
        raw = self._request_json(
            method_label="query_oco",
            http_method="GET",
            path=MARGIN_ORDER_LIST_PATH,
            params=params,
        )
        if not isinstance(raw, dict):
            raise MarginExchangeError(-1000, "oco_payload_invalid")
        return raw

    def query_open_orders(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        self._require_live_network_gate(for_protective_or_repay=True, label="read")
        self._require_cross_payload(payload)
        params: dict[str, Any] = {"isIsolated": CROSS_ISOLATED_FLAG}
        if payload.get("symbol") not in (None, ""):
            params["symbol"] = self._require_token(payload.get("symbol"), "symbol")
        raw = self._request_json(
            method_label="query_open_orders",
            http_method="GET",
            path=MARGIN_OPEN_ORDERS_PATH,
            params=params,
        )
        if not isinstance(raw, list):
            raise MarginExchangeError(-1000, "open_orders_payload_invalid")
        return [dict(item) for item in raw if isinstance(item, Mapping)]

    def query_trades(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        self._require_live_network_gate(for_protective_or_repay=True, label="read")
        if not isinstance(payload, Mapping):
            raise MarginExchangeError(-1102, "payload_must_be_mapping")
        if "isolatedSymbol" in payload:
            raise MarginExchangeError(-1102, "isolatedSymbol forbidden on cross margin")
        symbol = self._require_token(payload.get("symbol"), "symbol")
        is_iso = str(payload.get("isIsolated") or CROSS_ISOLATED_FLAG)
        if is_iso != CROSS_ISOLATED_FLAG:
            raise MarginExchangeError(-1102, "isIsolated must be FALSE for cross margin")
        params: dict[str, Any] = {"symbol": symbol, "isIsolated": CROSS_ISOLATED_FLAG}
        for opt in ("orderId", "startTime", "endTime", "fromId", "limit"):
            if payload.get(opt) not in (None, ""):
                params[opt] = str(payload[opt]).strip()
        raw = self._request_json(
            method_label="query_trades",
            http_method="GET",
            path=MARGIN_MY_TRADES_PATH,
            params=params,
        )
        if not isinstance(raw, list):
            raise MarginExchangeError(-1000, "trades_payload_invalid")
        return [dict(item) for item in raw if isinstance(item, Mapping)]

    def borrow(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._require_live_write_gate(for_protective_or_repay=False)
        self._require_cross_payload(payload, forbid_symbol=True)
        if payload.get("type") != "BORROW":
            raise MarginExchangeError(-1102, "type must be BORROW")
        asset = self._require_token(payload.get("asset"), "asset")
        amount = self._require_positive_amount(payload, "amount")
        clean_params = {
            "asset": asset,
            "isIsolated": CROSS_ISOLATED_FLAG,
            "amount": api_amount(amount),
            "type": "BORROW",
        }
        self._space_borrow_repay()
        raw = self._request_json(
            method_label="borrow",
            http_method="POST",
            path=MARGIN_BORROW_REPAY_PATH,
            params=clean_params,
            for_protective_or_repay=False,
        )
        if not isinstance(raw, dict):
            raise MarginExchangeError(-1000, "borrow_response_invalid")
        return raw

    def repay(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._require_live_write_gate(for_protective_or_repay=True)
        self._require_cross_payload(payload, forbid_symbol=True)
        if payload.get("type") != "REPAY":
            raise MarginExchangeError(-1102, "type must be REPAY")
        asset = self._require_token(payload.get("asset"), "asset")
        amount = self._require_positive_amount(payload, "amount")
        clean_params = {
            "asset": asset,
            "isIsolated": CROSS_ISOLATED_FLAG,
            "amount": api_amount(amount),
            "type": "REPAY",
        }
        self._space_borrow_repay()
        raw = self._request_json(
            method_label="repay",
            http_method="POST",
            path=MARGIN_BORROW_REPAY_PATH,
            params=clean_params,
            for_protective_or_repay=True,
        )
        if not isinstance(raw, dict):
            raise MarginExchangeError(-1000, "repay_response_invalid")
        return raw

    def new_order(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        is_flatten_ioc = (
            isinstance(payload, Mapping)
            and str(payload.get("timeInForce") or "") == "IOC"
            and str(payload.get("newClientOrderId") or "").endswith("-flatten")
        )
        self._require_live_write_gate(for_protective_or_repay=is_flatten_ioc)
        self._require_cross_payload(payload)
        self._require_no_side_effect(payload)
        if "quoteOrderQty" in payload:
            raise MarginExchangeError(-1102, "quoteOrderQty_forbidden")
        symbol = self._require_token(payload.get("symbol"), "symbol")
        side = str(payload.get("side") or "")
        if side not in ORDER_SIDES:
            raise MarginExchangeError(-1102, "side_invalid")
        otype = str(payload.get("type") or "")
        if otype not in ORDER_TYPES:
            raise MarginExchangeError(-1102, "order_type_invalid")
        self._require_positive_amount(payload, "quantity")
        self._require_client_id_field(payload, "newClientOrderId")
        if otype in {"LIMIT", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT", "LIMIT_MAKER"}:
            self._require_positive_amount(payload, "price")
        if otype in {"LIMIT", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"}:
            if str(payload.get("timeInForce") or "") not in TIME_IN_FORCE:
                raise MarginExchangeError(-1102, "timeInForce_invalid")
        if otype in {"STOP_LOSS", "STOP_LOSS_LIMIT", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT"}:
            self._require_positive_amount(payload, "stopPrice")
        raw = self._request_json(
            method_label="new_order",
            http_method="POST",
            path=MARGIN_ORDER_PATH,
            params=payload,
            for_protective_or_repay=is_flatten_ioc,
        )
        if not isinstance(raw, dict):
            raise MarginExchangeError(-1000, "new_order_response_invalid")
        return raw

    def cancel_order(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._require_live_write_gate(for_protective_or_repay=True)
        self._require_cross_payload(payload)
        self._require_token(payload.get("symbol"), "symbol")
        has_oid = payload.get("orderId") not in (None, "")
        has_cid = payload.get("origClientOrderId") not in (None, "")
        if not has_oid and not has_cid:
            raise MarginExchangeError(-1102, "order_identifier_required")
        if has_cid:
            self._require_client_id_field(payload, "origClientOrderId")
        raw = self._request_json(
            method_label="cancel_order",
            http_method="DELETE",
            path=MARGIN_ORDER_PATH,
            params=payload,
            for_protective_or_repay=True,
        )
        if not isinstance(raw, dict):
            raise MarginExchangeError(-1000, "cancel_order_response_invalid")
        return raw

    def new_oco(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._require_live_write_gate(for_protective_or_repay=True)
        self._require_cross_payload(payload)
        self._require_no_side_effect(payload)
        self._require_token(payload.get("symbol"), "symbol")
        if str(payload.get("side") or "") not in ORDER_SIDES:
            raise MarginExchangeError(-1102, "side_invalid")
        self._require_positive_amount(payload, "quantity")
        self._require_positive_amount(payload, "price")
        self._require_positive_amount(payload, "stopPrice")
        self._require_positive_amount(payload, "stopLimitPrice")
        if str(payload.get("stopLimitTimeInForce") or "") not in TIME_IN_FORCE:
            raise MarginExchangeError(-1102, "stopLimitTimeInForce_invalid")
        l_cid = self._require_client_id_field(payload, "listClientOrderId")
        lim_cid = self._require_client_id_field(payload, "limitClientOrderId")
        stp_cid = self._require_client_id_field(payload, "stopClientOrderId")
        if len({l_cid, lim_cid, stp_cid}) != 3:
            raise MarginExchangeError(-1102, "oco_client_ids_must_be_distinct")
        raw = self._request_json(
            method_label="new_oco",
            http_method="POST",
            path=MARGIN_OCO_PATH,
            params=payload,
            for_protective_or_repay=True,
        )
        if not isinstance(raw, dict):
            raise MarginExchangeError(-1000, "new_oco_response_invalid")
        return raw

    def cancel_oco(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._require_live_write_gate(for_protective_or_repay=True)
        self._require_cross_payload(payload)
        self._require_token(payload.get("symbol"), "symbol")
        has_lid = payload.get("orderListId") not in (None, "")
        has_lcid = payload.get("listClientOrderId") not in (None, "")
        if not has_lid and not has_lcid:
            raise MarginExchangeError(-1102, "order_list_identifier_required")
        if has_lcid:
            self._require_client_id_field(payload, "listClientOrderId")
        raw = self._request_json(
            method_label="cancel_oco",
            http_method="DELETE",
            path=MARGIN_ORDER_LIST_PATH,
            params=payload,
            for_protective_or_repay=True,
        )
        if not isinstance(raw, dict):
            raise MarginExchangeError(-1000, "cancel_oco_response_invalid")
        return raw

    def query_spot_account(self) -> dict[str, Any]:
        self._require_live_network_gate(for_protective_or_repay=True, label="read")
        raw = self._request_json(
            method_label="query_spot_account",
            http_method="GET",
            path=SPOT_ACCOUNT_PATH,
            params={"omitZeroBalances": "true"},
        )
        if not isinstance(raw, dict):
            raise MarginExchangeError(-1000, "spot_account_invalid")
        return raw

    def query_api_restrictions(self) -> dict[str, Any]:
        self._require_live_network_gate(for_protective_or_repay=True, label="read")
        raw = self._request_json(
            method_label="query_api_restrictions",
            http_method="GET",
            path=API_RESTRICTIONS_PATH,
        )
        if not isinstance(raw, dict):
            raise MarginExchangeError(-1000, "api_restrictions_invalid")
        return raw

    def query_cross_margin_data(self, coin: str) -> list[dict[str, Any]]:
        self._require_live_network_gate(for_protective_or_repay=True, label="read")
        clean_coin = self._require_token(coin, "coin")
        raw = self._request_json(
            method_label="query_cross_margin_data",
            http_method="GET",
            path=MARGIN_CROSS_DATA_PATH,
            params={"coin": clean_coin},
        )
        if not isinstance(raw, list):
            raise MarginExchangeError(-1000, "cross_margin_data_invalid")
        return [dict(item) for item in raw if isinstance(item, Mapping)]

    def query_exchange_info(self, symbol: str) -> dict[str, Any]:
        self._require_live_network_gate(for_protective_or_repay=True, label="read")
        clean_sym = self._require_token(symbol, "symbol")
        raw = self._request_json(
            method_label="query_exchange_info",
            http_method="GET",
            path=EXCHANGE_INFO_PATH,
            params={"symbol": clean_sym},
            signed=False,
        )
        if not isinstance(raw, dict):
            raise MarginExchangeError(-1000, "exchange_info_invalid")
        return raw

    def query_depth(self, symbol: str, *, limit: int = 5) -> dict[str, Any]:
        self._require_live_network_gate(for_protective_or_repay=True, label="read")
        clean_sym = self._require_token(symbol, "symbol")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise MarginExchangeError(-1102, "positive_limit_required")
        raw = self._request_json(
            method_label="query_depth",
            http_method="GET",
            path=DEPTH_PATH,
            params={"symbol": clean_sym, "limit": str(int(limit))},
            signed=False,
        )
        if not isinstance(raw, dict):
            raise MarginExchangeError(-1000, "depth_invalid")
        return raw

    def query_ticker_price(self, symbol: str) -> dict[str, Any]:
        self._require_live_network_gate(for_protective_or_repay=True, label="read")
        clean_sym = self._require_token(symbol, "symbol")
        raw = self._request_json(
            method_label="query_ticker_price",
            http_method="GET",
            path=TICKER_PRICE_PATH,
            params={"symbol": clean_sym},
            signed=False,
        )
        if not isinstance(raw, dict):
            raise MarginExchangeError(-1000, "ticker_price_invalid")
        return raw

def _zero_asset_liabilities(raw_assets: object) -> bool:
    if not isinstance(raw_assets, list):
        return False
    for item in raw_assets:
        if not isinstance(item, Mapping):
            return False
        b_val, b_err = parse_decimal(item.get("borrowed", "0"), "borrowed")
        i_val, i_err = parse_decimal(item.get("interest", "0"), "interest")
        if b_err or i_err or b_val is None or i_val is None:
            return False
        if b_val > 0 or i_val > 0:
            return False
    return True


def _extract_symbol_filters(info: Mapping[str, Any], symbol: str) -> tuple[dict[str, str] | None, str]:
    symbols = info.get("symbols")
    row: Mapping[str, Any] | None = None
    if isinstance(symbols, list):
        for item in symbols:
            if isinstance(item, Mapping) and str(item.get("symbol") or "") == symbol:
                row = item
                break
    elif str(info.get("symbol") or "") == symbol:
        row = info
    if row is None:
        return None, "symbol_not_in_exchange_info"
    if str(row.get("status") or "").upper() != "TRADING":
        return None, "symbol_not_trading"
    if row.get("isMarginTradingAllowed") is not True:
        return None, "symbol_margin_trading_disallowed"
    filters = row.get("filters")
    if not isinstance(filters, list):
        return None, "symbol_filters_missing"
    tick_size = ""
    step_size = ""
    min_qty = ""
    min_notional = ""
    for f_row in filters:
        if not isinstance(f_row, Mapping):
            continue
        f_type = str(f_row.get("filterType") or "")
        if f_type == "PRICE_FILTER":
            tick_size = str(f_row.get("tickSize") or "")
        elif f_type == "LOT_SIZE":
            step_size = str(f_row.get("stepSize") or "")
            min_qty = str(f_row.get("minQty") or "")
        elif f_type in {"NOTIONAL", "MIN_NOTIONAL"} and not min_notional:
            min_notional = str(f_row.get("minNotional") or "")
    if not (tick_size and step_size and min_qty and min_notional):
        return None, "symbol_filters_incomplete"
    t_val, t_err = parse_decimal(tick_size, "tick_size")
    s_val, s_err = parse_decimal(step_size, "step_size")
    q_val, q_err = parse_decimal(min_qty, "min_qty")
    n_val, n_err = parse_decimal(min_notional, "min_notional")
    if t_err or s_err or q_err or n_err or not all((t_val, s_val, q_val, n_val)):
        return None, "symbol_filters_unparseable"
    return {
        "tick_size": api_amount(t_val),
        "step_size": api_amount(s_val),
        "min_qty": api_amount(q_val),
        "min_notional": api_amount(n_val),
    }, ""


def fetch_live_cross_margin_snapshot(
    client: BinanceCrossMarginHttpClient,
    *,
    symbol: str,
    base_asset: str,
    quote_asset: str,
    borrow_asset: str,
    expected_uid: str | None = None,
    fee_rate: str = "0.001",
    fee_asset: str = "USDT",
    observed_at_ms: int | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Read signed margin account + restrictions + market filters into a canonical Snapshot dict."""
    try:
        restrictions = client.query_api_restrictions()
        spot_account = client.query_spot_account()
        margin_account = client.query_account()
        max_borrow = client.query_max_borrow({"asset": borrow_asset})
        hourly_rates = client.query_hourly_interest(
            {"assets": borrow_asset, "isIsolated": CROSS_ISOLATED_FLAG}
        )
        margin_coin_rows = client.query_cross_margin_data(borrow_asset)
        info = client.query_exchange_info(symbol)
        depth = client.query_depth(symbol, limit=5)
        ticker = client.query_ticker_price(symbol)
    except MarginTransportTimeout as exc:
        return None, f"live_snapshot_timeout:{exc.method}"
    except MarginExchangeError as exc:
        # One snapshot read. A terminal borrow code is not retried and does not borrow.
        call = str(getattr(exc, "method", "") or "")
        reason = f"live_snapshot_exchange_error:{exc.code}"
        if call:
            reason = f"{reason}:{call}"
        return None, reason

    if restrictions.get("enableReading") is not True:
        return None, "api_reading_disabled"
    if restrictions.get("enableSpotAndMarginTrading") is not True:
        return None, "api_spot_and_margin_trading_disabled"
    if restrictions.get("ipRestrict") is not True:
        return None, "api_ip_restrict_disabled"

    raw_uid = spot_account.get("uid")
    fallback_uid = (
        expected_uid.strip()
        if isinstance(expected_uid, str) and expected_uid.strip()
        else os.environ.get("CROSS_MARGIN_ACCOUNT_UID", "").strip()
    )
    resolved_uid = str(raw_uid).strip() if raw_uid not in (None, "") else fallback_uid
    if not resolved_uid:
        return None, "account_uid_unavailable"
    if resolved_uid == MCP_FORBIDDEN_UID:
        return None, "mcp_account_forbidden"
    if fallback_uid and resolved_uid != fallback_uid:
        return None, "account_uid_mismatch"
    if client.account_uid and resolved_uid != client.account_uid:
        return None, "approved_account_uid_mismatch"
    if (
        client._approved_config is not None
        and resolved_uid != client._approved_config.approved_account_uid
    ):
        return None, "approved_account_uid_mismatch"
    client.account_uid = resolved_uid

    if margin_coin_rows:
        coin_row = next(
            (r for r in margin_coin_rows if str(r.get("coin") or "") == borrow_asset),
            None,
        )
        if coin_row is not None and coin_row.get("borrowable") is False:
            return None, "asset_not_borrowable"

    filters, filter_err = _extract_symbol_filters(info, symbol)
    if filter_err or filters is None:
        return None, filter_err or "symbol_filters_missing"

    bids = depth.get("bids")
    asks = depth.get("asks")
    if (
        not isinstance(bids, list)
        or not isinstance(asks, list)
        or not bids
        or not asks
        or not isinstance(bids[0], (list, tuple))
        or not isinstance(asks[0], (list, tuple))
        or len(bids[0]) < 2
        or len(asks[0]) < 2
    ):
        return None, "orderbook_empty"
    best_bid, bid_px_err = parse_decimal(str(bids[0][0]), "best_bid")
    bid_qty, bid_qty_err = parse_decimal(str(bids[0][1]), "bid_qty")
    best_ask, ask_px_err = parse_decimal(str(asks[0][0]), "best_ask")
    ask_qty, ask_qty_err = parse_decimal(str(asks[0][1]), "ask_qty")
    if (
        bid_px_err
        or bid_qty_err
        or ask_px_err
        or ask_qty_err
        or best_bid is None
        or bid_qty is None
        or best_ask is None
        or ask_qty is None
        or best_ask <= best_bid
    ):
        return None, "orderbook_invalid_or_crossed"
    client.bid_qty = bid_qty
    client.ask_qty = ask_qty

    ref_px, ref_err = parse_decimal(str(ticker.get("price") or ""), "reference_price")
    if ref_err or ref_px is None or ref_px <= 0:
        return None, "reference_price_invalid"

    raw_assets = margin_account.get("userAssets")
    if not isinstance(raw_assets, list):
        return None, "margin_account_assets_missing"
    wanted_assets = {base_asset, quote_asset, borrow_asset}
    assets_list: list[dict[str, str]] = []
    seen_assets: set[str] = set()
    for item in raw_assets:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("asset") or "").strip()
        if name not in wanted_assets:
            continue
        seen_assets.add(name)
        assets_list.append(
            {
                "asset": name,
                "free": str(item.get("free") or "0"),
                "locked": str(item.get("locked") or "0"),
                "borrowed": str(item.get("borrowed") or "0"),
                "interest": str(item.get("interest") or "0"),
            }
        )
    for missing_name in sorted(wanted_assets - seen_assets):
        assets_list.append(
            {
                "asset": missing_name,
                "free": "0",
                "locked": "0",
                "borrowed": "0",
                "interest": "0",
            }
        )

    if not hourly_rates:
        return None, "hourly_interest_rate_missing"
    rate_str = str(hourly_rates[0].get("nextHourlyInterestRate") or "")
    account_type = str(margin_account.get("accountType") or "")
    account_max_lev = "3" if account_type == "MARGIN_1" else str(HARD_CLASSIC_CROSS_LEVERAGE_CEILING + Decimal(2))
    clock_now = int(time.time() * 1000) if observed_at_ms is None else int(observed_at_ms)

    snapshot_raw: dict[str, Any] = {
        "uid": resolved_uid,
        "margin_mode": "CROSS",
        "account_type": account_type,
        "created": bool(margin_account.get("created")),
        "trade_enabled": bool(margin_account.get("tradeEnabled")),
        "borrow_enabled": bool(margin_account.get("borrowEnabled")),
        "margin_level": str(margin_account.get("marginLevel") or "0"),
        "margin_level_status": str(margin_account.get("marginLevelStatus") or "EXCESSIVE"),
        "account_max_leverage": account_max_lev,
        "observed_at_ms": clock_now,
        "assets": assets_list,
        "max_borrowable": {borrow_asset: str(max_borrow.get("amount") or "0")},
        "borrow_limit": {borrow_asset: str(max_borrow.get("borrowLimit") or "0")},
        "hourly_interest_rate": {borrow_asset: rate_str},
        "bid_qty": api_amount(bid_qty),
        "ask_qty": api_amount(ask_qty),
        "reference_price": api_amount(ref_px),
        "filters": filters,
        "fee_rate": fee_rate,
        "fee_asset": fee_asset,
    }
    return snapshot_raw, ""
