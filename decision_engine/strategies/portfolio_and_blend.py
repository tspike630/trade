"""Equal-risk true-AND blend → race_portfolio_and_blend.

One strategy_id / one purpose / one future paper book. Three already-written
true AND admissions share equal risk; parent vol_climax alone is NOT stacked.

Sleeves (SIZE_MULT=20 → size_pct = weight/20):
  and_vol_climax_swing     weight 0.20 → size_pct 0.01
  and_vol_climax_selfbuilt weight 0.20 → size_pct 0.01
  and_swing_selfbuilt      weight 0.20 → size_pct 0.01
  Cash                     0.40 implicit (no emit)

Rules:
  - Each sleeve still true AND (import evaluate_and_*); no OR-fake.
  - Missing fields → skip; never invent.
  - Token mutex across sleeves; max 3 emits/tick; top-1 per sleeve by rank.
  - Pass sleeve-scaled size_pct explicitly (never thick_pool → 1.0).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from logging_setup import get_logger

from decision_engine.strategies.and_swing_selfbuilt import evaluate_and_swing_selfbuilt
from decision_engine.strategies.and_vol_climax_selfbuilt import (
    evaluate_and_vol_climax_selfbuilt,
)
from decision_engine.strategies.and_vol_climax_swing import evaluate_and_vol_climax_swing
from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import buy_size_fields

log = get_logger("decision_engine.portfolio_and_blend")

RULE_VERSION = "portfolio_and_blend_2026-11-01"
STRATEGY_ID = "portfolio_and_blend_v1"

SIZE_SLEEVE = 0.01  # 0.20 / 20
MAX_EMITS = 3

# Priority order for mutex claim: climax×swing → climax×selfbuilt → swing×selfbuilt
SLEEVES: tuple[tuple[str, Callable[..., tuple[bool, str | None, dict[str, Any]]]], ...] = (
    ("and_vol_climax_swing", evaluate_and_vol_climax_swing),
    ("and_vol_climax_selfbuilt", evaluate_and_vol_climax_selfbuilt),
    ("and_swing_selfbuilt", evaluate_and_swing_selfbuilt),
)


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


def _token_key(row: dict[str, Any]) -> str:
    return str(row.get("token_address") or "").strip().lower()


def _rank_and(extras: dict[str, Any]) -> tuple:
    """Prefer stronger climax accel / bounce / 1h change when present."""
    accel = _finite(extras.get("vol_accel"))
    ch1 = _finite(extras.get("change_1h"))
    bounce = max(
        _finite(extras.get("change_5m")) or -1e9,
        ch1 if ch1 is not None else -1e9,
    )
    liq = _finite(extras.get("liquidity_usd")) or -1.0
    return (
        accel if accel is not None else -1.0,
        bounce,
        liq,
    )


class PortfolioAndBlendStrategy(BaseStrategy):
    """Equal-risk blend of three true AND admissions; no parent climax alone."""

    strategy_id = STRATEGY_ID

    def __init__(self, position_pct: float = 0.05, **kwargs: Any):
        super().__init__(position_pct=position_pct, **kwargs)
        self.position_pct = float(position_pct)
        self.rejected: dict[str, int] = {}
        self.sleeve_hits: dict[str, int] = {}

    def on_tick(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        self._pending.clear()
        self.rejected = {}
        self.sleeve_hits = {name: 0 for name, _ in SLEEVES}
        hot = data.get("hot_tokens") or []
        now = datetime.now(timezone.utc)
        claimed: set[str] = set()
        emits = 0

        for sleeve_name, evaluate in SLEEVES:
            if emits >= MAX_EMITS:
                break
            hits: list[tuple[tuple, dict[str, Any], dict[str, Any]]] = []
            for row in hot:
                tok = _token_key(row)
                if tok and tok in claimed:
                    continue
                ok, why, extras = evaluate(row, now=now)
                if not ok:
                    key = f"{sleeve_name}:{why or 'unknown'}"
                    self.rejected[key] = self.rejected.get(key, 0) + 1
                    continue
                if not tok:
                    key = f"{sleeve_name}:token_address_missing"
                    self.rejected[key] = self.rejected.get(key, 0) + 1
                    continue
                hits.append((_rank_and(extras), row, extras))
            hits.sort(key=lambda t: t[0], reverse=True)
            self.sleeve_hits[sleeve_name] = len(hits)
            if not hits:
                continue
            _rank, row, extras = hits[0]
            sized = buy_size_fields(row, floor_pct=SIZE_SLEEVE, now=now)
            self.buy(
                chain_id=str(row.get("chain_id") or ""),
                token_address=str(row.get("token_address") or ""),
                pair_address=row.get("primary_pair_address"),
                size_pct=float(SIZE_SLEEVE),
                size_tier=f"sleeve_{sleeve_name}",
                entry_r3=int(sized.get("entry_r3") or 0),
                size_blockers=sized.get("size_blockers") or "",
                reason=(
                    f"portfolio_and_blend {RULE_VERSION} sleeve={sleeve_name} "
                    f"size_pct={SIZE_SLEEVE} "
                    f"accel={extras.get('vol_accel')} rsi={extras.get('rsi_14')} "
                    f"ch1h={extras.get('change_1h')}"
                ),
                rule_version=RULE_VERSION,
                portfolio_sleeve=sleeve_name,
                sleeve_weight=0.20,
                and_sidebook=sleeve_name,
                vol_accel=extras.get("vol_accel"),
                market_cap=extras.get("market_cap"),
                buy_ratio=extras.get("buy_ratio"),
                liquidity_usd=extras.get("liquidity_usd"),
                pool_age_days=extras.get("pool_age_days"),
                rsi_14=extras.get("rsi_14"),
                change_24h=extras.get("change_24h"),
                change_1h=extras.get("change_1h"),
                change_5m=extras.get("change_5m"),
                volatility_24h=extras.get("volatility_24h"),
                leg_vol_climax=extras.get("leg_vol_climax"),
                leg_swing=extras.get("leg_swing"),
                leg_selfbuilt_a=extras.get("leg_selfbuilt_a"),
            )
            claimed.add(_token_key(row))
            emits += 1

        signals = self.drain()
        log.info(
            "%s scanned=%s sleeve_hits=%s signals=%s rejected_top=%s",
            self.strategy_id,
            len(hot),
            dict(self.sleeve_hits),
            len(signals),
            dict(sorted(self.rejected.items(), key=lambda kv: -kv[1])[:12]),
        )
        return signals
