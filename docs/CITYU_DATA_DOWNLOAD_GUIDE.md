# 香港城市大学（CityU）32张机构级量化数据表下载终极指南与全部脚本

本指南专为在香港城市大学（CityU **商学院金融实验室 AC3 / 邵逸夫图书馆 Run Run Shaw Library**）采集全套华尔街机构级数据编写，把城大四大终端（**LSEG Workspace `CODEBOOK`、邵逸夫图书馆 `WRDS`、商学院 `Bloomberg` 彭博终端、以及 `Wind` 万得终端**）可下载的 **32 张核心数据表** 和 **全部一键运行 Python 脚本 / Excel 公式** 完整合成为一份操作手册。

## 一、城大 32 张机构级数据表总览（4 大站点）

| 站点与终端位置 | 表编号与导出文件名 | 核心覆盖资产与独家机构级字段 |
| :--- | :--- | :--- |
| **第一站：城大 LSEG Workspace 终端**<br>（内置 `CODEBOOK` 一键脚本 + `ECOCAL`，**最核心站点**） | **表 1**：`lseg_cityu_incremental_bars.csv`<br>**表 2**：`lseg_cityu_core_refresh_bars.csv`<br>**表 3**：`lseg_cityu_etf_flows_nav.csv`<br>**表 4**：`lseg_cityu_starmine_crowding.csv`<br>**表 5**：`lseg_cityu_institutional_ownership.csv`<br>**表 6**：`lseg_cityu_cftc_cot_positioning.csv`<br>**表 7**：`lseg_cityu_news_headlines.csv`<br>**表 8**：`US_CN_EU_JP_ECOCAL_SmartEconomics.xlsx` | 70+ 跨资产标的（`BTCc1`/`BTCc2`、`NQc1`、`.VIX`、`.MOVE`、`HYG.P`、CTA 基金 `DBMF.P`、外汇偏度 `JPY1MRR=`）的 `5min`/`1h`/`1d` K 线 + **做市商盘口 `BID`/`ASK`/`BID_NUMMOV`/`ASK_NUMMOV`/`VWAP`/`OPINT_1`/`BLKVOLUM`**；Lipper ETF 每日申赎流与 NAV；**StarMine 1–100 逼空风险评分 (`TR.StarMineShortSqueeze`)**；13F 机构持仓；CFTC COT 杠杆基金周度持仓；路透机读新闻；**SmartEconomics® 预测惊奇度与分析师最高/最低预测极值** |
| **第二站：城大邵逸夫图书馆 WRDS**<br>（连城大校园网 Wi-Fi 即可在自己电脑直接跑脚本导出） | **表 9**：`cityu_wrds_01_taq_iid.csv`（NYSE TAQ 盘口毒性）<br>**表 10**：`cityu_wrds_02_taq_ctm_sample.csv`（纳秒级逐笔）<br>**表 11~14**：`cityu_wrds_03..06_optionmetrics_*.csv`（期权波动率曲面與Greeks）<br>**表 15**：`cityu_wrds_07_ravenpack_news.csv`（毫秒级新闻情绪）<br>**表 16**：`cityu_wrds_08_trace_convertible_bonds.csv`（可转债套利）<br>**表 17~18**：`cityu_wrds_09..10_markit_short*.csv`（融券费率与逼空）<br>**表 19~21**：`cityu_wrds_11..13_13f_s12_insider.csv`（13F与高管买卖）<br>**表 22~23**：`cityu_wrds_14..15_crsp_cboe.csv`（CRSP国债与CBOE波动率） | 美股加密 ETF（`IBIT`/`ETHA`）与概念股（`MSTR`/`COIN`/`NVDA`）的 **NYSE TAQ 实际价差 `EffectiveSpread` 与价格冲击 `PriceImpact`**；OptionMetrics 30/60/90 天隐含波动率曲面与 `Gamma`/`Vega`；RavenPack 情绪得分 `ESS`；FINRA TRACE 矿企可转债机构暗盘买卖；**Markit 融券借贷费率 `IndicativeFee` 与券池利用率 `Utilization`** |
| **第三站：城大商学院 Bloomberg 终端**<br>（AC3 金融交易实验室，支持脚本或 Excel 公式导出） | **表 24~25**：`cityu_bbg_intraday_5m_trade_bid_ask_full.csv`<br>**表 26~27**：`cityu_bbg_daily_macro_options_flows_full.csv`<br>**表 28**：`cityu_bbg_eco_calendar.xlsx` | 18 只美/港现货加密 ETF **盘中每 5 分钟官方实时净值 `IBITIV Index`（精准算 AP 一级申赎折溢价）**；全品种期权 25-Delta 风险逆转偏度 `25D_RISK_REVERSAL_1M`；高盛/彭博金融条件指数 `GSUSFCI`/`BFCIUS`、花旗惊奇指数 `CESIUSD`、交叉货币基差 `JYBS3` |
| **第四站：城大 Wind 万得终端**<br>（亚洲早盘与香港本地加密生态专属） | **表 29**：`cityu_wind_hk_spot_etf.xlsx`<br>**表 30**：`cityu_wind_hk_web3_equities.xlsx`<br>**表 31**：`cityu_wind_asia_liquidity.xlsx`<br>**表 32**：`cityu_wind_asia_indices_5m.xlsx` | 香港 6 只现货 BTC/ETH ETF（`3042.HK` 华夏、`3046.HK` 嘉实、`3008.HK` 博时等港币+美元双柜台）5m K 线与 IOPV；香港离岸人民币 `CNH HIBOR`、港元 `HKD HIBOR` 与金管局总结余；恒生科指 `HSTECH.HI` 5m K 线 |

## 二、第一站操作与脚本：城大 LSEG Workspace 终端（表 1 ~ 表 8，10 分钟搞定）

### 步骤 1：在 LSEG `CODEBOOK` 中一键导出表 1 ~ 表 7
1. 在城大实验室电脑打开 **LSEG Workspace** 并登录。
2. 在顶部搜索栏输入 **`CODEBOOK`** 按回车，打开内置 Python 编程环境。
3. 点击左上角 **`+`（New File）➔ `Python File`**，将下面这段完整代码复制粘贴进去，点击顶部 **`Run`**（或打开 Terminal 输入 `python fetch_cityu_lseg_incremental.py`）。运行完毕后在左侧文件栏右键 **`Download`** 下载生成的 7 个 CSV 文件：

```python
#!/usr/bin/env python3
"""脚本 1：城大 LSEG Workspace CodeBook 一键全自动拉取脚本（导出表 1 ~ 表 7 共 7 个 CSV）"""

from __future__ import annotations
import pandas as pd
import refinitiv.data as rd

INTRADAY_AND_DAILY_RICS = [
    ".VIX", ".VIX9D", ".VIX3M", ".VXN", ".SKEW", "VIXc1", "VIXc2", ".MOVE",
    "JPY1MO=", "JPY1MRR=", "EUR1MO=", "EUR1MRR=", "XAU1MO=", "XAU1MRR=",
    "DBMF.P", "CTA.P", "KMLM.P", "RPAR.P", "SVOL.P", "TAIL.P",
    "SOFR=", "USONFFE=", "US2Y10Y=RR", "US5Y5YF=R", "JPYCBS3M=", "EURCBS3M=",
    "LQD.P", "BKLN.P", "CLc1",
    "NVDA.O", "SMH.O", "HOOD.O", "CLSK.O", "CORZ.O", "IREN.O",
    "AUDJPY=", "EURJPY=", "KRW=", "SGD=", "CHF=", "MXN=",
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
    "0#133741CFTC",  # CME Bitcoin CFTC COT
    "0#146021CFTC",  # CME Ether CFTC COT
    "0#209742CFTC",  # CME E-mini Nasdaq-100 CFTC COT
    "0#13874ACFTC",  # CME E-mini S&P 500 CFTC COT
    "0#097741CFTC",  # CME Japanese Yen CFTC COT
    "0#043602CFTC",  # CBOT 10Y Treasury Note CFTC COT
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
            except Exception as err:
                print(f"[SKIP] {ric} ({interval}): {err}")
    if frames:
        pd.concat(frames, ignore_index=True).to_csv(out_csv, index=False)
        print(f"[SAVED] {out_csv}")


def main() -> None:
    rd.open_session()
    try:
        # 表 1：新增 47 个跨资产/波动率期限/CTA/次月期货 5min/1h/1d 盘口与K线
        _fetch_bars(INTRADAY_AND_DAILY_RICS, "lseg_cityu_incremental_bars.csv", count=5000)

        # 表 2：核心 23 个跨资产标的 5min/1h/1d 续期更新（含 BID/ASK/BID_NUMMOV/ASK_NUMMOV/VWAP）
        _fetch_bars(CORE_REFRESH_RICS, "lseg_cityu_core_refresh_bars.csv", count=5000)

        # 表 3：美股现货 BTC/ETH ETF 与宏观基金每日净申赎流 + NAV 净值
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
                print("[SAVED] lseg_cityu_etf_flows_nav.csv")
        except Exception as err:
            print(f"[SKIP] ETF flows: {err}")

        # 表 4：LSEG StarMine 独家逼空风险评分（1-100）、机构聪明钱持仓与空头占比
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
                print("[SAVED] lseg_cityu_starmine_crowding.csv")
        except Exception as err:
            print(f"[SKIP] StarMine crowding: {err}")

        # 表 5：13F 机构持仓占比与内部高管净买卖
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
                print("[SAVED] lseg_cityu_institutional_ownership.csv")
        except Exception as err:
            print(f"[SKIP] Institutional ownership: {err}")

        # 表 6：CFTC COT 杠杆对冲基金 vs 资管机构周度持仓报告
        try:
            cot_frames = []
            for c_ric in CFTC_COT_RICS:
                c_df = rd.get_history(universe=c_ric, interval="1W", count=260)
                if c_df is not None and not c_df.empty:
                    c_df = c_df.reset_index()
                    c_df["cot_family_ric"] = c_ric
                    cot_frames.append(c_df)
            if cot_frames:
                pd.concat(cot_frames, ignore_index=True).to_csv("lseg_cityu_cftc_cot_positioning.csv", index=False)
                print("[SAVED] lseg_cityu_cftc_cot_positioning.csv")
        except Exception as err:
            print(f"[SKIP] CFTC COT positioning: {err}")

        # 表 7：路透社机读宏观与加密监管/ETF/黑客突发新闻标题流
        try:
            news_df = rd.news.get_headlines(
                query="Topic:CRYP OR (FOMC AND rate) OR (SEC AND ETF) OR (Binance OR Tether OR Hack)",
                count=1000,
            )
            if news_df is not None:
                news_df.to_csv("lseg_cityu_news_headlines.csv", index=False)
                print("[SAVED] lseg_cityu_news_headlines.csv")
        except Exception as err:
            print(f"[SKIP] News headlines: {err}")
    finally:
        rd.close_session()


if __name__ == "__main__":
    main()
```

### 步骤 2：在 LSEG `ECOCAL` 中导出表 8（含 SmartEconomics 预测惊奇度与 Min/Max 极值）
1. 在 LSEG Workspace 顶部搜索栏输入 **`ECOCAL`** 按回车打开经济日历。
2. 国家勾选 **`United States, China, Euro Zone, Japan`**，重要性勾选 **`High (3 stars)` & `Medium (2 stars)`**。
3. 点击右上角齿轮 **`Columns`（列设置）**，除常规字段外务必勾选：**`SmartEconomics®`、`Predicted Surprise`、`Standard Deviation`、`Min`（最低预测值）、`Max`（最高预测值）、`Count`**，点击右上角 **Excel 图标** 导出。
4. 切换到顶部的 **`Central Bank Events`**（央行官员讲话与议息会议）和 **`Government Debt Auctions`**（美债 2Y/10Y/30Y 拍卖）标签页，同样点击 Excel 导出。

## 三、第二站操作与脚本：城大邵逸夫图书馆 WRDS（表 9 ~ 表 23，连校园网一键跑）

在城大校园网 Wi-Fi 下登录邵逸夫图书馆 **WRDS**，在自己笔记本电脑上运行以下脚本，3 分钟内自动拉取 **NYSE TAQ 盘口毒性、OptionMetrics 期权曲面、RavenPack 新闻情绪、TRACE 矿企可转债、Markit 融券费率等全部 15 张表**：

```python
#!/usr/bin/env python3
"""脚本 2：城大邵逸夫图书馆 WRDS 15 张机构级量化表一键拉取脚本
用法：
  pip install wrds pandas
  python fetch_cityu_wrds.py --wrds-user 你的城大WRDS用户名
"""

from __future__ import annotations
import argparse
from pathlib import Path
import pandas as pd
import wrds


def run_cityu_wrds_export(out_dir: Path, wrds_username: str | None = None) -> None:
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
        except Exception as err:
            print(f"[SKIP] {filename}: {err}")
    db.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--out-dir", type=Path, default=Path("cityu_wrds_exports"))
    p.add_argument("--wrds-user", type=str, default=None)
    args = p.parse_args()
    run_cityu_wrds_export(args.out_dir, wrds_username=args.wrds_user)
```

## 四、第三站操作与脚本：城大商学院（AC3）Bloomberg 彭博实验室（表 24 ~ 表 28）

在城大商学院彭博终端机上，既可以直接运行 `python scripts/fetch_cityu_lseg_incremental.py --mode bloomberg` 全自动拉取 65+ 个标的的 5 分钟 `TRADE`/`BID`/`ASK` 盘口 K 线与日频全字段，也可以直接打开 Excel 粘贴以下公式块：

* **Excel 公式块 1（现货 ETF 5 分钟价格 vs 官方实时 iNAV 盘中净值折溢价）**：
  ```excel
  =BDH("IBIT US Equity","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("IBITIV Index","OPEN,HIGH,LOW,LAST_PRICE","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("FBTC US Equity","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("FBTCIV Index","OPEN,HIGH,LOW,LAST_PRICE","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("ETHA US Equity","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("ETHAIV Index","OPEN,HIGH,LOW,LAST_PRICE","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("BTCA Comdty","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("BTCB Comdty","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
  ```
* **Excel 公式块 2（日频 ETF 申赎流、空头占比 `SI_PCT_FLOAT` 与期权 25D 偏度 `25D_RISK_REVERSAL_1M`）**：
  ```excel
  =BDH("IBIT US Equity","PX_LAST,FUND_NET_ASSET_VAL,FUND_FLOW,SH_OUT,SI_PCT_FLOAT,IMP_VOL_ATM,25D_RISK_REVERSAL_1M,25D_BUTTERFLY_1M,PUT_CALL_OPEN_INTEREST_RATIO","20240110","")
  =BDH("MSTR US Equity","PX_LAST,PX_VOLUME,SI_PCT_FLOAT,SHORT_INT_RATIO,IMP_VOL_ATM,25D_RISK_REVERSAL_1M,25D_BUTTERFLY_1M,PUT_CALL_OPEN_INTEREST_RATIO","20230101","")
  =BDH("COIN US Equity","PX_LAST,PX_VOLUME,SI_PCT_FLOAT,SHORT_INT_RATIO,IMP_VOL_ATM,25D_RISK_REVERSAL_1M,25D_BUTTERFLY_1M,PUT_CALL_OPEN_INTEREST_RATIO","20230101","")
  ```

## 五、回宿舍后一键合入实盘数据库脚本（`upgrade_lseg_macro_db.py`）

把从城大下载回来的 CSV/Excel 放进项目目录后，在自己 Mac 终端运行以下命令，即可将所有新增的做市商盘口、SmartEconomics 预测极值与跨资产 K 线一键合入实盘 `data/lseg/lseg_macro.db`：

```bash
# 一键升级并合入城大下载的 LSEG 宏观与跨资产数据库
/usr/local/bin/python3 /tmp/trade_sync/scripts/upgrade_lseg_macro_db.py \
  --source /Users/yangyuxuan/Desktop/lseg_exports/lseg_macro.db \
  --dest /tmp/trade_sync/data/lseg/lseg_macro.db
```
