"""复盘优化层 R1+R2+R3+§10：只读信号链路聚合、周报、归因、寻优脚手架与进化建议（零侵入业务库）。"""

from review.attribution import fill_attribution, signal_attribution
from review.data import (
    DbPaths,
    SignalChainRow,
    annotate_forward_returns,
    annotate_token_age,
    load_ccxt_market_baseline,
    load_market_baseline,
    load_signal_chain,
    summarize_execution,
)
from review.metrics import compute_signal_metrics, slice_jev_filtered
from review.optimizer import (
    OptimizeResult,
    build_param_grid,
    format_optimize_summary,
    optimize,
    score_robustness,
    time_split,
)
from review.report import render_weekly_md, write_report
from review.evolution import build_evolution_suggestions, format_evolution_section
from review.joint import joint_jev_age_prior

__all__ = [
    "DbPaths",
    "OptimizeResult",
    "SignalChainRow",
    "annotate_forward_returns",
    "annotate_token_age",
    "build_evolution_suggestions",
    "build_param_grid",
    "format_evolution_section",
    "compute_signal_metrics",
    "fill_attribution",
    "format_optimize_summary",
    "load_ccxt_market_baseline",
    "load_market_baseline",
    "load_signal_chain",
    "optimize",
    "render_weekly_md",
    "score_robustness",
    "signal_attribution",
    "slice_jev_filtered",
    "summarize_execution",
    "time_split",
    "joint_jev_age_prior",
    "write_report",
]
