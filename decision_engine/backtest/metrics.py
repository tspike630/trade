"""Performance metrics: win rate, profit factor, max drawdown, equity curve summary."""

from __future__ import annotations

from typing import Any, Optional


def _safe_div(a: float, b: float) -> Optional[float]:
    if b == 0:
        return None
    return a / b


def compute_metrics(
    closed: list[dict[str, Any]],
    equity_curve: list[dict[str, Any]],
    *,
    start_cash: float,
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
