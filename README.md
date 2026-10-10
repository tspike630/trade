# trade

私有交易系统 monorepo（采集 / 指标 / 决策 / 执行 / 复盘 / 看板）。

## 目录

- `crawler/` — 消息采集层（DexScreener + Helius + X 等）
- `dashboard/` — 只读交易看板。仓库根执行 `pip install -r requirements-dashboard.txt`，再 `export DASHBOARD_DB_ROOT="$PWD/crawler" API_SECRET=...` 后 `python -m dashboard.app`（http://127.0.0.1:8081）。密钥与数据库不进 Git
- `config-backup/` — 启动脚本与 env 模板（**不含真实密钥**）
- `docs/RECOVERY.md` — 换机恢复清单
- 指标层代码在 `crawler/indicators/`（及 `sources/jev.py`）；决策 / 执行 / 复盘目录由各层后续提交

## 安全

- 永不提交 `.env`、API Key、`*.db`、日志
- 实盘默认关闭；保留 `LIVE_TRADING_DISABLED` 熔断文件

详见 [docs/RECOVERY.md](docs/RECOVERY.md)。
