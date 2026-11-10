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
STOP_LABEL = "stop_loss"

# Cited from the parent read-only audit at 2026-11-10 15:04:27 HKT.
# This process did not re-query those databases.
LOCAL_SHADOW_CCXT_AUDIT: dict[str, Any] = {
    "recomputed_here": False,
    "audit_id": "shadow-ccxt-audit-20261110",
    "audit_time_hkt": "2026-11-10 15:04:27 HKT",
    "label": "exit_reason==stop_loss",
    "negative_pnl_is_not_a_stop": True,
    "predictive_rows": "features_usable_at_entry==1 only",
    "flags_known_at_entry": False,
    "flags_tagged_at_hkt": "2026-11-03 after outcomes were known",
    "stored_skip_equals": "shadow_R3_vol_chg_rsi",
    "stored_skip_is_not": "R3 OR thin-pool, even though rule_id says R3+LIQ150k",
    "same_book_temporal_oos": {"n": 21, "stop_loss": 0},
    "multibook_oos_ex_discovery": {"n": 656, "stop_loss": 172, "take_profit": 111},
    "r3_on_multibook_oos": {
        "flagged": "37/97",
        "unflagged": "135/559",
        "fisher_p_two_sided": 0.00567764954741716,
        "take_profit_skipped": 28,
    },
    "strategies_in_multibook": 41,
    "oct20_grid_rules": 7,
    "population": "solana dex paper only; not Binance",
    "live_stop_loss": 11,
    "live_bad_tick_false_stop_loss": 2,
    "change_1h_lt_0_multibook_oos_fisher_p": 0.435304757622255,
    "ccxt_last_bars_hkt": "2026-10-24 about 17 days before the audit",
    "binance_series": 52,
    "binance_with_at_least_20_closed_1h": 0,
    "binance_max_closed_1h": 7,
    "listings_seeded_2026_11_05": 518,
    "cross_venue_pairs": 66,
    "pairs_with_at_least_20_matches_within_5s": 0,
    "enable_hard_filter": False,
    "enable_chase_block": False,
    "enable_spread_block": False,
}

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


def is_stop_loss(row: Mapping[str, Any]) -> bool:
    """Only the exact exit label. A negative pnl is not a stop."""
    return str(row.get("exit_reason") or "").strip().lower() == STOP_LABEL


def usable_at_entry(row: Mapping[str, Any]) -> bool:
    """Stored shadow flags were batch-tagged after the fact.

    A row is predictive only when the source says the raw features were
    already known at entry. Rows without that column are left in, and the
    report says they were not attested.
    """
    if "features_usable_at_entry" not in row:
        return True
    return str(row.get("features_usable_at_entry")).strip() in {"1", "1.0"}


def _stop_rate(rows: Sequence[Mapping[str, Any]]) -> tuple[int, int]:
    stops = sum(1 for row in rows if is_stop_loss(row))
    return stops, len(rows)


def run_oos_report(path: str | Path | None) -> dict[str, Any]:
    """Describe a sample. ``enable_block`` is false in this build."""
    cited = dict(LOCAL_SHADOW_CCXT_AUDIT)
    if path is None:
        return {
            "availability": "not_configured",
            "enable_block": False,
            "reason": "no path supplied; observe only",
            "dex_liquidity_transferable_to_binance": False,
            "change_1h_negative_is_a_state1_reject": False,
            "local_audit_2026_11_10": cited,
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
            "local_audit_2026_11_10": cited,
        }
    predictive = [row for row in rows if usable_at_entry(row)]
    splits = _time_split(predictive)
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
        "n": len(predictive),
        "rows_excluded_not_knowable_at_entry": len(rows) - len(predictive),
        "label": STOP_LABEL,
        "negative_pnl_counted_as_stop": False,
        "stored_skip_equals_r3_not_r3_or_liq": True,
        "local_audit_2026_11_10": cited,
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
