"""Entry filters aligned with weekly JEV check (liquidity / coverage / min_price)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional


def safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


@dataclass
class FilterConfig:
    """Defaults match scripts/jev_validity.py / weekly check."""

    min_liq: float = 50_000.0
    min_coverage: float = 10.0
    min_price: float = 1e-6
    enabled: bool = True
    # When coverage is unavailable, fail-open (True) or fail-closed (False).
    # Weekly check fails closed; backtest default fail-open so tech-only runs
    # still work when jev_signals is sparse. CLI can set --strict-coverage.
    coverage_fail_open: bool = True

    @classmethod
    def disabled(cls) -> "FilterConfig":
        return cls(enabled=False)


@dataclass
class FilterResult:
    ok: bool
    reasons: list[str]
    liquidity: Optional[float] = None
    coverage: Optional[float] = None
    price: Optional[float] = None


def apply_entry_filters(
    *,
    price: Optional[float],
    liquidity: Optional[float],
    coverage: Optional[float],
    cfg: FilterConfig,
) -> FilterResult:
    """Return whether a candidate entry passes the configured hooks."""
    if not cfg.enabled:
        return FilterResult(ok=True, reasons=[], liquidity=liquidity, coverage=coverage, price=price)

    fails: list[str] = []
    px = safe_float(price)
    liq = safe_float(liquidity)
    cov = safe_float(coverage)

    if px is None:
        fails.append("no_price")
    elif px < cfg.min_price:
        fails.append(f"price<{cfg.min_price}")

    if liq is None:
        fails.append("no_liquidity")
    elif liq < cfg.min_liq:
        fails.append(f"liq<{cfg.min_liq:g}")

    if cov is None:
        if not cfg.coverage_fail_open:
            fails.append("no_coverage")
    elif cov < cfg.min_coverage:
        fails.append(f"coverage<{cfg.min_coverage:g}")

    return FilterResult(
        ok=not fails,
        reasons=fails,
        liquidity=liq,
        coverage=cov,
        price=px,
    )
