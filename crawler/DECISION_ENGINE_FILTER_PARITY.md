# Decision Engine — Backtest ↔ live filter parity

**用途：** 下次改回测过滤或 live `RiskManager` 时先对这张表。避免再出现「回测有这条、live 忘了」——`min_liq` 就是这一次，已由 PR #41 补上。

本表只记录**代码里已经存在的数**。不要在这里新造阈值，也不要改 `JEV_MIN_STRENGTH`（仍是 0.3）或因子权重。

## 入场过滤

回测钩子：`decision_engine/backtest/filters.py` 的 `FilterConfig`，CLI 为 `python -m decision_engine.backtest.run`（默认 `--filter` 开）。

Live 买入闸门：`decision_engine/core/risk_manager.py` 的 `RiskManager`，由 `DecisionEngine` 在 `SignalBus` 写入 `decision_log` 之前调用。卖出不进这条闸门。

周检：`scripts/jev_validity.py` 在**已经写入**的 `decision_log` buy 上再滤一层，不是 live 进程里的闸门。

| 过滤 | 回测 `FilterConfig`（默认） | 周检 `jev_validity` | Live `RiskManager` / engine | 状态 |
|------|-----------------------------|---------------------|-----------------------------|------|
| `min_liq` | **50000**（`--min-liq`）。缺失 → `no_liquidity`；低于下限 → `liq<50000` | **50000**（`--min-liq`）。缺失 → `no_liq`；低于下限 → `liq<50000`。流动性优先 `pair_history` 在 t0±tol，缺失才回退 `pairs.raw_json` 的**当前**快照 | **50000**（`DECISION_MIN_LIQ_USD`，非法值回退 50000）。缺失 / 非有限数 → 日志 `reason=no_liquidity`；低于下限 → `reason=liq_below_min`。来源是扫描行上的 `pairs.raw_json` **当前**快照，不是 `pair_history` | **done**（PR #41）。数值对齐。数据源仍不同：live 用当前快照，回测 / 周检优先历史 |
| `min_price` | **1e-6**（`--min-price`）。缺失 → `no_price`；低于下限 → `price<1e-06` | **1e-6**（`--min-price`）。缺失 → `no_price`；低于下限 → `price<1e-06` | 不检查价格 | **gap**。live 未执行。本表不新增价格阈值 |
| `min_coverage` | **10**（`--min-coverage`）。低于下限 → `coverage<10` | **10**（`--min-coverage`）。低于下限 → `coverage<10` | 不检查 coverage | **gap**。live 未执行。本表不新增覆盖率阈值 |
| coverage 缺失 | 默认 **fail-open**（`coverage_fail_open=True`）。只有 `--strict-coverage` 才记 `no_coverage` 并丢弃 | **fail-closed**（`no_coverage` 即失败） | 不检查 coverage | **gap**。回测默认与周检也不一致；正式对比命令在 `DECISION_ENGINE_STEP2.md` 里要求 `--strict-coverage`。live 没有这条 |

`min_jev_strength` 是策略阈值，不是上表的入场过滤。实时与回测 CLI 默认都是 `JEV_MIN_STRENGTH` / `DECISION_JEV_MIN_STRENGTH` = **0.3**。0.15 只在显式传入时出现。

## Live 有、回测 `FilterConfig` 没有

这些不是「回测有、live 忘了」，不要为了配平而加新阈值。

| 项 | Live | 回测 |
|----|------|------|
| 买入冷却 | `DECISION_COOLDOWN_SEC`，代码默认 **0**。临时 paper 观察设 **3600**，对齐 `max_hold_min=60` / `time_exit` 默认 `max_hold_seconds=3600`。见 `DECISION_ENGINE_DRYRUN_24H.md` | 不用时间冷却。同一标的用「已持仓」挡住，另有 `--max-open`（默认 5） |
| hot / tech 新鲜度 | 读数时丢弃过期行：`DECISION_HOT_SEEN_WINDOW_SEC` 默认 3600，`DECISION_TECH_FRESH_SEC` 默认约 2× 指标 hot 间隔 | 按 `--days` / bar 回放，不走这两条环境变量 |

## 改过滤之前

1. 先改这张表里对应的一行，写明回测、周检、live 三边是否一起动。
2. live 只通过现有 `DECISION_MIN_LIQ_USD`（以及已有的冷却环境变量）对齐，不要顺手改强度或权重默认。
3. 重启 paper 观察后，用 dry-run 日志确认 `min_liq_usd=50000.0`，以及 `reason=liq_below_min` / `reason=no_liquidity` 是否在丢弃，而不是写入 `decision_log`。
4. 执行单警示（半胜仍亏、薄池 `stop_loss` 单笔抹盈、残余风险打穿）只解释本表已有的 `min_liq` / fail-closed / 冷却，不在这里加第三条盘中过滤。见 `DECISION_ENGINE_WARNING_CASES.md`。
