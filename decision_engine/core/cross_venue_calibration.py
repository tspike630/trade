"""Frozen cross-venue wick and breakout study.

Same base and same quote only. USD is not treated as USDT. A missing peer
bar is a gap, not a filled price. The study does not unlock the news gate.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from decision_engine.core.cross_venue_deviation import gap_counts
from decision_engine.core.news_oos_calibration import (
    MIN_TEST_CLUSTERS,
    bootstrap_treated,
    holm,
)

SCHEMA_VERSION = "cross_venue_v1"
STEP_MS = 300_000
WICK_BPS = 30.0
PEER_QUIET_BPS = 10.0
BREAKOUT_LOOKBACK = 12
FORWARD_BARS = 3
COST_BPS = 10.0
EMBARGO_MS = FORWARD_BARS * STEP_MS


def _bps(move: float, close: float) -> float | None:
    if close == 0:
        return None
    return move / close * 10_000.0


def _up_wick(bar: Mapping[str, float]) -> bool:
    body_top = max(bar["open"], bar["close"])
    size = _bps(bar["high"] - body_top, bar["close"])
    return size is not None and size >= WICK_BPS


def _down_wick(bar: Mapping[str, float]) -> bool:
    body_bottom = min(bar["open"], bar["close"])
    size = _bps(body_bottom - bar["low"], bar["close"])
    return size is not None and size >= WICK_BPS


def _quiet(bar: Mapping[str, float]) -> bool:
    up = _bps(bar["high"] - max(bar["open"], bar["close"]), bar["close"])
    down = _bps(min(bar["open"], bar["close"]) - bar["low"], bar["close"])
    return up is not None and down is not None and up < PEER_QUIET_BPS and down < PEER_QUIET_BPS


def label_aligned_events(
    left: Mapping[int, Mapping[str, float]],
    right: Mapping[int, Mapping[str, float]],
) -> list[dict[str, object]]:
    """Label only timestamps present on both venues, with a full lookback and exit."""
    common = sorted(set(left) & set(right))
    events: list[dict[str, object]] = []
    for index, open_ms in enumerate(common):
        if index < BREAKOUT_LOOKBACK or index + FORWARD_BARS >= len(common):
            continue
        prior = common[index - BREAKOUT_LOOKBACK : index]
        future = common[index + 1 : index + 1 + FORWARD_BARS]
        if prior[-1] + STEP_MS != open_ms:
            continue
        if any(future[i] != open_ms + (i + 1) * STEP_MS for i in range(FORWARD_BARS)):
            continue
        if any(prior[i] != open_ms - (BREAKOUT_LOOKBACK - i) * STEP_MS for i in range(BREAKOUT_LOOKBACK)):
            continue
        bar = left[open_ms]
        peer = right[open_ms]
        exit_bar = left[future[-1]]
        gross = (exit_bar["close"] - bar["close"]) / bar["close"] * 10_000.0 if bar["close"] else None
        if gross is None:
            continue
        prior_high = max(left[ts]["close"] for ts in prior)
        peer_prior_high = max(right[ts]["close"] for ts in prior)
        wick = (_up_wick(bar) or _down_wick(bar)) and _quiet(peer)
        breakout = bar["close"] > prior_high and peer["close"] <= peer_prior_high
        events.append(
            {
                "open_time_ms": open_ms,
                "wick": wick,
                "breakout": breakout,
                "return_bps": gross - COST_BPS,
            }
        )
    return events


def _purge(events: Sequence[Mapping[str, object]], boundary_ms: int | None) -> list[dict[str, object]]:
    kept: list[dict[str, object]] = []
    for event in events:
        open_ms = int(event["open_time_ms"])
        if boundary_ms is not None and open_ms + EMBARGO_MS > boundary_ms:
            continue
        kept.append(dict(event))
    return kept


def evaluate_cross_venue(
    left: Mapping[int, Mapping[str, float]],
    right: Mapping[int, Mapping[str, float]],
    *,
    quote: str,
    synthetic: bool = False,
) -> dict[str, object]:
    """Time-ordered study. Fewer than 30 test clusters is insufficient."""
    gaps = gap_counts(left, right)
    events = label_aligned_events(left, right)
    ordered = sorted(events, key=lambda item: int(item["open_time_ms"]))
    count = len(ordered)
    train_end = int(count * 0.60)
    valid_end = int(count * 0.80)
    train = ordered[:train_end]
    valid = ordered[train_end:valid_end]
    test = ordered[valid_end:]
    valid_start = int(valid[0]["open_time_ms"]) if valid else None
    test_start = int(test[0]["open_time_ms"]) if test else None
    train = _purge(train, valid_start)
    valid = _purge(valid, test_start)
    sample = [
        (str(item["open_time_ms"]), bool(item["wick"]), float(item["return_bps"]))
        for item in test
    ]
    breakout_sample = [
        (str(item["open_time_ms"]), bool(item["breakout"]), float(item["return_bps"]))
        for item in test
    ]
    wick_stats = bootstrap_treated(sample, reps=200)
    breakout_stats = bootstrap_treated(breakout_sample, reps=200)
    p_values = [item["p_value"] for item in (wick_stats, breakout_stats) if item["p_value"] is not None]
    adjusted = holm([float(item) for item in p_values]) if p_values else []
    enough = len(test) >= MIN_TEST_CLUSTERS
    status = "insufficient" if not enough or synthetic else "estimated_not_a_news_unlock"
    if synthetic:
        status = "synthetic_not_a_calibration"
    return {
        "schema_version": SCHEMA_VERSION,
        "quote": quote,
        "status": status,
        "unlock_news_gate": False,
        "gaps": gaps,
        "cost_bps_round_trip": COST_BPS,
        "wick_definition_bps": WICK_BPS,
        "peer_quiet_bps": PEER_QUIET_BPS,
        "breakout_lookback_bars": BREAKOUT_LOOKBACK,
        "forward_bars": FORWARD_BARS,
        "embargo_ms": EMBARGO_MS,
        "train": len(train),
        "validation": len(valid),
        "test_clusters": len(test),
        "holm_p": adjusted,
        "wick": wick_stats,
        "breakout": breakout_stats,
        "interpolation": "none",
    }


def usd_not_calibrated(reason: str) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "quote": "USD",
        "status": "not_calibrated",
        "reason": reason,
        "unlock_news_gate": False,
    }
