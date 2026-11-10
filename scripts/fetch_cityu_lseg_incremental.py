#!/usr/bin/env python3
"""City University of Hong Kong (CityU) All-in-One Institutional Data Exporter (LSEG + WRDS + Bloomberg).

Works out-of-the-box with ZERO arguments inside CityU LSEG Workspace ``CODEBOOK``
(defaults to ``--mode lseg``), and also supports CityU Run Run Shaw Library ``WRDS``
(``--mode wrds``) and CityU AC3 Finance Lab ``Bloomberg`` (``--mode bloomberg``).

Usage at CityU:
  1. In CityU LSEG Workspace CodeBook (or local PC with LSEG Workspace running):
       python fetch_cityu_lseg_incremental.py
       (or: python fetch_cityu_lseg_incremental.py --mode lseg)
  2. On CityU Wi-Fi / VPN via Run Run Shaw Library WRDS (15 Institutional Tables):
       pip install wrds pandas
       python fetch_cityu_lseg_incremental.py --mode wrds --wrds-user YOUR_CITYU_WRDS_USER
  3. In CityU College of Business (AC3) Bloomberg Lab:
       pip install blpapi pandas
       python fetch_cityu_lseg_incremental.py --mode bloomberg
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ============================================================================
# 1. LSEG WORKSPACE / CODEBOOK UNIVERSE (CityU Primary Terminal — Tables 1..7)
# ============================================================================
INTRADAY_AND_DAILY_RICS = [
    # Implied Volatility, Skew & Tail-Risk Term Structure
    ".VIX", ".VIX9D", ".VIX3M", ".VXN", ".SKEW", "VIXc1", "VIXc2", ".MOVE",
    "JPY1MO=", "JPY1MRR=", "EUR1MO=", "EUR1MRR=", "XAU1MO=", "XAU1MRR=",
    # Wall Street CTA / Managed Futures / Vol-Control / Risk-Parity Rebalance Proxies
    "DBMF.P", "CTA.P", "KMLM.P", "RPAR.P", "SVOL.P", "TAIL.P",
    # Rates, Funding Stress, Cross-Currency Basis & Credit Spreads
    "SOFR=", "USONFFE=", "US2Y10Y=RR", "US5Y5YF=R", "JPYCBS3M=", "EURCBS3M=",
    "LQD.P", "BKLN.P", "CLc1",
    # Sector Lead-Lag & Crypto / AI Equities
    "NVDA.O", "SMH.O", "HOOD.O", "CLSK.O", "CORZ.O", "IREN.O",
    # Asian & Global Carry FX Thermometers
    "AUDJPY=", "EURJPY=", "KRW=", "SGD=", "CHF=", "MXN=",
    # CME 2nd-Month Crypto Futures (Roll Yield / Term Structure) & Spot ETFs
    "BTCc2", "ETHc2", "SOL=", "FBTC.Z", "ARKB.Z", "BITB.P", "GBTC.P", "FETH.Z", "ETHE.P",
]

CORE_REFRESH_RICS = [
    "BTC=", "ETH=", "BTCc1", "ETHc1", "ESc1", "NQc1",
    "US2YT=RR", "US10YT=RR", "JP10YT=RR",
    ".DXY", "EUR=", "JPY=", "CNH=", "XAU=",
    "QQQ.O", "TLT.O", "HYG.P", "IBIT.O", "ETHA.O",
    "MSTR.O", "COIN.O", "MARA.O", "RIOT.O",
]

ETF_FLOW_RICS = [
    "IBIT.O", "FBTC.Z", "ARKB.Z", "BITB.P", "GBTC.P",
    "ETHA.O", "FETH.Z", "ETHE.P", "HYG.P", "TLT.O", "QQQ.O", "DBMF.P",
]

STARMINE_EQ_RICS = [
    "MSTR.O", "COIN.O", "MARA.O", "RIOT.O", "CLSK.O",
    "CORZ.O", "IREN.O", "HOOD.O", "NVDA.O", "SMH.O",
]

CFTC_COT_RICS = [
    "0#133741CFTC",  # CME Bitcoin CFTC COT family
    "0#146021CFTC",  # CME Ether CFTC COT family
    "0#209742CFTC",  # CME E-mini Nasdaq-100 CFTC COT family
    "0#13874ACFTC",  # CME E-mini S&P 500 CFTC COT family
    "0#097741CFTC",  # CME Japanese Yen CFTC COT family
    "0#043602CFTC",  # CBOT 10Y Treasury Note CFTC COT family
]


def run_cityu_lseg_export(out_dir: Path = Path(".")) -> None:
    """Export all 7 LSEG Workspace tables inside CityU CodeBook."""
    import pandas as pd
    try:
        import refinitiv.data as rd
    except ImportError as exc:
        raise SystemExit(
            "Install refinitiv-data (`pip install refinitiv-data`) or run inside CityU LSEG Workspace CodeBook."
        ) from exc

    out_dir.mkdir(parents=True, exist_ok=True)

    def _fetch_bars(rics: list[str], out_csv: Path, count: int = 5000) -> None:
        frames = []
        for interval in ("5min", "1h", "1d"):
            for ric in rics:
                try:
                    df = rd.get_history(universe=ric, interval=interval, count=count)
                    if df is not None and not df.empty:
                        df = df.reset_index()
                        df["ric"] = ric
                        df["interval"] = interval
                        frames.append(df)
                        print(f"[OK] {ric} ({interval}): {len(df)} rows")
                except Exception as err:  # noqa: BLE001
                    print(f"[SKIP] {ric} ({interval}): {err}")
        if frames:
            pd.concat(frames, ignore_index=True).to_csv(out_csv, index=False)
            print(f"[SAVED] {out_csv}")

    rd.open_session()
    try:
        # Table 1: Incremental 47 Cross-Asset / Volatility / CTA / Term-Structure Bars
        _fetch_bars(INTRADAY_AND_DAILY_RICS, out_dir / "lseg_cityu_incremental_bars.csv", count=5000)

        # Table 2: Core 23 RICs 5min/1h/1d Refresh (with full dealer BID/ASK/NUMMOV/VWAP columns)
        _fetch_bars(CORE_REFRESH_RICS, out_dir / "lseg_cityu_core_refresh_bars.csv", count=5000)

        # Table 3: Spot ETF & Macro Fund Flows + NAV (for AP Arbitrage Discount/Premium)
        try:
            etf_flows = rd.get_data(
                universe=ETF_FLOW_RICS,
                fields=[
                    "TR.FundNetFlows.date",
                    "TR.FundNetFlows",
                    "TR.NETASSETVAL",
                    "TR.CompanyMarketCap",
                    "TR.SharesOutstanding",
                ],
                parameters={"SDate": "2024-01-10", "EDate": "0D", "Frq": "D"},
            )
            if etf_flows is not None:
                etf_flows.to_csv(out_dir / "lseg_cityu_etf_flows_nav.csv", index=False)
                print(f"[SAVED] {out_dir / 'lseg_cityu_etf_flows_nav.csv'}")
        except Exception as err:  # noqa: BLE001
            print(f"[SKIP] ETF flows: {err}")

        # Table 4: LSEG StarMine Quantitative Crowding, Short Squeeze & Smart Holdings
        try:
            starmine_df = rd.get_data(
                universe=STARMINE_EQ_RICS,
                fields=[
                    "TR.ShortInterestPct.date",
                    "TR.ShortInterestPct",
                    "TR.StarMineShortSqueeze",
                    "TR.StarMineARM",
                    "TR.StarMineSmartHoldings",
                    "TR.StarMineCombinedAlpha",
                ],
                parameters={"SDate": "2024-01-01", "EDate": "0D", "Frq": "D"},
            )
            if starmine_df is not None:
                starmine_df.to_csv(out_dir / "lseg_cityu_starmine_crowding.csv", index=False)
                print(f"[SAVED] {out_dir / 'lseg_cityu_starmine_crowding.csv'}")
        except Exception as err:  # noqa: BLE001
            print(f"[SKIP] StarMine crowding: {err}")

        # Table 5: Institutional 13F Ownership & Insider Net Buying/Selling
        try:
            own_df = rd.get_data(
                universe=STARMINE_EQ_RICS + ETF_FLOW_RICS[:8],
                fields=[
                    "TR.PctOfSharesOutHeld.date",
                    "TR.PctOfSharesOutHeld",
                    "TR.NumOfInstitutionalHolders",
                    "TR.InsiderNetSharesTraded",
                ],
                parameters={"SDate": "2024-01-01", "EDate": "0D", "Frq": "Q"},
            )
            if own_df is not None:
                own_df.to_csv(out_dir / "lseg_cityu_institutional_ownership.csv", index=False)
                print(f"[SAVED] {out_dir / 'lseg_cityu_institutional_ownership.csv'}")
        except Exception as err:  # noqa: BLE001
            print(f"[SKIP] Institutional ownership: {err}")

        # Table 6: CFTC COT Weekly Leveraged Fund vs Asset Manager Positioning
        try:
            cot_frames = []
            for c_ric in CFTC_COT_RICS:
                c_df = rd.get_history(universe=c_ric, interval="1W", count=260)
                if c_df is not None and not c_df.empty:
                    c_df = c_df.reset_index()
                    c_df["cot_family_ric"] = c_ric
                    cot_frames.append(c_df)
            if cot_frames:
                pd.concat(cot_frames, ignore_index=True).to_csv(
                    out_dir / "lseg_cityu_cftc_cot_positioning.csv", index=False
                )
                print(f"[SAVED] {out_dir / 'lseg_cityu_cftc_cot_positioning.csv'}")
        except Exception as err:  # noqa: BLE001
            print(f"[SKIP] CFTC COT positioning: {err}")

        # Table 7: Reuters Machine-Readable Macro & Crypto Headlines
        try:
            news_df = rd.news.get_headlines(
                query="Topic:CRYP OR (FOMC AND rate) OR (SEC AND ETF) OR (Binance OR Tether OR Hack)",
                count=1000,
            )
            if news_df is not None:
                news_df.to_csv(out_dir / "lseg_cityu_news_headlines.csv", index=False)
                print(f"[SAVED] {out_dir / 'lseg_cityu_news_headlines.csv'}")
        except Exception as err:  # noqa: BLE001
            print(f"[SKIP] News headlines: {err}")
    finally:
        rd.close_session()


# ============================================================================
# 2. CITYU RUN RUN SHAW LIBRARY WRDS EXPORTER (15 Tables)
# ============================================================================
def run_cityu_wrds_export(out_dir: Path, wrds_username: str | None = None) -> None:
    """Export all 15 WRDS tables using CityU Run Run Shaw Library WRDS access."""
    import pandas as pd
    import wrds

    out_dir.mkdir(parents=True, exist_ok=True)
    db = wrds.Connection(wrds_username=wrds_username)

    tickers_sql = (
        "'IBIT','FBTC','ARKB','BITB','GBTC','HODL','ETHA','FETH','ETHE','BITO','BITX',"
        "'MSTR','COIN','MARA','RIOT','CLSK','CORZ','IREN','WULF','HUT','BITF','HOOD','SQ',"
        "'NVDA','SMH','SOXX','AMD','TSM','PLTR','ARM','QQQ','SPY','IWM','HYG','LQD','TLT','GLD',"
        "'DBMF','CTA','KMLM','RPAR','SVOL','TAIL'"
    )

    queries = {
        "cityu_wrds_01_taq_iid.csv": f"""
            SELECT * FROM taq.wrds_iid_2024 WHERE sym_root IN ({tickers_sql})
            UNION ALL
            SELECT * FROM taq.wrds_iid_2025 WHERE sym_root IN ({tickers_sql})
        """,
        "cityu_wrds_03_optionmetrics_vsurfd.csv": f"""
            SELECT s.ticker, v.date, v.days, v.delta, v.cp_flag,
                   v.impl_volatility, v.impl_strike, v.impl_premium, v.dispersion
            FROM optionm.vsurfd2024 v
            JOIN optionm.securd s ON v.secid = s.secid
            WHERE s.ticker IN ({tickers_sql}) AND v.days IN (10, 30, 60, 91, 182)
        """,
        "cityu_wrds_04_optionmetrics_stdopd.csv": f"""
            SELECT s.ticker, o.date, o.days, o.cp_flag,
                   o.forward_price, o.strike_price, o.impl_volatility,
                   o.delta, o.gamma, o.vega, o.theta
            FROM optionm.stdopd2024 o
            JOIN optionm.securd s ON o.secid = s.secid
            WHERE s.ticker IN ({tickers_sql}) AND o.days IN (30, 60, 91)
        """,
        "cityu_wrds_05_optionmetrics_opvold.csv": f"""
            SELECT s.ticker, v.date, v.cp_flag, v.volume, v.open_interest
            FROM optionm.opvold v
            JOIN optionm.securd s ON v.secid = s.secid
            WHERE s.ticker IN ({tickers_sql}) AND v.date >= '2023-01-01'
        """,
        "cityu_wrds_06_optionmetrics_zerocd.csv": """
            SELECT date, days, rate FROM optionm.zerocd WHERE date >= '2023-01-01'
        """,
        "cityu_wrds_07_ravenpack_news.csv": """
            SELECT timestamp_utc, entity_name, country_code, topic, "group", "type",
                   ess, ens, css, nip, relevance, headline
            FROM ravenpack_dj.rpa_djpr_equities_2024
            WHERE relevance >= 75
              AND (
                  entity_name ILIKE '%Bitcoin%' OR entity_name ILIKE '%Coinbase%'
                  OR entity_name ILIKE '%MicroStrategy%' OR entity_name ILIKE '%BlackRock%'
                  OR entity_name ILIKE '%Federal Reserve%' OR entity_name ILIKE '%Nvidia%'
              )
            LIMIT 250000
        """,
        "cityu_wrds_08_trace_convertible_bonds.csv": """
            SELECT trd_exctn_dt, trd_exctn_tm, sym_cd, cusip_id,
                   entrd_vol_qt, rptd_pr, yld_pt, rpt_side_cd, contra_party_type
            FROM trace.trace_enhanced
            WHERE trd_exctn_dt >= '2024-01-01'
              AND (
                  sym_cd ILIKE 'MSTR%' OR sym_cd ILIKE 'COIN%'
                  OR sym_cd ILIKE 'MARA%' OR sym_cd ILIKE 'RIOT%'
                  OR sym_cd ILIKE 'CLSK%' OR sym_cd ILIKE 'CORZ%'
              )
        """,
        "cityu_wrds_09_markit_short_loan.csv": f"""
            SELECT * FROM markit.msf_analytics
            WHERE ticker IN ({tickers_sql}) AND datadate >= '2023-01-01'
        """,
        "cityu_wrds_10_compustat_shortint.csv": f"""
            SELECT gvkey, iid, ticker, datadate, shortint, shortintadj, splitadjdate
            FROM comp.sec_shortint
            WHERE ticker IN ({tickers_sql}) AND datadate >= '2023-01-01'
        """,
        "cityu_wrds_11_13f_holdings.csv": f"""
            SELECT fdate, mgrname, ticker, cusip, shares, prc, shrout1
            FROM tfn.s34
            WHERE ticker IN ({tickers_sql}) AND fdate >= '2023-01-01'
        """,
        "cityu_wrds_12_mutual_fund_s12.csv": f"""
            SELECT fdate, fundname, ticker, cusip, shares, prc
            FROM tfn.s12
            WHERE ticker IN ({tickers_sql}) AND fdate >= '2023-01-01'
        """,
        "cityu_wrds_13_insider_trades.csv": f"""
            SELECT ticker, trandate, personname, rolecode1, trancode, shares, tprice
            FROM tfn.table1
            WHERE ticker IN ({tickers_sql}) AND trandate >= '2023-01-01'
        """,
        "cityu_wrds_14a_crsp_daily.csv": f"""
            SELECT n.ticker, d.date, d.prc, d.vol, d.ret, d.bid, d.ask, d.bidlo, d.askhi, d.shrout
            FROM crsp.dsf d
            JOIN crsp.dsenames n
              ON d.permno = n.permno
             AND d.date BETWEEN n.namedt AND COALESCE(n.nameendt, CURRENT_DATE)
            WHERE n.ticker IN ({tickers_sql}) AND d.date >= '2023-01-01'
        """,
        "cityu_wrds_14b_crsp_treasuries.csv": """
            SELECT caldt, kytreasno, tdnomprc, tdyld, tdduratn, tdretnua
            FROM crsp.tfz_dly WHERE caldt >= '2023-01-01'
        """,
        "cityu_wrds_15_cboe_indices.csv": """
            SELECT * FROM cboe.cboe WHERE date >= '2020-01-01'
        """,
    }

    for filename, sql in queries.items():
        try:
            df = db.raw_sql(sql)
            if df is not None and not df.empty:
                df.to_csv(out_dir / filename, index=False)
                print(f"[OK] Saved {len(df)} rows -> {out_dir / filename}")
        except Exception as err:  # noqa: BLE001
            print(f"[SKIP] {filename}: {err}")
    db.close()


# ============================================================================
# 3. CITYU AC3 FINANCE LAB BLOOMBERG EXPORTER (Intraday 5m + Daily)
# ============================================================================
BBG_INTRADAY_5M_TICKERS = [
    "IBIT US Equity", "IBITIV Index", "FBTC US Equity", "FBTCIV Index",
    "ARKB US Equity", "ARKBIV Index", "BITB US Equity", "BITBIV Index",
    "GBTC US Equity", "GBTCIV Index", "HODL US Equity", "HODLIV Index",
    "ETHA US Equity", "ETHAIV Index", "FETH US Equity", "FETHIV Index",
    "ETHE US Equity", "BITO US Equity", "BITOIV Index", "BITX US Equity",
    "3042 HK Equity", "3046 HK Equity", "3008 HK Equity", "3009 HK Equity",
    "3439 HK Equity", "3179 HK Equity", "9042 HK Equity", "9046 HK Equity",
    "BGCI Index", "BGDI Index",
    "XBTUSD Curncy", "XETUSD Curncy", "XSOUSD Curncy", "XBNUSD Curncy",
    "BTCA Comdty", "BTCB Comdty", "MBTA Comdty",
    "ETHA Comdty", "ETHB Comdty", "META Comdty",
    "MSTR US Equity", "COIN US Equity", "MARA US Equity", "RIOT US Equity",
    "CLSK US Equity", "CORZ US Equity", "IREN US Equity", "HOOD US Equity",
    "NVDA US Equity", "SMH US Equity",
    "DBMF US Equity", "CTA US Equity", "RPAR US Equity", "SVOL US Equity", "TAIL US Equity",
    "NQ1 Index", "ES1 Index", "VIX Index", "MOVE Index",
    "USGG2YR Index", "USGG10YR Index", "USGGT10Y Index",
    "USDJPY Curncy", "USDCNH Curncy", "AUDJPY Curncy", "XAU Curncy",
]

BBG_DAILY_UNIVERSE = [
    "BFCIUS Index", "BFCIEU Index", "BFCICN Index",
    "GSUSFCI Index", "GSEUFCI Index", "GSCNFCI Index", "NFCI Index", "ANFCI Index",
    "ECSURPUS Index", "ECSURPEU Index", "ECSURPCN Index",
    "CESIUSD Index", "CESIEUR Index", "CESICHN Index", "CESIJPY Index", "CESIG10 Index",
    "VIX Index", "VIX9D Index", "VIX3M Index", "VIX6M Index",
    "VVIX Index", "SKEW Index", "VXN Index", "RVX Index",
    "MOVE Index", "GVZ Index", "OVX Index",
    "JPMVXYGL Index", "JPMVXYG7 Index", "JPMVXYEM Index",
    "USDJPYV1M BGN Curncy", "USDJPY25R1M BGN Curncy", "USDJPY25B1M BGN Curncy",
    "EURUSDV1M BGN Curncy", "EURUSD25R1M BGN Curncy",
    "USDCNHV1M BGN Curncy", "USDCNH25R1M BGN Curncy",
    "AUDJPYV1M BGN Curncy", "AUDJPY25R1M BGN Curncy",
    "XAUUSDV1M BGN Curncy", "XAUUSD25R1M BGN Curncy",
    "SOFRRATE Index", "EFFR Index", "OBFR Index", "FRAOISUS Index",
    "USBMMY3M Index", "USGG2YR Index", "USGG5YR Index", "USGG10YR Index", "USGG30YR Index",
    "USYC2Y10 Index", "USYC5Y30 Index", "USGGBE05 Index", "USGGBE10 Index",
    "USGGT05Y Index", "USGGT10Y Index",
    "JYBS3 Curncy", "JYBS1 Curncy", "EUBS3 Curncy", "SFBS3 Curncy",
    "LF98OAS Index", "LUACOAS Index",
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


def run_cityu_bloomberg_export(out_dir: Path) -> None:
    """Export Bloomberg 5m TRADE/BID/ASK bars + Daily Macro/ETF/Options tables at CityU AC3 Lab."""
    import blpapi
    import pandas as pd

    out_dir.mkdir(parents=True, exist_ok=True)
    options = blpapi.SessionOptions()
    options.setServerHost("localhost")
    options.setServerPort(8194)
    session = blpapi.Session(options)
    if not session.start() or not session.openService("//blp/refdata"):
        raise SystemExit("Please log into Bloomberg Terminal on this CityU lab PC first.")

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
        pd.DataFrame(daily_rows).to_csv(out_dir / "cityu_bbg_daily_macro_options_flows_full.csv", index=False)

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
    if intraday_rows:
        pd.DataFrame(intraday_rows).to_csv(out_dir / "cityu_bbg_intraday_5m_trade_bid_ask_full.csv", index=False)
    session.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="CityU Master Institutional Data Exporter (LSEG + WRDS + Bloomberg)")
    parser.add_argument(
        "--mode",
        choices=("lseg", "wrds", "bloomberg"),
        default="lseg",
        help="Target CityU database platform (defaults to 'lseg' for direct execution inside CodeBook).",
    )
    parser.add_argument("--out-dir", type=Path, default=Path("."), help="Output directory for exported CSVs.")
    parser.add_argument("--wrds-user", type=str, default=None, help="CityU WRDS username (for --mode wrds).")
    args, _ = parser.parse_known_args()

    if args.mode == "wrds":
        run_cityu_wrds_export(args.out_dir, wrds_username=args.wrds_user)
    elif args.mode == "bloomberg":
        run_cityu_bloomberg_export(args.out_dir)
    else:
        run_cityu_lseg_export(args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
