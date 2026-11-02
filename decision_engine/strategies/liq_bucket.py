"""R3 liquidity mutual-exclusion buckets → race_liq_bucket_high / mid.

Same selfbuilt momentum core, but only admit inside a liq band:
  high: liq >= 200k
  mid:  50k <= liq < 200k
Does NOT rewrite DECISION_MIN_LIQ_USD. Missing liq → skip.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import buy_size_fields
from decision_engine.strategies.selfbuilt_momentum_a import evaluate_candidate_a

log = get_logger("decision_engine.liq_bucket")

RULE_VERSION = "liq_bucket_r3_2026-10-30"
HIGH_LO = 200_000.0
MID_LO = 50_000.0
MID_HI = 200_000.0


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


def evaluate_liq_bucket(
    row: dict[str, Any],
    *,
    bucket: Literal["high", "mid"],
    now: datetime | None = None,
) -> tuple[bool, str | None, dict[str, Any]]:
    ok, why, extras = evaluate_candidate_a(row, now=now)
    extras = dict(extras)
    extras["rule_version"] = RULE_VERSION
    extras["liq_bucket"] = bucket
    if not ok:
        return False, why, extras
    liq = _finite(row.get("liquidity_usd"))
    extras["liquidity_usd"] = liq
    if liq is None:
        return False, "liq_missing", extras
    if bucket == "high":
        if liq < HIGH_LO:
            return False, "liq_bucket_not_high", extras
    else:
        if not (MID_LO <= liq < MID_HI):
            return False, "liq_bucket_not_mid", extras
    return True, None, extras


class _LiqBucketBase(BaseStrategy):
    bucket: Literal["high", "mid"] = "high"
    strategy_id = "liq_bucket_base"

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
            ok, why, extras = evaluate_liq_bucket(row, bucket=self.bucket, now=now)
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
                reason=(
                    f"liq_bucket_{self.bucket} {RULE_VERSION} "
                    f"liq={extras.get('liquidity_usd'):.0f} "
                    f"ch1h={extras.get('change_1h')} rsi={extras.get('rsi_14')}"
                ),
                rule_version=RULE_VERSION,
                liq_bucket=self.bucket,
                change_1h=extras.get("change_1h"),
                rsi_14=extras.get("rsi_14"),
            )
        signals = self.drain()
        log.info(
            "%s scanned=%s hits=%s signals=%s rejected=%s",
            self.strategy_id, len(hot), hits, len(signals),
            dict(sorted(self.rejected.items())),
        )
        return signals


class LiqBucketHighStrategy(_LiqBucketBase):
    strategy_id = "liq_bucket_high_v1"
    bucket = "high"


class LiqBucketMidStrategy(_LiqBucketBase):
    strategy_id = "liq_bucket_mid_v1"
    bucket = "mid"
