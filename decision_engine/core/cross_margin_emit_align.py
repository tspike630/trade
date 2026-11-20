"""Align cross-margin emission to a just-landed 5m close, without relaxing gates.

The maintenance 30bps door stays abs(reference_price - P) / P > 0.003.
This module only (1) waits until a new 5m close_time_ms is stored,
(2) emits each (symbol, close_ms) at most once, and (3) applies the same
reference-vs-P early screen so a already-drifted bar is not published.
limit_price is never compared to P. Stops are not re-anchored to the
live reference.

t494 scan orchestration (does not relax not_target_bar or 30bps):
a symbol is ready only when the 5m kline close_time_ms and the
binance_indicators kline_close_time_ms both equal the target bar. The
first shot fires when the dual-ready share of the active pool reaches
READY_FRACTION, or FIRST_SHOT_WINDOW_SEC after the first kline of that
bar appears. Trailing micro-scans then pick up late symbols inside
TRAIL_WINDOW_SEC after the close, polling every TRAIL_POLL_SEC. Each
(symbol, target_bar) is evaluated at most once. Unready symbols are
held and are not routed on a stale indicator row. A single laggard
does not block the ready set.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from logging_setup import get_logger

log = get_logger("cross_margin_emit_align")

KLINE_INTERVAL = "5m"
KLINE_MS = 5 * 60 * 1000
MAX_DRIFT = Decimal("0.003")
PUBLIC_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
POLL_SEC = 0.5
SETTLE_SEC = 2.0
RECENT_OPEN_LOOKBACK_MS = 20 * 60 * 1000
# t494 dual-table first shot + trailing rescan. These are fixed values
# inside the t493 ranges. They do not relax not_target_bar or 30bps.
READY_FRACTION = 0.80
FIRST_SHOT_WINDOW_SEC = 8.0
TRAIL_WINDOW_SEC = 60.0
TRAIL_POLL_SEC = 3.0


@dataclass
class CrossMarginEmitState:
    """Process-local align memory. Disabled until the live infinite loop arms it."""

    armed: bool = False
    last_seen_close_ms: int | None = None
    target_close_ms: int | None = None
    emitted_close: set[tuple[str, int]] = field(default_factory=set)
    evaluated_close: set[tuple[str, int]] = field(default_factory=set)
    scan_symbols: set[str] | None = None
    first_appear_ms: int | None = None
    first_shot_done: bool = False
    bar_cycle_done: bool = False


def closed_bar_reference_drift_reason(
    closed_bar_close: object, reference_price: object
) -> str | None:
    """Same comparison as execution.margin_cross.entry._closed_bar_reference_drift.

    Inclusive 30bps: greater than 0.003 rejects; equal to 0.003 passes.
    Does not read limit_price.
    """
    if closed_bar_close in (None, "") or reference_price in (None, ""):
        return "closed_bar_reference_missing"
    try:
        closed = Decimal(str(closed_bar_close))
        reference = Decimal(str(reference_price))
    except Exception:
        return "closed_bar_reference_invalid"
    if closed <= 0 or reference <= 0:
        return "closed_bar_reference_invalid"
    if abs(reference - closed) / closed > MAX_DRIFT:
        return "closed_bar_reference_drift"
    return None



def _dev_bps(closed_bar_close: object, reference_price: object) -> Decimal | None:
    """Signed deviation in bps relative to P. None when either side is unusable."""
    if closed_bar_close in (None, "") or reference_price in (None, ""):
        return None
    try:
        closed = Decimal(str(closed_bar_close))
        reference = Decimal(str(reference_price))
    except Exception:
        return None
    if closed <= 0 or reference <= 0:
        return None
    return (reference - closed) / closed * Decimal("10000")


def _signal_closed_bar_px(sig: Mapping[str, Any]) -> object:
    closed = sig.get("closed_bar_close")
    if closed not in (None, ""):
        return closed
    market = sig.get("market_row") if isinstance(sig.get("market_row"), Mapping) else {}
    return market.get("price_usd") or market.get("close") or market.get("price")


def _log_emit_drop(
    *,
    reason: str,
    symbol: str,
    bar_close_ms: object,
    closed: object = None,
    live_px: object = None,
    dev_bps: object = None,
) -> None:
    log.info(
        "cross_margin_emit_align drop reason=%s symbol=%s bar_close_ms=%s P=%s live_px=%s dev_bps=%s",
        reason,
        symbol or "-",
        bar_close_ms,
        closed,
        live_px,
        dev_bps,
    )


def _as_close_ms(value: object) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def signal_symbol(sig: Mapping[str, Any]) -> str:
    market = sig.get("market_row") if isinstance(sig.get("market_row"), Mapping) else {}
    return str(
        sig.get("binance_symbol")
        or sig.get("symbol")
        or sig.get("token_address")
        or market.get("binance_symbol")
        or ""
    ).strip().upper()


def signal_5m_close_ms(sig: Mapping[str, Any]) -> int | None:
    market = sig.get("market_row") if isinstance(sig.get("market_row"), Mapping) else {}
    for source in (sig, market):
        for key in ("entry_kline_5m_close_ms", "kline_close_time_ms"):
            close_ms = _as_close_ms(source.get(key))
            if close_ms is not None:
                return close_ms
    return None


def latest_5m_close_watermark(
    conn: Any,
    *,
    now_ms: int | None = None,
    lookback_ms: int = RECENT_OPEN_LOOKBACK_MS,
) -> tuple[int | None, int]:
    """Return (max close_time_ms, symbol count at that close) for recent 5m bars."""
    clock = int(time.time() * 1000) if now_ms is None else int(now_ms)
    floor_open = max(0, clock - int(lookback_ms))
    row = conn.execute(
        """
        SELECT MAX(close_time_ms) AS mx
        FROM binance_klines
        WHERE interval = ? AND open_time_ms >= ?
        """,
        (KLINE_INTERVAL, floor_open),
    ).fetchone()
    if row is None:
        return None, 0
    mx = row[0] if not hasattr(row, "keys") else row["mx"]
    close_ms = _as_close_ms(mx)
    if close_ms is None:
        return None, 0
    count_row = conn.execute(
        """
        SELECT COUNT(DISTINCT symbol) AS n
        FROM binance_klines
        WHERE interval = ? AND close_time_ms = ?
        """,
        (KLINE_INTERVAL, close_ms),
    ).fetchone()
    count = int(count_row[0] if not hasattr(count_row, "keys") else count_row["n"])
    return close_ms, count


def wait_for_new_5m_close(
    conn: Any,
    state: CrossMarginEmitState,
    *,
    now_ms_fn: Callable[[], int] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    poll_sec: float = POLL_SEC,
    settle_sec: float = SETTLE_SEC,
) -> int:
    """Block until a close_time_ms newer than last_seen is stored, then settle briefly.

    On the first call, the current watermark is recorded and not fired, so a
    mid-bar restart does not republish the already-closed bar.
    """
    clock_fn = now_ms_fn or (lambda: int(time.time() * 1000))
    if state.last_seen_close_ms is None:
        current, _n = latest_5m_close_watermark(conn, now_ms=clock_fn())
        if current is not None:
            state.last_seen_close_ms = current
            log.info(
                "5m emit align armed last_seen_close_ms=%s (wait for the next landed bar)",
                current,
            )
    while True:
        current, _n = latest_5m_close_watermark(conn, now_ms=clock_fn())
        if current is not None and (
            state.last_seen_close_ms is None or current > state.last_seen_close_ms
        ):
            first_seen = current
            if settle_sec > 0:
                sleep_fn(float(settle_sec))
                settled, n = latest_5m_close_watermark(conn, now_ms=clock_fn())
                if settled is not None and settled > first_seen:
                    first_seen = settled
                log.info(
                    "5m close landed close_ms=%s symbols=%s after settle_sec=%s",
                    first_seen,
                    n,
                    settle_sec,
                )
            else:
                log.info("5m close landed close_ms=%s", first_seen)
            state.last_seen_close_ms = first_seen
            state.target_close_ms = first_seen
            return first_seen
        sleep_fn(float(poll_sec))


def fetch_public_ticker_prices(
    symbols: Iterable[str],
    *,
    timeout_sec: float = 2.0,
    urlopen_fn: Callable[..., Any] = urlopen,
    base_url: str = PUBLIC_TICKER_URL,
) -> dict[str, str]:
    """Unsigned GET /api/v3/ticker/price. The same last-price used as reference_price."""
    wanted = sorted({str(s).strip().upper() for s in symbols if str(s).strip()})
    if not wanted:
        return {}
    params = {"symbols": json.dumps(wanted, separators=(",", ":"))}
    request = Request(
        f"{base_url}?{urlencode(params)}",
        headers={"User-Agent": "cross-margin-emit-align/1.0"},
        method="GET",
    )
    try:
        with urlopen_fn(request, timeout=timeout_sec) as resp:
            raw = resp.read()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        log.warning("public ticker fetch failed: %s", exc)
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("public ticker decode failed: %s", exc)
        return {}
    rows = payload if isinstance(payload, list) else [payload]
    out: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        price = row.get("price")
        if symbol and price not in (None, ""):
            out[symbol] = str(price)
    return out


def apply_cross_margin_emit_filters(
    signals: list[dict[str, Any]],
    state: CrossMarginEmitState,
    *,
    fetch_prices: Callable[[Iterable[str]], Mapping[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Drop already-emitted bars, off-target bars, and emit-time 30bps drift.

    A missing live reference does not invent a reject (maintenance still gates).
    abs(limit_price - P) / P is never used.
    """
    if not state.armed or not signals:
        return signals
    kept: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    seen_batch: set[tuple[str, int]] = set()
    drop_missing_close_ms = 0
    drop_not_target_bar = 0
    drop_already_emitted = 0
    drop_dev_gt_30bps = 0
    live_px_unavailable = 0
    for sig in signals:
        symbol = signal_symbol(sig)
        close_ms = signal_5m_close_ms(sig)
        closed = _signal_closed_bar_px(sig)
        if not symbol or close_ms is None:
            drop_missing_close_ms += 1
            _log_emit_drop(
                reason="missing_close_ms",
                symbol=symbol,
                bar_close_ms=close_ms,
                closed=closed,
            )
            continue
        if state.target_close_ms is not None and close_ms != state.target_close_ms:
            drop_not_target_bar += 1
            _log_emit_drop(
                reason="not_target_bar",
                symbol=symbol,
                bar_close_ms=close_ms,
                closed=closed,
            )
            continue
        key = (symbol, close_ms)
        if key in state.emitted_close or key in seen_batch:
            drop_already_emitted += 1
            _log_emit_drop(
                reason="already_emitted",
                symbol=symbol,
                bar_close_ms=close_ms,
                closed=closed,
            )
            continue
        seen_batch.add(key)
        candidates.append(sig)

    needed = {signal_symbol(sig) for sig in candidates}
    prices: Mapping[str, str] = {}
    getter = fetch_prices if fetch_prices is not None else fetch_public_ticker_prices
    if needed:
        try:
            prices = getter(sorted(needed)) or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("emit-time reference fetch failed: %s", exc)
            prices = {}

    for sig in candidates:
        symbol = signal_symbol(sig)
        close_ms = signal_5m_close_ms(sig)
        assert close_ms is not None
        closed = _signal_closed_bar_px(sig)
        reference = prices.get(symbol)
        if reference in (None, ""):
            live_px_unavailable += 1
        else:
            reason = closed_bar_reference_drift_reason(closed, reference)
            if reason == "closed_bar_reference_drift":
                drop_dev_gt_30bps += 1
                _log_emit_drop(
                    reason="dev_gt_30bps",
                    symbol=symbol,
                    bar_close_ms=close_ms,
                    closed=closed,
                    live_px=reference,
                    dev_bps=_dev_bps(closed, reference),
                )
                continue
        kept.append(sig)
        state.emitted_close.add((symbol, close_ms))
    log.info(
        "cross_margin_emit_align candidates=%s emitted=%s drop_missing_close_ms=%s "
        "drop_not_target_bar=%s drop_dev_gt_30bps=%s live_px_unavailable=%s drop_already_emitted=%s",
        len(signals),
        len(kept),
        drop_missing_close_ms,
        drop_not_target_bar,
        drop_dev_gt_30bps,
        live_px_unavailable,
        drop_already_emitted,
    )
    return kept


def _norm_symbol(value: object) -> str:
    return str(value or "").strip().upper()


def _row_symbol(row: Mapping[str, Any]) -> str:
    return _norm_symbol(
        row.get("binance_symbol") or row.get("token_address") or row.get("symbol")
    )


def _indicators_table_ready(conn: Any) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'binance_indicators'"
        ).fetchone()
    except Exception:
        return False
    return row is not None


def active_pool_symbols(conn: Any, target_close_ms: int) -> list[str]:
    """Symbols expected on this bar: the previous 5m kline set.

    The denominator is the active pool, not the symbols that have already
    landed on the new bar. A missing previous bar falls back to the most
    recent earlier close, then to distinct kline symbols in the lookback.
    """
    target = int(target_close_ms)
    prev = target - KLINE_MS
    rows = conn.execute(
        """
        SELECT DISTINCT symbol
        FROM binance_klines
        WHERE interval = ? AND close_time_ms = ?
        """,
        (KLINE_INTERVAL, prev),
    ).fetchall()
    if not rows:
        prior = conn.execute(
            """
            SELECT MAX(close_time_ms) AS mx
            FROM binance_klines
            WHERE interval = ? AND close_time_ms < ?
            """,
            (KLINE_INTERVAL, target),
        ).fetchone()
        prior_ms = _as_close_ms(
            prior[0] if prior is not None and not hasattr(prior, "keys") else (prior["mx"] if prior is not None else None)
        )
        if prior_ms is not None:
            rows = conn.execute(
                """
                SELECT DISTINCT symbol
                FROM binance_klines
                WHERE interval = ? AND close_time_ms = ?
                """,
                (KLINE_INTERVAL, prior_ms),
            ).fetchall()
    if not rows:
        rows = conn.execute(
            """
            SELECT DISTINCT symbol
            FROM binance_klines
            WHERE interval = ?
            """,
            (KLINE_INTERVAL,),
        ).fetchall()
    out = []
    seen: set[str] = set()
    for row in rows:
        symbol = _norm_symbol(row[0] if not hasattr(row, "keys") else row["symbol"])
        if symbol and symbol not in seen:
            seen.add(symbol)
            out.append(symbol)
    return out


def dual_ready_symbols(conn: Any, target_close_ms: int) -> list[str]:
    """Symbols whose 5m kline and indicator bar clocks both equal target_close_ms."""
    target = int(target_close_ms)
    if not _indicators_table_ready(conn):
        return []
    rows = conn.execute(
        """
        SELECT DISTINCT k.symbol
        FROM binance_klines k
        INNER JOIN binance_indicators i ON i.symbol = k.symbol
        WHERE k.interval = ?
          AND k.close_time_ms = ?
          AND i.kline_close_time_ms = ?
        """,
        (KLINE_INTERVAL, target, target),
    ).fetchall()
    out = []
    seen: set[str] = set()
    for row in rows:
        symbol = _norm_symbol(row[0] if not hasattr(row, "keys") else row["symbol"])
        if symbol and symbol not in seen:
            seen.add(symbol)
            out.append(symbol)
    return out


def pending_ready_symbols(
    conn: Any, state: CrossMarginEmitState, target_close_ms: int
) -> list[str]:
    target = int(target_close_ms)
    pending = []
    for symbol in dual_ready_symbols(conn, target):
        if (symbol, target) not in state.evaluated_close:
            pending.append(symbol)
    return pending


def ready_fraction_reached(ready_count: int, pool_size: int, fraction: float = READY_FRACTION) -> bool:
    if pool_size <= 0 or ready_count <= 0:
        return False
    return ready_count >= fraction * pool_size


def trail_deadline_ms(target_close_ms: int, trail_window_sec: float = TRAIL_WINDOW_SEC) -> int:
    return int(target_close_ms) + int(float(trail_window_sec) * 1000)


def mark_evaluated(state: CrossMarginEmitState, symbols: Iterable[str], target_close_ms: int) -> None:
    target = int(target_close_ms)
    for symbol in symbols:
        text = _norm_symbol(symbol)
        if text:
            state.evaluated_close.add((text, target))


def filter_hot_to_pending_ready(
    rows: list[dict[str, Any]],
    state: CrossMarginEmitState,
) -> list[dict[str, Any]]:
    """Drop unreadies and stale indicator rows before routing.

    Only applies when the live align loop has a pending scan batch.
    A row whose kline_close_time_ms is not the target bar is not routed.
    """
    if not state.armed or not state.scan_symbols or state.target_close_ms is None:
        return rows
    allowed = {_norm_symbol(s) for s in state.scan_symbols if _norm_symbol(s)}
    target = int(state.target_close_ms)
    kept: list[dict[str, Any]] = []
    for row in rows:
        symbol = _row_symbol(row)
        if symbol not in allowed:
            continue
        close_ms = _as_close_ms(row.get("kline_close_time_ms"))
        if close_ms != target:
            log.info(
                "cross_margin_ready_skip stale_indicator symbol=%s bar_close_ms=%s target=%s",
                symbol or "-",
                close_ms,
                target,
            )
            continue
        kept.append(row)
    # The whole batch was given this bar's one look, including names that
    # turnover or the bar check removed. Leaving them pending blocks the
    # next bar. This does not mark a symbol that was never in the batch.
    mark_evaluated(state, allowed, target)
    state.scan_symbols = None
    return kept


def _begin_bar_cycle(
    conn: Any,
    state: CrossMarginEmitState,
    *,
    clock_fn: Callable[[], int],
    sleep_fn: Callable[[float], None],
    poll_sec: float,
) -> int:
    target = wait_for_new_5m_close(
        conn,
        state,
        now_ms_fn=clock_fn,
        sleep_fn=sleep_fn,
        poll_sec=poll_sec,
        settle_sec=0.0,
    )
    state.first_appear_ms = clock_fn()
    state.first_shot_done = False
    state.bar_cycle_done = False
    state.scan_symbols = None
    log.info(
        "5m close first appeared close_ms=%s first_appear_ms=%s",
        target,
        state.first_appear_ms,
    )
    return target


def _close_expired_bar(
    state: CrossMarginEmitState,
    target: int,
    pending: list[str],
    deadline: int,
) -> None:
    """Drop an expired target without returning it. Pending names are consumed.

    A symbol still dual-ready after the trail window is not scanned late.
    Marking it evaluated keeps the next bar from waiting on this close.
    """
    if pending:
        mark_evaluated(state, pending, target)
    log.info(
        "5m trail window closed close_ms=%s pending_skipped=%s evaluated=%s deadline_ms=%s",
        target,
        len(pending),
        sum(1 for _symbol, close_ms in state.evaluated_close if close_ms == target),
        deadline,
    )
    state.bar_cycle_done = True
    state.scan_symbols = None


def next_cross_margin_scan_batch(
    conn: Any,
    state: CrossMarginEmitState,
    *,
    now_ms_fn: Callable[[], int] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    poll_sec: float = POLL_SEC,
    first_shot_window_sec: float = FIRST_SHOT_WINDOW_SEC,
    trail_window_sec: float = TRAIL_WINDOW_SEC,
    trail_poll_sec: float = TRAIL_POLL_SEC,
    ready_fraction: float = READY_FRACTION,
) -> list[str]:
    """Block until the next ready subset should be scanned.

    Returns a non-empty list of symbols that are dual-table ready for the
    current target bar and have not been evaluated on that bar. A first
    shot fires at the ready-fraction threshold or the short window.
    Trailing micro-scans then return newly ready leftovers until the
    trail deadline. One laggard never delays the ready set. The caller
    must evaluate each returned symbol at most once.
    """
    clock_fn = now_ms_fn or (lambda: int(time.time() * 1000))
    while True:
        if (
            state.target_close_ms is None
            or state.last_seen_close_ms is None
            or state.bar_cycle_done
        ):
            _begin_bar_cycle(
                conn,
                state,
                clock_fn=clock_fn,
                sleep_fn=sleep_fn,
                poll_sec=poll_sec,
            )
        target = int(state.target_close_ms or 0)
        now = clock_fn()
        pending = pending_ready_symbols(conn, state, target)
        deadline = trail_deadline_ms(target, trail_window_sec)
        if not state.first_shot_done:
            pool = active_pool_symbols(conn, target)
            ready_n = len(dual_ready_symbols(conn, target))
            frac = (ready_n / len(pool)) if pool else 0.0
            appeared = int(state.first_appear_ms or now)
            elapsed_ms = now - appeared
            window_ms = int(float(first_shot_window_sec) * 1000)
            due = ready_fraction_reached(ready_n, len(pool), ready_fraction) or elapsed_ms >= window_ms or now >= deadline
            if now >= deadline:
                _close_expired_bar(state, target, pending, deadline)
                continue
            if not due:
                sleep_fn(float(poll_sec))
                continue
            state.first_shot_done = True
            log.info(
                "5m first shot close_ms=%s ready=%s pool=%s frac=%.4f elapsed_ms=%s pending=%s",
                target,
                ready_n,
                len(pool),
                frac,
                elapsed_ms,
                len(pending),
            )
            if pending:
                return pending
        if now >= deadline:
            _close_expired_bar(state, target, pending, deadline)
            continue
        if pending:
            log.info(
                "5m trail scan close_ms=%s pending=%s deadline_ms=%s",
                target,
                len(pending),
                deadline,
            )
            return pending
        sleep_fn(float(trail_poll_sec))
