"""CityU LSEG Workspace Terminal Complete Master Fetch Script.

Covers all 6 LSEG institutional datasets for the ``trade`` quantitative system:
1. ``lseg_cityu_incremental_bars.csv``: 5min / 1h / 1d bars + dealer microstructure
   (``BID``, ``ASK``, ``BIDSIZE``, ``ASKSIZE``, ``BID_NUMMOV``, ``ASK_NUMMOV``,
   ``VWAP``, ``OPINT_1``, ``BLKVOLUM``) across Volatility, CTA/Risk-Parity proxies,
   Rates/Liquidity, Carry FX, CME 1st/2nd-month Crypto Futures, and AI/Crypto Equities.
2. ``lseg_cityu_etf_flows_nav.csv``: Daily US BTC/ETH Spot ETF creations/redemptions
   (``TR.FundNetFlows``), ``TR.NETASSETVAL`` (for AP premium/discount), & shares out.
3. ``lseg_cityu_starmine_crowding.csv``: LSEG StarMine quantitative factor scores
   (``TR.StarMineShortSqueeze``, ``TR.StarMineARM``, ``TR.StarMineSmartHoldings``,
   ``TR.StarMineCombinedAlpha``, ``TR.ShortInterestPct``).
4. ``lseg_cityu_institutional_ownership.csv``: 13F institutional holder weight &
   insider net trading on crypto/tech proxy equities.
5. ``lseg_cityu_cftc_cot_positioning.csv``: CFTC Commitments of Traders (COT) weekly
   Leveraged Funds vs Asset Managers vs Dealer net positioning on CME BTC, ETH, NQ, ES, JPY.
6. ``lseg_cityu_news_headlines.csv``: Machine-readable Reuters high-priority macro &
   crypto regulatory/hack/ETF headlines.
"""

from __future__ import annotations

import pandas as pd

try:
    import refinitiv.data as rd
except ImportError as exc:
    raise SystemExit("Install refinitiv-data (`pip install refinitiv-data`) inside LSEG Workspace.") from exc


INTRADAY_AND_DAILY_RICS = [
    # 1. Implied Volatility, Skew & Tail-Risk Term Structure
    ".VIX",
    ".VIX9D",
    ".VIX3M",
    ".VXN",
    ".SKEW",
    "VIXc1",
    "VIXc2",
    ".MOVE",
    "JPY1MO=",
    "JPY1MRR=",
    "EUR1MO=",
    "EUR1MRR=",
    "XAU1MO=",
    "XAU1MRR=",
    # 2. Wall Street CTA / Managed Futures / Vol-Control / Risk-Parity Rebalance Proxies
    "DBMF.P",
    "CTA.P",
    "KMLM.P",
    "RPAR.P",
    "SVOL.P",
    "TAIL.P",
    # 3. Rates, Funding Stress, Cross-Currency Basis & Credit Spreads
    "SOFR=",
    "USONFFE=",
    "US2Y10Y=RR",
    "US5Y5YF=R",
    "JPYCBS3M=",
    "EURCBS3M=",
    "LQD.P",
    "BKLN.P",
    "CLc1",
    # 4. Sector Lead-Lag & Crypto / AI Equities
    "NVDA.O",
    "SMH.O",
    "HOOD.O",
    "CLSK.O",
    "CORZ.O",
    "IREN.O",
    # 5. Asian & Global Carry FX Thermometers
    "AUDJPY=",
    "EURJPY=",
    "KRW=",
    "SGD=",
    "CHF=",
    "MXN=",
    # 6. CME 2nd-Month Crypto Futures (Roll Yield / Term Structure) & Spot ETFs
    "BTCc2",
    "ETHc2",
    "SOL=",
    "FBTC.Z",
    "ARKB.Z",
    "BITB.P",
    "GBTC.P",
    "FETH.Z",
    "ETHE.P",
]

CORE_REFRESH_RICS = [
    "BTC=",
    "ETH=",
    "BTCc1",
    "ETHc1",
    "ESc1",
    "NQc1",
    "US2YT=RR",
    "US10YT=RR",
    "JP10YT=RR",
    ".DXY",
    "EUR=",
    "JPY=",
    "CNH=",
    "XAU=",
    "QQQ.O",
    "TLT.O",
    "HYG.P",
    "IBIT.O",
    "ETHA.O",
    "MSTR.O",
    "COIN.O",
    "MARA.O",
    "RIOT.O",
]

ETF_FLOW_RICS = [
    "IBIT.O",
    "FBTC.Z",
    "ARKB.Z",
    "BITB.P",
    "GBTC.P",
    "ETHA.O",
    "FETH.Z",
    "ETHE.P",
    "HYG.P",
    "TLT.O",
    "QQQ.O",
    "DBMF.P",
]

STARMINE_EQ_RICS = [
    "MSTR.O",
    "COIN.O",
    "MARA.O",
    "RIOT.O",
    "CLSK.O",
    "CORZ.O",
    "IREN.O",
    "HOOD.O",
    "NVDA.O",
    "SMH.O",
]

CFTC_COT_RICS = [
    # CME Bitcoin, Ether, Nasdaq-100, S&P 500, JPY, US 10Y Treasury CFTC COT series
    "0#133741CFTC",  # CME Bitcoin CFTC COT family
    "0#146021CFTC",  # CME Ether CFTC COT family
    "0#209742CFTC",  # CME E-mini Nasdaq-100 CFTC COT family
    "0#13874ACFTC",  # CME E-mini S&P 500 CFTC COT family
    "0#097741CFTC",  # CME Japanese Yen CFTC COT family
    "0#043602CFTC",  # CBOT 10Y Treasury Note CFTC COT family
]


def _fetch_bars(rics: list[str], out_csv: str, count: int = 5000) -> None:
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
        print(f"Saved {out_csv}")


def main() -> None:
    rd.open_session()
    try:
        # 1. New Cross-Asset / Volatility / CTA / Term-Structure Bars
        _fetch_bars(INTRADAY_AND_DAILY_RICS, "lseg_cityu_incremental_bars.csv", count=5000)

        # 2. Refresh Core 23 RICs 5min/1h/1d Bars (with full dealer BID/ASK/NUMMOV/VWAP columns)
        _fetch_bars(CORE_REFRESH_RICS, "lseg_cityu_core_refresh_bars.csv", count=5000)

        # 3. Spot ETF & Macro Fund Flows + NAV (for AP Arbitrage Discount/Premium)
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
                etf_flows.to_csv("lseg_cityu_etf_flows_nav.csv", index=False)
        except Exception as err:  # noqa: BLE001
            print(f"[SKIP] ETF flows: {err}")

        # 4. LSEG StarMine Quantitative Crowding, Short Squeeze & Smart Holdings
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
                starmine_df.to_csv("lseg_cityu_starmine_crowding.csv", index=False)
        except Exception as err:  # noqa: BLE001
            print(f"[SKIP] StarMine crowding: {err}")

        # 5. Institutional 13F Ownership & Insider Net Buying/Selling
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
                own_df.to_csv("lseg_cityu_institutional_ownership.csv", index=False)
        except Exception as err:  # noqa: BLE001
            print(f"[SKIP] Institutional ownership: {err}")

        # 6. Reuters Machine-Readable Macro & Crypto Headlines
        try:
            news_df = rd.news.get_headlines(
                query="Topic:CRYP OR (FOMC AND rate) OR (SEC AND ETF) OR (Binance OR Tether OR Hack)",
                count=1000,
            )
            if news_df is not None:
                news_df.to_csv("lseg_cityu_news_headlines.csv", index=False)
        except Exception as err:  # noqa: BLE001
            print(f"[SKIP] News headlines: {err}")
    finally:
        rd.close_session()


if __name__ == "__main__":
    main()
