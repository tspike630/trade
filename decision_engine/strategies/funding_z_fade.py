"""W2 funding z-score fade → race_funding_z_fade.

成名币永续 funding z（≈60d）>|thr| 反向；可选 OIΔ 滤。
Hot-path DEX meme rows have no funding_rate / funding_z field →
every ticket funding_missing skip (honest bookkeeping only).
Do NOT misuse jev_funding_bias as funding z.
Buy-only (fade extreme positive funding → short not available on DEX buy-only;
  extreme negative funding → long fade/squeeze). Without funding → no emit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import buy_size_fields

log = get_logger("decision_engine.funding_z_fade")

RULE_VERSION = "funding_z_fade_w2_2026-10-30"
STRATEGY_ID = "funding_z_fade_v1"

FUNDING_Z_ABS_MIN = 2.0  # |z| threshold; only applied when funding_z present


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


def _funding_z(row: dict[str, Any]) -> float | None:
    """Read explicit funding z only. Never invent from jev_funding_bias."""
    for key in ("funding_z", "funding_z_60d", "perp_funding_z", "funding_rate_z"):
        got = _finite(row.get(key))
        if got is not None:
            return got
    # Raw funding rate alone is not a z-score → still missing for this rule.
    return None


def evaluate_funding_z_fade(
    row: dict[str, Any],
    *,
    now: datetime | None = None,
    z_abs_min: float = FUNDING_Z_ABS_MIN,
) -> tuple[bool, str | None, dict[str, Any]]:
    when = now or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    fz = _funding_z(row)
    extras: dict[str, Any] = {
        "rule_version": RULE_VERSION,
        "funding_z": fz,
        "funding_z_abs_min": z_abs_min,
        "jev_funding_bias_ignored": True,
    }

    if fz is None:
        return False, "funding_missing", extras
    # Long-only fade: extreme negative funding (crowded short) → buy.
    if fz > -z_abs_min:
        # positive extreme would be short-fade; buy-only book skips
        if fz >= z_abs_min:
            return False, "funding_z_positive_fade_needs_short", extras
        return False, "funding_z_abs_below", extras
    return True, None, extras


class FundingZFadeStrategy(BaseStrategy):
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
            ok, why, extras = evaluate_funding_z_fade(row, now=now)
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
                    f"funding_z_fade {RULE_VERSION} "
                    f"funding_z={extras['funding_z']:.3f}"
                ),
                rule_version=RULE_VERSION,
                funding_z=extras["funding_z"],
                funding_z_sidebook="w2",
            )
        signals = self.drain()
        log.info(
            "%s scanned=%s hits=%s signals=%s rejected=%s",
            self.strategy_id, len(hot), hits, len(signals),
            dict(sorted(self.rejected.items())),
        )
        return signals
