# 执行层 — 调研映射（M-4 / M-5 / M-7）

**Time (HK / UTC+8):** 2026-10-14  
**用途：** 把 2026-10-14 调研里执行层认领的三条，写成已经拍板的不变量。不改代码默认，不改 TP/SL/hold，不改 PM 数值，不写 `.env`，不加仓位代码。

本文件**不算有效性**。到 2026-10-19 周检之前，这些映射只是纪律，不是调参依据。

来源是执行管理层已定的确认项。文献只引用，不在这里新造参数。参数表现状仍以 [`P1_PM_ROADMAP.md`](P1_PM_ROADMAP.md) 为准；警示侧例仍以 [`WARNING_CASES.md`](WARNING_CASES.md) 为准。

执行层认领 **M-4、M-5、M-7**。M-1 / M-2 / M-6 不在本文件。M-3 只留一行归属，不写指标规格。

## M-4 交易层确定性外置（确认项）

PM / RISK 是服务层硬检查，写在 Python 里，不写在 LLM prompt 里。Agent 不能覆盖。

验收不变量（保持，不重写）：

1. `execution/gateway.py` 在校验通过之后、执行器之前调用 `PositionManager.check_open`（`execution/position_manager.py`）。
2. 通过之后走 `_pretrade_risk`，检查在 `execution/risk/limits.py`（`max_open`、`daily_loss`）。风控开关关着时这条不跑；开着时缺账本 fail-closed。
3. 拒绝是回执与 `order_events`，不是一句提示词。模型自信犯错，这两道仍然生效。

代码默认仍关。2026-10-14 杨授权的 paper 进程覆盖见 [`P1_PM_ROADMAP.md`](P1_PM_ROADMAP.md)。本条不改那些开关，也不打开 Live。

文献（外部数字，不是本系统参数）：

- **Raven-Agent**（[arXiv:2607.03015](https://arxiv.org/abs/2607.03015)）：交易层在 prompt 外、确定性、模块化。受控回放里 full 配置是唯一正收益（约 +15.9%）。风险约束在模型自信犯错时仍然生效。
- **Prediction Arena**（[arXiv:2604.07355](https://arxiv.org/abs/2604.07355)）：prompt 级风控不可靠。唯一起效的是硬编码上限加 solvency 检查。

不要做：不要把 PM / RISK 改写成 prompt 条款。不要加「agent 可跳过」的开关。

## M-5 仓位顺序：先选择、后仓位

顺序是三段。先有选择，再谈仓位，最后才是硬风控。本文件不新增 sizing 代码。

| 顺序 | 段 | 已经在哪 | 本层 |
|------|----|----------|------|
| 1 | 选择 / 过滤 | 决策 `min_liq`：`DECISION_MIN_LIQ_USD` 默认 **50000**（[PR #41](https://github.com/tspike630/trade/pull/41)）。cooldown：代码默认 `DECISION_COOLDOWN_SEC=0`；paper 观察进程是 3600，不要把代码默认改成 3600。执行网关在填单之前只放行 `purpose=live_candidate` | 只守 purpose 门禁。不改流动性地板，不改冷却默认 |
| 2 | 仓位 | 当前固定小仓。信号 `size_pct` 来自 `DECISION_JEV_POSITION_PCT`，默认 **0.05**（`DECISION_ENGINE_STEP1.md`）。执行层照单填写，不重算 | **Kelly 与方案 7 冻结。** 不写新的仓位公式，不改 0.05 |
| 3 | 硬风控 | PM 去重 / `max_open`，加上日亏。已开的 paper 数值见 [`P1_PM_ROADMAP.md`](P1_PM_ROADMAP.md) | 不在本映射里改数 |

网关看到的行，选择已经发生过：`min_liq` 与 cooldown 未过的 buy 在写入 `decision_log` 之前就被丢掉。留下的行先过 purpose，仓位用信号上的固定 `size_pct`，然后才是 `check_open` 与 `_pretrade_risk`。不要在这两道硬检查之间插入 Kelly。

文献：Raven-Agent 消融里，没有选择的 edge 比例仓位约 **−55.5%**，比固定 $10 的约 **−10.7%** 更差。选择和风控过滤是仓位的前提。这两个数字解释顺序，不写成我们的仓位或止损。

不要做：不要先上 Kelly 再补过滤。不要在执行层另写一档 `size_pct`。

## M-7 滑点随仓位非线性

**PolyBench**（[arXiv:2604.14199](https://arxiv.org/abs/2604.14199)）：仓位从约 $100 放到约 $1000 时，alpha 被订单簿滑点吃掉。这支持已经定下的 **L2 ≤10U**，以及当前小额 paper 仓位（上节的固定 0.05）。不新开一档，不把 10U 写进新的环境变量。

单市场 ≤15% 保持 **R2-pending**。这是 Prediction Arena 的硬顶，在本层记为 **P-2**。它不是周检表里复盘的 P-2（真库 IC/IR，`DECISION_ENGINE_WEEKLY_CHECK.md`）。登记在 [`P1_PM_ROADMAP.md`](P1_PM_ROADMAP.md) 的未来项。

- **不启用** `PM_MAX_PER_TOKEN_PCT`。代码默认仍关，paper 授权组里也没有它。
- 不在盘中把 15 写成参数。`execution/README.md` 里该变量的 suggest **10** 是既有说明，本文件不把它改成 15。

`execution/pricing.py` 的滑点档仍是成本模型，不是盘中调参旋钮：`<50k` 或未知 → 2%，`50k~500k` → 1%，`≥500k` → 0.3%。它们不拒绝开仓。解释见 [`WARNING_CASES.md`](WARNING_CASES.md)。不改档位。

不要做：不要为了文献里的 $1000 衰减去改滑点档。不要用 15% 在盘中封顶。

## M-3 一行（规格不在本层）

因子代码的 operatorization 归指标加工层。执行层碰到指标相邻的辅助代码时，不绕过因果算子：时间序列只允许截止当前 bar 的窗口。指标规格不在本文件。

## E-2 一行（未实现）

**C4** `target_band_failover` 已登记为 **WAITING / 只观察**，与复盘 C3 并列、不并入。见 [`WAITING_ITEMS.md`](WAITING_ITEMS.md)。不改运行时，不改 TP/SL/hold，不启用新闸门。**不算有效性。**

## 归属

| 项 | 谁做 | 本文件 |
|----|------|--------|
| PM / RISK 硬检查留在 gateway → position_manager / risk | 执行 | M-4 验收不变量。不改默认 |
| 先选择、后固定小仓、再硬风控 | 决策过滤 + 执行门禁 | M-5。Kelly / 方案 7 冻结 |
| L2 ≤10U 与小额 paper；滑点档只记账 | 执行成本模型 | M-7。不改档 |
| 单市场 ≤15% | 未来，R2 | 只登记。不启用 `PM_MAX_PER_TOKEN_PCT` |
| operatorization | 指标加工层 | 一行。不写指标规格 |
| C4 / E-2 `target_band_failover` | 执行观察 | 只登记。见 [`WAITING_ITEMS.md`](WAITING_ITEMS.md)。不改运行时 |
| 10-19 周检 | 复盘 | 本文件不算有效性 |

## 不算有效性

- 文献里的约 +15.9%、约 −55.5%、约 −10.7%、约 $100 / $1000、15% 都是外部数字。不是本系统门槛，也不是 10-19 的过关样本。
- 到 2026-10-19 周检之前，不据此改 SL%、滑点档、净边际、账户回撤，或打开 `PM_MAX_PER_TOKEN_PCT`。
- 纪律同 R1 / R2 / R3：`docs/REVIEW_R1_SPEC.md`、`docs/REVIEW_R3_SCAFFOLD.md`。没有 R2，15% 单市场上限只登记、不落代码。
