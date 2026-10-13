"""Factor layer tests (Qlib-style quantile layers + rank IC) for review.

Read-only helper over SignalChainRow lists. No numpy/scipy dependency:
quantile cuts and Spearman rank-IC are implemented with stdlib only.

  cd /workspace/dexscreener-crawler
  .venv/bin/python -m review.run --period 7d --report --factor-layers
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Callable, Optional

from review.data import SignalChainRow, safe_float

# 因子提取器：row → float | None（缺返回 None，该样本不参与该因子分层）
FACTOR_GETTERS: dict[str, Callable[[SignalChainRow], Optional[float]]] = {}


def _raw_get(row: SignalChainRow, key: str) -> Optional[float]:
    """从 raw_json（顶层或 extras 子字典）取数值。"""
    raw = row.raw if isinstance(row.raw, dict) else {}
    extras = raw.get("extras")
    if isinstance(extras, dict) and key in extras:
        v = safe_float(extras.get(key))
        if v is not None:
            return v
    return safe_float(raw.get(key))


def _register(name: str, getter: Callable[[SignalChainRow], Optional[float]]) -> None:
    FACTOR_GETTERS[name] = getter


_register("jev_strength", lambda r: r.jev_strength)
_register("change_5m", lambda r: _raw_get(r, "change_5m"))
_register("volume_strength", lambda r: _raw_get(r, "volume_strength"))
_register("liquidity", lambda r: r.liquidity_at_signal)
_register("coverage", lambda r: r.jev_coverage)


def list_factors() -> list[str]:
    return sorted(FACTOR_GETTERS)


# ---------------------------------------------------------------------------
# 分位数分层
# ---------------------------------------------------------------------------

def _quantile_cuts(values: list[float], n_layers: int) -> list[float]:
    """返回 n_layers-1 个切点（升序），用线性分位数（接近 numpy 默认）。"""
    values = sorted(values)
    k = len(values)
    cuts: list[float] = []
    for i in range(1, n_layers):
        pos = (k - 1) * i / n_layers
        lo = int(math.floor(pos))
        hi = int(math.ceil(pos))
        if lo == hi:
            cuts.append(values[lo])
        else:
            cuts.append((values[lo] + values[hi]) / 2.0)
    return cuts


def _layer_index(value: float, cuts: list[float]) -> int:
    """返回 1..n 层编号。"""
    idx = 1
    for c in cuts:
        if value > c:
            idx += 1
        else:
            break
    return idx


def _layer_stats(values: list[tuple[float, float]]) -> dict[str, Any]:
    """输入 (factor, fwd_1h) 列表；输出均值/胜率/中位/样本。"""
    if not values:
        return {"n": 0, "mean_ret": None, "up_rate": None, "median_ret": None}
    rets = [v[1] for v in values]
    n_up = sum(1 for x in rets if x > 0)
    return {
        "n": len(values),
        "mean_ret": statistics.fmean(rets) if rets else None,
        "up_rate": n_up / len(rets) if rets else None,
        "median_ret": statistics.median(rets),
    }


def factor_layers(
    rows: list[SignalChainRow],
    factor_name: str,
    *,
    n_layers: int = 5,
    min_samples: int = 10,
) -> dict[str, Any] | None:
    """按因子值分位数分层，每层输出 1h 前向绩效。

    - min_samples: 有因子值且有 fwd_1h 的样本 < 该值 → 返回 None（样本不足）
    - 返回: {"factor": name, "ic": spearman_ic, "monotonic": note, "layers": [...], "n": n}
    """
    getter = FACTOR_GETTERS.get(factor_name)
    if getter is None:
        return None
    pairs: list[tuple[float, float]] = []
    for r in rows:
        if r.fwd_1h is None:
            continue
        fv = getter(r)
        if fv is None:
            continue
        pairs.append((float(fv), float(r.fwd_1h)))
    if len(pairs) < min_samples:
        return None
    vals = [p[0] for p in pairs]
    cuts = _quantile_cuts(vals, n_layers)
    layers = []
    for li in range(1, n_layers + 1):
        bucket = [p for p in pairs if _layer_index(p[0], cuts) == li]
        layers.append({"layer": f"Q{li}", **_layer_stats(bucket)})
    return {
        "factor": factor_name,
        "n": len(pairs),
        "ic": spearman_ic(pairs),
        "monotonic": _monotonicity(layers),
        "layers": layers,
    }


# ---------------------------------------------------------------------------
# Spearman 秩相关（stdlib 手写；无 scipy）
# ---------------------------------------------------------------------------

def _rank(xs: list[float]) -> list[float]:
    """平均秩（并列取均值）。"""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(a: list[float], b: list[float]) -> Optional[float]:
    n = len(a)
    if n < 3:
        return None
    ma = statistics.fmean(a)
    mb = statistics.fmean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    if da == 0 or db == 0:
        return None
    r = num / (da * db)
    # sqrt(s)*sqrt(s) is not bit-exact, so a perfect line lands at ±(1-eps).
    # Snap that dust so a strictly monotone sample reports ±1.0.
    if r >= 1.0 - 1e-12:
        return 1.0
    if r <= -1.0 + 1e-12:
        return -1.0
    return r


def spearman_ic(pairs: list[tuple[float, float]]) -> Optional[float]:
    """因子值 vs fwd_1h 的秩相关（rank IC）。"""
    if len(pairs) < 3:
        return None
    fa = [p[0] for p in pairs]
    rb = [p[1] for p in pairs]
    return _pearson(_rank(fa), _rank(rb))


# ---------------------------------------------------------------------------
# 单调性
# ---------------------------------------------------------------------------

def _monotonicity(layers: list[dict[str, Any]]) -> str:
    means = [l.get("mean_ret") for l in layers]
    if any(m is None for m in means):
        return "n/a"
    diffs = [means[i + 1] - means[i] for i in range(len(means) - 1)]
    ups = sum(1 for d in diffs if d > 0)
    downs = sum(1 for d in diffs if d < 0)
    if ups == len(diffs):
        return "单调递增"
    if downs == len(diffs):
        return "单调递减"
    if ups >= len(diffs) - 1 or downs >= len(diffs) - 1:
        return "基本单调（近似）"
    return "无单调性"


# ---------------------------------------------------------------------------
# vectorbt 式绩效矩阵（多策略 × 指标）
# ---------------------------------------------------------------------------

def performance_matrix(
    by_strategy: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """输入 metrics['by_strategy']；输出矩阵行（strategy → 指标行）。"""
    rows: list[dict[str, Any]] = []
    for sid, m in sorted(by_strategy.items()):
        rows.append(
            {
                "strategy": sid,
                "n": m.get("n", 0),
                "up_1h": m.get("up_1h_rate"),
                "med_1h": m.get("median_1h"),
                "up_4h": m.get("up_4h_rate"),
                "med_4h": m.get("median_4h"),
                "validation_ok": m.get("validation_ok_rate"),
            }
        )
    return rows
