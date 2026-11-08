"""Pre-accept BUY guards for the decision tick.

Stop distance applies to every purpose that puts a ``stop_price`` on a BUY.
The protection halt and optional LSEG macro circuit breaker apply to the live
Binance spot purposes. Sells are kept.
"""

from __future__ import annotations

from typing import Any

from logging_setup import get_logger

from decision_engine.core.portfolio_clusters import cluster_open_exposure_block
from decision_engine.core.protection_halt import ProtectionBook, purpose_halt_reason
from decision_engine.core.signal_bus import (
    PURPOSE_LIVE_BNB_BOLL_CLASSIC,
    PURPOSE_LIVE_BNB_BOLL_FADE,
)
from decision_engine.core.stop_gap import buy_stop_gap_reason
from execution.executors.binance_spot_gate import buy_exposure_block

log = get_logger("decision_engine.live_buy_guards")

LIVE_BNB_SPOT_PURPOSES = frozenset(
    {
        PURPOSE_LIVE_BNB_BOLL_FADE,
        PURPOSE_LIVE_BNB_BOLL_CLASSIC,
    }
)


def signal_symbol(signal: dict[str, Any]) -> str | None:
    for key in ("binance_symbol", "symbol"):
        raw = signal.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    token = signal.get("token_address")
    if isinstance(token, str) and token.strip().upper().endswith("USDT"):
        return token.strip()
    return None


def apply_live_buy_guards(
    signals: list[dict[str, Any]],
    *,
    purpose: str,
    book: ProtectionBook,
    macro_halt: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Drop BUYs that fail the stop gap, protection halt, cluster cap, or macro circuit breaker.

    ``protection_halt`` is set only for a purpose-wide block (unprotected
    inventory, an unreadable book, or one-open-per-arm when that flag is
    off). A healthy open that blocks a second BUY of the same symbol is
    counted in ``protection_dropped`` and does not halt the other symbols.
    ``macro_halt`` blocks new BUYs during configured LSEG macro/cross-asset
    blackouts while keeping SELL signals intact.
    """
    wide = purpose_halt_reason(purpose, book) if purpose in LIVE_BNB_SPOT_PURPOSES else None
    per_symbol = (
        purpose in LIVE_BNB_SPOT_PURPOSES and book.configured and book.error is None and wide is None
    )
    kept: list[dict[str, Any]] = []
    stop_rejected = 0
    protection_dropped = 0
    cluster_dropped = 0
    macro_dropped = 0
    for signal in signals:
        if str(signal.get("action") or "").lower() != "buy":
            kept.append(signal)
            continue
        gap = buy_stop_gap_reason(signal)
        if gap is not None:
            stop_rejected += 1
            log.info(
                "stop_gap_reject purpose=%s symbol=%s reason=%s order_type=%s "
                "price=%s limit=%s stop=%s",
                purpose,
                signal_symbol(signal),
                gap,
                signal.get("order_type") or "MARKET",
                signal.get("price_usd", signal.get("price")),
                signal.get("limit_price"),
                signal.get("stop_price"),
            )
            continue
        if wide is not None:
            protection_dropped += 1
            log.info(
                "protection_halt purpose=%s symbol=%s reason=%s",
                purpose,
                signal_symbol(signal),
                wide,
            )
            continue
        if per_symbol:
            reason = buy_exposure_block(
                list(book.positions),
                list(book.canonical),
                purpose=purpose,
                symbol=signal_symbol(signal),
            )
            if reason is not None:
                protection_dropped += 1
                log.info(
                    "protection_halt purpose=%s symbol=%s reason=%s",
                    purpose,
                    signal_symbol(signal),
                    reason,
                )
                continue
            cluster_reason = cluster_open_exposure_block(
                list(book.positions),
                purpose=purpose,
                symbol=signal_symbol(signal),
            )
            if cluster_reason is not None:
                cluster_dropped += 1
                log.info(
                    "cluster_exposure_halt purpose=%s symbol=%s reason=%s",
                    purpose,
                    signal_symbol(signal),
                    cluster_reason,
                )
                continue
        if macro_halt is not None:
            macro_dropped += 1
            log.info(
                "macro_halt purpose=%s symbol=%s reason=%s",
                purpose,
                signal_symbol(signal),
                macro_halt,
            )
            continue
        kept.append(signal)
    if wide is not None and protection_dropped:
        log.warning(
            "protection_halt purpose=%s reason=%s dropped_buys=%s",
            purpose,
            wide,
            protection_dropped,
        )
    if macro_halt is not None and macro_dropped:
        log.warning(
            "macro_halt purpose=%s reason=%s dropped_buys=%s",
            purpose,
            macro_halt,
            macro_dropped,
        )
    report: dict[str, Any] = {
        "stop_gap_rejected": stop_rejected,
        "protection_halt": wide,
        "protection_dropped": protection_dropped,
    }
    if cluster_dropped:
        report["cluster_dropped"] = cluster_dropped
    if macro_halt is not None or macro_dropped:
        report["macro_halt"] = macro_halt
        report["macro_dropped"] = macro_dropped
    return kept, report
