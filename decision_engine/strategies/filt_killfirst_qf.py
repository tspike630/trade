"""C1/N1 kill-first quality filter → race_filt_killfirst_qf.

selfbuilt admission THEN require qf_* bits all true:
  qf_mint_ok, qf_lp_ok, qf_top10_ok, qf_late80_ok, qf_vol1h_ok
  qf_dev_ok optional: if present must be true; if absent that bit is missing→skip
Any bit false → kill skip; any required bit missing → skip (never fake-pass).
No GoPlus cache on hot path tonight → expect high missing rate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import tick_now, buy_size_fields
from decision_engine.strategies.selfbuilt_momentum_a import evaluate_candidate_a

log = get_logger("decision_engine.filt_killfirst_qf")

RULE_VERSION = "filt_killfirst_qf_c1_2026-10-30"
STRATEGY_ID = "filt_killfirst_qf_v1"

REQUIRED_QF = (
    "qf_mint_ok",
    "qf_lp_ok",
    "qf_top10_ok",
    "qf_late80_ok",
    "qf_vol1h_ok",
)
OPTIONAL_QF = ("qf_dev_ok",)


def _tri(value: Any) -> bool | None:
    """True/False/None(missing)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value == value:
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"1", "true", "yes", "ok"}:
            return True
        if s in {"0", "false", "no", "bad", "kill"}:
            return False
    return None


def evaluate_killfirst_qf(
    row: dict[str, Any], *, now: datetime | None = None
) -> tuple[bool, str | None, dict[str, Any]]:
    ok, why, extras = evaluate_candidate_a(row, now=now)
    extras = dict(extras)
    extras["rule_version"] = RULE_VERSION
    qf_state: dict[str, bool | None] = {}
    for key in REQUIRED_QF + OPTIONAL_QF:
        qf_state[key] = _tri(row.get(key))
    extras["qf"] = qf_state
    if not ok:
        return False, why, extras
    for key in REQUIRED_QF:
        bit = qf_state[key]
        if bit is None:
            return False, f"{key}_missing", extras
        if bit is False:
            return False, f"{key}_kill", extras
    for key in OPTIONAL_QF:
        bit = qf_state[key]
        # 有则 AND；无则只记账 missing，不假 pass、也不因缺位单独 skip
        if bit is False:
            return False, f"{key}_kill", extras
        if bit is None:
            extras.setdefault("qf_optional_missing", []).append(key)
    return True, None, extras


class FiltKillfirstQfStrategy(BaseStrategy):
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
        now = tick_now(data)
        for row in hot:
            ok, why, extras = evaluate_killfirst_qf(row, now=now)
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
                entry_r3=0,
                size_blockers=sized["size_blockers"],
                reason=f"filt_killfirst_qf {RULE_VERSION} qf_all_ok",
                rule_version=RULE_VERSION,
                killfirst_sidebook="c1",
            )
        signals = self.drain()
        log.info(
            "%s scanned=%s hits=%s signals=%s rejected=%s",
            self.strategy_id, len(hot), hits, len(signals),
            dict(sorted(self.rejected.items())),
        )
        return signals
