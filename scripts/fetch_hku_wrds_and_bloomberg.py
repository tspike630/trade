#!/usr/bin/env python3
"""HKU (University of Hong Kong) Complete All-in-One WRDS & Bloomberg Data Exporter.

Automatically pulls:
- Mode 'wrds': All 8 WRDS institutional tables (TAQ Intraday Microstructure Indicators,
  OptionMetrics Volatility Surface, OptionMetrics Standardized Options & OI,
  RavenPack News Analytics, FINRA TRACE Convertible/Corporate Bond Trades,
  Compustat Short Interest, 13F Institutional & Insider Trades, CRSP Daily & Treasuries).
- Mode 'bloomberg': All 65+ Bloomberg Tickers across Intraday 5m (TRADE, BID, ASK)
  and Daily Macro FCI / Economic Surprise / Options 25D Risk Reversal / ETF iNAV & Flows.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

BBG_INTRADAY_5M_TICKERS = [
    # 1A. US Spot & Futures Crypto ETFs + Real-Time Intraday Indicative NAV (iNAV)
    "IBIT US Equity", "IBITIV Index",
    "FBTC US Equity", "FBTCIV Index",
    "ARKB US Equity", "ARKBIV Index",
    "BITB US Equity", "BITBIV Index",
    "GBTC US Equity", "GBTCIV Index",
    "HODL US Equity", "HODLIV Index",
    "ETHA US Equity", "ETHAIV Index",
    "FETH US Equity", "FETHIV Index",
    "ETHE US Equity",
    "BITO US Equity", "BITOIV Index",
    "BITX US Equity",
    # 1B. Hong Kong Spot BTC/ETH ETFs (HKD & USD Counters, Asian Session)
    "3042 HK Equity", "3046 HK Equity",
    "3008 HK Equity", "3009 HK Equity",
    "3439 HK Equity", "3179 HK Equity",
    "9042 HK Equity", "9046 HK Equity",
    # 1C. Crypto Spot Benchmarks & CME 1st/2nd Month + Micro Futures
    "BGCI Index", "BGDI Index",
    "XBTUSD Curncy", "XETUSD Curncy", "XSOUSD Curncy", "XBNUSD Curncy",
    "XRPUSD Curncy", "XDOUSD Curncy",
    "BTCA Comdty", "BTCB Comdty", "MBTA Comdty",
    "ETHA Comdty", "ETHB Comdty", "META Comdty",
    # 1D. Crypto Proxy Stocks, Miners, AI Leaders & Wall Street CTA / Risk-Parity ETFs
    "MSTR US Equity", "COIN US Equity", "MARA US Equity", "RIOT US Equity",
    "CLSK US Equity", "CORZ US Equity", "IREN US Equity", "HOOD US Equity",
    "NVDA US Equity", "SMH US Equity",
    "DBMF US Equity", "CTA US Equity", "RPAR US Equity", "SVOL US Equity", "TAIL US Equity",
    # 1E. Cross-Asset Futures, Rates & Carry FX
    "NQ1 Index", "ES1 Index", "VIX Index", "MOVE Index",
    "USGG2YR Index", "USGG10YR Index", "USGGT10Y Index",
    "USDJPY Curncy", "USDCNH Curncy", "AUDJPY Curncy", "XAU Curncy",
]

BBG_DAILY_UNIVERSE = [
    # Macro Financial Conditions Indices (FCI)
    "BFCIUS Index", "BFCIEU Index", "BFCICN Index",
    "GSUSFCI Index", "GSEUFCI Index", "GSCNFCI Index",
    "NFCI Index", "ANFCI Index",
    # Economic Surprise Indices (Bloomberg & Citi)
    "ECSURPUS Index", "ECSURPEU Index", "ECSURPCN Index",
    "CESIUSD Index", "CESIEUR Index", "CESICHN Index", "CESIJPY Index", "CESIG10 Index",
    # Volatility, Skew & Cross-Market Panic Indices
    "VIX Index", "VIX9D Index", "VIX3M Index", "VIX6M Index",
    "VVIX Index", "SKEW Index", "VXN Index", "RVX Index",
    "MOVE Index", "GVZ Index", "OVX Index",
    "JPMVXYGL Index", "JPMVXYG7 Index", "JPMVXYEM Index",
    # FX & Gold Implied Vol & 25-Delta Risk Reversal / Butterfly
    "USDJPYV1M BGN Curncy", "USDJPY25R1M BGN Curncy", "USDJPY25B1M BGN Curncy",
    "EURUSDV1M BGN Curncy", "EURUSD25R1M BGN Curncy",
    "USDCNHV1M BGN Curncy", "USDCNH25R1M BGN Curncy",
    "AUDJPYV1M BGN Curncy", "AUDJPY25R1M BGN Curncy",
    "XAUUSDV1M BGN Curncy", "XAUUSD25R1M BGN Curncy",
    # Repo Funding, Yield Curve Slopes, Breakevens, TIPS Real Yields, XCCY Basis & CDX Spreads
    "SOFRRATE Index", "EFFR Index", "OBFR Index", "FRAOISUS Index",
    "USBMMY3M Index", "USGG2YR Index", "USGG5YR Index", "USGG10YR Index", "USGG30YR Index",
    "USYC2Y10 Index", "USYC5Y30 Index", "USGGBE05 Index", "USGGBE10 Index",
    "USGGT05Y Index", "USGGT10Y Index",
    "JYBS3 Curncy", "JYBS1 Curncy", "EUBS3 Curncy", "SFBS3 Curncy",
    "LF98OAS Index", "LUACOAS Index",
    # Crypto Benchmarks, Spot ETFs, Proxy Stocks, AI & CTA/Vol-Control ETFs
    "BGCI Index", "BGDI Index",
    "IBIT US Equity", "FBTC US Equity", "ARKB US Equity", "BITB US Equity", "GBTC US Equity",
    "ETHA US Equity", "FETH US Equity", "ETHE US Equity", "BITO US Equity", "BITX US Equity",
    "3042 HK Equity", "3046 HK Equity", "3008 HK Equity", "3439 HK Equity",
    "MSTR US Equity", "COIN US Equity", "MARA US Equity", "RIOT US Equity",
    "CLSK US Equity", "CORZ US Equity", "IREN US Equity", "WULF US Equity",
    "HUT US Equity", "BITF US Equity", "HOOD US Equity",
    "NVDA US Equity", "SMH US Equity", "SOXX US Equity", "AMD US Equity", "TSM US Equity",
    "DBMF US Equity", "CTA US Equity", "KMLM US Equity", "RPAR US Equity",
    "SVOL US Equity", "SVIX US Equity", "UVIX US Equity", "TAIL US Equity",
    "SPY US Equity", "QQQ US Equity", "IWM US Equity", "HYG US Equity", "TLT US Equity", "GLD US Equity",
]

BBG_DAILY_FIELDS = [
    "PX_OPEN", "PX_HIGH", "PX_LOW", "PX_LAST", "PX_VOLUME",
    "PX_BID", "PX_ASK", "OPEN_INT",
    "FUND_NET_ASSET_VAL", "FUND_FLOW", "SH_OUT",
    "SHORT_INT_RATIO", "SI_PCT_FLOAT",
    "IMP_VOL_ATM",
    "30DAY_IMPVOL_90.0%MNY_DF", "30DAY_IMPVOL_100.0%MNY_DF", "30DAY_IMPVOL_110.0%MNY_DF",
    "25D_RISK_REVERSAL_1M", "25D_RISK_REVERSAL_3M", "25D_BUTTERFLY_1M",
    "PUT_CALL_OPEN_INTEREST_RATIO", "PUT_CALL_VOLUME_RATIO",
    "OPEN_INT_TOTAL_CALL", "OPEN_INT_TOTAL_PUT",
]


def fetch_bloomberg_data(out_dir: Path) -> None:
    import blpapi  # type: ignore[import-not-found]
    import pandas as pd

    out_dir.mkdir(parents=True, exist_ok=True)
    options = blpapi.SessionOptions()
    options.setServerHost("localhost")
    options.setServerPort(8194)
    session = blpapi.Session(options)
    if not session.start() or not session.openService("//blp/refdata"):
        raise SystemExit("Please log into Bloomberg Terminal on this PC first.")

    refdata = session.getService("//blp/refdata")
    req = refdata.createRequest("HistoricalDataRequest")
    for tk in BBG_DAILY_UNIVERSE:
        req.getElement("securities").appendValue(tk)
    for fld in BBG_DAILY_FIELDS:
        req.getElement("fields").appendValue(fld)
    req.set("startDate", "20230101")
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
        p_daily = out_dir / "hku_bbg_daily_macro_options_flows_full.csv"
        df_daily.to_csv(p_daily, index=False)
        print(f"[OK] Saved {len(df_daily)} daily rows -> {p_daily}")

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
                            intraday_rows.append({
                                "ticker": tk,
                                "event_type": event_type,
                                "time_utc": b.getElementAsString("time"),
                                "open": b.getElementAsFloat("open"),
                                "high": b.getElementAsFloat("high"),
                                "low": b.getElementAsFloat("low"),
                                "close": b.getElementAsFloat("close"),
                                "volume": b.getElementAsFloat("volume") if b.hasElement("volume") else None,
                                "num_events": b.getElementAsInteger("numEvents") if b.hasElement("numEvents") else None,
                            })
                if ev.eventType() == blpapi.Event.RESPONSE:
                    break
        print(f"[OK] Pulled 5m TRADE/BID/ASK bars for {tk}")

    if intraday_rows:
        df_intra = pd.DataFrame(intraday_rows)
        p_intra = out_dir / "hku_bbg_intraday_5m_trade_bid_ask_full.csv"
        df_intra.to_csv(p_intra, index=False)
        print(f"[OK] Saved {len(df_intra)} intraday rows -> {p_intra}")

    session.stop()


def fetch_wrds_data(out_dir: Path, wrds_username: str | None = None) -> None:
    import pandas as pd
    import wrds  # type: ignore[import-not-found]

    out_dir.mkdir(parents=True, exist_ok=True)
    db = wrds.Connection(wrds_username=wrds_username)

    tickers_sql = (
        "'IBIT','FBTC','ARKB','BITB','GBTC','ETHA','FETH','ETHE','BITO','BITX',"
        "'MSTR','COIN','MARA','RIOT','CLSK','CORZ','IREN','WULF','HUT','HOOD',"
        "'NVDA','SMH','SOXX','AMD','QQQ','SPY','IWM','HYG','TLT','GLD',"
        "'DBMF','CTA','KMLM','RPAR','SVOL','TAIL'"
    )

    queries = {
        "hku_wrds_1_taq_microstructure.csv": f"""
            SELECT *
            FROM taq.wrds_iid_2024
            WHERE sym_root IN ({tickers_sql})
            UNION ALL
            SELECT *
            FROM taq.wrds_iid_2025
            WHERE sym_root IN ({tickers_sql})
        """,
        "hku_wrds_2_optionmetrics_surface.csv": f"""
            SELECT s.ticker, v.date, v.days, v.delta, v.cp_flag,
                   v.impl_volatility, v.impl_strike, v.impl_premium, v.dispersion
            FROM optionm.vsurfd2024 v
            JOIN optionm.securd s ON v.secid = s.secid
            WHERE s.ticker IN ({tickers_sql})
              AND v.days IN (10, 30, 60, 91, 182)
        """,
        "hku_wrds_3_optionmetrics_stdopd.csv": f"""
            SELECT s.ticker, o.date, o.days, o.cp_flag,
                   o.forward_price, o.strike_price, o.impl_volatility,
                   o.delta, o.gamma, o.vega, o.theta
            FROM optionm.stdopd2024 o
            JOIN optionm.securd s ON o.secid = s.secid
            WHERE s.ticker IN ({tickers_sql})
              AND o.days IN (30, 60, 91)
        """,
        "hku_wrds_4_ravenpack_news.csv": """
            SELECT timestamp_utc, entity_name, country_code, topic, "group", "type",
                   ess, ens, css, nip, relevance, headline
            FROM ravenpack_dj.rpa_djpr_equities_2024
            WHERE relevance >= 80
              AND (
                  entity_name ILIKE '%Bitcoin%' OR entity_name ILIKE '%Coinbase%'
                  OR entity_name ILIKE '%MicroStrategy%' OR entity_name ILIKE '%BlackRock%'
                  OR entity_name ILIKE '%Federal Reserve%' OR entity_name ILIKE '%Nvidia%'
              )
            LIMIT 250000
        """,
        "hku_wrds_5_trace_convertible_bonds.csv": """
            SELECT trd_exctn_dt, trd_exctn_tm, sym_cd, cusip_id,
                   entrd_vol_qt, rptd_pr, yld_pt, rpt_side_cd, contra_party_type
            FROM trace.trace_enhanced
            WHERE trd_exctn_dt >= '2024-01-01'
              AND (
                  sym_cd ILIKE 'MSTR%' OR sym_cd ILIKE 'COIN%'
                  OR sym_cd ILIKE 'MARA%' OR sym_cd ILIKE 'RIOT%'
                  OR sym_cd ILIKE 'CLSK%'
              )
        """,
        "hku_wrds_6_short_interest.csv": f"""
            SELECT gvkey, iid, ticker, datadate, shortint, shortintadj, splitadjdate
            FROM comp.sec_shortint
            WHERE ticker IN ({tickers_sql})
              AND datadate >= '2023-01-01'
        """,
        "hku_wrds_7a_13f_holdings.csv": f"""
            SELECT fdate, mgrname, ticker, cusip, shares, prc, shrout1
            FROM tfn.s34
            WHERE ticker IN ({tickers_sql})
              AND fdate >= '2023-01-01'
        """,
        "hku_wrds_7b_insider_trades.csv": f"""
            SELECT ticker, trandate, personname, rolecode1, trancode, shares, tprice
            FROM tfn.table1
            WHERE ticker IN ({tickers_sql})
              AND trandate >= '2023-01-01'
        """,
        "hku_wrds_8a_crsp_daily.csv": f"""
            SELECT n.ticker, d.date, d.prc, d.vol, d.ret, d.bid, d.ask, d.bidlo, d.askhi, d.shrout
            FROM crsp.dsf d
            JOIN crsp.dsenames n
              ON d.permno = n.permno
             AND d.date BETWEEN n.namedt AND COALESCE(n.nameendt, CURRENT_DATE)
            WHERE n.ticker IN ({tickers_sql})
              AND d.date >= '2023-01-01'
        """,
        "hku_wrds_8b_crsp_treasuries.csv": """
            SELECT caldt, kytreasno, tdnomprc, tdyld, tdduratn, tdretnua
            FROM crsp.tfz_dly
            WHERE caldt >= '2023-01-01'
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
    parser = argparse.ArgumentParser(description="HKU All-in-One Bloomberg & WRDS Exporter")
    parser.add_argument("--mode", choices=("bloomberg", "wrds"), required=True)
    parser.add_argument("--out-dir", type=Path, default=Path("hku_exported_data"))
    parser.add_argument("--wrds-user", type=str, default=None)
    args = parser.parse_args()

    if args.mode == "bloomberg":
        fetch_bloomberg_data(args.out_dir)
    else:
        fetch_wrds_data(args.out_dir, wrds_username=args.wrds_user)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
