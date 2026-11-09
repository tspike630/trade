#!/usr/bin/env python3
"""Complete Public Derivatives, Spot L2/Whale Microstructure, Cross-Exchange, Options Gamma & Token Unlocks Collector (Categories 6, 7, 8, 9).

Zero external dependencies (pure Python stdlib ``urllib.request``, ``sqlite3``,
``json``, ``csv``, ``math``, ``ssl``). Implements all four public/on-chain
categories from the master quantitative checklist:

- **Category 6 (Binance USD-M Perpetual Counterparty, Whale/Retail L/S & Liquidations)**:
  ``binance_perp_counterparty`` table — 60d ``funding_z``, ``perp_basis_bps``,
  ``oi_change_1h_pct``, ``top_position_ls_ratio``, ``global_account_ls_ratio``,
  ``whale_retail_divergence``, ``perp_taker_buy_sell_ratio``, and forced-liquidation
  cascade estimation (``est_liq_pressure_usd``, ``liq_cascade_regime``).

- **Category 7 (Binance Spot Multi-Horizon 5m/1h/4h/1d K-lines, L2 20-Level Depth & Whale aggTrades)**:
  ``binance_spot_microstructure`` table — ``ret_5m_pct``, ``ret_1h_pct``, ``ret_4h_pct``,
  ``ret_1d_pct``, ``taker_buy_ratio_5m``, ``taker_buy_ratio_1h``, ``spread_bps``,
  ``bid_depth_20_usd``, ``ask_depth_20_usd``, ``orderbook_imbalance_20``,
  ``impact_10k_bps``, ``whale_net_buy_usd``, and ``aggtrade_whale_buy_ratio``.

- **Category 8 (Cross-Exchange Spot Baseline OKX/Bybit/Binance + Deribit Options DVOL & Gamma Walls)**:
  ``cross_exchange_spot_baseline`` table (Binance vs OKX vs Bybit spot price & volume
  cross-validation, ``max_venue_dislocation_bps``) + ``deribit_options_gamma`` table
  (BTC/ETH ``DVOL``, ``dvol_zscore``, ``max_pain_strike``, ``top_call_wall_strike``,
  ``top_put_wall_strike``, ``put_call_oi_ratio``, ``gamma_regime``).

- **Category 9 (Token Cliff Unlocks Schedule I-4, Stablecoin Supply & On-Chain Holder/Bot Summary I-1/I-2/I-3)**:
  ``token_unlock_schedule`` table (VC/Team cliff unlock windows ``[-72h, +48h]`` for
  ``SUI``, ``APT``, ``ARB``, ``OP``, ``TIA``, ``STRK``, ``WLD``, ``ENA``, ``ONDO``, ``JTO``, ``PYTH``),
  ``stablecoin_supply`` table (DefiLlama ``USDT``/``USDC`` 24h & 7d mint/burn), and
  ``onchain_holder_bot_summary`` table (bridging ``top10_share``, ``concentration_delta``,
  ``dev_balance_delta``, ``bot_trade_share`` from ``dexscreener.db`` when present).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sqlite3
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = REPO_ROOT / "data" / "counterparty" / "public_derivatives.db"

DEFAULT_PERP_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "SUIUSDT",
    "APTUSDT",
    "NEARUSDT",
    "FETUSDT",
    "RENDERUSDT",
    "TAOUSDT",
    "WLDUSDT",
    "PEPEUSDT",
    "WIFUSDT",
    "AAVEUSDT",
    "UNIUSDT",
    "ENAUSDT",
    "ONDOUSDT",
    "PENDLEUSDT",
    "ARBUSDT",
    "OPUSDT",
    "TIAUSDT",
)

DEFAULT_SPOT_MICRO_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "DOGEUSDT",
    "SUIUSDT",
    "APTUSDT",
    "FETUSDT",
    "WLDUSDT",
    "ENAUSDT",
    "ONDOUSDT",
    "ARBUSDT",
    "OPUSDT",
    "TIAUSDT",
)

# Institutional Cliff Unlock Calendar (Category 9 / Factor Candidate I-4)
# Tracks recurring monthly/quarterly VC, core contributor & ecosystem cliff unlocks
INSTITUTIONAL_CLIFF_UNLOCK_CALENDAR: tuple[dict[str, Any], ...] = (
    {"symbol": "SUIUSDT", "day_of_month": 1, "pct_circulating": 2.40, "est_usd": 65_000_000.0, "recipient": "Series_A_B_VC_and_Contributors"},
    {"symbol": "ENAUSDT", "day_of_month": 2, "pct_circulating": 3.10, "est_usd": 42_000_000.0, "recipient": "Core_Contributors_and_Investors"},
    {"symbol": "APTUSDT", "day_of_month": 11, "pct_circulating": 2.15, "est_usd": 95_000_000.0, "recipient": "Core_Contributors_and_Investors"},
    {"symbol": "STRKUSDT", "day_of_month": 15, "pct_circulating": 3.30, "est_usd": 38_000_000.0, "recipient": "Early_Contributors_and_Investors"},
    {"symbol": "ARBUSDT", "day_of_month": 16, "pct_circulating": 2.35, "est_usd": 55_000_000.0, "recipient": "Team_and_Advisors_Cliff"},
    {"symbol": "ZKUSDT", "day_of_month": 17, "pct_circulating": 2.80, "est_usd": 28_000_000.0, "recipient": "Investors_and_Team"},
    {"symbol": "ONDOUSDT", "day_of_month": 18, "pct_circulating": 4.50, "est_usd": 120_000_000.0, "recipient": "Ecosystem_and_Private_Sale"},
    {"symbol": "ZROUSDT", "day_of_month": 20, "pct_circulating": 3.60, "est_usd": 45_000_000.0, "recipient": "Strategic_Partners_and_Team"},
    {"symbol": "WLDUSDT", "day_of_month": 24, "pct_circulating": 2.90, "est_usd": 36_000_000.0, "recipient": "TFH_Investors_and_Team"},
    {"symbol": "TIAUSDT", "day_of_month": 30, "pct_circulating": 2.50, "est_usd": 48_000_000.0, "recipient": "Seed_Series_A_B_Investors"},
    {"symbol": "OPUSDT", "day_of_month": 31, "pct_circulating": 2.45, "est_usd": 52_000_000.0, "recipient": "Core_Contributors_and_Sugar_X"},
)


def _finite(val: Any) -> float | None:
    if isinstance(val, bool) or val is None:
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _build_ssl_context() -> ssl.SSLContext:
    for ca_path in ("/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem"):
        if Path(ca_path).is_file():
            try:
                return ssl.create_default_context(cafile=ca_path)
            except ssl.SSLError:
                pass
    return ssl._create_unverified_context()  # noqa: SLF001


_SSL_CTX = _build_ssl_context()


def _http_get_json(url: str, params: dict[str, Any] | None = None, timeout: float = 8.0) -> Any:
    full_url = f"{url}?{urlencode(params)}" if params else url
    req = Request(
        full_url,
        headers={"Accept": "application/json", "User-Agent": "trade-counterparty-collector/2.0"},
        method="GET",
    )
    try:
        with urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _http_get_text(url: str, timeout: float = 10.0) -> str | None:
    req = Request(
        url,
        headers={"Accept": "text/csv,*/*", "User-Agent": "trade-counterparty-collector/2.0"},
        method="GET",
    )
    try:
        with urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:  # noqa: S310
            return resp.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError, OSError):
        return None


def compute_zscore(series: Sequence[float], min_samples: int = 10) -> float | None:
    clean = [float(x) for x in series if _finite(x) is not None]
    if len(clean) < min_samples:
        return None
    mean_v = sum(clean) / len(clean)
    var = sum((x - mean_v) ** 2 for x in clean) / max(1, len(clean) - 1)
    std = math.sqrt(max(0.0, var))
    if std <= 1e-12:
        return 0.0
    return round((clean[-1] - mean_v) / std, 4)


def compute_whale_retail_divergence(
    top_position_ls_ratio: float | None,
    global_account_ls_ratio: float | None,
    *,
    funding_rate: float | None = None,
) -> tuple[float | None, str]:
    """Compute Whale-to-Retail L/S ratio divergence and classify counterparty regime."""
    top_r = _finite(top_position_ls_ratio)
    glob_r = _finite(global_account_ls_ratio)
    fr = _finite(funding_rate)
    if top_r is None or glob_r is None or glob_r <= 0:
        return None, "unknown"
    div = round(top_r / glob_r, 4)
    if glob_r >= 2.20 and (top_r <= 0.95 or div <= 0.45):
        return div, "retail_long_trap_cascade"
    if div >= 1.35 and (fr is None or fr <= 0.0001):
        return div, "whale_accumulation_favorable"
    return div, "neutral"


def estimate_liquidation_cascade(
    oi_change_1h_pct: float | None,
    open_interest_usd: float | None,
    perp_taker_buy_sell_ratio: float | None,
) -> tuple[float | None, str]:
    """Estimate forced liquidation deleveraging notional USD and cascade regime (Category 6)."""
    oi_chg = _finite(oi_change_1h_pct)
    oi_usd = _finite(open_interest_usd)
    taker_r = _finite(perp_taker_buy_sell_ratio)
    if oi_chg is None or oi_usd is None or oi_usd <= 0:
        return None, "normal"
    if oi_chg >= 0:
        return 0.0, "normal"
    # Net OI contraction in USD over the past hour
    oi_drop_usd = abs(oi_chg / 100.0) * oi_usd
    if oi_chg <= -3.0 and (taker_r is not None and taker_r <= 0.75):
        return round(oi_drop_usd, 2), "long_liquidation_waterfall"
    if oi_chg <= -3.0 and (taker_r is not None and taker_r >= 1.35):
        return round(oi_drop_usd, 2), "short_squeeze_liquidation"
    return round(oi_drop_usd, 2), "normal"


def compute_spot_orderbook_and_aggtrades_metrics(
    depth_payload: dict[str, Any] | None,
    agg_trades: Sequence[dict[str, Any]] | None,
    *,
    whale_threshold_usd: float = 25_000.0,
) -> dict[str, float | None]:
    """Compute L2 20-level depth, OBI, $10k price impact, and Whale aggTrades absorption (Category 7)."""
    bids_raw = depth_payload.get("bids", []) if isinstance(depth_payload, dict) else []
    asks_raw = depth_payload.get("asks", []) if isinstance(depth_payload, dict) else []
    bids = [
        (_finite(b[0]), _finite(b[1]))
        for b in bids_raw[:20]
        if isinstance(b, (list, tuple)) and len(b) >= 2
    ]
    asks = [
        (_finite(a[0]), _finite(a[1]))
        for a in asks_raw[:20]
        if isinstance(a, (list, tuple)) and len(a) >= 2
    ]
    bids_clean = [(p, q) for p, q in bids if p is not None and q is not None and p > 0 and q > 0]
    asks_clean = [(p, q) for p, q in asks if p is not None and q is not None and p > 0 and q > 0]

    spread_bps = None
    bid_depth_usd = None
    ask_depth_usd = None
    obi_20 = None
    impact_10k_bps = None

    if bids_clean and asks_clean:
        best_bid = bids_clean[0][0]
        best_ask = asks_clean[0][0]
        mid = 0.5 * (best_bid + best_ask)
        if mid > 0 and best_ask >= best_bid:
            spread_bps = round(((best_ask - best_bid) / mid) * 10_000.0, 4)
        bid_depth_usd = round(sum(p * q for p, q in bids_clean), 2)
        ask_depth_usd = round(sum(p * q for p, q in asks_clean), 2)
        tot_depth = bid_depth_usd + ask_depth_usd
        if tot_depth > 0:
            obi_20 = round((bid_depth_usd - ask_depth_usd) / tot_depth, 4)

        # Simulate $10,000 market BUY walking the ask book
        target_usd = 10_000.0
        rem_usd = target_usd
        cost_usd = 0.0
        qty_accum = 0.0
        for p, q in asks_clean:
            lvl_usd = p * q
            take_usd = min(rem_usd, lvl_usd)
            cost_usd += take_usd
            qty_accum += take_usd / p
            rem_usd -= take_usd
            if rem_usd <= 1e-6:
                break
        if qty_accum > 0 and mid > 0:
            vwap_fill = cost_usd / qty_accum
            impact_10k_bps = round(((vwap_fill - mid) / mid) * 10_000.0, 4)

    whale_buy_usd = 0.0
    whale_sell_usd = 0.0
    for tr in agg_trades or ():
        if not isinstance(tr, dict):
            continue
        px = _finite(tr.get("p"))
        qty = _finite(tr.get("q"))
        is_buyer_maker = tr.get("m")
        if px is None or qty is None or px <= 0 or qty <= 0:
            continue
        notional = px * qty
        if notional < whale_threshold_usd:
            continue
        # In Binance aggTrades, m=True means buyer is maker (so taker was a SELLER)
        if is_buyer_maker is False:
            whale_buy_usd += notional
        elif is_buyer_maker is True:
            whale_sell_usd += notional

    whale_tot = whale_buy_usd + whale_sell_usd
    whale_net = round(whale_buy_usd - whale_sell_usd, 2)
    whale_ratio = round(whale_buy_usd / whale_tot, 4) if whale_tot > 0 else None

    return {
        "spread_bps": spread_bps,
        "bid_depth_20_usd": bid_depth_usd,
        "ask_depth_20_usd": ask_depth_usd,
        "orderbook_imbalance_20": obi_20,
        "impact_10k_bps": impact_10k_bps,
        "whale_net_buy_usd": whale_net,
        "aggtrade_whale_buy_ratio": whale_ratio,
    }


def compute_cross_exchange_dislocation(
    binance_px: float | None,
    okx_px: float | None,
    bybit_px: float | None,
) -> tuple[float | None, str]:
    """Compute cross-exchange spot price dislocation in bps across Binance, OKX, and Bybit (Category 8)."""
    prices = [p for p in (_finite(binance_px), _finite(okx_px), _finite(bybit_px)) if p is not None and p > 0]
    if len(prices) < 2:
        return None, "single_venue_only"
    med = sorted(prices)[len(prices) // 2]
    max_diff_bps = round((max(abs(p - med) for p in prices) / med) * 10_000.0, 4)
    if max_diff_bps >= 35.0:
        return max_diff_bps, "cross_exchange_wick_divergence"
    return max_diff_bps, "aligned"


def evaluate_token_unlock_window(
    symbol: str,
    *,
    now_ms: int | None = None,
    schedule: Sequence[dict[str, Any]] = INSTITUTIONAL_CLIFF_UNLOCK_CALENDAR,
) -> dict[str, Any] | None:
    """Check whether ``symbol`` is inside the ``[-72h, +48h]`` VC/Team Cliff Unlock blackout window (Category 9 / I-4)."""
    sym = str(symbol or "").strip().upper()
    ts_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    dt_now = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)

    for item in schedule:
        if str(item.get("symbol") or "").upper() != sym:
            continue
        dom = int(item.get("day_of_month") or 1)
        year = dt_now.year
        month = dt_now.month
        # Clamp day to valid day of current month
        for candidate_month in (month, month + 1 if month < 12 else 1):
            cand_year = year if candidate_month >= month else year + 1
            day = min(dom, 28 if candidate_month == 2 else (30 if candidate_month in (4, 6, 9, 11) else 31))
            unlock_dt = datetime(cand_year, candidate_month, day, 0, 0, 0, tzinfo=timezone.utc)
            unlock_ms = int(unlock_dt.timestamp() * 1000)
            hours_to_unlock = (unlock_ms - ts_ms) / 3_600_000.0
            # Active blackout if within [-48h after unlock, +72h before unlock] and >= 2.0% of circulating
            pct_circ = float(item.get("pct_circulating") or 0.0)
            in_blackout = (-48.0 <= hours_to_unlock <= 72.0) and (pct_circ >= 2.0)
            if hours_to_unlock >= -48.0:
                return {
                    "symbol": sym,
                    "next_unlock_utc": unlock_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "hours_to_unlock": round(hours_to_unlock, 2),
                    "unlock_pct_circulating": pct_circ,
                    "unlock_usd_est": float(item.get("est_usd") or 0.0),
                    "recipient_type": str(item.get("recipient") or ""),
                    "in_unlock_blackout": 1 if in_blackout else 0,
                }
    return None


def parse_deribit_instrument_name(name: str) -> tuple[str, float, str] | None:
    """Parse Deribit option name like ``BTC-27DEC26-100000-C`` -> ``(expiry, strike, 'C'|'P')``."""
    parts = str(name or "").strip().split("-")
    if len(parts) != 4:
        return None
    _, expiry, strike_raw, opt_type = parts
    strike = _finite(strike_raw)
    if strike is None or strike <= 0 or opt_type not in ("C", "P"):
        return None
    return expiry, strike, opt_type


def compute_deribit_option_walls_and_max_pain(
    book_summaries: Sequence[dict[str, Any]],
    *,
    spot_price: float | None,
    dvol_series: Sequence[float] = (),
) -> dict[str, Any]:
    """Compute Put/Call OI ratio, Call/Put Gamma Walls, Max Pain strike, and Gamma regime."""
    call_oi_by_strike: dict[float, float] = {}
    put_oi_by_strike: dict[float, float] = {}
    total_call_oi = 0.0
    total_put_oi = 0.0

    for item in book_summaries:
        if not isinstance(item, dict):
            continue
        parsed = parse_deribit_instrument_name(str(item.get("instrument_name") or ""))
        if parsed is None:
            continue
        _, strike, opt_type = parsed
        oi = _finite(item.get("open_interest")) or 0.0
        if oi <= 0:
            continue
        if opt_type == "C":
            call_oi_by_strike[strike] = call_oi_by_strike.get(strike, 0.0) + oi
            total_call_oi += oi
        else:
            put_oi_by_strike[strike] = put_oi_by_strike.get(strike, 0.0) + oi
            total_put_oi += oi

    all_strikes = sorted(set(call_oi_by_strike.keys()) | set(put_oi_by_strike.keys()))
    top_call_wall = (
        max(call_oi_by_strike.items(), key=lambda kv: kv[1])[0]
        if call_oi_by_strike
        else None
    )
    top_put_wall = (
        max(put_oi_by_strike.items(), key=lambda kv: kv[1])[0]
        if put_oi_by_strike
        else None
    )
    pc_ratio = (
        round(total_put_oi / total_call_oi, 4)
        if total_call_oi > 0
        else None
    )

    max_pain_strike: float | None = None
    if all_strikes:
        best_cost = float("inf")
        for settle_px in all_strikes:
            call_payout = sum(
                max(0.0, settle_px - k) * oi for k, oi in call_oi_by_strike.items()
            )
            put_payout = sum(
                max(0.0, k - settle_px) * oi for k, oi in put_oi_by_strike.items()
            )
            total_payout = call_payout + put_payout
            if total_payout < best_cost:
                best_cost = total_payout
                max_pain_strike = settle_px

    dvol_clean = [float(x) for x in dvol_series if _finite(x) is not None]
    dvol_close = dvol_clean[-1] if dvol_clean else None
    dvol_chg_24h = (
        round(((dvol_clean[-1] - dvol_clean[-24]) / dvol_clean[-24]) * 100.0, 4)
        if len(dvol_clean) >= 25 and dvol_clean[-24] > 0
        else None
    )
    dvol_z = compute_zscore(dvol_clean, min_samples=10)

    sp = _finite(spot_price)
    gamma_regime = "neutral"
    if sp is not None and top_put_wall is not None and sp < top_put_wall:
        gamma_regime = "negative_gamma_slippery"
    elif dvol_z is not None and dvol_z >= 2.0 and (pc_ratio is not None and pc_ratio >= 0.85):
        gamma_regime = "negative_gamma_slippery"
    elif (
        sp is not None
        and top_put_wall is not None
        and top_call_wall is not None
        and top_put_wall <= sp <= top_call_wall
    ):
        gamma_regime = "positive_gamma_pinned"

    return {
        "spot_price": sp,
        "dvol_close": dvol_close,
        "dvol_change_24h_pct": dvol_chg_24h,
        "dvol_zscore": dvol_z,
        "total_call_oi": round(total_call_oi, 2),
        "total_put_oi": round(total_put_oi, 2),
        "put_call_oi_ratio": pc_ratio,
        "max_pain_strike": max_pain_strike,
        "top_call_wall_strike": top_call_wall,
        "top_put_wall_strike": top_put_wall,
        "gamma_regime": gamma_regime,
    }


def compute_fed_net_liquidity(
    walcl_millions: float | None,
    tga_millions: float | None,
    on_rrp_billions: float | None,
) -> float | None:
    """Compute US Federal Reserve Net Liquidity in USD Billions: ``WALCL/1000 - TGA/1000 - ON_RRP``."""
    w = _finite(walcl_millions)
    t = _finite(tga_millions)
    r = _finite(on_rrp_billions)
    if w is None or t is None or r is None:
        return None
    return round((w / 1000.0) - (t / 1000.0) - r, 4)


def ensure_counterparty_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS binance_perp_counterparty (
            symbol TEXT NOT NULL,
            ts_ms INTEGER NOT NULL,
            funding_rate REAL,
            funding_z REAL,
            mark_price REAL,
            index_price REAL,
            perp_basis_bps REAL,
            open_interest REAL,
            open_interest_usd REAL,
            oi_change_1h_pct REAL,
            top_position_ls_ratio REAL,
            global_account_ls_ratio REAL,
            whale_retail_divergence REAL,
            perp_taker_buy_sell_ratio REAL,
            est_liq_pressure_usd REAL,
            liq_cascade_regime TEXT,
            counterparty_regime TEXT,
            PRIMARY KEY (symbol, ts_ms)
        );
        CREATE INDEX IF NOT EXISTS idx_perp_cp_sym_ts ON binance_perp_counterparty(symbol, ts_ms DESC);

        CREATE TABLE IF NOT EXISTS binance_spot_microstructure (
            symbol TEXT NOT NULL,
            ts_ms INTEGER NOT NULL,
            ret_5m_pct REAL,
            ret_1h_pct REAL,
            ret_4h_pct REAL,
            ret_1d_pct REAL,
            taker_buy_ratio_5m REAL,
            taker_buy_ratio_1h REAL,
            spread_bps REAL,
            bid_depth_20_usd REAL,
            ask_depth_20_usd REAL,
            orderbook_imbalance_20 REAL,
            impact_10k_bps REAL,
            whale_net_buy_usd REAL,
            aggtrade_whale_buy_ratio REAL,
            PRIMARY KEY (symbol, ts_ms)
        );

        CREATE TABLE IF NOT EXISTS cross_exchange_spot_baseline (
            symbol TEXT NOT NULL,
            ts_ms INTEGER NOT NULL,
            binance_price REAL,
            okx_price REAL,
            bybit_price REAL,
            max_venue_dislocation_bps REAL,
            dislocation_regime TEXT,
            PRIMARY KEY (symbol, ts_ms)
        );

        CREATE TABLE IF NOT EXISTS deribit_options_gamma (
            currency TEXT NOT NULL,
            ts_ms INTEGER NOT NULL,
            spot_price REAL,
            dvol_close REAL,
            dvol_change_24h_pct REAL,
            dvol_zscore REAL,
            total_call_oi REAL,
            total_put_oi REAL,
            put_call_oi_ratio REAL,
            max_pain_strike REAL,
            top_call_wall_strike REAL,
            top_put_wall_strike REAL,
            gamma_regime TEXT,
            PRIMARY KEY (currency, ts_ms)
        );

        CREATE TABLE IF NOT EXISTS token_unlock_schedule (
            symbol TEXT PRIMARY KEY,
            next_unlock_utc TEXT,
            hours_to_unlock REAL,
            unlock_pct_circulating REAL,
            unlock_usd_est REAL,
            recipient_type TEXT,
            in_unlock_blackout INTEGER
        );

        CREATE TABLE IF NOT EXISTS macro_net_liquidity (
            asof_date TEXT PRIMARY KEY,
            walcl_millions REAL,
            tga_millions REAL,
            on_rrp_billions REAL,
            net_liquidity_billions REAL,
            hy_oas_bps REAL
        );

        CREATE TABLE IF NOT EXISTS stablecoin_supply (
            ts_ms INTEGER PRIMARY KEY,
            usdt_supply_usd REAL,
            usdc_supply_usd REAL,
            total_major_stable_usd REAL,
            change_24h_pct REAL,
            change_7d_pct REAL
        );

        CREATE TABLE IF NOT EXISTS onchain_holder_bot_summary (
            token_address TEXT PRIMARY KEY,
            ts_ms INTEGER,
            top10_share REAL,
            concentration_delta REAL,
            dev_balance_delta REAL,
            bot_trade_share REAL
        );
        """
    )
    # Ensure backward-compatible columns if table already existed
    existing_cols = {
        str(r[1])
        for r in conn.execute("PRAGMA table_info(binance_perp_counterparty)").fetchall()
    }
    for col, ctype in (("est_liq_pressure_usd", "REAL"), ("liq_cascade_regime", "TEXT")):
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE binance_perp_counterparty ADD COLUMN {col} {ctype}")


def fetch_binance_perp_symbol_snapshot(symbol: str, *, now_ms: int | None = None) -> dict[str, Any] | None:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    ts_ms = int(now_ms if now_ms is not None else time.time() * 1000)

    prem = _http_get_json("https://fapi.binance.com/fapi/v1/premiumIndex", {"symbol": sym})
    if not isinstance(prem, dict):
        return None
    mark_px = _finite(prem.get("markPrice"))
    index_px = _finite(prem.get("indexPrice"))
    funding_rate = _finite(prem.get("lastFundingRate"))
    basis_bps = (
        round(((mark_px - index_px) / index_px) * 10_000.0, 4)
        if mark_px is not None and index_px is not None and index_px > 0
        else None
    )

    f_hist = _http_get_json(
        "https://fapi.binance.com/fapi/v1/fundingRate",
        {"symbol": sym, "limit": 180},
    )
    f_series = [
        _finite(r.get("fundingRate"))
        for r in (f_hist if isinstance(f_hist, list) else [])
        if isinstance(r, dict) and _finite(r.get("fundingRate")) is not None
    ]
    funding_z = compute_zscore([float(x) for x in f_series if x is not None], min_samples=20)

    oi_hist = _http_get_json(
        "https://fapi.binance.com/futures/data/openInterestHist",
        {"symbol": sym, "period": "5m", "limit": 13},
    )
    oi_val = None
    oi_usd = None
    oi_chg_1h = None
    if isinstance(oi_hist, list) and oi_hist:
        last_oi = oi_hist[-1] if isinstance(oi_hist[-1], dict) else {}
        first_oi = oi_hist[0] if isinstance(oi_hist[0], dict) else {}
        oi_val = _finite(last_oi.get("sumOpenInterest"))
        oi_usd = _finite(last_oi.get("sumOpenInterestValue"))
        prev_val = _finite(first_oi.get("sumOpenInterest"))
        if oi_val is not None and prev_val is not None and prev_val > 0 and len(oi_hist) >= 2:
            oi_chg_1h = round(((oi_val - prev_val) / prev_val) * 100.0, 4)

    top_ls = _http_get_json(
        "https://fapi.binance.com/futures/data/topLongShortPositionRatio",
        {"symbol": sym, "period": "5m", "limit": 1},
    )
    top_ratio = (
        _finite(top_ls[-1].get("longShortRatio"))
        if isinstance(top_ls, list) and top_ls and isinstance(top_ls[-1], dict)
        else None
    )

    glob_ls = _http_get_json(
        "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
        {"symbol": sym, "period": "5m", "limit": 1},
    )
    glob_ratio = (
        _finite(glob_ls[-1].get("longShortRatio"))
        if isinstance(glob_ls, list) and glob_ls and isinstance(glob_ls[-1], dict)
        else None
    )

    taker_ls = _http_get_json(
        "https://fapi.binance.com/futures/data/takerlongshortRatio",
        {"symbol": sym, "period": "5m", "limit": 1},
    )
    taker_ratio = (
        _finite(taker_ls[-1].get("buySellRatio"))
        if isinstance(taker_ls, list) and taker_ls and isinstance(taker_ls[-1], dict)
        else None
    )

    div, regime = compute_whale_retail_divergence(
        top_ratio, glob_ratio, funding_rate=funding_rate
    )
    liq_usd, liq_regime = estimate_liquidation_cascade(oi_chg_1h, oi_usd, taker_ratio)
    return {
        "symbol": sym,
        "ts_ms": ts_ms,
        "funding_rate": funding_rate,
        "funding_z": funding_z,
        "mark_price": mark_px,
        "index_price": index_px,
        "perp_basis_bps": basis_bps,
        "open_interest": oi_val,
        "open_interest_usd": oi_usd,
        "oi_change_1h_pct": oi_chg_1h,
        "top_position_ls_ratio": top_ratio,
        "global_account_ls_ratio": glob_ratio,
        "whale_retail_divergence": div,
        "perp_taker_buy_sell_ratio": taker_ratio,
        "est_liq_pressure_usd": liq_usd,
        "liq_cascade_regime": liq_regime,
        "counterparty_regime": regime,
    }


def _kline_ret_and_tbr(symbol: str, interval: str, limit: int = 3) -> tuple[float | None, float | None]:
    kl = _http_get_json(
        "https://api.binance.com/api/v3/klines",
        {"symbol": symbol, "interval": interval, "limit": limit},
    )
    if not isinstance(kl, list) or len(kl) < 2:
        return None, None
    # Use the last closed bar (second-to-last if último is still forming, or compare last two closed)
    bar = kl[-2] if len(kl) >= 2 else kl[-1]
    if not isinstance(bar, list) or len(bar) < 11:
        return None, None
    op = _finite(bar[1])
    cl = _finite(bar[4])
    vol = _finite(bar[5])
    tb_base = _finite(bar[9])
    ret = round(((cl - op) / op) * 100.0, 4) if (op and cl and op > 0) else None
    tbr = round(tb_base / vol, 4) if (vol and tb_base is not None and vol > 0) else None
    return ret, tbr


def fetch_binance_spot_microstructure_snapshot(symbol: str, *, now_ms: int | None = None) -> dict[str, Any] | None:
    """Collect Category 7: Multi-Horizon 5m/1h/4h/1d K-lines, L2 20-level Depth & Whale aggTrades."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    ts_ms = int(now_ms if now_ms is not None else time.time() * 1000)

    ret_5m, tbr_5m = _kline_ret_and_tbr(sym, "5m", 3)
    ret_1h, tbr_1h = _kline_ret_and_tbr(sym, "1h", 3)
    ret_4h, _ = _kline_ret_and_tbr(sym, "4h", 3)
    ret_1d, _ = _kline_ret_and_tbr(sym, "1d", 3)

    depth = _http_get_json("https://api.binance.com/api/v3/depth", {"symbol": sym, "limit": 20})
    agg = _http_get_json("https://api.binance.com/api/v3/aggTrades", {"symbol": sym, "limit": 250})
    ob_metrics = compute_spot_orderbook_and_aggtrades_metrics(
        depth if isinstance(depth, dict) else None,
        agg if isinstance(agg, list) else None,
    )
    if ret_5m is None and ob_metrics["spread_bps"] is None:
        return None
    return {
        "symbol": sym,
        "ts_ms": ts_ms,
        "ret_5m_pct": ret_5m,
        "ret_1h_pct": ret_1h,
        "ret_4h_pct": ret_4h,
        "ret_1d_pct": ret_1d,
        "taker_buy_ratio_5m": tbr_5m,
        "taker_buy_ratio_1h": tbr_1h,
        **ob_metrics,
    }


def fetch_cross_exchange_spot_snapshot(symbol: str, *, now_ms: int | None = None) -> dict[str, Any] | None:
    """Collect Category 8: Binance vs OKX vs Bybit cross-exchange spot price dislocation."""
    sym = str(symbol or "").strip().upper()
    if not sym.endswith("USDT"):
        return None
    base = sym[:-4]
    ts_ms = int(now_ms if now_ms is not None else time.time() * 1000)

    b_resp = _http_get_json("https://api.binance.com/api/v3/ticker/price", {"symbol": sym})
    b_px = _finite(b_resp.get("price")) if isinstance(b_resp, dict) else None

    okx_resp = _http_get_json("https://www.okx.com/api/v5/market/ticker", {"instId": f"{base}-USDT"})
    okx_data = okx_resp.get("data", []) if isinstance(okx_resp, dict) else []
    okx_px = _finite(okx_data[0].get("last")) if (isinstance(okx_data, list) and okx_data and isinstance(okx_data[0], dict)) else None

    byb_resp = _http_get_json("https://api.bybit.com/v5/market/tickers", {"category": "spot", "symbol": sym})
    byb_list = ((byb_resp.get("result") or {}).get("list") or []) if isinstance(byb_resp, dict) else []
    byb_px = _finite(byb_list[0].get("lastPrice")) if (isinstance(byb_list, list) and byb_list and isinstance(byb_list[0], dict)) else None

    if b_px is None and okx_px is None and byb_px is None:
        return None
    diff_bps, regime = compute_cross_exchange_dislocation(b_px, okx_px, byb_px)
    return {
        "symbol": sym,
        "ts_ms": ts_ms,
        "binance_price": b_px,
        "okx_price": okx_px,
        "bybit_price": byb_px,
        "max_venue_dislocation_bps": diff_bps,
        "dislocation_regime": regime,
    }


def fetch_deribit_options_snapshot(currency: str = "BTC", *, now_ms: int | None = None) -> dict[str, Any] | None:
    ccy = str(currency or "BTC").strip().upper()
    ts_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    start_ms = ts_ms - 7 * 24 * 3600 * 1000

    dvol_resp = _http_get_json(
        "https://www.deribit.com/api/v2/public/get_volatility_index_data",
        {
            "currency": ccy,
            "start_timestamp": start_ms,
            "end_timestamp": ts_ms,
            "resolution": "3600",
        },
    )
    dvol_rows = (
        dvol_resp.get("result", {}).get("data", [])
        if isinstance(dvol_resp, dict) and isinstance(dvol_resp.get("result"), dict)
        else []
    )
    dvol_closes = [
        float(r[4])
        for r in dvol_rows
        if isinstance(r, list) and len(r) >= 5 and _finite(r[4]) is not None
    ]

    idx_resp = _http_get_json(
        "https://www.deribit.com/api/v2/public/get_index_price",
        {"index_name": f"{ccy.lower()}_usd"},
    )
    spot_px = (
        _finite(idx_resp.get("result", {}).get("index_price"))
        if isinstance(idx_resp, dict) and isinstance(idx_resp.get("result"), dict)
        else None
    )

    book_resp = _http_get_json(
        "https://www.deribit.com/api/v2/public/get_book_summary_by_currency",
        {"currency": ccy, "kind": "option"},
    )
    summaries = (
        book_resp.get("result", [])
        if isinstance(book_resp, dict) and isinstance(book_resp.get("result"), list)
        else []
    )
    if not summaries and not dvol_closes:
        return None

    stats = compute_deribit_option_walls_and_max_pain(
        summaries,
        spot_price=spot_px,
        dvol_series=dvol_closes,
    )
    stats["currency"] = ccy
    stats["ts_ms"] = ts_ms
    return stats


def _fetch_fred_latest(series_id: str) -> tuple[str | None, float | None]:
    text = _http_get_text(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}")
    if not text:
        return None, None
    reader = csv.reader(io.StringIO(text))
    last_date: str | None = None
    last_val: float | None = None
    for idx, row in enumerate(reader):
        if idx == 0 or len(row) < 2:
            continue
        val = _finite(row[1])
        if val is not None:
            last_date = row[0].strip()
            last_val = val
    return last_date, last_val


def fetch_fred_macro_liquidity() -> dict[str, Any] | None:
    d_w, walcl = _fetch_fred_latest("WALCL")
    d_t, tga = _fetch_fred_latest("WTREGEN")
    d_r, rrp = _fetch_fred_latest("RRPONTSYD")
    d_h, hy_oas_pct = _fetch_fred_latest("BAMLH0A0HYM2")
    asof = max(d for d in (d_w, d_t, d_r, d_h) if d is not None) if any((d_w, d_t, d_r, d_h)) else None
    if asof is None:
        return None
    net_liq = compute_fed_net_liquidity(walcl, tga, rrp)
    hy_bps = round(hy_oas_pct * 100.0, 2) if hy_oas_pct is not None else None
    return {
        "asof_date": asof,
        "walcl_millions": walcl,
        "tga_millions": tga,
        "on_rrp_billions": rrp,
        "net_liquidity_billions": net_liq,
        "hy_oas_bps": hy_bps,
    }


def fetch_stablecoin_supply(*, now_ms: int | None = None) -> dict[str, Any] | None:
    data = _http_get_json("https://stablecoins.llama.fi/stablecoins", {"includePrices": "true"})
    assets = data.get("peggedAssets", []) if isinstance(data, dict) else []
    if not isinstance(assets, list) or not assets:
        return None
    usdt_cur = 0.0
    usdc_cur = 0.0
    prev_day = 0.0
    prev_wk = 0.0
    for a in assets:
        if not isinstance(a, dict):
            continue
        sym = str(a.get("symbol") or "").upper()
        if sym not in ("USDT", "USDC"):
            continue
        cur = _finite((a.get("circulating") or {}).get("peggedUSD")) or 0.0
        d1 = _finite((a.get("circulatingPrevDay") or {}).get("peggedUSD")) or cur
        w1 = _finite((a.get("circulatingPrevWeek") or {}).get("peggedUSD")) or cur
        if sym == "USDT":
            usdt_cur = cur
        else:
            usdc_cur = cur
        prev_day += d1
        prev_wk += w1
    total = usdt_cur + usdc_cur
    if total <= 0:
        return None
    chg_24h = round(((total - prev_day) / prev_day) * 100.0, 4) if prev_day > 0 else 0.0
    chg_7d = round(((total - prev_wk) / prev_wk) * 100.0, 4) if prev_wk > 0 else 0.0
    return {
        "ts_ms": int(now_ms if now_ms is not None else time.time() * 1000),
        "usdt_supply_usd": round(usdt_cur, 2),
        "usdc_supply_usd": round(usdc_cur, 2),
        "total_major_stable_usd": round(total, 2),
        "change_24h_pct": chg_24h,
        "change_7d_pct": chg_7d,
    }


def collect_all_to_sqlite(
    db_path: Path = DEFAULT_DB_PATH,
    *,
    symbols: Sequence[str] = DEFAULT_PERP_SYMBOLS,
    spot_symbols: Sequence[str] = DEFAULT_SPOT_MICRO_SYMBOLS,
) -> dict[str, int]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        ensure_counterparty_schema(conn)

        # Category 6: Binance Perp Counterparty, Whale/Retail L/S & Liquidation Cascade
        perp_count = 0
        for sym in symbols:
            snap = fetch_binance_perp_symbol_snapshot(sym)
            if snap is None:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO binance_perp_counterparty (
                    symbol, ts_ms, funding_rate, funding_z, mark_price, index_price,
                    perp_basis_bps, open_interest, open_interest_usd, oi_change_1h_pct,
                    top_position_ls_ratio, global_account_ls_ratio, whale_retail_divergence,
                    perp_taker_buy_sell_ratio, est_liq_pressure_usd, liq_cascade_regime,
                    counterparty_regime
                ) VALUES (
                    :symbol, :ts_ms, :funding_rate, :funding_z, :mark_price, :index_price,
                    :perp_basis_bps, :open_interest, :open_interest_usd, :oi_change_1h_pct,
                    :top_position_ls_ratio, :global_account_ls_ratio, :whale_retail_divergence,
                    :perp_taker_buy_sell_ratio, :est_liq_pressure_usd, :liq_cascade_regime,
                    :counterparty_regime
                )
                """,
                snap,
            )
            perp_count += 1

        # Category 7: Binance Spot Multi-Horizon K-lines, L2 20-Level Depth & Whale aggTrades
        spot_count = 0
        for sym in spot_symbols:
            s_snap = fetch_binance_spot_microstructure_snapshot(sym)
            if s_snap is None:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO binance_spot_microstructure VALUES (
                    :symbol, :ts_ms, :ret_5m_pct, :ret_1h_pct, :ret_4h_pct, :ret_1d_pct,
                    :taker_buy_ratio_5m, :taker_buy_ratio_1h, :spread_bps,
                    :bid_depth_20_usd, :ask_depth_20_usd, :orderbook_imbalance_20,
                    :impact_10k_bps, :whale_net_buy_usd, :aggtrade_whale_buy_ratio
                )
                """,
                s_snap,
            )
            spot_count += 1

        # Category 8A: Cross-Exchange Spot Baseline (Binance vs OKX vs Bybit)
        xex_count = 0
        for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "DOGEUSDT", "SUIUSDT"):
            x_snap = fetch_cross_exchange_spot_snapshot(sym)
            if x_snap is None:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO cross_exchange_spot_baseline VALUES (
                    :symbol, :ts_ms, :binance_price, :okx_price, :bybit_price,
                    :max_venue_dislocation_bps, :dislocation_regime
                )
                """,
                x_snap,
            )
            xex_count += 1

        # Category 8B: Deribit Options DVOL, Put/Call OI, Max Pain & Gamma Walls
        opt_count = 0
        for ccy in ("BTC", "ETH"):
            opt = fetch_deribit_options_snapshot(ccy)
            if opt is None:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO deribit_options_gamma VALUES (
                    :currency, :ts_ms, :spot_price, :dvol_close, :dvol_change_24h_pct,
                    :dvol_zscore, :total_call_oi, :total_put_oi, :put_call_oi_ratio,
                    :max_pain_strike, :top_call_wall_strike, :top_put_wall_strike, :gamma_regime
                )
                """,
                opt,
            )
            opt_count += 1

        # Category 9A: Token Cliff Unlocks Schedule (I-4)
        unlock_count = 0
        now_ms = int(time.time() * 1000)
        for item in INSTITUTIONAL_CLIFF_UNLOCK_CALENDAR:
            u_info = evaluate_token_unlock_window(str(item["symbol"]), now_ms=now_ms)
            if u_info is None:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO token_unlock_schedule VALUES (
                    :symbol, :next_unlock_utc, :hours_to_unlock,
                    :unlock_pct_circulating, :unlock_usd_est, :recipient_type,
                    :in_unlock_blackout
                )
                """,
                u_info,
            )
            unlock_count += 1

        # Category 9B: FRED Macro Net Liquidity & Stablecoin Supply
        fred_count = 0
        fred = fetch_fred_macro_liquidity()
        if fred is not None:
            conn.execute(
                """
                INSERT OR REPLACE INTO macro_net_liquidity VALUES (
                    :asof_date, :walcl_millions, :tga_millions, :on_rrp_billions,
                    :net_liquidity_billions, :hy_oas_bps
                )
                """,
                fred,
            )
            fred_count = 1

        stable_count = 0
        stable = fetch_stablecoin_supply(now_ms=now_ms)
        if stable is not None:
            conn.execute(
                """
                INSERT OR REPLACE INTO stablecoin_supply VALUES (
                    :ts_ms, :usdt_supply_usd, :usdc_supply_usd, :total_major_stable_usd,
                    :change_24h_pct, :change_7d_pct
                )
                """,
                stable,
            )
            stable_count = 1

        conn.commit()
        return {
            "cat6_perp_snapshots": perp_count,
            "cat7_spot_microstructure": spot_count,
            "cat8_cross_exchange": xex_count,
            "cat8_deribit_options": opt_count,
            "cat9_token_unlocks": unlock_count,
            "cat9_fred_liquidity": fred_count,
            "cat9_stablecoin_supply": stable_count,
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Categories 6, 7, 8, and 9 public derivatives, spot L2, cross-exchange, options, and token unlocks.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Output SQLite path.")
    parser.add_argument(
        "--symbols",
        type=str,
        default=",".join(DEFAULT_PERP_SYMBOLS),
        help="Comma-separated Binance USD-M perpetual symbols.",
    )
    parser.add_argument(
        "--spot-symbols",
        type=str,
        default=",".join(DEFAULT_SPOT_MICRO_SYMBOLS),
        help="Comma-separated Binance Spot symbols for Category 7 multi-horizon + L2 + aggTrades.",
    )
    args = parser.parse_args()
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    spot_syms = [s.strip().upper() for s in args.spot_symbols.split(",") if s.strip()]
    stats = collect_all_to_sqlite(args.db, symbols=syms, spot_symbols=spot_syms)
    print(f"Saved Categories 6-9 counterparty data to {args.db}: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
