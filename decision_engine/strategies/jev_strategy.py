"""JEV strength threshold strategy (Step 1)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy

log = get_logger("decision_engine.jev_strategy")

_NOT_BULLISH_RE = re.compile(r"\bNOT\s+BULLISH\b", re.I)
_BULLISH_RE = re.compile(r"\b(BUY|BULLISH|LONG)\b", re.I)


def stance_is_bullish(stance: Any) -> bool:
    """True only for an explicit, non-negated bullish stance."""
    s = str(stance or "").strip()
    return bool(s) and bool(_BULLISH_RE.search(s)) and not _NOT_BULLISH_RE.search(s)


def jev_age_min(row: dict[str, Any], now: datetime | None = None) -> float | None:
    """Prefer indicator-provided jev_age_min; else derive from jev_updated_at."""
    v = row.get("jev_age_min")
    if v is not None:
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    ts = row.get("jev_updated_at")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - dt).total_seconds() / 60.0


class JevStrengthStrategy(BaseStrategy):
    strategy_id = "jev_strength_v1"

    def __init__(
        self,
        min_jev_strength: float = 0.3,
        position_pct: float = 0.05,
        max_age_min: float | None = None,
        require_bullish_stance: bool | None = None,
        **kwargs: Any,
    ):
        from decision_engine import config as de_config
        super().__init__(
            min_jev_strength=min_jev_strength,
            position_pct=position_pct,
            **kwargs,
        )
        self.min_jev_strength = float(min_jev_strength)
        self.position_pct = float(position_pct)
        self.max_age_min = float(de_config.JEV_MAX_AGE_MIN if max_age_min is None else max_age_min)
        self.require_bullish_stance = (
            de_config.JEV_REQUIRE_BULLISH_STANCE
            if require_bullish_stance is None else bool(require_bullish_stance))
        self.rejected = {"jev_stale": 0, "jev_stance_not_bullish": 0}

    def on_tick(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        self._pending.clear()
        hot = data.get("hot_tokens") or []
        above = 0
        for row in hot:
            strength = row.get("jev_strength")
            if strength is None:
                continue
            try:
                s = float(strength)
            except (TypeError, ValueError):
                continue
            if s >= self.min_jev_strength:
                above += 1
                age = jev_age_min(row)
                has_age_col = "jev_updated_at" in row or "jev_age_min" in row
                if self.max_age_min > 0 and has_age_col and (age is None or age > self.max_age_min):
                    self.rejected["jev_stale"] += 1
                    log.info("jev reject reason=jev_stale token=%s age_min=%s max=%s",
                             str(row.get("token_address") or "")[:16], age, self.max_age_min)
                    continue
                if self.require_bullish_stance and "jev_stance" in row and not stance_is_bullish(row.get("jev_stance")):
                    self.rejected["jev_stance_not_bullish"] += 1
                    log.info("jev reject reason=jev_stance_not_bullish token=%s stance=%r signal=%s",
                             str(row.get("token_address") or "")[:16], row.get("jev_stance"), row.get("jev_signal"))
                    continue
                self.buy(
                    chain_id=str(row.get("chain_id") or ""),
                    token_address=str(row.get("token_address") or ""),
                    pair_address=row.get("primary_pair_address"),
                    size_pct=self.position_pct,
                    reason=(
                        f"jev_strength={s:.4f}>={self.min_jev_strength}"
                        f" signal={row.get('jev_signal')} score={row.get('jev_score')}"
                    ),
                    jev_strength=s,
                    jev_signal=row.get("jev_signal"),
                    jev_score=row.get("jev_score"),
                    jev_age_min=None if age is None else round(age, 1),  # null if unknown; gateway may reject >1440
                    jev_stance_raw=row.get("jev_stance"),
                )
        signals = self.drain()
        log.info(
            "jev_strength_v1 scanned=%s above_threshold=%s signals=%s min=%.3f",
            len(hot),
            above,
            len(signals),
            self.min_jev_strength,
        )
        return signals
