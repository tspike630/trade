"""Risk manager — entry liquidity floor plus optional buy cooldown.

Default DECISION_COOLDOWN_SEC=0 keeps dry-run observation unchanged.
Step B (paper) should set it ≈ DECISION_TICK_INTERVAL_SEC so the same
(strategy, chain, token) is not re-bought every tick while PM dedup is off.
Cooldown state is in-process (resets on restart). Sells are never cooled.

DECISION_MIN_LIQ_USD (default 50000) is the shared buy choke point for every
strategy. It matches backtest min_liq. Missing liquidity fails closed.
This does not change JEV strength thresholds.
"""

from __future__ import annotations

import math
import time
from typing import Any

from logging_setup import get_logger

from decision_engine import config as de_config

log = get_logger("decision_engine.risk")

_DEFAULT_MIN_LIQ_USD = 50_000.0


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _floor_usd(raw: Any) -> float:
    number = _safe_float(raw)
    if number is None:
        return _DEFAULT_MIN_LIQ_USD
    return number


class RiskManager:
    """Drop thin or unpriced-liquidity buys, then apply the optional cooldown."""

    def __init__(
        self,
        cooldown_sec: float | None = None,
        min_liq_usd: float | None = None,
    ):
        raw = de_config.DECISION_COOLDOWN_SEC if cooldown_sec is None else cooldown_sec
        try:
            sec = float(raw)
        except (TypeError, ValueError):
            sec = 0.0
        self.cooldown_sec = sec if sec > 0 else 0.0
        raw_floor = de_config.DECISION_MIN_LIQ_USD if min_liq_usd is None else min_liq_usd
        self.min_liq_usd = _floor_usd(raw_floor)
        self._last_buy: dict[tuple[str, str, str], float] = {}
        log.info(
            "risk cooldown_sec=%s (0 = off, dry-run observation unchanged; "
            "Step B should set DECISION_COOLDOWN_SEC ≈ tick interval)",
            self.cooldown_sec,
        )
        log.info(
            "risk min_liq_usd=%s (buys below this, or with no liquidity_usd, "
            "are dropped before emit; aligned with backtest min_liq; sells unaffected)",
            self.min_liq_usd,
        )

    def _liquidity_skip_reason(self, raw: Any) -> str | None:
        liquidity = _safe_float(raw)
        if liquidity is None:
            return "no_liquidity"
        if liquidity < self.min_liq_usd:
            return "liq_below_min"
        return None

    def check(self, signal: dict[str, Any]) -> dict[str, Any]:
        out = dict(signal)
        action = str(out.get("action") or "").lower()
        if action != "buy":
            out["risk_passed"] = True
            return out
        skip = self._liquidity_skip_reason(out.get("liquidity_usd"))
        if skip is not None:
            out["risk_passed"] = False
            out["skip_reason"] = skip
            token = str(out.get("token_address") or "")
            log.info(
                "liq skip reason=%s strategy=%s chain=%s token=%s "
                "liquidity_usd=%s min_liq_usd=%s",
                skip,
                out.get("strategy_id"),
                out.get("chain_id"),
                token[:16],
                out.get("liquidity_usd"),
                self.min_liq_usd,
            )
            return out
        if self.cooldown_sec <= 0:
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
