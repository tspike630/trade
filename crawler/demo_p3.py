#!/usr/bin/env python3
"""Verify P3: risk, backtest seed, grok context, worker queue."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from risk import HeuristicRiskScanner, apply_risk_to_score, assess_risk  # noqa: E402
from scoring import score_from_metrics  # noqa: E402
from store import Store  # noqa: E402


def main() -> int:
    print("=== demo_p3 ===")
    # Risk heuristic
    pair = {
        "liquidity": {"usd": 200},
        "volume": {"h24": 50000},
        "txns": {"h1": {"buys": 100, "sells": 1}},
    }
    r = HeuristicRiskScanner().scan("solana", "TokDemo", pair=pair, token_raw={})
    print(f"risk deduction={r.deduction} tags={r.tags} flags={r.flags}")
    assert r.deduction > 0
    assert any(t.startswith("risk_") for t in r.tags)
    base = score_from_metrics(liquidity_usd=200, volume_h24=50000, age_hours=2, txns_h24=50)
    ns, nt = apply_risk_to_score(base["score"], base["tags"], r)
    print(f"score {base['score']} -> {ns} tags={nt}")
    assert ns < base["score"]

    # Disabled path still works
    from risk import RiskResult
    assert assess_risk("solana", "x", pair=pair).source in ("heuristic", "composite", "disabled")

    # Worker queue
    from worker.queue import SqliteQueue
    with tempfile.TemporaryDirectory() as td:
        q = SqliteQueue(Path(td) / "q.db")
        jid = q.enqueue({"type": "enrich", "chain_id": "solana", "token_address": "abc123"})
        job = q.claim("demo-worker")
        assert job and job.id == jid
        q.complete(job.id, result={"ok": True})
        print(f"worker queue stats={q.stats()}")
        assert q.stats()["done"] == 1

    # Grok context
    from tools.grok_context import build_context, to_markdown
    with Store("dexscreener.db") as store:
        ctx = build_context(store, top_n=5, alerts_n=5)
    md = to_markdown(ctx)
    assert "Top quality scores" in md
    print(f"grok context stats={ctx['stats']} md_len={len(md)}")

    # Backtest with seed on temp db copy pattern
    import backtest as bt
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "bt.db")
        with Store(db) as store:
            n = bt.seed_demo_history(store, n_pairs=5, points=10)
            summary = bt.run_backtest(store, buy_after_min=5, hold_min=30)
        print(f"backtest seeded={n} trades={summary['trades']} win_rate={summary['win_rate']}")
        assert summary["trades"] >= 1

    # API route import
    from api_server import app
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/export/context" in paths
    print("API /export/context registered")
    print("DRY_RUN_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
