"""Performance metrics: win rate, profit factor, max drawdown, Sharpe/Sortino/Calmar, and PSR/DSR."""

from __future__ import annotations

import math
from typing import Any, Optional, Sequence


def _safe_div(a: float, b: float) -> Optional[float]:
    if b == 0:
        return None
    return a / b


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Rational approximation of the standard normal inverse CDF for p in (0, 1)."""
    p_clamped = min(1.0 - 1e-12, max(1e-12, float(p)))
    # Beasley-Springer-Moro / Acklam rational approximation
    a = (
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    )
    b = (
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    )
    c = (
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    )
    d = (
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    )
    p_low = 0.02425
    p_high = 1.0 - p_low
    if p_clamped < p_low:
        q = math.sqrt(-2.0 * math.log(p_clamped))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p_clamped <= p_high:
        q = p_clamped - 0.5
        r = q * q
        return (
            (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
            * q
            / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
        )
    q = math.sqrt(-2.0 * math.log(1.0 - p_clamped))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
        (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
    )


def compute_psr_and_dsr(
    returns: Sequence[float],
    *,
    benchmark_sr: float = 0.0,
    num_trials: int = 1,
    trials_sr_std: float | None = None,
) -> dict[str, Optional[float]]:
    r"""Compute non-normal Probabilistic Sharpe Ratio (PSR) and Deflated Sharpe Ratio (DSR).

    Follows Bailey & López de Prado (2012, 2014):
      - Adjusts standard error of $\widehat{SR}$ for sample skewness ($\hat{\gamma}_3$)
        and kurtosis ($\hat{\gamma}_4$):
        $$\hat{\sigma}(\widehat{SR}) = \sqrt{\frac{1 - \hat{\gamma}_3 \widehat{SR} + \frac{\hat{\gamma}_4 - 1}{4} \widehat{SR}^2}{T - 1}}$$
      - For $K > 1$ multiple-testing trials, sets the expected maximum Sharpe hurdle
        $SR_0$ via False Strategy Theorem:
        $$SR_0 = \sigma_{\{SR_k\}} \left[ (1 - \gamma) \Phi^{-1}\left(1 - \frac{1}{K}\right) + \gamma \Phi^{-1}\left(1 - \frac{1}{K e}\right) \right]$$
    """
    n = len(returns)
    if n < 3:
        return {
            "sharpe_ratio": None,
            "sortino_ratio": None,
            "skewness": None,
            "kurtosis": None,
            "psr": None,
            "dsr": None,
            "dsr_hurdle_sr": None,
        }
    mean_r = sum(returns) / n
    diffs = [r - mean_r for r in returns]
    var = sum(d * d for d in diffs) / (n - 1)
    std = math.sqrt(max(0.0, var))
    if std <= 1e-12:
        return {
            "sharpe_ratio": 0.0,
            "sortino_ratio": 0.0,
            "skewness": 0.0,
            "kurtosis": 3.0,
            "psr": 0.5,
            "dsr": 0.5,
            "dsr_hurdle_sr": 0.0,
        }
    sr = mean_r / std
    downside_sq = [min(0.0, r) ** 2 for r in returns]
    downside_dev = math.sqrt(sum(downside_sq) / n)
    sortino = (mean_r / downside_dev) if downside_dev > 1e-12 else None

    m3 = sum(d**3 for d in diffs) / n
    m4 = sum(d**4 for d in diffs) / n
    skew = m3 / (std**3)
    kurt = m4 / (std**4)

    radicand = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * (sr**2)
    sr_std_err = math.sqrt(max(1e-9, radicand) / max(1, n - 1))

    psr_z = (sr - benchmark_sr) / sr_std_err
    psr = _norm_cdf(psr_z)

    k_trials = max(1, int(num_trials))
    if k_trials <= 1:
        hurdle_sr = benchmark_sr
        dsr = psr
    else:
        euler_gamma = 0.5772156649015329
        sigma_sr = trials_sr_std if (trials_sr_std is not None and trials_sr_std > 0) else (1.0 / math.sqrt(n))
        z1 = _norm_ppf(1.0 - 1.0 / k_trials)
        z2 = _norm_ppf(1.0 - 1.0 / (k_trials * math.e))
        hurdle_sr = benchmark_sr + sigma_sr * ((1.0 - euler_gamma) * z1 + euler_gamma * z2)
        dsr_z = (sr - hurdle_sr) / sr_std_err
        dsr = _norm_cdf(dsr_z)

    return {
        "sharpe_ratio": round(sr, 6),
        "sortino_ratio": None if sortino is None else round(sortino, 6),
        "skewness": round(skew, 6),
        "kurtosis": round(kurt, 6),
        "psr": round(psr, 6),
        "dsr": round(dsr, 6),
        "dsr_hurdle_sr": round(hurdle_sr, 6),
    }


def compute_metrics(
    closed: list[dict[str, Any]],
    equity_curve: list[dict[str, Any]],
    *,
    start_cash: float,
    num_trials: int = 1,
) -> dict[str, Any]:
    pnls = [float(p.get("pnl_usd") or 0.0) for p in closed]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    flats = [x for x in pnls if x == 0]
    n = len(pnls)
    win_rate = (len(wins) / n) if n else None
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = _safe_div(gross_profit, gross_loss) if losses else (
        float("inf") if wins else None
    )
    if profit_factor == float("inf"):
        profit_factor_out: Any = "inf"
    else:
        profit_factor_out = profit_factor

    avg_win = (sum(wins) / len(wins)) if wins else None
    avg_loss = (sum(losses) / len(losses)) if losses else None
    payoff = None
    if avg_win is not None and avg_loss is not None and avg_loss != 0:
        payoff = abs(avg_win / avg_loss)

    # Max drawdown on equity curve
    peak = start_cash
    max_dd = 0.0
    max_dd_pct = 0.0
    last_eq = start_cash
    for pt in equity_curve:
        eq = float(pt.get("equity_usd") or start_cash)
        last_eq = eq
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
        dd_pct = (dd / peak) if peak > 0 else 0.0
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct

    total_pnl = last_eq - start_cash
    ret_pct = (total_pnl / start_cash * 100.0) if start_cash else None

    trade_rets: list[float] = []
    for p in closed:
        sz_usd = float(p.get("size_usd") or 0.0)
        pnl_u = float(p.get("pnl_usd") or 0.0)
        if sz_usd > 0:
            trade_rets.append(pnl_u / sz_usd)
        elif start_cash > 0:
            trade_rets.append(pnl_u / start_cash)

    sr_stats = compute_psr_and_dsr(trade_rets, num_trials=num_trials)
    calmar = (
        round((ret_pct / 100.0) / max_dd_pct, 6)
        if (ret_pct is not None and max_dd_pct > 1e-9)
        else None
    )

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "flats": len(flats),
        "win_rate": win_rate,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": profit_factor_out,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff_ratio": payoff,
        "total_pnl_usd": total_pnl,
        "return_pct": ret_pct,
        "max_drawdown_usd": max_dd,
        "max_drawdown_pct": max_dd_pct * 100.0,
        "final_equity": last_eq,
        "start_cash": start_cash,
        "equity_points": len(equity_curve),
        "sharpe_ratio": sr_stats["sharpe_ratio"],
        "sortino_ratio": sr_stats["sortino_ratio"],
        "calmar_ratio": calmar,
        "skewness": sr_stats["skewness"],
        "kurtosis": sr_stats["kurtosis"],
        "probabilistic_sharpe_ratio": sr_stats["psr"],
        "deflated_sharpe_ratio": sr_stats["dsr"],
    }


def format_metrics_table(by_strategy: dict[str, dict[str, Any]]) -> str:
    lines = [
        "| strategy_id | trades | win_rate | PF | MDD% | return% | final_eq |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for sid, m in sorted(by_strategy.items()):
        wr = m.get("win_rate")
        wr_s = f"{wr*100:.1f}%" if wr is not None else "—"
        pf = m.get("profit_factor")
        pf_s = f"{pf:.2f}" if isinstance(pf, (int, float)) else str(pf or "—")
        mdd = m.get("max_drawdown_pct")
        mdd_s = f"{mdd:.2f}" if mdd is not None else "—"
        ret = m.get("return_pct")
        ret_s = f"{ret:.2f}" if ret is not None else "—"
        lines.append(
            f"| {sid} | {m.get('trades', 0)} | {wr_s} | {pf_s} | {mdd_s} | "
            f"{ret_s} | {m.get('final_equity', 0):.2f} |"
        )
    return "\n".join(lines)
