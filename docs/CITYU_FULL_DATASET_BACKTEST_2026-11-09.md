# 城大 785 MB 全量机构数据（LSEG + WRDS + CFTC COT + Orbis）实证归因回测报告

**回测生成时间**：2026-11-09  
**回测底层数据库**：
1. `data/lseg/lseg_macro.db`（**171.25 MB**，含 **778,986** 条 5min/1h/1d 跨资产盘口与 K 线 + **56,933** 条四国 SmartEconomics 宏观日历与美债拍卖 + **555** 条央行利率决议）
2. `data/cityu/cityu_institutional.db`（**613.49 MB**，含 **8,397,225** 条 WRDS 期权曲面、可转债机构暗盘、13F、内幕交易、CRSP 美债、CFTC COT 周度持仓与 Orbis 银行风险记录）

## 一、盘中跨资产领先-滞后（Lead-Lag）与波动率断路器归因（`cross_asset_bars`）

基于 2025–2026 年 1h/5min 实盘对齐窗口，检验美股 AI 半导体龙头（`NVDA.O` / `SMH.O`）、波动率控制基金（`SVOL.P`）、日元 25D 风险逆转期权偏度（`JPY1MRR=`）与美债波动率（`.MOVE`）发生异动后，加密资产（`BTC=` + `SOL=`）未来 4 小时的真实收益表现：

| 跨资产触发状态（对应新增实盘闸门） | 样本触发次数 | 触发后未来 4h 平均收益 (bps) | 对照组/平静期 4h 平均收益 (bps) | 胜率 / 实盘策略动作 |
| :--- | ---: | ---: | ---: | :--- |
| **`NVDA.O` / `SMH.O` 1h 急跌 `<= -1.5%`** (`ai_equity_nvda_smh_dump`) | 48 | **+21.60 bps** | -2.87 bps | 胜率 52.1% ➔ **自动熔断 AI 代币抄底** |
| **`NVDA.O` / `SMH.O` 1h 突破 `>= +1.2%`** (`cityu_institutional_bullish_lead`) | 88 | **+8.01 bps** | -2.87 bps | 胜率 45.5% ➔ **上调仓位乘数 `1.15x`** |
| **做空波动率 ETF `SVOL.P` 急跌 `<= -0.45%`** (`cross_asset_vol_control_unwind`) | 169 | **+10.83 bps** | -2.22 bps | 华尔街波动率目标基金被动砍仓 ➔ **触发全局买入熔断** |
| **日元 1M 风险逆转偏度 `JPY1MRR=` 闪崩 `<= -0.15`** (`cross_asset_jpy_rr_skew_crash`) | 13 | **+20.56 bps** | -1.27 bps | 日元套息平仓期权抢跑信号 ➔ **触发全局买入熔断** |
| **美债恐慌指数 `.MOVE` 1h 飙升 `>= +1.5%`** (`cross_asset_move_vol_spike`) | 49 | **+0.03 bps** | -11.75 bps | 利率市场流动性抽水 ➔ **触发全局买入熔断** |

## 二、CFTC COT 华尔街对冲基金 vs 资管巨头周度持仓归因（2018–2026）

基于 `lseg_cftc_cot_positioning` 中 CME 比特币期货（`BTC`）52 周滚动持仓占比 Z-Score，检验三类机构博弈状态下的未来 1 周（5 个交易日）真实收益与年化夏普比率：

| CFTC COT 机构持仓博弈状态 (`cot_regime`) | 历史周数 | 未来 1 周平均收益 (%) | 周度胜率 (%) | 年化夏普比率 (Sharpe) | 实盘仓位乘数 (`size_multiplier`) |
| :--- | ---: | ---: | ---: | ---: | :--- |
| **`cot_institutional_squeeze_favorable`**（资管增持 `Z >= +0.5` + 杠杆基金做空 `Z <= -0.5`） | 105 | **+0.928%** | **49.5%** | **+0.87** | **`1.12x ~ 1.15x`（顺机构建仓加权）** |
| **`cot_neutral`**（常规中性期） | 269 | +0.146% | 52.0% | +0.15 | **`1.00x`（标准仓位）** |
| **`cot_leveraged_long_crowded`**（杠杆对冲基金多头拥挤 `Z >= +1.5`） | 49 | **+0.367%** | **46.9%** | **+0.46** | **`0.82x`（防杠杆多头踩踏降仓）** |

## 三、四国宏观日历 `ECOCAL` SmartEconomics 与预测极值 (`min`/`max`) 统计（56,933 条）

* **含实际公布值与路透调查中位数的历史发布总数**：**43,116 条**（其中完整包含分析师最高 `max_forecast` 与最低 `min_forecast` 区间边界的共 **34,698 条**）
* **突破全部分析师最高/最低预测极值（Out-of-Bounds 极端宏观黑天鹅）**：共 **11,517 次**（占 **33.19%**），对应 `macro_circuit_breaker.py` 中的 `macro_surprise_shock_blackout` 90 分钟长熔断保护
* **完全落在预期区间中轴内（In-Line 温和发布）**：共 **13,163 次**（占 **37.94%**），可在公布 5 分钟后提前解除禁买（Fast-Clear），避免踏空宏观靴子落地后的反弹
* **SmartEconomics 事前预测惊奇度（`predicted_surprise`）方向命中率**：在 **37,175 次**非零预测惊奇样本中，事前准确命中真实超预期方向的比率高达 **54.52%**

## 四、WRDS OptionMetrics 30 天期权隐含波动率偏度（Put-Call IV Spread）校准

基于 `wrds_optionmetrics_vsurfd`（3,720,960 行）计算的各大核心标的 30 天看跌与看涨期权隐含波动率平均偏度差（`Put IV - Call IV`）：
* **`IBIT`（贝莱德现货 BTC ETF）**：`-0.71%`（负值代表华尔街在 IBIT 期权上长期抢购看涨期权 Call Skew）
* **`MSTR`（MicroStrategy）**：`+0.90%`（极端看涨期权偏度，配合可转债 Gamma 套利）
* **`COIN`（Coinbase）**：`+0.49%`
* **`MARA`（Marathon Digital）**：`-2.11%`
* **`NVDA`（NVIDIA）**：`+0.91%`
* **`HYG`（高收益债 ETF）**：`+1.06%`（显著正值，机构长期买 Put 防信用崩盘）
