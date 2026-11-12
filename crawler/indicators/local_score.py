"""Local旁路 score that replaces jevs.fun for ranking.

Range is clipped to [-1, 1]. Inputs are fields already stored on the indicator
row, plus ``liquidity_usd`` when the caller already has it from ``pair_history``.
``jev_*`` is ignored. This module does not call jevs.fun and does not read
``INDICATORS_WEIGHT_*`` (those stay 0). The score is not an entry gate.

A missing leg contributes nothing. If volume_strength, change_5m, and
liquidity are all missing, the result is None — a spike alone does not
invent a rank.

Legs:

- volume_strength, centered at 1.0:
  ``clip((volume_strength - 1) / 4, -1, 1) * 0.35``
- change_5m in percent, passed through tanh so a rug cannot dominate:
  ``tanh(change_5m / 20) * 0.45``
- liquidity_usd tiers (unknown liquidity is not a penalty):
  ``< 10_000 → -0.15``; ``>= 50_000 → +0.10``; otherwise 0
- spike_ratio / spike, the social-burst flag already on the row:
  ratio above 1 contributes up to 0.25; ``spike == 1`` contributes at
  least 0.15; the combined penalty is capped at 0.30 and subtracted.
"""

from __future__ import annotations

import math
from typing import Any

# volume_strength == 1 is neutral. ±4 around that center fills the volume leg.
VOLUME_HALF_SPAN = 4.0
VOLUME_CAP = 0.35

# change_5m is a percent. 20% is one tanh unit; larger moves saturate.
CHANGE_SCALE = 20.0
CHANGE_CAP = 0.45

LIQ_THIN_USD = 10_000.0
LIQ_THICK_USD = 50_000.0
LIQ_THIN_PENALTY = 0.15
LIQ_THICK_BONUS = 0.10

SPIKE_RATIO_SPAN = 20.0
SPIKE_RATIO_CAP = 0.25
SPIKE_FLAG_PENALTY = 0.15
SPIKE_PENALTY_CAP = 0.30

# Columns on token_indicators that should refresh local_score when written.
LOCAL_SCORE_INPUTS = frozenset(
    {"volume_strength", "change_5m", "spike_ratio", "spike"}
)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _clip(number: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, number))


def _spike_flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return None
    try:
        return int(value) == 1
    except (TypeError, ValueError):
        return None


def compute_local_score(
    row: dict[str, Any],
    *,
    liquidity_usd: float | None = None,
) -> float | None:
    """Blend local volume, 5m change, liquidity tier, and spike penalty.

    ``row`` may contain unrelated keys, including legacy ``jev_*``. Those
    keys are not read.
    """
    volume = _finite(row.get("volume_strength"))
    change = _finite(row.get("change_5m"))
    liquidity = _finite(liquidity_usd)
    if volume is None and change is None and liquidity is None:
        return None

    total = 0.0
    if volume is not None:
        centered = _clip((volume - 1.0) / VOLUME_HALF_SPAN, -1.0, 1.0)
        total += centered * VOLUME_CAP
    if change is not None:
        total += math.tanh(change / CHANGE_SCALE) * CHANGE_CAP
    if liquidity is not None:
        if liquidity < LIQ_THIN_USD:
            total -= LIQ_THIN_PENALTY
        elif liquidity >= LIQ_THICK_USD:
            total += LIQ_THICK_BONUS

    penalty = 0.0
    spike_ratio = _finite(row.get("spike_ratio"))
    if spike_ratio is not None and spike_ratio > 1.0:
        penalty = _clip((spike_ratio - 1.0) / SPIKE_RATIO_SPAN, 0.0, SPIKE_RATIO_CAP)
    if _spike_flag(row.get("spike")) is True:
        penalty = max(penalty, SPIKE_FLAG_PENALTY)
    total -= min(penalty, SPIKE_PENALTY_CAP)
    return round(_clip(total, -1.0, 1.0), 6)
