"""Dual-Mode Perpetual Short Alpha & Portfolio Beta-Hedge Engine (`perp_short_alpha_engine.py`).

Completes the short side of the 5-State institutional architecture without polluting
the Spot-only ``decision_log`` stream:

1. **Mode A — State 4 / State 5 Portfolio Beta-Hedge Shield (`build_perp_beta_hedge_orders`)**:
   - Turns ``require_perp_beta_hedge = True`` into concrete, executable 1x Isolated
     ``BTCUSDT`` / ``ETHUSDT`` USDT-M perpetual short hedge intents:
     * State 4 (``crowded_long_defensive``): 50% Beta-weighted hedge ratio.
     * State 5 (``crisis_liquidation_halt`` / macro blackout): 100% full Beta lock.

2. **Mode B — Three Asymmetric Directional Short Playbooks (`evaluate_directional_short_candidates`)**:
   - ``unlock_cliff_short``: ``SPECULATIVE`` token inside VC/Team cliff unlock window
     (``in_unlock_blackout == 1``) with weak L2 bid support or weak taker absorption -> 15% 1x short.
   - ``crowded_long_funding_carry_short``: Overcrowded retail longs + whale short divergence
     (``global_account_ls_ratio >= 2.20`` and ``whale_retail_divergence <= 0.45``) or extreme
     positive funding (``funding_z >= 2.3`` / ``funding_rate >= +0.00035``) -> 20% 1x short
     (collects positive 8h funding carry while positioning for long-unwind cascade).
   - ``cross_asset_crash_contagion_short``: ``NVDA.O`` / ``SMH.O`` 1h dump ``<= -2.0%``,
     Leave-One-Out option put-wing crash spike (``>= +4.0%``), or active
     ``long_liquidation_waterfall`` -> 20% 1x short on high-beta / AI tokens.

3. **Hard Anti-Squeeze Veto**:
   - Any symbol exhibiting short-squeeze fuel (``funding_z <= -1.0``, ``funding_rate <= -0.0001``,
     ``short_squeeze_liquidation``, or ``cot_institutional_squeeze_favorable``) is strictly
     vetoed from opening new directional shorts and emits an immediate ``close_short`` signal.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from decision_engine.core.conviction_whitelist import get_conviction_tier
from decision_engine.core.perp_microstructure_and_beta_hedge import (
    PERP_VENUE_ALIAS_FOR_STRATEGY,
    compute_portfolio_beta_and_hedge,
)
from decision_engine.core.smile_and_quantile_models import fit_volatility_smile_residuals
from decision_engine.core.wall_mid_pricing import extract_wall_mid_from_row

PERP_SHORT_ENGINE_ENV = "ENABLE_PERP_SHORT_ENGINE"
MAX_DIRECTIONAL_SHORTS_ENV = "MAX_CONCURRENT_PERP_SHORTS"
DEFAULT_MAX_DIRECTIONAL_SHORTS = 2
_BOOK_ENV = "BINANCE_SPOT_BOOK_USDT"
_MIN_OPEN_NOTIONAL_USDT = 20.0
_TERMINAL_SPOT_STATUSES = frozenset({"closed", "closed_dust", "flat", "oco_filled"})

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_SPOT_TO_USDM_PERP_MAP: dict[str, str] = {
    **PERP_VENUE_ALIAS_FOR_STRATEGY,
    "PEPEUSDT": "1000PEPEUSDT",
    "SHIBUSDT": "1000SHIBUSDT",
    "BONKUSDT": "1000BONKUSDT",
    "FLOKIUSDT": "1000FLOKIUSDT",
}
_SCALED_1000_CONTRACTS = frozenset({"1000PEPEUSDT", "1000SHIBUSDT", "1000BONKUSDT", "1000FLOKIUSDT"})


def _finite(val: object) -> float | None:
    if isinstance(val, bool) or val is None or val == "":
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def is_perp_short_engine_enabled(environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get(PERP_SHORT_ENGINE_ENV, "")).strip().lower() in _TRUTHY


def resolve_perp_book_usdt(book_usdt: float | None = None) -> float | None:
    """Positive spot book, or None when the caller and the env are both silent.

    A missing book is not replaced with a constant. Directional notionals stay
    unsent until a real book is supplied.
    """
    if book_usdt is not None:
        num = _finite(book_usdt)
        return num if num is not None and num > 0 else None
    num = _finite(os.environ.get(_BOOK_ENV))
    return num if num is not None and num > 0 else None


def open_spot_symbols(positions: Sequence[dict[str, Any]]) -> set[str]:
    """Spot symbols with a non-terminal row. A missing status counts as open."""
    held: set[str] = set()
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        status = str(pos.get("status") or "open").strip().lower()
        if status in _TERMINAL_SPOT_STATUSES:
            continue
        sym = str(pos.get("symbol") or pos.get("binance_symbol") or "").strip().upper()
        if sym:
            held.add(sym)
    return held


def _marked_spot_notional(pos: dict[str, Any]) -> bool:
    qty = _finite(pos.get("quantity"))
    px = _finite(pos.get("entry_price"))
    return qty is not None and px is not None and qty > 0 and px > 0


def map_spot_to_usdm_perp(symbol: str) -> tuple[str, float]:
    """Map a spot USDT symbol to its Binance USDT-M perpetual symbol and price multiplier."""
    clean = str(symbol or "").strip().upper()
    perp_sym = _SPOT_TO_USDM_PERP_MAP.get(clean, clean)
    scale = 1000.0 if perp_sym in _SCALED_1000_CONTRACTS and not clean.startswith("1000") else 1.0
    return perp_sym, scale


def is_short_squeeze_veto(row: dict[str, Any]) -> tuple[bool, str | None]:
    """Hard veto preventing directional shorts when squeeze fuel is building."""
    funding_z = _finite(row.get("funding_z", row.get("jev_funding_z")))
    funding_rate = _finite(row.get("funding_rate", row.get("lastFundingRate")))
    liq_cascade = str(row.get("liq_cascade_regime") or "").strip().lower()
    cot_regime = str(row.get("cot_regime") or "").strip().lower()
    whale_div = _finite(row.get("whale_retail_divergence"))

    if liq_cascade in ("short_squeeze_liquidation", "short_liquidation_squeeze"):
        return True, "veto_active_short_squeeze_cascade"
    if funding_z is not None and funding_z <= -1.0:
        return True, "veto_negative_funding_z_squeeze_risk"
    if funding_rate is not None and funding_rate <= -0.0001:
        return True, "veto_negative_funding_rate_squeeze_risk"
    if cot_regime == "cot_institutional_squeeze_favorable" and (whale_div is None or whale_div >= 1.10):
        return True, "veto_cftc_institutional_squeeze_regime"
    return False, None


@dataclass(frozen=True)
class PerpShortSignal:
    action: str  # "open_short" | "close_short"
    mode: str  # "beta_hedge" | "directional_alpha" | "squeeze_emergency_exit"
    playbook: str
    spot_symbol: str
    perp_symbol: str
    size_pct: float
    notional_usdt: float | None
    leverage: int
    margin_type: str
    order_type: str  # "LIMIT_MAKER" | "MARKET"
    entry_price: float | None
    stop_price: float | None
    take_profit_price: float | None
    conviction_tier: str
    reason: str
    expected_funding_carry_8h: float | None = None


def _resolve_spot_price(row: dict[str, Any]) -> float | None:
    for key in ("price_usd", "close", "last_price", "price", "perp_mark_price"):
        px = _finite(row.get(key))
        if px is not None and px > 0:
            return px
    return None


def _put_wing_spike(row: dict[str, Any], spot_px: float) -> bool:
    opt_chain = row.get("option_chain")
    if isinstance(opt_chain, Sequence) and not isinstance(opt_chain, (str, bytes)):
        snap = fit_volatility_smile_residuals(opt_chain, spot_px)
        if snap is not None and snap.residual_provenance == "leave_one_out":
            peak = snap.max_put_wing_residual_pct
            return peak is not None and peak >= 4.0
    if str(row.get("put_wing_residual_provenance") or "").strip().lower() == "leave_one_out":
        res = _finite(row.get("put_wing_iv_residual_pct"))
        return res is not None and res >= 4.0
    return False


def evaluate_directional_short_candidates(
    hot_rows: Sequence[dict[str, Any]],
    *,
    book_usdt: float | None = None,
    max_shorts: int | None = None,
    macro_halt: str | None = None,
    blocked_spot_symbols: set[str] | None = None,
) -> list[PerpShortSignal]:
    """Scan ``hot_rows`` for high-asymmetry 1x isolated perpetual short setups & squeeze exits.

    Missing book, missing taker/book weakness, or an already-open spot symbol
    does not open a short. Squeeze exits still emit.
    """
    resolved_book = resolve_perp_book_usdt(book_usdt)
    blocked = {str(sym).strip().upper() for sym in (blocked_spot_symbols or set()) if str(sym).strip()}
    cap_count = (
        int(max_shorts)
        if max_shorts is not None and max_shorts > 0
        else int(_finite(os.environ.get(MAX_DIRECTIONAL_SHORTS_ENV)) or DEFAULT_MAX_DIRECTIONAL_SHORTS)
    )

    scored_opens: list[tuple[float, PerpShortSignal]] = []
    squeeze_exits: list[PerpShortSignal] = []

    for row in hot_rows:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("binance_symbol") or row.get("symbol") or row.get("token_address") or "").strip().upper()
        if not sym or not sym.endswith("USDT"):
            continue

        perp_sym, px_scale = map_spot_to_usdm_perp(sym)
        conv_tier = get_conviction_tier(sym)
        veto, veto_reason = is_short_squeeze_veto(row)
        if veto:
            if bool(row.get("has_open_perp_short", False)):
                squeeze_exits.append(
                    PerpShortSignal(
                        action="close_short",
                        mode="squeeze_emergency_exit",
                        playbook="anti_squeeze_hard_veto_close",
                        spot_symbol=sym,
                        perp_symbol=perp_sym,
                        size_pct=1.0,
                        notional_usdt=None,
                        leverage=1,
                        margin_type="ISOLATED",
                        order_type="MARKET",
                        entry_price=None,
                        stop_price=None,
                        take_profit_price=None,
                        conviction_tier=conv_tier,
                        reason=str(veto_reason or "squeeze_veto"),
                    )
                )
            continue

        if sym in blocked:
            continue

        spot_px = _resolve_spot_price(row)
        if spot_px is None or spot_px <= 0:
            continue

        funding_rate = _finite(row.get("funding_rate", row.get("lastFundingRate")))
        funding_z = _finite(row.get("funding_z", row.get("jev_funding_z")))
        glob_ls = _finite(row.get("global_account_ls_ratio"))
        top_ls = _finite(row.get("top_position_ls_ratio"))
        whale_div = _finite(row.get("whale_retail_divergence"))
        if whale_div is None and top_ls is not None and glob_ls is not None and glob_ls > 0:
            whale_div = top_ls / glob_ls

        obi_top5 = _finite(row.get("obi_top5") or row.get("spot_obi_top5"))
        taker_buy = _finite(row.get("taker_buy_ratio") or row.get("spot_taker_buy_ratio"))
        nvda_1h = _finite(row.get("nvda_1h_ret_pct"))
        smh_1h = _finite(row.get("smh_1h_ret_pct"))
        liq_cascade = str(row.get("liq_cascade_regime") or "").strip().lower()
        raw_unlock = row.get("in_unlock_blackout")
        in_unlock = bool(int(raw_unlock)) if isinstance(raw_unlock, (int, float, bool)) else False

        playbook: str | None = None
        size_pct = 0.0
        score = 0.0
        stop_dist_pct = 0.020
        tp_dist_pct = 0.050
        reason = ""

        # Spear 1: VC/Team Cliff Unlock Dump on Speculative Altcoins
        obi_weak = obi_top5 is not None and obi_top5 <= -0.10
        taker_unlock_weak = taker_buy is not None and taker_buy <= 0.46
        if in_unlock and conv_tier == "SPECULATIVE":
            if obi_weak and taker_unlock_weak:
                playbook = "unlock_cliff_short"
                size_pct = 0.15
                stop_dist_pct = 0.022
                tp_dist_pct = 0.060
                score = 85.0 + (abs(obi_top5) * 20.0 if obi_top5 is not None else 0.0)
                reason = "vc_cliff_unlock_supply_overhang"

        # Spear 2: Crowded Retail Long Trap + Positive Funding Carry Short
        is_retail_trap = glob_ls is not None and glob_ls >= 2.20 and whale_div is not None and whale_div <= 0.45
        is_extreme_pos_funding = (funding_z is not None and funding_z >= 2.30) or (
            funding_rate is not None and funding_rate >= 0.00035
        )
        if playbook is None and (is_retail_trap or (is_extreme_pos_funding and (whale_div is None or whale_div <= 0.65))):
            if taker_buy is not None and taker_buy <= 0.48:
                playbook = "crowded_long_funding_carry_short"
                size_pct = 0.20
                stop_dist_pct = 0.018
                tp_dist_pct = 0.048
                carry_bonus = (funding_rate or 0.0) * 10_000.0
                score = 90.0 + carry_bonus + (glob_ls or 2.0) * 5.0
                reason = "retail_long_trap_positive_funding_carry"

        # Spear 3: Cross-Asset NVDA/SMH Dump, Option Put-Wing Crash Insurance, or Long Liquidation Waterfall
        is_macro_or_tech_crash = (
            (nvda_1h is not None and nvda_1h <= -2.0)
            or (smh_1h is not None and smh_1h <= -2.0)
            or liq_cascade == "long_liquidation_waterfall"
            or _put_wing_spike(row, spot_px)
            or bool(macro_halt and "oob" in str(macro_halt).lower())
        )
        if playbook is None and is_macro_or_tech_crash:
            if taker_buy is not None and taker_buy <= 0.45:
                playbook = "cross_asset_crash_contagion_short"
                size_pct = 0.20 if conv_tier != "SPECULATIVE" else 0.15
                stop_dist_pct = 0.020
                tp_dist_pct = 0.055
                score = 88.0 + abs(min(nvda_1h or 0.0, smh_1h or 0.0)) * 4.0
                reason = "cross_asset_or_liquidation_waterfall_short"

        if playbook is None or size_pct <= 0 or resolved_book is None:
            continue

        # Prefer WallMid 1-tick frontrun in front of heavy ask wall when L2 book is available
        wall_quote = extract_wall_mid_from_row(row)
        order_type = "LIMIT_MAKER"
        entry_spot_px = spot_px
        if wall_quote is not None and wall_quote.maker_sell_price > 0:
            entry_spot_px = wall_quote.maker_sell_price
        elif liq_cascade == "long_liquidation_waterfall":
            order_type = "MARKET"

        perp_entry = round(entry_spot_px * px_scale, 8)
        perp_stop = round(perp_entry * (1.0 + stop_dist_pct), 8)
        if wall_quote is not None and wall_quote.ask_wall_price > entry_spot_px:
            wall_stop = round(wall_quote.ask_wall_price * px_scale * 1.006, 8)
            # Tighter stop just above whale ask wall, clamped within [0.6%, 2.5%]
            min_stop = round(perp_entry * 1.006, 8)
            max_stop = round(perp_entry * 1.025, 8)
            perp_stop = min(max_stop, max(min_stop, wall_stop))
        perp_tp = round(perp_entry * (1.0 - tp_dist_pct), 8)

        notional = round(size_pct * resolved_book, 2)
        if notional < _MIN_OPEN_NOTIONAL_USDT:
            continue
        eff_pct = round(min(0.35, notional / resolved_book), 6)

        scored_opens.append(
            (
                score,
                PerpShortSignal(
                    action="open_short",
                    mode="directional_alpha",
                    playbook=playbook,
                    spot_symbol=sym,
                    perp_symbol=perp_sym,
                    size_pct=eff_pct,
                    notional_usdt=notional,
                    leverage=1,
                    margin_type="ISOLATED",
                    order_type=order_type,
                    entry_price=perp_entry,
                    stop_price=perp_stop,
                    take_profit_price=perp_tp,
                    conviction_tier=conv_tier,
                    reason=reason,
                    expected_funding_carry_8h=funding_rate,
                ),
            )
        )

    scored_opens.sort(key=lambda item: item[0], reverse=True)
    selected_opens = [sig for _score, sig in scored_opens[:cap_count]]
    return squeeze_exits + selected_opens


def build_perp_beta_hedge_orders(
    open_spot_positions: Sequence[dict[str, Any]],
    accepted_spot_buys: Sequence[dict[str, Any]],
    hot_rows: Sequence[dict[str, Any]],
    *,
    macro_halt: str | None = None,
    book_usdt: float | None = None,
) -> list[PerpShortSignal]:
    """Generate concrete 50% (State 4) or 100% (State 5) BTC/ETH Perp Beta-hedge short orders."""
    has_state5 = bool(macro_halt) or any(
        str(r.get("liq_cascade_regime") or "").strip().lower() == "long_liquidation_waterfall"
        or bool(r.get("xs_systemic_crash"))
        for r in hot_rows
        if isinstance(r, dict)
    )
    has_state4 = any(
        bool(sig.get("require_perp_beta_hedge"))
        and str(sig.get("market_regime") or "") == "crowded_long_defensive"
        for sig in accepted_spot_buys
        if isinstance(sig, dict)
    )

    if not has_state5 and not has_state4:
        return []

    hedge_ratio = 1.00 if has_state5 else 0.50
    regime_label = "state5_full_beta_lock_100pct" if has_state5 else "state4_defensive_beta_hedge_50pct"

    resolved_book = resolve_perp_book_usdt(book_usdt)
    if resolved_book is None:
        # Quantity times entry is a marked notional. Candidate percents are not,
        # and a stand-in book must not turn them into hedge size.
        marked = [row for row in open_spot_positions if isinstance(row, dict) and _marked_spot_notional(row)]
        hedge_calc = compute_portfolio_beta_and_hedge(
            marked,
            candidate_signals=(),
            book_usdt=1.0,
        )
    else:
        hedge_calc = compute_portfolio_beta_and_hedge(
            open_spot_positions,
            candidate_signals=accepted_spot_buys,
            book_usdt=resolved_book,
        )
    total_beta_usdt = float(hedge_calc.get("beta_weighted_usdt") or 0.0) * hedge_ratio
    if total_beta_usdt < 20.0:
        return []

    px_by_sym: dict[str, float] = {}
    for r in hot_rows:
        if isinstance(r, dict):
            s = str(r.get("binance_symbol") or r.get("symbol") or "").strip().upper()
            px = _resolve_spot_price(r)
            if s and px is not None:
                px_by_sym[s] = px

    # If total hedge notional is small (< 45 USDT), concentrate into single BTCUSDT leg to clear 20 USDT minimum cleanly
    basket = hedge_calc["hedge_recommendations"]["basket_hedge"]
    legs_spec: list[tuple[str, float]] = []
    if total_beta_usdt < 45.0:
        legs_spec.append(("BTCUSDT", round(total_beta_usdt, 2)))
    else:
        btc_leg = round(float(basket["BTCUSDT_PERP_SHORT_USDT"]) * hedge_ratio, 2)
        eth_leg = round(float(basket["ETHUSDT_PERP_SHORT_USDT"]) * hedge_ratio, 2)
        if btc_leg >= 20.0:
            legs_spec.append(("BTCUSDT", btc_leg))
        if eth_leg >= 20.0:
            legs_spec.append(("ETHUSDT", eth_leg))
        elif btc_leg >= 20.0 and eth_leg > 0:
            legs_spec = [("BTCUSDT", round(btc_leg + eth_leg, 2))]

    orders: list[PerpShortSignal] = []
    for sym, leg_usdt in legs_spec:
        ref_px = px_by_sym.get(sym)
        stop_px = round(ref_px * 1.030, 8) if ref_px is not None else None
        tp_px = round(ref_px * 0.940, 8) if ref_px is not None else None
        pct = round(min(1.0, leg_usdt / resolved_book), 6) if resolved_book else 0.0
        orders.append(
            PerpShortSignal(
                action="open_short",
                mode="beta_hedge",
                playbook=regime_label,
                spot_symbol=sym,
                perp_symbol=sym,
                size_pct=pct,
                notional_usdt=leg_usdt,
                leverage=1,
                margin_type="ISOLATED",
                order_type="MARKET" if has_state5 else "LIMIT_MAKER",
                entry_price=ref_px,
                stop_price=stop_px,
                take_profit_price=tp_px,
                conviction_tier="S",
                reason=f"portfolio_beta_hedge_ratio_{hedge_ratio:.2f}",
            )
        )
    return orders


def persist_perp_short_signals(
    db_path: str | Path,
    signals: Sequence[PerpShortSignal],
) -> int:
    """Persist perp short & hedge signals into an isolated ``perp_short_signals`` table."""
    if not signals:
        return 0
    ts_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS perp_short_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                action TEXT NOT NULL,
                mode TEXT NOT NULL,
                playbook TEXT NOT NULL,
                spot_symbol TEXT NOT NULL,
                perp_symbol TEXT NOT NULL,
                size_pct REAL NOT NULL,
                notional_usdt REAL,
                leverage INTEGER NOT NULL,
                margin_type TEXT NOT NULL,
                order_type TEXT NOT NULL,
                entry_price REAL,
                stop_price REAL,
                take_profit_price REAL,
                raw_json TEXT NOT NULL
            )
            """
        )
        for sig in signals:
            payload = asdict(sig)
            conn.execute(
                """
                INSERT INTO perp_short_signals (
                    ts, action, mode, playbook, spot_symbol, perp_symbol,
                    size_pct, notional_usdt, leverage, margin_type, order_type,
                    entry_price, stop_price, take_profit_price, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts_iso,
                    sig.action,
                    sig.mode,
                    sig.playbook,
                    sig.spot_symbol,
                    sig.perp_symbol,
                    sig.size_pct,
                    sig.notional_usdt,
                    sig.leverage,
                    sig.margin_type,
                    sig.order_type,
                    sig.entry_price,
                    sig.stop_price,
                    sig.take_profit_price,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
        conn.commit()
        return len(signals)
    finally:
        conn.close()


def run_perp_short_tick(
    *,
    hot_rows: Sequence[dict[str, Any]],
    open_spot_positions: Sequence[dict[str, Any]] = (),
    accepted_spot_buys: Sequence[dict[str, Any]] = (),
    macro_halt: str | None = None,
    db_path: str | Path | None = None,
    book_usdt: float | None = None,
) -> dict[str, Any]:
    """Run both Beta-Hedge Shield and Directional Short Spear for one tick."""
    hedge_orders = build_perp_beta_hedge_orders(
        open_spot_positions,
        accepted_spot_buys,
        hot_rows,
        macro_halt=macro_halt,
        book_usdt=book_usdt,
    )
    directional_orders = evaluate_directional_short_candidates(
        hot_rows,
        book_usdt=book_usdt,
        macro_halt=macro_halt,
        blocked_spot_symbols=open_spot_symbols(open_spot_positions),
    )
    combined = hedge_orders + directional_orders
    written = 0
    if db_path is not None and combined:
        written = persist_perp_short_signals(db_path, combined)
    return {
        "enabled": True,
        "hedge_orders": len(hedge_orders),
        "directional_orders": len([s for s in directional_orders if s.action == "open_short"]),
        "squeeze_exits": len([s for s in directional_orders if s.action == "close_short"]),
        "written": written,
        "signals": [asdict(s) for s in combined],
    }
