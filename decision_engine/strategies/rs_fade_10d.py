"""Invert rel_strength_10d → race_rs_fade_10d.

Same short-window machinery; select BOTTOM 20% ret with ret < 0.
Buy-only; missing return → skip.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import tick_now, buy_size_fields

log = get_logger("decision_engine.rs_fade_10d")

RULE_VERSION = "rs_fade_10d_v1_2026-11-01"
STRATEGY_ID = "rs_fade_10d_v1"

MIN_LIQ_USD = 50_000.0
MAX_EMIT_PER_TICK = 3
MIN_UNIVERSE = 10
BOTTOM_FRAC = 0.20


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


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return 0.5 * (s[mid - 1] + s[mid])


class RsFade10dStrategy(BaseStrategy):
    strategy_id = STRATEGY_ID

    def __init__(
        self,
        position_pct: float = 0.05,
        min_liq_usd: float = MIN_LIQ_USD,
        max_emit_per_tick: int = MAX_EMIT_PER_TICK,
        **kwargs: Any,
    ):
        super().__init__(position_pct=position_pct, **kwargs)
        self.position_pct = float(position_pct)
        self.min_liq_usd = float(min_liq_usd)
        self.max_emit_per_tick = max(1, int(max_emit_per_tick))
        self.rejected: dict[str, int] = {}

    def on_tick(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        self._pending.clear()
        self.rejected = {}
        hot = data.get("hot_tokens") or []
        now = tick_now(data)
        universe: list[dict[str, Any]] = []
        for row in hot:
            liq = _finite(row.get("liquidity_usd"))
            vs = _finite(row.get("volume_strength"))
            c1 = _finite(row.get("change_1h"))
            c24 = _finite(row.get("change_24h"))
            if liq is None:
                self.rejected["liq_missing"] = self.rejected.get("liq_missing", 0) + 1
                continue
            if vs is None:
                self.rejected["volume_strength_missing"] = (
                    self.rejected.get("volume_strength_missing", 0) + 1
                )
                continue
            if c1 is not None:
                ret, ret_src = c1, "change_1h"
            elif c24 is not None:
                ret, ret_src = c24, "change_24h_proxy"
            else:
                self.rejected["missing_return"] = self.rejected.get("missing_return", 0) + 1
                continue
            universe.append(
                {"row": row, "liq": liq, "vs": vs, "ret": ret, "ret_src": ret_src}
            )

        if len(universe) < MIN_UNIVERSE:
            self.rejected["universe_too_small"] = self.rejected.get("universe_too_small", 0) + 1
            signals = self.drain()
            log.info(
                "%s scanned=%s univ=%s hits=0 signals=%s rejected=%s",
                self.strategy_id, len(hot), len(universe), len(signals),
                dict(sorted(self.rejected.items())),
            )
            return signals

        med_ret = _median([u["ret"] for u in universe])
        med_vs = _median([u["vs"] for u in universe])
        med_liq = _median([u["liq"] for u in universe])
        assert med_ret is not None and med_vs is not None and med_liq is not None
        liq_floor = max(self.min_liq_usd, med_liq)

        rets_asc = sorted(u["ret"] for u in universe)
        bot_cut_idx = max(0, int(len(rets_asc) * BOTTOM_FRAC) - 1)
        bottom_cut = rets_asc[bot_cut_idx]

        scored: list[tuple[float, dict[str, Any]]] = []
        for u in universe:
            why = None
            if u["ret"] >= 0:
                why = "ret_non_negative"
            elif u["ret"] > bottom_cut:
                why = "ret_above_bottom20"
            elif u["vs"] < med_vs:
                why = "vs_below_median"
            elif u["liq"] < liq_floor:
                why = "liq_below_floor"
            if why:
                self.rejected[why] = self.rejected.get(why, 0) + 1
                continue
            weakness = -u["ret"]
            vs_rel = u["vs"] - med_vs
            score = weakness + 0.1 * vs_rel
            u.update(
                {
                    "ret_rel": u["ret"] - med_ret,
                    "vs_rel": vs_rel,
                    "med_ret": med_ret,
                    "med_vs": med_vs,
                    "liq_floor": liq_floor,
                    "bottom_cut": bottom_cut,
                }
            )
            scored.append((score, u))

        scored.sort(key=lambda t: t[0], reverse=True)
        hits = 0
        for score, u in scored[: self.max_emit_per_tick]:
            row = u["row"]
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
                    f"rs_fade_10d {RULE_VERSION} src={u['ret_src']} "
                    f"ret={u['ret']:.4f} bot20={u['bottom_cut']:.4f} "
                    f"vs={u['vs']:.3f} liq={u['liq']:.0f}"
                ),
                rule_version=RULE_VERSION,
                ret=u["ret"],
                ret_src=u["ret_src"],
                ret_rel=u["ret_rel"],
                rs_fade_sidebook="rs_fade_10d_v1",
            )
        if len(scored) > self.max_emit_per_tick:
            self.rejected["truncated_over_max_emit"] = len(scored) - self.max_emit_per_tick
        signals = self.drain()
        log.info(
            "%s scanned=%s univ=%s hits=%s signals=%s rejected=%s",
            self.strategy_id, len(hot), len(universe), hits, len(signals),
            dict(sorted(self.rejected.items())),
        )
        return signals
