"""W2 vol climax × mcap band → race_vol_climax_mcap_band.

Independent entry (not selfbuilt / not bare vol_spike):
  vol climax: volume_1h >= 3 × (volume_h24/24)   # vol_accel proxy
  mcap ∈ [$2M, $100M]  (market_cap or mcap)
  buy_ratio = txns_h1_buys / (buys+sells) >= 0.58
Missing any required field → *_missing skip (never invent).
Buy-only; exits with execution peer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import tick_now, buy_size_fields

log = get_logger("decision_engine.vol_climax_mcap_band")

RULE_VERSION = "vol_climax_mcap_band_w2_2026-10-30"
STRATEGY_ID = "vol_climax_mcap_band_v1"

MCAP_LO = 2_000_000.0
MCAP_HI = 100_000_000.0
BUY_RATIO_MIN = 0.58
VOL_ACCEL_MIN = 3.0


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


def _mcap(row: dict[str, Any]) -> float | None:
    for key in ("market_cap", "mcap", "marketCap"):
        got = _finite(row.get(key))
        if got is not None:
            return got
    return None


def evaluate_vol_climax_mcap_band(
    row: dict[str, Any],
    *,
    now: datetime | None = None,
    mcap_lo: float = MCAP_LO,
    mcap_hi: float = MCAP_HI,
    buy_ratio_min: float = BUY_RATIO_MIN,
    vol_accel_min: float = VOL_ACCEL_MIN,
) -> tuple[bool, str | None, dict[str, Any]]:
    when = now or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    v1 = _finite(row.get("volume_1h"))
    v24 = _finite(row.get("volume_h24"))
    mcap = _mcap(row)
    buys = _finite(row.get("txns_h1_buys"))
    sells = _finite(row.get("txns_h1_sells"))
    baseline = None if v24 is None else (v24 / 24.0)
    vol_accel = None
    if v1 is not None and baseline is not None and baseline > 0:
        vol_accel = v1 / baseline
    buy_ratio = None
    if buys is not None and sells is not None:
        denom = buys + sells
        if denom > 0:
            buy_ratio = buys / denom

    extras: dict[str, Any] = {
        "rule_version": RULE_VERSION,
        "volume_1h": v1,
        "volume_h24": v24,
        "volume_1h_baseline": None if baseline is None else round(baseline, 6),
        "vol_accel": None if vol_accel is None else round(vol_accel, 4),
        "market_cap": mcap,
        "txns_h1_buys": buys,
        "txns_h1_sells": sells,
        "buy_ratio": None if buy_ratio is None else round(buy_ratio, 4),
        "vol_accel_proxy": "volume_1h/(volume_h24/24)",
    }

    if v1 is None:
        return False, "volume_1h_missing", extras
    if v24 is None or baseline is None:
        return False, "volume_h24_missing", extras
    if baseline <= 0:
        return False, "volume_baseline_non_positive", extras
    if vol_accel is None or vol_accel < vol_accel_min:
        return False, "vol_accel_below", extras
    if mcap is None:
        return False, "mcap_missing", extras
    if not (mcap_lo <= mcap <= mcap_hi):
        return False, "mcap_out_of_band", extras
    if buys is None or sells is None:
        return False, "buy_ratio_missing", extras
    if buy_ratio is None:
        return False, "buy_ratio_undefined", extras
    if buy_ratio < buy_ratio_min:
        return False, "buy_ratio_below", extras
    return True, None, extras


class VolClimaxMcapBandStrategy(BaseStrategy):
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
            ok, why, extras = evaluate_vol_climax_mcap_band(row, now=now)
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
                    f"vol_climax_mcap_band {RULE_VERSION} "
                    f"accel={extras['vol_accel']:.2f} mcap={extras['market_cap']:.0f} "
                    f"br={extras['buy_ratio']:.3f}"
                ),
                rule_version=RULE_VERSION,
                vol_accel=extras["vol_accel"],
                market_cap=extras["market_cap"],
                buy_ratio=extras["buy_ratio"],
                vol_climax_sidebook="w2",
            )
        signals = self.drain()
        log.info(
            "%s scanned=%s hits=%s signals=%s rejected=%s",
            self.strategy_id, len(hot), hits, len(signals),
            dict(sorted(self.rejected.items())),
        )
        return signals
