# 香港大学（HKU）全量金融数据下载「零基础小白操作指南」（完整单文档版）

> **致港大同学**：非常感谢帮忙！这份文档专门针对**香港大学（HKU）**的三个实验室/图书馆权限（**① 港大图书馆 WRDS 数据库**、**② 梁銶琚楼经管学院 Bloomberg Lab 彭博终端**、**③ 经管学院 Wind 万得终端**）编写。
> 所有步骤均拆解为**「直接复制粘贴代码/公式 + 网页勾选」**，不需要懂金融或量化，也不需要额外打开其他文件（最下方已内嵌全自动 Python 脚本源码），下载完成后把生成的 `.csv` / `.xlsx` 文件夹打包发回即可！

## 目录与任务总览（共 3 大平台、19 张全量数据表）

| 平台地点 | 是否需要去实验室 | 包含数据表（共 19 张） | 预计耗时 |
| :--- | :--- | :--- | :--- |
| **一、港大图书馆 WRDS 数据库** | **不用去实验室**（宿舍连港大 Wi-Fi 或 VPN 即可） | **8 张表**：① TAQ 毫秒级微观盘口指标库（`wrds_taq_iid`）、② OptionMetrics 期权波动率曲面（`vsurfd`）、③ OptionMetrics 平价期权与持仓（`stdopd`/`opprcd`）、④ RavenPack 毫秒级机构新闻情绪、⑤ FINRA TRACE 可转债/公司债机构场外暗盘逐笔交易、⑥ 机构融券做空费率与拥挤度（Short Interest）、⑦ 华尔街对冲基金 13F 持仓与内部人减持、⑧ CRSP 美股 ETF 与美债日频盘口 | 15 分钟（跑内嵌脚本仅需 3 分钟） |
| **二、经管学院 Bloomberg Lab** | 梁銶琚楼（K.K. Leung Building）彭博机房 | **7 张表**：① 美股/港股 18 只加密 ETF 价格与盘中 5m 实时净值（iNAV，`TRADE`/`BID`/`ASK` 三套盘口）、② 加密现货与 CME 近月/次月/微型期货期限结构（`5m`/`1h`/`1d`）、③ 加密概念股/矿企/AI 龙头与华尔街 CTA/风险平价/做空波动率策略 ETF、④ 全球期权波动率曲面与 25-Delta 风险逆转偏度（含 `IBIT` 期权）、⑤ 彭博/高盛/花旗宏观金融条件与经济惊奇指数、⑥ 美元回购融资（SOFR/FRA-OIS）、交叉货币基差互换与北美 CDX 信用违约互换利差、⑦ CFTC COT 对冲基金持仓与彭博 `ECO` 经济日历（含预测高低极值与标准差） | 15 分钟（跑内嵌脚本仅需 5 分钟） |
| **三、经管学院 Wind 万得终端** | 经管学院机房 | **4 张表**：① 香港 6 只比特币/以太坊现货 ETF（港币+美元柜台）5m K 线与每日申赎份额、② 港股数字资产概念股行情、③ 亚太时段离岸人民币（`CNH HIBOR`）/港元（`HKD HIBOR`）/央行逆回购流动性、④ 亚太早盘风险情绪指数（恒生科技/日经/韩国 KOSPI 200 5m K 线） | 10 分钟 |

## 第一部分：港大图书馆 WRDS 数据库全量下载（宿舍/图书馆连校园网即可操作）

**登录方式**：连上港大校园网 Wi-Fi（`HKU`）或 HKU VPN，访问港大图书馆 WRDS 入口（`https://wrds-www.wharton.upenn.edu/`），用你的 `@connect.hku.hk` 或 `@hku.hk` 账号登录。
* **如果你电脑装有 Python（最推荐，3 分钟全自动下完 8 张表）**：直接跳到本文档 **第四部分 4.1 节**，复制脚本跑一行命令即可，不用手动点网页！
* **如果你不想跑代码，按下面 8 张表在网页上依次点选导出 `.csv`**：

### 表 1：美股加密 ETF 与概念股「毫秒级盘口微观结构日度汇总」（NYSE TAQ Intraday Indicators）
> **用途**：WRDS 已经帮我们把几百 GB 的美股毫秒级逐笔数据算成了每日价格冲击（Kyle's Lambda）、有效买卖价差（Effective Spread）和主动买卖失衡（Order Imbalance）。
1. 路径：WRDS 首页 ➔ **`TAQ`** ➔ **`WRDS Intraday Indicators (wrds_taq_iid)`**。
2. **Date Range**：`2024-01-01` 至最新日期。
3. **Ticker**（直接复制粘贴下面整行）：
   ```text
   IBIT FBTC ARKB BITB GBTC ETHA FETH ETHE BITO BITX MSTR COIN MARA RIOT CLSK CORZ IREN HOOD NVDA SMH QQQ SPY HYG TLT
   ```
4. **Variables**：全选（尤其是 `SYM_ROOT`, `DATE`, `QuotedSpread_Dollar`, `QuotedSpread_Percent`, `EffectiveSpread_Dollar`, `EffectiveSpread_Percent`, `RealizedSpread_Dollar`, `PriceImpact_Dollar`, `PriceImpact_Percent`, `t_vol`, `b_vol`, `s_vol`）。
5. **Output**：选 `Comma-delimited text (*.csv)` ➔ 保存为 **`hku_wrds_1_taq_microstructure.csv`**。

### 表 2：美股期权标准化隐含波动率曲面（OptionMetrics Volatility Surface）
1. 路径：WRDS 首页 ➔ **`OptionMetrics`** ➔ **`IvyDB US`** ➔ **`Volatility Surface (vsurfd)`**。
2. **Date Range**：`2023-01-01` 至最新日期。
3. **Ticker**（复制粘贴）：
   ```text
   IBIT BITO COIN MSTR MARA RIOT CLSK HOOD NVDA SMH QQQ SPY IWM HYG TLT GLD
   ```
   *（筛选条件：`Days to Expiration` 填 `10, 30, 60, 91, 182`；`Call/Put` 选 `Both`）*
4. **Variables**：勾选 `TICKER`, `DATE`, `DAYS`, `DELTA`, `IMPL_VOLATILITY`, `IMPL_STRIKE`, `IMPL_PREMIUM`, `DISPERSION`, `CP_FLAG`。
5. **Output**：选 `.csv` ➔ 保存为 **`hku_wrds_2_optionmetrics_surface.csv`**。

### 表 3：美股期权每日总持仓量、成交量与希腊字母汇总（OptionMetrics Volume & Open Interest）
1. 路径：WRDS 首页 ➔ **`OptionMetrics`** ➔ **`IvyDB US`** ➔ **`Volume / Open Interest`（及 `Standardized Options stdopd`）**。
2. **Date Range**：`2023-01-01` 至最新日期；**Ticker** 同表 2。
3. **Variables**：勾选 `TICKER`, `DATE`, `CP_FLAG`, `VOLUME`, `OPEN_INTEREST`, `IMPL_VOLATILITY`, `DELTA`, `GAMMA`, `VEGA`, `THETA`。
4. **Output**：选 `.csv` ➔ 保存为 **`hku_wrds_3_optionmetrics_oi_greeks.csv`**。

### 表 4：RavenPack 全球机读新闻情绪与事件冲击库（RavenPack News Analytics）
1. 路径：WRDS 首页 ➔ **`RavenPack`** ➔ **`RavenPack Edge / Dow Jones & Web Edition`**。
2. **Date Range**：`2024-01-01` 至最新日期。
3. **筛选条件**：`Relevance >= 75`，勾选字段：`TIMESTAMP_UTC`, `RP_ENTITY_ID`, `ENTITY_NAME`, `COUNTRY_CODE`, `TOPIC`, `GROUP`, `TYPE`, `ESS` (事件情绪分), `ENS` (事件新鲜度分), `CSS` (综合情绪分), `NIP` (新闻冲击预测分), `RELEVANCE`, `HEADLINE`。
4. **Output**：选 `.csv` ➔ 保存为 **`hku_wrds_4_ravenpack_news.csv`**。

### 表 5：华尔街可转债套利盘与高收益公司债场外暗盘逐笔成交（FINRA TRACE Enhanced）
> **用途**：华尔街对冲基金在场外买卖 `MSTR`、`COIN`、`MARA`、`RIOT` 的可转债（Convertible Bonds）时，会同步在币安/CME 做空 BTC 或正股对冲 Delta。
1. 路径：WRDS 首页 ➔ **`TRACE`** ➔ **`TRACE Enhanced`**。
2. **Date Range**：`2024-01-01` 至最新日期。
3. **Company Symbol / Sub-Product**：输入发行人代码 `MSTR COIN MARA RIOT CLSK CORZ`（或筛选科技/金融高收益债）。
4. **Variables**：勾选 `TRD_EXCTN_DT` (成交日期), `TRD_EXCTN_TM` (成交时间), `SYM_CD`, `CUSIP_ID`, `ENTRD_VOL_QT` (成交量), `RPTD_PR` (成交价), `YLD_PT` (收益率), `RPT_SIDE_CD` (买方 B / 卖方 S), `CONTRA_PARTY_TYPE` (交易对手类型：D=交易商 Dealer / C=机构客户 Customer)。
5. **Output**：选 `.csv` ➔ 保存为 **`hku_wrds_5_trace_bond_trades.csv`**。

### 表 6：美股加密概念股与 ETF 做空仓位与融券拥挤度（Compustat / Markit Short Interest）
1. 路径：WRDS 首页 ➔ **`Compustat - Capital IQ`** ➔ **`North America`** ➔ **`Supplemental Short Interest File (sec_shortint)`**（若港大开通了 `Markit Securities Finance / Short Loan`，也一并勾选）。
2. **Date Range**：`2023-01-01` 至最新日期；**Ticker** 同表 1。
3. **Variables**：勾选 `GVKEY`, `IID`, `TICKER`, `DATADATE`, `SHORTINT` (空头股数), `SHORTINTADJ`, `SPLITADJDATE`。
4. **Output**：选 `.csv` ➔ 保存为 **`hku_wrds_6_short_interest.csv`**。

### 表 7：华尔街对冲基金 13F 季度持仓与高管内部人减持流水（Thomson / LSEG 13F & Insiders）
1. 路径：WRDS 首页 ➔ **`Thomson / Refinitiv`** ➔ **`Institutional (13f) Holdings - s34`** 及 **`Insiders Data - Table 1`**。
2. **Date Range**：`2024-01-01` 至最新日期；**Ticker** 同表 1。
3. **Variables**：13F 勾选 `FDATE`, `MGRNAME` (基金名称，含 Millennium/Citadel/Jane Street 等), `TICKER`, `SHARES`, `PRC`, `SHROUT1`；Insiders 勾选 `TICKER`, `TRANDATE`, `PERSONNAME`, `ROLECODE`, `TRANCODE` (P=买入 / S=卖出), `SHARES`, `TPRICE`。
4. **Output**：分别保存为 **`hku_wrds_7a_13f_holdings.csv`** 与 **`hku_wrds_7b_insider_trades.csv`**。

### 表 8：CRSP 美股现货 ETF 日频盘口与美国国债全期限基准（CRSP Daily Stock & US Treasury）
1. 路径：WRDS 首页 ➔ **`CRSP`** ➔ **`Daily Stock File (dsf)`** 与 **`Treasuries (tfz_dly)`**。
2. **Date Range**：`2023-01-01` 至最新日期；**Ticker** 同表 1。
3. **Variables**：勾选 `TICKER`, `DATE`, `PRC`, `VOL`, `RET`, `BID`, `ASK`, `BIDLO`, `ASKHI`, `SHROUT`, `CFACPR`。
4. **Output**：保存为 **`hku_wrds_8_crsp_daily_and_treasury.csv`**。

## 第二部分：港大经管学院 Bloomberg Lab（彭博终端）全量 7 大表下载指南

**地点**：港大梁銶琚楼（K.K. Leung Building）经管学院 **Bloomberg Lab**。
* **最省事的方法（推荐，5 分钟全自动搞定全部 7 张表）**：登录彭博终端后，打开电脑命令行跑本文档 **第四部分 4.2 节** 的 Python 脚本 `python fetch_hku_all_in_one.py --mode bloomberg`，脚本会自动把下面全部 7 张表（含 `TRADE`/`BID`/`ASK` 三套 5 分钟盘口 K 线）一次性拉完存进文件夹！
* **如果不跑 Python，直接打开电脑上的 Excel 用彭博插件按下面粘贴公式**：

### 表 9（彭博表 1）：美股 + 港股 18 只加密 ETF 价格与盘中 5 分钟实时参考净值（iNAV，`5min` K 线）
> **核心价值**：彭博独有的 `IBITIV Index` / `ETHAIV Index` 等提供盘中每 5 分钟的**实时基金净值（iNAV）**，用 `(ETF现价 - iNAV) / iNAV` 能直接算出华尔街 AP 做市商（Jane Street / Virtu）每一分钟是在溢价申购买入 BTC，还是在折价赎回砸盘 BTC！
在 Excel 新建 `Sheet1`，在 `A1`、`G1`、`M1`... 横向依次粘贴以下公式（若用脚本则自动全部拉取）：
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
=BDH("ETHE US Equity","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
=BDH("BITO US Equity","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
=BDH("BITOIV Index","OPEN,HIGH,LOW,LAST_PRICE","1/5/2026","","BarTp=T","BarSz=5")
=BDH("BITX US Equity","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
```
保存为 **`hku_bbg_9_us_etf_and_inav_5m.xlsx`**。

### 表 10（彭博表 2）：加密现货基准、CME 期货近月/次月/微型合约期限结构（`5min` + `1h` + `1d`，含 `BID`/`ASK`/`TRADE`）
包含彭博银河加密指数、主流币及 CME 机构/微型合约：
* **标的代码清单**：`BGCI Index`（彭博银河加密指数）、`BGDI Index`（彭博 DeFi 指数）、`XBTUSD Curncy`（BTC）、`XETUSD Curncy`（ETH）、`XSOUSD Curncy`（SOL）、`XBNUSD Curncy`（BNB）、`XRPUSD Curncy`、`XDOUSD Curncy`（DOGE）、`BTCA Comdty`（CME BTC 近月）、`BTCB Comdty`（CME BTC 次月）、`MBTA Comdty`（CME Micro BTC 近月）、`ETHA Comdty`（CME ETH 近月）、`ETHB Comdty`（CME ETH 次月）、`META Comdty`（CME Micro ETH 近月）。
* **特别功能**：彭博支持分别把 `BarTp=T`（成交价）、`BarTp=B`（买一价 Bid K 线）、`BarTp=A`（卖一价 Ask K 线）拉出来，从而算出每一根 5 分钟 K 线真实的盘口高开低收价差！（第四部分脚本已自动循环拉取 `TRADE`/`BID`/`ASK` 三套 5m K 线）。

### 表 11（彭博表 3）：美股加密概念股/矿企、AI 算力龙头与华尔街量化策略 ETF（`5min` + `1d`）
* **加密概念与矿企（12 只）**：`MSTR US Equity`、`COIN US Equity`、`MARA US Equity`、`RIOT US Equity`、`CLSK US Equity`、`CORZ US Equity`、`IREN US Equity`、`WULF US Equity`、`HUT US Equity`、`BITF US Equity`、`HOOD US Equity`、`GLXY CN Equity`。
* **AI 算力与半导体龙头（7 只，对应币圈 AI 板块）**：`NVDA US Equity`、`SMH US Equity`、`SOXX US Equity`、`AMD US Equity`、`TSM US Equity`、`PLTR US Equity`、`ARM US Equity`。
* **华尔街 CTA 趋势、风险平价（Risk-Parity）与波动率控制（Vol-Control）复制 ETF（9 只）**：`DBMF US Equity`、`CTA US Equity`、`KMLM US Equity`、`RPAR US Equity`、`UPAR US Equity`、`SVOL US Equity`、`SVIX US Equity`、`UVIX US Equity`、`TAIL US Equity`。

### 表 12（彭博表 4）：跨市场期权隐含波动率、25-Delta 偏度（Risk Reversal）、蝶式（Butterfly）与持仓比（日频 + `1h`）
* **波动率与尾部偏度指数（14 个）**：`VIX Index`、`VIX9D Index`、`VIX3M Index`、`VIX6M Index`、`VVIX Index`、`SKEW Index`、`VXN Index`、`RVX Index`、`MOVE Index`（美债波动率）、`GVZ Index`（黄金波动率）、`OVX Index`（原油波动率）、`JPMVXYGL Index`（摩根大通全球外汇波动率）、`JPMVXYG7 Index`、`JPMVXYEM Index`。
* **外汇与黄金期权 1M/3M 隐含波动率及 25D 风险逆转偏度（11 个）**：`USDJPYV1M BGN Curncy`、`USDJPY25R1M BGN Curncy`、`USDJPY25B1M BGN Curncy`、`EURUSDV1M BGN Curncy`、`EURUSD25R1M BGN Curncy`、`USDCNHV1M BGN Curncy`、`USDCNH25R1M BGN Curncy`、`AUDJPYV1M BGN Curncy`、`AUDJPY25R1M BGN Curncy`、`XAUUSDV1M BGN Curncy`、`XAUUSD25R1M BGN Curncy`。
* **`IBIT` / `ETHA` / `BITO` / `MSTR` / `COIN` / `NVDA` / `QQQ` / `HYG` 期权偏度与资金流字段**：
  ```text
  PX_LAST, FUND_NET_ASSET_VAL, FUND_FLOW, SH_OUT, SHORT_INT_RATIO, SI_PCT_FLOAT, IMP_VOL_ATM, 30DAY_IMPVOL_90.0%MNY_DF, 30DAY_IMPVOL_100.0%MNY_DF, 30DAY_IMPVOL_110.0%MNY_DF, 25D_RISK_REVERSAL_1M, 25D_RISK_REVERSAL_3M, 25D_BUTTERFLY_1M, PUT_CALL_OPEN_INTEREST_RATIO, PUT_CALL_VOLUME_RATIO, OPEN_INT_TOTAL_CALL, OPEN_INT_TOTAL_PUT
  ```

### 表 13（彭博表 5）：华尔街四大行宏观金融条件指数（FCI）与经济惊奇指数（日频）
* **金融条件指数（8 个）**：`BFCIUS Index`（彭博美国）、`BFCIEU Index`（彭博欧元区）、`BFCICN Index`（彭博中国）、`GSUSFCI Index`（高盛美国）、`GSEUFCI Index`（高盛欧元区）、`GSCNFCI Index`（高盛中国）、`NFCI Index`（芝加哥联储金融条件）、`ANFCI Index`（调整后 NFCI）。
* **经济惊奇指数（8 个）**：`ECSURPUS Index`（彭博美国）、`ECSURPEU Index`（彭博欧元区）、`ECSURPCN Index`（彭博中国）、`CESIUSD Index`（花旗美国经济惊奇指数）、`CESIEUR Index`（花旗欧元区）、`CESICHN Index`（花旗中国）、`CESIJPY Index`（花旗日本）、`CESIG10 Index`（花旗 G10）。

### 表 14（彭博表 6）：美元回购压力、离岸交叉货币基差互换、TIPS 实际利率与北美 CDX 信用违约互换（`5min` + `1d`）
* **美元货币市场与美债实际利率（15 个）**：`SOFRRATE Index`（SOFR）、`EFFR Index`（联邦基金有效利率）、`OBFR Index`（隔夜银行融资利率）、`FRAOISUS Index`（美国 FRA-OIS 银行间压力）、`USBMMY3M Index`、`USGG2YR Index`、`USGG5YR Index`、`USGG10YR Index`、`USGG30YR Index`、`USYC2Y10 Index`（2s10s 斜率）、`USYC5Y30 Index`（5s30s 斜率）、`USGGBE05 Index`（5Y 通胀预期）、`USGGBE10 Index`（10Y 通胀预期）、`USGGT05Y Index`（5Y TIPS 实际利率）、`USGGT10Y Index`（10Y TIPS 实际利率）。
* **离岸美元交叉货币基差互换（4 个）**：`JYBS3 Curncy`（日元 3M 交叉货币基差）、`JYBS1 Curncy`（日元 1Y 基差）、`EUBS3 Curncy`（欧元 3M 基差）、`SFBS3 Curncy`（瑞郎 3M 基差）。
* **北美信用违约互换（CDS）与 OAS 利差（4 个）**：`CDX IG CDSI GEN 5Y Corp`（北美投资级 CDX 5Y）、`CDX HY CDSI GEN 5Y Corp`（北美高收益级 CDX 5Y）、`LF98OAS Index`（彭博高收益债 OAS）、`LUACOAS Index`（彭博投资级 OAS）。

### 表 15（彭博表 7）：彭博宏观经济日历（`ECO`）导出（含彭博调查中位数、均值、最高、最低与预测标准差）
1. 在彭博黑色终端窗口顶部命令行输入 **`ECO <GO>`** 按回车。
2. 左上角 `Region` 选择 **`United States`**、**`China`**、**`Eurozone`**、**`Japan`**；`Relevance` 筛选 **`Medium & High`**。
3. 点击右上角 **`Actions`** ➔ **`Export to Excel`**，导出 2024 年至今及未来 60 天的完整日历（包含 `Median Estimate`, `Average Estimate`, `High Estimate`, `Low Estimate`, `Standard Deviation`, `Actual`），保存为 **`hku_bbg_15_eco_calendar.xlsx`**。

## 第三部分：港大经管学院 Wind 万得终端（亚洲盘专属数据）全量 4 大表下载指南

在经管学院机房打开 **Wind 金融终端**（或 Excel 顶部的 **`Wind`** 插件选项卡）：

### 表 16（Wind 表 1）：香港 6 只现货比特币/以太坊 ETF（港币+美元双柜台）5 分钟 K 线、IOPV 净值与每日申赎份额
* **标的代码清单（12 个代码）**：
  * 港币柜台：`3042.HK`（华夏比特币）、`3046.HK`（华夏以太币）、`3008.HK`（博时 HashKey 比特币）、`3009.HK`（博时 HashKey 以太币）、`3439.HK`（嘉实比特币）、`3179.HK`（嘉实以太币）
  * 美元柜台：`9042.HK`、`9046.HK`、`9008.HK`、`9009.HK`、`9439.HK`、`9179.HK`
* **导出内容**：① `5min` K 线（`open, high, low, close, volume, amt`）；② 日频指标（`nav` 单位净值、`iopv` 盘中参考净值、`unit_total` 基金总份额每日申赎变动），保存为 **`hku_wind_16_hk_crypto_etf.xlsx`**。

### 表 17（Wind 表 2）：港股数字资产与 Web3 核心关联标的（`5min` + `1d`）
* **标的代码清单**：`0863.HK`（OSL 集团）、`1611.HK`（新火科技）、`0434.HK`（博雅互动）、`1357.HK`（美图公司）、`3066.HK`（FA 南方比特币期货 ETF）、`3068.HK`（FA 南方以太币期货 ETF），保存为 **`hku_wind_17_hk_web3_equities.xlsx`**。

### 表 18（Wind 表 3）：亚洲盘人民币与港元资金面高频拆借利率与央行流动性投放（日频 + `1h`）
* **指标清单**：离岸人民币香港银行间同业拆息 `CNH HIBOR`（隔夜、`1W`、`1M`）、香港银行同业拆息 `HKD HIBOR`（隔夜、`1W`、`1M`）、香港金管局银行体系总结余、中国银行间存款类机构质押式回购利率 `DR001` / `DR007` / `R007`、中国央行公开市场逆回购每日净投放额（亿元），保存为 **`hku_wind_18_asia_liquidity.xlsx`**。

### 表 19（Wind 表 4）：亚洲时段早盘风险情绪基准指数（`5min` K 线，填补美股休市窗口）
* **标的代码清单**：恒生科技指数（`HSTECH.HI`）、恒生指数（`HSI.HI`）、日经 225 指数（`N225.GI`）、韩国 KOSPI 200 指数（`KS200.GI`）、富时中国 A50 指数期货，保存为 **`hku_wind_19_asia_indices_5m.xlsx`**。

## 第四部分：完整内嵌的港大（HKU）WRDS + Bloomberg 全自动一键拉取脚本（`fetch_hku_all_in_one.py`）

> **使用方法**：把下面整段代码复制保存为 **`fetch_hku_all_in_one.py`**：
> 1. **在宿舍/图书馆连港大 Wi-Fi 跑 WRDS 全量 8 张表**：
>    ```bash
>    pip install wrds pandas
>    python fetch_hku_all_in_one.py --mode wrds --wrds-user 你的港大WRDS用户名
>    ```
> 2. **在梁銶琚楼 Bloomberg Lab 彭博电脑跑彭博全量表**：
>    ```bash
>    pip install blpapi pandas
>    python fetch_hku_all_in_one.py --mode bloomberg
>    ```

```python
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

# ============================================================================
# 1. BLOOMBERG FULL UNIVERSE DEFINITIONS
# ============================================================================

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


def fetch_bloomberg_all(out_dir: Path) -> None:
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

    # 1. Daily Macro, Options Skew, ETF Flows & Short Interest (2023-01-01 to Today)
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

    # 2. Intraday 5-Minute Bars (TRADE, BID, ASK) for past 180 days
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


# ============================================================================
# 2. WRDS FULL 8-DATABASE SQL DEFINITIONS
# ============================================================================

def fetch_wrds_all(out_dir: Path, wrds_username: str | None = None) -> None:
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
        # Table 1: NYSE TAQ Millisecond Intraday Microstructure Indicators (Spreads, Kyle's Lambda, Order Imbalance)
        "hku_wrds_1_taq_microstructure.csv": f"""
            SELECT *
            FROM taq.wrds_iid_2024
            WHERE sym_root IN ({tickers_sql})
            UNION ALL
            SELECT *
            FROM taq.wrds_iid_2025
            WHERE sym_root IN ({tickers_sql})
        """,
        # Table 2: OptionMetrics IvyDB US Standardized Volatility Surface (10D, 25D, 50D Call & Put Skew)
        "hku_wrds_2_optionmetrics_surface.csv": f"""
            SELECT s.ticker, v.date, v.days, v.delta, v.cp_flag,
                   v.impl_volatility, v.impl_strike, v.impl_premium, v.dispersion
            FROM optionm.vsurfd2024 v
            JOIN optionm.securd s ON v.secid = s.secid
            WHERE s.ticker IN ({tickers_sql})
              AND v.days IN (10, 30, 60, 91, 182)
        """,
        # Table 3: OptionMetrics IvyDB US Standardized Options & Open Interest / Greeks
        "hku_wrds_3_optionmetrics_stdopd.csv": f"""
            SELECT s.ticker, o.date, o.days, o.cp_flag,
                   o.forward_price, o.strike_price, o.impl_volatility,
                   o.delta, o.gamma, o.vega, o.theta
            FROM optionm.stdopd2024 o
            JOIN optionm.securd s ON o.secid = s.secid
            WHERE s.ticker IN ({tickers_sql})
              AND o.days IN (30, 60, 91)
        """,
        # Table 4: RavenPack News Analytics (High-Relevance Macro & Crypto Sentiment)
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
        # Table 5: FINRA TRACE Enhanced Corporate & Convertible Bond Institutional Trades (MSTR, COIN, MARA)
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
        # Table 6: Compustat Supplemental Short Interest
        "hku_wrds_6_short_interest.csv": f"""
            SELECT gvkey, iid, ticker, datadate, shortint, shortintadj, splitadjdate
            FROM comp.sec_shortint
            WHERE ticker IN ({tickers_sql})
              AND datadate >= '2023-01-01'
        """,
        # Table 7A: Thomson / LSEG 13F Institutional Holdings
        "hku_wrds_7a_13f_holdings.csv": f"""
            SELECT fdate, mgrname, ticker, cusip, shares, prc, shrout1
            FROM tfn.s34
            WHERE ticker IN ({tickers_sql})
              AND fdate >= '2023-01-01'
        """,
        # Table 7B: Thomson / LSEG Insider Transactions (Form 4)
        "hku_wrds_7b_insider_trades.csv": f"""
            SELECT ticker, trandate, personname, rolecode1, trancode, shares, tprice
            FROM tfn.table1
            WHERE ticker IN ({tickers_sql})
              AND trandate >= '2023-01-01'
        """,
        # Table 8A: CRSP Daily Stock & ETF Bid/Ask/Volume/Turnover
        "hku_wrds_8a_crsp_daily.csv": f"""
            SELECT n.ticker, d.date, d.prc, d.vol, d.ret, d.bid, d.ask, d.bidlo, d.askhi, d.shrout
            FROM crsp.dsf d
            JOIN crsp.dsenames n
              ON d.permno = n.permno
             AND d.date BETWEEN n.namedt AND COALESCE(n.nameendt, CURRENT_DATE)
            WHERE n.ticker IN ({tickers_sql})
              AND d.date >= '2023-01-01'
        """,
        # Table 8B: CRSP Daily US Treasury Yields & Returns
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
        fetch_bloomberg_all(args.out_dir)
    else:
        fetch_wrds_all(args.out_dir, wrds_username=args.wrds_user)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```
