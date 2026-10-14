"""Shared sample floors and 1h rate lines.

Used by weekly-check (`scripts/jev_validity.py`), `review.evolution`, and
`review.factor_loop`. Passing a gate does not activate a strategy:
`factor_loop` status max remains ``accepted``.

``factor_loop`` ``n_raw`` is the parameter-scan count from
``jev_validity.run_analysis`` (buys under one candidate's strength /
liquidity / coverage). Weekly activation still uses the weekly-check
sample: filtered decision_log n and unique_token for that week, not this
scan's ``n_raw``.
"""

from __future__ import annotations

import math
from typing import Any

# Sample floors (weekly-check / jev_validity). Do not lower these for M-1.
MIN_RAW_N = 15
MIN_UNIQUE_TOKEN = 10

# Filtered 1h up-rate lines.
UP_1H_PROMOTE = 0.55  # >= promote → candidate can be accepted
UP_1H_DEGRADE = 0.50  # < degrade → weak / reject

# Weeks that must clear the promote line before Step2.
CONSECUTIVE_WEEKS = 2

# Names factor_loop already used. The old 40% retry line is the degrade line.
GATE_HARD_N = MIN_RAW_N
GATE_UNIQUE = MIN_UNIQUE_TOKEN
GATE_UP_1H = UP_1H_PROMOTE
GATE_UP_1H_DEGRADE = UP_1H_DEGRADE

SAMPLE_NOTE = (
    "factor_loop 的 n_raw 是参数扫描样本（jev_validity.run_analysis 在该候选 "
    "strength/liq/coverage 下的过滤前计数），不等于 decision_log 周检信号样本。"
    "激活仍走周检口径（过滤后 n≥"
    f"{MIN_RAW_N} 且 unique_token≥{MIN_UNIQUE_TOKEN}，连续 {CONSECUTIVE_WEEKS} 周 "
    f"1h≥{UP_1H_PROMOTE:.0%}）；本循环过门槛最多 status=accepted，不自动激活。"
)

# M-2. factor_loop.gate_candidate must not call calibrate_direction yet.
# metrics["ic_mean"] is _latest_ic (often null, not this candidate's rank IC).
# Fail-closed wiring would change weekly accept/reject. Weights stay 0.
# A finite IC may be appended to the observe-only jsonl by the weekly path.
# That append does not change accepted.
DIRECTION_CALIBRATION_TODO = (
    "M-2: A-class accept requires calibrate_direction on a per-candidate "
    "rank IC (both signs recorded; keep kept_sign). Not wired into "
    "factor_loop.gate_candidate: ic_mean is the latest history row, not a "
    "both-sign eval of this candidate. Wiring fail-closed would change "
    "weekly accept/reject while that IC is null. Observe-only jsonl may "
    "record both signs when that IC is finite; it does not change accepted. "
    "Weights stay 0. "
    "不算有效性。"
)


def calibrate_direction(ic: object) -> dict[str, Any]:
    """Record both signs of one computed rank IC (AQuA / M-2).

    A finite int/float stores the written value and its flip (``-ic``) and
    keeps the orientation whose IC is >= 0 (``kept_sign`` +1, else -1).
    Missing, non-numeric, bool, or non-finite input fails closed:
    ``passed`` is False and no sign is registered.

    ``passed`` means both signs were recorded. It does not accept a
    candidate, move ``MIN_RAW_N``, or change weights. Weekly
    ``gate_candidate`` does not call this. The factor-loop weekly path may
    append an observe-only sidecar when IC is finite; that append does not
    change ``accepted``. See ``DIRECTION_CALIBRATION_TODO``.
    """
    if isinstance(ic, bool) or not isinstance(ic, (int, float)) or not math.isfinite(ic):
        return {
            "passed": False,
            "fail_closed": True,
            "blocks_a_class": True,
            "ic_as_written": None,
            "ic_flipped": None,
            "kept_sign": None,
            "cleaner_ic": None,
            "reason": "direction_calibration_missing",
        }
    written = float(ic)
    flipped = -written
    if written >= 0.0:
        kept_sign = 1
        cleaner = written
    else:
        kept_sign = -1
        cleaner = flipped
    return {
        "passed": True,
        "fail_closed": False,
        "blocks_a_class": False,
        "ic_as_written": written,
        "ic_flipped": flipped,
        "kept_sign": kept_sign,
        "cleaner_ic": cleaner,
        "reason": "both_signs_recorded",
    }
