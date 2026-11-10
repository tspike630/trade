"""Causality, walk-forward, and the decision not to unlock a calibration.

Counts come from the database passed in. The figures 1859 and 164 are not
inputs. ``x_watch`` is not a historical series. Percentage-band book depth
is not ``obi_top5``.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

# Mirrors decision_engine.core.news_shock_audit and news_shock_gate.
# research.tests.test_hist_calibration asserts these stay equal to those names.
MIN_TEST_EVENTS = 30
MIN_FEATURE_COVERAGE = 0.8
LATE_ARCHIVE_LAG_MIN = 90
TRAIN_FRACTION = 0.60
VALIDATION_FRACTION = 0.20
PURGE_MS = 24 * 60 * 60 * 1000
BLOCK_MS = 7 * 24 * 60 * 60 * 1000
FAMILY_SIZE = 1


def _parse_ts(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    return int(value.timestamp() * 1000)


def inventory_news(db_path: Path) -> dict[str, Any]:
    """Read-only counts. Does not update the news database."""
    path = Path(db_path)
    info: dict[str, Any] = {
        "path": str(path),
        "claimed_1859_not_used": True,
        "claimed_164_not_used": True,
    }
    if not path.is_file():
        info["availability"] = "missing_db"
        info["rows"] = None
        info["late_archive_rows"] = None
        return info
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        names = {row[1] for row in conn.execute("PRAGMA table_info(news_events)")}
        required = {"fetched_at", "published_at", "source_tier", "title", "url"}
        if not required.issubset(names):
            info["availability"] = "schema_error"
            info["missing_columns"] = sorted(required - names)
            return info
        rows = conn.execute(
            "SELECT source_tier, published_at, fetched_at, title, url, onchain_status FROM news_events"
        ).fetchall()
    finally:
        conn.close()
    late = whitelist = rumor = bad_clock = 0
    tradable = []
    for row in rows:
        tier = str(row["source_tier"] or "").strip().lower()
        if tier != "whitelist":
            rumor += 1
            continue
        whitelist += 1
        published = _parse_ts(row["published_at"])
        fetched = _parse_ts(row["fetched_at"])
        if published is None or fetched is None:
            bad_clock += 1
            continue
        lag_min = (fetched - published).total_seconds() / 60.0
        is_late = lag_min > LATE_ARCHIVE_LAG_MIN
        if is_late:
            late += 1
            continue
        tradable.append(
            {
                "url": str(row["url"] or ""),
                "title": str(row["title"] or ""),
                "fetched_at": fetched.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "event_ms": _ms(fetched),
                "onchain_status": str(row["onchain_status"] or ""),
            }
        )
    info.update(
        {
            "availability": "ok",
            "rows": len(rows),
            "whitelist_rows": whitelist,
            "non_whitelist_rows": rumor,
            "bad_clock_rows": bad_clock,
            "late_archive_rows": late,
            "tradable_rows": len(tradable),
            "events": tradable,
        }
    )
    return info


def asof_value(points: Sequence[tuple[int, float]], event_ms: int, max_age_ms: int) -> float | None:
    """Latest feature at or before the event, inside the evidence window."""
    chosen: tuple[int, float] | None = None
    for ts_ms, value in points:
        if ts_ms > event_ms:
            continue
        if event_ms - ts_ms > max_age_ms:
            continue
        if chosen is None or ts_ms > chosen[0]:
            chosen = (ts_ms, value)
    if chosen is None:
        return None
    return chosen[1]


def forward_return(bars: Sequence[tuple[int, float]], event_ms: int, horizon_ms: int) -> float | None:
    """Entry is the first open at or after the event. Exit is the first open at or after entry horizon.

    A bar that opened earlier is not used, even if it was still forming at the event.
    """
    opens = sorted((int(ts), float(px)) for ts, px in bars)
    entry = next((item for item in opens if item[0] >= event_ms), None)
    if entry is None or entry[1] == 0:
        return None
    exit_at = entry[0] + horizon_ms
    exit_bar = next((item for item in opens if item[0] >= exit_at), None)
    if exit_bar is None:
        return None
    return (exit_bar[1] - entry[1]) / entry[1]


def time_split(events: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    ordered = sorted(events, key=lambda item: int(item["event_ms"]))
    count = len(ordered)
    train_end = int(count * TRAIN_FRACTION)
    valid_end = train_end + int(count * VALIDATION_FRACTION)
    return {
        "train": list(ordered[:train_end]),
        "validation": list(ordered[train_end:valid_end]),
        "test": list(ordered[valid_end:]),
    }


def train_only_threshold(train_values: Sequence[float]) -> float | None:
    """A cut may be read from train. Holdout values are not an argument."""
    if not train_values:
        return None
    ordered = sorted(train_values)
    return ordered[len(ordered) // 2]


def permutation_effect(
    outcomes: Sequence[float],
    labels: Sequence[int],
    *,
    seed: int,
) -> float | None:
    """One shuffle of labels against fixed outcomes.

    The seed is an input. It is not searched for a smaller p-value.
    """
    if not outcomes or len(outcomes) != len(labels):
        return None
    draw = list(labels)
    random.Random(seed).shuffle(draw)
    pos = [value for value, label in zip(outcomes, draw) if label]
    neg = [value for value, label in zip(outcomes, draw) if not label]
    if not pos or not neg:
        return None
    return sum(pos) / len(pos) - sum(neg) / len(neg)


def purged_blocks(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Walk forward in 7-day blocks. The day before each test block is purged."""
    ordered = sorted(events, key=lambda item: int(item["event_ms"]))
    if not ordered:
        return []
    start = int(ordered[0]["event_ms"])
    end = int(ordered[-1]["event_ms"])
    blocks = []
    cursor = start
    index = 0
    while cursor <= end:
        block_end = cursor + BLOCK_MS
        test = [item for item in ordered if cursor <= int(item["event_ms"]) < block_end]
        train = [item for item in ordered if int(item["event_ms"]) < cursor - PURGE_MS]
        blocks.append({"index": index, "train": len(train), "test": len(test), "purged_ms": PURGE_MS})
        cursor = block_end
        index += 1
    return blocks


def cluster_means(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Mean of per-day and per-title cluster means. Not a searched p-value."""
    by_day: dict[str, list[float]] = {}
    by_title: dict[str, list[float]] = {}
    for row in rows:
        value = row.get("forward_return")
        if value is None:
            continue
        day = datetime.fromtimestamp(int(row["event_ms"]) / 1000.0, tz=timezone.utc).date().isoformat()
        key = f"{day}:{row.get('symbol') or 'unspecified'}"
        by_day.setdefault(key, []).append(float(value))
        title = " ".join(str(row.get("title") or "").casefold().split())
        by_title.setdefault(title or "untitled", []).append(float(value))
    def _mean_of_means(groups: dict[str, list[float]]) -> float | None:
        if not groups:
            return None
        means = [sum(items) / len(items) for items in groups.values() if items]
        if not means:
            return None
        return sum(means) / len(means)
    return {
        "day_symbol_clusters": len(by_day),
        "title_clusters": len(by_title),
        "day_symbol_mean": _mean_of_means(by_day),
        "title_mean": _mean_of_means(by_title),
        "multiple_testing_family": FAMILY_SIZE,
        "note": "Family size is declared before looking at holdout. This run does not pick a horizon.",
    }


def book_depth_is_not_obi(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {
        "series": "book_depth_percentage_band",
        "rows": len(rows),
        "obi_top5": None,
        "usable_as_obi_top5": False,
    }


def cross_venue_dislocation(
    left: Sequence[tuple[int, float, float, float]],
    right: Sequence[tuple[int, float, float, float]],
    *,
    left_quote: str,
    right_quote: str,
) -> dict[str, Any]:
    """Closed 5m bars aligned on open time.

    Each tuple is ``(open_ms, open, high, low)``. USD and USDT are reported
    as a labeled basis, not as one return series.
    """
    right_by_ts = {int(ts): (open_, high, low) for ts, open_, high, low in right}
    paired = []
    missing_right = 0
    for ts, open_, high, low in left:
        other = right_by_ts.get(int(ts))
        if other is None:
            missing_right += 1
            continue
        if open_ == 0 or other[0] == 0:
            continue
        wick = (high - low) / open_
        other_wick = (other[1] - other[2]) / other[0]
        paired.append(
            {
                "open_ms": int(ts),
                "wick_gap": wick - other_wick,
                "high_dislocation": (high - other[1]) / ((open_ + other[0]) / 2.0),
            }
        )
    comparable = left_quote == right_quote
    return {
        "left_quote": left_quote,
        "right_quote": right_quote,
        "quotes_pooled": False,
        "comparable_without_basis": comparable,
        "basis_label": None if comparable else f"{left_quote}_vs_{right_quote}",
        "aligned_bars": len(paired),
        "missing_right": missing_right,
        "mean_wick_gap": None if not paired else sum(item["wick_gap"] for item in paired) / len(paired),
        "mean_high_dislocation": None
        if not paired
        else sum(item["high_dislocation"] for item in paired) / len(paired),
    }


def _coverage(events: Sequence[dict[str, Any]], key: str) -> float:
    if not events:
        return 0.0
    return sum(1 for event in events if event.get(key) is not None) / len(events)


def decide(report_inputs: dict[str, Any]) -> dict[str, Any]:
    """Apply the existing sample gates. Do not relax them to obtain a cut."""
    events = list(report_inputs.get("events") or [])
    splits = time_split(events)
    test_n = len(splits["test"])
    obi_cov = _coverage(events, "obi_top5_asof")
    taker_cov = _coverage(events, "taker_asof")
    whale_cov = _coverage(events, "whale_asof")
    heat_cov = _coverage(events, "heat_delta")
    failed = []
    if report_inputs.get("news_availability") != "ok":
        failed.append("news_events_unreadable")
    if test_n < MIN_TEST_EVENTS:
        failed.append(f"test_split_below_{MIN_TEST_EVENTS}")
    if min(obi_cov, taker_cov, whale_cov, heat_cov) < MIN_FEATURE_COVERAGE:
        failed.append(f"asof_feature_coverage_below_{MIN_FEATURE_COVERAGE}")
    if report_inputs.get("book_depth_used_as_obi"):
        failed.append("book_depth_is_not_obi_top5")
    if report_inputs.get("x_watch_used_as_history"):
        failed.append("x_watch_has_no_historical_snapshots")
    obi_cov_is_proxy = bool(report_inputs.get("obi_from_book_depth"))
    if obi_cov_is_proxy:
        failed.append("deficient_proxy_not_exact_top5")
    passed = not failed
    train_returns = [float(item["forward_return"]) for item in splits["train"] if item.get("forward_return") is not None]
    test_returns = [float(item["forward_return"]) for item in splits["test"] if item.get("forward_return") is not None]
    body = {
        "calibration_status": "candidate_not_activated" if passed else "not_ready",
        "gates_passed": passed,
        "gates_failed": failed,
        "thresholds_enabled": False,
        "x_watch": "online_only",
        "obi_top5_from_book_depth": False,
        "minimum_gates": {
            "test_split_events": MIN_TEST_EVENTS,
            "asof_feature_coverage": MIN_FEATURE_COVERAGE,
            "source": "decision_engine.core.news_shock_audit",
        },
        "sample": {
            "events": len(events),
            "train": len(splits["train"]),
            "validation": len(splits["validation"]),
            "test": test_n,
            "late_archive_rows": report_inputs.get("late_archive_rows"),
            "raw_rows": report_inputs.get("raw_rows"),
        },
        "coverage": {
            "obi_top5_asof": obi_cov,
            "taker_asof": taker_cov,
            "whale_asof": whale_cov,
            "heat_delta": heat_cov,
        },
        "train_only_median_forward_return": train_only_threshold(train_returns),
        "holdout_used_for_threshold": False,
        "permutation_effect_seed_1": permutation_effect(
            train_returns,
            [index % 2 for index in range(len(train_returns))],
            seed=1,
        ),
        "clustered": cluster_means(splits["train"]),
        "blocks": purged_blocks(events),
        "cost": {
            "applied_to_gate": False,
            "round_trip_bps_not_a_gate": 10,
            "test_mean_gross": None if not test_returns else sum(test_returns) / len(test_returns),
        },
    }
    encoded = json.dumps(body, sort_keys=True, default=str).encode("utf-8")
    body["report_sha256"] = hashlib.sha256(encoded).hexdigest()
    body["as_of"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not passed:
        body["onchain_calibration"] = None
        body["cannot_unlock"] = (
            "Legitimate overlap does not meet the existing test-split and "
            "feature-coverage gates, or the book/heat series is the wrong instrument. "
            "No cut is published."
        )
        return body
    # Cuts exist only as a candidate file. The live router is not changed.
    body["onchain_calibration"] = {
        "status": "candidate_not_activated",
        "min_heat_delta": None,
        "min_obi_aligned": body["train_only_median_forward_return"],
        "scope": report_inputs.get("scope"),
        "provenance_sha256": body["report_sha256"],
    }
    return body


def write_json(path: Path, body: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def finite_or_none(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number
