#!/usr/bin/env python3
"""
Enqueue enrich jobs from pending_enrich / tier due tokens (single-node helper).

  python -m worker.scheduler --once
  python -m worker.scheduler --loop --interval 30
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from chains import chain_enabled  # noqa: E402
from config import DB_PATH  # noqa: E402
from logging_setup import get_logger, setup_logging  # noqa: E402
from store import Store  # noqa: E402
from worker.queue import create_queue  # noqa: E402

log = get_logger("worker.scheduler")


def enqueue_pending(*, db: str = DB_PATH, limit: int = 100) -> int:
    q = create_queue()
    n = 0
    with Store(db) as store:
        pending = store.list_pending_enrich()
        for chain_id, addrs in pending.items():
            if not chain_enabled(chain_id):
                continue
            for addr in addrs:
                if n >= limit:
                    break
                q.enqueue(
                    {
                        "type": "enrich",
                        "chain_id": chain_id,
                        "token_address": addr,
                    }
                )
                n += 1
            if n >= limit:
                break
    log.info("enqueued %s jobs stats=%s", n, q.stats())
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enqueue enrich jobs")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=float, default=30)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    setup_logging()
    if args.loop:
        while True:
            enqueue_pending(db=args.db, limit=args.limit)
            time.sleep(max(1.0, args.interval))
    enqueue_pending(db=args.db, limit=args.limit)
    print(create_queue().stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
