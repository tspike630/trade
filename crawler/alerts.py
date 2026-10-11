"""Anomaly detection, dedupe, log + webhook + Telegram delivery."""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

from alerts_telegram import get_telegram_alerter, telegram_enabled
from config import (
    ALERT_COOLDOWN_SEC,
    ALERT_LIQUIDITY_DROP_PCT,
    ALERT_MIN_SCORE,
    ALERT_PRICE_CHANGE_PCT,
    ALERT_VOLUME_H24_CHANGE_PCT,
    ALERT_WEBHOOK_TIMEOUT_SEC,
    ALERT_WEBHOOK_URL,
    TELEGRAM_PUSH_COOLDOWN_SEC,
    TELEGRAM_PUSH_EXCLUDE_TYPES,
    TELEGRAM_PUSH_MIN_SCORE,
    TELEGRAM_PUSH_MIN_SEVERITY,
)
from scoring import should_push_alert
from logging_setup import get_logger

log = get_logger("alerts")

# Severity order for TELEGRAM_PUSH_MIN_SEVERITY (delivery-only gate)
_SEVERITY_RANK = {
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}
# Always deliver admin/system to Telegram (bypass type/severity/score push filters)
_TG_ALWAYS_PUSH_TYPES = frozenset({
    "system_error",
    "pipeline_error",
    "heartbeat_fail",
    "proxy_pool_low",
})

# In-memory last Telegram push times — does NOT touch store cooldown keys
_tg_push_last: dict[tuple[str, str, str, str], float] = {}
_tg_push_lock = threading.Lock()


def _tg_push_key(alert: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(alert.get("type") or ""),
        str(alert.get("chain_id") or ""),
        str(alert.get("token_address") or ""),
        str(alert.get("pair_address") or ""),
    )


def telegram_push_allowed(store: Any, alert: dict[str, Any]) -> tuple[bool, str]:
    """
    Telegram-only delivery gate (after store + should_push_alert).
    Returns (allowed, skip_reason). Detect/store unchanged.
    """
    atype = str(alert.get("type") or "")
    if atype in _TG_ALWAYS_PUSH_TYPES:
        return True, ""

    if atype.lower() in TELEGRAM_PUSH_EXCLUDE_TYPES:
        return False, f"exclude_type={atype}"

    sev = str(alert.get("severity") or "info").lower()
    min_sev = TELEGRAM_PUSH_MIN_SEVERITY
    if _SEVERITY_RANK.get(sev, 0) < _SEVERITY_RANK.get(min_sev, 3):
        return False, f"severity={sev}<min={min_sev}"

    # Push score: stricter of ALERT_MIN_SCORE and TELEGRAM_PUSH_MIN_SCORE
    eff_min = max(float(ALERT_MIN_SCORE), float(TELEGRAM_PUSH_MIN_SCORE))
    chain = str(alert.get("chain_id") or "")
    token = str(alert.get("token_address") or "")
    if chain and token:
        q = store.get_token_quality(chain, token)
        if q and q.get("quality_score") is not None:
            score = float(q["quality_score"])
            if score < eff_min:
                return False, f"score={score:.1f}<min={eff_min:.1f}"

    if TELEGRAM_PUSH_COOLDOWN_SEC > 0:
        key = _tg_push_key(alert)
        now = time.time()
        with _tg_push_lock:
            last = _tg_push_last.get(key, 0.0)
            if last > 0 and (now - last) < TELEGRAM_PUSH_COOLDOWN_SEC:
                return False, (
                    f"push_cooldown remaining={TELEGRAM_PUSH_COOLDOWN_SEC - (now - last):.0f}s"
                )
    return True, ""


def _mark_telegram_push(alert: dict[str, Any]) -> None:
    if TELEGRAM_PUSH_COOLDOWN_SEC <= 0:
        return
    key = _tg_push_key(alert)
    with _tg_push_lock:
        _tg_push_last[key] = time.time()


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct_change(old: float | None, new: float | None) -> float | None:
    if old is None or new is None:
        return None
    if old == 0:
        return None
    return (new - old) / abs(old) * 100.0


def _liquidity_usd(pair: dict[str, Any] | None) -> float | None:
    if not pair:
        return None
    liq = pair.get("liquidity")
    if isinstance(liq, dict):
        return _num(liq.get("usd"))
    return _num(liq)


def detect_pair_anomalies(
    prev: dict[str, Any] | None,
    curr: dict[str, Any],
) -> list[dict[str, Any]]:
    """Compare previous and current pair snapshots; return alert dicts (not yet persisted)."""
    if prev is None:
        return []

    alerts: list[dict[str, Any]] = []
    chain_id = str(curr.get("chainId") or "")
    pair_address = str(curr.get("pairAddress") or "")
    token_address = str((curr.get("baseToken") or {}).get("address") or "")

    old_price = _num(prev.get("priceUsd"))
    new_price = _num(curr.get("priceUsd"))
    price_pct = _pct_change(old_price, new_price)
    if price_pct is not None and abs(price_pct) >= ALERT_PRICE_CHANGE_PCT:
        alerts.append(
            {
                "type": "price_change",
                "severity": "high" if abs(price_pct) >= ALERT_PRICE_CHANGE_PCT * 2 else "medium",
                "chain_id": chain_id,
                "token_address": token_address,
                "pair_address": pair_address,
                "message": (
                    f"priceUsd changed {price_pct:+.1f}% "
                    f"({old_price} → {new_price}) threshold={ALERT_PRICE_CHANGE_PCT}%"
                ),
                "payload": {
                    "old_priceUsd": old_price,
                    "new_priceUsd": new_price,
                    "change_pct": round(price_pct, 4),
                    "threshold_pct": ALERT_PRICE_CHANGE_PCT,
                },
            }
        )

    old_vol = (
        _num((prev.get("volume") or {}).get("h24"))
        if isinstance(prev.get("volume"), dict)
        else None
    )
    new_vol = (
        _num((curr.get("volume") or {}).get("h24"))
        if isinstance(curr.get("volume"), dict)
        else None
    )
    vol_pct = _pct_change(old_vol, new_vol)
    if vol_pct is not None and abs(vol_pct) >= ALERT_VOLUME_H24_CHANGE_PCT:
        alerts.append(
            {
                "type": "volume_h24_jump",
                "severity": "medium",
                "chain_id": chain_id,
                "token_address": token_address,
                "pair_address": pair_address,
                "message": (
                    f"volume.h24 changed {vol_pct:+.1f}% "
                    f"({old_vol} → {new_vol}) threshold={ALERT_VOLUME_H24_CHANGE_PCT}%"
                ),
                "payload": {
                    "old_volume_h24": old_vol,
                    "new_volume_h24": new_vol,
                    "change_pct": round(vol_pct, 4),
                    "threshold_pct": ALERT_VOLUME_H24_CHANGE_PCT,
                },
            }
        )

    old_liq = _liquidity_usd(prev)
    new_liq = _liquidity_usd(curr)
    if old_liq is not None and new_liq is not None and old_liq > 0:
        drop_pct = (old_liq - new_liq) / old_liq * 100.0
        if drop_pct >= ALERT_LIQUIDITY_DROP_PCT:
            alerts.append(
                {
                    "type": "liquidity_drop",
                    "severity": "high",
                    "chain_id": chain_id,
                    "token_address": token_address,
                    "pair_address": pair_address,
                    "message": (
                        f"liquidity.usd dropped {drop_pct:.1f}% "
                        f"({old_liq} → {new_liq}) threshold={ALERT_LIQUIDITY_DROP_PCT}%"
                    ),
                    "payload": {
                        "old_liquidity_usd": old_liq,
                        "new_liquidity_usd": new_liq,
                        "drop_pct": round(drop_pct, 4),
                        "threshold_pct": ALERT_LIQUIDITY_DROP_PCT,
                    },
                }
            )

    return alerts


def emit_alert(store: Any, alert: dict[str, Any], *, cycle_id: str) -> bool:
    """
    Persist + log + optional webhook + optional Telegram if not in cooldown.
    Returns True if a new alert was emitted (inserted), regardless of delivery.
    delivered flag set when at least one delivery channel succeeds (or log-only).
    """
    alert_type = alert["type"]
    chain_id = alert.get("chain_id") or ""
    token_address = alert.get("token_address") or ""
    pair_address = alert.get("pair_address") or ""

    if store.has_recent_alert(
        alert_type,
        chain_id=chain_id,
        token_address=token_address,
        pair_address=pair_address,
        within_sec=ALERT_COOLDOWN_SEC,
    ):
        log.info(
            "alert suppressed (cooldown) cycle_id=%s type=%s chain=%s pair=%s token=%s",
            cycle_id,
            alert_type,
            chain_id or "-",
            pair_address or "-",
            token_address or "-",
        )
        return False

    alert_id = store.insert_alert(
        alert_type=alert_type,
        severity=alert.get("severity") or "info",
        message=alert.get("message") or alert_type,
        chain_id=chain_id or None,
        token_address=token_address or None,
        pair_address=pair_address or None,
        payload=alert.get("payload") or {},
        delivered=False,
    )

    log.warning(
        "ALERT cycle_id=%s id=%s type=%s severity=%s chain=%s token=%s pair=%s msg=%s",
        cycle_id,
        alert_id,
        alert_type,
        alert.get("severity"),
        chain_id or "-",
        token_address or "-",
        pair_address or "-",
        alert.get("message"),
    )

    delivered = False
    channels = 0

    push = should_push_alert(store, alert)
    if not push:
        log.info(
            "alert stored without push (score < ALERT_MIN_SCORE=%.1f) id=%s type=%s",
            ALERT_MIN_SCORE,
            alert_id,
            alert_type,
        )
        # Persist only; count as emitted for cooldown but not delivered push
        return True

    # Webhook (optional, still works alongside Telegram)
    if ALERT_WEBHOOK_URL:
        channels += 1
        body = {
            "id": alert_id,
            "cycle_id": cycle_id,
            "type": alert_type,
            "severity": alert.get("severity"),
            "message": alert.get("message"),
            "chain_id": chain_id,
            "token_address": token_address,
            "pair_address": pair_address,
            "payload": alert.get("payload") or {},
        }
        try:
            resp = httpx.post(
                ALERT_WEBHOOK_URL,
                json=body,
                timeout=ALERT_WEBHOOK_TIMEOUT_SEC,
            )
            if resp.status_code < 300:
                delivered = True
            else:
                log.error(
                    "webhook failed cycle_id=%s alert_id=%s status=%s",
                    cycle_id,
                    alert_id,
                    resp.status_code,
                )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "webhook error cycle_id=%s alert_id=%s err=%s",
                cycle_id,
                alert_id,
                exc,
            )

    # Telegram (optional) — push throttle first; 429/rate inside alerter
    if telegram_enabled():
        channels += 1
        tg_ok, tg_skip = telegram_push_allowed(store, alert)
        if not tg_ok:
            log.info(
                "telegram push skip (%s) id=%s type=%s severity=%s",
                tg_skip,
                alert_id,
                alert_type,
                alert.get("severity"),
            )
        else:
            try:
                tg = get_telegram_alerter()
                _mark_telegram_push(alert)
                tg_result = tg.send_alert(alert, cycle_id=cycle_id)
                if tg_result is True:
                    delivered = True
                elif tg_result is None:
                    # Expected skip (long ban / policy drop) — alerter already throttled WARN
                    pass
                else:
                    # Unexpected failure; alerter escalates repeated same-key fails to ERROR
                    log.warning(
                        "telegram delivery failed cycle_id=%s alert_id=%s type=%s",
                        cycle_id,
                        alert_id,
                        alert_type,
                    )
            except Exception as exc:  # noqa: BLE001 — never crash cycle
                log.error(
                    "telegram delivery exception cycle_id=%s alert_id=%s err=%s",
                    cycle_id,
                    alert_id,
                    exc,
                )

    if channels == 0:
        # log-only mode
        delivered = True

    if delivered:
        store.mark_alert_delivered(alert_id)

    store.bump_counter("alerts_emitted")
    return True
