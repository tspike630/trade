# PRIOR_STEP5 — decision_prior_tags 旁路打标

> 时间：2026-10-11（Asia/Hong_Kong）
> 定位：**指标加工层旁路**；自动化 step4 手工做过的 JEV×token_age×prior 打标
> 只碰：`models/prior_tables.sql`、`data/prior/db/prior.db`、`scripts/prior_tagger.py`、`review/data.py`（只读 helper）、本文件、`PRIOR_STEP5_PATCH.md`
> 审核补丁原文：`PRIOR_STEP5_PATCH.md`

## 做什么

把 `decision_log` 每条信号打上：

| 字段 | 含义 |
|---|---|
| `age_bucket` | `<1h` / `1-24h` / `1-7d` / `>7d` / `unknown` |
| `token_age_hours` | `(signal_ts_ms - pair_created_at_ms) / 3_600_000` |
| `peak_window` | 信号落在 `pair_created_at` 后 0–3 分钟内则为 1（GMGN peak P50≈1.0m ×3，**暂定可调**） |
| `survival_ref_1h` | 仅 `<1h` 桶：`self_db.survive['1h']`；其余 NULL |
| `survival_ref_4h` | 仅 `1-24h` 桶：`self_db.survive['4h']`；其余 NULL |
| `survival_ref_note` | `1-7d` / `>7d` / `unknown`：`crawler 窗口覆盖上限` |

## 旁路原则（默认不进决策）

- **主库只读**（`dexscreener.db` via `config.DB_PATH`，`mode=ro`）
- **只写** `data/prior/db/prior.db.decision_prior_tags`
- **不**改 INDICATORS_WEIGHT_*、trading、alerts、业务四库
- 决策引擎 / 执行路径 **默认不读**本表；周报 / 复盘可只读 `prior.db` 做观察

## schema 适配（相对草案）

| 草案 | 实际 `decision_log` | 落地 |
|---|---|---|
| `decision_id TEXT` | `id INTEGER PK` | `decision_id = str(id)`；JOIN 用 `CAST(d.id AS TEXT) = t.decision_id` |
| `signal_ts` | 列名 `ts` (ISO TEXT) | 旁路表仍叫 `signal_ts`，值= `ts` |
| pair 创建时间 | `pairs.pair_created_at` (ms) | 优先 `raw_json.pair_address`，回退 `token_address` |

### 单位换算（必读）

- `signal_ts` = ISO 文本；`pair_created_at` = epoch **ms**
- 年龄：`token_age_hours = (parse_epoch_ms(signal_ts) - pair_created_at_ms) / 3_600_000`
- **禁止**对 ISO 字符串与 ms 直接相减

### peak_window（可调）

- 默认 3 分钟 = GMGN peak P50(1.0m) × 3（经验窗口，**暂定，数据成熟后可调**）
- 环境变量 `PRIOR_PEAK_WINDOW_MIN` 可覆盖
- 含义：信号发出时 token 是否还在创建后见顶窗口内；**不是**「是否已见顶」

## survival_ref 按 age_bucket（对齐 step4 joint）

| age_bucket | survival_ref_1h | survival_ref_4h | survival_ref_note |
|---|---|---|---|
| `<1h` | self_db 1h（≈0.9271） | NULL | NULL |
| `1-24h` | NULL | self_db 4h（≈0.7529） | NULL |
| `1-7d` / `>7d` / `unknown` | NULL | NULL | `crawler 窗口覆盖上限` |

**禁止**把同一对 1h/4h 写到所有行。

## 用法

```bash
cd /workspace/dexscreener-crawler
python3 scripts/prior_tagger.py                 # 打标全部 decision_log
python3 scripts/prior_tagger.py --since 2026-10-09T00:00:00Z
```

幂等：`ON CONFLICT(decision_id) DO UPDATE`。验收前连跑两次，PK 行数不得翻倍。

## 周报 §8 消费端（tagger 只保证表；复盘接线）

### Helper

`review.data.get_prior_tag_buckets(prior_db, min_ts=None, *, main_db=None) -> list[dict]`

- 只读；按 `age_bucket` 聚合 `n / unique_token / in_peak / has_ref_1h / has_ref_4h`
- `decision_id` 为 `str(id)`：若 ATTACH 主库，JOIN 使用 `CAST(d.id AS TEXT) = t.decision_id`
- 无 `main_db` 时仅查 `prior.db`（`token_address` / `signal_ts` 已落在 tags 表）
- **复盘 owns** 周报 §8 接线；tagger 不改 `review/report.py`

### 现成 SQL（仅 prior.db，周报可直接跑）

```sql
SELECT t.age_bucket,
       COUNT(*) AS n,
       COUNT(DISTINCT t.token_address) AS unique_token,
       SUM(CASE WHEN t.peak_window = 1 THEN 1 ELSE 0 END) AS in_peak,
       SUM(CASE WHEN t.survival_ref_1h IS NOT NULL THEN 1 ELSE 0 END) AS has_ref_1h,
       SUM(CASE WHEN t.survival_ref_4h IS NOT NULL THEN 1 ELSE 0 END) AS has_ref_4h
FROM decision_prior_tags t
GROUP BY t.age_bucket
ORDER BY CASE t.age_bucket
  WHEN '<1h' THEN 1 WHEN '1-24h' THEN 2
  WHEN '1-7d' THEN 3 WHEN '>7d' THEN 4 ELSE 5 END;
```

### 可选：ATTACH 主库（unique_token 对账 decision_log）

```sql
ATTACH DATABASE 'file:/ABS/PATH/dexscreener.db?mode=ro' AS main_ro;
SELECT t.age_bucket,
       COUNT(*) AS n,
       COUNT(DISTINCT COALESCE(d.token_address, t.token_address)) AS unique_token,
       SUM(CASE WHEN t.peak_window = 1 THEN 1 ELSE 0 END) AS in_peak,
       SUM(CASE WHEN t.survival_ref_1h IS NOT NULL THEN 1 ELSE 0 END) AS has_ref_1h,
       SUM(CASE WHEN t.survival_ref_4h IS NOT NULL THEN 1 ELSE 0 END) AS has_ref_4h
FROM decision_prior_tags t
LEFT JOIN main_ro.decision_log d ON CAST(d.id AS TEXT) = t.decision_id
GROUP BY t.age_bucket
ORDER BY CASE t.age_bucket
  WHEN '<1h' THEN 1 WHEN '1-24h' THEN 2
  WHEN '1-7d' THEN 3 WHEN '>7d' THEN 4 ELSE 5 END;
```

### up_1h 说明

周报 `up_1h` **不是** prior.db 列；由复盘运行时计算：

1. `review.data.load_signal_chain` → `annotate_forward_returns`（`pair_history` 就近匹配 → `fwd_1h`）
2. `review.joint.joint_jev_age_prior` / `_bucket_compact`：`up_1h = rate(fwd_1h > 0)`
3. 分桶优先吃 `decision_prior_tags.age_bucket`（`load_decision_prior_tags`）

§8 建议追加「先验标签自动分桶」表（上列 SQL / helper），与手工联合表对账。

## 验收线

| 检查 | 期望 | 入口 |
|------|------|------|
| 表存在 | `decision_prior_tags` | `SELECT name FROM sqlite_master WHERE name='decision_prior_tags'` |
| 行数 | **= decision_log 行数** | `SELECT count(*) FROM decision_prior_tags` vs main |
| unknown 占比 | 目标 0%（pair_created_at 覆盖高时）；验收 ≤ 5% | `GROUP BY age_bucket` |
| 幂等 | 重跑不翻倍（PK count 不变） | 连跑两次对比 count |
| survival_ref 语义 | `<1h` 仅 1h 列非空；`1-24h` 仅 4h 列非空；长窗两列 NULL | 按桶抽样本 |
| 可选 | `1-24h` 桶非空（历史有该龄段时） | 分桶 count |
| 隔离 | 业务四库 mtime 未因 tagger 写入而变 | 只写 prior.db |
| 周报对账 | §8 自动分桶与手工联合表一致 | 复盘生成后比对 |

## cron / 复盘（可选，非常驻）

```bash
# 例：每周一复盘前
python3 scripts/prior_tagger.py
python3 scripts/prior_tagger.py   # 第二次：验幂等
```

## 不做

- 不碰业务四库 / INDICATORS_WEIGHT_* / trading / alerts
- 不自动改决策门槛或权重
