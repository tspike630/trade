"""G-3 bankruptcy count. Read-only. Does not change the daily-loss limit.

``max_sequential_losses = floor(capital / per_trade_risk)``
``per_trade_risk = size_usd * (sl_pct / 100)``

Defaults match the code, not a live book: capital 10000 (``PAPER_START_CASH``
default), size 5% of capital (decision position fraction), stop 5%
(``execution.exits.DEFAULT_SL_PCT``). This does not read or write
``EXECUTION_DAILY_LOSS_USD``.
"""

from __future__ import annotations

import argparse
import math
from typing import Any

from execution.exits import DEFAULT_SL_PCT

# Code defaults. Not loaded from a paper database and not a new risk gate.
DEFAULT_CAPITAL_USD = 10_000.0
DEFAULT_SIZE_PCT = 0.05


def per_trade_risk_usd(size_usd: float, sl_pct: float = DEFAULT_SL_PCT) -> float:
    """Nominal loss if the stop fills at ``sl_pct`` of notional. Slippage is extra."""
    return float(size_usd) * (float(sl_pct) / 100.0)


def max_sequential_losses(capital: float, per_trade_risk: float) -> int:
    """How many full stop-outs fit in ``capital``. Zero risk is rejected."""
    cap = float(capital)
    risk = float(per_trade_risk)
    if cap < 0:
        raise ValueError("capital must be >= 0")
    if risk <= 0:
        raise ValueError("per_trade_risk must be > 0")
    return int(math.floor(cap / risk))


def bankruptcy_report(
    *,
    capital: float = DEFAULT_CAPITAL_USD,
    size_usd: float | None = None,
    size_pct: float = DEFAULT_SIZE_PCT,
    sl_pct: float = DEFAULT_SL_PCT,
) -> dict[str, Any]:
    """One weekly-check row. ``size_usd`` wins when passed; else ``capital * size_pct``."""
    cap = float(capital)
    notional = float(size_usd) if size_usd is not None else cap * float(size_pct)
    stop = float(sl_pct)
    risk = per_trade_risk_usd(notional, stop)
    return {
        "capital": cap,
        "size_usd": notional,
        "size_pct": None if size_usd is not None else float(size_pct),
        "sl_pct": stop,
        "per_trade_risk": risk,
        "max_sequential_losses": max_sequential_losses(cap, risk),
    }


def _format(row: dict[str, Any]) -> str:
    return (
        f"capital={row['capital']:.4g} size_usd={row['size_usd']:.4g} "
        f"sl_pct={row['sl_pct']:.4g} per_trade_risk={row['per_trade_risk']:.4g} "
        f"max_sequential_losses={row['max_sequential_losses']}\n"
        "EXECUTION_DAILY_LOSS_USD unchanged (this command does not read or set it).\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "G-3: theoretical max sequential stop-outs. "
            "Does not change exits or EXECUTION_DAILY_LOSS_USD."
        )
    )
    parser.add_argument("--capital", type=float, default=DEFAULT_CAPITAL_USD)
    parser.add_argument(
        "--size-usd",
        type=float,
        default=None,
        help="notional per trade; default is capital * --size-pct",
    )
    parser.add_argument("--size-pct", type=float, default=DEFAULT_SIZE_PCT)
    parser.add_argument("--sl-pct", type=float, default=DEFAULT_SL_PCT)
    args = parser.parse_args(argv)
    try:
        row = bankruptcy_report(
            capital=args.capital,
            size_usd=args.size_usd,
            size_pct=args.size_pct,
            sl_pct=args.sl_pct,
        )
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    print(_format(row), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
