# AGENTS.md / CLAUDE.md — Institutional Upgrade & Data Manifest (2026-11-10)

> **致仓库内的 AI Agent（Claude Code / Cursor / Codex / OpenClaw）**：
> 本文件是当前量化交易系统最新的**架构、数据库表结构与策略路由权威说明书**。在做任何代码修改、参数调整、回测或实盘诊断前，请务必先阅读本清单。

## 1. 核心架构升级总览（Milestones 1–24 已上线，355 项单元测试全通）

本系统已从单一技术指标反转升级为**跨资产机构级五态自适应量化引擎**：

1. **五态市场状态路由器 (`decision_engine/core/regime_strategy_router.py`)**
   - 已挂载至主引擎 `decision_engine/core/engine.py` (`apply_five_state_regime_router`)。
   - **状态 1 (`regime_1_positive_gamma_pinned`)**：常态正 Gamma 震荡（约占 55% 时间）。标准剧本仍是 `tech_boll_fade_standard`（乘数 `1.00`）。杨于 **2026-11-12 13:08 HKT** 裁定：四个新开关全关时也**默认启用**鲸鱼倾斜，不另设开关。`whale_retail_divergence >= 1.35` 且（`funding_z` 缺失或 `<= 0.80`）时，`size_multiplier = 1.08`、`tp_multiplier = 1.22`（传入 `size_pct = 0.05` 得到 **0.054**）。熊向进入条件是 **`0.45 < whale_div <= 0.72`**（`0.45` 本身不进入；刚大于 `0.72` 且未到 `1.35` 回到标准 `1.00`）。进入后，`boll_z` 有值且 **`> -2.25`** 阻断浅跌、不发单；`boll_z` 缺失或 `<= -2.25` 用 `size_multiplier = 0.85`、`tp_multiplier = 0.72`（`0.05` 得到 **0.0425**）。`USE_OPTIMAL_KELLY_MATRIX` 打开后的状态 1 底仓仍是 **`18.5%`**，本次不改。
   - **状态 2 (`regime_2_short_squeeze_breakout`)**：CFTC 杠杆基金净空头拥挤 / 高空头矿企 / 币安极端负资金费率逼空，运行**三阶零本金风险 OCO 棘轮推仓 (`35.0% -> 70.0% -> 95.0%`)**。
   - **状态 3 (`regime_3_cross_asset_bull_pullback`)**：`NVDA.O` / `SMH.O` 1h 领涨 (`>= +1.2%`) 或截面广度 `>= 68%` 的跨资产牛市，剔除截面后 35% 弱势币。杨于 **2026-11-12 13:08 HKT** 裁定保留**联合条件**，覆盖本行原先「无条件拒绝」的歧义。薄盘口：`obi_top5` 有值且 `<= -0.25`，并且主动吃单**缺失或 `<= 0.45`**，才拒绝；吃单 `> 0.45` 不因薄盘口拒绝；**`obi` 缺失不因该项拒绝**。巨量：没有历史样本时阈值是 **`3.8`**；`volume_z >= 3.8` 并且 `whale_retail_divergence` **有值且 `<= 0.65`** 才拒绝；**巨量但鲸鱼指标缺失仍允许放行**；**`volume_z` 缺失不因该项拒绝**。对前排龙头运行**三阶 OCO 棘轮推仓 (`30.0% -> 60.0% -> 85.0%`)**，这些比例本次不改。
   - **状态 4 (`regime_4_crowded_long_defensive`)**：散户多头拥挤或宏观高波冲击，现货缩至 **`5.0%`** 极深接针仓 + 强制开启 **`50%` 永续合约 Beta 空单对冲**。
   - **状态 5 (`regime_5_crisis_liquidation_halt`)**：宏观黑天鹅 / `.VIX` / `.MOVE` / 日元风险逆转 (`JPY1MRR=`) 崩盘 / CME 期货贴水 (`BTCc2 < BTCc1`) / 连环清算瀑布，**100% 熔断拦截现货开多**（保留卖出通道）+ **`100%` 永续空单锁仓**。

2. **城大 (CityU) 机构级跨资产 Alpha 引擎 (`decision_engine/core/cityu_institutional_alpha.py`)**
   - 实时读取 CFTC COT 持仓 (`cot_regime`)、美股加密矿企空头比例 (`avg_miner_short_interest_pct`)、FINRA TRACE 可转债机构买盘占比 (`trace_conv_bond_buy_ratio`)、`NVDA.O`/`SMH.O` 1h 领先动量、美债 OIS 利差与 Orbis 银行脆弱度。

3. **LSEG 宏观聪明钱预期走廊与 V2 跨资产熔断器 (`decision_engine/core/macro_circuit_breaker.py`)**
   - 基于 56,933 条全球经济日历 (`macro_events`) 的 `[min_forecast, max_forecast]` 区间与 `predicted_surprise`：数据落入走廊内 **5 分钟极速解封**，超走廊黑天鹅执行 **90 分钟硬熔断**。
   - V2 跨资产门控涵盖 `.VIX`、`.MOVE`、`SVOL.P`、`JPY1MRR=` 以及 CME `BTCc2 - BTCc1` 期限结构。

4. **公开衍生品、盘口微观结构与永续 Beta 对冲 (`decision_engine/core/perp_microstructure_and_beta_hedge.py`)**
   - 融合币安永续大户/散户多空背离 (`whale_retail_divergence`)、现货 L2 盘口失衡 (`obi_top5`)、主动吃单比 (`taker_buy_ratio`)、跨所基准价差 (Coinbase/OKX/Bybit)、Deribit 期权 Gamma 墙与代币大额解锁排期。

5. **执行层与风控棘轮**
   - `decision_engine/core/dynamic_oco_ratchet.py`：硬件级币安 OCO 止损上移（浮盈 `+1.2%` 移至保本线以上解锁二阶仓位，浮盈 `+2.2%` 锁利解锁三阶满仓）+ 75 分钟时间止损。
   - `decision_engine/core/drawdown_and_kelly_budget.py`：策略组回撤熔断（Pod Drawdown Halt）与凯利风险预算。
   - `execution/executors/binance_spot.py`：支持 `LIMIT_MAKER` 智能挂单路由，节省吃单费率与滑点。

## 2. 本地与服务器 SQLite 数据库清单（注意：`*.db` 在 `.gitignore` 中，换机需 `rsync` 同步）

| 数据库相对路径 | 体量与行数 | 核心数据表（共 30 张表，约 923 万行） | 更新/构建脚本 |
| :--- | :--- | :--- | :--- |
| `data/lseg/lseg_macro.db` | ~171 MB (`836,474` 行) | `cross_asset_bars` (63 个 LSEG 跨资产 RIC + CBOE 波动率), `macro_events` (56,933 条 SmartEconomics 日历), `central_bank_events` (555 条央行日程) | `scripts/ingest_cityu_usb_all.py` / `scripts/fetch_cityu_lseg_incremental.py` |
| `data/cityu/cityu_institutional.db` | ~613 MB (`8,397,225` 行) | `wrds_optionmetrics_vsurfd` (372万行期权曲面), `wrds_crsp_treasuries` (360万行美债), `wrds_13f_holdings` (46万行13F机构持仓), `wrds_trace_convertible_bonds`, `lseg_cftc_cot_positioning`, `lseg_short_interest_daily`, `orbis_bank_liquidity_risk` 等 19 张表 | `scripts/ingest_cityu_usb_all.py` |
| `data/counterparty/public_derivatives.db` | 实时滚动更新 | `binance_perp_counterparty`, `binance_spot_microstructure`（`orderbook_imbalance_20` 仍是前 20 档；`obi_top5` 是排序后前 5 档 price×qty，且只带覆盖该 `lastUpdateId` 的深度事件时间）, `cross_exchange_spot_baseline`, `deribit_options_gamma`, `token_unlock_schedule`, `macro_net_liquidity`, `stablecoin_supply`, `onchain_holder_bot_summary`, `collector_fetch_errors` | `scripts/fetch_public_derivatives_and_counterparty.py --loop --interval 300 --db data/counterparty/public_derivatives.db` |

## 3. 关键环境变量与开关

- `LSEG_MACRO_DB_PATH`：默认自动定位 `data/lseg/lseg_macro.db`。
- `CITYU_INSTITUTIONAL_DB_PATH`：默认自动定位 `data/cityu/cityu_institutional.db`。
- `PUBLIC_DERIVATIVES_DB_PATH`：默认自动定位 `data/counterparty/public_derivatives.db`。
- `use_optimal_kelly_matrix`：信号行或配置开启后，自动按五态最优凯利底座 + 三阶 OCO 保本棘轮 (`18.5% / 95.0% / 85.0% / 5.0% / 0.0%`) 下发目标仓位。

## 4. 常用自检与验证命令

```bash
# 1. 运行全部 355 项单元测试
python3 -m unittest discover -s decision_engine/tests -p "test_*.py"

# 2. 公开衍生品与对手盘。默认单次；请求到的标的有缺失时退出码 2，锁被占用时退出码 3。
#    --loop 单轮失败不退出。PEPE 的 USD-M 合约是 1000PEPEUSDT，不是 PEPEUSDT。
#    行时间用交易所事件时间，不用本机刷新时间。不读私钥、不下单。
#    运行时没有这份脚本时，先用 crawler 组件同步；systemd 模板不自动安装：
#    deploy/public-derivatives-collector.service
python3 scripts/fetch_public_derivatives_and_counterparty.py --loop --interval 300 --db data/counterparty/public_derivatives.db

# 3. 查看全量 785MB 城大数据集实证归因报告
cat docs/CITYU_FULL_DATASET_BACKTEST_2026-11-09.md
```
