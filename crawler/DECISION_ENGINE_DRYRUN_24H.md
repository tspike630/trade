# Decision Engine — 24h 常驻 dry-run

**Time (HK / UTC+8):** 启动 2026-10-10 13:09:57 HKT  
**Repo:** `/workspace/dexscreener-crawler`  
**Mode:** dry-run only（不接 paper / live）  
**Strategy:** `jev_strength_v1`（默认阈值 `min_jev_strength=0.3`）

## 原则

- 只写 `decision_log` + 本机 `logs/`；主业务表只读。
- **不**杀 `main.py` / `indicators`；**不**删 `LIVE_TRADING_DISABLED`；**不**接 paper/live。
- 优先单常驻；默认 0.3 可能长期「无信号」（当前库 max≈0.205），属预期，不另起第二套常驻。

## 启动信息

| 项 | 值 |
|---|---|
| 启动时间 (HKT) | **2026-10-10 13:09:57 HKT** |
| PID | **1887062**（写入 `logs/decision_engine_dryrun.pid`） |
| 命令 | 见下 |
| 日志 | `logs/decision_engine_dryrun.log` |
| PID 文件 | `logs/decision_engine_dryrun.pid` |
| interval | 60s |
| dry-run | True |

### 启动命令

```bash
cd /workspace/dexscreener-crawler
mkdir -p logs
nohup .venv/bin/python -m decision_engine.run --strategy jev_strength_v1 --dry-run --interval 60 \
  >> logs/decision_engine_dryrun.log 2>&1 &
echo $! > logs/decision_engine_dryrun.pid
```

> 注：若 shell 包装导致 `$!` 不是 python PID，以 `pgrep -af 'python -m decision_engine.run'` 为准，并回写 pid 文件。本机已校正为 **1887062**。

## 存活确认（启动后）

```
tick#1 @ 13:09:57 HKT  indicators_total=5840 hot_loaded=50 signals=0 min=0.300
tick#2 @ 13:10:57 HKT  indicators_total=5849 hot_loaded=50 signals=0 min=0.300
```

默认阈值下 `above_threshold=0` → 日志「无信号」，符合 `DECISION_ENGINE_STEP1_RESULT` 复验结论。

隔离：`LIVE_TRADING_DISABLED` 仍在；`main.py` / `indicators.run` 未动。

## 如何看状态

```bash
# 进程
pgrep -af 'python -m decision_engine.run'
cat logs/decision_engine_dryrun.pid
ps -p "$(cat logs/decision_engine_dryrun.pid)" -o pid,etime,cmd

# 日志 / tick
tail -f logs/decision_engine_dryrun.log
grep 'tick#' logs/decision_engine_dryrun.log | tail -20

# 新写入的 dry-run 信号（若有）
sqlite3 dexscreener.db "SELECT id,ts,action,chain_id,substr(token_address,1,12),size_pct,reason FROM decision_log WHERE mode='dry_run' ORDER BY id DESC LIMIT 20;"
```

## 如何停

```bash
kill "$(cat logs/decision_engine_dryrun.pid)"
# 或
pkill -f 'python -m decision_engine.run --strategy jev_strength_v1 --dry-run'
```

确认：`pgrep -af decision_engine` 应无输出。不要误杀 `main.py` / `indicators`。

## 可选：降低阈值补信号（非本常驻）

主常驻保持默认 **0.3**。若需快验数据，用一次性：

```bash
.venv/bin/python -m decision_engine.run --strategy jev_strength_v1 --dry-run --once \
  --min-jev-strength 0.15 --purpose sample
```

这是采样，不是常驻：`mode` 仍是 `dry_run`，`purpose=sample`（低于默认阈值时即使漏传 `--purpose` 也会写成 `sample`）。不要当 paper 候选。不要用这条命令重启下面的 paper 观察。

或临时 env：`DECISION_JEV_MIN_STRENGTH=0.15`（仅当你明确要第二套常驻时；本 24h 任务未启用）。改默认阈值且不传更低的 `--min-jev-strength` 时，purpose 仍是 `live_candidate`。

## Paper observation restart recipe

临时 paper 观察。和上面 2026-10-10 的默认 0.3 常驻、以及周一三五 `--purpose sample` 采样，是三条不同的命令。重启 paper 观察时用本节，不要回抄「启动命令」那一节（那一节没有冷却，也没有 0.15）。

**不改代码默认。** `JEV_MIN_STRENGTH` / `DECISION_JEV_MIN_STRENGTH` 仍是 **0.3**。`DECISION_COOLDOWN_SEC` 的代码默认仍是 **0**。0.15 只出现在这条临时命令的 `--min-jev-strength` 上。这不是有效性实验：这些行保持未标记，不要标成 accepted。见 `DECISION_ENGINE_WEEKLY_CHECK.md` 的 2026-10-19 登记表。

| 必须带上 | 值 | 漏掉时实际发生的事 |
|----------|----|--------------------|
| 环境变量 `DECISION_COOLDOWN_SEC` | **3600** | 代码默认 `0`，冷却关闭。3600 秒对齐持仓上限：回测与 paper 的 `max_hold_min` 默认 **60**，`time_exit` 的 `max_hold_seconds` 默认 **3600** |
| 流动性下限 | **不覆盖**。用 `decision_engine/config.py` 的 `DECISION_MIN_LIQ_USD` 默认 **50000** | 另设该环境变量会让 live 地板偏离回测 `FilterConfig.min_liq`（PR #41 已对齐） |
| `--purpose live_candidate` | 与 `--min-jev-strength 0.15` **一起写** | 阈值低于实时默认 0.3 且漏传 `--purpose` 时，`resolve_run_purpose` 把 purpose 写成 **`sample`**。`sample` 不是 paper 候选 |

```bash
cd /workspace/dexscreener-crawler
mkdir -p logs
# 只停这条 dry-run。不要杀 main.py / indicators。
if [ -f logs/decision_engine_dryrun.pid ]; then
  kill "$(cat logs/decision_engine_dryrun.pid)" 2>/dev/null || true
fi
# 冷却必须写在这条进程上。不要另 export DECISION_MIN_LIQ_USD 或 DECISION_JEV_MIN_STRENGTH。
nohup env DECISION_COOLDOWN_SEC=3600 \
  .venv/bin/python -m decision_engine.run \
  --strategy jev_strength_v1 --dry-run --interval 60 \
  --min-jev-strength 0.15 --purpose live_candidate \
  >> logs/decision_engine_dryrun.log 2>&1 &
echo $! > logs/decision_engine_dryrun.pid
pgrep -af 'python -m decision_engine.run'
```

`env DECISION_COOLDOWN_SEC=3600` 只作用于这一个进程。把 python 那一行单独抄走、不带这个 `env`，冷却就是代码默认 0。

**漏写 `--purpose` 的后果：** `--min-jev-strength 0.15` 单独出现时，启动日志是 `purpose=sample`。那是周检采样，不是这条 paper 观察。

### 启动后在日志里确认

```bash
grep -E 'purpose=|cooldown_sec=|min_liq_usd=' logs/decision_engine_dryrun.log | tail -30
grep -E 'reason=liq_below_min|reason=no_liquidity|cooldown skip' logs/decision_engine_dryrun.log | tail -30
```

通过标准（浮点按代码打印，带 `.0`）：

- `decision_engine start` 一行里同时有 `purpose=live_candidate`、`min_jev_strength=0.15`、`min_liq_usd=50000.0`
- `risk cooldown_sec=3600.0`（`cooldown_sec=0` 或 `0.0` 表示冷却没带上）
- `risk min_liq_usd=50000.0`（不是这个数就说明 `DECISION_MIN_LIQ_USD` 被覆盖了）
- `reason=liq_below_min`：`liquidity_usd` 低于 50000，buy 在写入 `decision_log` 前丢弃
- `reason=no_liquidity`：流动性缺失或不是有限数，同样 fail-closed 丢弃
- 卖出不走这两条。冷却命中另打 `cooldown skip`（`age=` / `need=`）
- 冷却状态在进程内存里，重启清零。同一次进程内，同一 `(strategy, chain, token)` 的 buy 在 3600 秒内不会再次发出

流动性地板与回测其余过滤的对照见 `DECISION_ENGINE_FILTER_PARITY.md`。

## Caveats

- 默认 0.3 高于当前 `jev_strength` 分布 → 不带 `--min-jev-strength` 的常驻日志多为「无信号」，仍可观察进程稳定性与 tick 节奏。
- 临时 paper 观察是 0.15 且 `purpose=live_candidate`，日志里可以出现信号。那不表示默认阈值改成了 0.15，也不表示有效性已通过。
- `decision_log` 与主库同文件；引擎对指标侧为 RO 连接。
- Step 1 禁止实盘；保留 `LIVE_TRADING_DISABLED`。
