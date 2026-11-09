# 港大（HKU）+ 城大（CityU）+ 公网对手盘：全量金融数据下载与内嵌脚本总指南（三合一完整版）

> **说明**：本文档已将 **① 港大（HKU）零基础小白下载指南（WRDS + 彭博 Bloomberg + 万得 Wind）**、**② 城大（CityU）LSEG 终端下载指南**、**③ 全系统 9 大类 38 项数据总清单与全部 3 个一键 Python 脚本源码** 完全合并在同一个文件内。转发给同学或去实验室时，**只需要带这 1 个文档即可**。

## 目录
1. **第一部分：港大（HKU）同学零基础傻瓜式下载指南（WRDS + Bloomberg + Wind）**
2. **第二部分：城大（CityU）LSEG Workspace 终端下载指南（CodeBook + 宏观/央行日历）**
3. **第三部分：全系统 9 大类 38 项量化与华尔街对手盘数据总清单（含回测归因结论）**
4. **第四部分：完整内嵌的 3 个一键自动拉取 Python 脚本源码（直接复制即可运行）**

## 第一部分：港大（HKU）同学零基础傻瓜式下载指南

> **给港大同学的说明**：非常感谢帮忙！下面所有操作都拆成了**「网页点选 / Excel 粘贴现成公式 / 复制运行脚本」**，不需要懂金融或量化，总共只需 **20～30 分钟**，完成后把导出的 `.csv` / `.xlsx` 文件打包发回即可。

### 1.1 任务一：在宿舍/图书馆就能下 —— 港大图书馆 WRDS 数据库（网页点选，约 10 分钟）

**准备工作**：连上港大校园网 Wi-Fi（或 HKU VPN），通过港大图书馆进入 **WRDS（Wharton Research Data Services，`https://wrds-www.wharton.upenn.edu/`）**，使用 `@connect.hku.hk` 或 `@hku.hk` 账号登录。

#### 表 1：美股加密 ETF 与科技股期权隐含波动率曲面（OptionMetrics IvyDB US）
1. 登录后，在 WRDS 首页点击 **`OptionMetrics`** ➔ **`IvyDB US`** ➔ **`Volatility Surface (vsurfd)`**。
2. **Step 1（Date Range）**：选择 **`2023-01-01`** 到 **最新日期**。
3. **Step 2（Company Codes）**：选择 **`Ticker`**，直接把下面这一行复制粘贴进去：
   ```text
   IBIT BITO COIN MSTR MARA RIOT NVDA QQQ SPY HYG TLT GLD
   ```
   *（下方筛选条件：`Days to Expiration` 填 `30, 60, 91`；`Call/Put` 选 `Both`）*
4. **Step 3（Query Variables）**：勾选 **`Days to Expiration`**、**`Delta`**、**`Implied Volatility`**、**`Implied Strike Price`**、**`Implied Premium`**、**`Dispersion`**。
5. **Step 4（Output Format）**：选择 **`Comma-delimited text (*.csv)`**，点击 **`Submit Query`**，下载保存为 **`hku_wrds_optionmetrics.csv`**。

#### 表 2：RavenPack 毫秒级机构新闻情绪数据（RavenPack News Analytics）
1. 回到 WRDS 首页，点击 **`RavenPack`** ➔ **`RavenPack Edge / Dow Jones & Web Edition`**。
2. **Step 1（Date Range）**：选择 **`2025-01-01`** 到 **最新日期**。
3. **Step 2（Company / Entity）**：选 **`All`**（若提示数据量过大，可勾选过滤条件 `Relevance >= 90` 且 `Event Similarity Days >= 1`）。
4. **Step 3（Variables）**：勾选 **`TIMESTAMP_UTC`**、**`ENTITY_NAME`**、**`COUNTRY_CODE`**、**`ESS` (Event Sentiment Score)**、**`ENS` (Event Novelty Score)**、**`CSS` (Composite Sentiment Score)**、**`RELEVANCE`**、**`HEADLINE`**。
5. **Step 4（Output）**：选择 **`.csv`**，下载保存为 **`hku_wrds_ravenpack_news.csv`**。

#### 表 3：美股现货 ETF 与加密概念股日频盘口价差与换手（CRSP Daily Stock）
1. 回到 WRDS 首页，点击 **`CRSP`** ➔ **`Stock / Security Files`** ➔ **`Daily Stock File`**。
2. **Step 1（Date Range）**：**`2024-01-01`** 到 **最新日期**。
3. **Step 2（Ticker）**：直接复制粘贴下面这一行：
   ```text
   IBIT FBTC ARKB BITB GBTC ETHA BITO COIN MSTR MARA RIOT CLSK HOOD NVDA QQQ HYG TLT
   ```
4. **Step 3（Variables）**：勾选 **`TICKER`**、**`PRC` (Price)**、**`VOL` (Volume)**、**`RET` (Return)**、**`BID`**、**`ASK`**、**`SHROUT` (Shares Outstanding)**。
5. **Step 4（Output）**：选择 **`.csv`**，下载保存为 **`hku_wrds_crsp_daily.csv`**。

### 1.2 任务二：港大经管学院 Bloomberg Lab（彭博终端 Excel 公式粘贴导出，约 15 分钟）

**地点**：港大梁銶琚楼（K.K. Leung Building）经管学院 **Bloomberg Lab**（配有双屏和彩色贴纸键盘的彭博专用电脑）。

#### 第 1 步：登录彭博并打开带插件的 Excel
1. 双击桌面上的 **Bloomberg** 图标登录账号（若无账号，点 `Create a New Login` 用 `@connect.hku.hk` 邮箱和手机号当场注册，2 分钟完成）。
2. 保持彭博窗口开启，打开 **Microsoft Excel**，确认顶部菜单栏有 **`Bloomberg`** 选项卡。
   *（若没有 `Bloomberg` 选项卡：点电脑左下角开始菜单 ➔ `All Programs` ➔ `Bloomberg` ➔ `Install Office Add-ins` ➔ 点击 `Install` 后重开 Excel。）*

#### 第 2 步：粘贴公式导出「表 4：美股/港股比特币 ETF 盘中 5 分钟实时净值（iNAV）与价格」
新建一个空白工作表（`Sheet1`），把下面 6 行公式分别复制粘贴到对应的第 1 行单元格里按回车（会自动向右、向下铺开过去 180 天的 5 分钟 K 线）：
* **粘贴到 `A1` 单元格**（贝莱德比特币 ETF 5m 价格与成交量）：
  ```excel
  =BDH("IBIT US Equity","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
  ```
* **粘贴到 `G1` 单元格**（贝莱德比特币 ETF 盘中每 5 分钟**实时参考净值 iNAV**）：
  ```excel
  =BDH("IBITIV Index","OPEN,HIGH,LOW,LAST_PRICE","1/5/2026","","BarTp=T","BarSz=5")
  ```
* **粘贴到 `L1` 单元格**（富达比特币 ETF 盘中实时参考净值 iNAV）：
  ```excel
  =BDH("FBTCIV Index","OPEN,HIGH,LOW,LAST_PRICE","1/5/2026","","BarTp=T","BarSz=5")
  ```
* **粘贴到 `Q1` 单元格**（贝莱德以太坊 ETF 5m 价格与成交量）：
  ```excel
  =BDH("ETHA US Equity","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
  ```
* **粘贴到 `W1` 单元格**（贝莱德以太坊 ETF 盘中每 5 分钟**实时参考净值 iNAV**）：
  ```excel
  =BDH("ETHAIV Index","OPEN,HIGH,LOW,LAST_PRICE","1/5/2026","","BarTp=T","BarSz=5")
  ```
* **粘贴到 `AB1` 单元格**（香港华夏比特币现货 ETF `3042.HK` 亚洲时段 5m K 线）：
  ```excel
  =BDH("3042 HK Equity","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
  ```
等待所有数字加载完毕（不再显示 `#N/A Requesting Data...`），另存为 **`hku_bloomberg_etf_inav_5m.xlsx`**。

#### 第 3 步：粘贴公式导出「表 5：华尔街宏观金融条件指数、经济惊奇指数与 IBIT 期权偏度」
新建第二个工作表（`Sheet2`），把下面 7 行公式依次粘贴到对应单元格：
* **`A1` 单元格**（彭博美国金融条件指数）：`=BDH("BFCIUS Index","PX_LAST","20240101","")`
* **`D1` 单元格**（高盛美国金融条件指数）：`=BDH("GSUSFCI Index","PX_LAST","20240101","")`
* **`G1` 单元格**（彭博美国经济惊奇指数）：`=BDH("ECSURPUS Index","PX_LAST","20240101","")`
* **`J1` 单元格**（美债期权波动率指数 MOVE）：`=BDH("MOVE Index","PX_LAST","20240101","")`
* **`M1` 单元格**（黑天鹅尾部偏度指数 SKEW）：`=BDH("SKEW Index","PX_LAST","20240101","")`
* **`P1` 单元格**（贝莱德 IBIT 每日净值、申赎资金流、ATM 隐含波动率、25D 风险逆转偏度、Put/Call 持仓比）：
  ```excel
  =BDH("IBIT US Equity","PX_LAST,FUND_NET_ASSET_VAL,FUND_FLOW,SH_OUT,IMP_VOL_ATM,25D_RISK_REVERSAL_1M,PUT_CALL_OPEN_INTEREST_RATIO","20240110","")
  ```
* **`Y1` 单元格**（美元兑日元 1M 25D 风险逆转偏度）：`=BDH("USDJPY25R1M BGN Curncy","PX_LAST","20240101","")`
等待加载完毕后，另存为 **`hku_bloomberg_macro_and_options.xlsx`**。

### 1.3 任务三：港大 Wind 万得终端（选做，约 5 分钟）
如果实验室电脑装有 **Wind（万得金融终端）**，打开 Wind Excel 插件导出香港 4 只现货加密 ETF（`3042.HK` 华夏比特币、`3046.HK` 华夏以太币、`3008.HK` 博时比特币、`3439.HK` 嘉实比特币）2024 年 4 月上市以来的**每日基金份额申赎变动**与 **`5min` K 线**，保存为 **`hku_wind_hk_crypto_etf.xlsx`**。

## 第二部分：城大（CityU）LSEG Workspace 终端下载指南

### 2.1 LSEG CodeBook 一键自动导出 6 张核心量化表
1. 在城大实验室登录 **LSEG Workspace**，在顶部搜索栏输入 **`CODEBOOK`** 打开内置 Python 云端环境。
2. 新建一个 `.py` 文件，直接复制本文档 **第四部分 4.2 节** 的完整代码并运行，脚本会自动生成以下 6 个 CSV 文件，右键 `Download` 即可：
   * **`lseg_cityu_incremental_bars.csv`**：48 个新增跨资产标的（`.VIX`、`.VIX9D`、`.VIX3M`、`.VXN`、`.SKEW`、`VIXc1`、`VIXc2`、`.MOVE`、`JPY1MO=`、`JPY1MRR=`、`DBMF.P`、`CTA.P`、`KMLM.P`、`RPAR.P`、`SVOL.P`、`TAIL.P`、`SOFR=`、`USONFFE=`、`US2Y10Y=RR`、`US5Y5YF=R`、`JPYCBS3M=`、`EURCBS3M=`、`LQD.P`、`BKLN.P`、`CLc1`、`NVDA.O`、`SMH.O`、`HOOD.O`、`CLSK.O`、`AUDJPY=`、`KRW=`、`BTCc2`、`ETHc2`、`SOL=`、`FBTC.Z`、`ARKB.Z`、`BITB.P` 等）的 `5min` / `1h` / `1d` K 线与做市商盘口微结构列（`BID`、`ASK`、`BID_NUMMOV`、`ASK_NUMMOV`、`VWAP`、`OPINT_1`、`BLKVOLUM`）。
   * **`lseg_cityu_core_refresh_bars.csv`**：原有 23 个核心跨资产标的（`BTC=`、`ETH=`、`BTCc1`、`NQc1`、`ESc1`、`US2YT=RR`、`JPY=`、`HYG.P`、`IBIT.O` 等）最新 5,000 根 `5min` / `1h` / `1d` 增量补齐。
   * **`lseg_cityu_etf_flows_nav.csv`**：12 只现货加密与宏观 ETF 的每日机构净申赎流（`TR.FundNetFlows`）、基金净值（`TR.NETASSETVAL`）与流通份额。
   * **`lseg_cityu_starmine_crowding.csv`**：10 只加密/矿企与 AI 龙头股的空头占比（`TR.ShortInterestPct`）、StarMine 逼空风险评分（`TR.StarMineShortSqueeze`）、分析师修正动量（`TR.StarMineARM`）与机构偏好度（`TR.StarMineSmartHoldings`）。
   * **`lseg_cityu_institutional_ownership.csv`**：13F 机构持仓占比与高管内部人净买卖股数（`TR.InsiderNetSharesTraded`）。
   * **`lseg_cityu_news_headlines.csv`**：路透社机读快讯历史标题与时间戳。

### 2.2 LSEG 桌面日历界面导出（每月初刷新 1 次，共 2 个 Excel）
1. **`Economic Events`（经济日历 `ECOCAL`）**：筛选国家 **US、China、Euro Zone、Japan**，重要度 `Relevance = 2, 3`，时间选**未来 60 天 + 过去 30 天**；在 `Columns` 设置中务必勾选 **`SmartEconomics®`**、**`Predicted Surprise`**、**`Min`**、**`Max`**、**`Count`**、**`Standard Deviation`** 后导出 Excel。
2. **`Central Bank and Political Events`（央行与国债拍卖日历）**：筛选同上四地区，导出 FOMC 议息/纪要、央行行长讲话与美国国债拍卖日程。

## 第三部分：全系统 9 大类 38 项量化与华尔街对手盘数据总清单（含回测归因结论）

| 大类编号与名称 | 核心标的 / 接口 | 周期 | 对应仓库代码模块 | 核心博弈机制与回测实证结论 |
| :--- | :--- | :--- | :--- | :--- |
| **1. 跨资产隐含波动率曲面与期货期限结构** | `.VIX`、`.VIX9D`、`VIXc1`、`VIXc2`、`.MOVE`、`.SKEW`、`JPY1MRR=`、`BTCc1`、`BTCc2`、`ETHc1`、`ETHc2` | `5min` / `1h` / `1d` | `macro_circuit_breaker.py` | 当 `VIXc1 > VIXc2` 期限倒挂或 `.MOVE` 飙升时代表华尔街进入恐慌去杠杆，拦截左侧接刀 |
| **2. 华尔街量化机构仓位（CTA / Vol-Control / CFTC COT）** | `DBMF.P`、`CTA.P`、`RPAR.P`、`SVOL.P`、`0#133741CFTC`（CME BTC COT） | `5min` / `1h` / `1W` | `multi_horizon_and_xs_regime.py` | 识别美盘尾盘（`19:30–20:00 UTC`）Vol-Control 机械砍仓与对冲基金空头回补极值 |
| **3. 美股/港股现货 ETF 盘中实时净值（iNAV）与 StarMine 逼空** | `IBITIV Index`、`ETHAIV Index`、`TR.FundNetFlows`、`TR.StarMineShortSqueeze`、`3042.HK` | `5min` / `1d` | `macro_circuit_breaker.py` | 实时捕捉 AP（Jane Street / Virtu）折价赎回抛压，以及美股开盘 `MSTR`/`COIN` 轧空共振 |
| **4. 全球美元流动性底座、离岸套息与信用债盘口微结构** | `SOFR=`、`USONFFE=`、`JPYCBS3M=`、`HYG.P`、`WALCL`、`WTREGEN`、`RRPONTSYD` | `5min` / `1h` / `1d` | `macro_circuit_breaker.py`<br>`drawdown_and_kelly_budget.py` | **实证**：`BTCc1`、`NQc1`、`HYG.P`（`spread_bps >= 35 bps`）做市商盘口微结构门控使 5m 策略 `PF` 升至 **`1.37`**、最大回撤降 **62%**，1h 全年策略 `PF` 升至 **`1.52`**、最大回撤降 **73%** |
| **5. 宏观日历 SmartEconomics 极值与机读新闻情绪** | `ECOCAL`（含 `Min`/`Max`/`Predicted Surprise`）、WRDS `RavenPack` | 事件级 / 毫秒级 | `macro_circuit_breaker.py`<br>`sources/news.py` | **实证**：突破全市场预测极值 `[Min, Max]` 延长封锁至 `90m`、符合共识 `5m` 快速解封，4 折样本外 `OOS PSR = 99.6%`、`DSR = 94.6%` |
| **6. 币安永续合约对手盘、大户/散户多空比与爆仓流** | `fapi.binance.com`（`fundingRate`、`premiumIndex`、`openInterestHist`、`topLongShortPositionRatio`、`globalLongShortAccountRatio`） | `5min` / `1h` | `perp_microstructure_and_beta_hedge.py`<br>`funding_z_fade.py` | 当散户多空比 `>= 2.20` 且大户分歧比 `<= 0.45` 时触发 `perp_retail_long_trap` 拦截（如实盘检测到 `BNBUSDT` 分歧比 `0.38` 自动拦截） |
| **7. 币安现货 7 大板块多周期 K 线与 L2 盘口深度** | 90+ 核心 USDT 现货对（`5m`/`1h`/`4h`/`1d` 含 `taker_buy_ratio`）+ `@depth20@100ms` | 毫秒级 / `5m` / `1h` / `4h` | `binance_ws_stream.py`<br>`binance_spot_orderbook_tca.py` | 支撑主动买盘毒性过滤（P1）、盘前冲击成本门控（P3）、Post-Only 被动挂单与全市场广度崩盘熔断 |
| **8. 跨交易所现货基准（CCXT）与 Deribit 期权 Gamma 墙** | `sources.ccxt`（Binance/OKX/KuCoin/Bybit）+ Deribit `DVOL` & 期权链持仓 | `5min` / `1h` | `perp_microstructure_and_beta_hedge.py`<br>`sources/ccxt/core.py` | 实时计算 BTC/ETH 期权 **Max Pain（最大痛点）**、**Put/Call Gamma 墙**，拦截负 Gamma + `DVOL` 飙升崩盘 |
| **9. 链上筹码集中度、狙击机器人与代币大额解锁日程** | `solana_helius.py`、`holder_metrics.py`、`bot_markers.py`、DefiLlama Stablecoins & Unlocks | 实时 / 日频 | `crawler/indicators/holder_metrics.py` | 监控 `top10_share`、`dev_balance_delta` 庄家派发、`bot_trade_share` 刷量过滤及 VC 大额解锁前 72h 规避 |

## 第四部分：完整内嵌的 3 个一键自动拉取 Python 脚本源码

### 4.1 脚本一：港大（HKU）Bloomberg & WRDS 一键导出脚本（保存为 `fetch_hku_wrds_and_bloomberg.py`）

* **彭博电脑运行**：`pip install blpapi pandas && python fetch_hku_wrds_and_bloomberg.py --mode bloomberg`
* **港大校园网运行 WRDS**：`pip install wrds pandas && python fetch_hku_wrds_and_bloomberg.py --mode wrds --wrds-user 你的WRDS用户名`

```python
#!/usr/bin/env python3
"""HKU (University of Hong Kong) Bloomberg Terminal & WRDS One-Click Fetcher."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

BBG_INTRADAY_5M_TICKERS = [
    "IBIT US Equity", "IBITIV Index", "FBTC US Equity", "FBTCIV Index",
    "ETHA US Equity", "ETHAIV Index", "BITO US Equity", "BITOIV Index",
    "3042 HK Equity", "3046 HK Equity", "3008 HK Equity", "3439 HK Equity",
    "XBTUSD Curncy", "XETUSD Curncy", "BTCA Comdty", "ETHA Comdty",
    "NQ1 Index", "ES1 Index", "USGG2YR Index", "USDJPY Curncy",
]

BBG_DAILY_MACRO_AND_VOL_TICKERS = [
    "BFCIUS Index", "BFCIEU Index", "GSUSFCI Index", "ECSURPUS Index",
    "ECSURPCN Index", "MOVE Index", "VIX Index", "VVIX Index",
    "SKEW Index", "BGCI Index", "IBIT US Equity", "ETHA US Equity",
    "FBTC US Equity", "MSTR US Equity", "COIN US Equity", "MARA US Equity",
    "NVDA US Equity", "QQQ US Equity", "HYG US Equity", "TLT US Equity",
    "USDJPY25R1M BGN Curncy", "EURUSD25R1M BGN Curncy", "XAUUSD25R1M BGN Curncy",
]

BBG_DAILY_FIELDS = [
    "PX_LAST", "PX_OPEN", "PX_HIGH", "PX_LOW", "PX_VOLUME",
    "FUND_NET_ASSET_VAL", "FUND_FLOW", "SH_OUT",
    "SHORT_INT_RATIO", "SI_PCT_FLOAT", "IMP_VOL_ATM",
    "25D_RISK_REVERSAL_1M", "25D_BUTTERFLY_1M", "PUT_CALL_OPEN_INTEREST_RATIO",
]


def fetch_bloomberg_data(out_dir: Path) -> None:
    import blpapi
    import pandas as pd

    out_dir.mkdir(parents=True, exist_ok=True)
    options = blpapi.SessionOptions()
    options.setServerHost("localhost")
    options.setServerPort(8194)
    session = blpapi.Session(options)
    if not session.start() or not session.openService("//blp/refdata"):
        raise SystemExit("Please log into Bloomberg Terminal first.")

    refdata = session.getService("//blp/refdata")
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
                            except Exception:
                                row[fld] = None
                    daily_rows.append(row)
        if ev.eventType() == blpapi.Event.RESPONSE:
            break

    if daily_rows:
        pd.DataFrame(daily_rows).to_csv(out_dir / "hku_bloomberg_daily_macro_vol_flows.csv", index=False)

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
        pd.DataFrame(intraday_rows).to_csv(out_dir / "hku_bloomberg_intraday_5m_bars.csv", index=False)
    session.stop()


def fetch_wrds_data(out_dir: Path, wrds_username: str | None = None) -> None:
    import pandas as pd
    import wrds

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
                df.to_csv(out_dir / filename, index=False)
        except Exception as err:
            print(f"[SKIP] {filename}: {err}")
    db.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=("bloomberg", "wrds"), required=True)
    p.add_argument("--out-dir", type=Path, default=Path("hku_exported_data"))
    p.add_argument("--wrds-user", type=str, default=None)
    a = p.parse_args()
    if a.mode == "bloomberg":
        fetch_bloomberg_data(a.out_dir)
    else:
        fetch_wrds_data(a.out_dir, wrds_username=a.wrds_user)
```

### 4.2 脚本二：城大（CityU）LSEG Workspace CodeBook 6 合 1 导出脚本（保存为 `fetch_cityu_lseg_incremental.py`）

* **在城大 LSEG CodeBook 中直接运行**：

```python
"""CityU LSEG Workspace Terminal Complete 6-in-1 Master Fetch Script."""

from __future__ import annotations
import pandas as pd
import refinitiv.data as rd

INTRADAY_AND_DAILY_RICS = [
    ".VIX", ".VIX9D", ".VIX3M", ".VXN", ".SKEW", "VIXc1", "VIXc2", ".MOVE",
    "JPY1MO=", "JPY1MRR=", "EUR1MO=", "EUR1MRR=", "XAU1MO=", "XAU1MRR=",
    "DBMF.P", "CTA.P", "KMLM.P", "RPAR.P", "SVOL.P", "TAIL.P",
    "SOFR=", "USONFFE=", "US2Y10Y=RR", "US5Y5YF=R", "JPYCBS3M=", "EURCBS3M=",
    "LQD.P", "BKLN.P", "CLc1", "NVDA.O", "SMH.O", "HOOD.O", "CLSK.O", "CORZ.O", "IREN.O",
    "AUDJPY=", "EURJPY=", "KRW=", "SGD=", "CHF=", "MXN=",
    "BTCc2", "ETHc2", "SOL=", "FBTC.Z", "ARKB.Z", "BITB.P", "GBTC.P", "FETH.Z", "ETHE.P",
]

CORE_REFRESH_RICS = [
    "BTC=", "ETH=", "BTCc1", "ETHc1", "ESc1", "NQc1", "US2YT=RR", "US10YT=RR", "JP10YT=RR",
    ".DXY", "EUR=", "JPY=", "CNH=", "XAU=", "QQQ.O", "TLT.O", "HYG.P",
    "IBIT.O", "ETHA.O", "MSTR.O", "COIN.O", "MARA.O", "RIOT.O",
]

ETF_FLOW_RICS = ["IBIT.O", "FBTC.Z", "ARKB.Z", "BITB.P", "GBTC.P", "ETHA.O", "FETH.Z", "ETHE.P", "HYG.P", "TLT.O", "QQQ.O", "DBMF.P"]
STARMINE_EQ_RICS = ["MSTR.O", "COIN.O", "MARA.O", "RIOT.O", "CLSK.O", "CORZ.O", "IREN.O", "HOOD.O", "NVDA.O", "SMH.O"]


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
            except Exception as err:
                print(f"[SKIP] {ric} ({interval}): {err}")
    if frames:
        pd.concat(frames, ignore_index=True).to_csv(out_csv, index=False)


def main() -> None:
    rd.open_session()
    try:
        _fetch_bars(INTRADAY_AND_DAILY_RICS, "lseg_cityu_incremental_bars.csv", count=5000)
        _fetch_bars(CORE_REFRESH_RICS, "lseg_cityu_core_refresh_bars.csv", count=5000)
        etf_flows = rd.get_data(
            universe=ETF_FLOW_RICS,
            fields=["TR.FundNetFlows.date", "TR.FundNetFlows", "TR.NETASSETVAL", "TR.CompanyMarketCap", "TR.SharesOutstanding"],
            parameters={"SDate": "2024-01-10", "EDate": "0D", "Frq": "D"},
        )
        if etf_flows is not None:
            etf_flows.to_csv("lseg_cityu_etf_flows_nav.csv", index=False)

        starmine_df = rd.get_data(
            universe=STARMINE_EQ_RICS,
            fields=["TR.ShortInterestPct.date", "TR.ShortInterestPct", "TR.StarMineShortSqueeze", "TR.StarMineARM", "TR.StarMineSmartHoldings", "TR.StarMineCombinedAlpha"],
            parameters={"SDate": "2024-01-01", "EDate": "0D", "Frq": "D"},
        )
        if starmine_df is not None:
            starmine_df.to_csv("lseg_cityu_starmine_crowding.csv", index=False)

        own_df = rd.get_data(
            universe=STARMINE_EQ_RICS + ETF_FLOW_RICS[:8],
            fields=["TR.PctOfSharesOutHeld.date", "TR.PctOfSharesOutHeld", "TR.NumOfInstitutionalHolders", "TR.InsiderNetSharesTraded"],
            parameters={"SDate": "2024-01-01", "EDate": "0D", "Frq": "Q"},
        )
        if own_df is not None:
            own_df.to_csv("lseg_cityu_institutional_ownership.csv", index=False)

        news_df = rd.news.get_headlines(
            query="Topic:CRYP OR (FOMC AND rate) OR (SEC AND ETF) OR (Binance OR Tether OR Hack)",
            count=1000,
        )
        if news_df is not None:
            news_df.to_csv("lseg_cityu_news_headlines.csv", index=False)
    finally:
        rd.close_session()


if __name__ == "__main__":
    main()
```

### 4.3 脚本三：本地/服务器公网衍生品、Deribit 期权墙与联储净流动性一键采集脚本

* 直接在本地或服务器上运行仓库内的 `python3 scripts/fetch_public_derivatives_and_counterparty.py`，即可自动拉取币安 26 个永续合约的大户/散户多空比、60d `funding_z`、Deribit BTC/ETH `DVOL` 与最大痛点/Gamma 墙、FRED 美联储净流动性与 DefiLlama 稳定币市值并写入 `data/counterparty/public_derivatives.db`。
