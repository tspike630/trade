r"""Institutional Walk-Forward Out-of-Sample (OOS) Validation & Purged Embargo Engine.

Implements López de Prado's Purged Walk-Forward Cross-Validation and Overfitting
Diagnostics for ``decision_engine.backtest``:

1. **Purged & Embargoed Rolling Train/Test Splits (``build_walk_forward_splits``)**:
   - Divides a time window or ordered trade/bar sequence into $K$ rolling (or anchored)
     In-Sample (IS) and Out-of-Sample (OOS) folds.
   - Enforces a strict **Purge + Embargo Horizon** (``embargo_sec``, default ``3600``s =
     60m max hold) between the end of the IS fold and the start of the OOS fold so open
     positions from IS never leak PnL into OOS.

2. **Walk-Forward Efficiency (WFE) & Overfitting Diagnostics (``evaluate_walk_forward_folds``)**:
   - Aggregates IS vs. OOS Sharpe ratios, mean trade returns, and win rates across folds.
   - Computes **Walk-Forward Efficiency (WFE)**:
     $$\text{WFE} = \frac{\overline{SR}_{\text{OOS}}}{\overline{SR}_{\text{IS}}}$$
   - Computes **Deflated Sharpe Ratio (DSR)** across all parameter trials $M$ tested
     during IS optimization.
   - Flags ``overfitting_detected = True`` when:
     - $\overline{SR}_{\text{OOS}} \le 0$ despite positive $\overline{SR}_{\text{IS}}$, or
     - $\text{WFE} < \text{min\_wfe\_ratio}$ (default ``0.35``), or
     - Pooled OOS Deflated Sharpe Ratio probability ``< min_dsr_prob`` (default ``0.50``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from decision_engine.backtest.metrics import compute_psr_and_dsr

DEFAULT_EMBARGO_SEC = 3600
DEFAULT_MIN_WFE_RATIO = 0.35
DEFAULT_MIN_DSR_PROB = 0.50


def _parse_ts_sec(raw: Any) -> float | None:
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
class WalkForwardSplit:
    fold_index: int
    train_start_ts: float
    train_end_ts: float
    embargo_end_ts: float
    test_start_ts: float
    test_end_ts: float


@dataclass(frozen=True)
class FoldEvaluation:
    fold_index: int
    selected_params: dict[str, Any]
    is_trades: int
    oos_trades: int
    is_sharpe: float
    oos_sharpe: float
    is_mean_return: float
    oos_mean_return: float
    wfe_ratio: float | None


@dataclass(frozen=True)
class WalkForwardSummary:
    folds_count: int
    total_oos_trades: int
    mean_is_sharpe: float
    mean_oos_sharpe: float
    pooled_oos_sharpe: float | None
    pooled_oos_psr: float | None
    pooled_oos_dsr: float | None
    wfe_ratio: float | None
    parameter_stability_score: float
    overfitting_detected: bool
    overfitting_reasons: tuple[str, ...]
    folds: tuple[FoldEvaluation, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "folds_count": self.folds_count,
            "total_oos_trades": self.total_oos_trades,
            "mean_is_sharpe": round(self.mean_is_sharpe, 4),
            "mean_oos_sharpe": round(self.mean_oos_sharpe, 4),
            "pooled_oos_sharpe": None if self.pooled_oos_sharpe is None else round(self.pooled_oos_sharpe, 4),
            "pooled_oos_psr": None if self.pooled_oos_psr is None else round(self.pooled_oos_psr, 4),
            "pooled_oos_dsr": None if self.pooled_oos_dsr is None else round(self.pooled_oos_dsr, 4),
            "wfe_ratio": None if self.wfe_ratio is None else round(self.wfe_ratio, 4),
            "parameter_stability_score": round(self.parameter_stability_score, 4),
            "overfitting_detected": self.overfitting_detected,
            "overfitting_reasons": list(self.overfitting_reasons),
        }


def build_walk_forward_splits(
    start_ts: float,
    end_ts: float,
    *,
    n_splits: int = 4,
    train_fraction: float = 0.65,
    embargo_sec: float = DEFAULT_EMBARGO_SEC,
    anchored: bool = False,
) -> list[WalkForwardSplit]:
    """Generate ``n_splits`` Purged & Embargoed Walk-Forward time splits."""
    total_span = float(end_ts) - float(start_ts)
    if n_splits < 1 or total_span <= 0 or not (0.20 <= train_fraction <= 0.90):
        return []
    # For rolling walk-forward, each fold advances by test_span
    # Total span = train_span + n_splits * test_span
    ratio = train_fraction / (1.0 - train_fraction)
    test_span = total_span / (ratio + n_splits)
    train_span = test_span * ratio
    if test_span <= embargo_sec:
        return []

    splits: list[WalkForwardSplit] = []
    for idx in range(n_splits):
        t_start = float(start_ts) if anchored else float(start_ts) + idx * test_span
        t_end = float(start_ts) + train_span + idx * test_span
        emb_end = t_end + max(0.0, float(embargo_sec))
        oos_start = emb_end
        oos_end = t_end + test_span
        if oos_end <= oos_start:
            continue
        splits.append(
            WalkForwardSplit(
                fold_index=idx + 1,
                train_start_ts=t_start,
                train_end_ts=t_end,
                embargo_end_ts=emb_end,
                test_start_ts=oos_start,
                test_end_ts=oos_end,
            )
        )
    return splits


def partition_trades_by_split(
    trades: Sequence[dict[str, Any]],
    split: WalkForwardSplit,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition trade records into Purged IS and Embargoed OOS sets.

    Any IS trade whose ``closed_ts`` (or ``exit_ts``) extends past ``split.train_end_ts``
    is purged from IS so no look-ahead leakage crosses into the OOS window.
    """
    is_set: list[dict[str, Any]] = []
    oos_set: list[dict[str, Any]] = []
    for t in trades:
        entry_ts = _parse_ts_sec(t.get("entry_ts") or t.get("opened_at") or t.get("created_at") or t.get("ts"))
        exit_ts = _parse_ts_sec(t.get("exit_ts") or t.get("closed_at") or t.get("updated_at") or entry_ts)
        if entry_ts is None or exit_ts is None:
            continue
        # Purged IS: both entry and exit must complete inside [train_start_ts, train_end_ts]
        if split.train_start_ts <= entry_ts <= split.train_end_ts and exit_ts <= split.train_end_ts:
            is_set.append(t)
        # Embargoed OOS: entry must start at or after test_start_ts (>= train_end_ts + embargo_sec)
        elif split.test_start_ts <= entry_ts <= split.test_end_ts:
            oos_set.append(t)
    return is_set, oos_set


def _sharpe_of_returns(rets: Sequence[float]) -> float:
    n = len(rets)
    if n < 2:
        return 0.0
    m = sum(rets) / n
    var = sum((r - m) ** 2 for r in rets) / (n - 1)
    std = math.sqrt(max(0.0, var))
    if std <= 1e-12:
        return 0.0
    return m / std


def evaluate_walk_forward_folds(
    fold_inputs: Sequence[dict[str, Any]],
    *,
    num_trials: int = 1,
    min_wfe_ratio: float = DEFAULT_MIN_WFE_RATIO,
    min_dsr_prob: float = DEFAULT_MIN_DSR_PROB,
) -> WalkForwardSummary:
    """Evaluate IS vs OOS fold returns, compute WFE & DSR, and detect overfitting.

    Each element in ``fold_inputs`` contains:
      - ``is_returns``: Sequence[float]
      - ``oos_returns``: Sequence[float]
      - ``selected_params``: dict[str, Any] (optional)
    """
    fold_evals: list[FoldEvaluation] = []
    pooled_oos: list[float] = []
    param_keys: list[str] = []

    for i, item in enumerate(fold_inputs, start=1):
        is_rets = [float(x) for x in (item.get("is_returns") or [])]
        oos_rets = [float(x) for x in (item.get("oos_returns") or [])]
        params = dict(item.get("selected_params") or {})
        param_keys.append(str(sorted(params.items())))

        is_sr = _sharpe_of_returns(is_rets)
        oos_sr = _sharpe_of_returns(oos_rets)
        is_mean = (sum(is_rets) / len(is_rets)) if is_rets else 0.0
        oos_mean = (sum(oos_rets) / len(oos_rets)) if oos_rets else 0.0
        wfe = (oos_sr / is_sr) if is_sr > 1e-6 else None

        pooled_oos.extend(oos_rets)
        fold_evals.append(
            FoldEvaluation(
                fold_index=i,
                selected_params=params,
                is_trades=len(is_rets),
                oos_trades=len(oos_rets),
                is_sharpe=is_sr,
                oos_sharpe=oos_sr,
                is_mean_return=is_mean,
                oos_mean_return=oos_mean,
                wfe_ratio=wfe,
            )
        )

    if not fold_evals:
        return WalkForwardSummary(
            folds_count=0,
            total_oos_trades=0,
            mean_is_sharpe=0.0,
            mean_oos_sharpe=0.0,
            pooled_oos_sharpe=None,
            pooled_oos_psr=None,
            pooled_oos_dsr=None,
            wfe_ratio=None,
            parameter_stability_score=1.0,
            overfitting_detected=True,
            overfitting_reasons=("no_walk_forward_folds",),
            folds=(),
        )

    mean_is_sr = sum(f.is_sharpe for f in fold_evals) / len(fold_evals)
    mean_oos_sr = sum(f.oos_sharpe for f in fold_evals) / len(fold_evals)
    overall_wfe = (mean_oos_sr / mean_is_sr) if mean_is_sr > 1e-6 else None

    # Parameter stability: fraction of folds sharing the modal parameter set
    unique_params = len(set(param_keys))
    stability = 1.0 if len(param_keys) <= 1 else max(0.0, 1.0 - (unique_params - 1) / len(param_keys))

    sr_stats = compute_psr_and_dsr(pooled_oos, num_trials=max(1, int(num_trials)))
    pooled_sr = sr_stats["sharpe_ratio"]
    pooled_psr = sr_stats["psr"]
    pooled_dsr = sr_stats["dsr"]

    reasons: list[str] = []
    if mean_oos_sr <= 0.0:
        reasons.append("oos_sharpe_non_positive")
    if overall_wfe is not None and overall_wfe < min_wfe_ratio:
        reasons.append("wfe_below_min_threshold")
    if pooled_dsr is not None and pooled_dsr < min_dsr_prob:
        reasons.append("deflated_sharpe_below_confidence")

    return WalkForwardSummary(
        folds_count=len(fold_evals),
        total_oos_trades=len(pooled_oos),
        mean_is_sharpe=mean_is_sr,
        mean_oos_sharpe=mean_oos_sr,
        pooled_oos_sharpe=pooled_sr,
        pooled_oos_psr=pooled_psr,
        pooled_oos_dsr=pooled_dsr,
        wfe_ratio=overall_wfe,
        parameter_stability_score=stability,
        overfitting_detected=len(reasons) > 0,
        overfitting_reasons=tuple(reasons),
        folds=tuple(fold_evals),
    )
