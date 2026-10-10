#!/usr/bin/env python3
"""
Dump recent scores, history sample, and alerts for Grok Bot / 蜘蛛爬虫 ingest.

  python -m tools.grok_context
  python -m tools.grok_context --format markdown --out exports/grok_context.md

Also available as GET /export/context (API key).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import ALERT_MIN_SCORE, DB_PATH, ENABLED_CHAINS  # noqa: E402
from heartbeat import check_health, read_heartbeat  # noqa: E402
from store import Store  # noqa: E402


def build_context(
    store: Store,
    *,
    top_n: int = 15,
    alerts_n: int = 25,
    history_pairs: int = 5,
    history_points: int = 8,
) -> dict[str, Any]:
    ok, msg, hb = check_health()
    top = store._conn.execute(
        """
        SELECT chain_id, token_address, quality_score, quality_tags, scored_at
        FROM tokens
        WHERE quality_score IS NOT NULL
        ORDER BY quality_score DESC
        LIMIT ?
        """,
        (top_n,),
    ).fetchall()
    low = store._conn.execute(
        """
        SELECT chain_id, token_address, quality_score, quality_tags
        FROM tokens
        WHERE quality_score IS NOT NULL AND quality_score < ?
        ORDER BY quality_score ASC
        LIMIT ?
        """,
        (ALERT_MIN_SCORE, top_n),
    ).fetchall()

    hist_samples = []
    pairs = store._conn.execute(
        """
        SELECT chain_id, pair_address, COUNT(*) AS n
        FROM pair_history GROUP BY 1,2 ORDER BY n DESC LIMIT ?
        """,
        (history_pairs,),
    ).fetchall()
    for r in pairs:
        pts = store.list_pair_history(
            r["chain_id"], r["pair_address"], limit=history_points
        )
        hist_samples.append(
            {
                "chain_id": r["chain_id"],
                "pair_address": r["pair_address"],
                "points": len(pts),
                "series": [
                    {
                        "t": p["recorded_at"],
                        "price": p["price_usd"],
                        "liq": p["liquidity_usd"],
                        "vol_h24": p["volume_h24"],
                        "tier": p["tier"],
                    }
                    for p in pts
                ],
            }
        )

    alerts = store.list_alerts_api(limit=alerts_n)
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "dexscreener-crawler",
        "enabled_chains": ENABLED_CHAINS,
        "alert_min_score": ALERT_MIN_SCORE,
        "health": {"ok": ok, "message": msg, "heartbeat": hb or read_heartbeat()},
        "stats": {
            "tokens": store.count_tokens(),
            "pairs": store.count_pairs(),
            "alerts": store.count_alerts(),
            "pair_history_rows": store.count_pair_history(),
            "pending_enrich": store.count_pending_enrich(),
            "cycle_number": store.get_cycle_number(),
        },
        "top_scores": [dict(r) for r in top],
        "low_scores": [dict(r) for r in low],
        "history_samples": hist_samples,
        "recent_alerts": alerts,
    }


def to_markdown(ctx: dict[str, Any]) -> str:
    lines = [
        f"# DexScreener crawler context",
        f"",
        f"Generated: `{ctx['generated_at']}`",
        f"Chains: `{', '.join(ctx.get('enabled_chains') or [])}`",
        f"Health: **{'OK' if ctx.get('health', {}).get('ok') else 'FAIL'}** — {ctx.get('health', {}).get('message')}",
        f"",
        f"## Stats",
        f"",
        f"```json",
        json.dumps(ctx.get("stats") or {}, indent=2),
        f"```",
        f"",
        f"## Top quality scores",
        f"",
    ]
    for t in ctx.get("top_scores") or []:
        lines.append(
            f"- `{t.get('chain_id')}` `{str(t.get('token_address'))[:12]}…` "
            f"score=**{t.get('quality_score')}** tags=`{t.get('quality_tags')}`"
        )
    lines += ["", "## Low scores (< ALERT_MIN_SCORE)", ""]
    for t in ctx.get("low_scores") or []:
        lines.append(
            f"- `{t.get('chain_id')}` `{str(t.get('token_address'))[:12]}…` "
            f"score=**{t.get('quality_score')}**"
        )
    lines += ["", "## History samples", ""]
    for h in ctx.get("history_samples") or []:
        lines.append(
            f"### {h.get('chain_id')} `{str(h.get('pair_address'))[:14]}…` ({h.get('points')} pts)"
        )
        for p in h.get("series") or []:
            lines.append(
                f"- {p.get('t')}: price={p.get('price')} liq={p.get('liq')} tier={p.get('tier')}"
            )
    lines += ["", "## Recent alerts", ""]
    for a in ctx.get("recent_alerts") or []:
        lines.append(
            f"- `{a.get('created_at')}` **{a.get('type')}** ({a.get('severity')}) "
            f"{a.get('message', '')[:120]}"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export crawler context for Grok Bot")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--format", choices=("json", "markdown", "both"), default="both")
    parser.add_argument("--out", default="", help="Output path (suffix inferred if both)")
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--alerts", type=int, default=25)
    args = parser.parse_args(argv)

    with Store(args.db) as store:
        ctx = build_context(store, top_n=args.top, alerts_n=args.alerts)

    exports = Path(ROOT) / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    if args.format in ("json", "both"):
        path = Path(args.out) if args.out and args.format == "json" else exports / f"grok_context-{stamp}.json"
        if args.out and args.format == "both":
            path = Path(args.out).with_suffix(".json")
        path.write_text(json.dumps(ctx, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"JSON → {path}")
    if args.format in ("markdown", "both"):
        md = to_markdown(ctx)
        path = Path(args.out) if args.out and args.format == "markdown" else exports / f"grok_context-{stamp}.md"
        if args.out and args.format == "both":
            path = Path(args.out).with_suffix(".md")
        path.write_text(md, encoding="utf-8")
        print(f"Markdown → {path}")
        if args.format == "markdown":
            print(md[:1500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
