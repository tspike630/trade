"""Risk manager — passthrough plus optional buy cooldown.

Default DECISION_COOLDOWN_SEC=0 keeps dry-run observation unchanged.
Step B (paper) should set it ≈ DECISION_TICK_INTERVAL_SEC so the same
(strategy, chain, token) is not re-bought every tick while PM dedup is off.
Cooldown state is in-process (resets on restart). Sells are never cooled.
"""

from __future__ import annotations

import time
from typing import Any

from logging_setup import get_logger

from decision_engine import config as de_config

log = get_logger("decision_engine.risk")


class RiskManager:
    """Mark risk_passed and optionally drop buys inside the cooldown window."""

    def __init__(self, cooldown_sec: float | None = None):
        raw = de_config.DECISION_COOLDOWN_SEC if cooldown_sec is None else cooldown_sec
        try:
            sec = float(raw)
        except (TypeError, ValueError):
            sec = 0.0
        self.cooldown_sec = sec if sec > 0 else 0.0
        self._last_buy: dict[tuple[str, str, str], float] = {}
        log.info(
            "risk cooldown_sec=%s (0 = off, dry-run observation unchanged; "
            "Step B should set DECISION_COOLDOWN_SEC ≈ tick interval)",
            self.cooldown_sec,
        )

    def check(self, signal: dict[str, Any]) -> dict[str, Any]:
        out = dict(signal)
        action = str(out.get("action") or "").lower()
        if self.cooldown_sec <= 0 or action != "buy":
            out["risk_passed"] = True
            return out
        key = (
            str(out.get("strategy_id") or ""),
            str(out.get("chain_id") or ""),
            str(out.get("token_address") or ""),
        )
        now = time.monotonic()
        last = self._last_buy.get(key)
        if last is not None and (now - last) < self.cooldown_sec:
            out["risk_passed"] = False
            age = now - last
            log.info(
                "cooldown skip strategy=%s chain=%s token=%s age=%.1fs need=%.1fs",
                key[0],
                key[1],
                key[2][:16],
                age,
                self.cooldown_sec,
            )
            return out
        self._last_buy[key] = now
        out["risk_passed"] = True
        return out

    def filter(self, signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept: list[dict[str, Any]] = []
        for sig in signals:
            checked = self.check(sig)
            if checked.get("risk_passed"):
                kept.append(checked)
        return kept
