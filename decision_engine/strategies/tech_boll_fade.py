"""Invert factor_tech_boll_and → race_tech_boll_fade.

True AND of inverted legs (禁 OR):
  tech_fade: change_5m <= -min_change_5m AND volume_strength >= min
  boll_fade: price < boll_lower (lower-band break; invert of upper breakout)
Missing any required field → skip.
"""

from __future__ import annotations

from typing import Any

from logging_setup import get_logger

from decision_engine.core.signal_bus import PURPOSE_LIVE_BNB_BOLL_FADE
from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import buy_size_fields
from decision_engine.strategies.live_bnb_entry_zone import (
    RULE_VERSION as LIVE_ENTRY_ZONE_VERSION,
    evaluate_entry_zone,
)

log = get_logger("decision_engine.tech_boll_fade")

RULE_VERSION = "tech_boll_fade_v1_2026-11-01"
STRATEGY_ID = "tech_boll_fade_v1"


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


class TechBollFadeStrategy(BaseStrategy):
    strategy_id = STRATEGY_ID

    def __init__(
        self,
        min_change_5m: float = 2.0,
        min_volume_strength: float = 0.5,
        position_pct: float = 0.05,
        **kwargs: Any,
    ):
        super().__init__(
            min_change_5m=min_change_5m,
            min_volume_strength=min_volume_strength,
            position_pct=position_pct,
            **kwargs,
        )
        self.min_change_5m = float(min_change_5m)
        self.min_volume_strength = float(min_volume_strength)
        self.position_pct = float(position_pct)
        self.rejected: dict[str, int] = {}

    def on_tick(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        purpose = str(data.get("purpose") or "")
        if purpose == PURPOSE_LIVE_BNB_BOLL_FADE:
            return self._on_tick_entry_zone(data)
        return self._on_tick_paper(data)

    def _on_tick_entry_zone(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Live arm only. Paper ``race_tech_boll_fade`` stays on ``_on_tick_paper``."""
        self._pending.clear()
        self.rejected = {}
        hot = data.get("hot_tokens") or []
        hits = 0
        for row in hot:
            if not isinstance(row, dict):
                self.rejected["row_not_dict"] = self.rejected.get("row_not_dict", 0) + 1
                continue
            decision = evaluate_entry_zone(
                row,
                min_change_5m=self.min_change_5m,
                min_volume_strength=self.min_volume_strength,
            )
            if not decision.emit:
                reason = decision.reject_reason or "entry_zone_rejected"
                self.rejected[reason] = self.rejected.get(reason, 0) + 1
                if reason == "retail_crowded_skip":
                    log.info(
                        "retail_crowded_skip symbol=%s trade_size_ratio=%s alpha=%s "
                        "deep_zone_price=%s note=avg_trade_notional_ratio",
                        row.get("binance_symbol"),
                        decision.trade_size_ratio,
                        decision.alpha,
                        decision.deep_zone_price,
                    )
                continue
            sized = buy_size_fields(row, floor_pct=self.position_pct)
            multiplier = float(decision.position_size_multiplier or 1.0)
            size_pct = float(sized["size_pct"]) * multiplier
            if not 0.0 < size_pct <= 1.0:
                self.rejected["size_pct_invalid"] = (
                    self.rejected.get("size_pct_invalid", 0) + 1
                )
                continue
            hits += 1
            order_type = decision.order_type or "MARKET"
            self.buy(
                chain_id=str(row.get("chain_id") or ""),
                token_address=str(row.get("token_address") or ""),
                pair_address=row.get("primary_pair_address"),
                size_pct=size_pct,
                size_tier=sized["size_tier"],
                entry_r3=sized["entry_r3"],
                size_blockers=sized["size_blockers"],
                planned_size_pct=float(sized["size_pct"]),
                reason=(
                    f"live_bnb_entry_zone {LIVE_ENTRY_ZONE_VERSION} "
                    f"{decision.entry_reason} alpha={decision.alpha} "
                    f"ratio={decision.trade_size_ratio} order={order_type}"
                ),
                rule_version=LIVE_ENTRY_ZONE_VERSION,
                binance_symbol=row.get("binance_symbol"),
                entry_zone_min=decision.entry_zone_min,
                entry_zone_max=decision.entry_zone_max,
                alpha=decision.alpha,
                trade_size_ratio=decision.trade_size_ratio,
                trade_size_ratio_source=decision.trade_size_ratio_source,
                trade_size_baseline_bars=decision.trade_size_baseline_bars,
                stop_price=decision.stop_price,
                take_profit_price=decision.take_profit_price,
                order_type=order_type,
                limit_price=decision.limit_price,
                position_size_multiplier=multiplier,
                position_size_note=decision.position_size_note,
                entry_reason=decision.entry_reason,
                crowding_tier=decision.tier,
                deep_zone_price=decision.deep_zone_price,
                deep_zone_order="not_sent",
                price_usd=decision.price,
                boll_mid=decision.mid,
                kline_low=decision.low,
                change_5m=row.get("change_5m"),
                volume_strength=row.get("volume_strength"),
                boll_lower=row.get("boll_lower", row.get("boll_low")),
                legacy_gate="diagnostic_only",
                legacy_change_5m_pass=decision.legacy_change_5m_pass,
                legacy_volume_strength_pass=decision.legacy_volume_strength_pass,
                legacy_boll_lower_break=decision.legacy_boll_lower_break,
                and_mode="entry_zone",
            )
        signals = self.drain()
        log.info(
            "%s entry_zone scanned=%s hits=%s signals=%s rejected=%s",
            self.strategy_id,
            len(hot),
            hits,
            len(signals),
            dict(sorted(self.rejected.items())),
        )
        return signals

    def _on_tick_paper(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        self._pending.clear()
        self.rejected = {}
        hot = data.get("hot_tokens") or []
        hits = 0
        thr = -self.min_change_5m
        for row in hot:
            c5 = _finite(row.get("change_5m"))
            vs = _finite(row.get("volume_strength"))
            if c5 is None:
                self.rejected["change_5m_missing"] = (
                    self.rejected.get("change_5m_missing", 0) + 1
                )
                continue
            if vs is None:
                self.rejected["volume_strength_missing"] = (
                    self.rejected.get("volume_strength_missing", 0) + 1
                )
                continue
            lo = row.get("boll_lower")
            if lo is None:
                lo = row.get("boll_low")
            px = row.get("price_usd")
            if px is None:
                px = row.get("price")
            if lo is None:
                self.rejected["boll_lower_missing"] = (
                    self.rejected.get("boll_lower_missing", 0) + 1
                )
                continue
            if px is None:
                self.rejected["price_missing"] = self.rejected.get("price_missing", 0) + 1
                continue
            try:
                lower = float(lo)
                price = float(px)
            except (TypeError, ValueError):
                self.rejected["boll_price_parse"] = (
                    self.rejected.get("boll_price_parse", 0) + 1
                )
                continue

            # AND both inverted legs
            if c5 > thr:
                self.rejected["tech_leg_fail"] = self.rejected.get("tech_leg_fail", 0) + 1
                continue
            if vs < self.min_volume_strength:
                self.rejected["tech_vs_fail"] = self.rejected.get("tech_vs_fail", 0) + 1
                continue
            if price >= lower:
                self.rejected["boll_leg_fail"] = self.rejected.get("boll_leg_fail", 0) + 1
                continue

            hits += 1
            sized = buy_size_fields(row, floor_pct=self.position_pct)
            self.buy(
                chain_id=str(row.get("chain_id") or ""),
                token_address=str(row.get("token_address") or ""),
                pair_address=row.get("primary_pair_address"),
                size_pct=float(sized["size_pct"]),
                size_tier=sized["size_tier"],
                entry_r3=sized["entry_r3"],
                size_blockers=sized["size_blockers"],
                reason=(
                    f"tech_boll_fade {RULE_VERSION} "
                    f"c5={c5:.3f}<={thr} vs={vs:.3f} px={price:.8g}<lo={lower:.8g}"
                ),
                rule_version=RULE_VERSION,
                change_5m=c5,
                volume_strength=vs,
                boll_lower=lower,
                price_usd=price,
                and_mode="tech_fade&boll_lower",
            )
        signals = self.drain()
        log.info(
            "%s scanned=%s hits=%s signals=%s rejected=%s",
            self.strategy_id, len(hot), hits, len(signals),
            dict(sorted(self.rejected.items())),
        )
        return signals
