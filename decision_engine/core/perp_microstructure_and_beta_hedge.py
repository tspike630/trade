r"""Perpetual Futures Microstructure Filter & Portfolio Beta-Neutral Hedge Engine.

Bridges the gap between a Spot-only directional book and an institutional
derivatives-aware, market-neutral statistical arbitrage desk:

1. **Perpetual Futures & Options Counterparty Microstructure Filter (`evaluate_perp_microstructure`)**:
   - Inspects USD-M perpetual ``funding_rate`` (8h rate), ``funding_z`` (rolling z-score),
     ``perp_basis_bps`` (``(perp_mark - index_price) / index_price * 10_000``),
     Whale-vs-Retail Long/Short divergence (``top_position_ls_ratio`` vs
     ``global_account_ls_ratio``), forced liquidation cascade (``liq_cascade_regime``),
     cross-exchange spot price dislocation (``max_venue_dislocation_bps``),
     token cliff unlock blackout (``in_unlock_blackout``), and Deribit Options Gamma/DVOL regime
     (``gamma_regime``, ``dvol_zscore``).
   - Blocks 5m spot dip-buying during VC/Team cliff unlock windows (`[-72h, +48h]`
     -> ``token_unlock_cliff_blackout``).
   - Blocks spot buying when single-venue flash-wick manipulation diverges from
     OKX/Bybit spot median by `>= 35 bps` (``cross_exchange_wick_dislocation``).
   - Blocks spot buying during active long liquidation waterfalls
     (``perp_long_liquidation_waterfall``).
   - Blocks 5m spot dip-buying when perpetual longs are overcrowded and actively
     cascading (`funding_rate > MAX_CROWDED_FUNDING_RATE` = +0.05%/8h or
     `funding_z > 2.5` -> ``perp_long_crowded_cascade``).
   - Blocks spot buying when perpetuals trade at an extreme toxic discount
     (`perp_basis_bps < -80 bps`) without buyer absorption (`taker_buy_ratio < 0.42`
     -> ``perp_toxic_discount_dump``).
   - Blocks spot buying when retail accounts are overwhelmingly long while top
     whale positions are net short (``global_account_ls_ratio >= 2.20`` and
     ``whale_retail_divergence <= 0.45`` -> ``perp_retail_long_trap``).
   - Blocks spot buying during options dealer negative-gamma volatility spikes
     (``gamma_regime == 'negative_gamma_slippery'`` and ``dvol_zscore >= 2.0``
     -> ``options_negative_gamma_crash``).
   - Identifies crowded-short squeeze or whale accumulation setups
     (``short_squeeze_favorable`` / ``whale_accumulation_favorable``).

2. **Portfolio Beta Budget & BTC/ETH Delta-Neutral Hedge Sizer (`compute_portfolio_beta_and_hedge`)**:
   - Assigns empirical BTC Beta ($\beta_i$) per asset and sector cluster.
   - Enforces a portfolio-wide Beta-adjusted exposure cap (`LIVE_BNB_MAX_PORTFOLIO_BETA`,
     default `0.45` when enabled) so high-beta altcoins (`ai_compute`, `meme_high_beta`)
     cannot compound tail risk during market-wide selloffs.
   - Computes the exact short notional in ``BTCUSDT-PERP`` (and optional ``ETHUSDT-PERP``
     split) required to neutralize portfolio market Beta ($\Delta_{\beta} = 0$).
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from logging_setup import get_logger

from decision_engine.core.portfolio_clusters import symbol_sector_cluster

log = get_logger("decision_engine.perp_beta_hedge")

PERP_PREMIUM_INDEX_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"

REQUIRE_PERP_FILTER_ENV = "LIVE_BNB_REQUIRE_PERP_FILTER"
MAX_CROWDED_FUNDING_ENV = "LIVE_BNB_MAX_CROWDED_FUNDING_RATE"
MAX_FUNDING_Z_ENV = "LIVE_BNB_MAX_FUNDING_Z"
MIN_PERP_BASIS_BPS_ENV = "LIVE_BNB_MIN_PERP_BASIS_BPS"

PORTFOLIO_BETA_GATE_ENV = "LIVE_BNB_PORTFOLIO_BETA_GATE"
MAX_PORTFOLIO_BETA_ENV = "LIVE_BNB_MAX_PORTFOLIO_BETA"
BOOK_USDT_ENV = "BINANCE_SPOT_BOOK_USDT"

DEFAULT_MAX_CROWDED_FUNDING_RATE = 0.0005  # +5 bps per 8h (+54.7% annualized)
DEFAULT_SHORT_SQUEEZE_FUNDING_RATE = -0.0003  # -3 bps per 8h
DEFAULT_MAX_FUNDING_Z = 2.5
DEFAULT_MIN_PERP_BASIS_BPS = -80.0
DEFAULT_MAX_PORTFOLIO_BETA = 0.45
DEFAULT_POSITION_SIZE_PCT = 0.05

_OPEN_STATUSES = frozenset({"open", "oco_working", "exit_submitted"})

# Empirical beta vs BTCUSDT across clusters and major assets
CLUSTER_BETA_MAP: dict[str, float] = {
    "major_l1": 1.45,
    "ai_compute": 1.65,
    "meme_high_beta": 1.85,
    "defi_infra": 1.35,
    "l2_scaling": 1.50,
    "gaming_metaverse": 1.55,
    "legacy_pow_privacy": 1.15,
    "idiosyncratic": 1.40,
}

SYMBOL_BETA_OVERRIDES: dict[str, float] = {
    "BTCUSDT": 1.00,
    "ETHUSDT": 1.15,
    "BNBUSDT": 1.05,
    "SOLUSDT": 1.45,
}


def _env_true(name: str) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def require_perp_filter_enabled() -> bool:
    return _env_true(REQUIRE_PERP_FILTER_ENV)


def portfolio_beta_gate_enabled() -> bool:
    return _env_true(PORTFOLIO_BETA_GATE_ENV)


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return num


def _env_float(name: str, default: float) -> float:
    val = _finite(os.environ.get(name))
    return float(val) if val is not None else default


def symbol_beta_vs_btc(symbol: str | None) -> float:
    """Return empirical Beta vs. BTCUSDT for ``symbol``."""
    raw = str(symbol or "").strip().upper()
    if raw in SYMBOL_BETA_OVERRIDES:
        return SYMBOL_BETA_OVERRIDES[raw]
    cluster = symbol_sector_cluster(raw)
    return CLUSTER_BETA_MAP.get(cluster, 1.40)


@dataclass(frozen=True)
class PerpMicrostructureVerdict:
    allow: bool
    reject_reason: str | None
    perp_regime: str | None
    funding_rate: float | None
    funding_z: float | None
    perp_basis_bps: float | None
    size_readjustment_multiplier: float = 1.0


def evaluate_perp_microstructure(
    row: dict[str, Any],
    *,
    taker_buy_ratio: float | None = None,
) -> PerpMicrostructureVerdict:
    """Evaluate perpetual futures funding crowding, Whale/Retail L/S divergence, and Options Gamma."""
    funding_rate = _finite(row.get("funding_rate", row.get("lastFundingRate")))
    funding_z = _finite(row.get("funding_z", row.get("jev_funding_z")))
    basis_bps = _finite(row.get("perp_basis_bps"))

    if basis_bps is None:
        mark_px = _finite(row.get("perp_mark_price", row.get("markPrice")))
        index_px = _finite(row.get("perp_index_price", row.get("indexPrice")))
        if mark_px is not None and index_px is not None and index_px > 0:
            basis_bps = ((mark_px - index_px) / index_px) * 10_000.0

    top_ls = _finite(row.get("top_position_ls_ratio"))
    glob_ls = _finite(row.get("global_account_ls_ratio"))
    whale_div = _finite(row.get("whale_retail_divergence"))
    if whale_div is None and top_ls is not None and glob_ls is not None and glob_ls > 0:
        whale_div = top_ls / glob_ls

    gamma_regime = str(row.get("gamma_regime") or "").strip().lower()
    dvol_z = _finite(row.get("dvol_zscore"))
    liq_cascade = str(row.get("liq_cascade_regime") or "").strip().lower()
    max_venue_dislocation_bps = _finite(row.get("max_venue_dislocation_bps"))
    raw_unlock = row.get("in_unlock_blackout")
    in_unlock_blackout = bool(int(raw_unlock)) if isinstance(raw_unlock, (int, float, bool)) else False

    has_any_signal = any(
        x is not None
        for x in (funding_rate, funding_z, basis_bps, whale_div, dvol_z, max_venue_dislocation_bps)
    ) or bool(gamma_regime) or bool(liq_cascade) or in_unlock_blackout

    if not has_any_signal:
        if require_perp_filter_enabled():
            return PerpMicrostructureVerdict(
                allow=False,
                reject_reason="perp_microstructure_missing",
                perp_regime="missing",
                funding_rate=None,
                funding_z=None,
                perp_basis_bps=None,
            )
        return PerpMicrostructureVerdict(
            allow=True,
            reject_reason=None,
            perp_regime=None,
            funding_rate=None,
            funding_z=None,
            perp_basis_bps=None,
        )

    max_funding = _env_float(MAX_CROWDED_FUNDING_ENV, DEFAULT_MAX_CROWDED_FUNDING_RATE)
    max_z = _env_float(MAX_FUNDING_Z_ENV, DEFAULT_MAX_FUNDING_Z)
    min_basis = _env_float(MIN_PERP_BASIS_BPS_ENV, DEFAULT_MIN_PERP_BASIS_BPS)

    # 0A. Token Cliff Unlock Blackout (Category 9 / I-4 [-72h, +48h] VC/Team dump risk)
    if in_unlock_blackout:
        return PerpMicrostructureVerdict(
            allow=False,
            reject_reason="token_unlock_cliff_blackout",
            perp_regime="unlock_cliff_blackout",
            funding_rate=funding_rate,
            funding_z=funding_z,
            perp_basis_bps=basis_bps,
        )

    # 0B. Cross-Exchange Spot Dislocation / Single-Venue Flash Wick (Category 8A >= 35 bps)
    if max_venue_dislocation_bps is not None and max_venue_dislocation_bps >= 35.0:
        return PerpMicrostructureVerdict(
            allow=False,
            reject_reason="cross_exchange_wick_dislocation",
            perp_regime="venue_dislocation_manipulation",
            funding_rate=funding_rate,
            funding_z=funding_z,
            perp_basis_bps=basis_bps,
        )

    # 0C. Active Perpetual Long Liquidation Waterfall (Category 6 OI Flush + Price Dump)
    if liq_cascade == "long_liquidation_waterfall":
        return PerpMicrostructureVerdict(
            allow=False,
            reject_reason="perp_long_liquidation_waterfall",
            perp_regime="long_liquidation_waterfall",
            funding_rate=funding_rate,
            funding_z=funding_z,
            perp_basis_bps=basis_bps,
        )

    # 1. Overcrowded perpetual longs cascading into spot
    if funding_rate is not None and funding_rate > max_funding:
        return PerpMicrostructureVerdict(
            allow=False,
            reject_reason="perp_long_crowded_cascade",
            perp_regime="long_crowded_cascade",
            funding_rate=funding_rate,
            funding_z=funding_z,
            perp_basis_bps=basis_bps,
        )
    if funding_z is not None and funding_z > max_z:
        return PerpMicrostructureVerdict(
            allow=False,
            reject_reason="perp_long_crowded_cascade",
            perp_regime="long_crowded_cascade",
            funding_rate=funding_rate,
            funding_z=funding_z,
            perp_basis_bps=basis_bps,
        )

    # 2. Extreme perpetual discount without spot buyer absorption
    tbr = taker_buy_ratio if taker_buy_ratio is not None else _finite(row.get("taker_buy_ratio"))
    if basis_bps is not None and basis_bps < min_basis:
        if tbr is None or tbr < 0.42:
            return PerpMicrostructureVerdict(
                allow=False,
                reject_reason="perp_toxic_discount_dump",
                perp_regime="toxic_discount_dump",
                funding_rate=funding_rate,
                funding_z=funding_z,
                perp_basis_bps=basis_bps,
            )

    # 3. Retail Long Trap vs Whale Short Divergence (Counterparty Stop-Hunt Risk)
    if (
        (glob_ls is not None and glob_ls >= 2.20 and (top_ls is not None and top_ls <= 0.95))
        or (glob_ls is not None and glob_ls >= 2.20 and whale_div is not None and whale_div <= 0.45)
    ):
        return PerpMicrostructureVerdict(
            allow=False,
            reject_reason="perp_retail_long_trap",
            perp_regime="retail_long_trap_cascade",
            funding_rate=funding_rate,
            funding_z=funding_z,
            perp_basis_bps=basis_bps,
        )

    # 4. Options Dealer Negative-Gamma Slippery Zone + DVOL Spike
    if gamma_regime == "negative_gamma_slippery" and dvol_z is not None and dvol_z >= 2.0:
        return PerpMicrostructureVerdict(
            allow=False,
            reject_reason="options_negative_gamma_crash",
            perp_regime="negative_gamma_crash",
            funding_rate=funding_rate,
            funding_z=funding_z,
            perp_basis_bps=basis_bps,
        )

    # 5. Crowded short squeeze fuel (deeply negative funding or short liquidation cascade)
    if (
        (
            funding_rate is not None
            and funding_rate <= DEFAULT_SHORT_SQUEEZE_FUNDING_RATE
            and (tbr is None or tbr >= 0.45)
        )
        or liq_cascade == "short_liquidation_squeeze"
    ):
        return PerpMicrostructureVerdict(
            allow=True,
            reject_reason=None,
            perp_regime="short_squeeze_favorable",
            funding_rate=funding_rate,
            funding_z=funding_z,
            perp_basis_bps=basis_bps,
            size_readjustment_multiplier=1.15,
        )

    # 6. Whale Accumulation Divergence (Whales heavily long relative to retail + non-crowded funding)
    if whale_div is not None and whale_div >= 1.35 and (funding_rate is None or funding_rate <= 0.0001):
        return PerpMicrostructureVerdict(
            allow=True,
            reject_reason=None,
            perp_regime="whale_accumulation_favorable",
            funding_rate=funding_rate,
            funding_z=funding_z,
            perp_basis_bps=basis_bps,
            size_readjustment_multiplier=1.15,
        )

    return PerpMicrostructureVerdict(
        allow=True,
        reject_reason=None,
        perp_regime="neutral",
        funding_rate=funding_rate,
        funding_z=funding_z,
        perp_basis_bps=basis_bps,
        size_readjustment_multiplier=1.0,
    )


def fetch_binance_perp_premium_index(
    symbol: str,
    *,
    timeout: float = 5.0,
) -> dict[str, Any] | None:
    """Public GET to ``https://fapi.binance.com/fapi/v1/premiumIndex``."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    query = urlencode({"symbol": sym})
    req = Request(
        f"{PERP_PREMIUM_INDEX_URL}?{query}",
        headers={"Accept": "application/json", "User-Agent": "trade-perp-microstructure"},
        method="GET",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read()
        data = json.loads(raw.decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    mark = _finite(data.get("markPrice"))
    index = _finite(data.get("indexPrice"))
    funding = _finite(data.get("lastFundingRate"))
    basis_bps = (
        ((mark - index) / index) * 10_000.0
        if mark is not None and index is not None and index > 0
        else None
    )
    return {
        "symbol": sym,
        "perp_mark_price": mark,
        "perp_index_price": index,
        "funding_rate": funding,
        "perp_basis_bps": round(basis_bps, 4) if basis_bps is not None else None,
    }


def _position_notional_and_pct(
    pos: dict[str, Any],
    *,
    book_usdt: float,
) -> tuple[float, float]:
    """Return ``(notional_usdt, size_pct_of_book)`` for an open ledger row."""
    qty = _finite(pos.get("quantity"))
    px = _finite(pos.get("entry_price"))
    if qty is not None and px is not None and qty > 0 and px > 0:
        notional = qty * px
        pct = notional / book_usdt if book_usdt > 0 else DEFAULT_POSITION_SIZE_PCT
        return notional, pct
    pct = _finite(pos.get("size_pct")) or DEFAULT_POSITION_SIZE_PCT
    notional = pct * book_usdt if book_usdt > 0 else 0.0
    return notional, pct


def compute_portfolio_beta_and_hedge(
    positions: Sequence[dict[str, Any]],
    *,
    candidate_signals: Sequence[dict[str, Any]] = (),
    purpose: str | None = None,
    book_usdt: float | None = None,
    btc_hedge_weight: float = 0.70,
    eth_hedge_weight: float = 0.30,
) -> dict[str, Any]:
    """Calculate portfolio gross long exposure, Beta-weighted exposure, and BTC/ETH Perp hedge notional."""
    resolved_book = (
        float(book_usdt)
        if book_usdt is not None and book_usdt > 0
        else _env_float(BOOK_USDT_ENV, 1000.0)
    )
    eth_beta_vs_btc = SYMBOL_BETA_OVERRIDES["ETHUSDT"]

    gross_long_usdt = 0.0
    beta_weighted_usdt = 0.0
    gross_size_pct = 0.0
    portfolio_beta_pct = 0.0
    active_legs: list[dict[str, Any]] = []

    for row in positions:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "").strip()
        if status not in _OPEN_STATUSES:
            continue
        row_purpose = str(row.get("purpose") or "").strip()
        if purpose and row_purpose and row_purpose != purpose:
            continue
        sym = str(row.get("symbol") or row.get("binance_symbol") or "").strip().upper()
        if not sym:
            continue
        notional, pct = _position_notional_and_pct(row, book_usdt=resolved_book)
        beta = symbol_beta_vs_btc(sym)
        gross_long_usdt += notional
        beta_weighted_usdt += notional * beta
        gross_size_pct += pct
        portfolio_beta_pct += pct * beta
        active_legs.append(
            {
                "symbol": sym,
                "source": "open_position",
                "notional_usdt": round(notional, 4),
                "size_pct": round(pct, 6),
                "beta_vs_btc": beta,
                "beta_notional_usdt": round(notional * beta, 4),
            }
        )

    for sig in candidate_signals:
        if not isinstance(sig, dict):
            continue
        if str(sig.get("action") or "").lower() != "buy":
            continue
        sym = str(sig.get("binance_symbol") or sig.get("token_address") or "").strip().upper()
        if not sym:
            continue
        pct = _finite(sig.get("size_pct")) or DEFAULT_POSITION_SIZE_PCT
        notional = pct * resolved_book
        beta = symbol_beta_vs_btc(sym)
        gross_long_usdt += notional
        beta_weighted_usdt += notional * beta
        gross_size_pct += pct
        portfolio_beta_pct += pct * beta
        active_legs.append(
            {
                "symbol": sym,
                "source": "candidate_signal",
                "notional_usdt": round(notional, 4),
                "size_pct": round(pct, 6),
                "beta_vs_btc": beta,
                "beta_notional_usdt": round(notional * beta, 4),
            }
        )

    w_btc = max(0.0, min(1.0, float(btc_hedge_weight)))
    w_eth = max(0.0, min(1.0 - w_btc, float(eth_hedge_weight)))
    btc_short_usdt = beta_weighted_usdt * w_btc
    eth_short_usdt = (beta_weighted_usdt * w_eth) / eth_beta_vs_btc if eth_beta_vs_btc > 0 else 0.0

    weighted_avg_beta = (
        beta_weighted_usdt / gross_long_usdt if gross_long_usdt > 0 else 0.0
    )

    return {
        "book_usdt": round(resolved_book, 2),
        "active_legs_count": len(active_legs),
        "gross_long_usdt": round(gross_long_usdt, 4),
        "gross_long_pct": round(gross_size_pct, 6),
        "beta_weighted_usdt": round(beta_weighted_usdt, 4),
        "portfolio_beta_pct": round(portfolio_beta_pct, 6),
        "weighted_avg_beta": round(weighted_avg_beta, 4),
        "hedge_recommendations": {
            "pure_btc_perp_short_usdt": round(beta_weighted_usdt, 2),
            "basket_hedge": {
                "BTCUSDT_PERP_SHORT_USDT": round(btc_short_usdt, 2),
                "ETHUSDT_PERP_SHORT_USDT": round(eth_short_usdt, 2),
            },
        },
        "legs": active_legs,
    }


def portfolio_beta_exposure_block(
    positions: Sequence[dict[str, Any]],
    *,
    purpose: str,
    symbol: str | None,
    candidate_size_pct: float | None = None,
    max_portfolio_beta: float | None = None,
    accepted_signals: Sequence[dict[str, Any]] = (),
) -> str | None:
    """Return a block reason when adding ``symbol`` would breach the portfolio Beta cap."""
    if not portfolio_beta_gate_enabled() and max_portfolio_beta is None:
        return None
    if not symbol:
        return None
    cap = (
        float(max_portfolio_beta)
        if max_portfolio_beta is not None
        else _env_float(MAX_PORTFOLIO_BETA_ENV, DEFAULT_MAX_PORTFOLIO_BETA)
    )
    if cap <= 0:
        return None
    state = compute_portfolio_beta_and_hedge(
        positions,
        candidate_signals=accepted_signals,
        purpose=purpose,
    )
    current_beta_pct = float(state["portfolio_beta_pct"])
    size_pct = (
        float(candidate_size_pct)
        if candidate_size_pct is not None and candidate_size_pct > 0
        else DEFAULT_POSITION_SIZE_PCT
    )
    cand_beta = symbol_beta_vs_btc(symbol)
    projected_beta_pct = current_beta_pct + size_pct * cand_beta
    if projected_beta_pct > cap + 1e-9:
        return (
            f"portfolio_beta_cap_exceeded:{symbol.strip().upper()}:"
            f"{projected_beta_pct:.4f}>{cap:.4f}"
        )
    return None
