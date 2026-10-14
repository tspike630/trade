# Decision Engine Step 2 — 回测框架（通用）

**Time (HK / UTC+8):** 2026-10-10  
**原则：** 框架先行；**暂缓的是有效性结论，不是框架本身**。不依赖 JEV 周检已验证才动手。

## 原则

- **只读主业务表**：`indicator_history` / `pair_history` /（可选）`jev_signals` / `token_indicators`，SQLite `mode=ro`。
- **可写**：独立账本 `backtest_ledger.db`（表 `runs` / `backtest_log` / `positions` / `equity_curve` / `account`）。不写 tokens / pairs / pair_history / token_indicators / decision_log。
- **零侵入采集与指标层**：不改 `main.py`、`indicators/`、`auto_hooks`、24h dry-run、采样、周检脚本。
- **不接实盘**：不调用 `live_trade`；不改 `paper_trade` 默认库；账本独立（接口形态对齐 `PaperLedger`，便于日后对接）。
- **策略插件化**：复用 `BaseStrategy.on_tick`；A/B/C 注册进 `STRATEGY_REGISTRY`。
- **过滤钩子默认开**：与周检一致 — `liquidity≥50k`、`coverage≥10`、`min_price=1e-6`（coverage 缺失时默认 fail-open，可用 `--strict-coverage`）。live 侧目前只对齐了 `min_liq`，对照表见 `DECISION_ENGINE_FILTER_PARITY.md`。
- **不打印密钥**。

## 与 Step1 / 周检 / 根目录 backtest.py 的关系

| 组件 | 关系 |
|------|------|
| Step 1 `decision_engine.run` | 实时 dry-run 信号 → `decision_log`；本 Step **不替代、不杀停** |
| 24h dry-run / 采样 / 周检 | **不要动**；本框架独立进程 |
| `scripts/jev_validity.py` | 有效性复盘；过滤阈值对齐，代码不耦合 |
| 根目录 `backtest.py` | 旧 hold/tpsl/grid 回测，**保留不动**；本框架在 `decision_engine/backtest/` |
| `paper_trade.py` | 模拟盘进程；本 Step 用独立 ledger，接口可后续对接 |

**暂缓：** 把回测数字当成「JEV / 技术面已证明有效」——有效性仍看周检决策表。Step 2 交付的是**可跑的对比脚手架**。

## 目录

```
decision_engine/
  backtest/
    __init__.py          # 0.2.0-step2
    filters.py           # liq / coverage / min_price 钩子
    replay.py            # indicator_history + pair_history → bar 快照
    ledger.py            # 独立 sqlite 账本
    metrics.py           # 胜率 / 盈亏比 / MDD / 资金曲线摘要
    engine.py            # tick 回放 → on_tick → 过滤 → 开平仓
    run.py               # CLI
  strategies/
    jev_strategy.py      # A: jev_strength_v1
    tech_strategy.py     # B: tech_momentum_v1
    combined_strategy.py # C: combined_jev_tech_v1
DECISION_ENGINE_STEP2.md
```

## 三组策略（脚手架）

| 标签 | strategy_id | 逻辑（可调阈值） |
|------|-------------|------------------|
| A | `jev_strength_v1` | `jev_strength ≥ min`（默认与实时 `JEV_MIN_STRENGTH` / `DECISION_JEV_MIN_STRENGTH` 相同，当前 0.3；要 0.15 必须显式传 `--min-jev-strength 0.15`） |
| B | `tech_momentum_v1` | `change_5m ≥ min` 且 `volume_strength ≥ min` |
| C | `combined_jev_tech_v1` | JEV 过滤 + 技术面确认 |

退出：TP / SL / max_hold（默认 10% / 5% / 60min），回放结束未平仓按入场价 EOD flat。

## 如何跑

仓库根 `/workspace/dexscreener-crawler`：

```bash
# 烟雾：三组对比，默认过滤，7 天窗口（数据不足会优雅退出说明）
.venv/bin/python -m decision_engine.backtest.run --strategy all --days 7 --dry-run

# 单策略 / 别名
.venv/bin/python -m decision_engine.backtest.run --strategy a --days 1
.venv/bin/python -m decision_engine.backtest.run --strategy tech_momentum_v1 --no-filter

# 调阈值 + 写 JSON
.venv/bin/python -m decision_engine.backtest.run --strategy all --days 7 \
  --min-jev-strength 0.15 --min-change-5m 1.0 --min-volume-strength 0.3 \
  --json-out exports/reports/backtest_smoke.json
```

常用参数：`--bar-minutes`、`--min-liq`、`--min-coverage`、`--min-price`、`--strict-coverage`、`--tp` / `--sl` / `--max-hold-min`、`--ledger`、`--db`。

## 数据流

1. **replay**：按 `--bar-minutes` 对 `indicator_history` 分桶。同一 bar、同一 token 合并非空字段（后写的 funding/sentiment NULL 不再盖掉 RSI/MACD/BOLL）；按 hot/jev/volume 排序截断 `hot_limit`。入场价仍是 bar 起点 ±`price_tol_sec` 的最近一笔（已知简化，可能取到 bar 起点之后的价格）。
2. **engine**：每 bar 先管退出，再 `strategy.on_tick({hot_tokens})`，买单过过滤钩子后写入账本。
3. **metrics**：按 `strategy_id` 汇总胜率、profit factor、最大回撤、收益、资金曲线点数。

## 验证标准

1. CLI 退出码 0（含「数据不足优雅退出」）。
2. 有足够历史时：`backtest_ledger.db` 有 runs / backtest_log / equity_curve；打印分策略绩效表。
3. 未改 24h dry-run / 周检 / 采集默认开关。


## 周检达标后的正式对比命令模板

周检门槛（unique_token≥10 + raw n≥15 + 连续两周 1h≥55%）通过后跑正式对比时：

```bash
# 必须 --strict-coverage，使 A/C 与周检口径一致（coverage 缺失 fail-closed）
.venv/bin/python -m decision_engine.backtest.run --strategy all --days 7 --dry-run \
  --strict-coverage \
  --min-liq 50000 --min-coverage 10 --min-price 1e-6 \
  --json-out exports/reports/backtest_formal_$(date +%Y%m%d).json
```

正式前待补：手续费 / 滑点参数；用周检过滤口径对拍 A/C。烟雾数字（短历史）不作有效性结论。

默认 `--min-jev-strength` 与实时 `JEV_MIN_STRENGTH`（`DECISION_JEV_MIN_STRENGTH`，当前 0.3）同源，不再单独用 0.15。周检采样若要沿用 0.15，必须显式传入（`DECISION_ENGINE_WEEKLY_CHECK.md` 的采样命令已经显式传 `--min-jev-strength 0.15`）。上面的正式对比命令不传该参数，因此按实时默认 0.3 跑。

## 后续（非本 Step）

- 对接 `paper_trade` 账本 API（若需统一模拟盘）
- 更细的 bar 对齐、手续费/滑点、多空
- 有效性结论仍归周检；达标后再谈 Step 3 实时模拟盘
