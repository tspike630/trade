# Decision Engine — JEV 周检 + 采样

**Time (HK / UTC+8):** 更新于 2026-10-17 00:32 HKT  
**Repo:** `/workspace/dexscreener-crawler`  
**原则：** 本机无系统 crontab（无 root 装 cron），由决策引擎 bot 的 routines **直接执行**仓库脚本；不接 paper/live；不杀 dry-run / main / indicators。

## 时序（周一当天）

| 时刻 (HKT) | Routine | 作用 |
|------------|---------|------|
| **09:15** | JEV 周检复盘 | 先跑 `scripts/jev_validity.py`（过滤版有效性 + 趋势 CSV + 决策表） |
| **10:19** | JEV 信号采样 | 再跑 `--once --min-jev-strength 0.15 --purpose sample`，攒 `decision_log`（`purpose=sample`，不是 paper 候选） |

**当天采样不计当天周检**：周一 10:19 采样写入的 buy **不进入**当天 09:15 已跑完的周检窗口；要等到**下周一**周检才会计入。周检在前、采样在后，避免「边采边验」自污染。

三线不冲突：dry-run（2026-10-10 那段默认 0.3 常驻，或临时 paper 观察）/ 周一三五采样（0.15 once，`purpose=sample`）/ 周一周检（只读复盘）各自独立进程与日志，互不杀停。临时 paper 观察的启动参数以 `DECISION_ENGINE_DRYRUN_24H.md` 的重启配方为准，不要和采样命令混用。

## 两层怎么分工

| 层 | 做什么 |
|----|--------|
| **Bot routine「JEV 信号采样」** | 周一/三/五 **10:19** 跑 `--once --min-jev-strength 0.15 --purpose sample`，攒 `decision_log`（`purpose=sample`） |
| **Bot routine「JEV 周检复盘」** | 每周一 **09:15** 跑 `scripts/jev_validity.py` + `python -m review.run --period 7d --report`，按决策表复盘并发 md |

若日后本机有 crontab，可把执行迁回系统 cron；bot 只做复盘即可。

## 已创建的 routines

| 名称 | 时间 (HKT) | 动作 |
|------|------------|------|
| JEV 周检复盘 | 周一 09:15 | 跑过滤版有效性 + R1 周报 + 决策表建议发本聊天 |
| JEV 信号采样 | 周一/三/五 10:19 | dry-run once（0.15，`purpose=sample`），短结果发本聊天 |

dry-run 的 pid 见 `DECISION_ENGINE_DRYRUN_24H.md` / `logs/decision_engine_dryrun.pid`。临时 paper 观察重启用该文的配方（`DECISION_COOLDOWN_SEC=3600`、config 默认 `min_liq` 50000、`--min-jev-strength 0.15 --purpose live_candidate`）。不要用 2026-10-10 那段默认 0.3、没有冷却的启动块把它拉起来。

## 周检命令（与 routine 一致）

```bash
cd /workspace/dexscreener-crawler
mkdir -p exports/reports logs
.venv/bin/python scripts/jev_validity.py \
  --db dexscreener.db \
  --min-strength 0.15 --min-liq 50000 --min-coverage 10 \
  --min-price 1e-06 --tol-sec 1800 \
  --out exports/reports/jev_validity_$(date +%Y%m%d).md \
  >> logs/jev_validity.log 2>&1

# R1 统一周报（同口径过滤；与 jev_validity 过滤后对账）
.venv/bin/python -m review.run --period 7d --report \
  --min-strength 0.15 --min-liq 50000 --min-coverage 10 --min-price 1e-06 \
  >> logs/review_weekly.log 2>&1
```

每次跑会**追加一行**到 `exports/reports/jev_validity_trend.csv`（含 `unique_token`、1h/4h 上涨率、0.19+ 层等），报告 md 含「对比上周」段。R1 另产 `exports/reports/review_weekly_YYYY-MM-DD.md`（全策略 + 执行层 + JEV 过滤后切片）。

采样：

```bash
.venv/bin/python -m decision_engine.run --strategy jev_strength_v1 --dry-run --once \
  --min-jev-strength 0.15 --purpose sample \
  >> logs/decision_engine_sample.log 2>&1
```

回测 CLI `--min-jev-strength` 的默认已与实时 `JEV_MIN_STRENGTH`（0.3）对齐。这条采样必须继续显式传 `0.15`，不要依赖回测或实时的默认值。

`purpose` 与 `mode` 分开：采样行是 `mode=dry_run` 且 `purpose=sample`；24h 常驻（默认 0.3，不传 `--purpose`）是 `mode=dry_run` 且 `purpose=live_candidate`。漏传 `--purpose sample` 时，只要 `--min-jev-strength` 低于实时默认，仍会写成 `sample`。执行网关应拒 `sample`；本命令只负责写入。

同一条 `scripts/jev_validity.py` 默认再读 paper `positions`（`--paper-db`，否则 `EXECUTION_PAPER_DB` / `PAPER_DB_PATH` / `paper_trading.db`；窗口 `--side-window-days` 默认 7），把硬旁证和 F-1 写进 md，并给趋势 CSV 加列。缺库则该段记未形成，不否决。`--no-side-evidence` 可跳过。只读，不改 cooldown / min_liq / strength，不熔断。

同一窗口再记 **信号拥挤**（`scripts/signal_crowding.py`）：`decision_log` 里 `jev_strength_v1` 的 buy，`purpose=live_candidate` 与 `purpose=sample` 分开。指标是 HKT 日均 buy、unique token / buy、峰值小时密度。写入周检 md，趋势 CSV 加 `crowd_lc_*` / `crowd_sample_*`。这是观察，**不否决 Step2**。`--no-side-evidence` 不关掉这一段。

```bash
python3 scripts/tests/test_weekly_side_evidence.py
python3 scripts/tests/test_signal_crowding.py
```

产物：`exports/reports/jev_validity_YYYYMMDD.md`、`exports/reports/jev_validity_trend.csv`、`exports/reports/review_weekly_YYYY-MM-DD.md`、`logs/jev_validity.log`、`logs/review_weekly.log`、`logs/decision_engine_sample.log`。

## 周检看什么

1. **过滤后 n 与 unique_token 是否增长**（有效样本优先 **unique_token≥10**，比 n≥15 更硬）
2. **1h 上涨率是否稳定 ≥55%**（连续 2 周，且两周均 unique_token≥10）
3. **strength 分层是否保持单调**（尤其 0.19+ 层 n→10+）
4. **信号拥挤（只观察）** — 同窗口 `live_candidate` 与 `sample` 的 HKT 日均 buy、unique/buy、峰值小时密度。不进上面的决策表，不否决 Step2

## 决策门槛

| 周检结果 | 动作 |
|----------|------|
| **连续 2 周** 1h 上涨率 ≥55% **且** unique_token≥10（同时 n≥15） | 启动 Step 2 回测（内置流动性 / 覆盖率过滤） |
| 上一行成立，但硬旁证任一为真（止损占亏≥50% **或** 单日开仓≥20） | **不可启动 Step2**。样本门槛不动 |
| n≥15 且 unique_token≥10 且上涨率 <50% | JEV 单因子降级为评分加分项；Step 2 转向技术面 |
| n<15，或 n≥15 但 unique_token<10，或 50–55% 边缘 | 继续攒样本，下周一再检 |


## 旁证分栏（硬否决 vs 健康度）

| 栏 | 来源 | 周检怎么用 | 改不改参 |
|----|------|------------|----------|
| **#71 / #72 硬旁证** | 止损占亏≥50% **或** 单日开仓≥20（`weekly_side_evidence` / 复盘同口径） | **Step2 硬否决**：样本门槛过了也不可启动 Step2 | **不改** strength / min_liq / cooldown / TP/SL |
| **#78 拥挤度** | `signal_crowding`：`live_candidate` / `sample` 的 HKT 日均 buy、unique/buy、峰值小时密度 | **健康度旁证**：**强制写入当周笔记**；**仍不否决** Step2 | **不改**运行参；不熔断；不盘中降级 |
| **滚动 1h 胜率斜率** | `scripts/jev_validity.py`「对比上周」观察脚注 | **只观察**。`delta_1h_win`（相对上周）+ `slope_1h_win`（近周 OLS，点/周）；**不改 ≥55% 门槛**，不单独否决 | 观察-only；不改运行参 |

硬否决只认 #71/#72。#78 与斜率脚注进健康度，进笔记，**不算有效性**。

## 统计功效标注

**不改样本门槛。** 上一张表照旧：有效样本优先 **unique_token≥10**（比 raw **n≥15** 更硬）；启动 Step 2 仍要 **连续 2 周** 1h 上涨率 **≥55%**，且两周都 unique_token≥10（同时 n≥15）。硬旁证（止损占亏≥50% 或 日开仓≥20）是额外否决，不降低也不替代这三道。本段只标注这些数字能主张什么。看板总览 JEV 闸的「统计功效旁证」与这里同一口径，都只解读。

| 现有样本能主张 | 现有样本不能主张 |
|----------------|------------------|
| unique_token≥10 且 raw n≥15，只够排除**纯噪声级**结论 | 过了这两条，或单周 1h≥55%，就证明 55% 这条线是真实 edge |
| 当前量级只作方向性观察。例如 W41：2026-10-13 周报过滤切片 n=32、unique_token=20、1h 上涨率 53.1% | 用这个 n 宣称已经检出约 2 个点的 edge |

Foresight Arena 的功效量级（已结算二元预测；相对市场共识的 Alpha，显著性 0.05、功效约 0.80）：检出 **α\*≈0.02** 约需 **~350** 笔（表内 348）；**α\*≈0.01** 约 **1392**；**α\*≈0.03** 约 **155**（arXiv:2605.00420，Table 1）。对照的是 Alpha Score 的样本量级；周检 1h 上涨率仍按决策表单独读。周检门槛远低于「证明 α\*≈0.02」所需的已结算笔数。

临时 `--min-jev-strength 0.15` 且 `purpose=live_candidate` 的行保持未标记，**不算有效性**，也不计入上面的已结算笔数。登记口径仍是下文「2026-10-19」表里那一行：`scripts/jev_validity.py` 不按 purpose 过滤，报告里单独脚注，不要拿它们去过「连续 2 周 ≥55%」。

## 策略退役（并入决策表，无新模块）

| 周检结果 | 动作 |
|----------|------|
| **连续 N 周**（建议 N=2）过滤后 1h 上涨率 **<50%** 且 **unique_token≥10**（样本充足） | 机制建议 **降级/停用该策略**（止损），**不要**进入 R3 继续调参 |
| 50–55% 边缘或样本不足 | 继续攒样本，下周一再检（与现表一致） |

原则：调参是「修」，退役是「止损」——无限调参本身就是过拟合的一种。现阶段 JEV 样本未跨门槛，本规则只写进决策表，不另写代码。


## 2026-10-19 周检要登记的证据

下周一（**2026-10-19**）周检除了现有决策表，把下面三行记进当周笔记。这是观察登记，不改阈值、不改默认强度、不把临时观察标成 accepted。

这三行对应执行单上的三条警示（半胜仍亏、薄池单笔抹盈、残余风险打穿）。映射到已有控制、以及为什么不在盘中改强度或再加过滤，见 `DECISION_ENGINE_WARNING_CASES.md`。仍**不算有效性**。

10:19 的采样命令保持不变：`--once --min-jev-strength 0.15 --purpose sample`。它和临时 paper 观察（`--purpose live_candidate` + `DECISION_COOLDOWN_SEC=3600`）不是同一条进程。重启配方见 `DECISION_ENGINE_DRYRUN_24H.md`。

| 证据 | 从哪看 | 怎么登记 |
|------|--------|----------|
| cooldown 是否生效 | paper 观察日志里的 `risk cooldown_sec=`，以及 `cooldown skip` | 启动行应是 `cooldown_sec=3600.0`。记下 skip 条数。`0` / `0.0` 表示重启时环境变量丢了。冷却在进程内存里，重启会清零，不要把重启后的第一笔 buy 当成冷却失效。键是 `(strategy_id, chain_id, token_address)`，3600s 不封顶跨票日开仓。不改 `DECISION_COOLDOWN_SEC` 代码默认（仍 0） |
| `min_liq` skip 次数 / 流动性分桶 | `reason=liq_below_min`、`reason=no_liquidity`，以及同行的 `liquidity_usd=` / `min_liq_usd=` | 分三桶计数：缺失（`no_liquidity`）、低于 50000（`liq_below_min`）、过闸后写入的 buy。`min_liq_usd=` 应为 `50000.0`。不要为了分桶改 50000 |
| 临时 `min=0.15` 观察行 | `decision_log` 里 `purpose=live_candidate` 且强度低于实时默认 0.3 的 buy | **保持未标记，不得标成 accepted。** 它们不是有效性样本。`scripts/jev_validity.py` 按 `strategy_id=jev_strength_v1` 的全部 buy 读取，**不按 purpose 过滤**，所以这些行会进入周检输入。报告里单独脚注，不要拿它们去过「连续 2 周 ≥55%」门槛 |

## 2026-10-19 复盘侧交付（P-2 / P-3）

上一节三行是决策引擎日志登记。下面是复盘拥有的同一天交付，细则在 `docs/REVIEW_WARNING_CASES.md`（`crawler/REVIEW_WARNING_CASES.md` 同文）。**不算有效性。** 不改上面的有效性决策表，不改阈值，不把临时 0.15 标成 accepted。

| 交付 | 从哪看 | 怎么登记 |
|------|--------|----------|
| **P-2** 真库 IC/IR 首跑 | 同一周检的 `review.run`，加上已有的 `--factor-layers`（IC 写入默认 `exports/reports/factor_ic_history.csv`） | 出数之后只写权重更新**设计**。一期只有 IC；不足两期则 IR 记「未形成」。`INDICATORS_WEIGHT_*` 保持 **0**，不激活。临时 `live_candidate` 且强度 0.15 **不得**写成 accepted |
| **P-3** `min_liq` 拒绝桶 + cooldown | 上一节前两行，外加平仓流动性桶 | 旁证，不是有效性。看 cooldown 是否在跑、`liq_below_min` / `no_liquidity` / 过闸 buy，以及已平仓 `liq_<50k` vs `≥50k`。不据此改 50000，不开第三道统计参 |
| **P-3 硬旁证** 止损占亏 ≥50% | `scripts/weekly_side_evidence.py`；paper `positions` 已平仓 | **硬旁证门槛，可否决 Step2。** 与看板 `stop_loss_share` 相同：净盈亏为负且有 `stop_loss` 时，`min(1, |Σ stop_loss pnl| / |净盈亏|)`。≥0.50 → `hard_旁证_stop_loss_share`。样本门槛过了也 **不可启动 Step2**。不算有效性 |
| **P-3 硬旁证** 日开仓 ≥20 | 同上，`opened_at` 按 HKT 日历日取窗口内峰值 | **硬旁证门槛，可否决 Step2。** `max_daily_opens` ≥20 → `hard_旁证_daily_opens`。与止损占亏任一为真即否决 |
| **G-1** 频率旁证 | 开仓计数，不是 cooldown 秒数 | 冷却键是 `(strategy_id, chain_id, token_address)`，不限制跨票日开仓。G-1 用 `max_daily_opens`（`positions.opened_at` 按 HKT 日；看板频率卡用 fills 的 UTC 日期前缀，都是单日开仓峰值）。**现在不改** `DECISION_COOLDOWN_SEC`（代码默认仍 0） |
| **G-4** `min_liq` 经典地板 | `DECISION_MIN_LIQ_USD`，回测 `FilterConfig.min_liq` | 经典回测地板 **50000** 已是买入闸（#41）。周检只记拒绝桶，不改 50000 |
| **F-1** 连续 2 亏之后的第 3 笔 | 周检 md「F-1 信号连亏（只记不熔断）」 | 周检优先 `up_1h<0`（没有 1h 时用已平仓 pnl）。看板 F-1 卡只看已平仓 pnl。两边都只观察，不接入 RiskManager / PM pause。熔断留 R2/R3 |
| **F-3 / C3** 出场 | `execution/exits.py`（C3 / E-2） | 出场归执行。决策层只指向，不改 hold / TP / SL |
| **⑥ 信号拥挤** | `scripts/jev_validity.py`「信号拥挤度」；`decision_log` 同窗口 | **健康度旁证（#78），不算有效性，不否决 Step2；强制写入当周笔记。** `live_candidate` 与 `sample` 分列：HKT 日均 buy、unique/buy、峰值小时密度。不改强度 / min_liq / cooldown，不熔断，不盘中降级。激活留 R2/R3 |
| 平仓归因三行 | paper 已平仓；看板 #45 旁证 | **胜率与期望分开**；**最差单笔**；**`liq_<50k` vs `≥50k`**（unknown 单列）。适用时标 **R2 规则缺口旁证**。不据此调 strength 或权重 |

目录与上面「周检命令」相同（`/workspace/dexscreener-crawler`）。这是 10-19 首跑，不把 `--factor-layers` 改成常驻默认。

```bash
.venv/bin/python -m review.run --period 7d --report --factor-layers \
  --min-strength 0.15 --min-liq 50000 --min-coverage 10 --min-price 1e-06 \
  >> logs/review_weekly.log 2>&1
```

复盘不认领 **P-1**（PM 仓位比例 / 回撤，`execution/README.md`）、**P-4**（JEV 错价，指标）、**P-5**（共识概率观察候选，指标）。**F-3 / C3**：出场归执行（`execution/exits.py`，C3 / E-2）；决策层只指向，不改 hold / TP / SL。趋势出场仍只观察，要立项先 R3 回测。`accepted` ≠ `activated`。临时 `live_candidate` 样本单独分桶。

## INTEL 边界（薄记）

周检不把情报候选写成生效策略。拥挤度是决策层自己的观察，不是 I-1/I-2 的校准结果。

| 项 | 周检怎么对待 |
|----|----------------|
| **I-1 / I-2** | 只读，直到方向校准完成。不在本层建表、不写采集器、不改权重 |
| **I-3** | 执行质量（`bot_trade_share` / `slippage_1h`）。归执行，决策周检不算 |
| **I-4** | OTC / 解锁时间表。只登记，决策周检不算 |
| **⑥ 信号拥挤** | 决策层观察（#78）。`live_candidate` 与 `sample` 分开。**强制写入当周健康度笔记**；不是硬旁证，不否决 |
| 候选 | 候选 ≠ 自动激活。`accepted` 仍不是 `activated` |
| Step2 | 仍只由 #71/#72 硬旁证否决（止损占亏≥50% 或单日开仓≥20）。#78 拥挤度不否决，但须进健康度笔记 |

## 10-19 复盘硬证据（代码计算）

`python -m review.run --report` 在周报第 8 节写出下面六项（`review/weekly_evidence.py`）。**不算有效性 / R2旁证。** 不改 JEV 门槛，不改 `min_liq` / cooldown / 权重，不接断路器。

| 交付 | 怎么算 |
|------|--------|
| 止损亏损占比 | 与看板相同：净已实现 PnL 为负且有 `stop_loss` 时 `min(1, \|Σ stop_loss pnl\| / \|Σ pnl\|)`，并列最差一笔止损。净正或无止损则为空 |
| 单日开仓峰值 | 只按 `opened_at` 的 Asia/Hong_Kong 日历日。没有 `opened_at` 记 data gap，不用 UTC 日期前缀，也不用 executed entries |
| F-1（两行，不合并） | (a) 有 `up_1h` 时 `up_1h<0` 为亏，0 不是亏，缺失打断连亏；(b) `python -m execution.f1_streak_report` 的 `streak_events`，`pnl<0` 为亏。各自的 P，不合成一个数 |
| C3 shadow | 优先 `order_events` 的 `shadow_c3`；否则 `shadow_c3` 表（peak / giveback / actual exit，含 `max_hold`）。都没有则 data gap，不从纸面 peak 编造 |
| G-3 | 有 `max_sequential_losses` 或 `floor(cash/per_trade_risk)` 导出则用该数；否则 `python -m execution.risk.bankruptcy` 的默认理论值（不是账本观察） |
| Step2 veto 旁证 | 止损占比≥50% 或单日开仓≥20 时写明：即使 1h≥55% 也不据此启动 Step2 |

## 背景（保留）

- 过滤后方向性 37.5%→71.4% 值得继续验证，但当前 n=14 / **unique_token=9** 不够开 Step 2。
- 分层单调（越强越高）若持续成立，支持默认阈值 0.3 方向。
- 覆盖率爬坡期：max jev≈0.205；默认常驻可能长期无信号。
