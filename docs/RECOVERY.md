# 换机恢复清单（trade）

目标：新电脑按本清单可恢复到「采集长跑 + 各层只读旁路」状态。**不含实盘开闸。**

## 0. 前置

1. 安装：`git`、`gh`、`python3.11+`、`pip`、`rsync`
2. `gh auth login`（需 `repo` 权限）
3. 克隆：

```bash
gh repo clone tspike630/trade
cd trade
```

## 1. 配置备份（密钥另存，不进 Git）

从旧机安全拷贝到本机（U 盘 / 1Password / 私密通道），**不要**进仓库：

| 文件 | 说明 |
|------|------|
| `.env` | `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID*`、`HELIUS_API_KEY`、`X_BEARER_TOKEN`、`API_SECRET`、可选 `RISK_API_*` / `JEV_*` |
| `LIVE_TRADING_DISABLED` | 实盘熔断文件（建议始终保留） |
| `*.db` 可选 | 若要延续历史数据：主库 `dexscreener.db` 等；否则空库冷启动 |

模板见 `config-backup/env.example`。

放到：

```bash
cp /path/to/secret.env crawler/.env
# 或 export 进 shell / systemd EnvironmentFile
```

## 2. 采集层恢复

```bash
cd crawler
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# 确认 WAL（store 会自动 PRAGMA）；若有旧库可直接拷贝 dexscreener.db
test -f LIVE_TRADING_DISABLED || cp LIVE_TRADING_DISABLED.example LIVE_TRADING_DISABLED
python main.py --health   # 无旧心跳会 FAIL，正常
chmod +x stabilize_run.sh paper_tick_loop.sh scripts/backup_once.sh 2>/dev/null || true
nohup ./stabilize_run.sh >> logs/stabilize-main.log 2>&1 &
# 可选
nohup ./paper_tick_loop.sh >> logs/paper-tick.log 2>&1 &
./scripts/backup_once.sh
python main.py --health   # 应 OK
python daily_check.py
```

## 3. 其它层（各层跟进提交后）

- 看板：`dashboard/` → 见其 README（通常 `8081` + `API_SECRET`）
- 指标：`indicators/` → migrate + `indicators.run` / JEV
- 决策：`decision_engine/` → dry-run 指只读 `DB_PATH`
- 执行：`execution/` → `--once` 再 `--loop`（paper/PM 默认关）
- 复盘：`review/` → `python -m review.run --period 7d --report`

## 4. 验收

- [ ] `gh` 已登录且能 `git pull`
- [ ] `crawler/.env` 有密钥但未进 Git（`git status` 干净）
- [ ] 主爬虫心跳新鲜；`LIVE_TRADING_DISABLED` 存在
- [ ] 看板 / 指标 / 决策 / 执行 / 复盘按各自清单冒烟通过
- [ ] 实盘相关环境变量仍为关闭

## 5. 回滚

- 代码：`git revert` / 切旧 commit
- 数据：`backups/` 或换机前拷贝的 `dexscreener-*.db`
- 实盘：确保 `LIVE_TRADING_DISABLED` 在；勿设 `LIVE_TRADING_ENABLED=1`
