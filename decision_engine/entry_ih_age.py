"""Entry indicator-history age gate for the shadow_entry_ih_ge1h sidebook.

``entry_ih_age_h`` is hours from signal time back to the latest
``indicator_history.recorded_at`` for that token. Pair lookup is used only
when the history table is not token-keyed. A missing or unparseable timestamp
is ``ih_hist_missing`` and is never stored as 0.

The resident live_candidate loop does not call this module. The launcher
(``scripts/decision_entry_ih_sidebook.sh`` / the watchdog sibling) reads
``DECISION_ENTRY_IH_SIDEBOOK`` and stays off unless that flag is ``1``.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Callable

ENTRY_IH_MIN_HOURS = 1.0
REJECT_IH_HIST_MISSING = "ih_hist_missing"
REJECT_IH_AGE_BELOW = "ih_age_below"
SIDEBOOK_ENV = "DECISION_ENTRY_IH_SIDEBOOK"

RecordedAtLookup = Callable[..., Any]


def _as_utc(when: datetime) -> datetime:
    if when.tzinfo is None:
        return when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def parse_recorded_at(value: Any) -> datetime | None:
    """Parse an indicator_history timestamp. Blank or garbage is missing, not zero."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def entry_ih_age_hours(recorded_at: Any, *, now: datetime) -> float | None:
    """Hours between ``now`` and ``recorded_at``. None when history is unusable."""
    recorded = parse_recorded_at(recorded_at)
    if recorded is None:
        return None
    hours = (_as_utc(now) - recorded).total_seconds() / 3600.0
    if not math.isfinite(hours):
        return None
    return hours


def filter_buys_by_entry_ih_age(
    signals: list[dict[str, Any]],
    recorded_at_for: RecordedAtLookup,
    *,
    min_hours: float = ENTRY_IH_MIN_HOURS,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Keep buys whose estimated entry_ih_age_h is at least ``min_hours``.

    Non-buys pass through and do not touch the lookup. Rejected buys are
    omitted. Missing history increments ``ih_hist_missing`` and does not
    stamp ``entry_ih_age_h``. A real age below the floor increments
    ``ih_age_below``.
    """
    when = _as_utc(now or datetime.now(timezone.utc))
    floor = float(min_hours)
    kept: list[dict[str, Any]] = []
    rejected: dict[str, int] = {}
    for sig in signals:
        if str(sig.get("action") or "").lower() != "buy":
            kept.append(sig)
            continue
        recorded = recorded_at_for(
            str(sig.get("chain_id") or ""),
            str(sig.get("token_address") or ""),
            sig.get("pair_address"),
        )
        age_h = entry_ih_age_hours(recorded, now=when)
        if age_h is None:
            rejected[REJECT_IH_HIST_MISSING] = rejected.get(REJECT_IH_HIST_MISSING, 0) + 1
            continue
        if age_h < floor:
            rejected[REJECT_IH_AGE_BELOW] = rejected.get(REJECT_IH_AGE_BELOW, 0) + 1
            continue
        stamped = dict(sig)
        stamped["entry_ih_age_h"] = round(age_h, 4)
        kept.append(stamped)
    return kept, rejected
