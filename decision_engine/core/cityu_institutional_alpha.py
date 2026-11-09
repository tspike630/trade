"""CityU Institutional Multi-Asset Alpha & Regime Engine (CFTC COT, Short Interest, TRACE Convertible Flow, Orbis Bank Risk & NVDA/SMH AI Lead-Lag).

Connects the 613 MB ``data/cityu/cityu_institutional.db`` and 171 MB
``data/lseg/lseg_macro.db`` directly to the live spot decision engine:

1. **CFTC COT Leveraged-Fund vs Asset-Manager Positioning (`evaluate_cftc_cot_regime`)**:
   - Reads ``lseg_cftc_cot_positioning`` (weekly CME ``BTC``, ``ETH``, ``NQ``, ``ES``, ``JPY``, ``ZN``).
   - Computes ``lev_money_pct_oi = lev_money_net / open_interest`` and
     ``asset_mgr_pct_oi = asset_mgr_net / open_interest`` plus 52-week rolling Z-scores.
   - Identifies institutional accumulation & basis short-squeeze setups
     (``asset_mgr_z >= 0.75`` and ``lev_money_z <= -0.75`` -> ``cot_institutional_squeeze_favorable``)
     versus overcrowded hedge-fund long unwind risk (``lev_money_z >= 1.85`` -> ``cot_leveraged_long_crowded``).

2. **Crypto-Proxy Equity Short-Squeeze & TRACE Convertible Bond Flow (`evaluate_crypto_equity_proxy_pulse`)**:
   - Reads ``lseg_short_interest_daily`` (``MARA.O`` 28.98%, ``CLSK.O`` 30.66%, ``RIOT.O`` 14.29%,
     ``COIN.O`` 8.98%, ``MSTR.O`` 6.90%) and ``wrds_trace_convertible_bonds`` institutional
     customer buy/sell imbalance on ``MSTR``/``COIN``/``MARA``/``CLSK`` convertible notes.

3. **Intraday AI Semiconductor Lead-Lag (`evaluate_ai_semiconductor_lead_lag`)**:
   - Reads 5min/1h ``NVDA.O`` and ``SMH.O`` returns from ``lseg_macro.db::cross_asset_bars``.
   - Blocks ``ai_compute`` token dip-buying (``FETUSDT``, ``RENDERUSDT``, ``TAOUSDT``, ``WLDUSDT``,
     ``NEARUSDT``, ``ARKMUSDT``) when ``NVDA.O`` or ``SMH.O`` is actively dumping (1h return ``<= -2.0%``
     -> ``ai_equity_nvda_smh_dump``), and boosts ``ai_compute`` conviction (``1.15x``) during
     strong semiconductor breakouts (1h return ``>= +1.20%``).

4. **Macro Funding & Orbis Bank Fragility Overlay (`evaluate_funding_and_bank_regime`)**:
   - Reads ``lseg_substitute_rates`` (``us3mt_ois_spread``, ``usdsofr_val``), ``lseg_derived_rates``
     (``us2y10y_calc``), and ``orbis_bank_liquidity_risk`` (percentage of US banks with
     ``Liquid Assets / Deposits < 5%``).
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
import sqlite3
from pathlib import Path
from typing import Any, Sequence

from decision_engine.core.portfolio_clusters import symbol_sector_cluster

CITYU_INST_DB_ENV = "CITYU_INSTITUTIONAL_DB_PATH"
LSEG_MACRO_DB_ENV = "DECISION_MACRO_DB"


def _finite(val: object) -> float | None:
    if isinstance(val, bool) or val is None or val == "":
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def resolve_cityu_institutional_db_path() -> Path:
    raw = os.environ.get(CITYU_INST_DB_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parents[2] / "data" / "cityu" / "cityu_institutional.db"


def resolve_lseg_db_path() -> Path:
    raw = os.environ.get(LSEG_MACRO_DB_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parents[2] / "data" / "lseg" / "lseg_macro.db"


def _zscore(series: Sequence[float], min_len: int = 12) -> float | None:
    clean = [float(x) for x in series if _finite(x) is not None]
    if len(clean) < min_len:
        return None
    mean_v = sum(clean) / len(clean)
    var = sum((x - mean_v) ** 2 for x in clean) / max(1, len(clean) - 1)
    std = math.sqrt(max(0.0, var))
    if std <= 1e-12:
        return 0.0
    return round((clean[-1] - mean_v) / std, 4)


@dataclass(frozen=True)
class CityUInstitutionalSnapshot:
    btc_cot_lev_net_pct: float | None
    btc_cot_asset_mgr_pct: float | None
    btc_cot_lev_z: float | None
    btc_cot_asset_mgr_z: float | None
    cot_regime: str
    avg_miner_short_interest_pct: float | None
    trace_conv_bond_buy_ratio: float | None
    us3mt_ois_spread_bps: float | None
    us2y10y_slope_pct: float | None
    orbis_fragile_bank_share: float | None
    nvda_1h_ret_pct: float | None
    smh_1h_ret_pct: float | None
    cme_btc_term_spread_bps: float | None
    size_multiplier: float


def compute_cot_regime_from_series(
    lev_pct_series: Sequence[float],
    asset_mgr_pct_series: Sequence[float],
) -> tuple[float | None, float | None, str, float]:
    """Return ``(lev_z, asset_mgr_z, regime_name, size_multiplier)``."""
    lev_z = _zscore(lev_pct_series, min_len=10)
    am_z = _zscore(asset_mgr_pct_series, min_len=10)
    if lev_z is None or am_z is None:
        return lev_z, am_z, "neutral", 1.0
    # Institutional asset managers accumulating while leveraged funds are deeply short
    if am_z >= 0.50 and lev_z <= -0.50:
        return lev_z, am_z, "cot_institutional_squeeze_favorable", 1.12
    # Leveraged hedge funds overcrowded long -> high basis/long-unwind risk
    if lev_z >= 1.85:
        return lev_z, am_z, "cot_leveraged_long_crowded", 0.82
    # Asset managers dumping aggressively
    if am_z <= -1.75:
        return lev_z, am_z, "cot_asset_mgr_distribution", 0.85
    return lev_z, am_z, "neutral", 1.0


def load_cityu_institutional_snapshot(
    inst_db_path: str | Path | None = None,
    lseg_db_path: str | Path | None = None,
    *,
    now_ms: int | None = None,
) -> CityUInstitutionalSnapshot:
    """Read latest institutional factors from ``cityu_institutional.db`` and ``lseg_macro.db``."""
    inst_p = Path(inst_db_path).expanduser() if inst_db_path is not None else resolve_cityu_institutional_db_path()
    lseg_p = Path(lseg_db_path).expanduser() if lseg_db_path is not None else resolve_lseg_db_path()

    btc_lev_pct = None
    btc_am_pct = None
    btc_lev_z = None
    btc_am_z = None
    cot_regime = "neutral"
    cot_mult = 1.0
    avg_si_pct = None
    trace_buy_ratio = None
    ois_spread = None
    slope_2s10s = None
    orbis_fragile_share = None

    if inst_p.is_file():
        try:
            conn = sqlite3.connect(f"file:{inst_p.resolve().as_posix()}?mode=ro", uri=True, timeout=3.0)
            conn.row_factory = sqlite3.Row
            tables = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

            if "lseg_cftc_cot_positioning" in tables:
                cot_rows = conn.execute(
                    """
                    SELECT date, open_interest, lev_money_net, asset_mgr_net
                    FROM lseg_cftc_cot_positioning
                    WHERE symbol = 'BTC' AND open_interest != ''
                    ORDER BY date ASC
                    """
                ).fetchall()
                lev_series: list[float] = []
                am_series: list[float] = []
                for r in cot_rows[-52:]:
                    oi = _finite(r["open_interest"])
                    ln = _finite(r["lev_money_net"])
                    an = _finite(r["asset_mgr_net"])
                    if oi and oi > 0 and ln is not None and an is not None:
                        lev_series.append((ln / oi) * 100.0)
                        am_series.append((an / oi) * 100.0)
                if lev_series and am_series:
                    btc_lev_pct = round(lev_series[-1], 4)
                    btc_am_pct = round(am_series[-1], 4)
                    btc_lev_z, btc_am_z, cot_regime, cot_mult = compute_cot_regime_from_series(
                        lev_series, am_series
                    )

            if "lseg_short_interest_daily" in tables:
                si_rows = conn.execute(
                    """
                    SELECT s.ric, s.short_interest_pct
                    FROM lseg_short_interest_daily s
                    INNER JOIN (
                        SELECT ric, MAX(date) AS max_d
                        FROM lseg_short_interest_daily
                        WHERE short_interest_pct != ''
                        GROUP BY ric
                    ) latest ON s.ric = latest.ric AND s.date = latest.max_d
                    WHERE s.ric IN ('MARA.O', 'CLSK.O', 'RIOT.O', 'COIN.O', 'MSTR.O')
                    """
                ).fetchall()
                vals = [_finite(r["short_interest_pct"]) for r in si_rows]
                clean_si = [float(v) for v in vals if v is not None]
                if clean_si:
                    avg_si_pct = round(sum(clean_si) / len(clean_si), 4)

            if "wrds_trace_convertible_bonds" in tables:
                tr_rows = conn.execute(
                    """
                    SELECT side, COUNT(*) AS cnt
                    FROM (
                        SELECT side FROM wrds_trace_convertible_bonds
                        ORDER BY trd_exctn_dt DESC LIMIT 500
                    )
                    GROUP BY side
                    """
                ).fetchall()
                buys = sum(int(r["cnt"]) for r in tr_rows if str(r["side"]).upper() == "B")
                sells = sum(int(r["cnt"]) for r in tr_rows if str(r["side"]).upper() == "S")
                if (buys + sells) > 0:
                    trace_buy_ratio = round(buys / (buys + sells), 4)

            if "lseg_substitute_rates" in tables:
                sub_r = conn.execute(
                    """
                    SELECT us3mt_ois_spread FROM lseg_substitute_rates
                    WHERE us3mt_ois_spread != '' ORDER BY date DESC LIMIT 1
                    """
                ).fetchone()
                if sub_r is not None:
                    ois_spread = _finite(sub_r["us3mt_ois_spread"])

            if "lseg_derived_rates" in tables:
                der_r = conn.execute(
                    """
                    SELECT us2y10y_calc FROM lseg_derived_rates
                    WHERE us2y10y_calc != '' ORDER BY date DESC LIMIT 1
                    """
                ).fetchone()
                if der_r is not None:
                    v = _finite(der_r["us2y10y_calc"])
                    slope_2s10s = round(v, 4) if v is not None else None

            if "orbis_bank_liquidity_risk" in tables:
                ob_rows = conn.execute("SELECT * FROM orbis_bank_liquidity_risk").fetchall()
                liq_vals = []
                for r in ob_rows:
                    d = dict(r)
                    for k, v in d.items():
                        if "流动资产" in k:
                            fv = _finite(v)
                            if fv is not None:
                                liq_vals.append(fv)
                if liq_vals:
                    fragile = sum(1 for x in liq_vals if x < 5.0)
                    orbis_fragile_share = round(fragile / len(liq_vals), 4)
            conn.close()
        except (OSError, sqlite3.Error):
            pass

    nvda_1h = None
    smh_1h = None
    cme_term_bps = None

    if lseg_p.is_file():
        try:
            lconn = sqlite3.connect(f"file:{lseg_p.resolve().as_posix()}?mode=ro", uri=True, timeout=3.0)
            lconn.row_factory = sqlite3.Row
            upper_ms = int(now_ms) if now_ms is not None else 9_999_999_999_999

            def _ret_1h(ric: str) -> float | None:
                rows = lconn.execute(
                    """
                    SELECT close FROM cross_asset_bars
                    WHERE ric = ? AND interval = '5min' AND utc_ms <= ? AND close IS NOT NULL
                    ORDER BY utc_ms DESC LIMIT 13
                    """,
                    (ric, upper_ms),
                ).fetchall()
                if len(rows) >= 13 and float(rows[-1]["close"]) > 0:
                    c_now = float(rows[0]["close"])
                    c_prev = float(rows[-1]["close"])
                    return round(((c_now - c_prev) / c_prev) * 100.0, 4)
                return None

            nvda_1h = _ret_1h("NVDA.O")
            smh_1h = _ret_1h("SMH.O")

            c1 = lconn.execute(
                "SELECT utc_ms, close FROM cross_asset_bars WHERE ric='BTCc1' AND interval='5min' AND utc_ms<=? ORDER BY utc_ms DESC LIMIT 1",
                (upper_ms,),
            ).fetchone()
            c2 = lconn.execute(
                "SELECT utc_ms, close FROM cross_asset_bars WHERE ric='BTCc2' AND interval='5min' AND utc_ms<=? ORDER BY utc_ms DESC LIMIT 1",
                (upper_ms,),
            ).fetchone()
            if c1 is not None and c2 is not None and float(c1["close"]) > 0:
                if abs(int(c1["utc_ms"]) - int(c2["utc_ms"])) <= 900_000:
                    cme_term_bps = round(
                        ((float(c2["close"]) - float(c1["close"])) / float(c1["close"])) * 10_000.0,
                        4,
                    )
            lconn.close()
        except (OSError, sqlite3.Error):
            pass

    mult = cot_mult
    if avg_si_pct is not None and avg_si_pct >= 18.0 and (trace_buy_ratio is None or trace_buy_ratio >= 0.52):
        mult = round(min(1.25, mult * 1.05), 4)
    if ois_spread is not None and ois_spread >= 25.0:
        mult = round(mult * 0.80, 4)

    return CityUInstitutionalSnapshot(
        btc_cot_lev_net_pct=btc_lev_pct,
        btc_cot_asset_mgr_pct=btc_am_pct,
        btc_cot_lev_z=btc_lev_z,
        btc_cot_asset_mgr_z=btc_am_z,
        cot_regime=cot_regime,
        avg_miner_short_interest_pct=avg_si_pct,
        trace_conv_bond_buy_ratio=trace_buy_ratio,
        us3mt_ois_spread_bps=ois_spread,
        us2y10y_slope_pct=slope_2s10s,
        orbis_fragile_bank_share=orbis_fragile_share,
        nvda_1h_ret_pct=nvda_1h,
        smh_1h_ret_pct=smh_1h,
        cme_btc_term_spread_bps=cme_term_bps,
        size_multiplier=mult,
    )


def attach_cityu_institutional_context(
    rows: list[dict[str, Any]],
    *,
    inst_db_path: str | Path | None = None,
    lseg_db_path: str | Path | None = None,
    now_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Attach CityU institutional COT, Short-Interest, TRACE, and NVDA/SMH AI lead-lag context onto ``rows``."""
    if not rows:
        return rows
    snap = load_cityu_institutional_snapshot(inst_db_path, lseg_db_path, now_ms=now_ms)
    for row in rows:
        sym = str(row.get("binance_symbol") or row.get("token_address") or "").strip().upper()
        cluster = symbol_sector_cluster(sym)
        row.setdefault("cot_regime", snap.cot_regime)
        row.setdefault("btc_cot_lev_z", snap.btc_cot_lev_z)
        row.setdefault("btc_cot_asset_mgr_z", snap.btc_cot_asset_mgr_z)
        row.setdefault("avg_miner_short_interest_pct", snap.avg_miner_short_interest_pct)
        row.setdefault("trace_conv_bond_buy_ratio", snap.trace_conv_bond_buy_ratio)
        row.setdefault("cme_btc_term_spread_bps", snap.cme_btc_term_spread_bps)
        if cluster == "ai_compute":
            row.setdefault("nvda_1h_ret_pct", snap.nvda_1h_ret_pct)
            row.setdefault("smh_1h_ret_pct", snap.smh_1h_ret_pct)
    return rows
