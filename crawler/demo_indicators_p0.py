#!/usr/bin/env python3
"""Verify P0 indicator API helpers + DD context (read-only; no crawl stop)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config import DB_PATH  # noqa: E402
from indicators.base import IndicatorStore  # noqa: E402
from store import Store  # noqa: E402


def main() -> int:
    print("=== demo_indicators_p0 ===")

    # Route registration
    from api_server import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/indicators/top" in paths, paths
    assert "/indicators/{chain}/{token_address}" in paths, paths
    print("API routes registered: /indicators/top , /indicators/{chain}/{token_address}")

    with IndicatorStore(DB_PATH) as ist:
        n = ist.count_token_indicators()
        print(f"token_indicators count={n}")
        assert n >= 0
        # whitelist sort
        for sort in IndicatorStore.TOP_SORT_COLUMNS:
            items = ist.list_top_token_indicators(chain="solana", sort=sort, limit=5)
            print(f"  top sort={sort} -> {len(items)}")
            assert len(items) <= 5
        try:
            ist.list_top_token_indicators(sort="not_a_col", limit=3)
            raise AssertionError("expected ValueError for bad sort")
        except ValueError as e:
            print(f"  bad sort rejected: {e}")

        sample = ist.list_top_token_indicators(
            chain="solana", sort="volume_strength", limit=1
        )
        if sample:
            row = ist.get_token_indicators(
                sample[0]["chain_id"], sample[0]["token_address"], use_cache=False
            )
            assert row is not None
            print(
                "  sample token="
                f"{row['chain_id']}/{row['token_address'][:12]}… "
                f"vol={row.get('volume_strength')} jev_strength={row.get('jev_strength')}"
            )
            # shape like API
            from api_server import _format_indicator_row

            shaped = _format_indicator_row(row)
            assert "technical" in shaped and "funding" in shaped
            assert "sentiment" in shaped and "composite" in shaped
            print("  shaped JSON snippet:")
            print(json.dumps(shaped, ensure_ascii=False, indent=2)[:1200])
        else:
            print("  (no sample rows — skip spot JSON)")

    # DD assemble includes indicators without failing
    from scripts.grok_dd_report import assemble_token_context, render_dd_markdown

    with Store(DB_PATH) as store:
        # pick any token that has indicators if possible
        tok = None
        with IndicatorStore(DB_PATH) as ist:
            tops = ist.list_top_token_indicators(sort="change_5m", limit=1)
            if tops:
                tok = (tops[0]["chain_id"], tops[0]["token_address"])
        if tok is None:
            # fallback: any token
            rows = store.list_tokens_api(limit=1)
            assert rows
            tok = (rows[0]["chain_id"], rows[0]["token_address"])
        ctx = assemble_token_context(store, tok[0], tok[1])
        assert "indicators" in ctx
        md = render_dd_markdown(ctx)
        assert "技术面" in md or "Technical" in md
        assert "资金面" in md or "Funding" in md
        assert "舆情面" in md or "Sentiment" in md
        print(f"DD ok for {tok[0]}/{tok[1][:12]}… md_len={len(md)} indicators={'yes' if ctx.get('indicators') else '暂无数据'}")

    # Composite skeleton stays no-op at weight 0
    from config import (
        INDICATORS_WEIGHT_FUNDING,
        INDICATORS_WEIGHT_SENTIMENT,
        INDICATORS_WEIGHT_TECHNICAL,
    )
    from indicators.composite import CompositeIndicators

    assert INDICATORS_WEIGHT_TECHNICAL == 0
    assert INDICATORS_WEIGHT_FUNDING == 0
    assert INDICATORS_WEIGHT_SENTIMENT == 0
    with IndicatorStore(DB_PATH) as ist:
        cstats = CompositeIndicators(ist).run_batch()
        assert cstats["ok"] == 0 and cstats["seen"] == 0
        print(f"  composite weight-0 no-op: {cstats}")

    print("DRY_RUN_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
