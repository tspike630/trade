#!/usr/bin/env python3
"""Complete Public Derivatives, Spot L2/Whale Microstructure, Cross-Exchange, Options Gamma & Token Unlocks Collector (Categories 6, 7, 8, 9).

Zero external dependencies (pure Python stdlib ``urllib.request``, ``sqlite3``,
``json``, ``csv``, ``math``, ``ssl``). Implements all four public/on-chain
categories from the master quantitative checklist:

- **Category 6 (Binance USD-M Perpetual Counterparty, Whale/Retail L/S & Liquidations)**:
  ``binance_perp_counterparty`` table — 60d ``funding_z``, ``perp_basis_bps``,
  ``oi_change_1h_pct``, ``top_position_ls_ratio``, ``global_account_ls_ratio``,
  ``whale_retail_divergence``, ``perp_taker_buy_sell_ratio``, and forced-liquidation
  cascade estimation (``est_liq_pressure_usd``, ``liq_cascade_regime``).

- **Category 7 (Binance Spot Multi-Horizon 5m/1h/4h/1d K-lines, L2 Depth & Whale aggTrades)**:
  ``binance_spot_microstructure`` table — ``ret_5m_pct``, ``ret_1h_pct``, ``ret_4h_pct``,
  ``ret_1d_pct``, ``taker_buy_ratio_5m``, ``taker_buy_ratio_1h``, ``spread_bps``,
  ``bid_depth_20_usd``, ``ask_depth_20_usd``, ``orderbook_imbalance_20`` (first 20
  payload levels, unchanged), ``bid_depth_5_usd``, ``ask_depth_5_usd``, ``obi_top5``
  (best 5 levels, price×qty notional), ``impact_10k_bps``, ``whale_net_buy_usd``,
  and ``aggtrade_whale_buy_ratio``. ``obi_top5`` is stored only with the exchange
  depth-update event time that covers that book. A refresh clock is never written
  into ``source_ts_ms``.

- **Category 8 (Cross-Exchange Spot Baseline OKX/Bybit/Binance + Deribit Options DVOL & Gamma Walls)**:
  ``cross_exchange_spot_baseline`` table (Binance vs OKX vs Bybit spot price & volume
  cross-validation, ``max_venue_dislocation_bps``) + ``deribit_options_gamma`` table
  (BTC/ETH ``DVOL``, ``dvol_zscore``, ``max_pain_strike``, ``top_call_wall_strike``,
  ``top_put_wall_strike``, ``put_call_oi_ratio``, ``gamma_regime``).

- **Category 9 (Token Cliff Unlocks Schedule I-4, Stablecoin Supply & On-Chain Holder/Bot Summary I-1/I-2/I-3)**:
  ``token_unlock_schedule`` table (VC/Team cliff unlock windows ``[-72h, +48h]`` for
  ``SUI``, ``APT``, ``ARB``, ``OP``, ``TIA``, ``STRK``, ``WLD``, ``ENA``, ``ONDO``, ``JTO``, ``PYTH``),
  ``stablecoin_supply`` table (DefiLlama ``USDT``/``USDC`` 24h & 7d mint/burn), and
  ``onchain_holder_bot_summary`` table (bridging ``top10_share``, ``concentration_delta``,
  ``dev_balance_delta``, ``bot_trade_share`` from ``dexscreener.db`` when present).
"""

from __future__ import annotations

import argparse
import base64
import contextvars
import csv
import fcntl
import io
import json
import math
import os
import signal
import socket
import sqlite3
import ssl
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = REPO_ROOT / "data" / "counterparty" / "public_derivatives.db"

DEFAULT_PERP_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "SUIUSDT",
    "APTUSDT",
    "NEARUSDT",
    "FETUSDT",
    "RENDERUSDT",
    "TAOUSDT",
    "WLDUSDT",
    # USD-M does not list PEPEUSDT. The perpetual is the 1000-unit contract.
    "1000PEPEUSDT",
    "WIFUSDT",
    "AAVEUSDT",
    "UNIUSDT",
    "ENAUSDT",
    "ONDOUSDT",
    "PENDLEUSDT",
    "ARBUSDT",
    "OPUSDT",
    "TIAUSDT",
)

DEFAULT_SPOT_MICRO_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "DOGEUSDT",
    "SUIUSDT",
    "APTUSDT",
    "FETUSDT",
    "WLDUSDT",
    "PEPEUSDT",
    "ENAUSDT",
    "ONDOUSDT",
    "ARBUSDT",
    "OPUSDT",
    "TIAUSDT",
)

# Institutional Cliff Unlock Calendar (Category 9 / Factor Candidate I-4)
# Tracks recurring monthly/quarterly VC, core contributor & ecosystem cliff unlocks
INSTITUTIONAL_CLIFF_UNLOCK_CALENDAR: tuple[dict[str, Any], ...] = (
    {"symbol": "SUIUSDT", "day_of_month": 1, "pct_circulating": 2.40, "est_usd": 65_000_000.0, "recipient": "Series_A_B_VC_and_Contributors"},
    {"symbol": "ENAUSDT", "day_of_month": 2, "pct_circulating": 3.10, "est_usd": 42_000_000.0, "recipient": "Core_Contributors_and_Investors"},
    {"symbol": "APTUSDT", "day_of_month": 11, "pct_circulating": 2.15, "est_usd": 95_000_000.0, "recipient": "Core_Contributors_and_Investors"},
    {"symbol": "STRKUSDT", "day_of_month": 15, "pct_circulating": 3.30, "est_usd": 38_000_000.0, "recipient": "Early_Contributors_and_Investors"},
    {"symbol": "ARBUSDT", "day_of_month": 16, "pct_circulating": 2.35, "est_usd": 55_000_000.0, "recipient": "Team_and_Advisors_Cliff"},
    {"symbol": "ZKUSDT", "day_of_month": 17, "pct_circulating": 2.80, "est_usd": 28_000_000.0, "recipient": "Investors_and_Team"},
    {"symbol": "ONDOUSDT", "day_of_month": 18, "pct_circulating": 4.50, "est_usd": 120_000_000.0, "recipient": "Ecosystem_and_Private_Sale"},
    {"symbol": "ZROUSDT", "day_of_month": 20, "pct_circulating": 3.60, "est_usd": 45_000_000.0, "recipient": "Strategic_Partners_and_Team"},
    {"symbol": "WLDUSDT", "day_of_month": 24, "pct_circulating": 2.90, "est_usd": 36_000_000.0, "recipient": "TFH_Investors_and_Team"},
    {"symbol": "TIAUSDT", "day_of_month": 30, "pct_circulating": 2.50, "est_usd": 48_000_000.0, "recipient": "Seed_Series_A_B_Investors"},
    {"symbol": "OPUSDT", "day_of_month": 31, "pct_circulating": 2.45, "est_usd": 52_000_000.0, "recipient": "Core_Contributors_and_Sugar_X"},
)

# Strategy rows still say PEPEUSDT. The venue row keeps the listed symbol and
# the 1000x price scale so mark price is not rewritten into a spot price.
VENUE_STRATEGY_SYMBOL = {"1000PEPEUSDT": "PEPEUSDT"}
VENUE_PRICE_SCALE = {"1000PEPEUSDT": 1000.0}

DEFAULT_LOOP_INTERVAL_SEC = 300
PARTIAL_EXIT = 2
FATAL_EXIT = 1
LOCK_BUSY_EXIT = 3
HTTP_MAX_ATTEMPTS = 3
HTTP_BACKOFF_BASE_SEC = 0.4
HTTP_BACKOFF_CAP_SEC = 8.0
DEFAULT_HTTP_TIMEOUT_SEC = 8.0
DEFAULT_SPOT_WS_BASE = "wss://data-stream.binance.vision/ws"
_TRANSIENT_HTTP = frozenset({429, 418, 500, 502, 503, 504})

_ROUND_ERRORS: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "public_feed_round_errors",
    default=None,
)
_LAST_HTTP: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "public_feed_last_http",
    default=None,
)
_HTTP_TIMEOUT: contextvars.ContextVar[float] = contextvars.ContextVar(
    "public_feed_http_timeout",
    default=DEFAULT_HTTP_TIMEOUT_SEC,
)
_CLOCK: Callable[[], float] = time.time
_SLEEP: Callable[[float], None] = time.sleep


def _finite(val: Any) -> float | None:
    if isinstance(val, bool) or val is None:
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _build_ssl_context() -> ssl.SSLContext:
    for ca_path in ("/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem"):
        if Path(ca_path).is_file():
            try:
                return ssl.create_default_context(cafile=ca_path)
            except ssl.SSLError:
                pass
    return ssl._create_unverified_context()  # noqa: SLF001


_SSL_CTX = _build_ssl_context()


class CollectorLockBusy(RuntimeError):
    """Another collector process already holds the database lock."""


def _clock_ms() -> int:
    return int(_CLOCK() * 1000)


def public_request_headers(accept: str) -> dict[str, str]:
    """Public market-data headers. API keys and signatures are never added."""
    return {"Accept": accept, "User-Agent": "trade-counterparty-collector/2.0"}


def record_fetch_error(
    *,
    category: str,
    symbol: str | None,
    endpoint: str,
    message: str,
    http_status: int | None = None,
    error_code: str | None = None,
    fails_round: bool = True,
) -> dict[str, Any]:
    row = {
        "collected_at_ms": _clock_ms(),
        "category": category,
        "symbol": symbol,
        "endpoint": endpoint,
        "http_status": http_status,
        "error_code": error_code,
        "message": message,
        "fails_round": fails_round,
    }
    bucket = _ROUND_ERRORS.get()
    if bucket is not None:
        bucket.append(row)
    print(
        "public_feed_error "
        f"category={category} symbol={symbol or '-'} endpoint={endpoint} "
        f"status={http_status if http_status is not None else '-'} "
        f"code={error_code or '-'} {message}",
        file=sys.stderr,
    )
    return row


def _error_from_body(body: bytes | None) -> tuple[str | None, str | None]:
    if not body:
        return None, None
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(payload, dict) or not payload.get("msg"):
        return None, None
    code = payload.get("code")
    return (str(code) if code is not None else None), str(payload.get("msg"))


def _binance_payload_error(payload: Any) -> tuple[str, str] | None:
    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    msg = payload.get("msg")
    if isinstance(code, int) and code < 0 and msg:
        return str(code), str(msg)
    return None


def _urllib_exchange(
    url: str,
    params: dict[str, Any] | None,
    timeout: float,
    accept: str,
) -> tuple[int | None, bytes | None, str | None, str | None, bool]:
    """Return ``(status, body, error, error_code, retryable)`` for one public GET."""
    full_url = f"{url}?{urlencode(params)}" if params else url
    req = Request(full_url, headers=public_request_headers(accept), method="GET")
    try:
        with _URL_OPEN(req, timeout) as resp:  # noqa: S310
            status = int(getattr(resp, "status", 200) or 200)
            return status, resp.read(), None, None, False
    except HTTPError as exc:
        body = exc.read() if getattr(exc, "fp", None) is not None else b""
        code, msg = _error_from_body(body)
        status = int(exc.code)
        return status, body, msg or f"HTTP {status}", code or str(status), status in _TRANSIENT_HTTP
    except TimeoutError as exc:
        return None, None, f"timeout:{exc}", "timeout", True
    except URLError as exc:
        return None, None, f"network:{exc.reason}", "network", True
    except OSError as exc:
        return None, None, f"{type(exc).__name__}:{exc}", type(exc).__name__, True


def _url_open(req: Request, timeout: float):
    return urlopen(req, timeout=timeout, context=_SSL_CTX)  # noqa: S310


_URL_OPEN: Callable[..., Any] = _url_open
_HTTP_EXCHANGE: Callable[..., tuple] = _urllib_exchange


def _http_get_bytes(
    url: str,
    params: dict[str, Any] | None,
    timeout: float,
    accept: str,
) -> tuple[int | None, bytes | None, str | None, str | None]:
    delay = HTTP_BACKOFF_BASE_SEC
    status: int | None = None
    body: bytes | None = None
    err = "not_attempted"
    code: str | None = None
    for attempt in range(HTTP_MAX_ATTEMPTS):
        status, body, err, code, retryable = _HTTP_EXCHANGE(url, params, timeout, accept)
        if err is None:
            return status, body, None, None
        if not retryable or attempt + 1 >= HTTP_MAX_ATTEMPTS:
            return status, body, err, code
        _SLEEP(delay)
        delay = min(HTTP_BACKOFF_CAP_SEC, delay * 2)
    return status, body, err, code


def _remember_http(url: str, status: int | None, error: str | None, error_code: str | None) -> None:
    _LAST_HTTP.set(
        {"url": url, "status": status, "error": error, "error_code": error_code}
    )


def _http_get_json(url: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
    limit = float(_HTTP_TIMEOUT.get() if timeout is None else timeout)
    status, body, err, code = _http_get_bytes(url, params, limit, "application/json")
    payload: Any = None
    if err is None and body is not None:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            err = f"decode:{exc}"
            code = "decode"
    api_err = _binance_payload_error(payload)
    if api_err is not None:
        code, err = api_err
        payload = None
    _remember_http(url, status, err, code)
    return payload


def _http_get_text(url: str, timeout: float = 10.0) -> str | None:
    status, body, err, code = _http_get_bytes(url, None, timeout, "text/csv,*/*")
    text = body.decode("utf-8", errors="replace") if err is None and body is not None else None
    _remember_http(url, status, err, code)
    return text


def _fail_from_last(category: str, symbol: str | None, endpoint: str, fallback: str) -> None:
    last = _LAST_HTTP.get() or {}
    record_fetch_error(
        category=category,
        symbol=symbol,
        endpoint=str(last.get("url") or endpoint),
        message=str(last.get("error") or fallback),
        http_status=last.get("status") if isinstance(last.get("status"), int) else None,
        error_code=str(last.get("error_code")) if last.get("error_code") is not None else None,
    )


def compute_zscore(series: Sequence[float], min_samples: int = 10) -> float | None:
    clean = [float(x) for x in series if _finite(x) is not None]
    if len(clean) < min_samples:
        return None
    mean_v = sum(clean) / len(clean)
    var = sum((x - mean_v) ** 2 for x in clean) / max(1, len(clean) - 1)
    std = math.sqrt(max(0.0, var))
    if std <= 1e-12:
        return 0.0
    return round((clean[-1] - mean_v) / std, 4)


def compute_whale_retail_divergence(
    top_position_ls_ratio: float | None,
    global_account_ls_ratio: float | None,
    *,
    funding_rate: float | None = None,
) -> tuple[float | None, str]:
    """Compute Whale-to-Retail L/S ratio divergence and classify counterparty regime."""
    top_r = _finite(top_position_ls_ratio)
    glob_r = _finite(global_account_ls_ratio)
    fr = _finite(funding_rate)
    if top_r is None or glob_r is None or glob_r <= 0:
        return None, "unknown"
    div = round(top_r / glob_r, 4)
    if glob_r >= 2.20 and (top_r <= 0.95 or div <= 0.45):
        return div, "retail_long_trap_cascade"
    if div >= 1.35 and (fr is None or fr <= 0.0001):
        return div, "whale_accumulation_favorable"
    return div, "neutral"


def estimate_liquidation_cascade(
    oi_change_1h_pct: float | None,
    open_interest_usd: float | None,
    perp_taker_buy_sell_ratio: float | None,
) -> tuple[float | None, str]:
    """Estimate forced liquidation deleveraging notional USD and cascade regime (Category 6)."""
    oi_chg = _finite(oi_change_1h_pct)
    oi_usd = _finite(open_interest_usd)
    taker_r = _finite(perp_taker_buy_sell_ratio)
    if oi_chg is None or oi_usd is None or oi_usd <= 0:
        return None, "normal"
    if oi_chg >= 0:
        return 0.0, "normal"
    # Net OI contraction in USD over the past hour
    oi_drop_usd = abs(oi_chg / 100.0) * oi_usd
    if oi_chg <= -3.0 and (taker_r is not None and taker_r <= 0.75):
        return round(oi_drop_usd, 2), "long_liquidation_waterfall"
    if oi_chg <= -3.0 and (taker_r is not None and taker_r >= 1.35):
        return round(oi_drop_usd, 2), "short_squeeze_liquidation"
    return round(oi_drop_usd, 2), "normal"


def _positive_book_levels(raw: Any) -> list[tuple[float, float]]:
    if not isinstance(raw, list):
        return []
    out: list[tuple[float, float]] = []
    for level in raw:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        price = _finite(level[0])
        qty = _finite(level[1])
        if price is None or qty is None or price <= 0 or qty <= 0:
            continue
        out.append((price, qty))
    return out


def compute_top_n_notional_imbalance(
    bids_raw: Any,
    asks_raw: Any,
    *,
    levels: int = 5,
) -> dict[str, float | str | None]:
    """Best ``levels`` bid/ask price×qty imbalance.

    Levels are sorted (bids high to low, asks low to high) before the cut.
    Fewer than ``levels`` strictly positive levels is ``obi_top5_levels_missing``.
    A non-positive denominator is ``obi_top5_denominator_zero``. Neither case
    returns a numeric imbalance.
    """
    need = int(levels)
    bids = sorted(_positive_book_levels(bids_raw), key=lambda row: row[0], reverse=True)[:need]
    asks = sorted(_positive_book_levels(asks_raw), key=lambda row: row[0])[:need]
    bid_notional = sum(price * qty for price, qty in bids)
    ask_notional = sum(price * qty for price, qty in asks)
    imbalance, error = notional_imbalance_result(
        len(bids),
        len(asks),
        bid_notional,
        ask_notional,
        levels=need,
    )
    return {
        "bid_notional": None if error and error.endswith("levels_missing") else round(bid_notional, 2),
        "ask_notional": None if error and error.endswith("levels_missing") else round(ask_notional, 2),
        "imbalance": imbalance,
        "error": error,
    }


def notional_imbalance_result(
    bid_levels: int,
    ask_levels: int,
    bid_notional: float,
    ask_notional: float,
    *,
    levels: int = 5,
) -> tuple[float | None, str | None]:
    """Return ``(imbalance, error)``. Missing levels and a non-positive denominator stay errors."""
    need = int(levels)
    prefix = "obi_top5" if need == 5 else f"obi_top{need}"
    if bid_levels < need or ask_levels < need:
        return None, f"{prefix}_levels_missing"
    denominator = bid_notional + ask_notional
    if denominator <= 0:
        return None, f"{prefix}_denominator_zero"
    return round((bid_notional - ask_notional) / denominator, 4), None


def classify_depth_event(last_update_id: int, event: Any) -> tuple[str, int | None]:
    """Classify one Binance diff-depth event against a REST snapshot id.

    ``match`` is the first event with ``U <= lastUpdateId+1 <= u``. Its ``E``
    is the exchange event time. A later event cannot cover a gap in front of it.
    """
    if not isinstance(event, dict):
        return "ignore", None
    try:
        final_id = int(event["u"])
        first_id = int(event["U"])
        event_ms = int(event["E"])
    except (KeyError, TypeError, ValueError):
        return "ignore", None
    if event_ms <= 0:
        return "ignore", None
    if final_id < int(last_update_id):
        return "older", None
    if first_id <= int(last_update_id) + 1 <= final_id:
        return "match", event_ms
    return "gap", None


def resolve_depth_source_ts(
    last_update_id: int,
    events: Iterable[Any],
) -> tuple[int | None, str | None]:
    """Return ``(event_time_ms, error)`` for the first decisive depth event."""
    saw_event = False
    for event in events:
        kind, event_ms = classify_depth_event(last_update_id, event)
        if kind == "ignore":
            continue
        saw_event = True
        if kind == "older":
            continue
        if kind == "match":
            return event_ms, None
        return None, "obi_top5_source_ts_gap"
    if not saw_event:
        return None, "obi_top5_source_ts_missing"
    return None, "obi_top5_source_ts_missing"


def spot_depth_ws_url(symbol: str, ws_base: str = DEFAULT_SPOT_WS_BASE) -> str:
    parsed = urlparse(ws_base)
    path = parsed.path.rstrip("/")
    stream = f"{path}/{str(symbol).strip().lower()}@depth@100ms"
    return parsed._replace(path=stream).geturl()


def _ws_send(sock: socket.socket, opcode: int, payload: bytes) -> None:
    mask = os.urandom(4)
    length = len(payload)
    header = bytearray([0x80 | (opcode & 0x0F)])
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    sock.sendall(bytes(header) + mask + masked)


def _read_ws_frame(sock: socket.socket, pending: bytes) -> tuple[int, bytes, bytes] | None:
    data = pending
    while len(data) < 2:
        chunk = sock.recv(4096)
        if not chunk:
            return None
        data += chunk
    first, second = data[0], data[1]
    if second & 0x80:
        raise OSError("server frame was masked")
    length = second & 0x7F
    offset = 2
    if length == 126:
        while len(data) < 4:
            data += sock.recv(4096)
        length = struct.unpack("!H", data[2:4])[0]
        offset = 4
    elif length == 127:
        while len(data) < 10:
            data += sock.recv(4096)
        length = struct.unpack("!Q", data[2:10])[0]
        offset = 10
    while len(data) < offset + length:
        chunk = sock.recv(65536)
        if not chunk:
            return None
        data += chunk
    return first, data[offset:offset + length], data[offset + length:]


def iter_spot_depth_events(
    symbol: str,
    *,
    timeout: float,
    ws_base: str = DEFAULT_SPOT_WS_BASE,
    max_events: int = 40,
) -> Iterator[dict[str, Any]]:
    """Yield public diff-depth events. No API key is sent."""
    url = urlparse(spot_depth_ws_url(symbol, ws_base))
    if url.scheme != "wss" or not url.hostname:
        raise OSError(f"unsupported depth stream url: {url.geturl()}")
    deadline = time.monotonic() + max(0.2, float(timeout))
    raw = socket.create_connection((url.hostname, url.port or 443), timeout=max(0.2, float(timeout)))
    try:
        ssock = _SSL_CTX.wrap_socket(raw, server_hostname=url.hostname)
        ssock.settimeout(max(0.2, deadline - time.monotonic()))
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = url.path or "/"
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {url.hostname}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"User-Agent: {public_request_headers('application/json')['User-Agent']}\r\n"
            "\r\n"
        )
        ssock.sendall(request.encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            if time.monotonic() >= deadline:
                return
            chunk = ssock.recv(4096)
            if not chunk:
                return
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status_line:
            raise OSError(status_line.decode("ascii", "replace"))
        produced = 0
        while produced < max_events and time.monotonic() < deadline:
            ssock.settimeout(max(0.2, deadline - time.monotonic()))
            frame = _read_ws_frame(ssock, rest)
            rest = b""
            if frame is None:
                return
            header, payload, rest = frame
            opcode = header & 0x0F
            if (header & 0x80) == 0:
                raise OSError("fragmented depth frame")
            if opcode == 0x9:
                _ws_send(ssock, 0xA, payload)
                continue
            if opcode == 0x8:
                return
            if opcode != 0x1:
                continue
            event = json.loads(payload.decode("utf-8"))
            if isinstance(event, dict):
                produced += 1
                yield event
    finally:
        try:
            raw.close()
        except OSError:
            pass


def _default_depth_events(symbol: str, timeout: float) -> Iterator[dict[str, Any]]:
    yield from iter_spot_depth_events(symbol, timeout=timeout)


_DEPTH_EVENT_ITER: Callable[[str, float], Iterable[dict[str, Any]]] = _default_depth_events


def capture_depth_source_ts(
    symbol: str,
    last_update_id: int,
    *,
    timeout: float = 3.0,
) -> tuple[int | None, str | None]:
    try:
        events = _DEPTH_EVENT_ITER(symbol, timeout)
    except Exception as exc:  # noqa: BLE001 — recorded by the caller as a missing source time
        return None, f"obi_top5_source_ts_unavailable:{type(exc).__name__}"
    try:
        for event in events:
            kind, event_ms = classify_depth_event(last_update_id, event)
            if kind == "ignore" or kind == "older":
                continue
            if kind == "match":
                return event_ms, None
            return None, "obi_top5_source_ts_gap"
    except Exception as exc:  # noqa: BLE001
        return None, f"obi_top5_source_ts_unavailable:{type(exc).__name__}"
    return None, "obi_top5_source_ts_missing"


def compute_spot_orderbook_and_aggtrades_metrics(
    depth_payload: dict[str, Any] | None,
    agg_trades: Sequence[dict[str, Any]] | None,
    *,
    whale_threshold_usd: float = 25_000.0,
) -> dict[str, float | None]:
    """Compute L2 20-level depth, OBI, $10k price impact, and Whale aggTrades absorption (Category 7)."""
    bids_raw = depth_payload.get("bids", []) if isinstance(depth_payload, dict) else []
    asks_raw = depth_payload.get("asks", []) if isinstance(depth_payload, dict) else []
    bids = [
        (_finite(b[0]), _finite(b[1]))
        for b in bids_raw[:20]
        if isinstance(b, (list, tuple)) and len(b) >= 2
    ]
    asks = [
        (_finite(a[0]), _finite(a[1]))
        for a in asks_raw[:20]
        if isinstance(a, (list, tuple)) and len(a) >= 2
    ]
    bids_clean = [(p, q) for p, q in bids if p is not None and q is not None and p > 0 and q > 0]
    asks_clean = [(p, q) for p, q in asks if p is not None and q is not None and p > 0 and q > 0]

    spread_bps = None
    bid_depth_usd = None
    ask_depth_usd = None
    obi_20 = None
    impact_10k_bps = None

    if bids_clean and asks_clean:
        best_bid = bids_clean[0][0]
        best_ask = asks_clean[0][0]
        mid = 0.5 * (best_bid + best_ask)
        if mid > 0 and best_ask >= best_bid:
            spread_bps = round(((best_ask - best_bid) / mid) * 10_000.0, 4)
        bid_depth_usd = round(sum(p * q for p, q in bids_clean), 2)
        ask_depth_usd = round(sum(p * q for p, q in asks_clean), 2)
        tot_depth = bid_depth_usd + ask_depth_usd
        if tot_depth > 0:
            obi_20 = round((bid_depth_usd - ask_depth_usd) / tot_depth, 4)

        # Simulate $10,000 market BUY walking the ask book
        target_usd = 10_000.0
        rem_usd = target_usd
        cost_usd = 0.0
        qty_accum = 0.0
        for p, q in asks_clean:
            lvl_usd = p * q
            take_usd = min(rem_usd, lvl_usd)
            cost_usd += take_usd
            qty_accum += take_usd / p
            rem_usd -= take_usd
            if rem_usd <= 1e-6:
                break
        if qty_accum > 0 and mid > 0:
            vwap_fill = cost_usd / qty_accum
            impact_10k_bps = round(((vwap_fill - mid) / mid) * 10_000.0, 4)

    whale_buy_usd = 0.0
    whale_sell_usd = 0.0
    for tr in agg_trades or ():
        if not isinstance(tr, dict):
            continue
        px = _finite(tr.get("p"))
        qty = _finite(tr.get("q"))
        is_buyer_maker = tr.get("m")
        if px is None or qty is None or px <= 0 or qty <= 0:
            continue
        notional = px * qty
        if notional < whale_threshold_usd:
            continue
        # In Binance aggTrades, m=True means buyer is maker (so taker was a SELLER)
        if is_buyer_maker is False:
            whale_buy_usd += notional
        elif is_buyer_maker is True:
            whale_sell_usd += notional

    whale_tot = whale_buy_usd + whale_sell_usd
    whale_net = round(whale_buy_usd - whale_sell_usd, 2)
    whale_ratio = round(whale_buy_usd / whale_tot, 4) if whale_tot > 0 else None
    top5 = compute_top_n_notional_imbalance(bids_raw, asks_raw, levels=5)

    return {
        "spread_bps": spread_bps,
        "bid_depth_20_usd": bid_depth_usd,
        "ask_depth_20_usd": ask_depth_usd,
        "orderbook_imbalance_20": obi_20,
        "bid_depth_5_usd": top5["bid_notional"],
        "ask_depth_5_usd": top5["ask_notional"],
        "obi_top5": top5["imbalance"],
        "obi_top5_error": top5["error"],
        "impact_10k_bps": impact_10k_bps,
        "whale_net_buy_usd": whale_net,
        "aggtrade_whale_buy_ratio": whale_ratio,
    }


def compute_cross_exchange_dislocation(
    binance_px: float | None,
    okx_px: float | None,
    bybit_px: float | None,
) -> tuple[float | None, str]:
    """Compute cross-exchange spot price dislocation in bps across Binance, OKX, and Bybit (Category 8)."""
    prices = [p for p in (_finite(binance_px), _finite(okx_px), _finite(bybit_px)) if p is not None and p > 0]
    if len(prices) < 2:
        return None, "single_venue_only"
    med = sorted(prices)[len(prices) // 2]
    max_diff_bps = round((max(abs(p - med) for p in prices) / med) * 10_000.0, 4)
    if max_diff_bps >= 35.0:
        return max_diff_bps, "cross_exchange_wick_divergence"
    return max_diff_bps, "aligned"


def evaluate_token_unlock_window(
    symbol: str,
    *,
    now_ms: int | None = None,
    schedule: Sequence[dict[str, Any]] = INSTITUTIONAL_CLIFF_UNLOCK_CALENDAR,
) -> dict[str, Any] | None:
    """Check whether ``symbol`` is inside the ``[-72h, +48h]`` VC/Team Cliff Unlock blackout window (Category 9 / I-4)."""
    sym = str(symbol or "").strip().upper()
    ts_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    dt_now = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)

    for item in schedule:
        if str(item.get("symbol") or "").upper() != sym:
            continue
        dom = int(item.get("day_of_month") or 1)
        year = dt_now.year
        month = dt_now.month
        # Clamp day to valid day of current month
        for candidate_month in (month, month + 1 if month < 12 else 1):
            cand_year = year if candidate_month >= month else year + 1
            day = min(dom, 28 if candidate_month == 2 else (30 if candidate_month in (4, 6, 9, 11) else 31))
            unlock_dt = datetime(cand_year, candidate_month, day, 0, 0, 0, tzinfo=timezone.utc)
            unlock_ms = int(unlock_dt.timestamp() * 1000)
            hours_to_unlock = (unlock_ms - ts_ms) / 3_600_000.0
            # Active blackout if within [-48h after unlock, +72h before unlock] and >= 2.0% of circulating
            pct_circ = float(item.get("pct_circulating") or 0.0)
            in_blackout = (-48.0 <= hours_to_unlock <= 72.0) and (pct_circ >= 2.0)
            if hours_to_unlock >= -48.0:
                return {
                    "symbol": sym,
                    "next_unlock_utc": unlock_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "hours_to_unlock": round(hours_to_unlock, 2),
                    "unlock_pct_circulating": pct_circ,
                    "unlock_usd_est": float(item.get("est_usd") or 0.0),
                    "recipient_type": str(item.get("recipient") or ""),
                    "in_unlock_blackout": 1 if in_blackout else 0,
                }
    return None


def parse_deribit_instrument_name(name: str) -> tuple[str, float, str] | None:
    """Parse Deribit option name like ``BTC-27DEC26-100000-C`` -> ``(expiry, strike, 'C'|'P')``."""
    parts = str(name or "").strip().split("-")
    if len(parts) != 4:
        return None
    _, expiry, strike_raw, opt_type = parts
    strike = _finite(strike_raw)
    if strike is None or strike <= 0 or opt_type not in ("C", "P"):
        return None
    return expiry, strike, opt_type


def compute_deribit_option_walls_and_max_pain(
    book_summaries: Sequence[dict[str, Any]],
    *,
    spot_price: float | None,
    dvol_series: Sequence[float] = (),
) -> dict[str, Any]:
    """Compute Put/Call OI ratio, Call/Put Gamma Walls, Max Pain strike, and Gamma regime."""
    call_oi_by_strike: dict[float, float] = {}
    put_oi_by_strike: dict[float, float] = {}
    total_call_oi = 0.0
    total_put_oi = 0.0

    for item in book_summaries:
        if not isinstance(item, dict):
            continue
        parsed = parse_deribit_instrument_name(str(item.get("instrument_name") or ""))
        if parsed is None:
            continue
        _, strike, opt_type = parsed
        oi = _finite(item.get("open_interest")) or 0.0
        if oi <= 0:
            continue
        if opt_type == "C":
            call_oi_by_strike[strike] = call_oi_by_strike.get(strike, 0.0) + oi
            total_call_oi += oi
        else:
            put_oi_by_strike[strike] = put_oi_by_strike.get(strike, 0.0) + oi
            total_put_oi += oi

    all_strikes = sorted(set(call_oi_by_strike.keys()) | set(put_oi_by_strike.keys()))
    top_call_wall = (
        max(call_oi_by_strike.items(), key=lambda kv: kv[1])[0]
        if call_oi_by_strike
        else None
    )
    top_put_wall = (
        max(put_oi_by_strike.items(), key=lambda kv: kv[1])[0]
        if put_oi_by_strike
        else None
    )
    pc_ratio = (
        round(total_put_oi / total_call_oi, 4)
        if total_call_oi > 0
        else None
    )

    max_pain_strike: float | None = None
    if all_strikes:
        best_cost = float("inf")
        for settle_px in all_strikes:
            call_payout = sum(
                max(0.0, settle_px - k) * oi for k, oi in call_oi_by_strike.items()
            )
            put_payout = sum(
                max(0.0, k - settle_px) * oi for k, oi in put_oi_by_strike.items()
            )
            total_payout = call_payout + put_payout
            if total_payout < best_cost:
                best_cost = total_payout
                max_pain_strike = settle_px

    dvol_clean = [float(x) for x in dvol_series if _finite(x) is not None]
    dvol_close = dvol_clean[-1] if dvol_clean else None
    dvol_chg_24h = (
        round(((dvol_clean[-1] - dvol_clean[-24]) / dvol_clean[-24]) * 100.0, 4)
        if len(dvol_clean) >= 25 and dvol_clean[-24] > 0
        else None
    )
    dvol_z = compute_zscore(dvol_clean, min_samples=10)

    sp = _finite(spot_price)
    gamma_regime = "neutral"
    if sp is not None and top_put_wall is not None and sp < top_put_wall:
        gamma_regime = "negative_gamma_slippery"
    elif dvol_z is not None and dvol_z >= 2.0 and (pc_ratio is not None and pc_ratio >= 0.85):
        gamma_regime = "negative_gamma_slippery"
    elif (
        sp is not None
        and top_put_wall is not None
        and top_call_wall is not None
        and top_put_wall <= sp <= top_call_wall
    ):
        gamma_regime = "positive_gamma_pinned"

    return {
        "spot_price": sp,
        "dvol_close": dvol_close,
        "dvol_change_24h_pct": dvol_chg_24h,
        "dvol_zscore": dvol_z,
        "total_call_oi": round(total_call_oi, 2),
        "total_put_oi": round(total_put_oi, 2),
        "put_call_oi_ratio": pc_ratio,
        "max_pain_strike": max_pain_strike,
        "top_call_wall_strike": top_call_wall,
        "top_put_wall_strike": top_put_wall,
        "gamma_regime": gamma_regime,
    }


def compute_fed_net_liquidity(
    walcl_millions: float | None,
    tga_millions: float | None,
    on_rrp_billions: float | None,
) -> float | None:
    """Compute US Federal Reserve Net Liquidity in USD Billions: ``WALCL/1000 - TGA/1000 - ON_RRP``."""
    w = _finite(walcl_millions)
    t = _finite(tga_millions)
    r = _finite(on_rrp_billions)
    if w is None or t is None or r is None:
        return None
    return round((w / 1000.0) - (t / 1000.0) - r, 4)


def ensure_counterparty_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS binance_perp_counterparty (
            symbol TEXT NOT NULL,
            ts_ms INTEGER NOT NULL,
            funding_rate REAL,
            funding_z REAL,
            mark_price REAL,
            index_price REAL,
            perp_basis_bps REAL,
            open_interest REAL,
            open_interest_usd REAL,
            oi_change_1h_pct REAL,
            top_position_ls_ratio REAL,
            global_account_ls_ratio REAL,
            whale_retail_divergence REAL,
            perp_taker_buy_sell_ratio REAL,
            est_liq_pressure_usd REAL,
            liq_cascade_regime TEXT,
            counterparty_regime TEXT,
            PRIMARY KEY (symbol, ts_ms)
        );
        CREATE INDEX IF NOT EXISTS idx_perp_cp_sym_ts ON binance_perp_counterparty(symbol, ts_ms DESC);

        CREATE TABLE IF NOT EXISTS binance_spot_microstructure (
            symbol TEXT NOT NULL,
            ts_ms INTEGER NOT NULL,
            ret_5m_pct REAL,
            ret_1h_pct REAL,
            ret_4h_pct REAL,
            ret_1d_pct REAL,
            taker_buy_ratio_5m REAL,
            taker_buy_ratio_1h REAL,
            spread_bps REAL,
            bid_depth_20_usd REAL,
            ask_depth_20_usd REAL,
            orderbook_imbalance_20 REAL,
            impact_10k_bps REAL,
            whale_net_buy_usd REAL,
            aggtrade_whale_buy_ratio REAL,
            PRIMARY KEY (symbol, ts_ms)
        );

        CREATE TABLE IF NOT EXISTS cross_exchange_spot_baseline (
            symbol TEXT NOT NULL,
            ts_ms INTEGER NOT NULL,
            binance_price REAL,
            okx_price REAL,
            bybit_price REAL,
            max_venue_dislocation_bps REAL,
            dislocation_regime TEXT,
            PRIMARY KEY (symbol, ts_ms)
        );

        CREATE TABLE IF NOT EXISTS deribit_options_gamma (
            currency TEXT NOT NULL,
            ts_ms INTEGER NOT NULL,
            spot_price REAL,
            dvol_close REAL,
            dvol_change_24h_pct REAL,
            dvol_zscore REAL,
            total_call_oi REAL,
            total_put_oi REAL,
            put_call_oi_ratio REAL,
            max_pain_strike REAL,
            top_call_wall_strike REAL,
            top_put_wall_strike REAL,
            gamma_regime TEXT,
            PRIMARY KEY (currency, ts_ms)
        );

        CREATE TABLE IF NOT EXISTS token_unlock_schedule (
            symbol TEXT PRIMARY KEY,
            next_unlock_utc TEXT,
            hours_to_unlock REAL,
            unlock_pct_circulating REAL,
            unlock_usd_est REAL,
            recipient_type TEXT,
            in_unlock_blackout INTEGER
        );

        CREATE TABLE IF NOT EXISTS macro_net_liquidity (
            asof_date TEXT PRIMARY KEY,
            walcl_millions REAL,
            tga_millions REAL,
            on_rrp_billions REAL,
            net_liquidity_billions REAL,
            hy_oas_bps REAL
        );

        CREATE TABLE IF NOT EXISTS stablecoin_supply (
            ts_ms INTEGER PRIMARY KEY,
            usdt_supply_usd REAL,
            usdc_supply_usd REAL,
            total_major_stable_usd REAL,
            change_24h_pct REAL,
            change_7d_pct REAL
        );

        CREATE TABLE IF NOT EXISTS onchain_holder_bot_summary (
            token_address TEXT PRIMARY KEY,
            ts_ms INTEGER,
            top10_share REAL,
            concentration_delta REAL,
            dev_balance_delta REAL,
            bot_trade_share REAL
        );

        CREATE TABLE IF NOT EXISTS collector_fetch_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collected_at_ms INTEGER NOT NULL,
            category TEXT NOT NULL,
            symbol TEXT,
            endpoint TEXT,
            http_status INTEGER,
            error_code TEXT,
            message TEXT NOT NULL,
            fails_round INTEGER NOT NULL
        );
        """
    )
    _ensure_added_columns(
        conn,
        "binance_perp_counterparty",
        (
            ("est_liq_pressure_usd", "REAL"),
            ("liq_cascade_regime", "TEXT"),
            ("collected_at_ms", "INTEGER"),
            ("source_ts_ms", "INTEGER"),
            ("venue_symbol", "TEXT"),
            ("strategy_symbol", "TEXT"),
            ("price_scale", "REAL"),
            ("oi_source_ts_ms", "INTEGER"),
        ),
    )
    _ensure_added_columns(
        conn,
        "binance_spot_microstructure",
        (
            ("bid_depth_5_usd", "REAL"),
            ("ask_depth_5_usd", "REAL"),
            ("obi_top5", "REAL"),
            ("obi_top5_error", "TEXT"),
            ("obi_top5_source_ts_ms", "INTEGER"),
            ("book_update_id", "INTEGER"),
            ("source_ts_ms", "INTEGER"),
            ("collected_at_ms", "INTEGER"),
            ("kline_5m_close_ts_ms", "INTEGER"),
        ),
    )
    _ensure_added_columns(
        conn,
        "cross_exchange_spot_baseline",
        (("source_ts_ms", "INTEGER"), ("collected_at_ms", "INTEGER")),
    )
    _ensure_added_columns(
        conn,
        "deribit_options_gamma",
        (("source_ts_ms", "INTEGER"), ("collected_at_ms", "INTEGER")),
    )
    _ensure_added_columns(
        conn,
        "stablecoin_supply",
        (("source_ts_ms", "INTEGER"), ("collected_at_ms", "INTEGER")),
    )


def _ensure_added_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: Sequence[tuple[str, str]],
) -> None:
    existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, col_type in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")


def fetch_binance_perp_symbol_snapshot(symbol: str, *, now_ms: int | None = None) -> dict[str, Any] | None:
    """Snapshot one USD-M symbol. ``now_ms`` is ignored as a row timestamp.

    The row time is ``premiumIndex.time``. A missing premium or a missing
    exchange time does not insert a row stamped with the refresh clock.
    """
    del now_ms  # refresh time must not become the stored source time
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    endpoint = "https://fapi.binance.com/fapi/v1/premiumIndex"
    prem = _http_get_json(endpoint, {"symbol": sym})
    if not isinstance(prem, dict):
        _fail_from_last("perp", sym, endpoint, "premium_index_missing")
        return None
    source_ts = _finite(prem.get("time"))
    if source_ts is None:
        record_fetch_error(
            category="perp",
            symbol=sym,
            endpoint=endpoint,
            message="perp_source_ts_missing",
            error_code="perp_source_ts_missing",
        )
        return None
    mark_px = _finite(prem.get("markPrice"))
    index_px = _finite(prem.get("indexPrice"))
    funding_rate = _finite(prem.get("lastFundingRate"))
    basis_bps = (
        round(((mark_px - index_px) / index_px) * 10_000.0, 4)
        if mark_px is not None and index_px is not None and index_px > 0
        else None
    )

    f_hist = _http_get_json(
        "https://fapi.binance.com/fapi/v1/fundingRate",
        {"symbol": sym, "limit": 180},
    )
    f_series = [
        _finite(r.get("fundingRate"))
        for r in (f_hist if isinstance(f_hist, list) else [])
        if isinstance(r, dict) and _finite(r.get("fundingRate")) is not None
    ]
    funding_z = compute_zscore([float(x) for x in f_series if x is not None], min_samples=20)

    oi_hist = _http_get_json(
        "https://fapi.binance.com/futures/data/openInterestHist",
        {"symbol": sym, "period": "5m", "limit": 13},
    )
    oi_val = None
    oi_usd = None
    oi_chg_1h = None
    oi_source_ts = None
    if isinstance(oi_hist, list) and oi_hist:
        last_oi = oi_hist[-1] if isinstance(oi_hist[-1], dict) else {}
        first_oi = oi_hist[0] if isinstance(oi_hist[0], dict) else {}
        oi_val = _finite(last_oi.get("sumOpenInterest"))
        oi_usd = _finite(last_oi.get("sumOpenInterestValue"))
        oi_ts = _finite(last_oi.get("timestamp"))
        oi_source_ts = int(oi_ts) if oi_ts is not None else None
        prev_val = _finite(first_oi.get("sumOpenInterest"))
        if oi_val is not None and prev_val is not None and prev_val > 0 and len(oi_hist) >= 2:
            oi_chg_1h = round(((oi_val - prev_val) / prev_val) * 100.0, 4)

    top_ls = _http_get_json(
        "https://fapi.binance.com/futures/data/topLongShortPositionRatio",
        {"symbol": sym, "period": "5m", "limit": 1},
    )
    top_ratio = (
        _finite(top_ls[-1].get("longShortRatio"))
        if isinstance(top_ls, list) and top_ls and isinstance(top_ls[-1], dict)
        else None
    )

    glob_ls = _http_get_json(
        "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
        {"symbol": sym, "period": "5m", "limit": 1},
    )
    glob_ratio = (
        _finite(glob_ls[-1].get("longShortRatio"))
        if isinstance(glob_ls, list) and glob_ls and isinstance(glob_ls[-1], dict)
        else None
    )

    taker_ls = _http_get_json(
        "https://fapi.binance.com/futures/data/takerlongshortRatio",
        {"symbol": sym, "period": "5m", "limit": 1},
    )
    taker_ratio = (
        _finite(taker_ls[-1].get("buySellRatio"))
        if isinstance(taker_ls, list) and taker_ls and isinstance(taker_ls[-1], dict)
        else None
    )

    div, regime = compute_whale_retail_divergence(
        top_ratio, glob_ratio, funding_rate=funding_rate
    )
    liq_usd, liq_regime = estimate_liquidation_cascade(oi_chg_1h, oi_usd, taker_ratio)
    return {
        "symbol": sym,
        "ts_ms": int(source_ts),
        "source_ts_ms": int(source_ts),
        "collected_at_ms": _clock_ms(),
        "venue_symbol": sym,
        "strategy_symbol": VENUE_STRATEGY_SYMBOL.get(sym, sym),
        "price_scale": VENUE_PRICE_SCALE.get(sym, 1.0),
        "oi_source_ts_ms": oi_source_ts,
        "funding_rate": funding_rate,
        "funding_z": funding_z,
        "mark_price": mark_px,
        "index_price": index_px,
        "perp_basis_bps": basis_bps,
        "open_interest": oi_val,
        "open_interest_usd": oi_usd,
        "oi_change_1h_pct": oi_chg_1h,
        "top_position_ls_ratio": top_ratio,
        "global_account_ls_ratio": glob_ratio,
        "whale_retail_divergence": div,
        "perp_taker_buy_sell_ratio": taker_ratio,
        "est_liq_pressure_usd": liq_usd,
        "liq_cascade_regime": liq_regime,
        "counterparty_regime": regime,
    }


def _kline_ret_and_tbr(
    symbol: str,
    interval: str,
    limit: int = 3,
) -> tuple[float | None, float | None, int | None]:
    kl = _http_get_json(
        "https://api.binance.com/api/v3/klines",
        {"symbol": symbol, "interval": interval, "limit": limit},
    )
    if not isinstance(kl, list) or len(kl) < 2:
        return None, None, None
    # Last closed bar. Its close time (index 6) is the exchange source time.
    bar = kl[-2] if len(kl) >= 2 else kl[-1]
    if not isinstance(bar, list) or len(bar) < 11:
        return None, None, None
    op = _finite(bar[1])
    cl = _finite(bar[4])
    vol = _finite(bar[5])
    close_ts = _finite(bar[6])
    tb_base = _finite(bar[9])
    ret = round(((cl - op) / op) * 100.0, 4) if (op and cl and op > 0) else None
    tbr = round(tb_base / vol, 4) if (vol and tb_base is not None and vol > 0) else None
    return ret, tbr, int(close_ts) if close_ts is not None else None


def _publish_spot_top5(
    symbol: str,
    depth: Any,
    metrics: dict[str, Any],
) -> tuple[int | None, int | None]:
    """Return ``(book_update_id, source_ts_ms)`` and clear untimed top-5 values."""
    endpoint = "https://api.binance.com/api/v3/depth"
    if metrics.get("obi_top5_error"):
        record_fetch_error(
            category="spot_obi_top5",
            symbol=symbol,
            endpoint=endpoint,
            message=str(metrics["obi_top5_error"]),
            error_code=str(metrics["obi_top5_error"]),
        )
        return None, None
    update_id = _finite(depth.get("lastUpdateId")) if isinstance(depth, dict) else None
    if update_id is None:
        metrics["obi_top5"] = None
        metrics["obi_top5_error"] = "obi_top5_update_id_missing"
        record_fetch_error(
            category="spot_obi_top5",
            symbol=symbol,
            endpoint=endpoint,
            message="obi_top5_update_id_missing",
            error_code="obi_top5_update_id_missing",
        )
        return None, None
    book_id = int(update_id)
    source_ts: int | None = None
    source_err: str | None = None
    for _attempt in range(2):
        source_ts, source_err = capture_depth_source_ts(symbol, book_id)
        if source_ts is not None or source_err != "obi_top5_source_ts_gap":
            break
        _SLEEP(HTTP_BACKOFF_BASE_SEC)
    if source_ts is None:
        metrics["obi_top5"] = None
        metrics["bid_depth_5_usd"] = None
        metrics["ask_depth_5_usd"] = None
        metrics["obi_top5_error"] = source_err or "obi_top5_source_ts_missing"
        record_fetch_error(
            category="spot_obi_top5",
            symbol=symbol,
            endpoint=spot_depth_ws_url(symbol),
            message=str(metrics["obi_top5_error"]),
            error_code=str(metrics["obi_top5_error"]),
        )
        return book_id, None
    metrics["obi_top5_error"] = None
    return book_id, source_ts


def fetch_binance_spot_microstructure_snapshot(symbol: str, *, now_ms: int | None = None) -> dict[str, Any] | None:
    """Collect Category 7 klines, 20-level depth, top-5 notional OBI, and whale aggTrades.

    ``now_ms`` is not a source timestamp. ``obi_top5`` is omitted unless a
    diff-depth event time covers the REST book's ``lastUpdateId``.
    """
    del now_ms
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None

    ret_5m, tbr_5m, close_5m = _kline_ret_and_tbr(sym, "5m", 3)
    ret_1h, tbr_1h, _close_1h = _kline_ret_and_tbr(sym, "1h", 3)
    ret_4h, _, _close_4h = _kline_ret_and_tbr(sym, "4h", 3)
    ret_1d, _, _close_1d = _kline_ret_and_tbr(sym, "1d", 3)

    depth_url = "https://api.binance.com/api/v3/depth"
    depth = _http_get_json(depth_url, {"symbol": sym, "limit": 20})
    agg = _http_get_json("https://api.binance.com/api/v3/aggTrades", {"symbol": sym, "limit": 250})
    depth_book = depth if isinstance(depth, dict) and "bids" in depth else None
    ob_metrics = compute_spot_orderbook_and_aggtrades_metrics(
        depth_book,
        agg if isinstance(agg, list) else None,
    )
    if depth_book is None:
        ob_metrics["obi_top5"] = None
        ob_metrics["bid_depth_5_usd"] = None
        ob_metrics["ask_depth_5_usd"] = None
        ob_metrics["obi_top5_error"] = "depth_missing"
        _fail_from_last("spot", sym, depth_url, "depth_missing")
        book_id, book_ts = None, None
    else:
        book_id, book_ts = _publish_spot_top5(sym, depth_book, ob_metrics)
    if ret_5m is None and ob_metrics["spread_bps"] is None and book_ts is None:
        _fail_from_last("spot", sym, depth_url, "spot_snapshot_missing")
        return None
    if book_ts is not None:
        row_ts = book_ts
        source_ts = book_ts
    elif close_5m is not None:
        row_ts = close_5m
        source_ts = close_5m
    else:
        record_fetch_error(
            category="spot",
            symbol=sym,
            endpoint=depth_url,
            message="spot_source_ts_missing",
            error_code="spot_source_ts_missing",
        )
        return None
    return {
        "symbol": sym,
        "ts_ms": row_ts,
        "source_ts_ms": source_ts,
        "collected_at_ms": _clock_ms(),
        "kline_5m_close_ts_ms": close_5m,
        "book_update_id": book_id,
        "obi_top5_source_ts_ms": book_ts,
        "ret_5m_pct": ret_5m,
        "ret_1h_pct": ret_1h,
        "ret_4h_pct": ret_4h,
        "ret_1d_pct": ret_1d,
        "taker_buy_ratio_5m": tbr_5m,
        "taker_buy_ratio_1h": tbr_1h,
        **ob_metrics,
    }


def fetch_cross_exchange_spot_snapshot(symbol: str, *, now_ms: int | None = None) -> dict[str, Any] | None:
    """Collect Category 8: Binance vs OKX vs Bybit cross-exchange spot price dislocation."""
    sym = str(symbol or "").strip().upper()
    if not sym.endswith("USDT"):
        return None
    base = sym[:-4]
    del now_ms

    b_resp = _http_get_json("https://api.binance.com/api/v3/ticker/price", {"symbol": sym})
    b_px = _finite(b_resp.get("price")) if isinstance(b_resp, dict) else None

    okx_resp = _http_get_json("https://www.okx.com/api/v5/market/ticker", {"instId": f"{base}-USDT"})
    okx_data = okx_resp.get("data", []) if isinstance(okx_resp, dict) else []
    okx_row = okx_data[0] if (isinstance(okx_data, list) and okx_data and isinstance(okx_data[0], dict)) else {}
    okx_px = _finite(okx_row.get("last")) if okx_row else None
    # OKX ticker ``ts`` is the instrument event time. Bybit's envelope ``time``
    # and Binance ticker/price have no book/trade event time, so they are not
    # used as a source clock.
    okx_ts = _finite(okx_row.get("ts")) if okx_row else None

    byb_resp = _http_get_json("https://api.bybit.com/v5/market/tickers", {"category": "spot", "symbol": sym})
    byb_list = ((byb_resp.get("result") or {}).get("list") or []) if isinstance(byb_resp, dict) else []
    byb_px = _finite(byb_list[0].get("lastPrice")) if (isinstance(byb_list, list) and byb_list and isinstance(byb_list[0], dict)) else None

    if b_px is None and okx_px is None and byb_px is None:
        _fail_from_last("cross_exchange", sym, "cross_exchange", "cross_exchange_missing")
        return None
    if okx_ts is None:
        record_fetch_error(
            category="cross_exchange",
            symbol=sym,
            endpoint="https://www.okx.com/api/v5/market/ticker",
            message="cross_exchange_source_ts_missing",
            error_code="cross_exchange_source_ts_missing",
        )
        return None
    diff_bps, regime = compute_cross_exchange_dislocation(b_px, okx_px, byb_px)
    return {
        "symbol": sym,
        "ts_ms": int(okx_ts),
        "source_ts_ms": int(okx_ts),
        "collected_at_ms": _clock_ms(),
        "binance_price": b_px,
        "okx_price": okx_px,
        "bybit_price": byb_px,
        "max_venue_dislocation_bps": diff_bps,
        "dislocation_regime": regime,
    }


def fetch_deribit_options_snapshot(currency: str = "BTC", *, now_ms: int | None = None) -> dict[str, Any] | None:
    ccy = str(currency or "BTC").strip().upper()
    # Query bound only. The stored row time is the last DVOL candle timestamp.
    query_end_ms = int(now_ms if now_ms is not None else _clock_ms())
    start_ms = query_end_ms - 7 * 24 * 3600 * 1000

    dvol_resp = _http_get_json(
        "https://www.deribit.com/api/v2/public/get_volatility_index_data",
        {
            "currency": ccy,
            "start_timestamp": start_ms,
            "end_timestamp": query_end_ms,
            "resolution": "3600",
        },
    )
    dvol_rows = (
        dvol_resp.get("result", {}).get("data", [])
        if isinstance(dvol_resp, dict) and isinstance(dvol_resp.get("result"), dict)
        else []
    )
    dvol_points = [
        (int(r[0]), float(r[4]))
        for r in dvol_rows
        if isinstance(r, list) and len(r) >= 5 and _finite(r[0]) is not None and _finite(r[4]) is not None
    ]
    dvol_closes = [close for _ts, close in dvol_points]
    dvol_source_ts = dvol_points[-1][0] if dvol_points else None

    idx_resp = _http_get_json(
        "https://www.deribit.com/api/v2/public/get_index_price",
        {"index_name": f"{ccy.lower()}_usd"},
    )
    spot_px = (
        _finite(idx_resp.get("result", {}).get("index_price"))
        if isinstance(idx_resp, dict) and isinstance(idx_resp.get("result"), dict)
        else None
    )

    book_resp = _http_get_json(
        "https://www.deribit.com/api/v2/public/get_book_summary_by_currency",
        {"currency": ccy, "kind": "option"},
    )
    summaries = (
        book_resp.get("result", [])
        if isinstance(book_resp, dict) and isinstance(book_resp.get("result"), list)
        else []
    )
    if dvol_source_ts is None:
        record_fetch_error(
            category="deribit",
            symbol=ccy,
            endpoint="https://www.deribit.com/api/v2/public/get_volatility_index_data",
            message="deribit_source_ts_missing",
            error_code="deribit_source_ts_missing",
        )
        return None
    if not summaries and not dvol_closes:
        record_fetch_error(
            category="deribit",
            symbol=ccy,
            endpoint="https://www.deribit.com/api/v2/public/get_book_summary_by_currency",
            message="deribit_book_missing",
            error_code="deribit_book_missing",
        )
        return None

    stats = compute_deribit_option_walls_and_max_pain(
        summaries,
        spot_price=spot_px,
        dvol_series=dvol_closes,
    )
    stats["currency"] = ccy
    stats["ts_ms"] = dvol_source_ts
    stats["source_ts_ms"] = dvol_source_ts
    stats["collected_at_ms"] = _clock_ms()
    return stats


def _fetch_fred_latest(series_id: str) -> tuple[str | None, float | None]:
    text = _http_get_text(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}")
    if not text:
        return None, None
    reader = csv.reader(io.StringIO(text))
    last_date: str | None = None
    last_val: float | None = None
    for idx, row in enumerate(reader):
        if idx == 0 or len(row) < 2:
            continue
        val = _finite(row[1])
        if val is not None:
            last_date = row[0].strip()
            last_val = val
    return last_date, last_val


def fetch_fred_macro_liquidity() -> dict[str, Any] | None:
    d_w, walcl = _fetch_fred_latest("WALCL")
    d_t, tga = _fetch_fred_latest("WTREGEN")
    d_r, rrp = _fetch_fred_latest("RRPONTSYD")
    d_h, hy_oas_pct = _fetch_fred_latest("BAMLH0A0HYM2")
    asof = max(d for d in (d_w, d_t, d_r, d_h) if d is not None) if any((d_w, d_t, d_r, d_h)) else None
    if asof is None:
        _fail_from_last("fred", None, "https://fred.stlouisfed.org/graph/fredgraph.csv", "fred_missing")
        return None
    net_liq = compute_fed_net_liquidity(walcl, tga, rrp)
    hy_bps = round(hy_oas_pct * 100.0, 2) if hy_oas_pct is not None else None
    return {
        "asof_date": asof,
        "walcl_millions": walcl,
        "tga_millions": tga,
        "on_rrp_billions": rrp,
        "net_liquidity_billions": net_liq,
        "hy_oas_bps": hy_bps,
    }


def fetch_stablecoin_supply(*, now_ms: int | None = None) -> dict[str, Any] | None:
    del now_ms
    endpoint = "https://stablecoins.llama.fi/stablecoins"
    data = _http_get_json(endpoint, {"includePrices": "true"})
    assets = data.get("peggedAssets", []) if isinstance(data, dict) else []
    if not isinstance(data, dict):
        _fail_from_last("stablecoin", None, endpoint, "stablecoin_missing")
        return None
    if not isinstance(assets, list) or not assets:
        record_fetch_error(
            category="stablecoin",
            symbol=None,
            endpoint=endpoint,
            message="stablecoin_supply_missing",
            error_code="stablecoin_supply_missing",
        )
        return None
    usdt_cur = 0.0
    usdc_cur = 0.0
    prev_day = 0.0
    prev_wk = 0.0
    for a in assets:
        if not isinstance(a, dict):
            continue
        sym = str(a.get("symbol") or "").upper()
        if sym not in ("USDT", "USDC"):
            continue
        cur = _finite((a.get("circulating") or {}).get("peggedUSD")) or 0.0
        d1 = _finite((a.get("circulatingPrevDay") or {}).get("peggedUSD")) or cur
        w1 = _finite((a.get("circulatingPrevWeek") or {}).get("peggedUSD")) or cur
        if sym == "USDT":
            usdt_cur = cur
        else:
            usdc_cur = cur
        prev_day += d1
        prev_wk += w1
    total = usdt_cur + usdc_cur
    if total <= 0:
        record_fetch_error(
            category="stablecoin",
            symbol=None,
            endpoint="https://stablecoins.llama.fi/stablecoins",
            message="stablecoin_supply_missing",
            error_code="stablecoin_supply_missing",
        )
        return None
    # The aggregate payload has no event time. ``now_ms`` is not a substitute.
    source_ts = _finite(data.get("updatedAt") or data.get("source_ts_ms")) if isinstance(data, dict) else None
    if source_ts is None:
        record_fetch_error(
            category="stablecoin",
            symbol=None,
            endpoint="https://stablecoins.llama.fi/stablecoins",
            message="stablecoin_source_ts_unavailable",
            error_code="stablecoin_source_ts_unavailable",
        )
        return None
    chg_24h = round(((total - prev_day) / prev_day) * 100.0, 4) if prev_day > 0 else 0.0
    chg_7d = round(((total - prev_wk) / prev_wk) * 100.0, 4) if prev_wk > 0 else 0.0
    return {
        "ts_ms": int(source_ts),
        "source_ts_ms": int(source_ts),
        "collected_at_ms": _clock_ms(),
        "usdt_supply_usd": round(usdt_cur, 2),
        "usdc_supply_usd": round(usdc_cur, 2),
        "total_major_stable_usd": round(total, 2),
        "change_24h_pct": chg_24h,
        "change_7d_pct": chg_7d,
    }


def _insert_snapshot(conn: sqlite3.Connection, sql: str, row: dict[str, Any], *, category: str, symbol: str | None) -> bool:
    try:
        conn.execute(sql, row)
    except sqlite3.Error as exc:
        record_fetch_error(
            category=category,
            symbol=symbol,
            endpoint="sqlite",
            message=f"sqlite:{exc}",
            error_code="sqlite",
        )
        return False
    return True


def collect_all_to_sqlite(
    db_path: Path = DEFAULT_DB_PATH,
    *,
    symbols: Sequence[str] = DEFAULT_PERP_SYMBOLS,
    spot_symbols: Sequence[str] = DEFAULT_SPOT_MICRO_SYMBOLS,
    cross_symbols: Sequence[str] = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "DOGEUSDT", "SUIUSDT"),
    option_currencies: Sequence[str] = ("BTC", "ETH"),
) -> dict[str, Any]:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    errors: list[dict[str, Any]] = []
    token = _ROUND_ERRORS.set(errors)
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=30.0)
        ensure_counterparty_schema(conn)

        perp_count = 0
        for sym in symbols:
            try:
                snap = fetch_binance_perp_symbol_snapshot(sym)
            except Exception as exc:  # noqa: BLE001 — one symbol cannot abort the round
                record_fetch_error(
                    category="perp",
                    symbol=str(sym),
                    endpoint="https://fapi.binance.com/fapi/v1/premiumIndex",
                    message=f"{type(exc).__name__}:{exc}",
                    error_code=type(exc).__name__,
                )
                continue
            if snap is None:
                continue
            if _insert_snapshot(
                conn,
                """
                INSERT OR REPLACE INTO binance_perp_counterparty (
                    symbol, ts_ms, funding_rate, funding_z, mark_price, index_price,
                    perp_basis_bps, open_interest, open_interest_usd, oi_change_1h_pct,
                    top_position_ls_ratio, global_account_ls_ratio, whale_retail_divergence,
                    perp_taker_buy_sell_ratio, est_liq_pressure_usd, liq_cascade_regime,
                    counterparty_regime, collected_at_ms, source_ts_ms, venue_symbol,
                    strategy_symbol, price_scale, oi_source_ts_ms
                ) VALUES (
                    :symbol, :ts_ms, :funding_rate, :funding_z, :mark_price, :index_price,
                    :perp_basis_bps, :open_interest, :open_interest_usd, :oi_change_1h_pct,
                    :top_position_ls_ratio, :global_account_ls_ratio, :whale_retail_divergence,
                    :perp_taker_buy_sell_ratio, :est_liq_pressure_usd, :liq_cascade_regime,
                    :counterparty_regime, :collected_at_ms, :source_ts_ms, :venue_symbol,
                    :strategy_symbol, :price_scale, :oi_source_ts_ms
                )
                """,
                snap,
                category="perp",
                symbol=str(sym),
            ):
                perp_count += 1

        spot_count = 0
        for sym in spot_symbols:
            try:
                s_snap = fetch_binance_spot_microstructure_snapshot(sym)
            except Exception as exc:  # noqa: BLE001
                record_fetch_error(
                    category="spot",
                    symbol=str(sym),
                    endpoint="https://api.binance.com/api/v3/depth",
                    message=f"{type(exc).__name__}:{exc}",
                    error_code=type(exc).__name__,
                )
                continue
            if s_snap is None:
                continue
            if _insert_snapshot(
                conn,
                """
                INSERT OR REPLACE INTO binance_spot_microstructure (
                    symbol, ts_ms, ret_5m_pct, ret_1h_pct, ret_4h_pct, ret_1d_pct,
                    taker_buy_ratio_5m, taker_buy_ratio_1h, spread_bps,
                    bid_depth_20_usd, ask_depth_20_usd, orderbook_imbalance_20,
                    impact_10k_bps, whale_net_buy_usd, aggtrade_whale_buy_ratio,
                    bid_depth_5_usd, ask_depth_5_usd, obi_top5, obi_top5_error,
                    obi_top5_source_ts_ms, book_update_id, source_ts_ms,
                    collected_at_ms, kline_5m_close_ts_ms
                ) VALUES (
                    :symbol, :ts_ms, :ret_5m_pct, :ret_1h_pct, :ret_4h_pct, :ret_1d_pct,
                    :taker_buy_ratio_5m, :taker_buy_ratio_1h, :spread_bps,
                    :bid_depth_20_usd, :ask_depth_20_usd, :orderbook_imbalance_20,
                    :impact_10k_bps, :whale_net_buy_usd, :aggtrade_whale_buy_ratio,
                    :bid_depth_5_usd, :ask_depth_5_usd, :obi_top5, :obi_top5_error,
                    :obi_top5_source_ts_ms, :book_update_id, :source_ts_ms,
                    :collected_at_ms, :kline_5m_close_ts_ms
                )
                """,
                s_snap,
                category="spot",
                symbol=str(sym),
            ):
                spot_count += 1

        xex_count = 0
        for sym in cross_symbols:
            try:
                x_snap = fetch_cross_exchange_spot_snapshot(sym)
            except Exception as exc:  # noqa: BLE001
                record_fetch_error(
                    category="cross_exchange",
                    symbol=str(sym),
                    endpoint="cross_exchange",
                    message=f"{type(exc).__name__}:{exc}",
                    error_code=type(exc).__name__,
                )
                continue
            if x_snap is None:
                continue
            if _insert_snapshot(
                conn,
                """
                INSERT OR REPLACE INTO cross_exchange_spot_baseline (
                    symbol, ts_ms, binance_price, okx_price, bybit_price,
                    max_venue_dislocation_bps, dislocation_regime,
                    source_ts_ms, collected_at_ms
                ) VALUES (
                    :symbol, :ts_ms, :binance_price, :okx_price, :bybit_price,
                    :max_venue_dislocation_bps, :dislocation_regime,
                    :source_ts_ms, :collected_at_ms
                )
                """,
                x_snap,
                category="cross_exchange",
                symbol=str(sym),
            ):
                xex_count += 1

        opt_count = 0
        for ccy in option_currencies:
            try:
                opt = fetch_deribit_options_snapshot(ccy)
            except Exception as exc:  # noqa: BLE001
                record_fetch_error(
                    category="deribit",
                    symbol=str(ccy),
                    endpoint="https://www.deribit.com/api/v2/public/get_volatility_index_data",
                    message=f"{type(exc).__name__}:{exc}",
                    error_code=type(exc).__name__,
                )
                continue
            if opt is None:
                continue
            if _insert_snapshot(
                conn,
                """
                INSERT OR REPLACE INTO deribit_options_gamma (
                    currency, ts_ms, spot_price, dvol_close, dvol_change_24h_pct,
                    dvol_zscore, total_call_oi, total_put_oi, put_call_oi_ratio,
                    max_pain_strike, top_call_wall_strike, top_put_wall_strike,
                    gamma_regime, source_ts_ms, collected_at_ms
                ) VALUES (
                    :currency, :ts_ms, :spot_price, :dvol_close, :dvol_change_24h_pct,
                    :dvol_zscore, :total_call_oi, :total_put_oi, :put_call_oi_ratio,
                    :max_pain_strike, :top_call_wall_strike, :top_put_wall_strike,
                    :gamma_regime, :source_ts_ms, :collected_at_ms
                )
                """,
                opt,
                category="deribit",
                symbol=str(ccy),
            ):
                opt_count += 1

        unlock_count = 0
        now_ms = _clock_ms()
        for item in INSTITUTIONAL_CLIFF_UNLOCK_CALENDAR:
            try:
                u_info = evaluate_token_unlock_window(str(item["symbol"]), now_ms=now_ms)
            except Exception as exc:  # noqa: BLE001
                record_fetch_error(
                    category="token_unlock",
                    symbol=str(item.get("symbol") or ""),
                    endpoint="unlock_calendar",
                    message=f"{type(exc).__name__}:{exc}",
                    error_code=type(exc).__name__,
                )
                continue
            if u_info is None:
                continue
            if _insert_snapshot(
                conn,
                """
                INSERT OR REPLACE INTO token_unlock_schedule (
                    symbol, next_unlock_utc, hours_to_unlock,
                    unlock_pct_circulating, unlock_usd_est, recipient_type,
                    in_unlock_blackout
                ) VALUES (
                    :symbol, :next_unlock_utc, :hours_to_unlock,
                    :unlock_pct_circulating, :unlock_usd_est, :recipient_type,
                    :in_unlock_blackout
                )
                """,
                u_info,
                category="token_unlock",
                symbol=str(item.get("symbol") or ""),
            ):
                unlock_count += 1

        fred_count = 0
        try:
            fred = fetch_fred_macro_liquidity()
        except Exception as exc:  # noqa: BLE001
            fred = None
            record_fetch_error(
                category="fred",
                symbol=None,
                endpoint="https://fred.stlouisfed.org/graph/fredgraph.csv",
                message=f"{type(exc).__name__}:{exc}",
                error_code=type(exc).__name__,
            )
        if fred is not None and _insert_snapshot(
            conn,
            """
            INSERT OR REPLACE INTO macro_net_liquidity (
                asof_date, walcl_millions, tga_millions, on_rrp_billions,
                net_liquidity_billions, hy_oas_bps
            ) VALUES (
                :asof_date, :walcl_millions, :tga_millions, :on_rrp_billions,
                :net_liquidity_billions, :hy_oas_bps
            )
            """,
            fred,
            category="fred",
            symbol=None,
        ):
            fred_count = 1

        stable_count = 0
        try:
            stable = fetch_stablecoin_supply()
        except Exception as exc:  # noqa: BLE001
            stable = None
            record_fetch_error(
                category="stablecoin",
                symbol=None,
                endpoint="https://stablecoins.llama.fi/stablecoins",
                message=f"{type(exc).__name__}:{exc}",
                error_code=type(exc).__name__,
            )
        if stable is not None and _insert_snapshot(
            conn,
            """
            INSERT OR REPLACE INTO stablecoin_supply (
                ts_ms, usdt_supply_usd, usdc_supply_usd, total_major_stable_usd,
                change_24h_pct, change_7d_pct, source_ts_ms, collected_at_ms
            ) VALUES (
                :ts_ms, :usdt_supply_usd, :usdc_supply_usd, :total_major_stable_usd,
                :change_24h_pct, :change_7d_pct, :source_ts_ms, :collected_at_ms
            )
            """,
            stable,
            category="stablecoin",
            symbol=None,
        ):
            stable_count = 1

        for err in errors:
            conn.execute(
                """
                INSERT INTO collector_fetch_errors (
                    collected_at_ms, category, symbol, endpoint, http_status,
                    error_code, message, fails_round
                ) VALUES (
                    :collected_at_ms, :category, :symbol, :endpoint, :http_status,
                    :error_code, :message, :fails_round
                )
                """,
                {**err, "fails_round": 1 if err.get("fails_round", True) else 0},
            )
        conn.commit()
        return {
            "cat6_perp_snapshots": perp_count,
            "cat7_spot_microstructure": spot_count,
            "cat8_cross_exchange": xex_count,
            "cat8_deribit_options": opt_count,
            "cat9_token_unlocks": unlock_count,
            "cat9_fred_liquidity": fred_count,
            "cat9_stablecoin_supply": stable_count,
            "errors": errors,
        }
    finally:
        _ROUND_ERRORS.reset(token)
        if conn is not None:
            conn.close()


def round_exit_code(stats: dict[str, Any]) -> int:
    errors = stats.get("errors") or []
    if any(err.get("fails_round", True) for err in errors):
        return PARTIAL_EXIT
    return 0


def acquire_collector_lock(db_path: Path):
    """Exclusive non-blocking lock held for the process lifetime of the collector."""
    path = Path(str(db_path) + ".collector.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise CollectorLockBusy(str(path)) from exc
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def resolve_db_path(cli_path: Path | None) -> Path:
    if cli_path is not None:
        return Path(cli_path).expanduser()
    env = os.environ.get("PUBLIC_DERIVATIVES_DB_PATH", "").strip()
    if env:
        return Path(env).expanduser()
    return DEFAULT_DB_PATH


def run_collection(
    db_path: Path,
    *,
    symbols: Sequence[str],
    spot_symbols: Sequence[str],
    loop: bool = False,
    interval: int = DEFAULT_LOOP_INTERVAL_SEC,
    max_rounds: int = 0,
    cross_symbols: Sequence[str] | None = None,
    option_currencies: Sequence[str] | None = None,
    sleep: Callable[[float], None] | None = None,
    collect: Callable[..., dict[str, Any]] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> int:
    """One shot, or a supervised loop. A round failure does not abort the next round."""
    try:
        lock = acquire_collector_lock(db_path)
    except CollectorLockBusy as exc:
        print(f"public_feed_lock_busy {exc}", file=sys.stderr)
        return LOCK_BUSY_EXIT
    sleeper = sleep or _SLEEP
    collector = collect or collect_all_to_sqlite
    pause_base = max(1, int(interval))
    rounds = 0
    last = 0
    try:
        while True:
            started = _CLOCK()
            try:
                kwargs: dict[str, Any] = {"symbols": symbols, "spot_symbols": spot_symbols}
                if cross_symbols is not None:
                    kwargs["cross_symbols"] = cross_symbols
                if option_currencies is not None:
                    kwargs["option_currencies"] = option_currencies
                stats = collector(db_path, **kwargs)
                last = round_exit_code(stats)
                print(
                    f"Saved Categories 6-9 counterparty data to {db_path}: "
                    f"exit={last} errors={len(stats.get('errors') or [])} {stats}"
                )
            except Exception as exc:  # noqa: BLE001 — keep a supervised loop alive
                last = FATAL_EXIT
                print(f"public_feed_round_crash {type(exc).__name__}:{exc}", file=sys.stderr)
            rounds += 1
            if not loop:
                return last
            if max_rounds and rounds >= max_rounds:
                return last
            if should_stop is not None and should_stop():
                return 0
            elapsed = max(0.0, _CLOCK() - started)
            pause = max(0.0, pause_base - elapsed)
            if last != 0:
                pause = max(pause, min(HTTP_BACKOFF_CAP_SEC, HTTP_BACKOFF_BASE_SEC * (2 ** min(rounds, 4))))
            sleeper(pause)
    finally:
        lock.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch public derivatives, spot L2, cross-exchange, options, and token unlocks. "
            "One shot by default. --loop repeats every --interval seconds. "
            "No orders and no API keys."
        )
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Output SQLite path. Default: PUBLIC_DERIVATIVES_DB_PATH or data/counterparty/public_derivatives.db.",
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default=",".join(DEFAULT_PERP_SYMBOLS),
        help="Comma-separated Binance USD-M perpetual symbols. PEPE's listed contract is 1000PEPEUSDT.",
    )
    parser.add_argument(
        "--spot-symbols",
        type=str,
        default=",".join(DEFAULT_SPOT_MICRO_SYMBOLS),
        help="Comma-separated Binance spot symbols for klines, top-20 depth, and top-5 OBI.",
    )
    parser.add_argument("--loop", action="store_true", help="Keep collecting. One round's errors do not stop the next.")
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_LOOP_INTERVAL_SEC,
        help=f"Seconds between loop rounds (default {DEFAULT_LOOP_INTERVAL_SEC}). Used only with --loop.",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=0,
        help="Stop the loop after this many rounds. 0 runs until SIGINT/SIGTERM.",
    )
    parser.add_argument(
        "--http-timeout",
        type=float,
        default=DEFAULT_HTTP_TIMEOUT_SEC,
        help="Per-request timeout in seconds. Transient HTTP failures back off and retry.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.interval < 1:
        parser.error("--interval must be >= 1")
    timeout_token = _HTTP_TIMEOUT.set(float(args.http_timeout))
    stop = False

    def _stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    restored: list[tuple[int, Any]] = []
    if args.loop:
        restored.append((signal.SIGINT, signal.signal(signal.SIGINT, _stop)))
        restored.append((signal.SIGTERM, signal.signal(signal.SIGTERM, _stop)))
    try:
        return run_collection(
            resolve_db_path(args.db),
            symbols=[s.strip().upper() for s in args.symbols.split(",") if s.strip()],
            spot_symbols=[s.strip().upper() for s in args.spot_symbols.split(",") if s.strip()],
            loop=bool(args.loop),
            interval=int(args.interval),
            max_rounds=int(args.max_rounds),
            should_stop=(lambda: stop) if args.loop else None,
        )
    finally:
        _HTTP_TIMEOUT.reset(timeout_token)
        for sig, handler in restored:
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
