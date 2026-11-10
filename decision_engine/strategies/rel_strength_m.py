"""H11 relative strength → race_rel_strength_m write-green.

截面相对强度 / 动量排名（只用现网可算字段）:

Universe (per tick, hot_tokens):
  - require liquidity_usd, volume_strength, and a return leg
  - return leg preference: change_24h if present else change_1h
    (缺两者 → 不进宇宙，记 missing_return)

Score:
  - ret_rel = ret - median(ret over universe)
  - vs_rel  = volume_strength - median(volume_strength)
  - liq_rel = liquidity_usd - median(liquidity_usd)

Admit when ALL:
  - ret > 0
  - ret >= median(ret)          # 相对热门池收益中位
  - volume_strength >= median(vs)
  - liquidity_usd >= max(MIN_LIQ, median(liq))
  - optional cap: top MAX_EMIT by ret_rel (default 3) so one tick does not spam

缺字段 → 明确 skip，不瞎 emit。Buy-only；purpose 名不变：race_rel_strength_m。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import tick_now, buy_size_fields

log = get_logger("decision_engine.rel_strength_m")

RULE_VERSION = "rel_strength_m_h11_2026-10-30"
STRATEGY_ID = "rel_strength_m_v1"

MIN_LIQ_USD = 50_000.0
MAX_EMIT_PER_TICK = 3


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


class RelStrengthMStrategy(BaseStrategy):
    """Cross-sectional relative strength vs hot-pool medians (H11-inspired, DEX-native fields)."""

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

        # Build universe with computable features
        universe: list[dict[str, Any]] = []
        for row in hot:
            liq = _finite(row.get("liquidity_usd"))
            vs = _finite(row.get("volume_strength"))
            c24 = _finite(row.get("change_24h"))
            c1 = _finite(row.get("change_1h"))
            if liq is None:
                self.rejected["liq_missing"] = self.rejected.get("liq_missing", 0) + 1
                continue
            if vs is None:
                self.rejected["volume_strength_missing"] = (
                    self.rejected.get("volume_strength_missing", 0) + 1
                )
                continue
            if c24 is not None:
                ret, ret_src = c24, "change_24h"
            elif c1 is not None:
                ret, ret_src = c1, "change_1h"
            else:
                self.rejected["missing_return"] = self.rejected.get("missing_return", 0) + 1
                continue
            universe.append(
                {
                    "row": row,
                    "liq": liq,
                    "vs": vs,
                    "ret": ret,
                    "ret_src": ret_src,
                    "c24": c24,
                    "c1": c1,
                }
            )

        if len(universe) < 3:
            # 宇宙太小无法算稳健中位 → 不 emit
            self.rejected["universe_too_small"] = self.rejected.get("universe_too_small", 0) + 1
            signals = self.drain()
            log.info(
                "%s scanned=%s univ=%s hits=0 signals=%s rejected=%s",
                self.strategy_id,
                len(hot),
                len(universe),
                len(signals),
                dict(sorted(self.rejected.items())),
            )
            return signals

        med_ret = _median([u["ret"] for u in universe])
        med_vs = _median([u["vs"] for u in universe])
        med_liq = _median([u["liq"] for u in universe])
        assert med_ret is not None and med_vs is not None and med_liq is not None
        liq_floor = max(self.min_liq_usd, med_liq)

        scored: list[tuple[float, dict[str, Any]]] = []
        for u in universe:
            ret_rel = u["ret"] - med_ret
            vs_rel = u["vs"] - med_vs
            liq_rel = u["liq"] - med_liq
            why = None
            if u["ret"] <= 0:
                why = "ret_non_positive"
            elif u["ret"] < med_ret:
                why = "ret_below_median"
            elif u["vs"] < med_vs:
                why = "vs_below_median"
            elif u["liq"] < liq_floor:
                why = "liq_below_floor"
            if why:
                self.rejected[why] = self.rejected.get(why, 0) + 1
                continue
            # composite rank key: ret_rel primary, vs_rel secondary
            score = ret_rel + 0.1 * vs_rel + 1e-9 * liq_rel
            u["ret_rel"] = ret_rel
            u["vs_rel"] = vs_rel
            u["liq_rel"] = liq_rel
            u["med_ret"] = med_ret
            u["med_vs"] = med_vs
            u["med_liq"] = med_liq
            u["liq_floor"] = liq_floor
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
                    f"rel_strength_m {RULE_VERSION} src={u['ret_src']} "
                    f"ret={u['ret']:.4f} med={u['med_ret']:.4f} rel={u['ret_rel']:.4f} "
                    f"vs={u['vs']:.3f} med_vs={u['med_vs']:.3f} "
                    f"liq={u['liq']:.0f} floor={u['liq_floor']:.0f}"
                ),
                rule_version=RULE_VERSION,
                ret=u["ret"],
                ret_src=u["ret_src"],
                ret_rel=u["ret_rel"],
                volume_strength=u["vs"],
                vs_rel=u["vs_rel"],
                liquidity_usd=u["liq"],
                rs_sidebook="rel_strength_h11",
            )

        # count truncated survivors
        if len(scored) > self.max_emit_per_tick:
            self.rejected["truncated_over_max_emit"] = len(scored) - self.max_emit_per_tick

        signals = self.drain()
        log.info(
            "%s scanned=%s univ=%s hits=%s signals=%s med_ret=%s med_vs=%s med_liq=%s rejected=%s",
            self.strategy_id,
            len(hot),
            len(universe),
            hits,
            len(signals),
            round(med_ret, 6),
            round(med_vs, 6),
            round(med_liq, 2),
            dict(sorted(self.rejected.items())),
        )
        return signals
