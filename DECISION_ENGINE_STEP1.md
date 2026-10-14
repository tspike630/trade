# Decision Engine Step 1 — 骨架 + JEV 强度策略 + dry-run

## 原则

- **完全独立进程**：`python -m decision_engine.run`，与 `main.py` 爬虫、`indicators.run` 解耦；崩溃不影响采集 / 指标 / 现有持仓。
- **只决策、不执行**：只生成标准化信号并写入 `decision_log`；**不调用** `paper_trade` / `live_trade`；默认 `--dry-run`。
- **只读下层数据**：指标用 SQLite `mode=ro` 读 `token_indicators`；可选读 `pairs.raw_json` 快照。**不写** tokens / pairs / pair_history / token_indicators。
- **策略插件化**：每个策略一个文件，注册进 `STRATEGY_REGISTRY`；加策略不改引擎核心。
- **风控占位**：`RiskManager` Step 1 透传（`risk_passed=True`），接口预留，不做实质校验。
- **零侵入**：不改 indicators / 爬虫主循环 / 权重 / hooks；不订 `INDICATORS_ON_UPDATE_HOOK`；不打印密钥（含 `API_SECRET` / `JEV_API_Key` 等）。

## 新增文件

```
decision_engine/
  __init__.py              # 版本 0.1.0-step1
  config.py                # DECISION_* 环境变量 + 项目 DB_PATH
  run.py                   # CLI：--strategy / --all / --dry-run / --once / --ticks
  core/
    engine.py              # 主循环：读数 → 策略 → 风控 → 信号总线
    data_adapter.py        # 只读 token_indicators；可选 get_pair_snapshot
    risk_manager.py        # passthrough 占位
    signal_bus.py          # 标准化信号 + decision_log 写入
  strategies/
    __init__.py            # STRATEGY_REGISTRY
    base_strategy.py       # on_tick / buy / sell
    jev_strategy.py        # jev_strength_v1
  adapters/
    dry_adapter.py         # 空跑适配器（不接执行）
DECISION_ENGINE_STEP1.md
DECISION_ENGINE_STEP1_RESULT.md
```

**对主链路的改动**：**无**。未改 `config.py` 默认开关、`store.py`、`main.py`、`indicators/`、`auto_hooks.py`、`paper_trade.py`、`live_trade.py`。

## 与上下游对接

### 输入（只读）

| 来源 | 用法 |
|------|------|
| `token_indicators` | 主输入；PK `(chain_id, token_address)`；`list_hot_tokens` 优先 `tier='hot'`，按 `jev_strength` 降序 |
| `pairs` | 可选 `get_pair_snapshot`（`priceUsd` / `liquidity.usd`）；Step 1 策略未调用 |
| API `127.0.0.1:8080` | 可选后备（`DECISION_USE_API_FALLBACK`，默认关）；Step 1 优先 SQLite |

关键字段：`change_5m/15m/1h/4h/24h`、`volatility_24h`、`volume_strength`、`jev_*`、`tier`、`primary_pair_address`、`updated_at`。

### 输出（仅日志 / 表）

| 输出 | 说明 |
|------|------|
| 结构化日志 | `SIGNAL strategy=… action=…` |
| `decision_log` | 新建表；独立 RW 连接写入（与指标 RO 连接分离） |

**不接**：`paper_trade.py`、`live_trade.py`、`auto_hooks`。

## `decision_log` 表

首次 emit / 启动 SignalBus 时 `CREATE IF NOT EXISTS`：

| 列 | 类型 | 说明 |
|----|------|------|
| `id` | INTEGER PK | 自增 |
| `ts` | TEXT | UTC ISO |
| `strategy_id` | TEXT | 如 `jev_strength_v1` |
| `action` | TEXT | `buy` / `sell` |
| `chain_id` / `token_address` | TEXT | 标的 |
| `size_pct` | REAL | 仓位占比 |
| `reason` | TEXT | 触发原因 |
| `risk_passed` | INTEGER | Step 1 恒为 1 |
| `mode` | TEXT | `dry_run` / `live_stub` |
| `raw_json` | TEXT | 完整信号 JSON |

## 策略 `jev_strength_v1`

| 参数 | 默认 | CLI / env |
|------|------|-----------|
| `min_jev_strength` | `0.3` | `--min-jev-strength` / `DECISION_JEV_MIN_STRENGTH` |
| `position_pct` | `0.05` | `--position-pct` / `DECISION_JEV_POSITION_PCT` |

逻辑：扫描 `list_hot_tokens(limit)`；若 `jev_strength >= min` 则 `buy`（附 `primary_pair_address`、`jev_signal`、`jev_score`）。

## 配置（`decision_engine/config.py` / 环境变量）

| 变量 | 默认 | 含义 |
|------|------|------|
| `DECISION_DB_PATH` / `DB_PATH` | 项目 `DB_PATH` / `dexscreener.db` | SQLite 路径（两者都认；启动日志打绝对路径） |
| `DECISION_TICK_INTERVAL_SEC` | `60` | 循环间隔 |
| `DECISION_HOT_TOKEN_LIMIT` | `50` | hot 扫描上限 |
| `DECISION_DEFAULT_STRATEGY` | `jev_strength_v1` | 默认策略 |
| `DECISION_JEV_MIN_STRENGTH` | `0.3` | JEV 阈值 |
| `DECISION_JEV_POSITION_PCT` | `0.05` | 信号仓位 |
| `DECISION_COOLDOWN_SEC` | `0` | 同一 `(strategy, chain, token)` 买入冷却秒数。0 = 关闭，dry-run 观察行为不变。Step B（paper）应设为约等于 tick 间隔，避免每个 tick 对同一 token 重复开仓 |
| `DECISION_MIN_LIQ_USD` | `50000` | 实盘/dry-run 买入流动性下限（USD），与回测 `FilterConfig.min_liq=50000` 对齐。`liquidity_usd` 缺失（`no_liquidity`）或低于下限（`liq_below_min`）的 buy 在写入 `decision_log` 前丢弃。卖出不过滤。只做风险入场过滤，不改变 JEV 强度阈值、权重或 purpose |
| `DECISION_USE_API_FALLBACK` | `0` | 是否走 API（Step 1 默认关） |

## 如何运行

仓库根 `/workspace/dexscreener-crawler`：

```bash
# 单次验证（推荐）
.venv/bin/python -m decision_engine.run --strategy jev_strength_v1 --dry-run --once

# 跑 N tick
.venv/bin/python -m decision_engine.run --strategy jev_strength_v1 --dry-run --ticks 3 --interval 2

# 调低阈值以便看到信号（库内 max jev_strength 可能 < 0.3）
.venv/bin/python -m decision_engine.run --strategy jev_strength_v1 --dry-run --once --min-jev-strength 0.15

# 持续循环（Ctrl+C 退出）
.venv/bin/python -m decision_engine.run --strategy jev_strength_v1 --dry-run --interval 60
```

与主爬虫 / 指标进程并行即可；**不要**杀 stabilize 的 `main.py --loop`，**不要**删 `LIVE_TRADING_DISABLED`。

## 验证标准（Step 1 通过条件）

1. `--once` → **退出码 0**
2. 日志含：读库 / tick / 信号或「无信号」
3. `decision_log` 表存在；有信号则有行
4. 指标层与采集层代码 / 默认开关未改动

## 验证结果（摘要）

详见 `DECISION_ENGINE_STEP1_RESULT.md`。

| 时间 | 命令 | 结果 |
|------|------|------|
| 2026-10-10 ~04:27 HKT | `--once` 默认阈值 0.3 | exit 0；indicators≈1507；hot=50；信号 0；max jev≈0.205 |
| 2026-10-10 ~04:27 HKT | `--once --min-jev-strength 0.15` | 14 条 buy 写入 `decision_log` |
| 2026-10-10 13:06 HKT | `--once` 默认阈值 0.3 | exit 0；indicators=**5782**；hot=50；信号 0；max jev=0.205 |

默认阈值下「无信号」属预期（强度未达 0.3），不是故障。

## 后续（非本 Step）

- Step 2：历史回测框架 + 对接 `pair_history` / `paper_trade` 账本
- Step 3：多策略并行 + 实时模拟盘
- Step 4：小额实盘灰度（须过五层闸门）
- 实质风控、事件 hook 订阅、API 写路径 — 均非 Step 1
