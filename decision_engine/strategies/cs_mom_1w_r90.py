"""Re-invert wiped race_cs_fade_1w → race_cs_mom_1w_r90 (Yang −90% 再反做 2026-11-03).

Same universe/liq/vs/return-src honesty as cs_mom_1w / cs_fade_1w.
Select TOP 20% by ret with ret > 0; vs>=median AND liq>=floor (禁 OR).
NEW strategy_id/purpose/module — do NOT reuse wiped cs_fade_1w / cs_mom_1w books.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import buy_size_fields

log = get_logger("decision_engine.cs_mom_1w_r90")

RULE_VERSION = "cs_mom_1w_r90_v1_2026-11-03"
STRATEGY_ID = "cs_mom_1w_r90_v1"

MIN_LIQ_USD = 50_000.0
MAX_EMIT_PER_TICK = 3
MIN_UNIVERSE = 10
TOP_FRAC = 0.20


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


class CsMom1wR90Strategy(BaseStrategy):
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
        now = datetime.now(timezone.utc)
        universe: list[dict[str, Any]] = []
        for row in hot:
            liq = _finite(row.get("liquidity_usd"))
            vs = _finite(row.get("volume_strength"))
            c7 = _finite(row.get("change_7d"))
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
            if c7 is not None:
                ret, ret_src = c7, "change_7d"
            elif c24 is not None:
                ret, ret_src = c24, "change_24h_1w_proxy"
            elif c1 is not None:
                ret, ret_src = c1, "change_1h_1w_proxy"
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

        rets = sorted((u["ret"] for u in universe), reverse=True)
        top_cut_idx = max(0, int(len(rets) * TOP_FRAC) - 1)
        top_cut = rets[top_cut_idx]

        scored: list[tuple[float, dict[str, Any]]] = []
        for u in universe:
            why = None
            if u["ret"] <= 0:
                why = "ret_non_positive"
            elif u["ret"] < top_cut:
                why = "ret_below_top20"
            elif u["vs"] < med_vs:
                why = "vs_below_median"
            elif u["liq"] < liq_floor:
                why = "liq_below_floor"
            if why:
                self.rejected[why] = self.rejected.get(why, 0) + 1
                continue
            ret_rel = u["ret"] - med_ret
            vs_rel = u["vs"] - med_vs
            score = ret_rel + 0.1 * vs_rel
            u.update(
                {
                    "ret_rel": ret_rel,
                    "vs_rel": vs_rel,
                    "med_ret": med_ret,
                    "med_vs": med_vs,
                    "liq_floor": liq_floor,
                    "top_cut": top_cut,
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
                    f"cs_mom_1w_r90 {RULE_VERSION} src={u['ret_src']} "
                    f"ret={u['ret']:.4f} top20={u['top_cut']:.4f} "
                    f"vs={u['vs']:.3f} liq={u['liq']:.0f}"
                ),
                rule_version=RULE_VERSION,
                ret=u["ret"],
                ret_src=u["ret_src"],
                ret_rel=u["ret_rel"],
                cs_mom_sidebook="cs_mom_1w_r90_v1",
                parent_wipe_purpose="race_cs_fade_1w",
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
