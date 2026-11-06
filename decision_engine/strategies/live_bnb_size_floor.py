"""Raise a live_bnb_boll_fade ``size_pct`` until it clears 20 USDT.

The entry zone still decides the multiplier. Neutral stays ``0.5`` because
that half size is the risk choice (there is no inverse-stop-width sizer),
not a Binance minimum. This module runs after that decision, and only the
live emitter calls it.

``quote = size_pct * BINANCE_SPOT_BOOK_USDT``. When the quote is strictly
under 20 USDT, ``size_pct`` becomes the smallest float whose
``Decimal(str(float)) * book`` is at least 20 and still ``<= 1``. Execution
reads ``size_pct`` back the same way (``binance_spot._to_decimal``), then
floors the quote to 8 decimal places. A product that is already ``>= 20``
stays put, so a large-trade ``0.05`` on a book near 437.64 is unchanged.

A missing or unparseable book does not invent a size. A floor that would
need ``size_pct > 1`` is left alone. ``ENTRY_MIN_NOTIONAL_USDT`` stays 20.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from decimal import Decimal

# Twin of execution.executors.binance_spot_protect.ENTRY_MIN_NOTIONAL_USDT.
ENTRY_MIN_NOTIONAL_USDT = Decimal("20")
# Twin of execution.executors.binance_spot_protect.BOOK_ENV.
BOOK_ENV = "BINANCE_SPOT_BOOK_USDT"

# Same token shape as execution.executors.binance_spot._DECIMAL.
_DECIMAL = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d+)?$")
_WALK_STEPS = 64


def parse_book_usdt(raw: object) -> Decimal | None:
    """Positive book, or None when the value is missing or unparseable.

    Matches execution ``book_usdt`` / ``_to_decimal`` for an env string:
    strip, the decimal pattern, finite, and strictly positive.
    """
    if isinstance(raw, bool) or raw is None:
        return None
    text = str(raw).strip()
    if not text or _DECIMAL.fullmatch(text) is None:
        return None
    number = Decimal(text)
    if not number.is_finite() or number <= 0:
        return None
    return number


def _size_decimal(size_pct: float) -> Decimal | None:
    """Float ``size_pct`` the way execution ``_to_decimal`` reads it."""
    if isinstance(size_pct, bool) or not isinstance(size_pct, (int, float)):
        return None
    if isinstance(size_pct, float):
        if not math.isfinite(size_pct) or size_pct <= 0:
            return None
        return Decimal(str(size_pct))
    if size_pct <= 0:
        return None
    return Decimal(size_pct)


def _quote_clears(size_pct: float, book: Decimal) -> bool:
    pct = _size_decimal(size_pct)
    if pct is None or pct > 1:
        return False
    return pct * book >= ENTRY_MIN_NOTIONAL_USDT


def _min_clearing_size(book: Decimal) -> float | None:
    """Smallest float in ``(0, 1]`` whose decimal product clears 20 USDT."""
    if book <= 0:
        return None
    raw = ENTRY_MIN_NOTIONAL_USDT / book
    if raw > 1:
        return None
    candidate = float(raw)
    if candidate > 1.0:
        candidate = 1.0
    if candidate <= 0.0:
        candidate = math.nextafter(0.0, 1.0)
    for _ in range(_WALK_STEPS):
        if candidate > 1.0:
            return 1.0 if _quote_clears(1.0, book) else None
        if _quote_clears(candidate, book):
            return candidate
        nxt = math.nextafter(candidate, 2.0)
        if nxt == candidate or nxt <= 0.0:
            return None
        candidate = nxt
    return None


def lift_size_pct_for_entry_min(
    size_pct: float,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[float, bool]:
    """Return ``(size_pct, lifted)``.

    ``lifted`` is true only when ``size_pct`` was raised so the quote clears
    20 USDT. Already-clear, missing-book, and impossible (``20/book > 1``)
    sizes come back unchanged.
    """
    current = _size_decimal(size_pct)
    if current is None or current > 1:
        return size_pct, False
    env = os.environ if environ is None else environ
    raw = env.get(BOOK_ENV) if hasattr(env, "get") else None
    book = parse_book_usdt(raw)
    if book is None or _quote_clears(size_pct, book):
        return size_pct, False
    lifted = _min_clearing_size(book)
    if lifted is None or not (0.0 < lifted <= 1.0) or not _quote_clears(lifted, book):
        return size_pct, False
    if lifted <= size_pct:
        return size_pct, False
    return lifted, True
