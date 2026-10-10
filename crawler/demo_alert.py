"""Trigger a price_change alert by rewriting one pair snapshot then re-comparing."""

from __future__ import annotations

import json

from alerts import detect_pair_anomalies, emit_alert
from logging_setup import setup_logging, get_logger
from store import Store


def main() -> None:
    setup_logging()
    log = get_logger("demo_alert")
    with Store("dexscreener.db") as store:
        row = store._conn.execute(
            "SELECT chain_id, pair_address, token_address, raw_json FROM pairs LIMIT 1"
        ).fetchone()
        if row is None:
            raise SystemExit("no pairs in DB — run a crawl first")
        curr = json.loads(row["raw_json"])
        prev = json.loads(row["raw_json"])
        old_price = float(prev.get("priceUsd") or 1.0)
        # Force a 50% drop vs stored snapshot comparison path
        prev["priceUsd"] = old_price
        curr["priceUsd"] = old_price * 1.5  # +50%
        # also bump volume for a second alert type
        vol = dict(curr.get("volume") or {})
        old_v = float(vol.get("h24") or 1000.0)
        prev["volume"] = {**vol, "h24": old_v}
        curr["volume"] = {**vol, "h24": old_v * 2.5}

        found = detect_pair_anomalies(prev, curr)
        log.info("detected %s anomaly alert(s)", len(found))
        emitted = 0
        for a in found:
            if emit_alert(store, a, cycle_id="demo-alert"):
                emitted += 1
        print(f"ALERT_DEMO_OK detected={len(found)} emitted={emitted} total_alerts={store.count_alerts()}")


if __name__ == "__main__":
    main()
