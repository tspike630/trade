"""Drop a live_bnb_boll_fade buy that is still under 20 USDT after the lift.

``lift_size_pct_for_entry_min`` runs first. A lifted quote is already at
least 20 and is kept. When no ``size_pct <= 1`` can clear the floor, this
prefilter refuses the buy so it never reaches ``decision_log``. A missing
or unparseable book fails open.

The floor and the book parser are ``live_bnb_size_floor``'s. That module
twins ``execution.executors.binance_spot_protect.ENTRY_MIN_NOTIONAL_USDT``
(``Decimal("20")``) and ``BOOK_ENV``. The check is the same strict ``<``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from decision_engine.strategies.live_bnb_size_floor import (
    BOOK_ENV,
    ENTRY_MIN_NOTIONAL_USDT,
    _size_decimal,
    parse_book_usdt,
)

REJECT_BELOW_MIN = "entry_notional_below_min_prefilter"

__all__ = [
    "BOOK_ENV",
    "ENTRY_MIN_NOTIONAL_USDT",
    "REJECT_BELOW_MIN",
    "live_buy_notional_skip",
]


def live_buy_notional_skip(
    size_pct: float,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[bool, str | None]:
    """Return ``(skip, warn)`` for one sized live buy after the min-notional lift.

    ``skip`` is true only when the book parses and ``size_pct * book`` is
    strictly under 20 USDT. ``warn`` is set when the book is missing or
    unparseable; ``skip`` is then false (fail-open).
    """
    env = os.environ if environ is None else environ
    raw = env.get(BOOK_ENV) if hasattr(env, "get") else None
    book = parse_book_usdt(raw)
    if book is None:
        return False, (
            f"{BOOK_ENV} missing or unparseable; "
            "live_bnb_boll_fade notional prefilter fail-open"
        )
    pct = _size_decimal(size_pct)
    if pct is None or pct > 1:
        return False, None
    if pct * book < ENTRY_MIN_NOTIONAL_USDT:
        return True, None
    return False, None
