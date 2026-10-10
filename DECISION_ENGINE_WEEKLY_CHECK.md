# Decision Engine — JEV 周检 + 采样

**Time (HK / UTC+8):** 更新于 2026-10-10 13:26 HKT  
**Repo:** `/workspace/dexscreener-crawler`  
**原则：** 本机无系统 crontab（无 root 装 cron），由决策引擎 bot 的 routines **直接执行**仓库脚本；不接 paper/live；不杀 dry-run / main / indicators。

## 时序（周一当天）

| 时刻 (HKT) | Routine | 作用 |
|------------|---------|------|
| **09:15** | JEV 周检复盘 | 先跑 `scripts/jev_validity.py`（过滤版有效性 + 趋势 CSV + 决策表） |
| **10:19** | JEV 信号采样 | 再跑 `--once --min-jev-strength 0.15`，攒 `decision_log` |

**当天采样不计当天周检**：周一 10:19 采样写入的 buy **不进入**当天 09:15 已跑完的周检窗口；要等到**下周一**周检才会计入。周检在前、采样在后，避免「边采边验」自污染。

三线不冲突：24h 常驻 dry-run（默认 0.3）/ 周一三五采样（0.15 once）/ 周一周检（只读复盘）各自独立进程与日志，互不杀停。

## 两层怎么分工

| 层 | 做什么 |
|----|--------|
| **Bot routine「JEV 信号采样」** | 周一/三/五 **10:19** 跑 `--once --min-jev-strength 0.15`，攒 `decision_log` |
| **Bot routine「JEV 周检复盘」** | 每周一 **09:15** 跑 `scripts/jev_validity.py` + `python -m review.run --period 7d --report`，按决策表复盘并发 md |

若日后本机有 crontab，可把执行迁回系统 cron；bot 只做复盘即可。

## 已创建的 routines

| 名称 | 时间 (HKT) | 动作 |
|------|------------|------|
| JEV 周检复盘 | 周一 09:15 | 跑过滤版有效性 + R1 周报 + 决策表建议发本聊天 |
| JEV 信号采样 | 周一/三/五 10:19 | dry-run once（0.15），短结果发本聊天 |

24h 常驻 dry-run（默认 0.3）继续挂着：见 `DECISION_ENGINE_DRYRUN_24H.md` / `logs/decision_engine_dryrun.pid`。

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
.venv/bin/python -m decision_engine.run --strategy jev_strength_v1 --dry-run --once --min-jev-strength 0.15 \
  >> logs/decision_engine_sample.log 2>&1
```

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


## 背景（保留）

- 过滤后方向性 37.5%→71.4% 值得继续验证，但当前 n=14 / **unique_token=9** 不够开 Step 2。
- 分层单调（越强越高）若持续成立，支持默认阈值 0.3 方向。
- 覆盖率爬坡期：max jev≈0.205；默认常驻可能长期无信号。
