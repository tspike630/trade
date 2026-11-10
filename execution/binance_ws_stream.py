"""Low-latency Binance Spot WebSocket L1/L2 & Kline Stream Engine with REST Fallback.

Replaces synchronous REST polling on the hot path with an in-memory + shared-snapshot
multiplexed WebSocket cache for:

1. ``<symbol>@bookTicker`` — Real-time L1 best bid/ask price & quantity ticks.
2. ``<symbol>@depth20@100ms`` — Real-time L2 top-20 bid/ask orderbook snapshots (100ms push).
3. ``<symbol>@kline_<interval>`` — Real-time kline updates with closed-bar (``x=true``)
   event callbacks and active taker-buy volume fields (``V``, ``Q``).

Every read enforces a strict monotonic staleness threshold (``max_age_ms``, default
2500ms for L1/L2 book and 10000ms for klines). When the WebSocket stream is disabled,
disconnected, or stale, callers transparently fall back to REST HTTP endpoints
without dropping or corrupting orders.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterable, Sequence

from logging_setup import get_logger

log = get_logger("execution.binance_ws_stream")

WS_ENABLED_ENV = "BINANCE_SPOT_WS_ENABLED"
WS_BASE_URL_ENV = "BINANCE_SPOT_WS_BASE"
WS_MAX_AGE_MS_ENV = "BINANCE_SPOT_WS_MAX_AGE_MS"
WS_SNAPSHOT_PATH_ENV = "BINANCE_SPOT_WS_SNAPSHOT_PATH"

DEFAULT_WS_BASE_URL = "wss://stream.binance.com:9443"
DEFAULT_MAX_AGE_MS = 2500
DEFAULT_KLINE_MAX_AGE_MS = 360_000
MAX_STREAMS_PER_CONN = 180


def ws_stream_enabled() -> bool:
    raw = os.environ.get(WS_ENABLED_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


def default_max_age_ms() -> int:
    raw = os.environ.get(WS_MAX_AGE_MS_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_MAX_AGE_MS
    try:
        val = int(float(raw.strip()))
    except (TypeError, ValueError):
        return DEFAULT_MAX_AGE_MS
    return max(100, val)


def _now_mono_ms() -> int:
    return int(time.monotonic() * 1000)


def _now_epoch_ms() -> int:
    return int(time.time() * 1000)


def _finite_float(raw: object) -> float | None:
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(val):
        return None
    return val


def _pos_str(raw: object) -> str | None:
    val = _finite_float(raw)
    if val is None or val <= 0:
        return None
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return f"{val:.8f}".rstrip("0").rstrip(".")


@dataclass(frozen=True)
class L1BookTick:
    symbol: str
    best_bid: str
    best_bid_qty: str
    best_ask: str
    best_ask_qty: str
    mid_price: str
    event_time_ms: int
    received_mono_ms: int
    received_epoch_ms: int


@dataclass(frozen=True)
class L2DepthSnapshot:
    symbol: str
    bids: list[list[str]]
    asks: list[list[str]]
    last_update_id: int | None
    event_time_ms: int
    received_mono_ms: int
    received_epoch_ms: int


@dataclass(frozen=True)
class WsKlineBar:
    symbol: str
    interval: str
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    trades: int | None
    taker_buy_base_volume: float | None
    taker_buy_quote_volume: float | None
    is_closed: bool
    event_time_ms: int
    received_mono_ms: int


def build_combined_stream_names(
    symbols: Iterable[str],
    *,
    book_ticker: bool = True,
    depth20: bool = True,
    kline_interval: str | None = "5m",
) -> list[str]:
    """Build lowercase Binance combined stream names for ``symbols``."""
    names: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        sym = str(raw or "").strip().lower()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        if book_ticker:
            names.append(f"{sym}@bookTicker")
        if depth20:
            names.append(f"{sym}@depth20@100ms")
        if kline_interval:
            names.append(f"{sym}@kline_{kline_interval.strip()}")
    return names


def build_combined_stream_urls(
    symbols: Iterable[str],
    *,
    base_url: str | None = None,
    book_ticker: bool = True,
    depth20: bool = True,
    kline_interval: str | None = "5m",
    max_streams_per_conn: int = MAX_STREAMS_PER_CONN,
) -> list[str]:
    """Chunk combined stream names into ``wss://.../stream?streams=...`` URLs."""
    root = (
        base_url
        or os.environ.get(WS_BASE_URL_ENV, "").strip()
        or DEFAULT_WS_BASE_URL
    ).rstrip("/")
    names = build_combined_stream_names(
        symbols,
        book_ticker=book_ticker,
        depth20=depth20,
        kline_interval=kline_interval,
    )
    if not names:
        return []
    step = max(1, min(int(max_streams_per_conn), 1024))
    urls: list[str] = []
    for idx in range(0, len(names), step):
        chunk = "/".join(names[idx : idx + step])
        urls.append(f"{root}/stream?streams={chunk}")
    return urls


def _clean_levels(raw: object, *, limit: int = 20) -> list[list[str]]:
    if not isinstance(raw, list):
        return []
    out: list[list[str]] = []
    for item in raw[:limit]:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        px = _pos_str(item[0])
        qty = _pos_str(item[1])
        if px is not None and qty is not None:
            out.append([px, qty])
    return out


def parse_ws_message(
    raw: str | bytes | dict[str, Any],
    *,
    now_mono_ms: int | None = None,
    now_epoch_ms: int | None = None,
) -> L1BookTick | L2DepthSnapshot | WsKlineBar | None:
    """Parse a Binance combined-stream or raw WebSocket message."""
    mono_ms = _now_mono_ms() if now_mono_ms is None else int(now_mono_ms)
    epoch_ms = _now_epoch_ms() if now_epoch_ms is None else int(now_epoch_ms)

    if isinstance(raw, (str, bytes)):
        try:
            payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
    elif isinstance(raw, dict):
        payload = raw
    else:
        return None

    if not isinstance(payload, dict):
        return None

    stream_name = str(payload.get("stream") or "").strip()
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return None

    stream_symbol = ""
    if "@" in stream_name:
        stream_symbol = stream_name.partition("@")[0].strip().upper()

    event_type = str(data.get("e") or "").strip()
    symbol = str(data.get("s") or stream_symbol or "").strip().upper()
    event_time = int(_finite_float(data.get("E")) or epoch_ms)

    # 1. Kline message (`e == "kline"` or `@kline_` in stream)
    if event_type == "kline" or "@kline_" in stream_name or isinstance(data.get("k"), dict):
        k = data.get("k")
        if not isinstance(k, dict):
            return None
        ksym = str(k.get("s") or symbol).strip().upper()
        interval = str(k.get("i") or "5m").strip()
        open_ms = _finite_float(k.get("t"))
        close_ms = _finite_float(k.get("T"))
        op = _finite_float(k.get("o"))
        hi = _finite_float(k.get("h"))
        lo = _finite_float(k.get("l"))
        cl = _finite_float(k.get("c"))
        vol = _finite_float(k.get("v"))
        qvol = _finite_float(k.get("q"))
        if (
            not ksym
            or open_ms is None
            or close_ms is None
            or None in (op, hi, lo, cl, vol, qvol)
            or min(op, hi, lo, cl) <= 0
        ):
            return None
        trades_raw = _finite_float(k.get("n"))
        trades = int(trades_raw) if trades_raw is not None and trades_raw >= 0 else None
        tbb = _finite_float(k.get("V"))
        tbq = _finite_float(k.get("Q"))
        return WsKlineBar(
            symbol=ksym,
            interval=interval,
            open_time_ms=int(open_ms),
            close_time_ms=int(close_ms),
            open=float(op),
            high=float(hi),
            low=float(lo),
            close=float(cl),
            volume=float(vol),
            quote_volume=float(qvol),
            trades=trades,
            taker_buy_base_volume=float(tbb) if tbb is not None and tbb >= 0 else None,
            taker_buy_quote_volume=float(tbq) if tbq is not None and tbq >= 0 else None,
            is_closed=bool(k.get("x") is True),
            event_time_ms=event_time,
            received_mono_ms=mono_ms,
        )

    # 2. L2 Partial Book Depth (`@depth` or `bids`/`asks` present)
    if (
        "@depth" in stream_name
        or event_type == "depthUpdate"
        or ("bids" in data and "asks" in data)
        or ("b" in data and "a" in data and isinstance(data.get("b"), list))
    ):
        if not symbol:
            return None
        bids = _clean_levels(data.get("bids") if "bids" in data else data.get("b"))
        asks = _clean_levels(data.get("asks") if "asks" in data else data.get("a"))
        if not bids or not asks:
            return None
        uid_raw = _finite_float(data.get("lastUpdateId", data.get("u")))
        return L2DepthSnapshot(
            symbol=symbol,
            bids=bids,
            asks=asks,
            last_update_id=int(uid_raw) if uid_raw is not None else None,
            event_time_ms=event_time,
            received_mono_ms=mono_ms,
            received_epoch_ms=epoch_ms,
        )

    # 3. L1 BookTicker (`b`, `B`, `a`, `A` strings)
    if (
        "@bookTicker" in stream_name
        or event_type == "bookTicker"
        or ("b" in data and "a" in data and not isinstance(data.get("b"), list))
    ):
        if not symbol:
            return None
        bid = _pos_str(data.get("b"))
        bid_qty = _pos_str(data.get("B")) or "0"
        ask = _pos_str(data.get("a"))
        ask_qty = _pos_str(data.get("A")) or "0"
        if bid is None or ask is None:
            return None
        bid_f = float(bid)
        ask_f = float(ask)
        if ask_f <= 0 or bid_f <= 0 or ask_f < bid_f:
            return None
        mid = f"{(bid_f + ask_f) / 2.0:.8f}".rstrip("0").rstrip(".")
        return L1BookTick(
            symbol=symbol,
            best_bid=bid,
            best_bid_qty=bid_qty,
            best_ask=ask,
            best_ask_qty=ask_qty,
            mid_price=mid,
            event_time_ms=event_time,
            received_mono_ms=mono_ms,
            received_epoch_ms=epoch_ms,
        )

    return None


KlineCloseCallback = Callable[[str, str, list[WsKlineBar]], None]


class BinanceMarketStreamCache:
    """Thread-safe sub-millisecond market data cache for L1, L2 depth, and Klines."""

    def __init__(
        self,
        *,
        max_age_ms: int | None = None,
        kline_max_bars: int = 500,
        snapshot_path: Path | str | None = None,
    ) -> None:
        self.max_age_ms = default_max_age_ms() if max_age_ms is None else int(max_age_ms)
        self.kline_max_bars = max(30, int(kline_max_bars))
        env_snap = os.environ.get(WS_SNAPSHOT_PATH_ENV, "").strip()
        self.snapshot_path = (
            Path(snapshot_path)
            if snapshot_path is not None
            else (Path(env_snap) if env_snap else None)
        )
        self._lock = threading.RLock()
        self._l1: dict[str, L1BookTick] = {}
        self._l2: dict[str, L2DepthSnapshot] = {}
        self._closed_klines: dict[tuple[str, str], deque[WsKlineBar]] = {}
        self._live_kline: dict[tuple[str, str], WsKlineBar] = {}
        self._kline_callbacks: list[KlineCloseCallback] = []
        self._stats: dict[str, int] = {
            "messages_ingested": 0,
            "l1_updates": 0,
            "l2_updates": 0,
            "kline_updates": 0,
            "kline_closed_events": 0,
            "l1_ws_hits": 0,
            "l1_ws_misses": 0,
            "l2_ws_hits": 0,
            "l2_ws_misses": 0,
        }

    def register_kline_close_callback(self, callback: KlineCloseCallback) -> None:
        with self._lock:
            self._kline_callbacks.append(callback)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._stats)

    def ingest_message(
        self,
        raw: str | bytes | dict[str, Any],
        *,
        now_mono_ms: int | None = None,
        now_epoch_ms: int | None = None,
    ) -> L1BookTick | L2DepthSnapshot | WsKlineBar | None:
        parsed = parse_ws_message(
            raw,
            now_mono_ms=now_mono_ms,
            now_epoch_ms=now_epoch_ms,
        )
        if parsed is None:
            return None
        callbacks_to_fire: list[tuple[KlineCloseCallback, str, str, list[WsKlineBar]]] = []
        with self._lock:
            self._stats["messages_ingested"] += 1
            if isinstance(parsed, L1BookTick):
                self._l1[parsed.symbol] = parsed
                self._stats["l1_updates"] += 1
            elif isinstance(parsed, L2DepthSnapshot):
                self._l2[parsed.symbol] = parsed
                self._stats["l2_updates"] += 1
                # Also synthesize L1 top-of-book if L1 is absent or older
                best_bid = parsed.bids[0][0]
                best_bid_qty = parsed.bids[0][1]
                best_ask = parsed.asks[0][0]
                best_ask_qty = parsed.asks[0][1]
                bid_f = float(best_bid)
                ask_f = float(best_ask)
                if ask_f >= bid_f > 0:
                    mid = f"{(bid_f + ask_f) / 2.0:.8f}".rstrip("0").rstrip(".")
                    prev_l1 = self._l1.get(parsed.symbol)
                    if prev_l1 is None or parsed.received_mono_ms >= prev_l1.received_mono_ms:
                        self._l1[parsed.symbol] = L1BookTick(
                            symbol=parsed.symbol,
                            best_bid=best_bid,
                            best_bid_qty=best_bid_qty,
                            best_ask=best_ask,
                            best_ask_qty=best_ask_qty,
                            mid_price=mid,
                            event_time_ms=parsed.event_time_ms,
                            received_mono_ms=parsed.received_mono_ms,
                            received_epoch_ms=parsed.received_epoch_ms,
                        )
            elif isinstance(parsed, WsKlineBar):
                self._stats["kline_updates"] += 1
                key = (parsed.symbol, parsed.interval)
                self._live_kline[key] = parsed
                if parsed.is_closed:
                    self._stats["kline_closed_events"] += 1
                    buf = self._closed_klines.setdefault(
                        key, deque(maxlen=self.kline_max_bars)
                    )
                    if buf and buf[-1].open_time_ms == parsed.open_time_ms:
                        buf[-1] = parsed
                    elif not buf or parsed.open_time_ms > buf[-1].open_time_ms:
                        buf.append(parsed)
                    else:
                        merged = {b.open_time_ms: b for b in buf}
                        merged[parsed.open_time_ms] = parsed
                        buf.clear()
                        for oms in sorted(merged)[-self.kline_max_bars :]:
                            buf.append(merged[oms])
                    snapshot_bars = list(buf)
                    for cb in self._kline_callbacks:
                        callbacks_to_fire.append((cb, parsed.symbol, parsed.interval, snapshot_bars))

        for cb, sym, tf, bars in callbacks_to_fire:
            try:
                cb(sym, tf, bars)
            except Exception as exc:  # noqa: BLE001
                log.warning("ws kline callback failed symbol=%s err=%s", sym, exc)
        return parsed

    def seed_klines(
        self,
        symbol: str,
        interval: str,
        bars: Sequence[Any],
        *,
        now_mono_ms: int | None = None,
    ) -> int:
        """Seed rolling closed-kline buffer from REST/SQLite SpotKline or tuples."""
        sym = str(symbol or "").strip().upper()
        tf = str(interval or "5m").strip()
        if not sym or not tf:
            return 0
        mono_ms = _now_mono_ms() if now_mono_ms is None else int(now_mono_ms)
        converted: dict[int, WsKlineBar] = {}
        for item in bars:
            if isinstance(item, WsKlineBar):
                converted[item.open_time_ms] = item
            elif hasattr(item, "open_time_ms") and hasattr(item, "close_time_ms"):
                converted[int(item.open_time_ms)] = WsKlineBar(
                    symbol=sym,
                    interval=tf,
                    open_time_ms=int(item.open_time_ms),
                    close_time_ms=int(item.close_time_ms),
                    open=float(item.open),
                    high=float(item.high),
                    low=float(item.low),
                    close=float(item.close),
                    volume=float(item.volume),
                    quote_volume=float(item.quote_volume),
                    trades=getattr(item, "trades", None),
                    taker_buy_base_volume=getattr(item, "taker_buy_base_volume", None),
                    taker_buy_quote_volume=getattr(item, "taker_buy_quote_volume", None),
                    is_closed=True,
                    event_time_ms=int(item.close_time_ms),
                    received_mono_ms=mono_ms,
                )
        if not converted:
            return 0
        with self._lock:
            key = (sym, tf)
            existing = self._closed_klines.get(key)
            if existing:
                for b in existing:
                    converted.setdefault(b.open_time_ms, b)
            buf = deque(
                (converted[k] for k in sorted(converted)[-self.kline_max_bars :]),
                maxlen=self.kline_max_bars,
            )
            self._closed_klines[key] = buf
            return len(buf)

    def get_ticker_price(
        self,
        symbol: str,
        *,
        max_age_ms: int | None = None,
        now_mono_ms: int | None = None,
    ) -> dict[str, Any] | None:
        """Return fresh L1 mid/mark price payload or None if missing/stale."""
        sym = str(symbol or "").strip().upper()
        limit_ms = self.max_age_ms if max_age_ms is None else int(max_age_ms)
        mono_ms = _now_mono_ms() if now_mono_ms is None else int(now_mono_ms)

        with self._lock:
            tick = self._l1.get(sym)
            if tick is not None:
                age_ms = max(0, mono_ms - tick.received_mono_ms)
                if age_ms <= limit_ms:
                    self._stats["l1_ws_hits"] += 1
                    return {
                        "symbol": sym,
                        "price": tick.mid_price,
                        "best_bid": tick.best_bid,
                        "best_ask": tick.best_ask,
                        "source": "ws_l1",
                        "ws_age_ms": age_ms,
                    }

        snap = self._read_shared_snapshot(sym, limit_ms=limit_ms)
        if snap is not None and isinstance(snap.get("l1"), dict):
            l1 = snap["l1"]
            with self._lock:
                self._stats["l1_ws_hits"] += 1
            return {
                "symbol": sym,
                "price": str(l1.get("price") or ""),
                "best_bid": str(l1.get("best_bid") or ""),
                "best_ask": str(l1.get("best_ask") or ""),
                "source": "ws_l1_shared",
                "ws_age_ms": int(snap.get("age_ms") or 0),
            }

        with self._lock:
            self._stats["l1_ws_misses"] += 1
        return None

    def get_order_book(
        self,
        symbol: str,
        *,
        max_age_ms: int | None = None,
        now_mono_ms: int | None = None,
    ) -> dict[str, Any] | None:
        """Return fresh L2 depth20 payload or None if missing/stale."""
        sym = str(symbol or "").strip().upper()
        limit_ms = self.max_age_ms if max_age_ms is None else int(max_age_ms)
        mono_ms = _now_mono_ms() if now_mono_ms is None else int(now_mono_ms)

        with self._lock:
            book = self._l2.get(sym)
            if book is not None:
                age_ms = max(0, mono_ms - book.received_mono_ms)
                if age_ms <= limit_ms:
                    self._stats["l2_ws_hits"] += 1
                    return {
                        "lastUpdateId": book.last_update_id,
                        "bids": [list(item) for item in book.bids],
                        "asks": [list(item) for item in book.asks],
                        "source": "ws_l2",
                        "ws_age_ms": age_ms,
                    }

        snap = self._read_shared_snapshot(sym, limit_ms=limit_ms)
        if snap is not None and isinstance(snap.get("l2"), dict):
            l2 = snap["l2"]
            with self._lock:
                self._stats["l2_ws_hits"] += 1
            return {
                "lastUpdateId": l2.get("lastUpdateId"),
                "bids": l2.get("bids") or [],
                "asks": l2.get("asks") or [],
                "source": "ws_l2_shared",
                "ws_age_ms": int(snap.get("age_ms") or 0),
            }

        with self._lock:
            self._stats["l2_ws_misses"] += 1
        return None

    def get_closed_klines(
        self,
        symbol: str,
        interval: str = "5m",
        *,
        min_bars: int = 20,
        max_age_ms: int = DEFAULT_KLINE_MAX_AGE_MS,
        now_mono_ms: int | None = None,
    ) -> list[WsKlineBar] | None:
        """Return closed klines from memory when fresh and at least ``min_bars`` long."""
        sym = str(symbol or "").strip().upper()
        tf = str(interval or "5m").strip()
        mono_ms = _now_mono_ms() if now_mono_ms is None else int(now_mono_ms)
        with self._lock:
            buf = self._closed_klines.get((sym, tf))
            if not buf or len(buf) < int(min_bars):
                return None
            last_bar = buf[-1]
            if max_age_ms > 0 and (mono_ms - last_bar.received_mono_ms) > int(max_age_ms):
                return None
            return list(buf)

    def dump_shared_snapshot(self, path: Path | None = None) -> bool:
        """Atomically persist L1/L2 state for cross-process low-latency reads."""
        target = path or self.snapshot_path
        if target is None:
            return False
        now_epoch = _now_epoch_ms()
        with self._lock:
            symbols_payload: dict[str, Any] = {}
            all_syms = set(self._l1) | set(self._l2)
            for sym in sorted(all_syms):
                entry: dict[str, Any] = {}
                t = self._l1.get(sym)
                if t is not None:
                    entry["l1"] = {
                        "price": t.mid_price,
                        "best_bid": t.best_bid,
                        "best_ask": t.best_ask,
                        "received_epoch_ms": t.received_epoch_ms,
                    }
                b = self._l2.get(sym)
                if b is not None:
                    entry["l2"] = {
                        "lastUpdateId": b.last_update_id,
                        "bids": b.bids,
                        "asks": b.asks,
                        "received_epoch_ms": b.received_epoch_ms,
                    }
                symbols_payload[sym] = entry
        doc = {
            "written_epoch_ms": now_epoch,
            "symbols": symbols_payload,
        }
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(json.dumps(doc), encoding="utf-8")
            os.replace(tmp, target)
            return True
        except OSError as exc:
            log.warning("ws snapshot write failed path=%s err=%s", target, exc)
            return False

    def _read_shared_snapshot(
        self,
        symbol: str,
        *,
        limit_ms: int,
    ) -> dict[str, Any] | None:
        target = self.snapshot_path
        if target is None or not target.is_file():
            return None
        try:
            doc = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(doc, dict):
            return None
        symbols = doc.get("symbols")
        if not isinstance(symbols, dict):
            return None
        row = symbols.get(symbol)
        if not isinstance(row, dict):
            return None
        now_epoch = _now_epoch_ms()
        l2 = row.get("l2") if isinstance(row.get("l2"), dict) else None
        l1 = row.get("l1") if isinstance(row.get("l1"), dict) else None
        ts_candidates = [
            int(item["received_epoch_ms"])
            for item in (l2, l1)
            if isinstance(item, dict) and isinstance(item.get("received_epoch_ms"), int)
        ]
        if not ts_candidates:
            written = doc.get("written_epoch_ms")
            if isinstance(written, int):
                ts_candidates.append(written)
        if not ts_candidates:
            return None
        age_ms = max(0, now_epoch - max(ts_candidates))
        if age_ms > limit_ms:
            return None
        return {"l1": l1, "l2": l2, "age_ms": age_ms}


_GLOBAL_CACHE_LOCK = threading.Lock()
_GLOBAL_CACHE: BinanceMarketStreamCache | None = None


def get_global_ws_cache() -> BinanceMarketStreamCache:
    """Process-wide singleton cache shared by stream daemons, indicators, and adapters."""
    global _GLOBAL_CACHE
    with _GLOBAL_CACHE_LOCK:
        if _GLOBAL_CACHE is None:
            _GLOBAL_CACHE = BinanceMarketStreamCache()
        return _GLOBAL_CACHE


def set_global_ws_cache(cache: BinanceMarketStreamCache | None) -> None:
    global _GLOBAL_CACHE
    with _GLOBAL_CACHE_LOCK:
        _GLOBAL_CACHE = cache


class BinanceSpotWsDaemon:
    """Background multiplexed WebSocket daemon with auto-reconnect and snapshot flush."""

    def __init__(
        self,
        symbols: Sequence[str],
        *,
        cache: BinanceMarketStreamCache | None = None,
        base_url: str | None = None,
        book_ticker: bool = True,
        depth20: bool = True,
        kline_interval: str | None = "5m",
        ping_interval_sec: float = 20.0,
        reconnect_max_sec: float = 30.0,
        snapshot_flush_interval_sec: float = 0.25,
        stop_flag: Callable[[], bool] | None = None,
    ) -> None:
        self.symbols = [str(s).strip().upper() for s in symbols if str(s).strip()]
        self.cache = cache or get_global_ws_cache()
        self.base_url = base_url
        self.book_ticker = book_ticker
        self.depth20 = depth20
        self.kline_interval = kline_interval
        self.ping_interval_sec = float(ping_interval_sec)
        self.reconnect_max_sec = float(reconnect_max_sec)
        self.snapshot_flush_interval_sec = float(snapshot_flush_interval_sec)
        self.stop_flag = stop_flag
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.reconnect_count = 0

    def urls(self) -> list[str]:
        return build_combined_stream_urls(
            self.symbols,
            base_url=self.base_url,
            book_ticker=self.book_ticker,
            depth20=self.depth20,
            kline_interval=self.kline_interval,
        )

    def start_background(self) -> None:
        if not self.symbols:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._thread_main,
            name="binance-spot-ws",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "binance spot ws daemon started symbols=%s shards=%s",
            len(self.symbols),
            len(self.urls()),
        )

    def stop(self) -> None:
        self._stop.set()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(lambda: None)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def _should_stop(self) -> bool:
        if self._stop.is_set():
            return True
        if self.stop_flag and self.stop_flag():
            return True
        return False

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run_all_shards())
        finally:
            self._loop.close()

    async def _run_all_shards(self) -> None:
        shard_urls = self.urls()
        if not shard_urls:
            return
        tasks = [asyncio.create_task(self._run_shard(url)) for url in shard_urls]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()

    async def _run_shard(self, url: str) -> None:
        try:
            import websockets
        except ImportError:
            log.warning("websockets package not installed; BinanceSpotWsDaemon staying on REST fallback")
            return

        backoff = 1.0
        last_flush = 0.0
        while not self._should_stop():
            try:
                async with websockets.connect(
                    url,
                    ping_interval=self.ping_interval_sec,
                    ping_timeout=max(20.0, self.ping_interval_sec * 2),
                    max_size=4 * 1024 * 1024,
                ) as ws:
                    backoff = 1.0
                    while not self._should_stop():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=self.ping_interval_sec + 10.0)
                        except asyncio.TimeoutError:
                            pong = await ws.ping()
                            await asyncio.wait_for(pong, timeout=10.0)
                            continue
                        self.cache.ingest_message(raw)
                        now_m = time.monotonic()
                        if (
                            self.cache.snapshot_path is not None
                            and now_m - last_flush >= self.snapshot_flush_interval_sec
                        ):
                            self.cache.dump_shared_snapshot()
                            last_flush = now_m
            except Exception as exc:  # noqa: BLE001
                if self._should_stop():
                    break
                self.reconnect_count += 1
                log.warning(
                    "binance ws shard reconnect=%s err=%s",
                    self.reconnect_count,
                    type(exc).__name__,
                )
                await asyncio.sleep(min(backoff, self.reconnect_max_sec))
                backoff = min(backoff * 2.0, self.reconnect_max_sec)
