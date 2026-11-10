"""Out-of-sample report for the stop-prone shadow. Default is observe, not block.

State 1 is an oversold fade. ``change_1h < 0`` is the setup, not a reject.
DEX ``liquidity_usd`` is a pool field and is not a Binance book. This module
does not enable a block from sample size, and it does not write trading weights.
"""

from __future__ import annotations

import csv
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

BLOCK_ENABLED = False
MIN_TEST_ROWS = 80

CANDIDATE_FEATURES = (
    "volatility_24h_extreme",
    "thin_pool_measured",
    "toxic_flow",
)


def _parse_ts(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def state1_blocks_on_negative_change_1h(regime: str, change_1h: float | None) -> bool:
    """State 1 must not reject a dip only because the last hour is down."""
    del regime, change_1h
    return False


def annotate_shadow_stop_prone(
    signals: Sequence[Mapping[str, Any]],
    *,
    availability: str = "not_configured",
    detail: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Stamp every signal. Never drop a buy. ``block_enabled`` stays false."""
    out: list[dict[str, Any]] = []
    for sig in signals:
        row = dict(sig)
        change = _float(row.get("change_1h"))
        regime = str(row.get("market_regime") or "")
        row["shadow_stop_prone_block"] = False
        row["shadow_stop_prone_block_enabled"] = BLOCK_ENABLED
        row["shadow_stop_prone_availability"] = availability
        row["shadow_stop_prone_detail"] = detail
        row["shadow_change_1h_negative_ignored"] = bool(
            regime == "positive_gamma_mean_reversion" and change is not None and change < 0
        )
        row["shadow_dex_liq_not_used_for_binance"] = True
        out.append(row)
    report = {
        "block_enabled": BLOCK_ENABLED,
        "availability": availability,
        "detail": detail,
        "dropped_buys": 0,
    }
    return out, report


def _load_rows(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    if not path.exists():
        return [], "missing"
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle)), None
    if path.suffix.lower() == ".db":
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        except sqlite3.Error as exc:
            return [], f"error:{exc}"
        conn.row_factory = sqlite3.Row
        try:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "shadow_stop_prone" in tables:
                query = "SELECT * FROM shadow_stop_prone"
            elif "positions" in tables:
                query = "SELECT * FROM positions"
            else:
                return [], "schema_error"
            rows = [dict(row) for row in conn.execute(query)]
        except sqlite3.Error as exc:
            return [], f"error:{exc}"
        finally:
            conn.close()
        return rows, None
    return [], "unsupported_path"


def _time_split(rows: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    stamped: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        stamp = _parse_ts(row.get("opened_at") or row.get("tagged_at"))
        if stamp is None:
            continue
        stamped.append((stamp, row))
    stamped.sort(key=lambda item: item[0])
    ordered = [row for _stamp, row in stamped]
    count = len(ordered)
    train_end = int(count * 0.6)
    valid_end = int(count * 0.8)
    return {
        "train": ordered[:train_end],
        "validation": ordered[train_end:valid_end],
        "test": ordered[valid_end:],
        "unsorted_dropped": len(rows) - count,
    }


def _stop_rate(rows: Sequence[Mapping[str, Any]]) -> tuple[int, int]:
    stops = 0
    for row in rows:
        reason = str(row.get("exit_reason") or "").lower()
        if "stop" in reason:
            stops += 1
    return stops, len(rows)


def run_oos_report(path: str | Path | None) -> dict[str, Any]:
    """Describe a sample. ``enable_block`` is false in this build."""
    if path is None:
        return {
            "availability": "not_configured",
            "enable_block": False,
            "reason": "no path supplied; observe only",
            "dex_liquidity_transferable_to_binance": False,
            "change_1h_negative_is_a_state1_reject": False,
        }
    source = Path(path)
    rows, problem = _load_rows(source)
    if problem is not None:
        return {
            "availability": problem,
            "path": str(source),
            "enable_block": False,
            "reason": "missing or unreadable sample; fail open",
            "dex_liquidity_transferable_to_binance": False,
            "change_1h_negative_is_a_state1_reject": False,
            "n": 0,
        }
    splits = _time_split(rows)
    test = splits["test"]
    high: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for row in test:
        vol = _float(row.get("volatility_24h"))
        if vol is not None and vol > 2.5:
            high.append(row)
        else:
            rest.append(row)
    high_stops, high_n = _stop_rate(high)
    rest_stops, rest_n = _stop_rate(rest)
    return {
        "availability": "ok",
        "path": str(source),
        "enable_block": False,
        "production_attested": False,
        "reason": (
            "Descriptive test-split counts are not a license to block. "
            "State 1 is not rejected for change_1h < 0. "
            "DEX liquidity_usd is not a Binance gate."
        ),
        "n": len(rows),
        "split": {key: len(value) if key != "unsorted_dropped" else value for key, value in splits.items()},
        "test_high_vol_stops": {"stops": high_stops, "n": high_n},
        "test_other_stops": {"stops": rest_stops, "n": rest_n},
        "test_n_below_minimum": len(test) < MIN_TEST_ROWS,
        "minimum_test_rows": MIN_TEST_ROWS,
        "candidate_features_not_enabled": list(CANDIDATE_FEATURES),
        "excluded_from_any_block": ["change_1h<0 alone", "dex liquidity_usd on binance"],
        "cost_applied": False,
        "multiple_testing": (
            "Three pre-specified candidates. A later enablement needs a "
            "pre-registered test and a multiplicity adjustment. This run enables nothing."
        ),
        "dex_liquidity_transferable_to_binance": False,
        "change_1h_negative_is_a_state1_reject": False,
        "time_order": "opened_at ascending; earliest 60/20/20; no shuffle",
    }
