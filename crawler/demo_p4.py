#!/usr/bin/env python3
"""Verify P4: grok report, GoPlus risk parse/fallback, multi-strategy backtest, paper trade."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    print("=== demo_p4 ===")

    # 1) Grok report
    from scripts import grok_report as gr  # may fail if scripts not package

    # run via path
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "grok_report", ROOT / "scripts" / "grok_report.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rc = mod.main(["--mode", "hourly", "--incremental"])
    assert rc == 0
    latest = ROOT / "exports" / "reports" / "latest-hourly.md"
    assert latest.is_file() and "Grok crawler report" in latest.read_text()
    print(f"grok_report OK → {latest}")

    # 2) Risk GoPlus parse + fallback without live key
    from risk import (
        GoPlusRiskScanner,
        HeuristicRiskScanner,
        _parse_external_risk,
        assess_risk,
        get_risk_scanner,
    )
    import risk as risk_mod

    risk_mod._scanner = None  # reset singleton
    fixture = json.loads((ROOT / "fixtures" / "goplus_token_security.json").read_text())
    parsed = _parse_external_risk(fixture)
    # sell_tax 0.12 → 12% → high tax
    print(f"goplus_parse tags={parsed.tags} deduction={parsed.deduction}")
    assert "risk_high_tax" in parsed.tags

    gp = GoPlusRiskScanner(base_url="https://api.gopluslabs.io")
    ep = gp._endpoint("ethereum", "0xabc")
    assert ep and "token_security/1" in ep[0]
    ep_sol = gp._endpoint("solana", "So1ana")
    assert ep_sol and "solana/token_security" in ep_sol[0]
    # unsupported → notes
    assert gp.scan("unknownchain", "x").notes == "unsupported_chain"

    # no RISK_API_URL → heuristic only
    os.environ.pop("RISK_API_KEY", None)
    r = assess_risk(
        "solana",
        "tok",
        pair={
            "liquidity": {"usd": 100},
            "volume": {"h24": 10000},
            "txns": {"h1": {"buys": 50, "sells": 0}},
        },
    )
    print(f"assess_no_url source={r.source} notes={r.notes} tags={r.tags}")
    assert r.source in ("heuristic", "composite") or "heuristic" in (r.notes or "")
    # Simulate remote failure fallback
    from risk import CompositeRiskScanner, RiskResult

    class Boom(GoPlusRiskScanner):
        def scan(self, *a, **k):
            return RiskResult(source="goplus", notes="http_429")

    c = CompositeRiskScanner()
    c.remote = Boom()
    fb = c.scan(
        "solana",
        "tok",
        pair={"liquidity": {"usd": 100}, "volume": {"h24": 1}, "txns": {"h1": {"buys": 1, "sells": 1}}},
    )
    assert "fallback_heuristic" in (fb.notes or "")
    print(f"fallback OK notes={fb.notes}")

    # 3) Multi-strategy backtest
    import backtest as bt
    from store import Store

    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "bt.db")
        with Store(db) as store:
            bt.seed_demo_history(store, n_pairs=6, points=14)
            hold = bt.run_backtest(store, buy_after_min=5, hold_min=30, strategy="hold")
            tpsl = bt.run_backtest(
                store, buy_after_min=5, hold_min=60, strategy="tpsl", tp_pct=8, sl_pct=4
            )
            grid = bt.run_backtest(
                store,
                buy_after_min=5,
                hold_min=60,
                strategy="grid",
                grid_step_pct=3,
                grid_levels=3,
            )
        print(
            f"backtest hold trades={hold['trades']} tpsl={tpsl['trades']} "
            f"grid={grid['trades']} tpsl_reasons={tpsl.get('exit_reasons')}"
        )
        assert hold["trades"] and tpsl["trades"] and grid["trades"]

    # 4) Paper trading
    paper_db = str(ROOT / "paper_trading_demo.db")
    Path(paper_db).unlink(missing_ok=True)
    import paper_trade as pt

    rc = pt.main(["--paper-db", paper_db, "--db", str(ROOT / "dexscreener.db"), "demo"])
    assert rc == 0
    print("paper_trade demo OK")

    print("DRY_RUN_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
