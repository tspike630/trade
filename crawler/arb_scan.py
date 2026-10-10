#!/usr/bin/env python3
"""
Multi-venue / cross-chain spread scanner (SIGNAL ONLY — not executable arb).

Compares latest prices for the same base token symbol (or token_address when
shared) across chains/pairs in the local DB. Emits alert type=arb_opportunity
when spread >= ARB_MIN_SPREAD_PCT and both sides meet ARB_MIN_LIQ_USD.

  python arb_scan.py
  python arb_scan.py --min-spread 5 --dry-run

Does NOT place trades, bridge, or call DEX routers.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from typing import Any

from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from alerts import emit_alert  # noqa: E402
from config import ARB_MIN_LIQ_USD, ARB_MIN_SPREAD_PCT, DB_PATH  # noqa: E402
from logging_setup import get_logger, setup_logging  # noqa: E402
from store import Store  # noqa: E402

log = get_logger("arb_scan")


def _pair_metrics(raw: dict[str, Any]) -> dict[str, Any] | None:
    try:
        price = float(raw.get("priceUsd"))
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    liq = raw.get("liquidity") if isinstance(raw.get("liquidity"), dict) else {}
    try:
        liq_usd = float(liq.get("usd") or 0)
    except (TypeError, ValueError):
        liq_usd = 0.0
    base = raw.get("baseToken") if isinstance(raw.get("baseToken"), dict) else {}
    symbol = (base.get("symbol") or "").strip().upper()
    addr = (base.get("address") or raw.get("token_address") or "").strip().lower()
    return {
        "price": price,
        "liq_usd": liq_usd,
        "symbol": symbol,
        "token_address": addr,
    }


def scan_spreads(
    store: Store,
    *,
    min_spread_pct: float,
    min_liq_usd: float,
) -> list[dict[str, Any]]:
    """
    Group by symbol (fallback token_address). Require ≥2 venues with liq filter.
    """
    rows = store._conn.execute(
        "SELECT chain_id, pair_address, token_address, raw_json FROM pairs"
    ).fetchall()
    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        try:
            raw = json.loads(r["raw_json"])
        except json.JSONDecodeError:
            continue
        m = _pair_metrics(raw)
        if not m or m["liq_usd"] < min_liq_usd:
            continue
        key = m["symbol"] if m["symbol"] and len(m["symbol"]) >= 2 else m["token_address"]
        if not key:
            continue
        by_key[key].append(
            {
                "chain_id": r["chain_id"],
                "pair_address": r["pair_address"],
                "token_address": r["token_address"] or m["token_address"],
                **m,
            }
        )

    opportunities: list[dict[str, Any]] = []
    for key, venues in by_key.items():
        if len(venues) < 2:
            continue
        # distinct chains preferred
        chains = {v["chain_id"] for v in venues}
        if len(chains) < 2 and len(venues) < 2:
            continue
        richest = sorted(venues, key=lambda v: v["price"])
        low, high = richest[0], richest[-1]
        if low["price"] <= 0:
            continue
        spread = (high["price"] - low["price"]) / low["price"] * 100.0
        if spread < min_spread_pct:
            continue
        # skip same chain identical pair
        if (
            low["chain_id"] == high["chain_id"]
            and low["pair_address"] == high["pair_address"]
        ):
            continue
        opportunities.append(
            {
                "key": key,
                "spread_pct": round(spread, 4),
                "buy_venue": {
                    "chain_id": low["chain_id"],
                    "pair_address": low["pair_address"],
                    "price": low["price"],
                    "liq_usd": low["liq_usd"],
                },
                "sell_venue": {
                    "chain_id": high["chain_id"],
                    "pair_address": high["pair_address"],
                    "price": high["price"],
                    "liq_usd": high["liq_usd"],
                },
                "note": "signal_only_not_executable_arb",
            }
        )
    opportunities.sort(key=lambda x: x["spread_pct"], reverse=True)
    return opportunities


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cross-chain/pair spread signals (not executable arb)"
    )
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--min-spread", type=float, default=ARB_MIN_SPREAD_PCT)
    parser.add_argument("--min-liq", type=float, default=ARB_MIN_LIQ_USD)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print opportunities without writing alerts",
    )
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    setup_logging()

    with Store(args.db) as store:
        opps = scan_spreads(
            store, min_spread_pct=args.min_spread, min_liq_usd=args.min_liq
        )[: args.limit]
        print(f"arb_opportunities={len(opps)} min_spread={args.min_spread}%")
        for o in opps[:10]:
            print(
                f"  {o['key']}: {o['spread_pct']}% "
                f"buy {o['buy_venue']['chain_id']}/{o['buy_venue']['pair_address'][:8]}… "
                f"@ {o['buy_venue']['price']} → sell {o['sell_venue']['chain_id']}/… "
                f"@ {o['sell_venue']['price']}"
            )
        if args.dry_run:
            print("DRY_RUN no alerts written")
            Path("exports").mkdir(exist_ok=True)
            Path("exports/arb_scan_last.json").write_text(
                json.dumps(opps, indent=2) + "\n", encoding="utf-8"
            )
            return 0
        emitted = 0
        for o in opps:
            buy = o["buy_venue"]
            alert = {
                "type": "arb_opportunity",
                "severity": "medium" if o["spread_pct"] < 10 else "high",
                "chain_id": buy["chain_id"],
                "token_address": "",
                "pair_address": buy["pair_address"],
                "message": (
                    f"arb_opportunity {o['key']} spread={o['spread_pct']}% "
                    f"(signal only — not executable)"
                ),
                "payload": o,
            }
            if emit_alert(store, alert, cycle_id="arb-scan"):
                emitted += 1
        print(f"alerts_emitted={emitted}")
        Path("exports").mkdir(exist_ok=True)
        Path("exports/arb_scan_last.json").write_text(
            json.dumps(opps, indent=2) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
