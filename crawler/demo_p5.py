#!/usr/bin/env python3
"""Verify P5: live dry-run, new strategies, arb scan, DD report — no real exchange keys."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    print("=== demo_p5 ===")

    # 1) Live trading — must stay dry-run
    import live_trade as lt

    allowed, reason = lt.live_gates_open()
    assert allowed is False, reason
    assert lt.kill_switch_active()
    r = lt.submit_intent(
        lt.OrderIntent(side="buy", symbol="DEMO", size_usd=5.0), force_dry=True
    )
    assert r.dry_run and r.ok
    print(f"live_trade dry-run OK gate={reason} order_id={r.exchange_order_id}")
    st = lt.main(["status"])
    assert st == 0

    # oversize rejected
    bad = lt.submit_intent(lt.OrderIntent(side="buy", symbol="X", size_usd=1e9))
    assert bad.ok is False
    print(f"size_cap OK msg={bad.message[:60]}")

    # 2) Strategies
    import backtest as bt
    from store import Store

    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "p5.db")
        with Store(db) as store:
            bt.seed_demo_history(store, n_pairs=8, points=16)
            for strat in ("dca", "martingale", "trend"):
                s = bt.run_backtest(
                    store, buy_after_min=5, hold_min=40, strategy=strat, limit_pairs=8
                )
                print(f"backtest {strat} trades={s['trades']} win_rate={s['win_rate']}")
                assert s["trades"] >= 1

            # arb seed: same symbol two chains
            for chain, pair, price in (
                ("solana", "arbpairSol" + "1" * 30, 1.0),
                ("ethereum", "arbpairEth" + "2" * 30, 1.08),
            ):
                tok = ("arbtok" + chain[:3] + "3" * 40)[:42]
                store.upsert_token(
                    chain, tok, {"chainId": chain, "tokenAddress": tok}, "arb_seed"
                )
                store.upsert_pair(
                    chain,
                    pair[:44],
                    {
                        "chainId": chain,
                        "pairAddress": pair[:44],
                        "priceUsd": price,
                        "liquidity": {"usd": 50000},
                        "volume": {"h24": 10000},
                        "baseToken": {"address": tok, "symbol": "ARBDEMO"},
                    },
                    "arb_seed",
                    token_address=tok,
                )

            import arb_scan as arb

            opps = arb.scan_spreads(store, min_spread_pct=3.0, min_liq_usd=1000)
            print(f"arb_opps={len(opps)} sample={opps[:1]}")
            assert any(o["key"] == "ARBDEMO" for o in opps)

    # paper simulate
    import paper_trade as pt

    pdb = str(ROOT / "paper_p5_demo.db")
    Path(pdb).unlink(missing_ok=True)
    # ensure history on main db via seed into temp already done; seed main lightly
    with Store(str(ROOT / "dexscreener.db")) as store:
        bt.seed_demo_history(store, n_pairs=2, points=12)
    rc = pt.main(
        [
            "--paper-db",
            pdb,
            "--db",
            str(ROOT / "dexscreener.db"),
            "simulate",
            "--strategy",
            "dca",
            "--size-usd",
            "50",
        ]
    )
    assert rc == 0
    print("paper simulate dca OK")

    # 3) arb_scan dry-run CLI on main db
    rc = arb.main(["--db", str(ROOT / "dexscreener.db"), "--dry-run", "--min-spread", "2"])
    assert rc == 0

    # 4) DD report — pick any scored token
    with Store(str(ROOT / "dexscreener.db")) as store:
        row = store._conn.execute(
            "SELECT chain_id, token_address FROM tokens WHERE quality_score IS NOT NULL LIMIT 1"
        ).fetchone()
    assert row
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "grok_dd_report", ROOT / "scripts" / "grok_dd_report.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rc = mod.main(
        ["--chain", row["chain_id"], "--token", row["token_address"]]
    )
    assert rc == 0
    print("dd_report OK")

    print("DRY_RUN_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
