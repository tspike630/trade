# trade

私有交易系统 monorepo（采集 / 指标 / 决策 / 执行 / 复盘 / 看板）。

## 目录

- `crawler/` — 消息采集层（DexScreener + Helius + X 等）
- `config-backup/` — 启动脚本与 env 模板（**不含真实密钥**）
- `docs/RECOVERY.md` — 换机恢复清单
- `decision_engine/` — 决策层（Step1 dry-run + Step2 回测）；恢复见 [DECISION_ENGINE_RESTORE.md](DECISION_ENGINE_RESTORE.md)
- `scripts/jev_validity.py` — JEV 有效性周检
- 指标层代码在 `crawler/indicators/`（及 `sources/jev.py`）；其它层目录由各层后续提交（`dashboard/`、`execution/`、`review/` …）

## 安全

- 永不提交 `.env`、API Key、`*.db`、日志
- 实盘默认关闭；保留 `LIVE_TRADING_DISABLED` 熔断文件

详见 [docs/RECOVERY.md](docs/RECOVERY.md)。
