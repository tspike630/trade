"""Signal-layer performance metrics for R1 weekly review."""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any, Optional

from review.data import SignalChainRow


def _median(xs: list[float]) -> Optional[float]:
    if not xs:
        return None
    return float(statistics.median(xs))


def _rate(num: int, den: int) -> Optional[float]:
    if den <= 0:
        return None
    return num / den


def _bucket_metrics(rows: list[SignalChainRow]) -> dict[str, Any]:
    n = len(rows)
    tokens = {(r.chain_id, r.token_address) for r in rows if r.token_address}
    r1 = [r.fwd_1h for r in rows if r.fwd_1h is not None]
    r4 = [r.fwd_4h for r in rows if r.fwd_4h is not None]
    up1 = sum(1 for x in r1 if x > 0)
    up4 = sum(1 for x in r4 if x > 0)

    with_exec = [r for r in rows if r.exec_id is not None]
    n_exec = len(with_exec)
    ok = sum(1 for r in with_exec if r.validation_ok == 1)
    executed = sum(1 for r in with_exec if r.executed == 1)
    dry = sum(1 for r in rows if (r.mode or "").lower() == "dry_run")

    return {
        "n": n,
        "unique_token": len(tokens),
        "up_1h_rate": _rate(up1, len(r1)),
        "up_4h_rate": _rate(up4, len(r4)),
        "median_1h": _median(r1),
        "median_4h": _median(r4),
        "n_1h": len(r1),
        "n_4h": len(r4),
        "validation_ok_rate": _rate(ok, n_exec),
        "executed_rate": _rate(executed, n_exec),
        "dry_run_rate": _rate(dry, n),
    }


def compute_signal_metrics(rows: list[SignalChainRow]) -> dict[str, Any]:
    """按全样本 + 按 strategy_id 分桶。"""
    overall = _bucket_metrics(rows)
    by_sid: dict[str, list[SignalChainRow]] = defaultdict(list)
    for r in rows:
        by_sid[r.strategy_id or "(unknown)"].append(r)
    by_strategy = {sid: _bucket_metrics(rs) for sid, rs in sorted(by_sid.items())}
    overall["by_strategy"] = by_strategy
    return overall


def slice_jev_filtered(
    rows: list[SignalChainRow],
    *,
    min_strength: float = 0.15,
    strategy_id: str = "jev_strength_v1",
    min_liq: float = 50_000.0,
    min_coverage: float = 10.0,
    min_price: float = 1e-6,
) -> list[SignalChainRow]:
    """与 scripts/jev_validity.py 同口径的 JEV 子集。

    过滤：strategy + action=buy + strength>=min_strength
          + liquidity_at_signal>=min_liq（缺失排除）
          + jev_coverage>=min_coverage（缺失排除）
          + price_at_signal>=min_price 且非 None（无对价排除）
    """
    out: list[SignalChainRow] = []
    for r in rows:
        if r.strategy_id != strategy_id:
            continue
        if (r.action or "").lower() != "buy":
            continue
        if r.jev_strength is None or r.jev_strength < min_strength:
            continue
        if r.liquidity_at_signal is None or r.liquidity_at_signal < min_liq:
            continue
        if r.jev_coverage is None or r.jev_coverage < min_coverage:
            continue
        if r.price_at_signal is None or r.price_at_signal < min_price:
            continue
        out.append(r)
    return out
