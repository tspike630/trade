#!/usr/bin/env python3
"""HKU (University of Hong Kong) Bloomberg Terminal & WRDS One-Click Fetcher.

Usage for HKU Classmate:
1. On a Bloomberg Lab PC (K.K. Leung Building, HKU) with Bloomberg Terminal logged in:
     pip install blpapi pandas
     python fetch_hku_wrds_and_bloomberg.py --mode bloomberg

2. On any laptop connected to HKU Wi-Fi / VPN with a WRDS account:
     pip install wrds pandas
     python fetch_hku_wrds_and_bloomberg.py --mode wrds --wrds-user YOUR_HKU_WRDS_USERNAME
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 1. Bloomberg Tickers (Intraday 5m & Daily History)
BBG_INTRADAY_5M_TICKERS = [
    # US Spot BTC/ETH ETFs & Real-Time Intraday Indicative NAV (iNAV)
    "IBIT US Equity",
    "IBITIV Index",
    "FBTC US Equity",
    "FBTCIV Index",
    "ETHA US Equity",
    "ETHAIV Index",
    "BITO US Equity",
    "BITOIV Index",
    # HK Spot BTC/ETH ETFs (Asian Session 09:30-16:00 HKT)
    "3042 HK Equity",
    "3046 HK Equity",
    "3008 HK Equity",
    "3439 HK Equity",
    # Crypto & CME Futures / Benchmarks
    "XBTUSD Curncy",
    "XETUSD Curncy",
    "BTCA Comdty",
    "ETHA Comdty",
    "NQ1 Index",
    "ES1 Index",
    "USGG2YR Index",
    "USDJPY Curncy",
]

BBG_DAILY_MACRO_AND_VOL_TICKERS = [
    # Macro Financial Conditions & Economic Surprise Indices
    "BFCIUS Index",     # Bloomberg US Financial Conditions Index
    "BFCIEU Index",     # Bloomberg Eurozone Financial Conditions Index
    "GSUSFCI Index",    # Goldman Sachs US Financial Conditions Index
    "ECSURPUS Index",   # Bloomberg US Economic Surprise Index
    "ECSURPCN Index",   # Bloomberg China Economic Surprise Index
    "MOVE Index",       # ICE BofA US Bond Market Option Volatility Estimate
    "VIX Index",
    "VVIX Index",       # Volatility of VIX
    "SKEW Index",       # CBOE Black Swan Skew Index
    "BGCI Index",       # Bloomberg Galaxy Crypto Index
    # Crypto & Tech Proxies for Options Skew & Fund Flows
    "IBIT US Equity",
    "ETHA US Equity",
    "FBTC US Equity",
    "MSTR US Equity",
    "COIN US Equity",
    "MARA US Equity",
    "NVDA US Equity",
    "QQQ US Equity",
    "HYG US Equity",
    "TLT US Equity",
    # FX & Gold Risk Reversals (25-Delta 1M Skew)
    "USDJPY25R1M BGN Curncy",
    "EURUSD25R1M BGN Curncy",
    "XAUUSD25R1M BGN Curncy",
]

BBG_DAILY_FIELDS = [
    "PX_LAST",
    "PX_OPEN",
    "PX_HIGH",
    "PX_LOW",
    "PX_VOLUME",
    "FUND_NET_ASSET_VAL",
    "FUND_FLOW",
    "SH_OUT",
    "SHORT_INT_RATIO",
    "SI_PCT_FLOAT",
    "IMP_VOL_ATM",
    "25D_RISK_REVERSAL_1M",
    "25D_BUTTERFLY_1M",
    "PUT_CALL_OPEN_INTEREST_RATIO",
]


def fetch_bloomberg_data(out_dir: Path) -> None:
    try:
        import blpapi  # type: ignore[import-not-found]
        import pandas as pd
    except ImportError as exc:
        raise SystemExit(
            "Please run: pip install blpapi pandas\n"
            "(Or use the Excel Add-In method in HKU_DATA_DOWNLOAD_GUIDE.md)"
        ) from exc

    out_dir.mkdir(parents=True, exist_ok=True)
    options = blpapi.SessionOptions()
    options.setServerHost("localhost")
    options.setServerPort(8194)
    session = blpapi.Session(options)
    if not session.start():
        raise SystemExit("Failed to start Bloomberg session. Make sure Bloomberg Terminal is logged in.")
    if not session.openService("//blp/refdata"):
        raise SystemExit("Failed to open //blp/refdata service.")

    refdata = session.getService("//blp/refdata")

    # Part A: Historical Daily Data (2024-01-01 to Today)
    req = refdata.createRequest("HistoricalDataRequest")
    for tk in BBG_DAILY_MACRO_AND_VOL_TICKERS:
        req.getElement("securities").appendValue(tk)
    for fld in BBG_DAILY_FIELDS:
        req.getElement("fields").appendValue(fld)
    req.set("startDate", "20240101")
    req.set("endDate", datetime.now(timezone.utc).strftime("%Y%m%d"))
    req.set("periodicitySelection", "DAILY")
    session.sendRequest(req)

    daily_rows = []
    while True:
        ev = session.nextEvent(5000)
        for msg in ev:
            if msg.hasElement("securityData"):
                sec = msg.getElement("securityData")
                ticker = sec.getElementAsString("security")
                f_data = sec.getElement("fieldData")
                for i in range(f_data.numValues()):
                    el = f_data.getValueAsElement(i)
                    row = {"ticker": ticker, "date": el.getElementAsString("date")}
                    for fld in BBG_DAILY_FIELDS:
                        if el.hasElement(fld):
                            try:
                                row[fld] = el.getElementAsFloat(fld)
                            except Exception:  # noqa: BLE001
                                row[fld] = None
                    daily_rows.append(row)
        if ev.eventType() == blpapi.Event.RESPONSE:
            break

    if daily_rows:
        df_daily = pd.DataFrame(daily_rows)
        daily_csv = out_dir / "hku_bloomberg_daily_macro_vol_flows.csv"
        df_daily.to_csv(daily_csv, index=False)
        print(f"[OK] Saved {len(df_daily)} rows -> {daily_csv}")

    # Part B: Intraday 5-Minute Bars (TRADE, BID, ASK) for past 180 days
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=180)
    intraday_rows = []
    for tk in BBG_INTRADAY_5M_TICKERS:
        for event_type in ("TRADE", "BID", "ASK"):
            ireq = refdata.createRequest("IntradayBarRequest")
            ireq.set("security", tk)
            ireq.set("eventType", event_type)
            ireq.set("interval", 5)
            ireq.set("startDateTime", start_dt)
            ireq.set("endDateTime", end_dt)
            session.sendRequest(ireq)
            while True:
                ev = session.nextEvent(5000)
                for msg in ev:
                    if msg.hasElement("barData") and msg.getElement("barData").hasElement("barTickData"):
                        ticks = msg.getElement("barData").getElement("barTickData")
                        for i in range(ticks.numValues()):
                            b = ticks.getValueAsElement(i)
                            intraday_rows.append(
                                {
                                    "ticker": tk,
                                    "event_type": event_type,
                                    "time_utc": b.getElementAsString("time"),
                                    "open": b.getElementAsFloat("open"),
                                    "high": b.getElementAsFloat("high"),
                                    "low": b.getElementAsFloat("low"),
                                    "close": b.getElementAsFloat("close"),
                                    "volume": b.getElementAsFloat("volume") if b.hasElement("volume") else None,
                                    "num_events": b.getElementAsInteger("numEvents") if b.hasElement("numEvents") else None,
                                }
                            )
                if ev.eventType() == blpapi.Event.RESPONSE:
                    break
        print(f"[OK] Pulled 5m TRADE/BID/ASK bars for {tk}")

    if intraday_rows:
        df_intra = pd.DataFrame(intraday_rows)
        intra_csv = out_dir / "hku_bloomberg_intraday_5m_bars.csv"
        df_intra.to_csv(intra_csv, index=False)
        print(f"[OK] Saved {len(df_intra)} rows -> {intra_csv}")

    session.stop()


def fetch_wrds_data(out_dir: Path, wrds_username: str | None = None) -> None:
    try:
        import pandas as pd
        import wrds  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SystemExit("Please run: pip install wrds pandas") from exc

    out_dir.mkdir(parents=True, exist_ok=True)
    db = wrds.Connection(wrds_username=wrds_username)

    queries = {
        "hku_wrds_optionmetrics_vol_surface.csv": """
            SELECT s.ticker, v.date, v.days, v.delta,
                   v.impl_volatility, v.impl_strike, v.impl_premium, v.dispersion
            FROM optionm.vsurfd2024 v
            JOIN optionm.securd s ON v.secid = s.secid
            WHERE s.ticker IN ('IBIT', 'BITO', 'COIN', 'MSTR', 'MARA', 'RIOT', 'NVDA', 'QQQ', 'SPY', 'HYG', 'TLT', 'GLD')
              AND v.days IN (30, 60, 91)
              AND ABS(v.delta) IN (25, 50)
        """,
        "hku_wrds_crsp_crypto_etf_daily.csv": """
            SELECT n.ticker, d.date, d.prc, d.vol, d.ret, d.bid, d.ask, d.shrout
            FROM crsp.dsf d
            JOIN crsp.dsenames n
              ON d.permno = n.permno
             AND d.date BETWEEN n.namedt AND COALESCE(n.nameendt, CURRENT_DATE)
            WHERE n.ticker IN ('IBIT', 'FBTC', 'ARKB', 'BITB', 'ETHA', 'BITO', 'COIN', 'MSTR', 'MARA', 'RIOT', 'NVDA', 'HYG', 'TLT')
              AND d.date >= '2024-01-01'
        """,
    }

    for filename, sql in queries.items():
        try:
            df = db.raw_sql(sql)
            if df is not None and not df.empty:
                out_path = out_dir / filename
                df.to_csv(out_path, index=False)
                print(f"[OK] Saved {len(df)} rows -> {out_path}")
        except Exception as err:  # noqa: BLE001
            print(f"[SKIP] {filename}: {err}")

    db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="HKU Bloomberg & WRDS One-Click Data Exporter")
    parser.add_argument("--mode", choices=("bloomberg", "wrds"), required=True, help="Which platform to fetch from.")
    parser.add_argument("--out-dir", type=Path, default=Path("hku_exported_data"), help="Output directory for CSV files.")
    parser.add_argument("--wrds-user", type=str, default=None, help="HKU WRDS username (for --mode wrds).")
    args = parser.parse_args()

    if args.mode == "bloomberg":
        fetch_bloomberg_data(args.out_dir)
    else:
        fetch_wrds_data(args.out_dir, wrds_username=args.wrds_user)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
