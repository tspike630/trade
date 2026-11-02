"""N8 swing pullback → race_swing_pullback.

成名币回踩（缺日线 MA20 时用 boll_mid 代理，并记账）:
  pool_age >= 7d
  liq >= 100k (成名地板旁证)
  rsi_14 in [35, 55)
  change_1h > 0
  price within ±3% of boll_mid (MA20 proxy) OR price <= boll_mid * 1.03
Missing MA20/day series: if boll_mid+price present → proxy; else structure_missing skip.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import buy_size_fields, pool_age_days

log = get_logger("decision_engine.swing_pullback")

RULE_VERSION = "swing_pullback_n8_2026-10-30"
STRATEGY_ID = "swing_pullback_v1"

MIN_LIQ_USD = 100_000.0
MIN_POOL_AGE_DAYS = 7.0
RSI_LO = 35.0
RSI_HI = 55.0
PULLBACK_BAND = 0.03


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def evaluate_swing_pullback(
    row: dict[str, Any], *, now: datetime | None = None
) -> tuple[bool, str | None, dict[str, Any]]:
    when = now or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    liq = _finite(row.get("liquidity_usd"))
    age = pool_age_days(row, when)
    rsi = _finite(row.get("rsi_14"))
    c1 = _finite(row.get("change_1h"))
    price = _finite(row.get("price_usd"))
    mid = _finite(row.get("boll_mid"))
    ma20 = _finite(row.get("ma20"))  # usually absent
    extras: dict[str, Any] = {
        "rule_version": RULE_VERSION,
        "liquidity_usd": liq,
        "pool_age_days": None if age is None else round(age, 4),
        "rsi_14": rsi,
        "change_1h": c1,
        "price_usd": price,
        "boll_mid": mid,
        "ma20": ma20,
        "structure_proxy": None,
    }
    if liq is None:
        return False, "liq_missing", extras
    if liq < MIN_LIQ_USD:
        return False, "liq_below", extras
    if age is None:
        return False, "pool_age_missing", extras
    if age < MIN_POOL_AGE_DAYS:
        return False, "pool_age_below", extras
    if rsi is None:
        return False, "rsi_missing", extras
    if not (RSI_LO <= rsi < RSI_HI):
        return False, "rsi_out_of_band", extras
    if c1 is None:
        return False, "change_1h_missing", extras
    if not (c1 > 0):
        return False, "change_1h_non_positive", extras

    anchor = ma20 if ma20 is not None else mid
    if anchor is None or price is None:
        return False, "structure_missing", extras
    extras["structure_proxy"] = "ma20" if ma20 is not None else "boll_mid"
    # within ±3% of anchor (pullback zone) and not extended far above
    lo = anchor * (1.0 - PULLBACK_BAND)
    hi = anchor * (1.0 + PULLBACK_BAND)
    if not (lo <= price <= hi):
        # also allow price slightly below mid (still recovering)
        if not (price < anchor and price >= lo):
            return False, "not_near_pullback_anchor", extras
    return True, None, extras


class SwingPullbackStrategy(BaseStrategy):
    strategy_id = STRATEGY_ID

    def __init__(self, position_pct: float = 0.05, **kwargs: Any):
        super().__init__(position_pct=position_pct, **kwargs)
        self.position_pct = float(position_pct)
        self.rejected: dict[str, int] = {}

    def on_tick(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        self._pending.clear()
        self.rejected = {}
        hot = data.get("hot_tokens") or []
        hits = 0
        now = datetime.now(timezone.utc)
        for row in hot:
            ok, why, extras = evaluate_swing_pullback(row, now=now)
            if not ok:
                self.rejected[why or "unknown"] = self.rejected.get(why or "unknown", 0) + 1
                continue
            hits += 1
            sized = buy_size_fields(row, floor_pct=self.position_pct, now=now)
            self.buy(
                chain_id=str(row.get("chain_id") or ""),
                token_address=str(row.get("token_address") or ""),
                pair_address=row.get("primary_pair_address"),
                size_pct=float(sized["size_pct"]),
                size_tier=sized["size_tier"],
                entry_r3=sized["entry_r3"],
                size_blockers=sized["size_blockers"],
                reason=(
                    f"swing_pullback {RULE_VERSION} proxy={extras['structure_proxy']} "
                    f"rsi={extras['rsi_14']:.2f} ch1h={extras['change_1h']:.4f} "
                    f"px={extras['price_usd']} mid={extras['boll_mid']}"
                ),
                rule_version=RULE_VERSION,
                structure_proxy=extras["structure_proxy"],
                rsi_14=extras["rsi_14"],
                change_1h=extras["change_1h"],
                swing_sidebook="pullback_n8",
            )
        signals = self.drain()
        log.info(
            "%s scanned=%s hits=%s signals=%s rejected=%s",
            self.strategy_id, len(hot), hits, len(signals),
            dict(sorted(self.rejected.items())),
        )
        return signals
