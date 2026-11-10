"""Out-of-sample news study. The live gate stays locked.

``fetched_at`` is the clock. ``published_at`` does not replace it. A row
whose fetch is more than 90 minutes after publication is a late archive:
it is counted and then removed from the predictive sample.

Archive book depth and top-trader ratios are not the gate's ``obi_top5``
or ``whale_retail_divergence``. ``x_watch`` has no history, so heat stays
unavailable. The live ``OnchainCalibration`` needs heat, top-5 imbalance,
taker share, and whale divergence together. This module never constructs
that object. A taker quote share can be recorded only as partial evidence,
and it still does not open the live gate.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence

from decision_engine.core.vision_backfill import CORE_BASES

SCHEMA_VERSION = "news_oos_v2"
LATE_ARCHIVE_MIN = 90
HORIZON_MIN = 60
EMBARGO_MIN = 60
COST_BPS = 10.0
MIN_TEST_CLUSTERS = 30
BOOTSTRAP_REPS = 1000
BOOTSTRAP_SEED = 20261110
TRAIN_FRACTION = 0.60
VALIDATION_FRACTION = 0.20
# Frozen before any fit. These are not searched.
TAKER_SHARE_ALIGNED = 0.55
TAKER_SHARE_DIVERGENT = 0.45
GATE_SERIES = (
    "obi_top5",
    "taker_buy_ratio",
    "whale_retail_divergence",
    "heat_evolution",
)
ARCHIVE_SERIES = (
    "taker_buy_quote_share",
    "count_toptrader_long_short_ratio",
    "book_depth_imbalance_pct_1",
)
_BASE_RE = re.compile(r"\b(" + "|".join(CORE_BASES) + r")\b")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip().lower())


def mentioned_bases(title: str) -> tuple[str, ...]:
    found = []
    for match in _BASE_RE.findall(title or ""):
        if match not in found:
            found.append(match)
    return tuple(found)


def _delay_minutes(row: Mapping[str, object]) -> float | None:
    fetched = _parse(str(row.get("fetched_at") or ""))
    published = _parse(str(row.get("published_at") or ""))
    if fetched is None or published is None:
        return None
    return (fetched - published).total_seconds() / 60.0


def audit_news_rows(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Count the rows that were actually passed in. Nothing is hardcoded.

    The primary predictive cut is a fetch more than 90 minutes after
    publication. Other cuts are reported so that cut can be audited. A
    parent pre-count of 1906 / 1325 / 581 and an older inventory of
    1859 / 164 are not results of this function.
    """
    total = len(rows)
    missing_fetched = 0
    missing_published = 0
    fetch_before_publish = 0
    delays: list[float] = []
    for row in rows:
        fetched = _parse(str(row.get("fetched_at") or ""))
        published = _parse(str(row.get("published_at") or ""))
        if fetched is None:
            missing_fetched += 1
            continue
        if published is None:
            missing_published += 1
            continue
        delay = (fetched - published).total_seconds() / 60.0
        if delay < 0:
            fetch_before_publish += 1
            continue
        delays.append(delay)
    sensitivity = {}
    for cut in (60, 90, 180, 360):
        late = sum(1 for item in delays if item > cut)
        sensitivity[str(cut)] = {
            "late_archive_excluded": late,
            "predictive_candidates": len(delays) - late,
        }
    primary = sensitivity["90"]
    return {
        "rows_in_input": total,
        "missing_fetched_at": missing_fetched,
        "missing_published_at": missing_published,
        "fetched_before_published": fetch_before_publish,
        "dated_rows": len(delays),
        "late_archive_excluded": primary["late_archive_excluded"],
        "bad_clock_excluded": missing_fetched + missing_published + fetch_before_publish,
        "predictive_candidates": primary["predictive_candidates"],
        "late_archive_rule": "exclude when fetched_at - published_at > 90 minutes",
        "clock": "fetched_at",
        "published_at_replaces_fetched_at": False,
        "sensitivity_by_cutoff_minutes": sensitivity,
        "hardcoded_inventory_used": False,
        "reference_counts_not_used": {
            "parent_precount_2026_11_10": {
                "rows": 1906,
                "late_archive_gt_90min": 1325,
                "predictive_candidates": 581,
                "is_final": False,
            },
            "older_inventory": {"rows": 1859, "excluded_old_news": 164},
            "used_as_this_result": False,
        },
    }


def dedup_events(rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Keep the earliest ``fetched_at`` for the same source and title in 6 hours."""
    kept: list[dict[str, object]] = []
    seen: list[tuple[str, str, datetime]] = []
    ordered = sorted(rows, key=lambda item: str(item.get("fetched_at") or ""))
    for row in ordered:
        fetched = _parse(str(row.get("fetched_at") or ""))
        published = _parse(str(row.get("published_at") or ""))
        if fetched is None or published is None:
            continue
        if (fetched - published).total_seconds() / 60.0 > LATE_ARCHIVE_MIN:
            continue
        source = str(row.get("source") or "")
        title = normalize_title(str(row.get("title") or ""))
        duplicate = False
        for old_source, old_title, old_fetch in seen:
            if old_source == source and old_title == title and abs(
                (fetched - old_fetch).total_seconds()
            ) <= 6 * 3600:
                duplicate = True
                break
        if duplicate:
            continue
        seen.append((source, title, fetched))
        item = dict(row)
        item["fetched_at_dt"] = fetched
        item["bases"] = mentioned_bases(str(row.get("title") or "")) or ("BTC",)
        item["benchmark_assumed"] = not bool(mentioned_bases(str(row.get("title") or "")))
        kept.append(item)
    return kept


def cluster_id(row: Mapping[str, object]) -> str:
    fetched: datetime = row["fetched_at_dt"]  # type: ignore[assignment]
    bucket = int(fetched.timestamp() // (HORIZON_MIN * 60))
    title = normalize_title(str(row.get("title") or ""))
    return f"{bucket}:{title}"


def time_split(rows: Sequence[Mapping[str, object]]) -> dict[str, list[dict[str, object]]]:
    ordered = sorted(rows, key=lambda item: item["fetched_at_dt"])  # type: ignore[arg-type, return-value]
    count = len(ordered)
    train_end = int(count * TRAIN_FRACTION)
    valid_end = int(count * (TRAIN_FRACTION + VALIDATION_FRACTION))
    train = [dict(item) for item in ordered[:train_end]]
    valid = [dict(item) for item in ordered[train_end:valid_end]]
    test = [dict(item) for item in ordered[valid_end:]]
    return _purge({"train": train, "validation": valid, "test": test})


def _purge(parts: dict[str, list[dict[str, object]]]) -> dict[str, list[dict[str, object]]]:
    """Drop events whose 60-minute label window crosses the next split."""
    order = ("train", "validation", "test")
    starts: dict[str, datetime | None] = {}
    for name in order:
        rows = parts[name]
        starts[name] = rows[0]["fetched_at_dt"] if rows else None  # type: ignore[assignment]
    purged: dict[str, list[dict[str, object]]] = {}
    for index, name in enumerate(order[:-1]):
        nxt = starts[order[index + 1]]
        kept: list[dict[str, object]] = []
        for row in parts[name]:
            fetched: datetime = row["fetched_at_dt"]  # type: ignore[assignment]
            if nxt is not None and fetched + timedelta(minutes=EMBARGO_MIN) > nxt:
                continue
            kept.append(row)
        purged[name] = kept
    purged["test"] = list(parts["test"])
    return purged


def feature_is_known(feature_time: datetime, fetched_at: datetime, close_time: datetime) -> bool:
    """A feature must be closed and not later than the live fetch."""
    return close_time <= fetched_at and feature_time <= fetched_at


def forward_return_bps(close_now: float, close_later: float) -> float | None:
    if close_now <= 0 or not math.isfinite(close_now) or not math.isfinite(close_later):
        return None
    gross = (close_later - close_now) / close_now * 10_000.0
    return gross - COST_BPS


def bootstrap_treated(
    rows: Sequence[tuple[str, bool, float]],
    *,
    reps: int = BOOTSTRAP_REPS,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, float | None]:
    """Block bootstrap of treated-minus-control mean. Clusters stay intact."""
    treated: dict[str, list[float]] = {}
    control: dict[str, list[float]] = {}
    for cluster, flag, value in rows:
        bucket = treated if flag else control
        bucket.setdefault(cluster, []).append(value)
    if not treated or not control:
        return {"mean_diff_bps": None, "ci_low": None, "ci_high": None, "p_value": None, "clusters": 0}
    def _mean(block: dict[str, list[float]], keys: list[str]) -> float:
        values = [item for key in keys for item in block[key]]
        return sum(values) / len(values)

    observed = _mean(treated, list(treated)) - _mean(control, list(control))
    rng = random.Random(seed)
    treated_keys = list(treated)
    control_keys = list(control)
    diffs: list[float] = []
    for _ in range(reps):
        draw_t = [treated_keys[rng.randrange(len(treated_keys))] for _ in treated_keys]
        draw_c = [control_keys[rng.randrange(len(control_keys))] for _ in control_keys]
        diffs.append(_mean(treated, draw_t) - _mean(control, draw_c))
    diffs.sort()
    low = diffs[int(0.025 * (len(diffs) - 1))]
    high = diffs[int(0.975 * (len(diffs) - 1))]
    extreme = sum(1 for item in diffs if abs(item) >= abs(observed))
    return {
        "mean_diff_bps": observed,
        "ci_low": low,
        "ci_high": high,
        "p_value": extreme / len(diffs),
        "clusters": float(len(treated_keys) + len(control_keys)),
    }


def holm(p_values: Sequence[float]) -> list[float]:
    """Holm adjusted p-values, same order as the input."""
    indexed = sorted(enumerate(p_values), key=lambda item: item[1])
    adjusted = [1.0] * len(p_values)
    running = 0.0
    count = len(p_values)
    for rank, (index, value) in enumerate(indexed):
        corrected = min(1.0, (count - rank) * value)
        running = max(running, corrected)
        adjusted[index] = running
    return adjusted


def evaluate_study(
    *,
    predictive_rows: int,
    test_clusters: int,
    hypotheses: Sequence[Mapping[str, object]],
    series: Sequence[str],
    heat_available: bool,
    news_db_sha256: str | None,
    split_times: Mapping[str, str | None],
    synthetic: bool,
) -> dict[str, object]:
    """Apply the frozen rule. Thresholds are not revised to chase a p-value."""
    usable = [
        (index, float(item["p_value"]))
        for index, item in enumerate(hypotheses)
        if item.get("p_value") is not None
    ]
    adjusted_map = {index: value for index, value in zip((item[0] for item in usable), holm([item[1] for item in usable]))} if usable else {}
    adjusted = [adjusted_map.get(index) for index in range(len(hypotheses))]
    significant = [value is not None and value <= 0.05 for value in adjusted]
    enough = test_clusters >= MIN_TEST_CLUSTERS and predictive_rows > 0
    taker_index = next(
        (index for index, item in enumerate(hypotheses) if item.get("name") == "taker_buy_quote_share"),
        None,
    )
    taker_p = None if taker_index is None else adjusted[taker_index]
    taker_significant = taker_p is not None and taker_p <= 0.05
    partial = None
    if (
        (not synthetic)
        and enough
        and taker_significant
        and bool(news_db_sha256)
        and all(split_times.get(key) for key in ("train_end", "validation_end", "test_end"))
    ):
        partial = {
            "field": "taker_buy_ratio",
            "definition": "taker_buy_quote_volume / quote_volume",
            "matches_live_entry_zone_definition": True,
            "aligned_min": TAKER_SHARE_ALIGNED,
            "divergent_max": TAKER_SHARE_DIVERGENT,
            "applied_to_live_gate": False,
            "reason": (
                "judge_onchain still returns pending unless heat_evolution, "
                "obi_top5, and whale_retail_divergence are present too"
            ),
        }
    if synthetic:
        status = "synthetic_not_a_calibration"
    elif not enough:
        status = "insufficient"
    elif not taker_significant:
        status = "not_significant"
    else:
        status = "partial_taker_only"
    unlock = False
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "unlock": unlock,
        "production_attested": False,
        "synthetic": synthetic,
        "x_watch_history": "closed",
        "x_watch_online_accumulation": "off",
        "hardcoded_row_counts": False,
        "clock": "fetched_at",
        "late_archive_minutes": LATE_ARCHIVE_MIN,
        "horizon_min": HORIZON_MIN,
        "embargo_min": EMBARGO_MIN,
        "cost_bps_round_trip": COST_BPS,
        "predictive_rows": predictive_rows,
        "test_clusters": test_clusters,
        "holm_p": adjusted,
        "series": list(series),
        "gate_series": list(GATE_SERIES),
        "heat_available": heat_available,
        "archive_assumption": (
            "Exchange create_time is treated as the print time a live API could "
            "have shown. The vision file's later download time is not a feature. "
            "Historical publication lag of the archive itself is unknown."
        ),
        "lag_sensitivity_minutes": [0, 5, 15],
        "news_db_sha256": news_db_sha256,
        "split_times": dict(split_times),
        "hypotheses": list(hypotheses),
        "live_gate_unlock": False,
        "cannot_identify": [
            "heat_evolution",
            "obi_top5",
            "whale_retail_divergence",
            "book_depth_as_obi_top5",
            "top_trader_ratio_as_whale_retail_divergence",
        ],
        "will_not_fully_unlock_onchain_calibration": True,
        "partial_taker_evidence": partial,
        "false_positive_rate": None,
        "classification_accuracy": None,
    }


def taker_buy_quote_share(quote_volume: float, taker_buy_quote_volume: float) -> float | None:
    """Same ratio as ``taker_buy_ratio = taker_buy_quote_volume / quote_volume``.

    Values outside ``[0, 1]`` are not that share and are rejected.
    """
    if quote_volume <= 0 or not math.isfinite(quote_volume) or not math.isfinite(taker_buy_quote_volume):
        return None
    share = taker_buy_quote_volume / quote_volume
    if share < 0 or share > 1:
        return None
    return share


def partial_taker_record(evidence: Mapping[str, object]) -> dict[str, object] | None:
    """Partial taker evidence only. Never a live ``OnchainCalibration``."""
    if evidence.get("schema_version") != SCHEMA_VERSION:
        return None
    if evidence.get("synthetic") is True or evidence.get("live_gate_unlock") is not False:
        return None
    raw = evidence.get("partial_taker_evidence")
    if not isinstance(raw, Mapping) or raw.get("applied_to_live_gate") is not False:
        return None
    if raw.get("definition") != "taker_buy_quote_volume / quote_volume":
        return None
    return dict(raw)


def read_news_readonly(path: Path) -> tuple[list[dict[str, object]], str]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    uri = "file:" + path.resolve().as_posix() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, source, source_tier, title, url, published_at, fetched_at, onchain_status
                FROM news_events
                """
            )
        ]
    finally:
        conn.close()
    return rows, digest


def _as_dt(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    raise TypeError("expected datetime")


def build_effect_rows(
    events: Sequence[Mapping[str, object]],
    klines: Sequence[Mapping[str, object]],
    metrics: Sequence[Mapping[str, object]],
    depth: Sequence[Mapping[str, object]],
    *,
    lag_min: int = 0,
) -> list[dict[str, object]]:
    """Join only closed prints at or before ``fetched_at``. Do not fill gaps."""
    bars: dict[str, list[dict[str, object]]] = {}
    for row in klines:
        bars.setdefault(str(row["symbol"]), []).append(dict(row))
    for rows in bars.values():
        rows.sort(key=lambda item: _as_dt(item["close_time"]))
    metric_rows: dict[str, list[dict[str, object]]] = {}
    for row in metrics:
        metric_rows.setdefault(str(row["symbol"]), []).append(dict(row))
    depth_rows: dict[str, list[dict[str, object]]] = {}
    for row in depth:
        depth_rows.setdefault(str(row["symbol"]), []).append(dict(row))
    out: list[dict[str, object]] = []
    for event in events:
        fetched = _as_dt(event["fetched_at_dt"])
        cutoff = fetched - timedelta(minutes=lag_min)
        horizon = fetched + timedelta(minutes=HORIZON_MIN)
        for base in tuple(event.get("bases") or ("BTC",)):
            symbol = f"{base}USDT"
            series = bars.get(symbol) or []
            known = [row for row in series if _as_dt(row["close_time"]) <= cutoff]
            future = [
                row
                for row in series
                if _as_dt(row["open_time"]) >= fetched and _as_dt(row["close_time"]) <= horizon
            ]
            if not known or not future:
                continue
            if _as_dt(future[-1]["close_time"]) < horizon - timedelta(minutes=5):
                continue
            ret = forward_return_bps(float(future[0]["open"]), float(future[-1]["close"]))
            if ret is None:
                continue
            taker_share = None
            if known[-1].get("quote_volume") is not None and known[-1].get("taker_buy_quote_volume") is not None:
                taker_share = taker_buy_quote_share(
                    float(known[-1]["quote_volume"]),
                    float(known[-1]["taker_buy_quote_volume"]),
                )
            top_ratio = None
            for row in metric_rows.get(symbol) or []:
                stamp = _as_dt(row["create_time"])
                if stamp <= cutoff:
                    top_ratio = row.get("count_toptrader_long_short_ratio")
            imbalance = _depth_imbalance(depth_rows.get(symbol) or [], cutoff)
            out.append(
                {
                    "cluster": cluster_id(event),
                    "symbol": symbol,
                    "return_bps": ret,
                    "taker_buy_quote_share": taker_share,
                    "count_toptrader_long_short_ratio": None if top_ratio is None else float(top_ratio),
                    "book_depth_imbalance_pct_1": imbalance,
                }
            )
    return out


def _depth_imbalance(rows: Sequence[Mapping[str, object]], cutoff: datetime) -> float | None:
    latest: dict[float, tuple[datetime, float]] = {}
    for row in rows:
        stamp = _as_dt(row["timestamp"])
        if stamp > cutoff:
            continue
        percentage = float(row["percentage"])
        if percentage not in (1.0, -1.0):
            continue
        previous = latest.get(percentage)
        if previous is None or stamp >= previous[0]:
            latest[percentage] = (stamp, float(row["notional"] or 0))
    if 1.0 not in latest or -1.0 not in latest:
        return None
    if latest[1.0][0] != latest[-1.0][0]:
        return None
    ask = latest[1.0][1]
    bid = latest[-1.0][1]
    total = ask + bid
    if total <= 0:
        return None
    return (bid - ask) / total


def hypotheses_from_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    reps: int = BOOTSTRAP_REPS,
) -> list[dict[str, object]]:
    specs = (
        ("taker_buy_quote_share", TAKER_SHARE_ALIGNED),
        ("count_toptrader_long_short_ratio", 1.2),
        ("book_depth_imbalance_pct_1", 0.15),
    )
    # Collapse to one observation per cluster per hypothesis by averaging returns.
    reports: list[dict[str, object]] = []
    for name, cut in specs:
        grouped: dict[str, list[tuple[bool, float]]] = {}
        for row in rows:
            value = row.get(name)
            if value is None:
                continue
            grouped.setdefault(str(row["cluster"]), []).append(
                (float(value) >= cut, float(row["return_bps"]))
            )
        sample: list[tuple[str, bool, float]] = []
        for cluster, items in grouped.items():
            flag = any(item[0] for item in items)
            mean_ret = sum(item[1] for item in items) / len(items)
            sample.append((cluster, flag, mean_ret))
        stats = bootstrap_treated(sample, reps=reps)
        reports.append({"name": name, "cut": cut, "pre_specified": True, **stats})
    return reports


def export_spec() -> dict[str, object]:
    """Minimum desensitized extract. Raw titles and URLs stay on the source machine."""
    return {
        "schema": "news_export_v1",
        "clock": "fetched_at",
        "columns": [
            "id",
            "source",
            "source_tier",
            "published_at",
            "fetched_at",
            "onchain_status",
            "url_sha256",
            "title_sha256",
            "title_bases",
        ],
        "notes": [
            "Do not replace fetched_at with published_at.",
            "title_bases is the frozen CORE_BASES mention list.",
            "A row with no mention is a BTC benchmark, and that assumption is flagged.",
            "1859 and 164 are not constants in this study.",
        ],
    }
