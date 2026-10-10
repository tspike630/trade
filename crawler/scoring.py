"""P2 token quality score 0–100 + tags."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from config import ALERT_MIN_SCORE
from logging_setup import get_logger

log = get_logger("scoring")


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _age_hours(pair_created_ms: Any, first_seen_iso: str | None) -> float | None:
    now = datetime.now(timezone.utc)
    if pair_created_ms is not None:
        try:
            ms = float(pair_created_ms)
            if ms > 1e12:
                created = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
            else:
                created = datetime.fromtimestamp(ms, tz=timezone.utc)
            return max(0.0, (now - created).total_seconds() / 3600.0)
        except (TypeError, ValueError, OSError):
            pass
    if first_seen_iso:
        try:
            ts = datetime.strptime(first_seen_iso, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            return max(0.0, (now - ts).total_seconds() / 3600.0)
        except ValueError:
            pass
    return None


def score_from_metrics(
    *,
    liquidity_usd: float | None = None,
    volume_h24: float | None = None,
    age_hours: float | None = None,
    txns_h24: int | None = None,
    social_mentions: int | None = None,
) -> dict[str, Any]:
    """
    Weighted 0–100 score.
    liquidity ~30, volume ~25, age ~15, txns ~15, social ~15.
    """
    components: dict[str, float] = {}

    # Liquidity: log10 scale, $1k→~10, $10k→~20, $100k→~30
    liq = liquidity_usd or 0.0
    if liq <= 0:
        components["liquidity"] = 0.0
    else:
        components["liquidity"] = _clamp(math.log10(liq + 1) / 5.0 * 30.0, 0, 30)

    vol = volume_h24 or 0.0
    if vol <= 0:
        components["volume"] = 0.0
    else:
        components["volume"] = _clamp(math.log10(vol + 1) / 5.0 * 25.0, 0, 25)

    # Age: sweet spot 6h–7d; brand-new slight discount for rug risk; very old mild
    if age_hours is None:
        components["age"] = 7.0
    elif age_hours < 1:
        components["age"] = 4.0
    elif age_hours < 6:
        components["age"] = 10.0
    elif age_hours < 24 * 7:
        components["age"] = 15.0
    elif age_hours < 24 * 30:
        components["age"] = 12.0
    else:
        components["age"] = 8.0

    tx = int(txns_h24 or 0)
    if tx <= 0:
        components["txns"] = 0.0
    else:
        components["txns"] = _clamp(math.log10(tx + 1) / 3.0 * 15.0, 0, 15)

    sm = int(social_mentions or 0)
    if sm <= 0:
        components["social"] = 0.0
    else:
        components["social"] = _clamp(math.log10(sm + 1) / 2.5 * 15.0, 0, 15)

    score = round(sum(components.values()), 1)
    score = _clamp(score, 0, 100)

    tags: list[str] = []
    if age_hours is not None and age_hours < 24:
        tags.append("new")
    if liq >= 50_000:
        tags.append("high_liq")
    if sm >= 10:
        tags.append("high_social")
    if score < ALERT_MIN_SCORE:
        tags.append("low_quality")

    return {
        "score": score,
        "tags": tags,
        "components": components,
        "inputs": {
            "liquidity_usd": liquidity_usd,
            "volume_h24": volume_h24,
            "age_hours": age_hours,
            "txns_h24": txns_h24,
            "social_mentions": social_mentions,
        },
    }


def score_token_from_store(store: Any, chain_id: str, token_address: str) -> dict[str, Any]:
    """Pull best pair + optional x_watch mentions → score → persist."""
    from store import norm_address, norm_chain

    import json

    chain_id = norm_chain(chain_id)
    token_address = norm_address(token_address)

    row = store._conn.execute(
        """
        SELECT raw_json, updated_at FROM pairs
        WHERE chain_id = ? AND token_address = ?
        ORDER BY updated_at DESC LIMIT 1
        """,
        (chain_id, token_address),
    ).fetchone()

    liquidity_usd = volume_h24 = None
    txns_h24 = None
    age_hours = None
    pair_created = None
    if row:
        import json

        try:
            raw = json.loads(row["raw_json"])
        except json.JSONDecodeError:
            raw = {}
        liq = raw.get("liquidity") if isinstance(raw.get("liquidity"), dict) else {}
        vol = raw.get("volume") if isinstance(raw.get("volume"), dict) else {}
        txns = raw.get("txns") if isinstance(raw.get("txns"), dict) else {}
        h24 = txns.get("h24") if isinstance(txns.get("h24"), dict) else {}
        liquidity_usd = _num(liq.get("usd"))
        volume_h24 = _num(vol.get("h24"))
        try:
            txns_h24 = int(h24.get("buys") or 0) + int(h24.get("sells") or 0)
        except (TypeError, ValueError):
            txns_h24 = None
        pair_created = raw.get("pairCreatedAt")
        tok = store._conn.execute(
            "SELECT last_seen_at, updated_at FROM tokens WHERE chain_id=? AND token_address=?",
            (chain_id, token_address),
        ).fetchone()
        first = (tok["last_seen_at"] if tok else None) or (
            tok["updated_at"] if tok else None
        )
        age_hours = _age_hours(pair_created, first)

    social = 0
    try:
        w = store.get_x_watch(chain_id, token_address)
        if w:
            social = int(w.get("mention_count_total") or 0)
    except Exception:  # noqa: BLE001
        social = 0

    result = score_from_metrics(
        liquidity_usd=liquidity_usd,
        volume_h24=volume_h24,
        age_hours=age_hours,
        txns_h24=txns_h24,
        social_mentions=social,
    )

    # P3: on-chain risk deduction + risk_* tags (non-invasive)
    try:
        from risk import apply_risk_to_score, assess_risk

        token_raw = None
        trow = store._conn.execute(
            "SELECT raw_json FROM tokens WHERE chain_id=? AND token_address=?",
            (chain_id, token_address),
        ).fetchone()
        if trow:
            try:
                token_raw = json.loads(trow["raw_json"])
            except (json.JSONDecodeError, TypeError):
                token_raw = None
        pair_raw = None
        if row:
            try:
                pair_raw = json.loads(row["raw_json"])
            except (json.JSONDecodeError, TypeError):
                pair_raw = None
        risk = assess_risk(
            chain_id, token_address, pair=pair_raw, token_raw=token_raw
        )
        new_score, new_tags = apply_risk_to_score(
            result["score"], result["tags"], risk
        )
        result["score_before_risk"] = result["score"]
        result["score"] = new_score
        result["tags"] = new_tags
        result["risk"] = {
            "deduction": risk.deduction,
            "tags": risk.tags,
            "flags": risk.flags,
            "source": risk.source,
            "notes": risk.notes,
        }
        if new_score < ALERT_MIN_SCORE and "low_quality" not in result["tags"]:
            result["tags"].append("low_quality")
    except Exception as exc:  # noqa: BLE001 — never break enrich
        log.warning("risk assess failed: %s", exc)

    store.set_token_quality(chain_id, token_address, result["score"], result["tags"])
    return result


def should_push_alert(store: Any, alert: dict[str, Any]) -> bool:
    """
    If token has quality_score below ALERT_MIN_SCORE, store-only (no Telegram/webhook).
    Missing score → allow push (don't block unknown).
    System/admin alerts always push.
    """
    atype = alert.get("type") or ""
    if atype in (
        "system_error",
        "pipeline_error",
        "heartbeat_fail",
        "proxy_pool_low",
    ):
        return True
    chain = alert.get("chain_id") or ""
    token = alert.get("token_address") or ""
    if not chain or not token:
        return True
    q = store.get_token_quality(chain, token)
    if not q or q.get("quality_score") is None:
        return True
    score = float(q["quality_score"])
    if score < ALERT_MIN_SCORE:
        log.debug(
            "alert push skipped low_score=%.1f min=%.1f type=%s token=%s",
            score,
            ALERT_MIN_SCORE,
            atype,
            token[:12],
        )
        return False
    return True
