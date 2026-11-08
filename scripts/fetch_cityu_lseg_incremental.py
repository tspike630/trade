"""CityU LSEG Workspace Terminal Incremental Fetch Script.

Run inside LSEG Workspace CodeBook or a local Python environment attached to a
running LSEG Workspace desktop session (``pip install refinitiv-data``).
"""

from __future__ import annotations

import pandas as pd

try:
    import refinitiv.data as rd
except ImportError as exc:
    raise SystemExit("Install refinitiv-data (`pip install refinitiv-data`) inside LSEG Workspace.") from exc


INTRADAY_RICS = [
    # 1. Implied Volatility & Tail-Risk Term Structure
    ".VIX",
    ".VXN",
    "VIXc1",
    "VIXc2",
    ".MOVE",
    "JPY1MO=",
    "JPY1MRR=",
    "XAU1MO=",
    # 2. Sector Lead-Lag & Wall Street CTA Positioning Proxies
    "NVDA.O",
    "SMH.O",
    "HOOD.O",
    "DBMF.P",
    # 3. Asian & Global Carry FX Thermometers
    "AUDJPY=",
    "KRW=",
    # 4. CME 2nd-Month Futures & Spot ETFs / L1 Base
    "BTCc2",
    "ETHc2",
    "SOL=",
    "FBTC.Z",
    "ARKB.Z",
    "BITB.P",
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
]

STARMINE_EQ_RICS = [
    "MSTR.O",
    "COIN.O",
    "MARA.O",
    "RIOT.O",
    "CLSK.O",
    "HOOD.O",
    "NVDA.O",
]


def main() -> None:
    rd.open_session()
    try:
        frames = []
        for interval in ("5min", "1h", "1d"):
            for ric in INTRADAY_RICS:
                try:
                    df = rd.get_history(universe=ric, interval=interval, count=5000)
                    if df is not None and not df.empty:
                        df = df.reset_index()
                        df["ric"] = ric
                        df["interval"] = interval
                        frames.append(df)
                        print(f"[OK] {ric} ({interval}): {len(df)} rows")
                except Exception as err:  # noqa: BLE001
                    print(f"[SKIP] {ric} ({interval}): {err}")
        if frames:
            pd.concat(frames, ignore_index=True).to_csv(
                "lseg_cityu_incremental_bars.csv", index=False
            )

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

        starmine_df = rd.get_data(
            universe=STARMINE_EQ_RICS,
            fields=[
                "TR.ShortInterestPct.date",
                "TR.ShortInterestPct",
                "TR.StarMineShortSqueeze",
                "TR.StarMineARM",
            ],
            parameters={"SDate": "2024-01-01", "EDate": "0D", "Frq": "D"},
        )
        if starmine_df is not None:
            starmine_df.to_csv("lseg_cityu_starmine_crowding.csv", index=False)
    finally:
        rd.close_session()


if __name__ == "__main__":
    main()
