"""H8 Patient Swing → race_swing_established write-green.

成名/结构确认入场（可复现、缺字段不瞎 emit）:

Required (all):
  - liquidity_usd >= MIN_LIQ (default 100k) — 「有地板」
  - pool_age_days >= MIN_AGE_DAYS (default 7) — 成名/非新池
  - rsi_14 in [RSI_LO, RSI_HI] — 回调区结构
  - change_24h < 0 — 日级回撤（缺则 change_24h_missing，不 emit）
  - bounce: change_5m > 0 OR change_1h > 0

Optional structure (bonus in reason only, never required):
  - boll_mid + price_usd → note if price <= boll_mid
  - jev_funding_bias present → note only

Buy-only; exits stay with execution peer. purpose 名不变：race_swing_established。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import tick_now, buy_size_fields, pool_age_days

log = get_logger("decision_engine.swing_established")

RULE_VERSION = "swing_established_h8_2026-10-30"
STRATEGY_ID = "swing_established_v1"

MIN_LIQ_USD = 100_000.0
MIN_POOL_AGE_DAYS = 7.0
RSI_LO = 35.0
RSI_HI = 55.0


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


def evaluate_swing_established(
    row: dict[str, Any],
    *,
    now: datetime | None = None,
    min_liq_usd: float = MIN_LIQ_USD,
    min_pool_age_days: float = MIN_POOL_AGE_DAYS,
    rsi_lo: float = RSI_LO,
    rsi_hi: float = RSI_HI,
) -> tuple[bool, str | None, dict[str, Any]]:
    """Return (admitted, reject_reason|None, extras). Missing → explicit reason, never emit."""
    when = now or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    liq = _finite(row.get("liquidity_usd"))
    age = pool_age_days(row, when)
    rsi = _finite(row.get("rsi_14"))
    c24 = _finite(row.get("change_24h"))
    c5 = _finite(row.get("change_5m"))
    c1 = _finite(row.get("change_1h"))
    boll_mid = _finite(row.get("boll_mid"))
    price = _finite(row.get("price_usd"))
    funding = _finite(row.get("jev_funding_bias"))

    extras: dict[str, Any] = {
        "rule_version": RULE_VERSION,
        "liquidity_usd": liq,
        "pool_age_days": None if age is None else round(age, 4),
        "rsi_14": rsi,
        "change_24h": c24,
        "change_5m": c5,
        "change_1h": c1,
        "boll_mid": boll_mid,
        "price_usd": price,
        "jev_funding_bias": funding,
        "near_boll_mid": None,
    }

    if liq is None:
        return False, "liq_missing", extras
    if liq < min_liq_usd:
        return False, "liq_below", extras
    if age is None:
        return False, "pool_age_missing", extras
    if age < min_pool_age_days:
        return False, "pool_age_below", extras
    if rsi is None:
        return False, "rsi_missing", extras
    if not (rsi_lo <= rsi <= rsi_hi):
        return False, "rsi_out_of_band", extras
    if c24 is None:
        # 日级回撤是成名回调的结构确认；缺字段 → 不瞎 emit
        return False, "change_24h_missing", extras
    if not (c24 < 0):
        return False, "no_pullback_24h", extras
    if c5 is None and c1 is None:
        return False, "bounce_change_missing", extras
    bounce = (c5 is not None and c5 > 0) or (c1 is not None and c1 > 0)
    if not bounce:
        return False, "no_bounce", extras

    if boll_mid is not None and price is not None:
        extras["near_boll_mid"] = bool(price <= boll_mid * 1.02)

    return True, None, extras


class SwingEstablishedStrategy(BaseStrategy):
    """H8 established-meme pullback bounce; attributable strategy_id for race_swing_established."""

    strategy_id = STRATEGY_ID

    def __init__(
        self,
        position_pct: float = 0.05,
        min_liq_usd: float = MIN_LIQ_USD,
        min_pool_age_days: float = MIN_POOL_AGE_DAYS,
        rsi_lo: float = RSI_LO,
        rsi_hi: float = RSI_HI,
        **kwargs: Any,
    ):
        super().__init__(position_pct=position_pct, **kwargs)
        self.position_pct = float(position_pct)
        self.min_liq_usd = float(min_liq_usd)
        self.min_pool_age_days = float(min_pool_age_days)
        self.rsi_lo = float(rsi_lo)
        self.rsi_hi = float(rsi_hi)
        self.rejected: dict[str, int] = {}

    def on_tick(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        self._pending.clear()
        self.rejected = {}
        hot = data.get("hot_tokens") or []
        hits = 0
        now = tick_now(data)
        for row in hot:
            ok, why, extras = evaluate_swing_established(
                row,
                now=now,
                min_liq_usd=self.min_liq_usd,
                min_pool_age_days=self.min_pool_age_days,
                rsi_lo=self.rsi_lo,
                rsi_hi=self.rsi_hi,
            )
            if not ok:
                self.rejected[why or "unknown"] = self.rejected.get(why or "unknown", 0) + 1
                continue
            hits += 1
            sized = buy_size_fields(row, floor_pct=self.position_pct, now=now)
            near = extras.get("near_boll_mid")
            near_s = "" if near is None else f" near_mid={int(near)}"
            self.buy(
                chain_id=str(row.get("chain_id") or ""),
                token_address=str(row.get("token_address") or ""),
                pair_address=row.get("primary_pair_address"),
                size_pct=float(sized["size_pct"]),
                size_tier=sized["size_tier"],
                entry_r3=sized["entry_r3"],
                size_blockers=sized["size_blockers"],
                reason=(
                    f"swing_established {RULE_VERSION} "
                    f"liq={extras['liquidity_usd']:.0f} age_d={extras['pool_age_days']} "
                    f"rsi={extras['rsi_14']:.2f} c24={extras['change_24h']:.4f} "
                    f"bounce_c5={extras['change_5m']} c1h={extras['change_1h']}{near_s}"
                ),
                rule_version=RULE_VERSION,
                change_24h=extras["change_24h"],
                change_1h=extras["change_1h"],
                change_5m=extras["change_5m"],
                rsi_14=extras["rsi_14"],
                pool_age_days=extras["pool_age_days"],
                swing_sidebook="established_h8",
            )
        signals = self.drain()
        log.info(
            "%s scanned=%s hits=%s signals=%s rejected=%s",
            self.strategy_id,
            len(hot),
            hits,
            len(signals),
            dict(sorted(self.rejected.items())),
        )
        return signals
