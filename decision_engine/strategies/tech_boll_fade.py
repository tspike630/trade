"""Invert factor_tech_boll_and → race_tech_boll_fade.

True AND of inverted legs (禁 OR):
  tech_fade: change_5m <= -min_change_5m AND volume_strength >= min
  boll_fade: price < boll_lower (lower-band break; invert of upper breakout)
Missing any required field → skip.

``live_bnb_boll_fade`` replaces those legs with the entry zone.
``live_bnb_boll_classic`` keeps the legs. It writes the paper TP/SL/hold
(``execution.exits`` 10 / 5 / 60) as extras and as absolute prices so a later
native OCO can read them. This module does not place that order.
``race_tech_boll_fade`` stays on the paper emit.
"""

from __future__ import annotations

from typing import Any

from logging_setup import get_logger

from decision_engine.core.signal_bus import (
    PURPOSE_LIVE_BNB_BOLL_CLASSIC,
    PURPOSE_LIVE_BNB_BOLL_FADE,
)
from decision_engine.core.stop_gap import stop_distance_reason
from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import buy_size_fields
from decision_engine.strategies.live_bnb_entry_zone import (
    RULE_VERSION as LIVE_ENTRY_ZONE_VERSION,
    evaluate_entry_zone,
)
from decision_engine.strategies.live_bnb_notional import (
    REJECT_BELOW_MIN,
    live_buy_notional_skip,
)
from decision_engine.strategies.live_bnb_size_floor import (
    ENTRY_MIN_NOTIONAL_USDT,
    lift_size_pct_for_entry_min,
)

log = get_logger("decision_engine.tech_boll_fade")

RULE_VERSION = "tech_boll_fade_v1_2026-11-01"
STRATEGY_ID = "tech_boll_fade_v1"
# Twins of execution.exits.DEFAULT_TP_PCT / DEFAULT_SL_PCT / DEFAULT_MAX_HOLD_MIN.
# Paper applies these when a signal omits them. The classic CEX emit writes
# the same percents and the absolute prices they imply at the break price.
CLASSIC_TP_PCT = 10.0
CLASSIC_SL_PCT = 5.0
CLASSIC_MAX_HOLD_MIN = 60.0
CLASSIC_EXIT_SHELL = "classic_fixed_tp_sl"


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
        if purpose == PURPOSE_LIVE_BNB_BOLL_CLASSIC:
            return self._on_tick_classic_cex(data)
        return self._on_tick_paper(data)

    def _on_tick_entry_zone(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Live arm only. Paper ``race_tech_boll_fade`` stays on ``_on_tick_paper``.

        Neutral keeps multiplier 0.5. If that quote is under 20 USDT,
        ``size_pct`` is raised to the exchange floor. A quote that is still
        under 20 after that is not written. Bands are not retuned.
        """
        self._pending.clear()
        self.rejected = {}
        hot = data.get("hot_tokens") or []
        hits = 0
        book_warned = False
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
                elif reason == "orderflow_toxic_sell_dump":
                    log.info(
                        "orderflow_toxic_sell_dump symbol=%s taker_buy_ratio=%s "
                        "trade_size_ratio=%s alpha=%s",
                        row.get("binance_symbol"),
                        decision.taker_buy_ratio,
                        decision.trade_size_ratio,
                        decision.alpha,
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
            # 0.5 is the zone's risk half-size, not the exchange minimum.
            # Raise only the emitted fraction, and only when the book parses.
            size_before_floor = size_pct
            size_pct, lifted = lift_size_pct_for_entry_min(size_pct)
            if not 0.0 < size_pct <= 1.0:
                self.rejected["size_pct_invalid"] = (
                    self.rejected.get("size_pct_invalid", 0) + 1
                )
                continue
            # After the lift. A quote that still cannot clear 20 USDT is not
            # written. A missing book fails open. Paper never reaches here.
            skip, book_warn = live_buy_notional_skip(size_pct)
            if book_warn is not None and not book_warned:
                log.warning("%s", book_warn)
                book_warned = True
            if skip:
                self.rejected[REJECT_BELOW_MIN] = (
                    self.rejected.get(REJECT_BELOW_MIN, 0) + 1
                )
                continue
            lift_extra: dict[str, Any] = {}
            if lifted:
                lift_extra = {
                    "size_pct_before_min_notional": size_before_floor,
                    "min_notional_lift": True,
                    "min_notional_usdt": format(ENTRY_MIN_NOTIONAL_USDT, "f"),
                }
                log.info(
                    "entry_min_notional_lift symbol=%s size_pct=%s -> %s multiplier=%s",
                    row.get("binance_symbol"),
                    size_before_floor,
                    size_pct,
                    multiplier,
                )
            if decision.taker_buy_ratio is not None:
                lift_extra["taker_buy_ratio"] = decision.taker_buy_ratio
                lift_extra["taker_buy_ratio_source"] = decision.taker_buy_ratio_source
                lift_extra["orderflow_regime"] = decision.orderflow_regime
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
                **lift_extra,
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

    def _collect_classic_hits(self, hot: list[Any]) -> list[dict[str, Any]]:
        """Pre-entry-zone legs. Shared by paper and ``live_bnb_boll_classic``."""
        hits: list[dict[str, Any]] = []
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
            hits.append(
                {
                    "row": row,
                    "c5": c5,
                    "vs": vs,
                    "lower": lower,
                    "price": price,
                    "thr": thr,
                }
            )
        return hits

    def _classic_reason(self, hit: dict[str, Any]) -> str:
        return (
            f"tech_boll_fade {RULE_VERSION} "
            f"c5={hit['c5']:.3f}<={hit['thr']} vs={hit['vs']:.3f} "
            f"px={hit['price']:.8g}<lo={hit['lower']:.8g}"
        )

    def _emit_paper_buy(self, hit: dict[str, Any]) -> None:
        row = hit["row"]
        sized = buy_size_fields(row, floor_pct=self.position_pct)
        self.buy(
            chain_id=str(row.get("chain_id") or ""),
            token_address=str(row.get("token_address") or ""),
            pair_address=row.get("primary_pair_address"),
            size_pct=float(sized["size_pct"]),
            size_tier=sized["size_tier"],
            entry_r3=sized["entry_r3"],
            size_blockers=sized["size_blockers"],
            reason=self._classic_reason(hit),
            rule_version=RULE_VERSION,
            change_5m=hit["c5"],
            volume_strength=hit["vs"],
            boll_lower=hit["lower"],
            price_usd=hit["price"],
            and_mode="tech_fade&boll_lower",
        )

    def _emit_classic_cex_buy(self, hit: dict[str, Any]) -> None:
        """Same legs as paper, plus the fixed TP/SL shell as absolute prices.

        ``binance_symbol`` is required. A non-positive price cannot anchor a
        stop below the entry and a take-profit above it, so that row is not
        written. Sizing stays on ``buy_size_fields``; the entry-zone notional
        lift is not applied.
        """
        row = hit["row"]
        raw_symbol = row.get("binance_symbol")
        if not isinstance(raw_symbol, str) or not raw_symbol.strip():
            self.rejected["binance_symbol_missing"] = (
                self.rejected.get("binance_symbol_missing", 0) + 1
            )
            return
        price = float(hit["price"])
        if not price > 0.0:
            self.rejected["price_not_positive"] = (
                self.rejected.get("price_not_positive", 0) + 1
            )
            return
        stop_price = price * (1.0 - CLASSIC_SL_PCT / 100.0)
        take_profit_price = price * (1.0 + CLASSIC_TP_PCT / 100.0)
        if not stop_price < price < take_profit_price:
            self.rejected["classic_exit_invalid"] = (
                self.rejected.get("classic_exit_invalid", 0) + 1
            )
            return
        if stop_distance_reason(price, stop_price):
            self.rejected["stop_too_close_to_entry"] = (
                self.rejected.get("stop_too_close_to_entry", 0) + 1
            )
            return
        sized = buy_size_fields(row, floor_pct=self.position_pct)
        self.buy(
            chain_id=str(row.get("chain_id") or ""),
            token_address=str(row.get("token_address") or ""),
            pair_address=row.get("primary_pair_address"),
            size_pct=float(sized["size_pct"]),
            size_tier=sized["size_tier"],
            entry_r3=sized["entry_r3"],
            size_blockers=sized["size_blockers"],
            reason=self._classic_reason(hit),
            rule_version=RULE_VERSION,
            change_5m=hit["c5"],
            volume_strength=hit["vs"],
            boll_lower=hit["lower"],
            price_usd=price,
            and_mode="tech_fade&boll_lower",
            binance_symbol=raw_symbol.strip(),
            order_type="MARKET",
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            tp_pct=CLASSIC_TP_PCT,
            sl_pct=CLASSIC_SL_PCT,
            max_hold_min=CLASSIC_MAX_HOLD_MIN,
            exit_shell=CLASSIC_EXIT_SHELL,
        )

    def _on_tick_classic_cex(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Binance spot arm on the original legs. Not the entry zone."""
        self._pending.clear()
        self.rejected = {}
        hot = data.get("hot_tokens") or []
        hits = self._collect_classic_hits(hot)
        for hit in hits:
            self._emit_classic_cex_buy(hit)
        signals = self.drain()
        log.info(
            "%s classic_exit scanned=%s hits=%s signals=%s rejected=%s",
            self.strategy_id,
            len(hot),
            len(hits),
            len(signals),
            dict(sorted(self.rejected.items())),
        )
        return signals

    def _on_tick_paper(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        self._pending.clear()
        self.rejected = {}
        hot = data.get("hot_tokens") or []
        hits = self._collect_classic_hits(hot)
        for hit in hits:
            self._emit_paper_buy(hit)
        signals = self.drain()
        log.info(
            "%s scanned=%s hits=%s signals=%s rejected=%s",
            self.strategy_id, len(hot), len(hits), len(signals),
            dict(sorted(self.rejected.items())),
        )
        return signals
