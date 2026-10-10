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

## 3. 指标层恢复（代码在 `crawler/indicators/`，与采集同仓同 venv）

指标旁路只写 `token_indicators` / `indicator_history` / `jev_signals`，**不改**评分权重（`INDICATORS_WEIGHT_*=0`）。密钥可选：无 `JEV_API_KEY` 时走 `jevs.fun` 公开档。

```bash
cd crawler && source .venv/bin/activate
# 表结构：首次跑 run 会 migrate；亦可
# sqlite3 dexscreener.db < models/indicator_tables.sql
python -m indicators.run --once          # 或 --full
python demo_indicators_p0.py             # API 冒烟（需 api_server + API_SECRET）
nohup python -m indicators.run --loop --interval 300 >> logs/indicators.log 2>&1 &
nohup python -m sources.jev --loop --interval 900 >> logs/jev.log 2>&1 &
# 可选 Top：./top.sh jev|change5m|volume
```

验收：`pgrep -af 'indicators.run|sources.jev'`；`token_indicators` 有新鲜 `updated_at`；权重仍为 0。

细节：`crawler/INDICATORS_AUDIT.md`、`AUTH_JEV.md`、`INDICATORS_STEP*.md`。

## 4. 复盘层

只读旁路，不改策略参数、不开实盘闸门。完整步骤与注意见 [RECOVERY_REVIEW.md](RECOVERY_REVIEW.md)。

在仓库根（`review/` 的上一级；四库只读路径或符号链接放在该目录，默认 `--db-root` 即此处）：

1. 装依赖（与主仓同一 venv / requirements；采集层为 `crawler/.venv`）
2. 四库只读：`dexscreener.db` / `execution_ledger.db` / `paper_trading.db` / `backtest_ledger.db`
3. `python -m review.run --period 7d --report`（报告写入 `exports/reports/`，该目录不进 Git）
4. 可选：`python -m sources.news --once`（独立 `news_events.db`）；周一 routine（周一 09:30 周报+预回测；新闻 RSS 工作日半小时）

```bash
# 若上一步停在 crawler/，先回到仓库根
cd "$(git rev-parse --show-toplevel)"
source crawler/.venv/bin/activate
python -m review.run --period 7d --report
# 可选。news 依赖采集层 logging_setup / httpx
PYTHONPATH=crawler python -m sources.news --once
```

规格与旁路说明：`docs/REVIEW_R1_SPEC.md`、`docs/REVIEW_R3_SCAFFOLD.md`、`docs/REVIEW_R4_LOOP.md`、`docs/EVOLUTION_SECTION10.md`、`docs/NEWS_SIDECHANNEL.md`。

## 5. 其它层（各层跟进提交后）

- 看板：`dashboard/` → 见其 README（通常 `8081` + `API_SECRET`）
- 决策：`decision_engine/` → dry-run 指只读 `DB_PATH`
- 执行：`execution/` → `--once` 再 `--loop`（paper/PM 默认关）

## 6. 验收

- [ ] `gh` 已登录且能 `git pull`
- [ ] `crawler/.env` 有密钥但未进 Git（`git status` 干净）
- [ ] 主爬虫心跳新鲜；`LIVE_TRADING_DISABLED` 存在
- [ ] 指标 loop + JEV loop 冒烟；看板 / 决策 / 执行 / 复盘按各自清单冒烟通过
- [ ] 实盘相关环境变量仍为关闭

## 7. 回滚

- 代码：`git revert` / 切旧 commit
- 数据：`backups/` 或换机前拷贝的 `dexscreener-*.db`
- 实盘：确保 `LIVE_TRADING_DISABLED` 在；勿设 `LIVE_TRADING_ENABLED=1`
