# PRIOR_STEP6 — 指标加工层：baseline 滚动历史

> 时间：2026-10-11（Asia/Hong_Kong）
> 定位：**指标加工层旁路**；积累基线快照证据，支持日后「稳定两周才进风控」
> 只碰：`models/prior_tables.sql`、`data/prior/db/prior.db`、`scripts/prior_baseline_trend.py`、`scripts/self_survival.py`（成功后 hook）、本文件
> **零策略/权重影响**：不改 INDICATORS_WEIGHT_*、trading、alerts、业务四库；`RISK_USE_PRIOR` 仍默认关

## 三项分工

| # | 项 | 状态 | 谁 owns |
|---|---|---|---|
| ① | `baseline_history` 滚动表 + trend 脚本 | **done** | 本 step（指标加工层） |
| ② | dashboard 展示趋势 | **skip** | dashboard owns（本 step 不接 UI） |
| ③ | 满窗重跑后追加快照 | **文档约定** | 已有 routine「自库存活·满窗重跑」；勿在代码里再建冲突 cron |

## ① baseline rolling history（已完成）

### Schema

`models/prior_tables.sql` → `prior.db.baseline_history`（只追加）：

- 三元组：`graduation_rate` / `same_day_death_rate` / `solrpds_remove_gt_add`
- 自库存活：`survive_1h/4h/24h/7d`（7d ← `self_db.survive['168h']`）
- 辅助：`peak_p50_min`（GMGN）、`crawler_window_hours`、`n_tokens`、`params_json` 全量 blob

### 用法

```bash
cd /workspace/dexscreener-crawler
# 默认：读 data/prior/exports/baseline_params.json，追加一行（source=manual）
python3 scripts/prior_baseline_trend.py

# self_survival 成功后的约定 source
python3 scripts/prior_baseline_trend.py --from-self-survival

# 最近 N 行 / 近 DAYS 天变化 + 稳定检查
python3 scripts/prior_baseline_trend.py --list 20
python3 scripts/prior_baseline_trend.py --since 14
```

稳定检查（observational）：近 14d 内 `survive_4h` 的 |Δ| 与 min–max span 均 ≤ 0.05，且日历跨度够约两周 → 打印 `STABLE`；否则 `UNSTABLE_OR_SHORT` / `INSUFFICIENT_DATA`。**仅打印，不进风控。**

### self_survival hook

`scripts/self_survival.py` 在成功写出 `baseline_params.json` 后自动调用：

```bash
python3 scripts/prior_baseline_trend.py --from-self-survival
```

失败只 WARN，不拖垮主报告。

## ② dashboard（skip）

趋势图 / 列表由 dashboard 读 `prior.db.baseline_history`（只读）自行接线；本 step 不改 `dashboard/`。

## ③ Monday routine「自库存活·满窗重跑」如何追加

已有 Grok Bot routine **「自库存活·满窗重跑」** 负责周一（或满窗后）重跑 `self_survival.py`。

**正确接法（二选一，勿重复建 cron）：**

1. **推荐**：routine 只跑 `python3 scripts/self_survival.py` —— 脚本末尾已 hook append，满窗重跑后自动多一行 `source=self_survival`。
2. **显式**：若 routine 只更新 JSON、不经 hook，在重跑成功后追加一步：
   ```bash
   python3 scripts/prior_baseline_trend.py --from-self-survival
   ```

**不要**在仓库里再写一份 crontab / schedule 与该 routine 抢同一触发；本 step 只提供 append 能力与文档约定。

## 周报挂载（复盘 §10）

`review/evolution.py` → `load_baseline_history_report` / `format_baseline_history_section`：
周报 §10 建议表后展示最近 N 条快照（survive_1h/4h、窗长、稳定判定）。缺表跳过并注明。**只观察，不调权。**

## 旁路原则

- 只写 `data/prior/db/prior.db`
- 不覆盖历史行（无 UPDATE/DELETE 路径）
- 不改权重、不交易、默认不进决策

## Smoke（2026-10-11）

```bash
python3 scripts/prior_baseline_trend.py   # append once from current baseline_params.json
python3 scripts/prior_baseline_trend.py --list 1
```
