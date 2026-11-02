"""N5/P25 vol squeeze then breakout AND → race_and_vol_squeeze_break.

True AND (no OR):
  1) bandwidth compression proxy: (boll_upper-boll_lower)/boll_mid <= BW_MAX
     (history percentile unavailable → absolute paper threshold; missing boll → skip)
  2) close/price above boll_upper
  3) volume_strength >= 1.5 OR volume_1h >= 1.5 * (volume_h24/24) when available
  4) liq_usd >= 50k
Missing price_usd / boll / liq → skip.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import buy_size_fields

log = get_logger("decision_engine.and_vol_squeeze_break")

RULE_VERSION = "and_vol_squeeze_break_n5_2026-10-30"
STRATEGY_ID = "and_vol_squeeze_break_v1"

MIN_LIQ_USD = 50_000.0
BW_MAX = 0.08  # paper absolute compression proxy (no 20/100 hist percentile)
VOL_MULT = 1.5
MIN_VS = 1.5


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


def evaluate_vol_squeeze_break(
    row: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[bool, str | None, dict[str, Any]]:
    del now  # sizing uses now; admission is snapshot-only
    liq = _finite(row.get("liquidity_usd"))
    price = _finite(row.get("price_usd"))
    up = _finite(row.get("boll_upper"))
    mid = _finite(row.get("boll_mid"))
    low = _finite(row.get("boll_lower"))
    vs = _finite(row.get("volume_strength"))
    v1 = _finite(row.get("volume_1h"))
    v24 = _finite(row.get("volume_h24"))
    bw = None
    if up is not None and low is not None and mid is not None and mid != 0:
        bw = (up - low) / abs(mid)
    extras: dict[str, Any] = {
        "rule_version": RULE_VERSION,
        "liquidity_usd": liq,
        "price_usd": price,
        "boll_upper": up,
        "boll_mid": mid,
        "boll_lower": low,
        "bandwidth": None if bw is None else round(bw, 6),
        "volume_strength": vs,
        "volume_1h": v1,
        "volume_h24": v24,
        "bw_proxy": "absolute_bw_max",
    }
    if liq is None:
        return False, "liq_missing", extras
    if liq < MIN_LIQ_USD:
        return False, "liq_below", extras
    if up is None or mid is None or low is None:
        return False, "boll_missing", extras
    if bw is None:
        return False, "bandwidth_missing", extras
    if bw > BW_MAX:
        return False, "not_compressed", extras
    if price is None:
        return False, "price_usd_missing", extras
    if price <= up:
        return False, "not_above_upper", extras
    # volume confirmation: prefer volume_strength; else volume_1h vs h24/24
    vol_ok = False
    if vs is not None and vs >= MIN_VS:
        vol_ok = True
        extras["vol_confirm"] = "volume_strength"
    elif v1 is not None and v24 is not None and v24 > 0 and v1 >= VOL_MULT * (v24 / 24.0):
        vol_ok = True
        extras["vol_confirm"] = "volume_1h_vs_h24"
    elif vs is None and (v1 is None or v24 is None):
        return False, "volume_confirm_missing", extras
    if not vol_ok:
        return False, "volume_confirm_below", extras
    return True, None, extras


class AndVolSqueezeBreakStrategy(BaseStrategy):
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
            ok, why, extras = evaluate_vol_squeeze_break(row, now=now)
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
                    f"vol_squeeze_break {RULE_VERSION} "
                    f"bw={extras['bandwidth']} px={extras['price_usd']} "
                    f"up={extras['boll_upper']} vol={extras.get('vol_confirm')}"
                ),
                rule_version=RULE_VERSION,
                bandwidth=extras["bandwidth"],
                price_usd=extras["price_usd"],
                boll_upper=extras["boll_upper"],
                vol_squeeze_sidebook="n5",
            )
        signals = self.drain()
        log.info(
            "%s scanned=%s hits=%s signals=%s rejected=%s",
            self.strategy_id, len(hot), hits, len(signals),
            dict(sorted(self.rejected.items())),
        )
        return signals
