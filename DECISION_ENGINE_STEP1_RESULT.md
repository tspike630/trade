# Decision Engine Step 1 Result — 骨架 + dry-run 空跑

**Time (HK / UTC+8):** 2026-10-10 ~04:27 HKT（首验）/ 13:06 HKT（复验）  
**Repo:** `/workspace/dexscreener-crawler`  
**DB:** `dexscreener.db`  
**Command:** `.venv/bin/python -m decision_engine.run --strategy jev_strength_v1 --dry-run --once`

## Added

| # | Item | Files |
|---|---|---|
| 1 | 引擎主循环 + CLI | `decision_engine/core/engine.py`, `run.py` |
| 2 | 只读数据适配器 | `decision_engine/core/data_adapter.py` |
| 3 | 风控占位（透传） | `decision_engine/core/risk_manager.py` |
| 4 | 信号总线 + `decision_log` | `decision_engine/core/signal_bus.py` |
| 5 | 策略基类 + JEV 策略 | `strategies/base_strategy.py`, `jev_strategy.py` |
| 6 | dry 适配器 | `adapters/dry_adapter.py` |
| 7 | 文档 | `DECISION_ENGINE_STEP1.md`, `DECISION_ENGINE_STEP1_RESULT.md` |

**未改：** `indicators/`、`main.py`、`store.py`、`auto_hooks.py`、`paper_trade.py`、`live_trade.py`、`LIVE_TRADING_DISABLED`、指标权重 / hook 默认开关。

## Verification (this machine)

### Default threshold (`min_jev_strength=0.3`)

```
exit=0
indicators_total=5782   # 13:06 HKT 复验；首验约 1507
hot_loaded=50
signals=0               # 「无信号」— max(jev_strength)≈0.205 < 0.3
decision_log table: exists
```

### Lowered threshold (`--min-jev-strength 0.15`)

```
buy signals written to decision_log: 14
structured SIGNAL logs: yes
```

### Isolation checks

| Check | Result |
|---|---|
| Indicator RO (`mode=ro`) | OK |
| No calls to paper/live | OK |
| No edits to crawl/indicator defaults | OK |
| Secrets not logged | OK |

## How to re-run

```bash
cd /workspace/dexscreener-crawler
.venv/bin/python -m decision_engine.run --strategy jev_strength_v1 --dry-run --once
.venv/bin/python -m decision_engine.run --strategy jev_strength_v1 --dry-run --once --min-jev-strength 0.15
```

## Caveats

- 默认阈值 0.3 高于当前库 max≈0.205 → 空跑常为「无信号」，属预期。
- `RiskManager` 仅为占位，不做仓位 / 熔断校验。
- `decision_log` 与主库同文件、独立 RW 连接；勿对主业务表做 ALTER。
- Step 1 **禁止**接实盘；`LIVE_TRADING_DISABLED` 必须保留。

## Next

- 24h 常驻 dry-run 稳定性（可选）
- Step 2：历史回测 + `paper_trade` 对接
