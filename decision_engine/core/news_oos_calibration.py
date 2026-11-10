"""Out-of-sample news study. The live gate stays locked.

``fetched_at`` is the clock. ``published_at`` does not replace it. A row
whose fetch is more than 90 minutes after publication is a late archive:
it is counted and then removed from the predictive sample.

Archive book depth and top-trader ratios are not the gate's ``obi_top5``,
``taker_buy_ratio``, or ``whale_retail_divergence``. ``x_watch`` has no
history, so heat stays unavailable and online accumulation stays off.
``OnchainCalibration`` is returned only when a non-synthetic study names
those gate series, has heat history, and clears the frozen tests. This
module does not attach that object to the engine.
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

from decision_engine.core.news_shock_gate import OnchainCalibration
from decision_engine.core.vision_backfill import CORE_BASES

SCHEMA_VERSION = "news_oos_v1"
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


def audit_news_rows(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Count the rows that were actually passed in. Nothing is hardcoded."""
    total = len(rows)
    late = 0
    bad_clock = 0
    usable = 0
    for row in rows:
        fetched = _parse(str(row.get("fetched_at") or ""))
        published = _parse(str(row.get("published_at") or ""))
        if fetched is None:
            bad_clock += 1
            continue
        if published is None:
            bad_clock += 1
            continue
        delay_min = (fetched - published).total_seconds() / 60.0
        if delay_min > LATE_ARCHIVE_MIN:
            late += 1
            continue
        usable += 1
    return {
        "rows_in_input": total,
        "late_archive_excluded": late,
        "bad_clock_excluded": bad_clock,
        "predictive_candidates": usable,
        "hardcoded_inventory_used": False,
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
    series_match = tuple(series) == GATE_SERIES and heat_available
    enough = test_clusters >= MIN_TEST_CLUSTERS and predictive_rows > 0
    any_significant = any(significant)
    unlock = (
        (not synthetic)
        and series_match
        and enough
        and any_significant
        and bool(news_db_sha256)
        and all(split_times.get(key) for key in ("train_end", "validation_end", "test_end"))
    )
    if synthetic:
        status = "synthetic_not_a_calibration"
    elif not enough:
        status = "insufficient"
    elif not series_match or not heat_available:
        status = "not_transferable"
    elif not any_significant:
        status = "not_significant"
    elif unlock:
        status = "unlock_evidence"
    else:
        status = "insufficient"
    calibration = None
    if unlock:
        calibration = {
            "min_heat_delta": 1,
            "min_obi_aligned": 0.15,
            "min_taker_aligned": TAKER_SHARE_ALIGNED,
            "min_whale_aligned": 1.2,
            "max_obi_divergent": -0.15,
            "max_taker_divergent": TAKER_SHARE_DIVERGENT,
            "max_whale_divergent": 0.8,
            "max_feature_age_min": 90.0,
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "unlock": unlock,
        "production_attested": unlock,
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
        "calibration": calibration,
        "false_positive_rate": None,
        "classification_accuracy": None,
    }


def load_unlocked_calibration(evidence: Mapping[str, object]) -> OnchainCalibration | None:
    """Return a calibration only from an attested non-synthetic evidence record."""
    if evidence.get("schema_version") != SCHEMA_VERSION:
        return None
    if evidence.get("synthetic") is True or evidence.get("unlock") is not True:
        return None
    if evidence.get("production_attested") is not True:
        return None
    if not evidence.get("news_db_sha256") or not evidence.get("split_times"):
        return None
    raw = evidence.get("calibration")
    if not isinstance(raw, Mapping):
        return None
    try:
        return OnchainCalibration(
            min_heat_delta=int(raw["min_heat_delta"]),
            min_obi_aligned=float(raw["min_obi_aligned"]),
            min_taker_aligned=float(raw["min_taker_aligned"]),
            min_whale_aligned=float(raw["min_whale_aligned"]),
            max_obi_divergent=float(raw["max_obi_divergent"]),
            max_taker_divergent=float(raw["max_taker_divergent"]),
            max_whale_divergent=float(raw["max_whale_divergent"]),
            max_feature_age_min=float(raw["max_feature_age_min"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


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
            quote = float(known[-1].get("quote_volume") or 0)
            taker_quote = known[-1].get("taker_buy_quote_volume")
            taker_share = None
            if quote > 0 and taker_quote is not None:
                taker_share = float(taker_quote) / quote
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
