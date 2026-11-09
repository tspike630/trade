# 香港大学（HKU）金融数据下载「零基础小白指南」

> **给港大同学的说明**：非常感谢帮忙！这份指南把所有操作拆成了**「直接复制粘贴公式 / 点网页按钮」**的步骤，不需要懂金融或量化，总共只需要 **20～30 分钟**，最后把导出的 CSV / Excel 文件发回给我即可。

## 任务一：在宿舍/图书馆就能下 —— 港大图书馆 WRDS 数据库（网页点选，约 10 分钟）

**准备工作**：连上港大校园网 Wi-Fi（或 HKU VPN），打开港大图书馆数据库入口登录 **WRDS（Wharton Research Data Services）**：
1. 打开港大图书馆查找 `WRDS`（或直接访问 `https://wrds-www.wharton.upenn.edu/`），用你的 `@connect.hku.hk` / `@hku.hk` 账号登录。

### 表 1：下载美股加密 ETF 与科技股的期权隐含波动率曲面（OptionMetrics）
1. 登录后，在首页点击 **`OptionMetrics`** ➔ **`IvyDB US`** ➔ **`Volatility Surface (vsurfd)`**。
2. **Step 1（Date Range）**：选择 **`2023-01-01`** 到 **最新日期**。
3. **Step 2（Company Codes）**：选择 **`Ticker`**，直接把下面这一行复制粘贴进去：
   ```text
   IBIT BITO COIN MSTR MARA RIOT NVDA QQQ SPY HYG TLT GLD
   ```
   *（下方筛选条件：Days to Expiration 填 `30, 60, 91`；Call/Put 选 `Both`）*
4. **Step 3（Query Variables）**：全选 **`Days to Expiration`**、**`Delta`**、**`Implied Volatility`**、**`Implied Strike Price`**、**`Implied Premium`**、**`Dispersion`**。
5. **Step 4（Output Format）**：选择 **`Comma-delimited text (*.csv)`**，点击 **`Submit Query`**，等网页生成下载链接后保存为 **`hku_wrds_optionmetrics.csv`**。

### 表 2：下载 RavenPack 毫秒级机构新闻情绪数据（RavenPack News Analytics）
1. 回到 WRDS 首页，点击 **`RavenPack`** ➔ **`RavenPack Edge / Dow Jones & Web Edition`**。
2. **Step 1（Date Range）**：选择 **`2025-01-01`** 到 **最新日期**。
3. **Step 2（Company / Entity）**：选 **`All`**（如果文件太大，可筛选 `Relevance >= 90` 且 `Event Similarity Days >= 1`）。
4. **Step 3（Variables）**：勾选 **`TIMESTAMP_UTC`**、**`ENTITY_NAME`**、**`COUNTRY_CODE`**、**`ESS` (Event Sentiment Score)**、**`ENS` (Event Novelty Score)**、**`CSS` (Composite Sentiment Score)**、**`RELEVANCE`**、**`HEADLINE`**。
5. **Step 4（Output）**：选择 **`.csv`**，下载保存为 **`hku_wrds_ravenpack_news.csv`**。

### 表 3：下载美股现货 ETF 与股票的日频盘口价差与换手（CRSP Daily Stock）
1. 回到 WRDS 首页，点击 **`CRSP`** ➔ **`Stock / Security Files`** ➔ **`Daily Stock File`**。
2. **Step 1（Date Range）**：**`2024-01-01`** 到 **最新日期**。
3. **Step 2（Ticker）**：复制粘贴下面这一行：
   ```text
   IBIT FBTC ARKB BITB GBTC ETHA BITO COIN MSTR MARA RIOT CLSK HOOD NVDA QQQ HYG TLT
   ```
4. **Step 3（Variables）**：勾选 **`TICKER`**、**`PRC` (Price)**、**`VOL` (Volume)**、**`RET` (Return)**、**`BID`**、**`ASK`**、**`SHROUT` (Shares Outstanding)**。
5. **Step 4（Output）**：选择 **`.csv`**，下载保存为 **`hku_wrds_crsp_daily.csv`**。

## 任务二：港大经管学院 Bloomberg Lab（彭博终端 Excel 插件傻瓜式导出，约 15 分钟）

**地点**：港大梁銶琚楼（K.K. Leung Building）经管学院 **Bloomberg Lab**（找贴有黄色/彩色贴纸键盘的彭博专用电脑）。

### 第 1 步：登录彭博并打开带插件的 Excel
1. 双击桌面上的 **Bloomberg** 图标，用你的港大彭博账号登录（如果没有账号，点 `Create a New Login` 用 `@connect.hku.hk` 邮箱当场注册，2 分钟搞定）。
2. 保持彭博窗口开着，打开电脑上的 **Microsoft Excel**，确认顶部菜单栏有一个 **`Bloomberg`** 选项卡。
   *（如果 Excel 顶部没有 `Bloomberg` 选项卡：点电脑左下角开始菜单 ➔ `All Programs` ➔ `Bloomberg` ➔ `Install Office Add-ins` ➔ 点 `Install` 再重开 Excel 即可。）*

### 第 2 步：导出「表 4：美股/港股比特币 ETF 盘中 5 分钟实时净值（iNAV）与价格」
1. 在 Excel 里新建一个空白工作表（Sheet1）。
2. 在第一行横向依次填入下面 **14 个标的代码**（分别放在 `A1`, `D1`, `G1`... 每个标的留 3 列，或者直接在 `Bloomberg` 顶部菜单点 **`Spreadsheet Builder`** ➔ **`Intraday Bars`**）：
   ```text
   IBIT US Equity, IBITIV Index, FBTC US Equity, FBTCIV Index, ETHA US Equity, ETHAIV Index, 3042 HK Equity, 3046 HK Equity, 3008 HK Equity, 3439 HK Equity, XBTUSD Curncy, XETUSD Curncy, BTCA Comdty, NQ1 Index
   ```
   * **最省事的公式法**：你只需要在空白 Sheet 的 **`A1`** 单元格直接粘贴下面这一行公式并按回车（会自动拉出贝莱德比特币 ETF 过去 180 天的 5 分钟 K 线）：
     ```excel
     =BDH("IBIT US Equity","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
     ```
   * 然后在旁边 **`G1`** 单元格粘贴下面这一行公式（会自动拉出贝莱德比特币 ETF 盘中每 5 分钟的**真实基金净值 iNAV**）：
     ```excel
     =BDH("IBITIV Index","OPEN,HIGH,LOW,LAST_PRICE","1/5/2026","","BarTp=T","BarSz=5")
     ```
   * 在 **`M1`** 单元格粘贴以太坊 ETF 及其盘中实时净值：
     ```excel
     =BDH("ETHA US Equity","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
     ```
   * 在 **`S1`** 单元格粘贴：
     ```excel
     =BDH("ETHAIV Index","OPEN,HIGH,LOW,LAST_PRICE","1/5/2026","","BarTp=T","BarSz=5")
     ```
   * 在 **`Y1`** 单元格粘贴香港华夏比特币现货 ETF（`3042 HK Equity`）5 分钟 K 线：
     ```excel
     =BDH("3042 HK Equity","OPEN,HIGH,LOW,LAST_PRICE,VOLUME","1/5/2026","","BarTp=T","BarSz=5")
     ```
3. 等表格数字全部加载出来（右上角不再显示 `#N/A Requesting Data...`）后，按 `Ctrl + S` 另存为 **`hku_bloomberg_etf_inav_5m.xlsx`**。

### 第 3 步：导出「表 5：华尔街宏观金融条件指数、经济惊奇指数与期权偏度（日频）」
1. 在 Excel 里新建第二个工作表（Sheet2）。
2. 在 **`A1`** 单元格直接粘贴下面这一行公式（拉取华尔街宏观金融条件与恐慌指数 2024 年至今的日频数据）：
   ```excel
   =BDH("BFCIUS Index","PX_LAST","20240101","")
   ```
3. 依次在旁边的 `D1`、`G1`、`J1`、`M1`、`P1`、`S1` 单元格粘贴以下公式：
   * **`D1`**（高盛美国金融条件指数）：`=BDH("GSUSFCI Index","PX_LAST","20240101","")`
   * **`G1`**（彭博美国经济惊奇指数）：`=BDH("ECSURPUS Index","PX_LAST","20240101","")`
   * **`J1`**（美债波动率指数 MOVE）：`=BDH("MOVE Index","PX_LAST","20240101","")`
   * **`M1`**（黑天鹅偏度指数 SKEW）：`=BDH("SKEW Index","PX_LAST","20240101","")`
   * **`P1`**（贝莱德 IBIT 期权隐含波动率、25D 偏度与每日申赎资金流）：
     ```excel
     =BDH("IBIT US Equity","PX_LAST,FUND_NET_ASSET_VAL,FUND_FLOW,SH_OUT,IMP_VOL_ATM,25D_RISK_REVERSAL_1M,PUT_CALL_OPEN_INTEREST_RATIO","20240110","")
     ```
   * **`Y1`**（日元 1 个月期权风险逆转偏度）：`=BDH("USDJPY25R1M BGN Curncy","PX_LAST","20240101","")`
4. 等数据加载完成后，另存为 **`hku_bloomberg_macro_and_options.xlsx`**。

## 任务三：如果你会用 Python（最省事的一键全自动方式）

如果你习惯跑 Python 脚本，不用手工粘 Excel 公式，直接用我写好的 [`fetch_hku_wrds_and_bloomberg.py`](../scripts/fetch_hku_wrds_and_bloomberg.py)：
1. **在彭博电脑上**打开命令行（Command Prompt），运行：
   ```bash
   pip install blpapi pandas
   python fetch_hku_wrds_and_bloomberg.py --mode bloomberg
   ```
2. **在自己电脑上**连港大 Wi-Fi，运行：
   ```bash
   pip install wrds pandas
   python fetch_hku_wrds_and_bloomberg.py --mode wrds --wrds-user 你的WRDS用户名
   ```
3. 脚本会自动在当前目录生成 `hku_exported_data/` 文件夹，里面包含所有整理好的 `.csv` 文件，直接打包发我就行！
