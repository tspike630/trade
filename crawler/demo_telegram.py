"""Dry-run Telegram formatting + routing; optionally send a live test if token set."""

from __future__ import annotations

import os
import sys

from alerts_telegram import (
    TelegramAlerter,
    format_alert_html,
    resolve_chat_id,
    telegram_enabled,
)
from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TELEGRAM_CHAT_ID_ADMIN,
    TELEGRAM_CHAT_ID_ALERT,
    TELEGRAM_CHAT_ID_MAIN,
)
from logging_setup import setup_logging, get_logger


SAMPLES = [
    {
        "type": "new_listing",
        "severity": "info",
        "chain_id": "solana",
        "token_address": "ExampleMint1111111111111111111111111111111",
        "pair_address": "",
        "message": "new token in profiles/latest",
        "payload": {"description": "Demo token for Telegram dry-run", "source": "token-profiles/latest"},
    },
    {
        "type": "cto_new",
        "severity": "medium",
        "chain_id": "ethereum",
        "token_address": "0xabc123",
        "pair_address": "",
        "message": "new CTO token",
        "payload": {"description": "Community takeover demo"},
    },
    {
        "type": "price_change",
        "severity": "high",
        "chain_id": "solana",
        "token_address": "TokenAAA",
        "pair_address": "PairBBB",
        "message": "priceUsd changed +55%",
        "payload": {
            "old_priceUsd": 0.001,
            "new_priceUsd": 0.00155,
            "change_pct": 55.0,
            "threshold_pct": 20.0,
        },
    },
    {
        "type": "volume_h24_jump",
        "severity": "medium",
        "chain_id": "solana",
        "token_address": "TokenAAA",
        "pair_address": "PairBBB",
        "message": "volume jump",
        "payload": {
            "old_volume_h24": 1000,
            "new_volume_h24": 2500,
            "change_pct": 150.0,
            "threshold_pct": 50.0,
        },
    },
    {
        "type": "liquidity_drop",
        "severity": "high",
        "chain_id": "solana",
        "token_address": "TokenAAA",
        "pair_address": "PairBBB",
        "message": "liq drop",
        "payload": {
            "old_liquidity_usd": 100000,
            "new_liquidity_usd": 40000,
            "drop_pct": 60.0,
            "threshold_pct": 30.0,
        },
    },
    {
        "type": "proxy_pool_low",
        "severity": "high",
        "chain_id": "",
        "token_address": "",
        "pair_address": "",
        "message": "proxy pool availability 20% below threshold 30%",
        "payload": {"availability": 0.2, "threshold": 0.3},
    },
]


def main() -> int:
    setup_logging()
    log = get_logger("demo_telegram")

    print("=== routing (dry) ===")
    # Temporarily show with env as-is
    for a in SAMPLES:
        chat = resolve_chat_id(a["type"])
        print(f"  {a['type']:20} → chat_id={chat or '(unset — need TELEGRAM_CHAT_ID_*)'}")

    print("\n=== HTML samples (dry) ===")
    for a in SAMPLES[:3]:
        html = format_alert_html(a, cycle_id="demo-cycle")
        print(f"\n--- {a['type']} ---")
        print(html)

    print("\n=== env status ===")
    print(f"  TELEGRAM_BOT_TOKEN set: {bool(TELEGRAM_BOT_TOKEN)}")
    print(f"  TELEGRAM_CHAT_ID: {TELEGRAM_CHAT_ID or '-'}")
    print(f"  MAIN/ALERT/ADMIN: {TELEGRAM_CHAT_ID_MAIN or '-'}/"
          f"{TELEGRAM_CHAT_ID_ALERT or '-'}/{TELEGRAM_CHAT_ID_ADMIN or '-'}")
    print(f"  telegram_enabled(): {telegram_enabled()}")

    # Unit asserts
    assert "DexScreener" in format_alert_html(SAMPLES[0], cycle_id="x")
    assert "priceUsd" in format_alert_html(SAMPLES[2], cycle_id="x")
    assert "Admin" in format_alert_html(SAMPLES[5], cycle_id="x")

    # Routing with fallback simulation
    os.environ.setdefault("TELEGRAM_CHAT_ID", "999")
    # reload resolve uses config module constants — test function with monkey values
    from alerts_telegram import _MAIN_TYPES, _ALERT_TYPES, _ADMIN_TYPES

    assert SAMPLES[0]["type"] in _MAIN_TYPES
    assert SAMPLES[2]["type"] in _ALERT_TYPES
    assert SAMPLES[5]["type"] in _ADMIN_TYPES
    print("\nDRY_RUN_OK formatting+routing")

    if not telegram_enabled():
        print("LIVE_SEND skipped: TELEGRAM_BOT_TOKEN not set")
        print("Enable with:")
        print("  export TELEGRAM_BOT_TOKEN=...")
        print("  export TELEGRAM_CHAT_ID_ADMIN=...   # or TELEGRAM_CHAT_ID")
        print("  python demo_telegram.py --send")
        return 0

    if "--send" not in sys.argv:
        print("Token present — re-run with --send to deliver one test message")
        return 0

    alerter = TelegramAlerter()
    test = {
        "type": "system_error",
        "severity": "info",
        "chain_id": "",
        "token_address": "",
        "pair_address": "",
        "message": "DexScreener crawler Telegram demo — connectivity OK",
        "payload": {},
    }
    # Prefer admin/main
    ok = alerter.send_alert(test, cycle_id="demo-telegram")
    print(f"LIVE_SEND {'OK' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
