"""F-1 signal-source loss streak. Record only.

Same ``strategy_id``: a close with ``pnl_usd < 0`` increments the streak; a
close with ``pnl_usd > 0`` clears it. ``pnl_usd == 0`` leaves the streak as it
is (not a loss, and not the clear the spec names).

``EXECUTION_F1_CIRCUIT`` defaults off. This module does not reject orders.
``circuit_rejects_open`` is hardcoded False and has no gateway, risk, or
executor caller. A future fuse, if approved, may refuse a new buy only; it
must not flatten an open position.
"""

from __future__ import annotations

from typing import Any


def next_streak(streak: int, pnl_usd: float) -> int:
    """Next consecutive-loss count after one close."""
    pnl = float(pnl_usd)
    current = int(streak)
    if pnl < 0:
        return current + 1
    if pnl > 0:
        return 0
    return current


def circuit_rejects_open(action: str, streak: int) -> bool:
    """Always False. Not wired into the order path.

    Arguments keep a stable signature for a later buy-only fuse. A sell is
    never a flatten trigger here.
    """
    _ = (str(action or ""), int(streak))
    return False


def conditional_third_loss(pnls: list[float]) -> tuple[int, int, float | None]:
    """P(close is a loss | the previous two closes were losses).

    Sliding window on one strategy's close sequence, in time order. A loss is
    ``pnl < 0``. Longer streaks contribute later windows, so the condition is
    "already down at least two", not only the first pair of a run.

    Returns ``(cond_n, cond_hits, rate)``. ``rate`` is None when ``cond_n`` is 0.
    """
    cond = 0
    hits = 0
    for i in range(len(pnls) - 2):
        if pnls[i] < 0 and pnls[i + 1] < 0:
            cond += 1
            if pnls[i + 2] < 0:
                hits += 1
    if cond == 0:
        return 0, 0, None
    return cond, hits, hits / cond


def summarize_pnls(pnls: list[float]) -> dict[str, Any]:
    """Replay one strategy. ``max_streak`` is the longest value ``next_streak`` reached."""
    streak = 0
    max_streak = 0
    for pnl in pnls:
        streak = next_streak(streak, pnl)
        if streak > max_streak:
            max_streak = streak
    cond_n, cond_hits, rate = conditional_third_loss(pnls)
    return {
        "n": len(pnls),
        "max_streak": max_streak,
        "current_streak": streak,
        "cond_n": cond_n,
        "cond_hits": cond_hits,
        "p_third_loss_given_streak_ge2": rate,
    }


def summarize_groups(groups: dict[str, list[float]]) -> dict[str, Any]:
    """Per strategy, plus a micro-average of the conditional rate (windows, not strategies)."""
    by_strategy: list[dict[str, Any]] = []
    samples = 0
    longest = 0
    cond_n = 0
    cond_hits = 0
    for strategy_id in sorted(groups):
        stats = summarize_pnls(groups[strategy_id])
        by_strategy.append({"strategy_id": strategy_id, **stats})
        samples += int(stats["n"])
        longest = max(longest, int(stats["max_streak"]))
        cond_n += int(stats["cond_n"])
        cond_hits += int(stats["cond_hits"])
    rate = (cond_hits / cond_n) if cond_n else None
    return {
        "samples": samples,
        "longest_loss_streak": longest,
        "cond_n": cond_n,
        "cond_hits": cond_hits,
        "p_third_loss_given_streak_ge2": rate,
        "by_strategy": by_strategy,
    }
