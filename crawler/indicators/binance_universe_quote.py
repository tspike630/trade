"""Refresh the Binance spot universe from the public 24h ticker.

Writes ``symbols.quoteVolume_24h`` and ``symbols.quoteVolume_collected_at``
together, and only after ``GET /api/v3/ticker/24hr`` and
``GET /api/v3/exchangeInfo`` both parse. The timestamp is that successful
collection, not the file mtime and not a migration clock.

A failed fetch does not open a write. A symbol with no finite ``quoteVolume``
is left out of the new snapshot instead of keeping an older timestamp that
would still look fresh. Spot USDT only; names with ``:`` are futures and are
ignored. This module does not read ``binance_indicators`` and does not place
orders.

``python -m indicators.binance_universe_quote --db <universe.db>``
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from logging_setup import get_logger, setup_logging

log = get_logger("indicators.binance_universe_quote")

QUOTE_COLUMN = "quoteVolume_24h"
COLLECTED_AT_COLUMN = "quoteVolume_collected_at"
TICKER_24H_PATH = "/api/v3/ticker/24hr"
EXCHANGE_INFO_PATH = "/api/v3/exchangeInfo"
DEFAULT_BASE = "https://api.binance.com"

_CREATE_NEXT = f"""
CREATE TABLE symbols_next (
    symbol TEXT PRIMARY KEY,
    baseAsset TEXT,
    quoteAsset TEXT,
    status TEXT,
    {QUOTE_COLUMN} REAL NOT NULL,
    {COLLECTED_AT_COLUMN} TEXT NOT NULL
)
"""


def collection_timestamp(moment: datetime | None = None) -> str:
    """UTC timestamp stored only beside a quote that was just parsed."""
    when = moment or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_json(url: str, *, timeout: float = 30.0) -> Any:
    """Public GET. No API key."""
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "trade-binance-universe-quote/1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(f"binance GET failed: {exc}") from exc
    return json.loads(payload.decode("utf-8"))


def _finite_quote(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")) or number < 0:
        return None
    return number


def trading_spot_usdt(exchange_info: Any) -> dict[str, tuple[str, str]]:
    """``BASEUSDT -> (base, USDT)`` for spot TRADING rows. Futures are dropped."""
    rows = exchange_info.get("symbols") if isinstance(exchange_info, dict) else None
    if not isinstance(rows, list):
        return {}
    out: dict[str, tuple[str, str]] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        base = str(item.get("baseAsset") or "").strip().upper()
        quote = str(item.get("quoteAsset") or "").strip().upper()
        status = str(item.get("status") or "").strip().upper()
        if not symbol or ":" in symbol or status != "TRADING" or quote != "USDT":
            continue
        spot_flag = item.get("isSpotTradingAllowed")
        if spot_flag is False or str(spot_flag).strip().lower() in {"false", "0"}:
            continue
        if not base:
            if not symbol.endswith("USDT"):
                continue
            base = symbol[: -len("USDT")]
        if symbol != f"{base}USDT":
            continue
        out[symbol] = (base, "USDT")
    return out


def ticker_quote_volume(payload: Any) -> dict[str, float]:
    """Spot ticker ``quoteVolume`` by symbol. Missing or non-finite values are omitted."""
    rows = payload if isinstance(payload, list) else None
    if rows is None and isinstance(payload, dict):
        rows = [payload]
    if not isinstance(rows, list):
        return {}
    out: dict[str, float] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol or ":" in symbol:
            continue
        number = _finite_quote(item.get("quoteVolume"))
        if number is None:
            continue
        out[symbol] = number
    return out


def build_snapshot_rows(
    exchange_info: Any,
    ticker_payload: Any,
    *,
    collected_at: str,
) -> list[tuple[str, str, str, str, float, str]]:
    """Rows that have both a TRADING spot identity and a parsed quoteVolume."""
    listed = trading_spot_usdt(exchange_info)
    quotes = ticker_quote_volume(ticker_payload)
    rows: list[tuple[str, str, str, str, float, str]] = []
    for symbol in sorted(listed):
        quote = quotes.get(symbol)
        if quote is None:
            continue
        base, quote_asset = listed[symbol]
        rows.append((symbol, base, quote_asset, "TRADING", quote, collected_at))
    return rows


def _replace_symbols(
    conn: sqlite3.Connection,
    rows: list[tuple[str, str, str, str, float, str]],
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP TABLE IF EXISTS symbols_next")
        conn.execute(_CREATE_NEXT)
        conn.executemany(
            f"""
            INSERT INTO symbols_next (
                symbol, baseAsset, quoteAsset, status,
                {QUOTE_COLUMN}, {COLLECTED_AT_COLUMN}
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.execute("DROP TABLE IF EXISTS symbols")
        conn.execute("ALTER TABLE symbols_next RENAME TO symbols")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def refresh_universe(
    db_path: Path,
    *,
    fetch_json: Callable[[str], Any],
    collected_at: datetime | None = None,
    base_url: str = DEFAULT_BASE,
    json_path: Path | None = None,
) -> dict[str, Any]:
    """Replace ``symbols`` only after both public payloads parse.

    Returns ``wrote=False`` without changing the file when either fetch
    fails or no symbol has a finite quoteVolume.
    """
    root = base_url.rstrip("/")
    try:
        exchange_info = fetch_json(root + EXCHANGE_INFO_PATH)
        ticker_payload = fetch_json(root + TICKER_24H_PATH)
    except Exception as exc:  # noqa: BLE001 — leave the previous snapshot untouched
        log.warning("universe quote refresh failed closed: %s", exc)
        return {"ok": False, "wrote": False, "error": str(exc), "symbols": 0}
    stamp = collection_timestamp(collected_at)
    rows = build_snapshot_rows(exchange_info, ticker_payload, collected_at=stamp)
    if not rows:
        log.warning("universe quote refresh produced no finite quoteVolume rows")
        return {"ok": False, "wrote": False, "error": "no_quote_rows", "symbols": 0}
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        _replace_symbols(conn, rows)
    except sqlite3.Error as exc:
        log.warning("universe quote write failed: %s", exc)
        return {"ok": False, "wrote": False, "error": str(exc), "symbols": 0}
    finally:
        conn.close()
    if json_path is not None:
        payload = [
            {
                "symbol": symbol,
                "baseAsset": base,
                "quoteAsset": quote_asset,
                "status": status,
                QUOTE_COLUMN: quote,
                COLLECTED_AT_COLUMN: stamp,
            }
            for symbol, base, quote_asset, status, quote, _stamp in rows
        ]
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(payload), encoding="utf-8")
    log.info("universe quote refresh wrote=%s collected_at=%s", len(rows), stamp)
    return {"ok": True, "wrote": True, "symbols": len(rows), "collected_at": stamp}


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(
        description=(
            "Rewrite binance_spot_universe.db symbols from Binance spot "
            "ticker quoteVolume. Does not place orders."
        )
    )
    parser.add_argument("--db", required=True, help="Universe sqlite path")
    parser.add_argument("--json", default="", help="Optional JSON mirror written after the sqlite commit")
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    args = parser.parse_args(argv)
    result = refresh_universe(
        Path(args.db),
        fetch_json=fetch_json,
        base_url=args.base_url,
        json_path=Path(args.json) if args.json else None,
    )
    if not result.get("wrote"):
        return 2
    print(
        f"symbols={result['symbols']} collected_at={result['collected_at']} db={args.db}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
