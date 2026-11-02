"""Single-capital risk sleeves → race_portfolio_risk (locked id portfolio_risk_v1).

One strategy_id / one purpose / one future paper book. Portfolio allocation
lives inside this module (not multi-purpose OR into one book).

Sleeves (cluster budgets; SIZE_MULT≈7 on execution → size_pct = weight/7):
  A vol_climax          weight 0.35 → size_pct 0.05
  B swing_established   weight 0.25 → size_pct ≈0.035714
  C selfbuilt satellite weight 0.10 → size_pct ≈0.014286
  Cash                  0.30 implicit (no emit)

Choice documented: B = swing only; C = selfbuilt satellite 10%.
(Alternative rejected: B = swing-OR-selfbuilt cluster would starve C.)

Rules:
  - Missing fields → skip (helpers already return *_missing); never invent.
  - Token mutex across sleeves: same token_address emits from at most one sleeve/tick.
  - Rank candidates within each sleeve; take top 1 per sleeve.
  - Max 3 emits/tick (one per sleeve).
  - Pass sleeve-scaled size_pct explicitly (never let thick_pool promote to 1.0).

Aliases (registry only — no extra peers): port_top3_equal_v1, sleeve_top_v1,
portfolio_risk_sleeves_v1 → portfolio_risk_v1. Old purpose race_portfolio_risk_sleeves
kept in PURPOSES for drain; tonight primary purpose is race_portfolio_risk.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from logging_setup import get_logger

from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.entry_sizing import buy_size_fields
from decision_engine.strategies.selfbuilt_momentum_a import evaluate_candidate_a
from decision_engine.strategies.swing_established import evaluate_swing_established
from decision_engine.strategies.vol_climax_mcap_band import evaluate_vol_climax_mcap_band

log = get_logger("decision_engine.portfolio_risk_sleeves")

RULE_VERSION = "portfolio_risk_v1_2026-11-01"
STRATEGY_ID = "portfolio_risk_v1"
LEGACY_STRATEGY_ID = "portfolio_risk_sleeves_v1"

# weight / SIZE_MULT(7) — equity frac = size_pct * SIZE_MULT
SIZE_MULT_ASSUMED = 7.0
SIZE_A = 0.35 / SIZE_MULT_ASSUMED  # 0.05
SIZE_B = 0.25 / SIZE_MULT_ASSUMED  # ≈0.0357142857
SIZE_C = 0.10 / SIZE_MULT_ASSUMED  # ≈0.0142857143
MAX_EMITS = 3

SLEEVE_WEIGHTS = {
    "A_vol_climax": 0.35,
    "B_swing": 0.25,
    "C_selfbuilt": 0.10,
}


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


def _token_key(row: dict[str, Any]) -> str:
    return str(row.get("token_address") or "").strip().lower()


def _rank_vol_climax(extras: dict[str, Any]) -> tuple:
    accel = _finite(extras.get("vol_accel")) or -1.0
    br = _finite(extras.get("buy_ratio")) or -1.0
    return (accel, br)


def _rank_swing(extras: dict[str, Any]) -> tuple:
    bounce = max(
        _finite(extras.get("change_5m")) or -1e9,
        _finite(extras.get("change_1h")) or -1e9,
    )
    liq = _finite(extras.get("liquidity_usd")) or -1.0
    return (bounce, liq)


def _rank_selfbuilt(extras: dict[str, Any]) -> tuple:
    ch1 = _finite(extras.get("change_1h")) or -1e9
    liq = _finite(extras.get("liquidity_usd")) or -1.0
    return (ch1, liq)


def _collect_hits(
    hot: list[dict[str, Any]],
    evaluate: Callable[..., tuple[bool, str | None, dict[str, Any]]],
    rank_fn: Callable[[dict[str, Any]], tuple],
    *,
    now: datetime,
    rejected: dict[str, int],
    claimed: set[str],
) -> list[tuple[tuple, dict[str, Any], dict[str, Any]]]:
    hits: list[tuple[tuple, dict[str, Any], dict[str, Any]]] = []
    for row in hot:
        tok = _token_key(row)
        if tok and tok in claimed:
            continue
        ok, why, extras = evaluate(row, now=now)
        if not ok:
            rejected[why or "unknown"] = rejected.get(why or "unknown", 0) + 1
            continue
        if not tok:
            rejected["token_address_missing"] = rejected.get("token_address_missing", 0) + 1
            continue
        hits.append((rank_fn(extras), row, extras))
    hits.sort(key=lambda t: t[0], reverse=True)
    return hits


class PortfolioRiskSleevesStrategy(BaseStrategy):
    """Single-book risk sleeves: climax 35% / swing 25% / selfbuilt 10% / cash 30%.

    Exit meta: omit tp/sl/hold so execution peer defaults apply (TP10/SL5/hold60).
    """

    strategy_id = STRATEGY_ID
    RULE_VERSION = RULE_VERSION

    def __init__(self, position_pct: float = 0.05, **kwargs: Any):
        super().__init__(position_pct=position_pct, **kwargs)
        # CLI position_pct ignored for sleeve budgets; kept for BaseStrategy kwargs parity.
        self.position_pct = float(position_pct)
        self.rejected: dict[str, int] = {}
        self.sleeve_hits: dict[str, int] = {}

    def on_tick(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        self._pending.clear()
        self.rejected = {}
        self.sleeve_hits = {"A_vol_climax": 0, "B_swing": 0, "C_selfbuilt": 0}
        hot = data.get("hot_tokens") or []
        now = datetime.now(timezone.utc)
        claimed: set[str] = set()
        emits = 0

        # Sleeve A — vol climax
        a_hits = _collect_hits(
            hot,
            evaluate_vol_climax_mcap_band,
            _rank_vol_climax,
            now=now,
            rejected=self.rejected,
            claimed=claimed,
        )
        self.sleeve_hits["A_vol_climax"] = len(a_hits)
        if a_hits and emits < MAX_EMITS:
            _rank, row, extras = a_hits[0]
            self._emit_sleeve(
                row, extras, sleeve="A_vol_climax", size_pct=SIZE_A, now=now
            )
            claimed.add(_token_key(row))
            emits += 1

        # Sleeve B — swing only (documented choice)
        b_hits = _collect_hits(
            hot,
            evaluate_swing_established,
            _rank_swing,
            now=now,
            rejected=self.rejected,
            claimed=claimed,
        )
        self.sleeve_hits["B_swing"] = len(b_hits)
        if b_hits and emits < MAX_EMITS:
            _rank, row, extras = b_hits[0]
            self._emit_sleeve(
                row, extras, sleeve="B_swing", size_pct=SIZE_B, now=now
            )
            claimed.add(_token_key(row))
            emits += 1

        # Sleeve C — selfbuilt satellite (only if not claimed by A/B)
        c_hits = _collect_hits(
            hot,
            evaluate_candidate_a,
            _rank_selfbuilt,
            now=now,
            rejected=self.rejected,
            claimed=claimed,
        )
        self.sleeve_hits["C_selfbuilt"] = len(c_hits)
        if c_hits and emits < MAX_EMITS:
            _rank, row, extras = c_hits[0]
            self._emit_sleeve(
                row, extras, sleeve="C_selfbuilt", size_pct=SIZE_C, now=now
            )
            claimed.add(_token_key(row))
            emits += 1

        signals = self.drain()
        log.info(
            "%s scanned=%s sleeve_hits=%s signals=%s rejected=%s "
            "choice=B_swing_only_C_selfbuilt_sat size_mult_assumed=%s",
            self.strategy_id,
            len(hot),
            dict(self.sleeve_hits),
            len(signals),
            dict(sorted(self.rejected.items())),
            SIZE_MULT_ASSUMED,
        )
        return signals

    def _exit_meta(self) -> dict[str, Any]:
        """Parent book: no override → peer defaults (TP10 / SL5 / hold60)."""
        return {}

    def _emit_sleeve(
        self,
        row: dict[str, Any],
        extras: dict[str, Any],
        *,
        sleeve: str,
        size_pct: float,
        now: datetime,
    ) -> None:
        # Metadata only — size_pct forced to sleeve budget (override thick_pool).
        sized = buy_size_fields(row, floor_pct=size_pct, now=now)
        exit_meta = self._exit_meta()
        self.buy(
            chain_id=str(row.get("chain_id") or ""),
            token_address=str(row.get("token_address") or ""),
            pair_address=row.get("primary_pair_address"),
            size_pct=float(size_pct),
            size_tier=f"sleeve_{sleeve}",
            entry_r3=int(sized.get("entry_r3") or 0),
            size_blockers=sized.get("size_blockers") or "",
            reason=(
                f"{self.strategy_id} {self.RULE_VERSION} sleeve={sleeve} "
                f"size_pct={size_pct} size_mult_assumed={SIZE_MULT_ASSUMED} "
                f"accel={extras.get('vol_accel')} rsi={extras.get('rsi_14')} "
                f"ch1h={extras.get('change_1h')} liq={extras.get('liquidity_usd')}"
                + (
                    f" sl={exit_meta.get('sl_pct')} trail={exit_meta.get('trail_pct')}"
                    if exit_meta
                    else ""
                )
            ),
            rule_version=self.RULE_VERSION,
            portfolio_sleeve=sleeve,
            sleeve_weight=SLEEVE_WEIGHTS.get(sleeve),
            size_mult_assumed=SIZE_MULT_ASSUMED,
            vol_accel=extras.get("vol_accel"),
            market_cap=extras.get("market_cap"),
            buy_ratio=extras.get("buy_ratio"),
            liquidity_usd=extras.get("liquidity_usd"),
            pool_age_days=extras.get("pool_age_days"),
            rsi_14=extras.get("rsi_14"),
            change_24h=extras.get("change_24h"),
            change_1h=extras.get("change_1h"),
            change_5m=extras.get("change_5m"),
            volatility_24h=extras.get("volatility_24h"),
            **exit_meta,
        )
