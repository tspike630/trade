#!/usr/bin/env python3
"""Run the deployed attach_counterparty_context against an explicit database.

This process installs a logger stub before importing the decision package, so
it does not import crawler config and does not read a .env file. The database
is opened read-only. A write is attempted and must fail. now_ms is required;
the process clock is not used.

The report is the consumer stamp, not a raw listing. A raw top-5 value that
is missing, non-finite, errored, stale, or in the future is left unstamped.
orderbook_imbalance_20 is reported beside obi_top5 and is not a stamp source.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import types
from pathlib import Path

FRESH_DEFAULT_SEC = 3600
TABLE = "binance_spot_microstructure"


def release_root_from_here() -> Path:
    return Path(__file__).resolve().parents[1]


def install_logger_stub() -> None:
    """Register logging_setup before decision_engine is imported."""
    if "decision_engine" in sys.modules or "logging_setup" in sys.modules:
        raise RuntimeError("logger stub must be installed before decision imports")
    module = types.ModuleType("logging_setup")

    def get_logger(name: str | None = None):
        import logging

        return logging.getLogger(name or "obi_proof")

    def setup_logging(level: str | None = None):
        del level
        raise RuntimeError("proof runner does not configure file logging")

    module.get_logger = get_logger
    module.setup_logging = setup_logging
    module.redact_secrets = lambda text: text
    sys.modules["logging_setup"] = module


def write_probe(path: Path) -> dict[str, object]:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=3.0)
    try:
        conn.execute("PRAGMA query_only=ON")
        query_only = int(conn.execute("PRAGMA query_only").fetchone()[0])
        try:
            conn.execute("CREATE TABLE obi_proof_write_probe (x INTEGER)")
            conn.commit()
        except sqlite3.OperationalError as exc:
            return {
                "uri_mode": "ro",
                "query_only": query_only == 1,
                "write_rejected": True,
                "error": f"{type(exc).__name__}: {exc}",
            }
        return {
            "uri_mode": "ro",
            "query_only": query_only == 1,
            "write_rejected": False,
            "error": "write succeeded",
        }
    finally:
        conn.close()


def _open_ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=3.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({TABLE})")}


def _finite(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _load_rows(conn: sqlite3.Connection) -> tuple[list[dict[str, object]], int]:
    columns = _columns(conn)
    wanted = [
        "symbol",
        "obi_top5",
        "obi_top5_source_ts_ms",
        "obi_top5_error",
        "orderbook_imbalance_20",
        "ts_ms",
        "source_ts_ms",
        "collected_at_ms",
        "kline_5m_close_ts_ms",
        "book_update_id",
        "bid_depth_5_usd",
        "ask_depth_5_usd",
    ]
    selected = [name for name in wanted if name in columns]
    if "symbol" not in selected:
        return [], 0
    rows = []
    for raw in conn.execute(f"SELECT {', '.join(selected)} FROM {TABLE}"):
        item: dict[str, object] = {name: raw[name] for name in selected}
        rows.append(item)
    return rows, len(rows)


def _newest(rows: list[dict[str, object]]) -> tuple[dict[str, object] | None, int]:
    ranked = []
    for row in rows:
        try:
            stamp = int(row["obi_top5_source_ts_ms"])  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError):
            continue
        ranked.append((stamp, row))
    if not ranked:
        return None, 0
    peak = max(stamp for stamp, _row in ranked)
    winners = [row for stamp, row in ranked if stamp == peak]
    return winners[0], len(winners)


def prove(db_path: Path, now_ms: int, release_root: Path) -> tuple[dict[str, object], int]:
    path = db_path.expanduser().resolve()
    if not path.is_file():
        return {"error": "db_missing", "db_path": str(path)}, 2
    probe = write_probe(path)
    if not probe["write_rejected"] or not probe["query_only"]:
        return {"error": "write_probe_open", "db_path": str(path), "write_probe": probe}, 2

    deploy = release_root / "deploy"
    if not (deploy / "decision_engine" / "core" / "perp_microstructure_and_beta_hedge.py").is_file():
        return {"error": "deploy_tree_missing", "release_root": str(release_root)}, 2
    sys.path.insert(0, str(deploy))
    install_logger_stub()
    from decision_engine.core.binance_indicators import binance_indicators_fresh_sec
    from decision_engine.core.perp_microstructure_and_beta_hedge import (
        attach_counterparty_context,
    )

    window_sec = binance_indicators_fresh_sec()
    window_ms = window_sec * 1000
    conn = _open_ro(path)
    try:
        columns = _columns(conn) if TABLE in {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        } else set()
        raw_rows, row_count = _load_rows(conn) if columns else ([], 0)
    finally:
        conn.close()

    by_symbol: dict[str, list[dict[str, object]]] = {}
    for row in raw_rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if symbol:
            by_symbol.setdefault(symbol, []).append(row)

    attach_rows = [{"binance_symbol": symbol} for symbol in sorted(by_symbol)]
    seen: list[str] = []
    original_connect = sqlite3.connect

    def _recording_connect(*args, **kwargs):
        uri = ""
        if args:
            uri = str(args[0])
        elif "database" in kwargs:
            uri = str(kwargs["database"])
        seen.append(uri)
        return original_connect(*args, **kwargs)

    sqlite3.connect = _recording_connect
    try:
        attach_counterparty_context(attach_rows, db_path=path, now_ms=now_ms)
    finally:
        sqlite3.connect = original_connect

    conn = _open_ro(path)
    try:
        _raw_after, row_count_after = _load_rows(conn) if columns else ([], 0)
    finally:
        conn.close()

    symbols_out = []
    violations = 0
    stamped_count = 0
    unstamped: list[str] = []
    for symbol in sorted(by_symbol):
        newest, newest_ties = _newest(by_symbol[symbol])
        source_ts = None
        top5 = None
        top20 = None
        error = None
        if newest is not None:
            source_ts = _optional_int_from_mapping(newest, "obi_top5_source_ts_ms")
            top5 = _finite(newest.get("obi_top5"))
            top20 = _finite(newest.get("orderbook_imbalance_20"))
            error = _text_from_mapping(newest, "obi_top5_error")
        age_ms = None if source_ts is None else int(now_ms) - source_ts
        if window_ms <= 0:
            fresh = source_ts is not None
        else:
            fresh = age_ms is not None and 0 <= age_ms <= window_ms
        attached = next(row for row in attach_rows if row["binance_symbol"] == symbol)
        stamped_value = _finite(attached.get("obi_top5"))
        stamped_ts = attached.get("obi_top5_source_ts_ms")
        stamped = stamped_value is not None
        if stamped:
            stamped_count += 1
        distinct = top5 is not None and top20 is not None and not math.isclose(top5, top20, abs_tol=0.0)
        used_top20 = (
            stamped
            and top20 is not None
            and math.isclose(stamped_value, top20, abs_tol=0.0)
            and (top5 is None or not math.isclose(top5, top20, abs_tol=0.0))
        )
        publishable = top5 is not None and not error and fresh and newest_ties == 1
        expected_stamp = top5 if publishable else None
        stamp_ok = (
            not used_top20
            and "orderbook_imbalance_20" not in attached
            and (
                (expected_stamp is None and not stamped)
                or (
                    expected_stamp is not None
                    and stamped
                    and math.isclose(stamped_value, expected_stamp, abs_tol=0.0)
                    and int(stamped_ts) == source_ts
                )
            )
        )
        if not stamp_ok:
            violations += 1
        if not stamped:
            unstamped.append(symbol)
        symbols_out.append(
            {
                "symbol": symbol,
                "obi_top5": top5,
                "orderbook_imbalance_20": top20,
                "distinct_top5_vs_top20": distinct,
                "obi_top5_source_ts_ms": source_ts,
                "age_ms": age_ms,
                "age_sec": None if age_ms is None else age_ms / 1000.0,
                "fresh": fresh,
                "error": error,
                "newest_source_tie": newest_ties,
                "stamped": stamped,
                "stamped_obi_top5": stamped_value,
                "stamped_source_ts_ms": None if stamped_ts is None else int(stamped_ts),
                "row_ts_ms": None if newest is None else _optional_int_from_mapping(newest, "ts_ms"),
                "source_ts_ms": None if newest is None else _optional_int_from_mapping(newest, "source_ts_ms"),
                "collected_at_ms": None if newest is None else _optional_int_from_mapping(newest, "collected_at_ms"),
                "kline_5m_close_ts_ms": None
                if newest is None
                else _optional_int_from_mapping(newest, "kline_5m_close_ts_ms"),
                "book_update_id": None if newest is None else _optional_int_from_mapping(newest, "book_update_id"),
                "stamp_matches_reader": stamp_ok,
            }
        )

    ro_ok = bool(seen) and all("mode=ro" in uri for uri in seen)
    unchanged = row_count == row_count_after
    btc = next((row for row in symbols_out if row["symbol"] == "BTCUSDT"), None)
    report: dict[str, object] = {
        "kind": "obi_consumer_attach",
        "not_a_raw_listing": True,
        "db_path": str(path),
        "now_ms": int(now_ms),
        "columns_present": {
            "obi_top5": "obi_top5" in columns,
            "obi_top5_source_ts_ms": "obi_top5_source_ts_ms" in columns,
            "obi_top5_error": "obi_top5_error" in columns,
            "orderbook_imbalance_20": "orderbook_imbalance_20" in columns,
        },
        "fresh_window_sec": window_sec,
        "fresh_window_default_sec": FRESH_DEFAULT_SEC,
        "logger": "stub",
        "imported_config": "config" in sys.modules,
        "imported_dotenv": "dotenv" in sys.modules,
        "write_probe": probe,
        "attach_uris": seen,
        "attach_mode_ro": ro_ok,
        "rows_before": row_count,
        "rows_after": row_count_after,
        "rows_unchanged": unchanged,
        "symbol_count": len(symbols_out),
        "stamped_count": stamped_count,
        "unstamped_symbols": unstamped,
        "coverage_complete": not unstamped and bool(symbols_out),
        "contract_violations": violations,
        "exit_means": "reader_contract_held",
        "btc": btc,
        "symbols": symbols_out,
    }
    ok = (
        probe["write_rejected"]
        and probe["query_only"]
        and ro_ok
        and unchanged
        and not report["imported_config"]
        and not report["imported_dotenv"]
        and violations == 0
    )
    return report, 0 if ok else 3


def _optional_int_from_mapping(row: dict[str, object], column: str) -> int | None:
    if column not in row or row[column] is None:
        return None
    try:
        return int(row[column])  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _text_from_mapping(row: dict[str, object], column: str) -> str | None:
    if column not in row or row[column] is None:
        return None
    text = str(row[column]).strip()
    return text or None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--now-ms", required=True, type=int)
    parser.add_argument("--release-root", type=Path, default=None)
    args = parser.parse_args()
    root = (args.release_root or release_root_from_here()).resolve()
    report, code = prove(args.db, args.now_ms, root)
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
