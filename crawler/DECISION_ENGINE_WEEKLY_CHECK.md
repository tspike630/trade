# Decision Engine — JEV 周检 + 采样

**Time (HK / UTC+8):** 更新于 2026-10-10 13:26 HKT  
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

产物：`exports/reports/jev_validity_YYYYMMDD.md`、`exports/reports/jev_validity_trend.csv`、`exports/reports/review_weekly_YYYY-MM-DD.md`、`logs/jev_validity.log`、`logs/review_weekly.log`、`logs/decision_engine_sample.log`。

## 周检看什么

1. **过滤后 n 与 unique_token 是否增长**（有效样本优先 **unique_token≥10**，比 n≥15 更硬）
2. **1h 上涨率是否稳定 ≥55%**（连续 2 周，且两周均 unique_token≥10）
3. **strength 分层是否保持单调**（尤其 0.19+ 层 n→10+）

## 决策门槛

| 周检结果 | 动作 |
|----------|------|
| **连续 2 周** 1h 上涨率 ≥55% **且** unique_token≥10（同时 n≥15） | 启动 Step 2 回测（内置流动性 / 覆盖率过滤） |
| n≥15 且 unique_token≥10 且上涨率 <50% | JEV 单因子降级为评分加分项；Step 2 转向技术面 |
| n<15，或 n≥15 但 unique_token<10，或 50–55% 边缘 | 继续攒样本，下周一再检 |


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
| cooldown 是否生效 | paper 观察日志里的 `risk cooldown_sec=`，以及 `cooldown skip` | 启动行应是 `cooldown_sec=3600.0`。记下 skip 条数。`0` / `0.0` 表示重启时环境变量丢了。冷却在进程内存里，重启会清零，不要把重启后的第一笔 buy 当成冷却失效 |
| `min_liq` skip 次数 / 流动性分桶 | `reason=liq_below_min`、`reason=no_liquidity`，以及同行的 `liquidity_usd=` / `min_liq_usd=` | 分三桶计数：缺失（`no_liquidity`）、低于 50000（`liq_below_min`）、过闸后写入的 buy。`min_liq_usd=` 应为 `50000.0`。不要为了分桶改 50000 |
| 临时 `min=0.15` 观察行 | `decision_log` 里 `purpose=live_candidate` 且强度低于实时默认 0.3 的 buy | **保持未标记，不得标成 accepted。** 它们不是有效性样本。`scripts/jev_validity.py` 按 `strategy_id=jev_strength_v1` 的全部 buy 读取，**不按 purpose 过滤**，所以这些行会进入周检输入。报告里单独脚注，不要拿它们去过「连续 2 周 ≥55%」门槛 |

## 背景（保留）

- 过滤后方向性 37.5%→71.4% 值得继续验证，但当前 n=14 / **unique_token=9** 不够开 Step 2。
- 分层单调（越强越高）若持续成立，支持默认阈值 0.3 方向。
- 覆盖率爬坡期：max jev≈0.205；默认常驻可能长期无信号。
