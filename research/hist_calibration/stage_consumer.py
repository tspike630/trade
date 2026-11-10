"""Read a staged Vision database only through a verified batch manifest.

A raw ``SELECT *`` is not a study. Two source URLs at the same instrument
time with different prices are a conflict: neither price is used. An
unverified hash, a missing manifest, or a URL outside the batch plan is
ignored. Book-depth bands are never returned as ``obi_top5``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

VERIFIED_STATUS = frozenset({"ok", "checksum_verified", "downloaded", "cached"})
PRICE_FIELDS = ("open", "high", "low", "close")


def load_manifest(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    if not Path(path).is_file():
        return None
    body = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(body, dict):
        return None
    return body


def _day_bounds(start: date, end: date) -> tuple[int, int]:
    start_ms = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp() * 1000)
    end_excl = int(
        datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp() * 1000
    ) + 86_400_000
    return start_ms, end_excl


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[1]) for row in rows}


def verify_manifest_files(conn: sqlite3.Connection, manifest: dict[str, Any]) -> dict[str, Any]:
    """Each planned URL must be checksum-verified and inside the batch."""
    planned = [str(url) for url in manifest.get("iter_plan_urls") or []]
    files = {str(item.get("url")): item for item in manifest.get("files") or [] if item.get("url")}
    symbols = {str(item).upper() for item in manifest.get("symbols") or []}
    errors: list[str] = []
    verified: list[str] = []
    if not planned:
        errors.append("missing_iter_plan_urls")
    if not symbols:
        errors.append("missing_symbols")
    if "start" not in manifest or "end" not in manifest:
        errors.append("missing_date_window")
    have = _columns(conn, "vision_files")
    if "vision_files" not in {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    } or not {"url", "sha256", "status"}.issubset(have):
        errors.append("vision_files_absent")
        return {"ok": False, "errors": errors, "verified_urls": [], "symbols": sorted(symbols)}
    for url in planned:
        spec = files.get(url)
        if spec is None:
            errors.append(f"url_not_in_manifest_files:{url}")
            continue
        expected = str(spec.get("expected_sha256") or "").strip().lower()
        row = conn.execute(
            "SELECT status, sha256 FROM vision_files WHERE url=?",
            (url,),
        ).fetchone()
        if row is None:
            errors.append(f"url_not_in_vision_files:{url}")
            continue
        status = str(row[0] or "").strip().lower()
        actual = str(row[1] or "").strip().lower()
        if status not in VERIFIED_STATUS:
            errors.append(f"status_not_verified:{url}")
            continue
        if not expected or actual != expected:
            errors.append(f"checksum_mismatch:{url}")
            continue
        verified.append(url)
    return {
        "ok": not errors and len(verified) == len(planned) and bool(planned),
        "errors": errors,
        "verified_urls": verified,
        "symbols": sorted(symbols),
    }


def _distinct(values: list[Any]) -> list[Any]:
    seen: list[Any] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


def study_from_stage(conn: sqlite3.Connection, manifest: dict[str, Any] | None) -> dict[str, Any]:
    """Never treats an unmanifested stage as ready."""
    if not manifest:
        return {
            "study_ready": False,
            "raw_stage_study_ready": False,
            "verification": {"ok": False, "errors": ["missing_manifest"], "verified_urls": []},
            "bars": [],
            "conflicts": [],
            "ignored_rows": 0,
            "obi_top5": None,
            "book_depth_is_obi_top5": False,
        }
    return read_verified_bars(conn, manifest)


def read_verified_bars(conn: sqlite3.Connection, manifest: dict[str, Any]) -> dict[str, Any]:
    """Unique instrument-time bars from verified plan URLs only.

    A timestamp with two prices is omitted. The later row is not chosen.
    """
    check = verify_manifest_files(conn, manifest)
    result: dict[str, Any] = {
        "study_ready": False,
        "raw_stage_study_ready": False,
        "verification": check,
        "bars": [],
        "conflicts": [],
        "ignored_rows": 0,
        "obi_top5": None,
        "book_depth_is_obi_top5": False,
    }
    if not check["ok"]:
        return result
    cols = _columns(conn, "spot_klines")
    if not {"symbol", "interval", "open_time_ms", "source_url"}.issubset(cols):
        result["verification"]["errors"].append("spot_klines_absent")
        result["verification"]["ok"] = False
        return result
    start = date.fromisoformat(str(manifest["start"]))
    end = date.fromisoformat(str(manifest["end"]))
    start_ms, end_ms = _day_bounds(start, end)
    interval = str(manifest.get("interval") or "5m")
    price_cols = [name for name in PRICE_FIELDS if name in cols]
    select_cols = ["symbol", "interval", "open_time_ms", "source_url", *price_cols]
    if "taker_buy_quote" in cols:
        select_cols.append("taker_buy_quote")
    placeholders = ",".join("?" for _ in check["verified_urls"])
    symbols = check["symbols"]
    symbol_sql = ",".join("?" for _ in symbols)
    query = f"""
        SELECT {", ".join(select_cols)}
        FROM spot_klines
        WHERE source_url IN ({placeholders})
          AND symbol IN ({symbol_sql})
          AND interval = ?
          AND open_time_ms >= ? AND open_time_ms < ?
    """
    rows = conn.execute(
        query,
        (*check["verified_urls"], *symbols, interval, start_ms, end_ms),
    ).fetchall()
    names = select_cols
    grouped: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for raw in rows:
        item = {names[index]: raw[index] for index in range(len(names))}
        key = (str(item["symbol"]), str(item["interval"]), int(item["open_time_ms"]))
        grouped.setdefault(key, []).append(item)
    # Rows outside the plan are not in this query. Count them as ignored.
    ignored = conn.execute("SELECT COUNT(*) FROM spot_klines").fetchone()[0] - len(rows)
    result["ignored_rows"] = int(ignored)
    bars = []
    conflicts = []
    for key, group in sorted(grouped.items()):
        urls = _distinct([str(item["source_url"]) for item in group])
        signatures = _distinct(
            [tuple(item.get(name) for name in (*price_cols, "taker_buy_quote")) for item in group]
        )
        if len(urls) > 1 and len(signatures) > 1:
            conflicts.append(
                {
                    "symbol": key[0],
                    "interval": key[1],
                    "open_time_ms": key[2],
                    "source_urls": urls,
                    "used": False,
                }
            )
            continue
        chosen = group[0]
        bars.append(
            {
                "symbol": key[0],
                "interval": key[1],
                "open_time_ms": key[2],
                "open": chosen.get("open"),
                "source_url": chosen["source_url"],
                "source_urls": urls,
                "taker_buy_quote": chosen.get("taker_buy_quote"),
            }
        )
    result["bars"] = bars
    result["conflicts"] = conflicts
    result["study_ready"] = not conflicts and bool(bars)
    return result


def attach_verified_returns(
    events: list[dict[str, Any]],
    bars: list[dict[str, Any]],
    *,
    horizon_ms: int = 300_000,
) -> list[dict[str, Any]]:
    """Join fetched_at events to verified opens. Book depth is not a feature."""
    from research.hist_calibration.study import forward_return

    by_symbol: dict[str, list[tuple[int, float]]] = {}
    for bar in bars:
        if bar.get("open") is None:
            continue
        by_symbol.setdefault(str(bar["symbol"]), []).append((int(bar["open_time_ms"]), float(bar["open"])))
    joined = []
    for event in events:
        item = dict(event)
        symbol = str(item.get("symbol") or "")
        event_ms = item.get("event_ms")
        series = by_symbol.get(symbol, [])
        item["forward_return"] = (
            None if event_ms is None else forward_return(series, int(event_ms), horizon_ms)
        )
        item["obi_top5_asof"] = None
        item["taker_asof"] = None
        item["whale_asof"] = None
        item["heat_delta"] = None
        item["book_depth_used_as_obi"] = False
        joined.append(item)
    return joined
