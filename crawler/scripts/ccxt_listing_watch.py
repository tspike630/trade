#!/usr/bin/env python3
"""CCXT full-market listing events (load_markets diff, precise).

Observe-only. Calls CCXT load_markets() for the full symbol set (no top-N).
Diffs that set against prior.db.cex_listing_events and inserts new symbols.
Does not open ccxt.db. Does not change weights, gates, or decisions.
Default off (not started by main.py / the decision engine).

  cd crawler
  .venv/bin/python scripts/ccxt_listing_watch.py --once
  .venv/bin/python scripts/ccxt_listing_watch.py --loop --interval 1800
  .venv/bin/python scripts/ccxt_listing_watch.py --once --exchanges okx,kucoin

Cold start inserts every symbol the exchange returns (often thousands).
The next run inserts 0 unless a symbol is actually new.
listing_ts is unix seconds (first time the full set contained the symbol).
One exchange failure logs a warning and is skipped; it does not abort the pass.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

import ccxt

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from logging_setup import get_logger, setup_logging  # noqa: E402

log = get_logger("ccxt_listing_watch")

DB_PRIOR = str(_ROOT / "data" / "prior" / "db" / "prior.db")
_DEFAULT_EXCHANGES = "binance,okx,kucoin"
_DEFAULT_INTERVAL = 1800

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


def _exchanges_from_env() -> str:
    """CCXT_EXCHANGES from the process env, else the same key in crawler/.env.

    Only that one key is read from .env. Other lines (including secrets) are ignored
    and nothing from the file is logged.
    """
    raw = os.environ.get("CCXT_EXCHANGES", "").strip()
    if raw:
        return raw
    env_path = _ROOT / ".env"
    if not env_path.is_file():
        return _DEFAULT_EXCHANGES
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return _DEFAULT_EXCHANGES
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != "CCXT_EXCHANGES":
            continue
        picked = value.strip().strip('"').strip("'")
        if picked:
            return picked
    return _DEFAULT_EXCHANGES


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def _split_symbol(symbol: str) -> tuple[str, str]:
    if "/" in symbol:
        base, quote = symbol.split("/", 1)
        return base, quote
    return symbol, ""


def _base_quote(symbol: str, market: object) -> tuple[str, str]:
    info = market if isinstance(market, dict) else {}
    base = str(info.get("base") or "")
    quote = str(info.get("quote") or "")
    if not base or not quote:
        fallback_base, fallback_quote = _split_symbol(symbol)
        base = base or fallback_base
        quote = quote or fallback_quote
    return base, quote


def diff_and_store(
    conn: sqlite3.Connection,
    exchange: str,
    markets: dict,
    now_s: int,
) -> tuple[int, int]:
    """Insert symbols not already stored for this exchange.

    ``markets`` is the full ``load_markets()`` dict (symbol → market). No top-N cut.
    Quote-like bases (USDT/TRY and similar) are kept; chain matching filters later.
    Returns ``(n_symbols, n_inserted)``. UNIQUE(exchange, symbol) makes a rerun insert 0.
    """
    symbols = {str(sym) for sym in markets.keys()}
    known = {
        row[0]
        for row in conn.execute(
            "SELECT symbol FROM cex_listing_events WHERE exchange=?",
            (exchange,),
        )
    }
    new_symbols = symbols - known
    inserted = 0
    for sym in sorted(new_symbols):
        base, quote = _base_quote(sym, markets.get(sym))
        cur = conn.execute(
            "INSERT OR IGNORE INTO cex_listing_events"
            " (exchange, symbol, listing_ts, base, quote, source)"
            " VALUES (?,?,?,?,?,?)",
            (exchange, sym, int(now_s), base, quote, "markets_diff"),
        )
        if cur.rowcount and cur.rowcount > 0:
            inserted += 1
    conn.commit()
    return len(symbols), inserted


def build_exchanges(names: list[str]) -> list:
    """Construct CCXT clients. Unsupported or failed inits are skipped."""
    out = []
    for name in names:
        name = (name or "").strip().lower()
        if not name:
            continue
        klass = getattr(ccxt, name, None)
        if klass is None:
            log.warning("ccxt exchange %r not supported; skip", name)
            continue
        try:
            out.append(klass({"enableRateLimit": True}))
        except Exception as exc:  # noqa: BLE001 —— 单所初始化失败不杀全局
            log.warning("init exchange %s failed: %s", name, exc)
    return out


def run_pass(exchanges: list, db_prior: str, now_s: int | None = None) -> dict:
    """One full-market diff per exchange. Writes only ``db_prior``."""
    stamp = int(time.time()) if now_s is None else int(now_s)
    path = Path(db_prior)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_prior)
    summary: dict = {}
    try:
        ensure_schema(conn)
        for ex in exchanges:
            exname = getattr(ex, "id", None) or "unknown"
            try:
                markets = ex.load_markets()
            except Exception as exc:  # noqa: BLE001 —— 单所失败 warning 跳过
                log.warning("load_markets %s failed: %s", exname, exc)
                continue
            if not isinstance(markets, dict):
                log.warning(
                    "load_markets %s returned %s; skip",
                    exname,
                    type(markets).__name__,
                )
                continue
            n_symbols, n_inserted = diff_and_store(conn, exname, markets, stamp)
            summary[exname] = {"symbols": n_symbols, "inserted": n_inserted}
            log.info(
                "cex_listing_events exchange=%s symbols=%s inserted=%s",
                exname,
                n_symbols,
                n_inserted,
            )
    finally:
        conn.close()
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="CCXT load_markets diff → prior.db.cex_listing_events (observe-only)"
    )
    parser.add_argument("--once", action="store_true", help="single pass then exit")
    parser.add_argument("--loop", action="store_true", help="repeat until interrupted")
    parser.add_argument(
        "--interval",
        type=int,
        default=_DEFAULT_INTERVAL,
        help=f"seconds between loop passes (default {_DEFAULT_INTERVAL})",
    )
    parser.add_argument(
        "--exchanges",
        default=None,
        help=f"comma list (default CCXT_EXCHANGES or {_DEFAULT_EXCHANGES})",
    )
    parser.add_argument("--db-prior", default=DB_PRIOR)
    args = parser.parse_args(argv)
    setup_logging()

    raw = args.exchanges if args.exchanges is not None else _exchanges_from_env()
    names = [part.strip() for part in raw.split(",") if part.strip()]
    exchanges = build_exchanges(names)
    if not exchanges:
        log.error("no usable exchanges (check CCXT_EXCHANGES / --exchanges)")
        return 2

    log.info(
        "listing watch start db=%s exchanges=%s interval=%s once=%s loop=%s",
        args.db_prior,
        [getattr(ex, "id", "?") for ex in exchanges],
        args.interval,
        args.once,
        args.loop,
    )

    def one_pass() -> None:
        summary = run_pass(exchanges, args.db_prior)
        inserted = sum(item["inserted"] for item in summary.values())
        log.info(
            "cex_listing_events pass exchanges=%s inserted=%s",
            list(summary),
            inserted,
        )

    try:
        if args.loop and not args.once:
            while True:
                try:
                    one_pass()
                except Exception as exc:  # noqa: BLE001 —— 单轮失败不杀循环
                    log.warning("listing watch pass failed: %s", exc)
                time.sleep(args.interval)
        one_pass()
        return 0
    except KeyboardInterrupt:
        log.info("listing watch interrupted")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
