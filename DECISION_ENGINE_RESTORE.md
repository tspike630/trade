# Decision Engine — 换机恢复清单

## 本仓库（monorepo）

- 决策包在仓库根 `decision_engine/`。`crawler/decision_engine/` 是采集层备份里的同内容副本，这次未改。
- 周检脚本：`scripts/jev_validity.py`（`crawler/scripts/jev_validity.py` 保留）。
- 虚拟环境、`logging_setup`、`config` 在 `crawler/`。根包导入时把 `crawler/` 放进 `sys.path`。
- 主库默认 `crawler/dexscreener.db`，或用 `DB_PATH` / `DECISION_DB_PATH` 指向本机库。勿提交。

## 推仓内容（已进 trade）

- `decision_engine/`（含 backtest / strategies）
- `scripts/jev_validity.py`
- `DECISION_ENGINE_*.md` 文档

**不含：** `.env`、API Key、`*.db`、`decision_log` 数据、`logs/`、`backtest_ledger.db`

## 恢复步骤

1. Clone `tspike630/trade`，装项目依赖（与主仓同一 venv 即可）。
2. 配置只读主库：环境变量 `DB_PATH` / `DECISION_DB_PATH` 指向本机 `dexscreener.db`（或复制库文件；勿提交）。
3. 烟雾：`python -m decision_engine.run --strategy jev_strength_v1 --dry-run --once`
4. 可选 24h：`nohup python -m decision_engine.run --strategy jev_strength_v1 --dry-run --interval 60 >> logs/decision_engine_dryrun.log 2>&1 &`
5. 周检：`python scripts/jev_validity.py --out exports/reports/jev_validity_$(date +%Y%m%d).md`
6. 回测烟雾：`python -m decision_engine.backtest.run --strategy all --days 7 --dry-run`
7. 正式对比（周检达标后）：加 `--strict-coverage`

## 闸门

- 默认 dry-run；不接 paper/live。
- 策略上线门槛：unique_token≥10、raw n≥15、1h≥55% 连续两周（群内裁决）。
