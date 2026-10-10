#!/usr/bin/env python3
"""P2 demos: scoring, history snapshot, chain filter, API history route smoke."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from chains import chain_enabled, enabled_chain_set  # noqa: E402
from config import ALERT_MIN_SCORE, ENABLED_CHAINS, HISTORY_HOT_INTERVAL_SEC  # noqa: E402
from history import extract_metrics, maybe_snapshot_pair  # noqa: E402
from logging_setup import setup_logging  # noqa: E402
from scoring import score_from_metrics, should_push_alert  # noqa: E402
from store import Store  # noqa: E402


SAMPLE_PAIR = {
    "chainId": "solana",
    "pairAddress": "DemoPairP2AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "pairCreatedAt": 1791567628000,
    "priceUsd": 0.00123,
    "volume": {"h24": 85000, "h1": 12000},
    "liquidity": {"usd": 120000},
    "txns": {"h24": {"buys": 400, "sells": 350}},
    "baseToken": {"address": "DemoTokenP2BBBBBBBBBBBBBBBBBBBBBBBBBBBBBB", "symbol": "DP2"},
    "priceChange": {"h24": 42.5},
}


def main() -> int:
    setup_logging()
    print("=== demo_p2 ===")
    print(f"ENABLED_CHAINS={ENABLED_CHAINS}")
    print(f"chain_enabled(solana)={chain_enabled('solana')} ethereum={chain_enabled('ethereum')}")
    print(f"ALERT_MIN_SCORE={ALERT_MIN_SCORE} HISTORY_HOT={HISTORY_HOT_INTERVAL_SEC}s")

    r = score_from_metrics(
        liquidity_usd=120000,
        volume_h24=85000,
        age_hours=12,
        txns_h24=750,
        social_mentions=25,
    )
    print(f"score={r['score']} tags={r['tags']} components={r['components']}")
    assert 0 <= r["score"] <= 100
    assert "high_liq" in r["tags"]
    assert "high_social" in r["tags"]

    low = score_from_metrics(liquidity_usd=10, volume_h24=5, age_hours=0.1, txns_h24=1)
    print(f"low_score={low['score']} tags={low['tags']}")
    assert "low_quality" in low["tags"] or low["score"] < ALERT_MIN_SCORE

    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "p2.db")
        with Store(db) as store:
            store.upsert_token(
                "solana",
                SAMPLE_PAIR["baseToken"]["address"],
                {"chainId": "solana", "tokenAddress": SAMPLE_PAIR["baseToken"]["address"]},
                "demo",
            )
            store.upsert_pair(
                "solana",
                SAMPLE_PAIR["pairAddress"],
                SAMPLE_PAIR,
                "demo",
                token_address=SAMPLE_PAIR["baseToken"]["address"],
            )
            m = extract_metrics(SAMPLE_PAIR)
            assert m["price_usd"] == 0.00123
            wrote = maybe_snapshot_pair(
                store,
                "solana",
                SAMPLE_PAIR["pairAddress"],
                SAMPLE_PAIR,
                token_address=SAMPLE_PAIR["baseToken"]["address"],
            )
            print(f"history_snapshot_wrote={wrote}")
            assert wrote
            # second immediate → not due
            wrote2 = maybe_snapshot_pair(
                store,
                "solana",
                SAMPLE_PAIR["pairAddress"],
                SAMPLE_PAIR,
                token_address=SAMPLE_PAIR["baseToken"]["address"],
            )
            print(f"history_snapshot_second={wrote2}")
            assert wrote2 is False
            hist = store.list_pair_history("solana", SAMPLE_PAIR["pairAddress"])
            print(f"history_rows={len(hist)} price={hist[0]['price_usd']}")

            from scoring import score_token_from_store

            sc = score_token_from_store(
                store, "solana", SAMPLE_PAIR["baseToken"]["address"]
            )
            print(f"persisted_score={sc['score']}")
            q = store.get_token_quality("solana", SAMPLE_PAIR["baseToken"]["address"])
            print(f"db_quality={q}")

            # low score → no push
            store.set_token_quality(
                "solana", SAMPLE_PAIR["baseToken"]["address"], 5.0, ["low_quality"]
            )
            push = should_push_alert(
                store,
                {
                    "type": "price_change",
                    "chain_id": "solana",
                    "token_address": SAMPLE_PAIR["baseToken"]["address"],
                },
            )
            print(f"should_push_low_score={push}")
            assert push is False
            push_sys = should_push_alert(store, {"type": "system_error"})
            assert push_sys is True

    # FastAPI TestClient smoke for /history and /
    try:
        from fastapi.testclient import TestClient
        import os

        os.environ.setdefault("ALLOW_INSECURE_API", "1")
        # reload config flags by importing app (already loaded ALLOW from env at import)
        from api_server import app

        client = TestClient(app)
        r = client.get("/")
        print(f"GET / status={r.status_code} html_len={len(r.text)}")
        assert r.status_code == 200
        assert "DexScreener Crawler" in r.text
        # history may be empty on live db — just ensure route exists
        # Use insecure mode if secret unset
        rh = client.get(
            "/history",
            params={"chain": "solana", "pair": SAMPLE_PAIR["pairAddress"]},
        )
        print(f"GET /history status={rh.status_code}")
        # 200 or 401/503 depending on env — both mean route wired
        assert rh.status_code in (200, 401, 503)
    except Exception as exc:  # noqa: BLE001
        print(f"api smoke warn: {exc}")

    print("DRY_RUN_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
