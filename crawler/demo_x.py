#!/usr/bin/env python3
"""
P1.3 X social dry-run: parse fixture search JSON, spike detection, budget fuse sim.
Does NOT call live X unless X_BEARER_TOKEN is set and --live is passed.
Never prints the bearer token.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config import X_BEARER_TOKEN  # noqa: E402
from logging_setup import setup_logging  # noqa: E402
from sources.x_api import (  # noqa: E402
    BudgetFuse,
    TokenBucket,
    build_search_query,
    detect_spike,
    extract_addresses_from_texts,
    parse_search_response,
    x_enabled,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="X social source dry-run")
    parser.add_argument(
        "--fixture",
        default="fixtures/x_search_recent.json",
        help="tweets/search/recent JSON fixture",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="If X_BEARER_TOKEN set, run one real search for a harmless query",
    )
    args = parser.parse_args(argv)
    setup_logging()

    print("=== demo_x dry-run ===")
    print(f"X_BEARER_TOKEN set: {bool(X_BEARER_TOKEN)}")
    print(f"x_enabled(): {x_enabled()}")
    # Never print token value
    print(f"query sample: {build_search_query('DEMO', 'He1iusDemoMintAAAA1111111111111111111111111')}")

    fix = Path(args.fixture)
    if not fix.is_file():
        print(f"FAIL: missing fixture {fix}")
        return 1
    raw = json.loads(fix.read_text(encoding="utf-8"))
    parsed = parse_search_response(raw)
    print(
        f"parsed result_count={parsed['result_count']} "
        f"newest_id={parsed['newest_id']} tweets={len(parsed['tweets'])}"
    )
    addrs = extract_addresses_from_texts(parsed["texts"])
    print(f"addresses_from_texts ({len(addrs)}):")
    for a in addrs:
        print(f"  {a}")

    # Spike matrix
    cases = [(0, 12), (2, 8), (5, 6), (10, 80), (3, 3)]
    print("detect_spike cases:")
    for prev, last in cases:
        ok, kind = detect_spike(prev, last)
        print(f"  prev={prev} last={last} → spike={ok} kind={kind or '-'}")

    # Budget fuse on temp DB
    from store import Store

    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "demo_x.db")
        with Store(db) as store:
            fuse = BudgetFuse(store)
            assert fuse.allow()
            for _ in range(3):
                fuse.record()
            d, h = fuse.remaining()
            print(f"budget after 3 records: day_left={d} hour_left={h}")
            store.upsert_x_watch(
                "solana",
                "He1iusDemoMintAAAA1111111111111111111111111",
                symbol="DEMO",
                query_key=build_search_query(
                    "DEMO", "He1iusDemoMintAAAA1111111111111111111111111"
                ),
                mode="early",
            )
            store.update_x_watch_search(
                "solana",
                "He1iusDemoMintAAAA1111111111111111111111111",
                since_id=parsed["newest_id"],
                mention_count_last=parsed["result_count"],
                bump_total=parsed["result_count"],
            )
            w = store.get_x_watch(
                "solana", "He1iusDemoMintAAAA1111111111111111111111111"
            )
            print(
                f"x_watch since_id={w and w.get('since_id')} "
                f"last={w and w.get('mention_count_last')} "
                f"total={w and w.get('mention_count_total')}"
            )

    bucket = TokenBucket(capacity=2, refill_per_sec=100)
    assert bucket.try_consume(1)
    assert bucket.try_consume(1)
    assert not bucket.try_consume(1)
    print("token_bucket OK")

    # Import / source smoke (no live)
    import asyncio
    from sources import available_source_names, build_sources
    from sources.x_api import XApiSource

    assert "x" in available_source_names()
    src = build_sources(["x"])[0]
    assert isinstance(src, XApiSource)
    result = asyncio.run(src.fetch())
    print(f"XApiSource.fetch notes={result.notes}")
    assert result.notes == "no_bearer" or "no_store" in result.notes or x_enabled()

    print("DRY_RUN_OK")

    if args.live:
        if not x_enabled():
            print("LIVE_SKIP: X_BEARER_TOKEN unset")
            return 0
        return _live_one()
    return 0


def _live_one() -> int:
    import asyncio

    from sources.x_api import search_recent

    async def _run() -> None:
        # Harmless short query; still counts against budget
        data = await search_recent("bitcoin -is:retweet", bearer=X_BEARER_TOKEN, max_results=10)
        parsed = parse_search_response(data)
        print(f"LIVE_OK result_count={parsed['result_count']} newest_id={parsed['newest_id']}")

    try:
        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        # redacted by search_recent messages already
        print(f"LIVE_FAIL: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
