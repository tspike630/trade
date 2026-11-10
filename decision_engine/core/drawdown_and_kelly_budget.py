r"""Institutional Pod-Level Drawdown Circuit Breaker & Rolling Sharpe / Half-Kelly Risk Budgeting.

Implements Wall Street multi-manager pod risk controls (Millennium / Citadel style)
on top of the read-only execution ledger:

1. **High-Water Mark (HWM) Drawdown Circuit Breaker**:
   - Reconstructs closed trade returns from ``binance_spot_positions``
     (``status IN ('closed', 'oco_filled')``).
   - Hard Halt: When peak-to-trough book drawdown ``>= LIVE_BNB_MAX_DRAWDOWN_HALT_PCT``
     (default ``5.0%``), all new BUY signals on that purpose are halted with
     ``pod_max_drawdown_halt``.
   - Soft Linear De-allocation Throttle: Between ``LIVE_BNB_SOFT_DRAWDOWN_PCT``
     (default ``2.5%``) and the hard halt (``5.0%``), position sizing is linearly
     throttled from ``1.0x`` down to ``0.25x``.

2. **Consecutive Stop-Loss Streak Cooldown**:
   - Counts consecutive losing trades at the tail of closed positions.
   - When ``consecutive_losses >= LIVE_BNB_MAX_CONSECUTIVE_LOSSES`` (default ``4``)
     within the cooldown window (``LIVE_BNB_LOSS_COOLDOWN_SEC``, default ``1800``s),
     new BUYs are halted with ``pod_consecutive_loss_cooldown``.

3. **Rolling Half-Kelly & Sharpe Adaptive Risk-Budgeting**:
   - Once at least ``min_trades_for_kelly`` (default ``5``) closed trades exist,
     computes rolling win rate $p$, payoff ratio $b = \bar{W} / \bar{L}$,
     Half-Kelly fraction $f^*_{\text{half}} = \frac{1}{2}(p - \frac{1-p}{b})$,
     and per-trade Sharpe ratio $S = \mu / \sigma$.
   - Maps empirical edge to a bounded scalar in ``[0.50, 1.25]`` and combines it
     conservatively with the drawdown throttle scalar.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

DRAWDOWN_KELLY_GATE_ENV = "LIVE_BNB_DRAWDOWN_KELLY_GATE"
MAX_DRAWDOWN_HALT_PCT_ENV = "LIVE_BNB_MAX_DRAWDOWN_HALT_PCT"
SOFT_DRAWDOWN_PCT_ENV = "LIVE_BNB_SOFT_DRAWDOWN_PCT"
MAX_CONSECUTIVE_LOSSES_ENV = "LIVE_BNB_MAX_CONSECUTIVE_LOSSES"
LOSS_COOLDOWN_SEC_ENV = "LIVE_BNB_LOSS_COOLDOWN_SEC"
DEFAULT_POSITION_RISK_WEIGHT_ENV = "LIVE_BNB_DEFAULT_TRADE_WEIGHT"

DEFAULT_MAX_DRAWDOWN_HALT_PCT = 0.05
DEFAULT_SOFT_DRAWDOWN_PCT = 0.025
DEFAULT_MAX_CONSECUTIVE_LOSSES = 4
DEFAULT_LOSS_COOLDOWN_SEC = 1800
DEFAULT_TRADE_WEIGHT = 0.05
DEFAULT_ROUND_TRIP_FEE_PCT = 0.0020
DEFAULT_MIN_TRADES_FOR_KELLY = 5
DEFAULT_LOOKBACK_TRADES = 50

_TRUE = frozenset({"1", "true", "yes", "on"})
_CLOSED_STATUSES = frozenset({"closed", "oco_filled"})
_FADE_ARM = "live_bnb_boll_fade"


def drawdown_kelly_gate_enabled() -> bool:
    raw = os.environ.get(DRAWDOWN_KELLY_GATE_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in _TRUE


def _finite(val: Any) -> float | None:
    if isinstance(val, bool) or val is None:
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return num


def _env_float(name: str, default: float) -> float:
    val = _finite(os.environ.get(name))
    if val is None or val <= 0:
        return default
    return val


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        val = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default
    return max(1, val)


def _row_matches_purpose(row: dict[str, Any], purpose: str | None) -> bool:
    if not purpose:
        return True
    row_p = str(row.get("purpose") or "").strip()
    if purpose == _FADE_ARM:
        return row_p in ("", _FADE_ARM)
    return row_p == purpose


def _parse_iso_ts(raw: Any) -> float | None:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        val = float(raw)
        return val / 1000.0 if val > 1e11 else val
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


@dataclass(frozen=True)
class ClosedTradeSample:
    decision_log_id: int | None
    symbol: str
    net_return_pct: float
    book_return_pct: float
    exit_reason: str
    closed_ts: float | None


@dataclass(frozen=True)
class PodRiskBudgetResult:
    allow_buy: bool
    halt_reason: str | None
    risk_budget_multiplier: float
    drawdown_throttle_scalar: float
    kelly_sharpe_scalar: float
    current_drawdown_pct: float
    max_drawdown_pct: float
    peak_equity_index: float
    current_equity_index: float
    closed_trades_count: int
    win_rate: float | None
    payoff_ratio: float | None
    half_kelly_fraction: float | None
    rolling_sharpe: float | None
    consecutive_losses: int
    regime: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "allow_buy": self.allow_buy,
            "halt_reason": self.halt_reason,
            "risk_budget_multiplier": round(self.risk_budget_multiplier, 4),
            "drawdown_throttle_scalar": round(self.drawdown_throttle_scalar, 4),
            "kelly_sharpe_scalar": round(self.kelly_sharpe_scalar, 4),
            "current_drawdown_pct": round(self.current_drawdown_pct, 6),
            "max_drawdown_pct": round(self.max_drawdown_pct, 6),
            "peak_equity_index": round(self.peak_equity_index, 6),
            "current_equity_index": round(self.current_equity_index, 6),
            "closed_trades_count": self.closed_trades_count,
            "win_rate": None if self.win_rate is None else round(self.win_rate, 4),
            "payoff_ratio": None if self.payoff_ratio is None else round(self.payoff_ratio, 4),
            "half_kelly_fraction": (
                None if self.half_kelly_fraction is None else round(self.half_kelly_fraction, 4)
            ),
            "rolling_sharpe": None if self.rolling_sharpe is None else round(self.rolling_sharpe, 4),
            "consecutive_losses": self.consecutive_losses,
            "regime": self.regime,
        }


def infer_closed_trade_return(
    row: dict[str, Any],
    *,
    round_trip_fee_pct: float = DEFAULT_ROUND_TRIP_FEE_PCT,
    default_trade_weight: float = DEFAULT_TRADE_WEIGHT,
) -> ClosedTradeSample | None:
    """Reconstruct net trade return and book-weighted return from a closed ledger row."""
    status = str(row.get("status") or "").strip().lower()
    if status not in _CLOSED_STATUSES:
        return None

    symbol = str(row.get("symbol") or "").strip().upper()
    exit_reason = str(row.get("exit_reason") or "").strip().lower()
    dec_id_raw = row.get("decision_log_id")
    dec_id = int(dec_id_raw) if isinstance(dec_id_raw, int) and not isinstance(dec_id_raw, bool) else None

    explicit_ret = _finite(row.get("realized_return_pct"))
    if explicit_ret is not None:
        net_ret = explicit_ret
    else:
        entry_px = _finite(row.get("entry_price"))
        exit_px = _finite(row.get("exit_price"))
        stop_px = _finite(row.get("stop_price"))
        tp_px = _finite(row.get("take_profit_price"))

        if entry_px is None or entry_px <= 0:
            return None

        resolved_exit: float | None = exit_px
        if resolved_exit is None or resolved_exit <= 0:
            if exit_reason == "take_profit" and tp_px is not None and tp_px > 0:
                resolved_exit = tp_px
            elif exit_reason in {"stop_loss", "oco_failed_auto_flatten"} and stop_px is not None and stop_px > 0:
                resolved_exit = stop_px
            elif exit_reason == "time_stop_max_hold":
                resolved_exit = entry_px
            elif status == "oco_filled" and tp_px is not None and tp_px > 0:
                resolved_exit = tp_px

        if resolved_exit is None or resolved_exit <= 0:
            return None

        gross_ret = (resolved_exit - entry_px) / entry_px
        net_ret = gross_ret - max(0.0, float(round_trip_fee_pct))

    weight = _finite(row.get("size_pct"))
    if weight is None or not (0.0 < weight <= 1.0):
        weight = default_trade_weight

    closed_ts = _parse_iso_ts(row.get("updated_at") or row.get("closed_at") or row.get("created_at"))
    return ClosedTradeSample(
        decision_log_id=dec_id,
        symbol=symbol,
        net_return_pct=net_ret,
        book_return_pct=net_ret * weight,
        exit_reason=exit_reason or status,
        closed_ts=closed_ts,
    )


def reconstruct_closed_trade_returns(
    positions: Sequence[dict[str, Any]],
    *,
    purpose: str | None = None,
    lookback_trades: int = DEFAULT_LOOKBACK_TRADES,
    round_trip_fee_pct: float = DEFAULT_ROUND_TRIP_FEE_PCT,
    default_trade_weight: float = DEFAULT_TRADE_WEIGHT,
) -> list[ClosedTradeSample]:
    """Extract ordered closed trade samples for ``purpose`` up to ``lookback_trades``."""
    samples: list[ClosedTradeSample] = []
    for row in positions:
        if not isinstance(row, dict):
            continue
        if not _row_matches_purpose(row, purpose):
            continue
        sample = infer_closed_trade_return(
            row,
            round_trip_fee_pct=round_trip_fee_pct,
            default_trade_weight=default_trade_weight,
        )
        if sample is not None:
            samples.append(sample)
    if lookback_trades > 0 and len(samples) > lookback_trades:
        samples = samples[-lookback_trades:]
    return samples


def evaluate_pod_drawdown_and_kelly(
    positions: Sequence[dict[str, Any]],
    *,
    purpose: str | None = None,
    now_ts: float | None = None,
    max_drawdown_halt_pct: float | None = None,
    soft_drawdown_pct: float | None = None,
    max_consecutive_losses: int | None = None,
    loss_cooldown_sec: int | None = None,
    min_trades_for_kelly: int = DEFAULT_MIN_TRADES_FOR_KELLY,
    lookback_trades: int = DEFAULT_LOOKBACK_TRADES,
) -> PodRiskBudgetResult:
    """Evaluate portfolio drawdown circuit breakers, loss-streak cooldown, and Kelly/Sharpe scalar."""
    hard_dd_cap = (
        float(max_drawdown_halt_pct)
        if max_drawdown_halt_pct is not None
        else _env_float(MAX_DRAWDOWN_HALT_PCT_ENV, DEFAULT_MAX_DRAWDOWN_HALT_PCT)
    )
    soft_dd_trig = (
        float(soft_drawdown_pct)
        if soft_drawdown_pct is not None
        else _env_float(SOFT_DRAWDOWN_PCT_ENV, DEFAULT_SOFT_DRAWDOWN_PCT)
    )
    if soft_dd_trig >= hard_dd_cap:
        soft_dd_trig = hard_dd_cap * 0.5

    streak_cap = (
        int(max_consecutive_losses)
        if max_consecutive_losses is not None
        else _env_int(MAX_CONSECUTIVE_LOSSES_ENV, DEFAULT_MAX_CONSECUTIVE_LOSSES)
    )
    cooldown_sec = (
        int(loss_cooldown_sec)
        if loss_cooldown_sec is not None
        else _env_int(LOSS_COOLDOWN_SEC_ENV, DEFAULT_LOSS_COOLDOWN_SEC)
    )
    default_weight = _env_float(DEFAULT_POSITION_RISK_WEIGHT_ENV, DEFAULT_TRADE_WEIGHT)

    samples = reconstruct_closed_trade_returns(
        positions,
        purpose=purpose,
        lookback_trades=lookback_trades,
        default_trade_weight=default_weight,
    )
    if not samples:
        return PodRiskBudgetResult(
            allow_buy=True,
            halt_reason=None,
            risk_budget_multiplier=1.0,
            drawdown_throttle_scalar=1.0,
            kelly_sharpe_scalar=1.0,
            current_drawdown_pct=0.0,
            max_drawdown_pct=0.0,
            peak_equity_index=1.0,
            current_equity_index=1.0,
            closed_trades_count=0,
            win_rate=None,
            payoff_ratio=None,
            half_kelly_fraction=None,
            rolling_sharpe=None,
            consecutive_losses=0,
            regime="normal_no_history",
        )

    # 1. Reconstruct High-Water Mark (HWM) equity curve & drawdown
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for s in samples:
        equity *= max(0.01, 1.0 + s.book_return_pct)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    current_dd = (peak - equity) / peak if peak > 0 else 0.0

    # 2. Count consecutive losses at the tail
    consecutive_losses = 0
    last_loss_ts: float | None = None
    for s in reversed(samples):
        if s.net_return_pct < 0.0:
            consecutive_losses += 1
            if last_loss_ts is None:
                last_loss_ts = s.closed_ts
        else:
            break

    # 3. Compute rolling Win-Rate, Payoff Ratio, Half-Kelly, and Sharpe
    n = len(samples)
    wins = [s.net_return_pct for s in samples if s.net_return_pct > 0.0]
    losses = [abs(s.net_return_pct) for s in samples if s.net_return_pct < 0.0]
    win_rate = len(wins) / n if n > 0 else None
    avg_win = (sum(wins) / len(wins)) if wins else None
    avg_loss = (sum(losses) / len(losses)) if losses else None

    payoff_ratio: float | None = None
    half_kelly: float | None = None
    if win_rate is not None:
        if avg_win is not None and avg_loss is not None and avg_loss > 1e-9:
            payoff_ratio = avg_win / avg_loss
            full_kelly = win_rate - ((1.0 - win_rate) / payoff_ratio)
            half_kelly = 0.5 * full_kelly
        elif avg_win is not None and not losses:
            half_kelly = 0.5 * win_rate
        elif not wins and losses:
            half_kelly = -0.5

    rets = [s.net_return_pct for s in samples]
    mean_ret = sum(rets) / n
    rolling_sharpe: float | None = None
    if n >= 2:
        var = sum((r - mean_ret) ** 2 for r in rets) / (n - 1)
        std_ret = math.sqrt(max(0.0, var))
        if std_ret > 1e-9:
            rolling_sharpe = mean_ret / std_ret

    # 4. Compute Kelly/Sharpe Adaptive Scalar in [0.50, 1.25]
    kelly_sharpe_scalar = 1.0
    if n >= min_trades_for_kelly:
        if half_kelly is not None and half_kelly <= 0.0:
            # Negative empirical expectancy -> defensive half size
            kelly_sharpe_scalar = 0.50
        elif half_kelly is not None:
            # Normalize against a 10% baseline Half-Kelly target
            raw_k = 0.65 + min(0.60, max(0.0, half_kelly / 0.20))
            if rolling_sharpe is not None and rolling_sharpe < 0.0:
                raw_k = min(raw_k, 0.60)
            elif rolling_sharpe is not None and rolling_sharpe >= 0.40:
                raw_k = min(1.25, raw_k * 1.05)
            kelly_sharpe_scalar = max(0.50, min(1.25, raw_k))

    # 5. Compute Drawdown Linear Throttle Scalar in [0.25, 1.0]
    dd_scalar = 1.0
    if current_dd >= hard_dd_cap:
        dd_scalar = 0.0
    elif current_dd > soft_dd_trig and hard_dd_cap > soft_dd_trig:
        frac = (current_dd - soft_dd_trig) / (hard_dd_cap - soft_dd_trig)
        dd_scalar = max(0.25, 1.0 - 0.75 * frac)

    # 6. Check Hard Circuit Breakers
    if current_dd >= hard_dd_cap:
        return PodRiskBudgetResult(
            allow_buy=False,
            halt_reason="pod_max_drawdown_halt",
            risk_budget_multiplier=0.0,
            drawdown_throttle_scalar=0.0,
            kelly_sharpe_scalar=kelly_sharpe_scalar,
            current_drawdown_pct=current_dd,
            max_drawdown_pct=max_dd,
            peak_equity_index=peak,
            current_equity_index=equity,
            closed_trades_count=n,
            win_rate=win_rate,
            payoff_ratio=payoff_ratio,
            half_kelly_fraction=half_kelly,
            rolling_sharpe=rolling_sharpe,
            consecutive_losses=consecutive_losses,
            regime="halted_max_drawdown",
        )

    if consecutive_losses >= streak_cap:
        in_cooldown = True
        if last_loss_ts is not None and cooldown_sec > 0:
            ref_now = float(now_ts) if now_ts is not None else datetime.now(timezone.utc).timestamp()
            in_cooldown = (ref_now - last_loss_ts) < cooldown_sec
        if in_cooldown:
            return PodRiskBudgetResult(
                allow_buy=False,
                halt_reason="pod_consecutive_loss_cooldown",
                risk_budget_multiplier=0.0,
                drawdown_throttle_scalar=dd_scalar,
                kelly_sharpe_scalar=kelly_sharpe_scalar,
                current_drawdown_pct=current_dd,
                max_drawdown_pct=max_dd,
                peak_equity_index=peak,
                current_equity_index=equity,
                closed_trades_count=n,
                win_rate=win_rate,
                payoff_ratio=payoff_ratio,
                half_kelly_fraction=half_kelly,
                rolling_sharpe=rolling_sharpe,
                consecutive_losses=consecutive_losses,
                regime="halted_loss_streak_cooldown",
            )

    # Combine Drawdown Throttle and Kelly/Sharpe Scalar:
    # When in soft drawdown (< 1.0), never allow Kelly to boost above dd_scalar.
    if dd_scalar < 1.0:
        combined = max(0.25, min(dd_scalar, kelly_sharpe_scalar))
        regime = "throttled_soft_drawdown"
    else:
        combined = max(0.25, min(1.25, kelly_sharpe_scalar))
        if combined < 0.85:
            regime = "defensive_kelly_downscale"
        elif combined > 1.05:
            regime = "offensive_kelly_upscale"
        else:
            regime = "normal"

    return PodRiskBudgetResult(
        allow_buy=True,
        halt_reason=None,
        risk_budget_multiplier=combined,
        drawdown_throttle_scalar=dd_scalar,
        kelly_sharpe_scalar=kelly_sharpe_scalar,
        current_drawdown_pct=current_dd,
        max_drawdown_pct=max_dd,
        peak_equity_index=peak,
        current_equity_index=equity,
        closed_trades_count=n,
        win_rate=win_rate,
        payoff_ratio=payoff_ratio,
        half_kelly_fraction=half_kelly,
        rolling_sharpe=rolling_sharpe,
        consecutive_losses=consecutive_losses,
        regime=regime,
    )
