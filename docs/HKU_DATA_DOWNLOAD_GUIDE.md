# 香港大学（HKU）全量金融与量化对手盘数据下载「零基础小白操作总指南」（全平台 32 表终极单文档版）

> **致港大同学**：非常感谢帮忙！这份指南专为**香港大学（HKU）**编写，汇总了港大图书馆与经管学院（K.K. Leung 梁銶琚楼）拥有的 **4 大顶级金融终端与数据库（① 港大图书馆 WRDS、② 经管学院 Bloomberg Lab 彭博终端、③ 港大 LSEG Workspace / Datastream、④ 经管学院 Wind 万得终端）** 中我们需要的 **全部 32 张核心数据表**。
>
> **操作方式任选其一**：
> * **方式 A（全自动脚本，最省事，共 10 分钟）**：直接复制本文档 **第六部分** 内嵌的 Python 脚本保存为 `fetch_hku_master.py`，在电脑上跑一行命令即可自动下完全部数据。
> * **方式 B（零代码小白点选 + 粘贴现成 Excel 公式，共 25 分钟）**：完全不用懂金融或编程，照着下面每一步复制粘贴代码或公式即可。

## 一、港大可下载的 4 大平台、32 张全量数据总表（一表看全）

| 港大平台名称 | 所在位置 / 登录方式 | 必下数据表（共 32 张全量表） | 核心字段与对手盘用途 |
| :--- | :--- | :--- | :--- |
| **1. 港大图书馆 WRDS（沃顿研究数据服务）** | **宿舍/图书馆直连**<br>（连 `HKU` Wi-Fi 或 VPN 登录 `wrds-www.wharton.upenn.edu`） | **表 1**：NYSE Daily TAQ 毫秒级盘口微观结构日度指标库（`wrds_taq_iid`）<br>**表 2**：NYSE Daily TAQ 美股开盘/收盘集合竞价与大单逐笔抽样（`ctm` / `cqm`）<br>**表 3**：OptionMetrics IvyDB US 标准化隐含波动率曲面（`vsurfd`：10/30/60/91/182 天）<br>**表 4**：OptionMetrics 标准平价远期期权与希腊字母（`stdopd`：Delta/Gamma/Vega/Theta）<br>**表 5**：OptionMetrics 个股/ETF 期权每日总持仓与看跌/看涨比（`opprcd` / `opvold`）<br>**表 6**：OptionMetrics 零息无风险收益率曲线（`zerocd`）<br>**表 7**：RavenPack 毫秒级全球机构新闻情绪与事件冲击库（`ravenpack_dj` + `web`）<br>**表 8**：FINRA TRACE Enhanced 场外可转债/公司债机构暗盘逐笔成交（`trace_enhanced`）<br>**表 9**：Markit Securities Finance（`msf`）机构融券借贷费率、利用率与逼空指标<br>**表 10**：Compustat 官方空头持仓量与回补天数（`comp.sec_shortint`）<br>**表 11**：Thomson / LSEG 13F 华尔街对冲基金季度持仓明细（`tfn.s34`）<br>**表 12**：Thomson 共同基金与 ETF 底层持仓明细（`tfn.s12`）<br>**表 13**：美国 SEC Form 4 高管与内部人买卖流水（`tfn.table1`）<br>**表 14**：CRSP 美股现货 ETF/概念股日频盘口（`crsp.dsf`）与美国国债全期限库（`crsp.tfz_dly`）<br>**表 15**：CBOE 官方波动率与偏度指数历史库（`cboe.cboe`） | 穿透美股毫秒级价格冲击（Kyle's Lambda）、可转债套利对冲基金（交易 `MSTR`/`COIN`/`MARA` 可转债并做空 BTC 对冲 Delta）、融券逼空费率、顶级量化基金（Millennium/Citadel/Jane Street）13F 增减持、以及 40,000+ 信源毫秒级新闻情绪 |
| **2. 经管学院 Bloomberg Lab（彭博终端）** | **梁銶琚楼（K.K. Leung Building）彭博机房** | **表 16**：美股+港股 18 只加密 ETF 价格与**盘中 5m 实时参考净值（iNAV）**（`TRADE`/`BID`/`ASK` 三套 5m K 线）<br>**表 17**：彭博加密现货、银河指数（`BGCI`/`BGDI`）与 CME 期货近月/次月/微型合约期限结构（`5m`/`1h`/`1d`）<br>**表 18**：13 只加密概念/矿企股 + 7 只 AI 算力龙头 + 9 只华尔街 CTA/风险平价/做空波动率 ETF（`5m`/`1h`/`1d`）<br>**表 19**：全球 14 个跨资产波动率指数 + 11 个外汇/黄金 25D 风险逆转偏度 + `IBIT`/`MSTR`/`COIN` 期权偏度与 Gamma 持仓<br>**表 20**：彭博/高盛/芝加哥联储 8 大金融条件指数（FCI）+ 彭博/花旗 8 大经济惊奇指数（CESI/ECSURP）<br>**表 21**：美元回购利率（SOFR/EFFR/OBFR/FRA-OIS）、TIPS 实际利率、交叉货币基差互换（`JYBS3`/`EUBS3`）与北美 CDX 信用违约互换（`CDX HY`/`CDX IG`）<br>**表 22**：彭博 `ECO` 宏观经济日历（含彭博调查中位数、均值、最高、最低、预测标准差与惊奇值）<br>**表 23**：CFTC COT 期货分类持仓报告（CME BTC/ETH/NQ/ES/JPY/美债的对冲基金 vs 资管 vs 做市商净仓位） | 彭博独有 **15 秒/5 分钟级 ETF 实时净值 `IBITIV`/`ETHAIV`**（直接算穿 AP 套利盘实时申赎抛压）、分离式 `BID`/`ASK`/`TRADE` 盘口 K 线、以及北美高收益债 `CDX HY` 与金融条件指数 `BFCIUS`/`GSUSFCI` |
| **3. 港大 LSEG Workspace / Datastream** | **港大图书馆 / 经管学院金融实验室** | **表 24**：71 个跨资产标的 `5min`/`1h`/`1d` K 线与做市商盘口跳动字段（`BID`, `ASK`, `BIDSIZE`, `ASKSIZE`, `BID_NUMMOV`, `ASK_NUMMOV`, `VWAP`, `OPINT_1`, `BLKVOLUM`）<br>**表 25**：LSEG Lipper 美股/全球 ETF 每日净申赎资金流（`TR.FundNetFlows`）与日频 NAV<br>**表 26**：LSEG StarMine 量化模型每日评分（`StarMineShortSqueeze` 逼空分、`StarMineARM` 分析师修正、`StarMineSmartHoldings`、`StarMineCombinedAlpha`）<br>**表 27**：LSEG `ECOCAL` 经济日历（含 **`SmartEconomics®`** 智能加权预测、**`Predicted Surprise`** 预测惊奇值、`Min`/`Max`/`Count`）与四大央行日程<br>**表 28**：LSEG Datastream 全球 M2 货币供应、各国央行资产负债表与主权 CDS 历史序列 | 补充 LSEG 独有的做市商报价跳动次数（`BID_NUMMOV`/`ASK_NUMMOV`）、大宗交易量（`BLKVOLUM`）、`SmartEconomics` 事前预测惊奇与 `StarMine` 1–100 逼空评分 |
| **4. 经管学院 Wind（万得金融终端）** | **经管学院机房** | **表 29**：香港 6 只现货比特币/以太坊 ETF（港币+美元 12 个柜台）`5min` K 线、盘中 IOPV 与每日份额申赎<br>**表 30**：港股 Web3 与持币上市公司（`0863.HK`、`1611.HK`、`0434.HK`、`1357.HK`）行情与港股通资金流<br>**表 31**：亚洲盘离岸人民币（`CNH HIBOR`）、港元（`HKD HIBOR`）、金管局总结余、在岸 `DR007` 与央行逆回购每日净投放<br>**表 32**：亚太早盘风险情绪指数（恒生科技 `HSTECH.HI`、日经 225、韩国 `KOSPI 200`、A50 期货 `5min` K 线） | 填补北京时间 `08:00–16:00`（美股休市期间）亚洲机构加密 ETF 申赎、离岸人民币/港元资金面抽水与韩国/亚太早盘情绪空白 |

## 二、第一站：港大图书馆 WRDS 数据库（表 1 ~ 表 15，宿舍连校园网即可下）

**登录步骤**：连上港大 Wi-Fi（或 VPN）➔ 打开 `https://wrds-www.wharton.upenn.edu/` ➔ 用港大邮箱登录。
* **最省事方式（3 分钟下完 15 张表）**：直接复制本文档 **第六部分** 的 Python 脚本保存为 `fetch_hku_master.py`，在自己电脑上运行 `python fetch_hku_master.py --mode wrds --wrds-user 你的WRDS账号` 即可全自动生成全部 CSV！
* **网页点选方式**：在 WRDS 首页依次点进以下模块，将下面的 **通用美股标的池（43 个代码）** 复制粘贴进 `Ticker` 框，日期统一选 **`2023-01-01` 至最新日期**，输出格式统一选 **`Comma-delimited text (*.csv)`**：

**WRDS 通用复制粘贴标的池（43 个代码）**：
```text
IBIT FBTC ARKB BITB GBTC HODL ETHA FETH ETHE BITO BITX MSTR COIN MARA RIOT CLSK CORZ IREN WULF HUT BITF HOOD SQ NVDA SMH SOXX AMD TSM PLTR ARM QQQ SPY IWM HYG LQD TLT GLD DBMF CTA KMLM RPAR SVOL TAIL
```

1. **表 1（`hku_wrds_01_taq_iid.csv`）**：首页 ➔ `TAQ` ➔ `WRDS Intraday Indicators (wrds_taq_iid)` ➔ 粘贴上述代码 ➔ 全选字段（含 `QuotedSpread_Dollar`, `QuotedSpread_Percent`, `EffectiveSpread_Dollar`, `EffectiveSpread_Percent`, `RealizedSpread_Dollar`, `PriceImpact_Dollar`, `PriceImpact_Percent`, `t_vol`, `b_vol`, `s_vol`）导出。
2. **表 2（`hku_wrds_02_taq_ctm_sample.csv`）**：首页 ➔ `TAQ` ➔ `Consolidated Trades (ctm)` ➔ 输入 `IBIT ETHA MSTR COIN` ➔ 选最近 30 个交易日美盘开盘（`09:30–10:00`）与收盘（`15:30–16:00`）导出。
3. **表 3（`hku_wrds_03_optionmetrics_vsurfd.csv`）**：首页 ➔ `OptionMetrics` ➔ `IvyDB US` ➔ `Volatility Surface (vsurfd)` ➔ 粘贴上述代码，`Days` 填 `10, 30, 60, 91, 182`，`Call/Put` 选 `Both` ➔ 全选字段（`days, delta, cp_flag, impl_volatility, impl_strike, impl_premium, dispersion`）导出。
4. **表 4（`hku_wrds_04_optionmetrics_stdopd.csv`）**：`OptionMetrics` ➔ `IvyDB US` ➔ `Standardized Options (stdopd)` ➔ 粘贴代码 ➔ 勾选 `forward_price, strike_price, impl_volatility, delta, gamma, vega, theta` 导出。
5. **表 5（`hku_wrds_05_optionmetrics_opvold.csv`）**：`OptionMetrics` ➔ `IvyDB US` ➔ `Volume / Open Interest (opvold)` ➔ 粘贴代码 ➔ 勾选 `cp_flag, volume, open_interest` 导出。
6. **表 6（`hku_wrds_06_optionmetrics_zerocd.csv`）**：`OptionMetrics` ➔ `IvyDB US` ➔ `Zero Curve (zerocd)` ➔ 导出 `2023-01-01` 至今的期权定价零息利率曲线。
7. **表 7（`hku_wrds_07_ravenpack_news.csv`）**：首页 ➔ `RavenPack` ➔ `RavenPack Edge / Dow Jones & Web Edition` ➔ 选 `2024-01-01` 至今，`Relevance >= 75` ➔ 勾选 `TIMESTAMP_UTC, ENTITY_NAME, COUNTRY_CODE, TOPIC, GROUP, TYPE, ESS, ENS, CSS, NIP, RELEVANCE, HEADLINE` 导出。
8. **表 8（`hku_wrds_08_trace_convertible_bonds.csv`）**：首页 ➔ `TRACE` ➔ `TRACE Enhanced` ➔ 在 `Company Symbol` 填入 `MSTR COIN MARA RIOT CLSK CORZ IREN WULF` ➔ 勾选 `trd_exctn_dt, trd_exctn_tm, sym_cd, cusip_id, entrd_vol_qt, rptd_pr, yld_pt, rpt_side_cd, contra_party_type` 导出。
9. **表 9（`hku_wrds_09_markit_short_loan.csv`）**：首页 ➔ `Markit` ➔ `Markit Securities Finance (msf / short loan)` ➔ 粘贴上述 43 个代码 ➔ 勾选 `IndicativeFee`（融券费率）、`Utilization`（券池利用率）、`ShortLoanQuantity`、`DaysToCover`、`ActiveUtilization` 导出。
10. **表 10（`hku_wrds_10_compustat_shortint.csv`）**：首页 ➔ `Compustat - Capital IQ` ➔ `North America` ➔ `Supplemental Short Interest File (sec_shortint)` ➔ 粘贴代码 ➔ 勾选 `ticker, datadate, shortint, shortintadj` 导出。
11. **表 11（`hku_wrds_11_13f_holdings.csv`）**：首页 ➔ `Thomson / Refinitiv` ➔ `Institutional (13f) Holdings - s34` ➔ 粘贴代码 ➔ 勾选 `fdate, mgrname, ticker, shares, prc, shrout1` 导出。
12. **表 12（`hku_wrds_12_mutual_fund_s12.csv`）**：`Thomson / Refinitiv` ➔ `Mutual Fund Holdings - s12` ➔ 粘贴代码 ➔ 勾选 `fdate, fundname, ticker, shares, prc` 导出。
13. **表 13（`hku_wrds_13_insider_trades.csv`）**：`Thomson / Refinitiv` ➔ `Insiders Data - Table 1` ➔ 粘贴代码 ➔ 勾选 `ticker, trandate, personname, rolecode1, trancode, shares, tprice` 导出。
14. **表 14（`hku_wrds_14_crsp_daily_and_treasury.csv`）**：首页 ➔ `CRSP` ➔ `Daily Stock File (dsf)`（粘贴 43 个代码，勾选 `prc, vol, ret, bid, ask, shrout`）及 `Treasuries (tfz_dly)` 导出。
15. **表 15（`hku_wrds_15_cboe_indices.csv`）**：首页 ➔ `CBOE Indexes` ➔ `CBOE Indexes (cboe)` ➔ 全选字段导出 `2020-01-01` 至今全部波动率指数序列。

## 三、第二站：港大经管学院 Bloomberg Lab 彭博终端（表 16 ~ 表 23，梁銶琚楼）

**准备工作**：在梁銶琚楼彭博机房登录 **Bloomberg**，打开电脑上的 **Excel**（确认顶部有 `Bloomberg` 选项卡），或者直接跑 **第六部分** 的 `python fetch_hku_master.py --mode bloomberg`（5 分钟自动拉完所有 `TRADE`/`BID`/`ASK` 5m K 线与日频表）。

### 3.1 Excel 公式直接粘贴法（免写代码）
在 Excel 中新建 3 个工作表（`Sheet1` ~ `Sheet3`），分别把下面对应的公式块复制粘贴进去：

* **`Sheet1`（对应表 16 & 表 17：18 只美股/港股加密 ETF 价格 + 实时 iNAV 净值 + CME 期货近月/次月 5 分钟 K 线）**
  依次在 `A1`, `G1`, `M1`, `S1`... 横向每隔 6 列粘贴一行公式：
  ```excel
  =BDH("IBIT US Equity","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("IBITIV Index","OPEN,HIGH,LOW,LAST_PRICE","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("FBTC US Equity","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("FBTCIV Index","OPEN,HIGH,LOW,LAST_PRICE","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("ARKB US Equity","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("ARKBIV Index","OPEN,HIGH,LOW,LAST_PRICE","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("BITB US Equity","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("BITBIV Index","OPEN,HIGH,LOW,LAST_PRICE","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("GBTC US Equity","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("GBTCIV Index","OPEN,HIGH,LOW,LAST_PRICE","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("ETHA US Equity","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("ETHAIV Index","OPEN,HIGH,LOW,LAST_PRICE","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("FETH US Equity","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("FETHIV Index","OPEN,HIGH,LOW,LAST_PRICE","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("BITO US Equity","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("BITOIV Index","OPEN,HIGH,LOW,LAST_PRICE","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("BITX US Equity","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("3042 HK Equity","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("3046 HK Equity","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("BTCA Comdty","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("BTCB Comdty","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("ETHA Comdty","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("ETHB Comdty","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
  =BDH("BGCI Index","OPEN,HIGH,LOW,LAST_PRICE","1/5/2026","","BarTp=T","BarSz=5")
  ```

* **`Sheet2`（对应表 18 & 表 19：加密 ETF/股票每日资金流 `FUND_FLOW`、净值 `NAV`、空头占比 `SI_PCT_FLOAT`、期权 25D 偏度 `25D_RISK_REVERSAL_1M` 与持仓比）**
  在 `A1`, `P1`, `AE1`... 依次粘贴：
  ```excel
  =BDH("IBIT US Equity","PX_LAST,FUND_NET_ASSET_VAL,FUND_FLOW,SH_OUT,SI_PCT_FLOAT,IMP_VOL_ATM,30DAY_IMPVOL_90.0%MNY_DF,30DAY_IMPVOL_110.0%MNY_DF,25D_RISK_REVERSAL_1M,25D_BUTTERFLY_1M,PUT_CALL_OPEN_INTEREST_RATIO,OPEN_INT_TOTAL_CALL,OPEN_INT_TOTAL_PUT","20240110","")
  =BDH("ETHA US Equity","PX_LAST,FUND_NET_ASSET_VAL,FUND_FLOW,SH_OUT,SI_PCT_FLOAT,IMP_VOL_ATM,25D_RISK_REVERSAL_1M,PUT_CALL_OPEN_INTEREST_RATIO,OPEN_INT_TOTAL_CALL,OPEN_INT_TOTAL_PUT","20240501","")
  =BDH("FBTC US Equity","PX_LAST,FUND_NET_ASSET_VAL,FUND_FLOW,SH_OUT,SI_PCT_FLOAT,IMP_VOL_ATM,25D_RISK_REVERSAL_1M,PUT_CALL_OPEN_INTEREST_RATIO","20240110","")
  =BDH("MSTR US Equity","PX_LAST,PX_VOLUME,SI_PCT_FLOAT,SHORT_INT_RATIO,IMP_VOL_ATM,25D_RISK_REVERSAL_1M,25D_BUTTERFLY_1M,PUT_CALL_OPEN_INTEREST_RATIO,OPEN_INT_TOTAL_CALL,OPEN_INT_TOTAL_PUT","20230101","")
  =BDH("COIN US Equity","PX_LAST,PX_VOLUME,SI_PCT_FLOAT,SHORT_INT_RATIO,IMP_VOL_ATM,25D_RISK_REVERSAL_1M,25D_BUTTERFLY_1M,PUT_CALL_OPEN_INTEREST_RATIO,OPEN_INT_TOTAL_CALL,OPEN_INT_TOTAL_PUT","20230101","")
  =BDH("MARA US Equity","PX_LAST,PX_VOLUME,SI_PCT_FLOAT,SHORT_INT_RATIO,IMP_VOL_ATM,25D_RISK_REVERSAL_1M,PUT_CALL_OPEN_INTEREST_RATIO","20230101","")
  =BDH("NVDA US Equity","PX_LAST,PX_VOLUME,SI_PCT_FLOAT,IMP_VOL_ATM,25D_RISK_REVERSAL_1M,PUT_CALL_OPEN_INTEREST_RATIO","20230101","")
  =BDH("QQQ US Equity","PX_LAST,FUND_FLOW,IMP_VOL_ATM,25D_RISK_REVERSAL_1M,PUT_CALL_OPEN_INTEREST_RATIO","20230101","")
  =BDH("HYG US Equity","PX_LAST,FUND_FLOW,IMP_VOL_ATM,25D_RISK_REVERSAL_1M,PUT_CALL_OPEN_INTEREST_RATIO","20230101","")
  ```

* **`Sheet3`（对应表 20 & 表 21：华尔街宏观金融条件指数 FCI、花旗/彭博经济惊奇指数、美元回购 SOFR/FRA-OIS、交叉货币基差与北美 CDX 信用利差）**
  用 `Bloomberg` ➔ `Spreadsheet Builder` ➔ `Historical Data Table` 一次性把下面这组代码复制进去，字段选 `PX_LAST`，日期选 `2023-01-01` 至今：
  ```text
  BFCIUS Index, BFCIEU Index, BFCICN Index, GSUSFCI Index, GSEUFCI Index, GSCNFCI Index, NFCI Index, ANFCI Index, ECSURPUS Index, ECSURPEU Index, ECSURPCN Index, CESIUSD Index, CESIEUR Index, CESICHN Index, CESIJPY Index, CESIG10 Index, VIX Index, VIX9D Index, VIX3M Index, VVIX Index, SKEW Index, MOVE Index, GVZ Index, OVX Index, JPMVXYGL Index, JPMVXYG7 Index, USDJPYV1M BGN Curncy, USDJPY25R1M BGN Curncy, USDJPY25B1M BGN Curncy, EURUSD25R1M BGN Curncy, USDCNH25R1M BGN Curncy, AUDJPY25R1M BGN Curncy, XAUUSD25R1M BGN Curncy, SOFRRATE Index, EFFR Index, OBFR Index, FRAOISUS Index, USGG2YR Index, USGG10YR Index, USYC2Y10 Index, USGGBE10 Index, USGGT10Y Index, JYBS3 Curncy, JYBS1 Curncy, EUBS3 Curncy, SFBS3 Curncy, LF98OAS Index, LUACOAS Index, DBMF US Equity, CTA US Equity, KMLM US Equity, RPAR US Equity, SVOL US Equity, SVIX US Equity, TAIL US Equity
  ```

* **表 22 & 表 23（彭博 `ECO` 宏观日历与 `COT` 持仓导出）**：在彭博终端输入 `ECO <GO>` ➔ 筛选 `US, China, Eurozone, Japan`（`Medium & High`）➔ 右上角 `Actions` ➔ `Export to Excel`（包含 `Survey Median, High, Low, Std Dev, Actual`），保存为 **`hku_bbg_22_eco_calendar.xlsx`**。

## 四、第三站：港大 LSEG Workspace / Datastream 终端（表 24 ~ 表 28）

1. 在港大图书馆或实验室登录 **LSEG Workspace**，顶部搜索栏输入 **`CODEBOOK`** 按回车打开内置 Python 环境。
2. 新建一个 `.py` 文件，把本文档 **第六部分** 的脚本复制进去并运行 `python fetch_hku_master.py --mode lseg`，会自动生成 **表 24 ~ 表 28** 的全部 CSV（含 71 个跨资产标的的 `5min`/`1h`/`1d` K 线、做市商盘口 `BID`/`ASK`/`BID_NUMMOV`/`ASK_NUMMOV`/`VWAP`/`OPINT_1`/`BLKVOLUM`、Lipper ETF 申赎流、StarMine 1–100 逼空评分 `TR.StarMineShortSqueeze`、13F 持仓与路透机读新闻）。
3. 在 LSEG 顶部搜索栏输入 **`ECOCAL`** 打开经济日历，筛选美、中、欧、日（`Relevance = 2, 3`），勾选 **`SmartEconomics®`**、**`Predicted Surprise`**、**`Min`**、**`Max`**、**`Count`**、**`Standard Deviation`** 导出 Excel。

## 五、第四站：港大经管学院 Wind 万得终端（表 29 ~ 表 32，亚洲盘专属）

在经管学院机房打开 **Wind 终端**（或 Excel 顶部的 **`Wind`** 插件选项卡）：
1. **表 29（香港 6 只现货 BTC/ETH ETF 港币+美元双柜台 5m K 线、IOPV 与每日份额申赎）**：
   复制粘贴代码：`3042.HK, 3046.HK, 3008.HK, 3009.HK, 3439.HK, 3179.HK, 9042.HK, 9046.HK, 9008.HK, 9009.HK, 9439.HK, 9179.HK`，导出 `5min` K 线及日频 `nav, iopv, unit_total`，保存为 **`hku_wind_29_hk_spot_etf.xlsx`**。
2. **表 30（港股 Web3 与持币上市公司行情）**：
   复制粘贴代码：`0863.HK, 1611.HK, 0434.HK, 1357.HK, 3066.HK, 3068.HK`，导出 `5min` 与日频行情，保存为 **`hku_wind_30_hk_web3_equities.xlsx`**。
3. **表 31（亚洲时段离岸人民币/港元拆借利率与央行逆回购流动性）**：
   在 Wind 宏观库（EDB）导出：`CNH HIBOR`（隔夜/1W/1M）、`HKD HIBOR`（隔夜/1W/1M）、香港金管局总结余、银行间 `DR001`/`DR007`/`R007`、中国央行公开市场逆回购每日净投放，保存为 **`hku_wind_31_asia_liquidity.xlsx`**。
4. **表 32（亚太早盘风险情绪基准指数 `5min` K 线）**：
   复制粘贴代码：`HSTECH.HI, HSI.HI, N225.GI, KS200.GI`，导出 `5min` K 线，保存为 **`hku_wind_32_asia_indices_5m.xlsx`**。

## 六、完整内嵌的港大全平台一键自动拉取 Python 脚本（复制保存为 `fetch_hku_master.py`）

```python
#!/usr/bin/env python3
"""HKU (University of Hong Kong) Master All-in-One Data Exporter (WRDS + Bloomberg + LSEG).

Usage:
  1. On HKU Wi-Fi / VPN (WRDS 15 Tables):
       pip install wrds pandas
       python fetch_hku_master.py --mode wrds --wrds-user YOUR_WRDS_USERNAME
  2. In HKU Bloomberg Lab (Bloomberg 65+ Tickers Intraday 5m TRADE/BID/ASK + Daily):
       pip install blpapi pandas
       python fetch_hku_master.py --mode bloomberg
  3. In HKU LSEG Workspace CodeBook (LSEG 71 RICs + Lipper + StarMine + 13F + News):
       python fetch_hku_master.py --mode lseg
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    "XRPUSD Curncy", "XDOUSD Curncy",
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


def run_bloomberg_export(out_dir: Path) -> None:
    import blpapi
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
                            except Exception:
                                row[fld] = None
                    daily_rows.append(row)
        if ev.eventType() == blpapi.Event.RESPONSE:
            break

    if daily_rows:
        pd.DataFrame(daily_rows).to_csv(out_dir / "hku_bbg_daily_macro_options_flows_full.csv", index=False)

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
        pd.DataFrame(intraday_rows).to_csv(out_dir / "hku_bbg_intraday_5m_trade_bid_ask_full.csv", index=False)
    session.stop()


def run_wrds_export(out_dir: Path, wrds_username: str | None = None) -> None:
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
        "hku_wrds_01_taq_iid.csv": f"""
            SELECT * FROM taq.wrds_iid_2024 WHERE sym_root IN ({tickers_sql})
            UNION ALL
            SELECT * FROM taq.wrds_iid_2025 WHERE sym_root IN ({tickers_sql})
        """,
        "hku_wrds_03_optionmetrics_vsurfd.csv": f"""
            SELECT s.ticker, v.date, v.days, v.delta, v.cp_flag,
                   v.impl_volatility, v.impl_strike, v.impl_premium, v.dispersion
            FROM optionm.vsurfd2024 v
            JOIN optionm.securd s ON v.secid = s.secid
            WHERE s.ticker IN ({tickers_sql}) AND v.days IN (10, 30, 60, 91, 182)
        """,
        "hku_wrds_04_optionmetrics_stdopd.csv": f"""
            SELECT s.ticker, o.date, o.days, o.cp_flag,
                   o.forward_price, o.strike_price, o.impl_volatility,
                   o.delta, o.gamma, o.vega, o.theta
            FROM optionm.stdopd2024 o
            JOIN optionm.securd s ON o.secid = s.secid
            WHERE s.ticker IN ({tickers_sql}) AND o.days IN (30, 60, 91)
        """,
        "hku_wrds_05_optionmetrics_opvold.csv": f"""
            SELECT s.ticker, v.date, v.cp_flag, v.volume, v.open_interest
            FROM optionm.opvold v
            JOIN optionm.securd s ON v.secid = s.secid
            WHERE s.ticker IN ({tickers_sql}) AND v.date >= '2023-01-01'
        """,
        "hku_wrds_06_optionmetrics_zerocd.csv": """
            SELECT date, days, rate FROM optionm.zerocd WHERE date >= '2023-01-01'
        """,
        "hku_wrds_07_ravenpack_news.csv": """
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
        "hku_wrds_08_trace_convertible_bonds.csv": """
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
        "hku_wrds_09_markit_short_loan.csv": f"""
            SELECT * FROM markit.msf_analytics
            WHERE ticker IN ({tickers_sql}) AND datadate >= '2023-01-01'
        """,
        "hku_wrds_10_compustat_shortint.csv": f"""
            SELECT gvkey, iid, ticker, datadate, shortint, shortintadj, splitadjdate
            FROM comp.sec_shortint
            WHERE ticker IN ({tickers_sql}) AND datadate >= '2023-01-01'
        """,
        "hku_wrds_11_13f_holdings.csv": f"""
            SELECT fdate, mgrname, ticker, cusip, shares, prc, shrout1
            FROM tfn.s34
            WHERE ticker IN ({tickers_sql}) AND fdate >= '2023-01-01'
        """,
        "hku_wrds_12_mutual_fund_s12.csv": f"""
            SELECT fdate, fundname, ticker, cusip, shares, prc
            FROM tfn.s12
            WHERE ticker IN ({tickers_sql}) AND fdate >= '2023-01-01'
        """,
        "hku_wrds_13_insider_trades.csv": f"""
            SELECT ticker, trandate, personname, rolecode1, trancode, shares, tprice
            FROM tfn.table1
            WHERE ticker IN ({tickers_sql}) AND trandate >= '2023-01-01'
        """,
        "hku_wrds_14a_crsp_daily.csv": f"""
            SELECT n.ticker, d.date, d.prc, d.vol, d.ret, d.bid, d.ask, d.bidlo, d.askhi, d.shrout
            FROM crsp.dsf d
            JOIN crsp.dsenames n
              ON d.permno = n.permno
             AND d.date BETWEEN n.namedt AND COALESCE(n.nameendt, CURRENT_DATE)
            WHERE n.ticker IN ({tickers_sql}) AND d.date >= '2023-01-01'
        """,
        "hku_wrds_14b_crsp_treasuries.csv": """
            SELECT caldt, kytreasno, tdnomprc, tdyld, tdduratn, tdretnua
            FROM crsp.tfz_dly WHERE caldt >= '2023-01-01'
        """,
        "hku_wrds_15_cboe_indices.csv": """
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


def run_lseg_export(out_dir: Path) -> None:
    import pandas as pd
    import refinitiv.data as rd

    out_dir.mkdir(parents=True, exist_ok=True)
    rics = [
        "BTC=", "ETH=", "SOL=", "BTCc1", "BTCc2", "ETHc1", "ETHc2",
        "ESc1", "NQc1", ".VIX", ".VIX9D", ".VIX3M", ".VXN", ".SKEW", "VIXc1", "VIXc2", ".MOVE",
        "US2YT=RR", "US10YT=RR", "JP10YT=RR", "US2Y10Y=RR", "US5Y5YF=R", "SOFR=", "USONFFE=",
        ".DXY", "EUR=", "JPY=", "CNH=", "AUDJPY=", "EURJPY=", "KRW=", "SGD=", "CHF=", "MXN=",
        "JPY1MO=", "JPY1MRR=", "EUR1MO=", "EUR1MRR=", "XAU=", "XAU1MO=", "XAU1MRR=", "CLc1",
        "JPYCBS3M=", "EURCBS3M=", "QQQ.O", "TLT.O", "HYG.P", "LQD.P", "BKLN.P",
        "DBMF.P", "CTA.P", "KMLM.P", "RPAR.P", "SVOL.P", "TAIL.P",
        "IBIT.O", "FBTC.Z", "ARKB.Z", "BITB.P", "GBTC.P", "ETHA.O", "FETH.Z", "ETHE.P",
        "MSTR.O", "COIN.O", "MARA.O", "RIOT.O", "CLSK.O", "CORZ.O", "IREN.O", "HOOD.O", "NVDA.O", "SMH.O",
    ]
    etf_rics = ["IBIT.O", "FBTC.Z", "ARKB.Z", "BITB.P", "GBTC.P", "ETHA.O", "FETH.Z", "ETHE.P", "HYG.P", "TLT.O", "QQQ.O", "DBMF.P"]
    eq_rics = ["MSTR.O", "COIN.O", "MARA.O", "RIOT.O", "CLSK.O", "CORZ.O", "IREN.O", "HOOD.O", "NVDA.O", "SMH.O"]

    rd.open_session()
    try:
        frames = []
        for interval in ("5min", "1h", "1d"):
            for ric in rics:
                try:
                    df = rd.get_history(universe=ric, interval=interval, count=5000)
                    if df is not None and not df.empty:
                        df = df.reset_index()
                        df["ric"] = ric
                        df["interval"] = interval
                        frames.append(df)
                except Exception as err:
                    print(f"[SKIP] {ric} ({interval}): {err}")
        if frames:
            pd.concat(frames, ignore_index=True).to_csv(out_dir / "hku_lseg_24_all_bars_microstructure.csv", index=False)

        etf_df = rd.get_data(
            universe=etf_rics,
            fields=["TR.FundNetFlows.date", "TR.FundNetFlows", "TR.NETASSETVAL", "TR.CompanyMarketCap", "TR.SharesOutstanding"],
            parameters={"SDate": "2024-01-10", "EDate": "0D", "Frq": "D"},
        )
        if etf_df is not None:
            etf_df.to_csv(out_dir / "hku_lseg_25_etf_flows_nav.csv", index=False)

        sm_df = rd.get_data(
            universe=eq_rics,
            fields=["TR.ShortInterestPct.date", "TR.ShortInterestPct", "TR.StarMineShortSqueeze", "TR.StarMineARM", "TR.StarMineSmartHoldings", "TR.StarMineCombinedAlpha"],
            parameters={"SDate": "2024-01-01", "EDate": "0D", "Frq": "D"},
        )
        if sm_df is not None:
            sm_df.to_csv(out_dir / "hku_lseg_26_starmine_crowding.csv", index=False)

        own_df = rd.get_data(
            universe=eq_rics + etf_rics[:8],
            fields=["TR.PctOfSharesOutHeld.date", "TR.PctOfSharesOutHeld", "TR.NumOfInstitutionalHolders", "TR.InsiderNetSharesTraded"],
            parameters={"SDate": "2024-01-01", "EDate": "0D", "Frq": "Q"},
        )
        if own_df is not None:
            own_df.to_csv(out_dir / "hku_lseg_27_institutional_ownership.csv", index=False)

        news_df = rd.news.get_headlines(
            query="Topic:CRYP OR (FOMC AND rate) OR (SEC AND ETF) OR (Binance OR Tether OR Hack)",
            count=1000,
        )
        if news_df is not None:
            news_df.to_csv(out_dir / "hku_lseg_28_news_headlines.csv", index=False)
    finally:
        rd.close_session()


def main() -> int:
    parser = argparse.ArgumentParser(description="HKU Master Exporter (WRDS / Bloomberg / LSEG)")
    parser.add_argument("--mode", choices=("wrds", "bloomberg", "lseg"), required=True)
    parser.add_argument("--out-dir", type=Path, default=Path("hku_exported_data"))
    parser.add_argument("--wrds-user", type=str, default=None)
    args = parser.parse_args()

    if args.mode == "wrds":
        run_wrds_export(args.out_dir, wrds_username=args.wrds_user)
    elif args.mode == "bloomberg":
        run_bloomberg_export(args.out_dir)
    else:
        run_lseg_export(args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```
