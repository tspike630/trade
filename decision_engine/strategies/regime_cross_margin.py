"""Approved cross-margin regime strategies.

Each class scans the same hot window and only emits when
``evaluate_approved_regime_intent`` admits that exact strategy id.
This module does not invent thresholds; it reuses the execution strategy gate.
"""

from __future__ import annotations

import os
from typing import Any

from decision_engine.core.cross_margin_price_contract import order_for_strategy
from decision_engine.core.regime_v2_policy import merge_cross_margin_clocks
from execution.margin_cross.strategy_gate import (
    REGIME_REQUIRED_INPUTS_REASON,
    evaluate_approved_regime_intent,
)
from decision_engine.strategies.base_strategy import BaseStrategy
from logging_setup import get_logger

log = get_logger("decision_engine.regime_cross_margin")

# Official approved set for purpose live_bnb_cross_margin. Do not pass --all.
CROSS_MARGIN_APPROVED_STRATEGIES: tuple[str, ...] = (
    "regime1_positive_gamma_fade",
    "regime2_short_squeeze_breakout",
    "regime3_xs_leader_pullback",
    "regime4_crowded_long_carry_short",
    "regime4_defensive_deep_fade",
    "regime5_crisis_contagion_short",
    "regime5_unlock_cliff_short",
    "tech_boll_fade_v1",
)

_LONG_STRATEGIES = frozenset(
    {
        "regime1_positive_gamma_fade",
        "regime2_short_squeeze_breakout",
        "regime3_xs_leader_pullback",
        "regime4_defensive_deep_fade",
    }
)
_SHORT_STRATEGIES = frozenset(
    {
        "regime4_crowded_long_carry_short",
        "regime5_crisis_contagion_short",
        "regime5_unlock_cliff_short",
    }
)

# Official base_position_pct is 1.00. Gate multipliers stay in extras so
# size_pct never carries an unruled Kelly fraction or a value > 1.
_BASE_SIZE_PCT = 1.0


def _symbol(row: dict[str, Any]) -> str:
    return str(row.get("binance_symbol") or row.get("symbol") or "").strip().upper()


def _assets(symbol: str) -> tuple[str, str]:
    if symbol.endswith("USDT") and len(symbol) > 4:
        return symbol[:-4], "USDT"
    return symbol, "USDT"


def _gate_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out.pop("size_pct", None)
    return out


def drop_opposite_side_conflicts(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop every signal for a symbol that got both a long and a short in one tick."""
    sides: dict[str, set[str]] = {}
    for sig in signals:
        if not isinstance(sig, dict):
            continue
        sym = str(sig.get("binance_symbol") or sig.get("token_address") or "").strip().upper()
        if not sym:
            continue
        req = str(sig.get("requested_action") or "")
        action = str(sig.get("action") or "").lower()
        if req == "OPEN_SHORT" or action == "sell":
            sides.setdefault(sym, set()).add("short")
        elif req == "OPEN_LONG" or action == "buy":
            sides.setdefault(sym, set()).add("long")
    conflicted = {sym for sym, got in sides.items() if "long" in got and "short" in got}
    if not conflicted:
        return signals
    log.info("same_symbol_long_short_conflict symbols=%s dropped", sorted(conflicted))
    kept: list[dict[str, Any]] = []
    for sig in signals:
        sym = str(sig.get("binance_symbol") or sig.get("token_address") or "").strip().upper()
        if sym in conflicted:
            continue
        kept.append(sig)
    return kept



def stamp_cross_margin_classifier_inputs(
    rows: list[dict[str, Any]],
    *,
    now_ms: int,
    snap: Any | None = None,
) -> list[dict[str, Any]]:
    """Stamp session clock and broadcast market-wide NVDA/SMH returns.

    Does not invent returns. A missing snapshot leaves returns unset so the
    required-input gate stays closed while the US cash session is open.
    ``lseg_market_open`` already present on a row is left unchanged.
    """
    from decision_engine.core.cityu_institutional_alpha import (
        load_cityu_institutional_snapshot,
        lseg_us_cash_market_open,
    )

    open_now = lseg_us_cash_market_open(now_ms)
    if snap is None:
        snap = load_cityu_institutional_snapshot(now_ms=now_ms)
    nvda = getattr(snap, "nvda_1h_ret_pct", None) if snap is not None else None
    smh = getattr(snap, "smh_1h_ret_pct", None) if snap is not None else None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if nvda is None:
            nvda = row.get("nvda_1h_ret_pct")
        if smh is None:
            smh = row.get("smh_1h_ret_pct")
    stamped: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        if open_now is not None:
            item.setdefault("lseg_market_open", open_now)
        if item.get("nvda_1h_ret_pct") is None and nvda is not None:
            item["nvda_1h_ret_pct"] = nvda
        if item.get("smh_1h_ret_pct") is None and smh is not None:
            item["smh_1h_ret_pct"] = smh
        stamped.append(item)
    return stamped


class _RegimeIntentStrategy(BaseStrategy):
    strategy_id = "regime_intent_base"
    requested_action = "OPEN_LONG"

    def __init__(self, **params: Any):
        super().__init__(**params)
        self.rejected: dict[str, int] = {}

    def on_tick(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        self._pending.clear()
        self.rejected = {}
        self.missing_inputs = {}
        hot = data.get("hot_tokens") or []
        macro_halt = data.get("macro_halt")
        if macro_halt is not None:
            macro_halt = str(macro_halt)
        environ = data.get("environ")
        if not isinstance(environ, dict):
            environ = os.environ
        signals: list[dict[str, Any]] = []
        for row in hot:
            if not isinstance(row, dict):
                self.rejected["row_not_dict"] = self.rejected.get("row_not_dict", 0) + 1
                continue
            symbol = _symbol(row)
            if not symbol:
                self.rejected["symbol_missing"] = self.rejected.get("symbol_missing", 0) + 1
                continue
            try:
                decision = evaluate_approved_regime_intent(
                    _gate_row(row),
                    symbol=symbol,
                    requested_action=self.requested_action,
                    macro_halt=macro_halt,
                    environ=environ,
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("%s gate failed symbol=%s: %s", self.strategy_id, symbol, exc)
                self.rejected["gate_error"] = self.rejected.get("gate_error", 0) + 1
                continue
            if not decision.allowed or decision.strategy_id != self.strategy_id:
                reason = "strategy_unmatched"
                if decision.reasons:
                    reason = str(decision.reasons[0])
                elif not decision.allowed:
                    reason = "not_allowed"
                self.rejected[reason] = self.rejected.get(reason, 0) + 1
                if reason == REGIME_REQUIRED_INPUTS_REASON:
                    for key in decision.missing_inputs:
                        self.missing_inputs[key] = self.missing_inputs.get(key, 0) + 1
                continue
            priced, price_reason = order_for_strategy(self.strategy_id, row)
            if priced is None:
                self.rejected[price_reason or "price_unruled"] = (
                    self.rejected.get(price_reason or "price_unruled", 0) + 1
                )
                continue
            chain_id = str(row.get("chain_id") or "binance")
            token_address = str(row.get("token_address") or symbol)
            pair_address = row.get("pair_address") or symbol
            base_asset, quote_asset = _assets(symbol)
            extra = {
                "binance_symbol": symbol,
                "symbol": symbol,
                "base_asset": base_asset,
                "quote_asset": quote_asset,
                "requested_action": decision.action,
                "size_multiplier": str(decision.size_multiplier),
                "tp_multiplier": str(decision.tp_multiplier),
                "market_regime": decision.regime,
                "market_row": dict(row),
                **priced,
                "clocks": merge_cross_margin_clocks(
                    row, row.get("clocks") if isinstance(row.get("clocks"), dict) else None
                ),
            }
            if self.requested_action == "OPEN_SHORT":
                sig = self.sell(
                    chain_id=chain_id,
                    token_address=token_address,
                    pair_address=str(pair_address),
                    size_pct=_BASE_SIZE_PCT,
                    reason=self.strategy_id,
                    **extra,
                )
            else:
                sig = self.buy(
                    chain_id=chain_id,
                    token_address=token_address,
                    pair_address=str(pair_address),
                    size_pct=_BASE_SIZE_PCT,
                    reason=self.strategy_id,
                    **extra,
                )
            signals.append(sig)
        log.info(
            "%s scanned=%s signals=%s rejected=%s missing_inputs=%s",
            self.strategy_id,
            len(hot) if isinstance(hot, list) else 0,
            len(signals),
            dict(sorted(self.rejected.items())),
            dict(sorted(self.missing_inputs.items())),
        )
        return signals


class Regime1PositiveGammaFadeStrategy(_RegimeIntentStrategy):
    strategy_id = "regime1_positive_gamma_fade"
    requested_action = "OPEN_LONG"


class Regime2ShortSqueezeBreakoutStrategy(_RegimeIntentStrategy):
    strategy_id = "regime2_short_squeeze_breakout"
    requested_action = "OPEN_LONG"


class Regime3XsLeaderPullbackStrategy(_RegimeIntentStrategy):
    strategy_id = "regime3_xs_leader_pullback"
    requested_action = "OPEN_LONG"


class Regime4CrowdedLongCarryShortStrategy(_RegimeIntentStrategy):
    strategy_id = "regime4_crowded_long_carry_short"
    requested_action = "OPEN_SHORT"


class Regime4DefensiveDeepFadeStrategy(_RegimeIntentStrategy):
    strategy_id = "regime4_defensive_deep_fade"
    requested_action = "OPEN_LONG"


class Regime5CrisisContagionShortStrategy(_RegimeIntentStrategy):
    strategy_id = "regime5_crisis_contagion_short"
    requested_action = "OPEN_SHORT"


class Regime5UnlockCliffShortStrategy(_RegimeIntentStrategy):
    strategy_id = "regime5_unlock_cliff_short"
    requested_action = "OPEN_SHORT"


REGIME_CROSS_MARGIN_STRATEGY_CLASSES: tuple[type[_RegimeIntentStrategy], ...] = (
    Regime1PositiveGammaFadeStrategy,
    Regime2ShortSqueezeBreakoutStrategy,
    Regime3XsLeaderPullbackStrategy,
    Regime4CrowdedLongCarryShortStrategy,
    Regime4DefensiveDeepFadeStrategy,
    Regime5CrisisContagionShortStrategy,
    Regime5UnlockCliffShortStrategy,
)

__all__ = [
    "CROSS_MARGIN_APPROVED_STRATEGIES",
    "REGIME_CROSS_MARGIN_STRATEGY_CLASSES",
    "drop_opposite_side_conflicts",
    "stamp_cross_margin_classifier_inputs",
    "Regime1PositiveGammaFadeStrategy",
    "Regime2ShortSqueezeBreakoutStrategy",
    "Regime3XsLeaderPullbackStrategy",
    "Regime4CrowdedLongCarryShortStrategy",
    "Regime4DefensiveDeepFadeStrategy",
    "Regime5CrisisContagionShortStrategy",
    "Regime5UnlockCliffShortStrategy",
]
