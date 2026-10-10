#!/usr/bin/env python3
"""
P1.2 Helius dry-run: parse fixture logsNotification + getTransaction mint extract.
Does NOT open a live WebSocket unless HELIUS_API_KEY is set and --live is passed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config import HELIUS_API_KEY  # noqa: E402
from sources.solana_helius import (  # noqa: E402
    DEFAULT_HELIUS_PROGRAM_IDS,
    extract_mints_from_logs,
    extract_mints_from_transaction,
    helius_enabled,
    parse_logs_notification,
    resolve_program_ids,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Helius parser dry-run / optional live probe")
    parser.add_argument(
        "--fixture",
        default="fixtures/helius_logs_notification.json",
        help="logsNotification JSON fixture",
    )
    parser.add_argument(
        "--tx-fixture",
        default="fixtures/helius_get_transaction.json",
        help="getTransaction result JSON fixture",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="If HELIUS_API_KEY set, open WS briefly and exit (smoke)",
    )
    parser.add_argument("--live-seconds", type=float, default=8.0)
    args = parser.parse_args(argv)

    print("=== demo_helius dry-run ===")
    print(f"HELIUS_API_KEY set: {bool(HELIUS_API_KEY)}")
    print(f"helius_enabled(): {helius_enabled()}")
    print("Default program IDs:")
    for pid, label in DEFAULT_HELIUS_PROGRAM_IDS:
        print(f"  {pid}  # {label}")
    print(f"resolve_program_ids() count={len(resolve_program_ids())}")

    fix = Path(args.fixture)
    if not fix.is_file():
        print(f"FAIL: missing fixture {fix}")
        return 1
    msg = json.loads(fix.read_text(encoding="utf-8"))
    parsed = parse_logs_notification(msg)
    if not parsed:
        print("FAIL: parse_logs_notification returned None")
        return 1
    print(f"parsed signature={parsed['signature'][:24]}...")
    print(f"  err={parsed['err']} logs={len(parsed['logs'])} slot={parsed.get('slot')}")

    programs = resolve_program_ids()
    mints = extract_mints_from_logs(parsed["logs"], program_ids=programs)
    print(f"mints_from_logs ({len(mints)}):")
    for m in mints:
        print(f"  {m}")
    if not mints:
        print("WARN: no mints from logs (fixture may need richer lines)")

    tx_path = Path(args.tx_fixture)
    if tx_path.is_file():
        tx = json.loads(tx_path.read_text(encoding="utf-8"))
        tx_mints = extract_mints_from_transaction(tx)
        print(f"mints_from_transaction ({len(tx_mints)}):")
        for m in tx_mints:
            print(f"  {m}")
        if not tx_mints:
            print("FAIL: expected mints from tx fixture")
            return 1
    else:
        print(f"WARN: no tx fixture at {tx_path}")

    # Import / source smoke
    from sources import available_source_names, build_sources
    import asyncio
    from sources.solana_helius import HeliusSolanaSource

    assert "helius" in available_source_names()
    src = build_sources(["helius"])[0]
    assert isinstance(src, HeliusSolanaSource)
    result = asyncio.run(src.fetch())
    print(f"HeliusSolanaSource.fetch notes={result.notes}")

    print("DRY_RUN_OK")

    if args.live:
        if not helius_enabled():
            print("LIVE_SKIP: HELIUS_API_KEY unset")
            return 0
        return _live_smoke(args.live_seconds)
    return 0


def _live_smoke(seconds: float) -> int:
    import asyncio
    import time

    from sources.solana_helius import HeliusListener

    print(f"LIVE: starting HeliusListener for ~{seconds}s ...")
    listener = HeliusListener()
    listener.start_background()
    time.sleep(max(1.0, seconds))
    listener.stop()
    print("LIVE_OK (session ran; check logs for subscribe)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
