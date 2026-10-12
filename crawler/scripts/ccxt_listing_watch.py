#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CCXT full-market listing events (load_markets diff → prior.db).

  cd /workspace/dexscreener-crawler
  .venv/bin/python scripts/ccxt_listing_watch.py --once
  .venv/bin/python scripts/ccxt_listing_watch.py --once --exchanges okx,kucoin
  .venv/bin/python scripts/ccxt_listing_watch.py --loop --interval 1800

Reads: CCXT public load_markets() (full symbol set, not topN)
Writes: prior.db.cex_listing_events only (UNIQUE exchange+symbol)
Never writes ccxt.db / business DBs; no weights/gates/decisions.

Note: Binance public API often 451 geo-restrict on this host — use
--exchanges okx,kucoin for smoke. Single-exchange failure → warning skip.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from logging_setup import get_logger, setup_logging  # noqa: E402

log = get_logger("ccxt_listing_watch")

DB_PRIOR = str(_ROOT / "data" / "prior" / "db" / "prior.db")
_DEFAULT_EXCHANGES = "binance,okx,kucoin"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cex_listing_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    listing_ts INTEGER NOT NULL,          -- epoch seconds（首次全量见到）
    base TEXT NOT NULL,
    quote TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'markets_diff',
    UNIQUE (exchange, symbol)
);
CREATE INDEX IF NOT EXISTS idx_cex_listing_events_ts ON cex_listing_events(listing_ts);
"""


def _default_exchanges() -> list[str]:
    raw = os.environ.get("CCXT_EXCHANGES", "").strip() or _DEFAULT_EXCHANGES
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


def _parse_exchanges(s: str | None) -> list[str]:
    if not s:
        return _default_exchanges()
    return [x.strip().lower() for x in s.split(",") if x.strip()]


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def _known_symbols(conn: sqlite3.Connection, exchange: str) -> set[str]:
    return {
        str(r[0])
        for r in conn.execute(
            "SELECT symbol FROM cex_listing_events WHERE exchange=?",
            (exchange,),
        ).fetchall()
    }


def _watch_one(ex_name: str, conn: sqlite3.Connection, now: int) -> dict:
    """load_markets for one exchange; insert new symbols. Returns summary."""
    import ccxt  # local import keeps --help cheap if ccxt missing

    klass = getattr(ccxt, ex_name, None)
    if klass is None:
        log.warning("ccxt exchange %r not supported; skip", ex_name)
        return {"exchange": ex_name, "ok": False, "error": "unsupported", "new": 0, "total": 0}

    try:
        ex = klass({"enableRateLimit": True})
        markets = ex.load_markets()
    except Exception as exc:  # noqa: BLE001 —— 单所失败不杀整轮
        log.warning("load_markets %s failed: %s", ex_name, exc)
        return {"exchange": ex_name, "ok": False, "error": str(exc), "new": 0, "total": 0}

    # Full symbol set (no topN filter). Prefer unified symbol keys.
    symbols: dict[str, tuple[str, str]] = {}
    for sym, m in (markets or {}).items():
        if not isinstance(m, dict):
            continue
        # skip inactive if flagged
        if m.get("active") is False:
            continue
        base = str(m.get("base") or "").strip()
        quote = str(m.get("quote") or "").strip()
        if not base or not quote:
            # fall back: split unified "BASE/QUOTE:settle" style
            part = str(sym).split(":")[0]
            if "/" in part:
                base, quote = part.split("/", 1)
            else:
                continue
        symbols[str(sym)] = (base, quote)

    known = _known_symbols(conn, ex_name)
    new_syms = [s for s in symbols if s not in known]
    added = 0
    for sym in new_syms:
        base, quote = symbols[sym]
        conn.execute(
            "INSERT OR IGNORE INTO cex_listing_events"
            "(exchange, symbol, listing_ts, base, quote, source)"
            " VALUES (?,?,?,?,?,?)",
            (ex_name, sym, now, base, quote, "markets_diff"),
        )
        added += 1
    conn.commit()
    log.info(
        "listing_watch exchange=%s markets=%s known=%s new=%s",
        ex_name,
        len(symbols),
        len(known),
        added,
    )
    return {
        "exchange": ex_name,
        "ok": True,
        "error": "",
        "new": added,
        "total": len(symbols),
        "known": len(known),
    }


def _pass(exchanges: list[str], db_prior: str) -> dict:
    now = int(time.time())
    summaries = []
    with sqlite3.connect(db_prior) as conn:
        _ensure_schema(conn)
        for name in exchanges:
            summaries.append(_watch_one(name, conn, now))
    new_total = sum(s.get("new", 0) for s in summaries)
    ok_n = sum(1 for s in summaries if s.get("ok"))
    log.info(
        "listing_watch pass done exchanges=%s ok=%s new_total=%s",
        len(exchanges),
        ok_n,
        new_total,
    )
    return {"summaries": summaries, "new_total": new_total}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="CCXT full-market listing events (load_markets diff)"
    )
    parser.add_argument("--once", action="store_true", help="single pass")
    parser.add_argument("--loop", action="store_true", help="run in loop")
    parser.add_argument(
        "--interval",
        type=int,
        default=1800,
        help="loop interval seconds (default 1800)",
    )
    parser.add_argument(
        "--exchanges",
        default=None,
        help="comma list (default CCXT_EXCHANGES or binance,okx,kucoin)",
    )
    parser.add_argument("--db-prior", default=DB_PRIOR)
    args = parser.parse_args(argv)
    setup_logging()

    exchanges = _parse_exchanges(args.exchanges)
    if not exchanges:
        log.error("no exchanges configured")
        return 1

    if args.loop:
        while True:
            try:
                _pass(exchanges, args.db_prior)
            except Exception as exc:  # noqa: BLE001
                log.warning("listing_watch pass failed: %s", exc)
            time.sleep(args.interval)
        return 0

    # default / --once: single pass
    _pass(exchanges, args.db_prior)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
