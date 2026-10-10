# Indicators Step 2 — JEV 数据源 + 资金面（funding）指标

## 原则

- **非侵入旁路**：JEV 只写入独立表 `jev_signals`；funding 只读 `jev_signals`，写入 `token_indicators` /（可选）`indicator_history`。
- **不发明代币**：JEV **不是** `TokenSource`，只 enrich 已有 token（热档）。
- **异常只记日志**：单 token / 单 pass 失败不影响 `main.py` 与 `indicators.run` 主循环。
- **API Key 可选**：未设置 `JEV_API_KEY` 时公开扫描；设置后按 `JEV_AUTH_MODE`（默认 `header`/`X-API-Key`）鉴权。 密钥永不入日志；`query` 模式经 `redact_url` 脱敏。详见 `AUTH_JEV.md`。

## 新增 / 修改文件

```
sources/jev.py              # JEV 拉取、限速、jev_signals、CLI
scripts/run_jev.py          # 薄封装
indicators/funding.py       # 填充 stub：映射 jev_* 字段
indicators/base.py          # 部分 upsert + 列迁移；读 jev_signals
indicators/run.py           # --full/--loop 调用 funding
models/indicator_tables.sql # jev_raw_score/signal/confidence/strength
config.py                   # JEV_* / 沿用 INDICATORS_*
auto_hooks.py               # note_new_listing_for_jev（默认关）
pipeline.py                 # 调用上述 hook（无开关时 no-op）
INDICATORS_STEP2.md         # 本文
```

未改：`store.py` 主 schema、爬虫核心节奏（仅可选 hook）。

## 表结构

### `jev_signals`（由 `sources.jev` 建表）

| 列 | 说明 |
|----|------|
| `chain_id`, `token_address` | PK（小写，与爬虫一致） |
| `jev_score`, `observed_score`, `coverage_pct`, `score_kind` | API 字段 |
| `stance` | `investment_verdict.stance` |
| `flags_json`, `metrics_json` | JSON 文本 |
| `analyzed_at`, `fetched_at` | API / 本地时间 |
| `raw` | 截断后的原始 JSON |
| `latency_ms`, `cached`, `error` | 元数据 |

### `token_indicators`（Step 2 扩展）

| 列 | 含义 |
|----|------|
| `jev_raw_score` | = `jev_signals.jev_score`（0–100） |
| `jev_score` | 同上（兼容 Step 1 列名） |
| `jev_signal` | `bullish` / `bearish` / `neutral` |
| `jev_confidence` | `coverage_pct / 100`，裁剪到 \[0,1\] |
| `jev_strength` | 见下方公式，约 \[-1, 1\] |
| `jev_funding_bias` | 方向系数：+1 / 0 / −1 |
| `jev_updated_at` | 来自 `fetched_at` 或 `analyzed_at` |

`IndicatorStore.upsert_token_indicators` 为 **部分更新**：只写 `row` 里出现的列，technical 与 funding 互不覆盖。

## JEV API

- Base：`https://jevs.fun`（`JEV_BASE_URL`）
- `GET /v1/score/{contract}?chain=solana|ethereum|base|bsc|…`
- 限速：IP **8/min**，全局 60/min；本客户端默认 **`JEV_MAX_PER_MIN=7`**
- `429` / `5xx`：指数退避；分钟配额用尽则等待窗口滑动
- `503`：按不完整/重试处理；`<90s` 缓存可能 `cached=true`
- **Solana 地址大小写敏感**：库内 PK 为小写，请求前从 `pairs.raw_json`（`baseToken.address`）恢复大小写；Wrapped SOL 有内置 well-known 映射

## 触发方式

| 方式 | 命令 / 条件 |
|------|-------------|
| 一次性（due 热档） | `python -m sources.jev --once` |
| 强制热档 | `python -m sources.jev --once --force --limit 20` |
| 指定代币 | `python -m sources.jev --token solana:So1111…` |
| 常驻 | `python -m sources.jev --loop --interval 900`（默认 15 min） |
| 脚本 | `python scripts/run_jev.py …` |
| 新上市（默认关） | `JEV_ON_NEW_LISTING=1` → `exports/jev_pending.jsonl`，由 loop 消费 |

热档：`has_pairs=1` 且 `last_seen_at` 在 `HOT_SEEN_WINDOW_SEC`（1h）内。  
暖/冷档：**不主动拉取**。每轮最多 `JEV_HOT_LIMIT`（默认 20）个，优先从未拉过 / `fetched_at` 最旧。

## 配置（`config.py` / 环境变量）

| 变量 | 默认 | 含义 |
|------|------|------|
| `JEV_ENABLED` | `1` | 总开关（可 `--force-enabled`） |
| `JEV_BASE_URL` | `https://jevs.fun` | API 根 |
| `JEV_API_KEY` | `""` | 可选密钥；空 = 公开模式（永不 log） |
| `JEV_AUTH_MODE` | `header` | `header` / `bearer` / `query` |
| `JEV_HOT_INTERVAL_SEC` | `900` | 热档刷新间隔 |
| `JEV_MAX_PER_MIN` | `7` | 客户端每分钟上限（公开 <8；鉴权可调高） |
| `JEV_HOT_LIMIT` | `20` | 每轮最多拉取数 |
| `JEV_ON_NEW_LISTING` | `0` | 新上市入队（默认关） |
| `JEV_REQUEST_TIMEOUT_SEC` | `30` | HTTP 超时 |
| `JEV_RAW_MAX_CHARS` | `8000` | raw/metrics 截断 |
| `JEV_PENDING_PATH` | `exports/jev_pending.jsonl` | 新上市队列 |

## funding 映射规则

### `jev_signal`

1. stance 匹配 `SELL` / `BEARISH` / `SHORT`（且非 `NOT BEARISH`）→ **bearish**
2. stance 匹配 `BUY` / `BULLISH` / `LONG`（且非 `NOT BULLISH`）→ **bullish**
3. 否则若 `jev_raw_score ≥ 65` → **bullish**
4. 否则若 `jev_raw_score ≤ 35` → **bearish**
5. 否则 → **neutral**（含 `NEUTRAL`、`INSUFFICIENT EVIDENCE`、`NOT BULLISH` 且分数在中间带等）

说明：`NOT BULLISH` 本身不记作 bullish；若分数 ≥65，会因分数带落到 bullish。

### `jev_strength`

```
direction_coef = +1 (bullish) | −1 (bearish) | 0 (neutral)
jev_confidence = clip(coverage_pct / 100, 0, 1)
jev_strength   = round( (jev_raw_score / 100) * jev_confidence * direction_coef , 6 )
```

缺 `jev_raw_score` 时 `jev_strength = 0`；缺 coverage 时 confidence 按 0 参与 strength。

funding **从不发 HTTP**，只读 `jev_signals`。`indicators.run --full` / `--loop` 都会在 technical 之后跑 funding。

## 如何运行

仓库根目录 `/workspace/dexscreener-crawler`：

```bash
# JEV 拉热档（注意限速）
python -m sources.jev --once --force --limit 5
# 或常驻（日志 logs/jev.log）
nohup python -m sources.jev --loop --interval 900 >> logs/jev.log 2>&1 &

# 将 jev_signals 映射进 token_indicators
python -m indicators.run --full
# indicators 常驻（勿杀已有 loop）
# python -m indicators.run --loop --interval 300
```

**不要**停止已有 `main.py` stabilize / `indicators.run --loop` 进程。

配置 `JEV_API_KEY` 后**只重启** `sources.jev`（见 `AUTH_JEV.md`）。

启用新上市 JEV 队列：

```bash
export JEV_ON_NEW_LISTING=1
# 需重启会加载 env 的爬虫进程后才生效；或仅依赖 JEV loop 热档轮询
```

## 验证结果（2026-10-10 约 03:22 HKT / 数据 UTC 约 19:22Z）

### 进程

| 进程 | PID | 状态 |
|------|-----|------|
| `main.py --helius-listen --sources x --loop` | 1559283 | 仍在运行 |
| `indicators.run --loop --interval 300` | 1577906 | 仍在运行 |
| `sources.jev --loop --interval 900` | 1585766 | 已后台启动 → `logs/jev.log` |

### `jev_signals` 样本（5）— API 与 DB 一致（\|Δ\|≤1）

| token（前缀） | DB `jev_score` | API `jev_score` | coverage | stance |
|---------------|----------------|-----------------|----------|--------|
| `2qr34h7jw6bqz4x1…pump` | 100 | 100 | 7 | NEUTRAL |
| `cbjqn8q3haxra9ov…` | 97 | 97 | 9 | NEUTRAL |
| `diqf6to3tbzzetu4…pump` | 94 | 94 | 9 | NEUTRAL |
| `so11111111111111…` (Wrapped SOL) | 92 | 92 | 7 | NEUTRAL |
| `hhq5nmrube7u1eqb…` | 48 | 48 | 11 | NOT BULLISH |

### `token_indicators` funding 字段（同批）

| token 前缀 | raw | signal | confidence | strength | bias |
|------------|-----|--------|------------|----------|------|
| 2qr34h7j… | 100 | bullish | 0.07 | 0.07 | +1 |
| cbjqn8q3… | 97 | bullish | 0.09 | 0.0873 | +1 |
| diqf6to3… | 94 | bullish | 0.09 | 0.0846 | +1 |
| so111111… | 92 | bullish | 0.07 | 0.0644 | +1 |
| hhq5nmru… | 48 | neutral | 0.11 | 0.0 | 0 |

`indicators.run --full`：technical `ok=805`；funding `seen=5 ok=5`。

## 限速与运营注意

- 热档可能远超 20 个：靠 `JEV_HOT_LIMIT` + 15 min 轮转覆盖；勿把 `JEV_MAX_PER_MIN` 调到 ≥8。
- JEV 对部分新 meme 可能返回 `INSUFFICIENT EVIDENCE` / `jev_score=null`，属正常，仍会落库。
- Solana 务必保留/恢复正确大小写，否则分数易变空。
