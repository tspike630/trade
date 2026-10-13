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

这是采样，不是常驻：`mode` 仍是 `dry_run`，`purpose=sample`（低于默认阈值时即使漏传 `--purpose` 也会写成 `sample`）。不要当 paper 候选。

或临时 env：`DECISION_JEV_MIN_STRENGTH=0.15`（仅当你明确要第二套常驻时；本 24h 任务未启用）。改默认阈值且不传更低的 `--min-jev-strength` 时，purpose 仍是 `live_candidate`。

## Caveats

- 默认 0.3 高于当前 `jev_strength` 分布 → 常驻日志多为「无信号」，仍可观察进程稳定性与 tick 节奏。
- `decision_log` 与主库同文件；引擎对指标侧为 RO 连接。
- Step 1 禁止实盘；保留 `LIVE_TRADING_DISABLED`。
