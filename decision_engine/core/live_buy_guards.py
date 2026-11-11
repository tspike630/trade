"""Pre-accept BUY guards for the decision tick.

Stop distance applies to every purpose that puts a ``stop_price`` on a BUY.
The protection halt and optional LSEG macro circuit breaker apply to the live
Binance spot purposes. Sells are kept.
"""

from __future__ import annotations

from typing import Any

from logging_setup import get_logger

from decision_engine.core.drawdown_and_kelly_budget import (
    PodRiskBudgetResult,
    drawdown_kelly_gate_enabled,
    evaluate_pod_drawdown_and_kelly,
)
from decision_engine.core.perp_microstructure_and_beta_hedge import (
    compute_portfolio_beta_and_hedge,
    portfolio_beta_exposure_block,
    portfolio_beta_gate_enabled,
)
from decision_engine.core.portfolio_clusters import cluster_open_exposure_block
from decision_engine.core.protection_halt import ProtectionBook, purpose_halt_reason
from decision_engine.core.signal_bus import (
    PURPOSE_LIVE_BNB_BOLL_CLASSIC,
    PURPOSE_LIVE_BNB_BOLL_FADE,
)
from decision_engine.core.stop_gap import buy_stop_gap_reason
from decision_engine.strategies.live_bnb_size_floor import lift_size_pct_for_entry_min
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
    pod_risk_gate: bool | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Drop BUYs that fail the stop gap, protection halt, cluster cap, drawdown/Kelly gate, or macro circuit breaker.

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
    use_pod_gate = drawdown_kelly_gate_enabled() if pod_risk_gate is None else bool(pod_risk_gate)
    pod_budget: PodRiskBudgetResult | None = None
    if per_symbol and use_pod_gate:
        pod_budget = evaluate_pod_drawdown_and_kelly(
            list(book.positions),
            purpose=purpose,
        )

    kept: list[dict[str, Any]] = []
    stop_rejected = 0
    protection_dropped = 0
    cluster_dropped = 0
    beta_dropped = 0
    drawdown_dropped = 0
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
        if pod_budget is not None and not pod_budget.allow_buy:
            drawdown_dropped += 1
            log.info(
                "pod_drawdown_halt purpose=%s symbol=%s reason=%s dd=%.4f streak=%s",
                purpose,
                signal_symbol(signal),
                pod_budget.halt_reason,
                pod_budget.current_drawdown_pct,
                pod_budget.consecutive_losses,
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
            if pod_budget is not None and pod_budget.closed_trades_count > 0:
                raw_sz = signal.get("size_pct")
                if isinstance(raw_sz, (int, float)) and not isinstance(raw_sz, bool) and raw_sz > 0:
                    scaled_sz = min(1.0, max(0.005, float(raw_sz) * pod_budget.risk_budget_multiplier))
                    scaled_sz, _ = lift_size_pct_for_entry_min(scaled_sz)
                    signal = dict(signal)
                    signal["size_pct_before_pod_budget"] = float(raw_sz)
                    signal["size_pct"] = round(scaled_sz, 6)
                    signal["pod_risk_multiplier"] = round(pod_budget.risk_budget_multiplier, 4)
                    signal["pod_drawdown_pct"] = round(pod_budget.current_drawdown_pct, 6)
                    signal["pod_rolling_sharpe"] = (
                        None if pod_budget.rolling_sharpe is None else round(pod_budget.rolling_sharpe, 4)
                    )
                    signal["pod_risk_regime"] = pod_budget.regime
            beta_reason = portfolio_beta_exposure_block(
                list(book.positions),
                purpose=purpose,
                symbol=signal_symbol(signal),
                candidate_size_pct=signal.get("size_pct"),
                accepted_signals=kept,
            )
            if beta_reason is not None:
                beta_dropped += 1
                log.info(
                    "portfolio_beta_halt purpose=%s symbol=%s reason=%s",
                    purpose,
                    signal_symbol(signal),
                    beta_reason,
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
    if beta_dropped:
        report["beta_dropped"] = beta_dropped
    if drawdown_dropped or (pod_budget is not None and not pod_budget.allow_buy):
        report["drawdown_halt"] = None if pod_budget is None else pod_budget.halt_reason
        report["drawdown_dropped"] = drawdown_dropped
    if pod_budget is not None:
        report["pod_risk_budget"] = pod_budget.to_dict()
    if per_symbol and (book.positions or kept):
        report["beta_hedge_plan"] = compute_portfolio_beta_and_hedge(
            list(book.positions),
            candidate_signals=kept,
            purpose=purpose,
        )
    if macro_halt is not None or macro_dropped:
        report["macro_halt"] = macro_halt
        report["macro_dropped"] = macro_dropped
    return kept, report


def recheck_portfolio_beta_after_sizing(
    signals: list[dict[str, Any]],
    *,
    purpose: str,
    book: ProtectionBook,
) -> tuple[list[dict[str, Any]], int]:
    """Drop buys whose post-router size breaches the portfolio beta cap.

    The pre-router guard sees the strategy size. One-shot Kelly replaces that
    size later. When the beta gate is off this is a no-op. Sells stay.
    """
    if purpose not in LIVE_BNB_SPOT_PURPOSES or not portfolio_beta_gate_enabled():
        return signals, 0
    kept: list[dict[str, Any]] = []
    accepted_buys: list[dict[str, Any]] = []
    dropped = 0
    for signal in signals:
        if str(signal.get("action") or "").lower() != "buy":
            kept.append(signal)
            continue
        symbol = signal_symbol(signal)
        if symbol is None:
            dropped += 1
            log.info("portfolio_beta_halt purpose=%s symbol= reason=missing_symbol_after_sizing", purpose)
            continue
        reason = portfolio_beta_exposure_block(
            list(book.positions),
            purpose=purpose,
            symbol=symbol,
            candidate_size_pct=signal.get("size_pct"),
            accepted_signals=accepted_buys,
        )
        if reason is not None:
            dropped += 1
            log.info(
                "portfolio_beta_halt purpose=%s symbol=%s reason=%s stage=after_sizing",
                purpose,
                symbol,
                reason,
            )
            continue
        accepted_buys.append(signal)
        kept.append(signal)
    return kept, dropped
