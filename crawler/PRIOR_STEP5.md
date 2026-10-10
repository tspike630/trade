# PRIOR_STEP5 — decision_prior_tags 旁路打标

> 时间：2026-10-11（Asia/Hong_Kong）
> 定位：**指标加工层旁路**；自动化 step4 手工做过的 JEV×token_age×prior 打标
> 只碰：`models/prior_tables.sql`、`data/prior/db/prior.db`、`scripts/prior_tagger.py`、本文件

## 做什么

把 `decision_log` 每条信号打上：

| 字段 | 含义 |
|---|---|
| `age_bucket` | `<1h` / `1-24h` / `1-7d` / `>7d` / `unknown` |
| `token_age_hours` | `(signal_ts_ms - pair_created_at_ms) / 3600000` |
| `peak_window` | 信号落在 `pair_created_at` 后 0–3 分钟内则为 1（GMGN peak P50≈1.0m ×3） |
| `survival_ref_1h/4h` | `baseline_params.json` → `self_db.survive` 全网网络参考 |

## 旁路原则（默认不进决策）

- **主库只读**（`dexscreener.db` via `config.DB_PATH`，`mode=ro`）
- **只写** `data/prior/db/prior.db.decision_prior_tags`
- **不**改 INDICATORS_WEIGHT_*、trading、alerts、业务四库
- 决策引擎 / 执行路径 **默认不读**本表；周报 / 复盘可只读 `prior.db` 做观察

## schema 适配（相对草案）

| 草案 | 实际 `decision_log` | 落地 |
|---|---|---|
| `decision_id TEXT` | `id INTEGER PK` | `decision_id = str(id)` |
| `signal_ts` | 列名 `ts` (ISO TEXT) | 旁路表仍叫 `signal_ts`，值= `ts` |
| pair 创建时间 | `pairs.pair_created_at` (ms) | 优先 `raw_json.pair_address`，回退 `token_address` |

## 用法

```bash
cd /workspace/dexscreener-crawler
python3 scripts/prior_tagger.py                 # 打标全部 recent decision_log
python3 scripts/prior_tagger.py --since 2026-10-09T00:00:00Z
```

幂等：`ON CONFLICT(decision_id) DO UPDATE`。

## cron / 复盘（可选，非常驻）

无需 always-on。周报复盘前可一枪：

```bash
# 例：每周一复盘前
python3 scripts/prior_tagger.py
```

## survival_ref 说明

- 当前拷贝 `self_db.survive['1h']` / `['4h']` **同一对值写到所有行**（全网网络参考）
- age-specific 存活率暂不可用（RED-PUMP 无细粒度曲线；GMGN early-window 覆盖限制）
- JSON 缺失时用文档默认：`0.9271` / `0.7529`（与 2026-10-10 self_survival 实测一致）

## 不做

- 不碰业务四库 / INDICATORS_WEIGHT_* / trading / alerts
- 不自动改决策门槛或权重
