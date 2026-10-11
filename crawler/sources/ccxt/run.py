"""
CCXT side-channel CLI (read-only, default off).

  python -m sources.ccxt --once
  python -m sources.ccxt --loop --interval 300
  python -m sources.ccxt --once --exchanges binance,okx --limit 20
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from logging_setup import get_logger, setup_logging  # noqa: E402

from sources.ccxt import config as ccxt_config  # noqa: E402
from sources.ccxt.core import CcxtFetcher  # noqa: E402

log = get_logger("sources.ccxt.run")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="CCXT market-data side channel (ticks/ohlcv/listings into ccxt.db)"
    )
    parser.add_argument(
        "--once", action="store_true", help="single pass then exit"
    )
    parser.add_argument(
        "--loop", action="store_true", help="continuous loop until interrupted"
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help=f"seconds between passes (default {ccxt_config.INTERVAL_SEC})",
    )
    parser.add_argument(
        "--exchanges",
        default=None,
        help=f"comma list (default {','.join(ccxt_config.EXCHANGES)})",
    )
    parser.add_argument(
        "--timeframe",
        default=None,
        help=f"ohlcv timeframe (default {ccxt_config.TIMEFRAME})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=f"top N symbols per exchange (default {ccxt_config.TOP_LIMIT})",
    )
    parser.add_argument(
        "--db", default=None, help=f"sqlite path (default {ccxt_config.DB_PATH})"
    )
    args = parser.parse_args(argv)

    setup_logging()

    exchanges = (args.exchanges or ",".join(ccxt_config.EXCHANGES)).split(",")
    timeframe = args.timeframe or ccxt_config.TIMEFRAME
    top_limit = args.limit if args.limit is not None else ccxt_config.TOP_LIMIT
    db_path = args.db or ccxt_config.DB_PATH
    interval = (
        args.interval
        if args.interval is not None
        else float(ccxt_config.INTERVAL_SEC)
    )

    fetcher = CcxtFetcher(
        exchanges,
        db_path=db_path,
        top_limit=top_limit,
        min_quote_volume=ccxt_config.MIN_QUOTE_VOLUME,
        ohlcv_limit=ccxt_config.OHLCV_LIMIT,
    )
    if not fetcher.exchanges:
        log.error("no usable exchanges (check CCXT_EXCHANGES / install ccxt)")
        return 2

    log.info(
        "ccxt start db=%s exchanges=%s timeframe=%s top=%s interval=%s once=%s loop=%s",
        db_path,
        [e.id for e in fetcher.exchanges],
        timeframe,
        top_limit,
        interval,
        args.once,
        args.loop,
    )

    def one_pass() -> None:
        summary = fetcher.fetch_and_store(timeframe)
        for exname, s in summary.items():
            log.info(
                "ccxt pass exchange=%s symbols=%s ticks=%s ohlcv=%s new_listings=%s",
                exname, s["symbols"], s["ticks"], s["ohlcv"], s["new_listings"],
            )

    try:
        if args.once:
            one_pass()
        elif args.loop:
            while True:
                one_pass()
                time.sleep(interval)
        else:
            # default: single pass (safe), loop only when asked
            one_pass()
        return 0
    except KeyboardInterrupt:
        log.info("ccxt interrupted")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
