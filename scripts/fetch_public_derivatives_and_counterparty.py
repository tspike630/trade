#!/usr/bin/env python3
"""Public Derivatives, Options Gamma Wall & Macro Liquidity One-Click Collector.

Zero external dependencies (pure Python stdlib ``urllib.request``, ``sqlite3``,
``json``, ``csv``, ``math``). Collects the public counterparty datasets from
Categories 6, 7, 8, and 9 of the institutional data checklist:

1. **Binance USD-M Perpetual Counterparty & Whale-vs-Retail Positioning**:
   - ``GET /fapi/v1/fundingRate`` (60d 8h history -> ``funding_z``)
   - ``GET /fapi/v1/premiumIndex`` (``markPrice``, ``indexPrice`` -> ``perp_basis_bps``)
   - ``GET /futures/data/openInterestHist`` (``oi_change_1h_pct``)
   - ``GET /futures/data/topLongShortPositionRatio`` (Whale position L/S ratio)
   - ``GET /futures/data/globalLongShortAccountRatio`` (Retail account L/S ratio)
   - ``GET /futures/data/takerlongshortRatio`` (Perp taker buy/sell volume ratio)
   - Computes ``whale_retail_divergence = top_position_ls_ratio / global_account_ls_ratio``.

2. **Deribit Crypto Options DVOL, Put/Call OI, Max Pain & Dealer Gamma Walls**:
   - ``GET /api/v2/public/get_volatility_index_data`` (BTC & ETH ``DVOL`` + rolling z-score)
   - ``GET /api/v2/public/get_book_summary_by_currency`` (parses all active BTC/ETH options,
     computes ``max_pain_strike``, ``top_call_wall_strike``, ``top_put_wall_strike``,
     ``put_call_oi_ratio``, and ``gamma_regime``).

3. **FRED US Fed Net Liquidity (``WALCL - TGA - ON_RRP``) & High-Yield Credit OAS**:
   - Public keyless CSV endpoints from St. Louis Fed (``WALCL``, ``WTREGEN``,
     ``RRPONTSYD``, ``BAMLH0A0HYM2``).

4. **Global Stablecoin Circulating Supply & Mint/Burn Velocity (DefiLlama)**:
   - Public ``https://stablecoins.llama.fi/stablecoins`` for ``USDT`` and ``USDC``
     24h and 7d supply expansion/contraction.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sqlite3
import time
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


def _finite(val: Any) -> float | None:
    if isinstance(val, bool) or val is None:
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _http_get_json(url: str, params: dict[str, Any] | None = None, timeout: float = 8.0) -> Any:
    full_url = f"{url}?{urlencode(params)}" if params else url
    req = Request(
        full_url,
        headers={"Accept": "application/json", "User-Agent": "trade-counterparty-collector/1.0"},
        method="GET",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _http_get_text(url: str, timeout: float = 10.0) -> str | None:
    req = Request(
        url,
        headers={"Accept": "text/csv,*/*", "User-Agent": "trade-counterparty-collector/1.0"},
        method="GET",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310
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

    # Max Pain Strike: strike K that minimizes intrinsic value payout to option holders
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
            counterparty_regime TEXT,
            PRIMARY KEY (symbol, ts_ms)
        );
        CREATE INDEX IF NOT EXISTS idx_perp_cp_sym_ts ON binance_perp_counterparty(symbol, ts_ms DESC);

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
        """
    )


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
        "counterparty_regime": regime,
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
) -> dict[str, int]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        ensure_counterparty_schema(conn)
        perp_count = 0
        for sym in symbols:
            snap = fetch_binance_perp_symbol_snapshot(sym)
            if snap is None:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO binance_perp_counterparty VALUES (
                    :symbol, :ts_ms, :funding_rate, :funding_z, :mark_price, :index_price,
                    :perp_basis_bps, :open_interest, :open_interest_usd, :oi_change_1h_pct,
                    :top_position_ls_ratio, :global_account_ls_ratio, :whale_retail_divergence,
                    :perp_taker_buy_sell_ratio, :counterparty_regime
                )
                """,
                snap,
            )
            perp_count += 1

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
        stable = fetch_stablecoin_supply()
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
            "perp_snapshots": perp_count,
            "deribit_options": opt_count,
            "fred_liquidity": fred_count,
            "stablecoin_supply": stable_count,
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch public derivatives, options gamma, and macro liquidity.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Output SQLite path.")
    parser.add_argument(
        "--symbols",
        type=str,
        default=",".join(DEFAULT_PERP_SYMBOLS),
        help="Comma-separated Binance USD-M perpetual symbols.",
    )
    args = parser.parse_args()
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    stats = collect_all_to_sqlite(args.db, symbols=syms)
    print(f"Saved public counterparty data to {args.db}: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
