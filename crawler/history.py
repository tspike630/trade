"""P2 pair_history snapshots — due by hot/warm/cold interval after pair upsert."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from config import (
    HISTORY_COLD_INTERVAL_SEC,
    HISTORY_HOT_INTERVAL_SEC,
    HISTORY_WARM_INTERVAL_SEC,
    HOT_SEEN_WINDOW_SEC,
)
from logging_setup import get_logger

log = get_logger("history")


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def history_tier_for_token(store: Any, chain_id: str, token_address: str | None) -> str:
    """Map token recency → hot|warm|cold for snapshot cadence."""
    if not token_address:
        return "warm"
    row = store._conn.execute(
        """
        SELECT last_seen_at, updated_at, has_pairs FROM tokens
        WHERE chain_id = ? AND token_address = ?
        """,
        (chain_id, token_address),
    ).fetchone()
    if not row:
        return "warm"
    seen = _parse_iso(row["last_seen_at"] or row["updated_at"])
    if seen is None:
        return "cold"
    age = (datetime.now(timezone.utc) - seen).total_seconds()
    if age <= HOT_SEEN_WINDOW_SEC and row["has_pairs"] == 1:
        return "hot"
    if age <= HOT_SEEN_WINDOW_SEC * 6:
        return "warm"
    return "cold"


def interval_for_tier(tier: str) -> int:
    if tier == "hot":
        return HISTORY_HOT_INTERVAL_SEC
    if tier == "cold":
        return HISTORY_COLD_INTERVAL_SEC
    return HISTORY_WARM_INTERVAL_SEC


def history_due(store: Any, chain_id: str, pair_address: str, tier: str) -> bool:
    last = store.get_pair_last_history_at(chain_id, pair_address)
    if not last:
        return True
    ts = _parse_iso(last)
    if ts is None:
        return True
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    return age >= interval_for_tier(tier)


def extract_metrics(pair: dict[str, Any]) -> dict[str, Any]:
    vol = pair.get("volume") if isinstance(pair.get("volume"), dict) else {}
    liq = pair.get("liquidity") if isinstance(pair.get("liquidity"), dict) else {}
    txns = pair.get("txns") if isinstance(pair.get("txns"), dict) else {}
    h24 = txns.get("h24") if isinstance(txns.get("h24"), dict) else {}
    buys = h24.get("buys")
    sells = h24.get("sells")
    txns_h24 = None
    try:
        if buys is not None or sells is not None:
            txns_h24 = int(buys or 0) + int(sells or 0)
    except (TypeError, ValueError):
        txns_h24 = None
    return {
        "price_usd": _num(pair.get("priceUsd")),
        "volume_h24": _num(vol.get("h24")),
        "volume_h1": _num(vol.get("h1")),
        "liquidity_usd": _num(liq.get("usd")),
        "txns_h24": txns_h24,
    }


def maybe_snapshot_pair(
    store: Any,
    chain_id: str,
    pair_address: str,
    pair: dict[str, Any],
    *,
    token_address: str | None = None,
) -> bool:
    """
    Insert a history row if the tier interval has elapsed.
    Returns True if a snapshot was written.
    """
    tok = token_address or (pair.get("baseToken") or {}).get("address")
    if tok:
        from store import norm_address

        tok = norm_address(tok)
    tier = history_tier_for_token(store, chain_id, tok)
    if not history_due(store, chain_id, pair_address, tier):
        return False
    m = extract_metrics(pair)
    store.insert_pair_history(
        chain_id,
        pair_address,
        token_address=tok,
        price_usd=m["price_usd"],
        volume_h24=m["volume_h24"],
        volume_h1=m["volume_h1"],
        liquidity_usd=m["liquidity_usd"],
        txns_h24=m["txns_h24"],
        tier=tier,
    )
    log.debug(
        "pair_history snap chain=%s pair=%s tier=%s price=%s",
        chain_id,
        pair_address[:12],
        tier,
        m["price_usd"],
    )
    return True
