"""H3 split obs filters → race_filt_obs_bs_ratio / race_filt_obs_buyers_n.

Both: selfbuilt first, then single-dimension obs (no OR merge).
  bs_ratio: txns_h1_buys / txns_h1_sells >= 2.0; missing buys/sells → skip
  buyers_n: unique buyer fingerprint not on hot path → obs_buyers_n_missing skip
            (do NOT fake unique addresses with buy-count)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import buy_size_fields
from decision_engine.strategies.selfbuilt_momentum_a import evaluate_candidate_a

log = get_logger("decision_engine.filt_obs")

RULE_VERSION_BS = "filt_obs_bs_ratio_h3_2026-10-30"
RULE_VERSION_BN = "filt_obs_buyers_n_h3_2026-10-30"
BS_MIN = 2.0
BUYERS_MIN = 5.0


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


def evaluate_obs_bs_ratio(
    row: dict[str, Any], *, now: datetime | None = None
) -> tuple[bool, str | None, dict[str, Any]]:
    ok, why, extras = evaluate_candidate_a(row, now=now)
    extras = dict(extras)
    extras["rule_version"] = RULE_VERSION_BS
    buys = _finite(row.get("txns_h1_buys"))
    sells = _finite(row.get("txns_h1_sells"))
    extras["txns_h1_buys"] = buys
    extras["txns_h1_sells"] = sells
    extras["obs_bs_ratio"] = None
    if not ok:
        return False, why, extras
    if buys is None or sells is None:
        return False, "obs_bs_missing", extras
    if sells <= 0:
        # no sells → ratio undefined / infinite; treat as missing-safe skip
        return False, "obs_bs_sells_non_positive", extras
    ratio = buys / sells
    extras["obs_bs_ratio"] = round(ratio, 4)
    if ratio < BS_MIN:
        return False, "obs_bs_below", extras
    return True, None, extras


def evaluate_obs_buyers_n(
    row: dict[str, Any], *, now: datetime | None = None
) -> tuple[bool, str | None, dict[str, Any]]:
    ok, why, extras = evaluate_candidate_a(row, now=now)
    extras = dict(extras)
    extras["rule_version"] = RULE_VERSION_BN
    # Prefer explicit unique-buyer field; never invent from txns buy count.
    n = _finite(row.get("obs_buyers_n"))
    if n is None:
        n = _finite(row.get("unique_buyers_n"))
    extras["obs_buyers_n"] = n
    if not ok:
        return False, why, extras
    if n is None:
        return False, "obs_buyers_n_missing", extras
    if n < BUYERS_MIN:
        return False, "obs_buyers_n_below", extras
    return True, None, extras


class _FiltObsBase(BaseStrategy):
    mode: Literal["bs_ratio", "buyers_n"] = "bs_ratio"
    strategy_id = "filt_obs_base"

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
            if self.mode == "bs_ratio":
                ok, why, extras = evaluate_obs_bs_ratio(row, now=now)
                rule = RULE_VERSION_BS
            else:
                ok, why, extras = evaluate_obs_buyers_n(row, now=now)
                rule = RULE_VERSION_BN
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
                reason=f"filt_obs_{self.mode} {rule} extras={ {k: extras.get(k) for k in ('obs_bs_ratio','obs_buyers_n')} }",
                rule_version=rule,
                obs_mode=self.mode,
            )
        signals = self.drain()
        log.info(
            "%s scanned=%s hits=%s signals=%s rejected=%s",
            self.strategy_id, len(hot), hits, len(signals),
            dict(sorted(self.rejected.items())),
        )
        return signals


class FiltObsBsRatioStrategy(_FiltObsBase):
    strategy_id = "filt_obs_bs_ratio_v1"
    mode = "bs_ratio"


class FiltObsBuyersNStrategy(_FiltObsBase):
    strategy_id = "filt_obs_buyers_n_v1"
    mode = "buyers_n"
