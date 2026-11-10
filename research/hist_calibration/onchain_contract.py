"""Decision-side input contract for a future OnchainCalibration.

This does not enable a live cut and does not read x_watch as history.
Collection owns archive and CCXT downloads. Review owns the out-of-sample
statistics. This module only checks identity, causality, and time validity
of rows those runs may hand to the decision engine.
"""

from __future__ import annotations

from typing import Any, Mapping

from research.hist_calibration.study import (
    MIN_FEATURE_COVERAGE,
    MIN_TEST_EVENTS,
    decide,
)

BOOK_DEPTH_SERIES = "book_depth_percentage_band"
X_WATCH_SOURCE = "x_watch"
EVIDENCE_MAX_AGE_MS = 90 * 60 * 1000
REQUIRED_SERIES = ("obi_top5", "taker_buy_ratio", "whale_retail_divergence", "heat_history")


def validate_evidence_point(point: Mapping[str, Any]) -> dict[str, Any]:
    """One lagged feature. A failure stays out of any calibration object."""
    errors: list[str] = []
    series = str(point.get("series") or "")
    source = str(point.get("source_id") or "")
    instrument = str(point.get("instrument") or "")
    if not source:
        errors.append("missing_source_id")
    if series == "obi_top5" and instrument == BOOK_DEPTH_SERIES:
        errors.append("book_depth_is_not_obi_top5")
    if series == "heat_history" and source == X_WATCH_SOURCE:
        errors.append("x_watch_not_historical")
    try:
        ts_ms = int(point["ts_ms"])
        event_ms = int(point["event_ms"])
    except (KeyError, TypeError, ValueError):
        errors.append("missing_time")
        ts_ms = event_ms = None
    if ts_ms is not None and event_ms is not None:
        if ts_ms > event_ms:
            errors.append("feature_after_event")
        max_age = int(point.get("max_age_ms") or EVIDENCE_MAX_AGE_MS)
        if event_ms - ts_ms > max_age:
            errors.append("feature_outside_window")
    if point.get("value") is None:
        errors.append("missing_value")
    return {
        "ok": not errors,
        "errors": errors,
        "x_watch_enabled": False,
        "calibration_constructed": False,
    }


def review_inputs_to_decision(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Turn a review payload into the decision gate result.

    ``payload`` may count coverage. It must not ask this package to enable
    x_watch or to treat book depth as top-5. The live router is not edited.
    """
    events = []
    for raw in payload.get("events") or []:
        item = dict(raw)
        if payload.get("obi_instrument") == BOOK_DEPTH_SERIES:
            item["obi_top5_asof"] = None
        events.append(item)
    body = decide(
        {
            "events": events,
            "news_availability": payload.get("news_availability") or "missing_db",
            "late_archive_rows": payload.get("late_archive_rows"),
            "raw_rows": payload.get("raw_rows"),
            "book_depth_used_as_obi": payload.get("obi_instrument") == BOOK_DEPTH_SERIES,
            "obi_from_book_depth": payload.get("obi_instrument") == BOOK_DEPTH_SERIES,
            "x_watch_used_as_history": bool(payload.get("x_watch_used_as_history")),
            "scope": "decision_input_contract",
        }
    )
    body["x_watch_enabled"] = False
    body["live_router_changed"] = False
    body["minimum_gates"] = {
        "test_split_events": MIN_TEST_EVENTS,
        "asof_feature_coverage": MIN_FEATURE_COVERAGE,
        "required_series": list(REQUIRED_SERIES),
    }
    body["evidence_pending"] = not body["gates_passed"]
    return body
