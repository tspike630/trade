"""P3 Institutional Post-Trade TCA (Transaction Cost Analysis) & Implementation Shortfall Engine.

    python -m execution.tca_report --ledger execution_ledger.db --decision-db dexscreener.db
    python -m execution.tca_report --ledger execution_ledger.db --json

Analyzes Binance Spot live & shadow execution records from ``binance_spot_positions``,
``execution_log``, and ``decision_log`` in read-only mode:

1. **Implementation Shortfall (IS) in bps**:
   ``IS_bps = ((fill_price - signal_entry_price) / signal_entry_price) * 10_000``
   for BUY fills (positive = adverse slippage / shortfall; negative = price improvement).
2. **Execution Funnel & Rejection Attribution**:
   Total signals -> submitted -> OCO-verified working/filled vs. pre-trade / exchange
   rejections (broken down by reason and exchange HTTP/API error code).
3. **P0 Protection & OCO Re-Anchoring Audit**:
   Tracks how many fills required dynamic OCO re-anchoring (``oco_reanchored``),
   how many triggered emergency reduce-only auto-flatten (``auto_flatten``), and
   verifies whether any non-terminal position is currently unprotected.
4. **Realized Round-Trip PnL & Win-Rate Attribution**:
   Computes gross and net return (after configurable round-trip fee, default 20 bps)
   across ``take_profit``, ``stop_loss``, and ``oco_failed_auto_flatten`` exits,
   rolled up globally, by ``purpose``, and by ``symbol``.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from execution.slippage_report import percentile, summarize  # noqa: E402

DEFAULT_ROUND_TRIP_FEE_BPS = 20.0  # 10 bps entry + 10 bps exit


def _connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return num


def _as_dict(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def _load_signal_prices(decision_db: Path | None) -> dict[int, dict[str, Any]]:
    """Map decision_log.id -> {signal_price, sector_cluster, taker_buy_ratio, risk_parity_scalar}."""
    out: dict[int, dict[str, Any]] = {}
    if decision_db is None or not decision_db.is_file():
        return out
    try:
        conn = _connect_ro(decision_db)
    except sqlite3.Error:
        return out
    try:
        if "decision_log" not in _tables(conn):
            return out
        cols = _columns(conn, "decision_log")
        if not {"id", "raw_json"} <= cols:
            return out
        for row in conn.execute("SELECT id, raw_json FROM decision_log"):
            did = row["id"]
            if not isinstance(did, int):
                continue
            payload = _as_dict(row["raw_json"]) or {}
            extras = _as_dict(payload.get("extras")) or {}
            sig_px = _finite(
                extras.get("limit_price")
                if str(extras.get("order_type") or "").upper() == "LIMIT"
                and extras.get("limit_price") is not None
                else extras.get("price_usd", payload.get("price_usd"))
            )
            out[did] = {
                "signal_price": sig_px,
                "sector_cluster": extras.get("sector_cluster"),
                "taker_buy_ratio": _finite(extras.get("taker_buy_ratio")),
                "risk_parity_scalar": _finite(extras.get("risk_parity_scalar")),
            }
    except sqlite3.Error:
        return out
    finally:
        conn.close()
    return out


def build_tca_report(
    *,
    ledger_path: Path,
    decision_db: Path | None = None,
    round_trip_fee_bps: float = DEFAULT_ROUND_TRIP_FEE_BPS,
) -> dict[str, Any]:
    """Generate comprehensive post-trade TCA report from execution_ledger.db."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sig_meta = _load_signal_prices(decision_db)

    positions: list[dict[str, Any]] = []
    exec_logs: list[dict[str, Any]] = []

    if ledger_path.is_file():
        conn = _connect_ro(ledger_path)
        try:
            names = _tables(conn)
            if "binance_spot_positions" in names:
                for row in conn.execute("SELECT * FROM binance_spot_positions ORDER BY id ASC"):
                    positions.append(dict(row))
            if "execution_log" in names:
                for row in conn.execute("SELECT * FROM execution_log ORDER BY rowid ASC"):
                    exec_logs.append(dict(row))
        finally:
            conn.close()

    # Map decision_log_id -> receipt info from execution_log
    receipt_by_decision: dict[int, dict[str, Any]] = {}
    reject_reasons: Counter[str] = Counter()
    exchange_error_codes: Counter[str] = Counter()
    orderbook_spreads: list[float] = []
    orderbook_impacts: list[float] = []
    orderbook_ws_ages_ms: list[float] = []
    ws_l2_hits = 0
    rest_fallback_hits = 0
    submitted_count = 0
    executed_count = 0

    for item in exec_logs:
        payload = _as_dict(item.get("receipt_json")) or {}
        exec_res = _as_dict(payload.get("exec_result")) or payload
        if exec_res.get("submitted") is True:
            submitted_count += 1
        if int(item.get("executed") or exec_res.get("executed") or 0) == 1:
            executed_count += 1
        err = exec_res.get("error")
        if isinstance(err, str) and err.strip():
            reject_reasons[err.strip()] += 1
        exch_err = _as_dict(exec_res.get("exchange_error"))
        if exch_err is not None and exch_err.get("code") is not None:
            exchange_error_codes[str(exch_err.get("code"))] += 1
        ob = _as_dict(exec_res.get("orderbook_metrics"))
        if ob is not None:
            sp = _finite(ob.get("spread_bps"))
            imp = _finite(ob.get("impact_bps"))
            if sp is not None:
                orderbook_spreads.append(sp)
            if imp is not None:
                orderbook_impacts.append(imp)
            src = str(ob.get("market_data_source") or "")
            if src.startswith("ws_"):
                ws_l2_hits += 1
                age = _finite(ob.get("ws_age_ms"))
                if age is not None:
                    orderbook_ws_ages_ms.append(age)
            elif src == "rest_fallback":
                rest_fallback_hits += 1
        did_raw = item.get("decision_log_id")
        if did_raw is not None and str(did_raw).strip().isdigit():
            receipt_by_decision[int(str(did_raw).strip())] = exec_res

    shortfall_bps_list: list[float] = []
    reanchored_count = 0
    auto_flattened_count = 0
    oco_working_count = 0
    oco_filled_count = 0
    unprotected_open_count = 0
    dust_residual_count = 0

    exit_counts: Counter[str] = Counter()
    gross_returns_bps: list[float] = []
    net_returns_bps: list[float] = []
    by_purpose: dict[str, dict[str, Any]] = {}
    by_symbol: dict[str, dict[str, Any]] = {}

    for pos in positions:
        did = pos.get("decision_log_id")
        did_int = int(did) if isinstance(did, int) or (isinstance(did, str) and did.isdigit()) else None
        status = str(pos.get("status") or "")
        protection = str(pos.get("protection") or "")
        purpose = str(pos.get("purpose") or "live_bnb_boll_fade")
        symbol = str(pos.get("symbol") or "UNKNOWN")

        if status == "oco_working":
            oco_working_count += 1
        elif status == "oco_filled":
            oco_filled_count += 1
        elif status in {"open", "oco_failed", "oco_unknown", "qty_unconfirmed", "held_naked"}:
            if protection != "exchange_oco":
                unprotected_open_count += 1
        elif status == "closed_dust":
            residual = pos.get("residual_qty")
            sold = pos.get("exit_filled_qty")
            try:
                if (
                    residual not in (None, "")
                    and sold not in (None, "")
                    and Decimal(str(residual)) > 0
                    and Decimal(str(sold)) > 0
                ):
                    dust_residual_count += 1
            except Exception:
                dust_residual_count += 1

        if protection == "auto_flatten" or str(pos.get("exit_reason") or "") == "oco_failed_auto_flatten":
            auto_flattened_count += 1

        exec_res = receipt_by_decision.get(did_int, {}) if did_int is not None else {}
        arm_pos = _as_dict(exec_res.get("arm_position")) or {}
        if arm_pos.get("oco_reanchored") is True or pos.get("oco_reanchored") in (1, True, "1"):
            reanchored_count += 1

        fill_px = _finite(pos.get("entry_price"))
        sig_px = _finite(
            pos.get("signal_entry_price")
            or arm_pos.get("signal_entry_price")
            or (_as_dict(exec_res.get("exit_plan")) or {}).get("entry_price")
            or (sig_meta.get(did_int, {}) if did_int is not None else {}).get("signal_price")
        )

        is_bps: float | None = None
        if fill_px is not None and sig_px is not None and sig_px > 0:
            is_bps = ((fill_px - sig_px) / sig_px) * 10_000.0
            shortfall_bps_list.append(is_bps)

        exit_reason = str(pos.get("exit_reason") or "").strip()
        if exit_reason:
            exit_counts[exit_reason] += 1

        # Estimate realized return for terminal take_profit / stop_loss positions
        realized_px: float | None = _finite(pos.get("exit_price"))
        if realized_px is None and status == "oco_filled":
            if exit_reason == "take_profit":
                realized_px = _finite(pos.get("take_profit_price"))
            elif exit_reason == "stop_loss":
                realized_px = _finite(pos.get("stop_price"))

        gross_bps: float | None = None
        net_bps: float | None = None
        if fill_px is not None and fill_px > 0 and realized_px is not None and realized_px > 0:
            gross_bps = ((realized_px - fill_px) / fill_px) * 10_000.0
            net_bps = gross_bps - float(round_trip_fee_bps)
            gross_returns_bps.append(gross_bps)
            net_returns_bps.append(net_bps)

        for bucket_map, key in ((by_purpose, purpose), (by_symbol, symbol)):
            b = bucket_map.setdefault(
                key,
                {
                    "positions": 0,
                    "shortfall_bps": [],
                    "net_returns_bps": [],
                    "take_profit": 0,
                    "stop_loss": 0,
                    "auto_flatten": 0,
                },
            )
            b["positions"] += 1
            if is_bps is not None:
                b["shortfall_bps"].append(is_bps)
            if net_bps is not None:
                b["net_returns_bps"].append(net_bps)
            if exit_reason == "take_profit":
                b["take_profit"] += 1
            elif exit_reason == "stop_loss":
                b["stop_loss"] += 1
            elif exit_reason == "oco_failed_auto_flatten" or protection == "auto_flatten":
                b["auto_flatten"] += 1

    def _finalize_Rollup(raw_map: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for k in sorted(raw_map):
            item = raw_map[k]
            sf = item["shortfall_bps"]
            nr = item["net_returns_bps"]
            tp = item["take_profit"]
            sl = item["stop_loss"]
            closed_decided = tp + sl
            out[k] = {
                "positions": item["positions"],
                "mean_shortfall_bps": round(sum(sf) / len(sf), 3) if sf else None,
                "mean_net_return_bps": round(sum(nr) / len(nr), 3) if nr else None,
                "win_rate": round(tp / closed_decided, 4) if closed_decided > 0 else None,
                "take_profit": tp,
                "stop_loss": sl,
                "auto_flatten": item["auto_flatten"],
            }
        return out

    tp_total = exit_counts.get("take_profit", 0)
    sl_total = exit_counts.get("stop_loss", 0)
    decided_total = tp_total + sl_total
    ob_source_total = ws_l2_hits + rest_fallback_hits

    return {
        "report": "binance_spot_tca",
        "generated_at": now_iso,
        "ledger_path": str(ledger_path),
        "round_trip_fee_bps": float(round_trip_fee_bps),
        "execution_funnel": {
            "execution_log_rows": len(exec_logs),
            "submitted": submitted_count,
            "executed": executed_count,
            "positions_total": len(positions),
            "reject_reasons": dict(sorted(reject_reasons.items())),
            "exchange_error_codes": dict(sorted(exchange_error_codes.items())),
        },
        "implementation_shortfall_bps": summarize(shortfall_bps_list),
        "pre_trade_orderbook": {
            "spread_bps": summarize(orderbook_spreads),
            "impact_bps": summarize(orderbook_impacts),
            "ws_l2_hits": ws_l2_hits,
            "rest_fallback_hits": rest_fallback_hits,
            "ws_hit_rate": round(ws_l2_hits / ob_source_total, 4) if ob_source_total > 0 else None,
            "ws_age_ms": summarize(orderbook_ws_ages_ms),
        },
        "protection_audit": {
            "oco_working": oco_working_count,
            "oco_filled": oco_filled_count,
            "oco_reanchored_saves": reanchored_count,
            "auto_flatten_rescues": auto_flattened_count,
            "unprotected_open_count": unprotected_open_count,
            "dust_residual_count": dust_residual_count,
            "zero_naked_exposure": unprotected_open_count == 0 and dust_residual_count == 0,
        },
        "realized_performance": {
            "closed_with_price": len(net_returns_bps),
            "take_profit_count": tp_total,
            "stop_loss_count": sl_total,
            "win_rate": round(tp_total / decided_total, 4) if decided_total > 0 else None,
            "gross_return_bps": summarize(gross_returns_bps),
            "net_return_bps": summarize(net_returns_bps),
        },
        "by_purpose": _finalize_Rollup(by_purpose),
        "by_symbol": _finalize_Rollup(by_symbol),
    }


def format_tca_markdown(report: dict[str, Any]) -> str:
    funnel = report["execution_funnel"]
    is_stats = report["implementation_shortfall_bps"]
    prot = report["protection_audit"]
    perf = report["realized_performance"]
    net_stats = perf["net_return_bps"]

    def _f(val: Any) -> str:
        if val is None:
            return "n/a"
        if isinstance(val, float):
            return f"{val:.2f}"
        return str(val)

    lines = [
        "# Binance Spot Post-Trade TCA & Implementation Shortfall Report",
        f"- **Generated At**: `{report['generated_at']}`",
        f"- **Ledger**: `{report['ledger_path']}` (Round-Trip Fee Assumption: `{report['round_trip_fee_bps']} bps`)",
        "",
        "## 1. Execution Funnel & Protection Audit",
        f"- **Execution Log Rows**: `{funnel['execution_log_rows']}` | **Submitted**: `{funnel['submitted']}` | **Executed**: `{funnel['executed']}`",
        f"- **OCO Working**: `{prot['oco_working']}` | **OCO Filled**: `{prot['oco_filled']}` | **Re-Anchored Saves (P0)**: `{prot['oco_reanchored_saves']}` | **Auto-Flatten Rescues (P0)**: `{prot['auto_flatten_rescues']}`",
        f"- **Zero Naked Exposure**: `{prot['zero_naked_exposure']}` (`unprotected_open_count={prot['unprotected_open_count']}`)",
        "",
        "## 2. Implementation Shortfall (Signal vs. Actual Fill Price)",
        f"- **Samples (`n`)**: `{is_stats['n']}` | **Mean IS**: `{_f(is_stats['mean'])} bps` | **Median (P50)**: `{_f(is_stats['p50'])} bps` | **P90**: `{_f(is_stats['p90'])} bps`",
        "",
        "## 3. Realized Exit Performance",
        f"- **Take-Profit**: `{perf['take_profit_count']}` | **Stop-Loss**: `{perf['stop_loss_count']}` | **Win Rate**: `{_f(perf['win_rate'] * 100.0 if perf['win_rate'] is not None else None)}%`",
        f"- **Mean Net Return**: `{_f(net_stats['mean'])} bps` | **P50 Net Return**: `{_f(net_stats['p50'])} bps`",
    ]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Binance Spot Post-Trade TCA Report")
    parser.add_argument("--ledger", required=True, help="Path to execution_ledger.db")
    parser.add_argument("--decision-db", default=None, help="Optional path to decision SQLite DB")
    parser.add_argument("--fee-bps", type=float, default=DEFAULT_ROUND_TRIP_FEE_BPS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = build_tca_report(
        ledger_path=Path(args.ledger).expanduser(),
        decision_db=Path(args.decision_db).expanduser() if args.decision_db else None,
        round_trip_fee_bps=float(args.fee_bps),
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_tca_markdown(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
