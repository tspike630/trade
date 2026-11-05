"""Public Binance kline GET for the live_bnb MVP. No API key, no orders.

Isolated on purpose. The decision tick calls this only when
``LIVE_BNB_KLINE_FETCH=1``. Production factors should come from the
indicator side tables (``binance_indicators`` / ``binance_klines``);
this module does not read secrets and does not touch
``LIVE_TRADING_DISABLED``.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from logging_setup import get_logger

log = get_logger("decision_engine.bnb_kline_fetch")

PUBLIC_KLINES_URL = "https://api.binance.com/api/v3/klines"
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,24}$")
# Binance kline array: open ms, o, h, l, c, base vol, close ms, quote vol, ...
_QUOTE_VOLUME_INDEX = 7


def fetch_public_klines(
    symbol: str,
    *,
    interval: str = "5m",
    limit: int = 300,
    timeout: float = 8.0,
) -> list[tuple[int, float, float, float, float, float, float | None]]:
    """Closed-bar payload is the caller's job. Returns ``[]`` on any failure.

    Each tuple is ``(open_ms, open, high, low, close, base_volume, quote_volume)``.
    """
    spot = str(symbol or "").strip().upper()
    if not _SYMBOL_RE.match(spot):
        log.warning("kline fetch refused symbol=%r", symbol)
        return []
    tf = (interval or "5m").strip()
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = 300
    n = min(1000, max(1, n))
    query = urlencode({"symbol": spot, "interval": tf, "limit": n})
    req = Request(
        f"{PUBLIC_KLINES_URL}?{query}",
        headers={"Accept": "application/json", "User-Agent": "trade-decision-bnb-native"},
        method="GET",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        log.warning("kline fetch failed symbol=%s err=%s", spot, exc)
        return []
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("kline fetch bad json symbol=%s err=%s", spot, exc)
        return []
    if not isinstance(payload, list):
        log.warning("kline fetch unexpected body symbol=%s", spot)
        return []
    out: list[tuple[int, float, float, float, float, float, float | None]] = []
    for item in payload:
        parsed = _parse_rest_kline(item)
        if parsed is not None:
            out.append(parsed)
    return out


def _parse_rest_kline(
    item: Any,
) -> tuple[int, float, float, float, float, float, float | None] | None:
    if not isinstance(item, (list, tuple)) or len(item) < 6:
        return None
    try:
        open_ms = int(item[0])
        op = float(item[1])
        hi = float(item[2])
        lo = float(item[3])
        close = float(item[4])
        base_vol = float(item[5])
    except (TypeError, ValueError):
        return None
    quote: float | None
    try:
        quote = float(item[_QUOTE_VOLUME_INDEX]) if len(item) > _QUOTE_VOLUME_INDEX else None
    except (TypeError, ValueError):
        quote = None
    return (open_ms, op, hi, lo, close, base_vol, quote)
