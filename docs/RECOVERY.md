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

## 4. 交易看板
## 4. 执行层恢复

代码：仓库根 `execution/`（网关 / ledger / PaperExecutor / position_manager / run）。`crawler/execution` 指向同一目录，这样采集目录里的 `python -m execution.run` 与 `daily_check.py` 仍能导入。

systemd 模板：`deploy/dexscreener-execution.service`（可选；`ExecStart` 只有 `--loop`，不含 `--paper-enabled` / `--pm-enabled`）。`crawler/deploy/dexscreener-execution.service` 内容相同。

不含：`.env`、密钥、`execution_ledger.db`、`paper_trading.db`、`logs/`。

采集主码、依赖和 `crawler/.venv` 见第 2 节。在采集目录恢复执行层：

1. 指主库只读：`DB_PATH` / `DECISION_DB_PATH` → 本机 `dexscreener.db`（WAL 推荐）。
2. 验证（不下单）：

```bash
cd crawler
python -m execution.run --once
```

预期：消费 `decision_log` 增量，写入同目录 `execution_ledger.db`（或 `EXECUTION_LEDGER_PATH`）。

3. 挂常驻（默认关闸）：

```bash
cd crawler
nohup .venv/bin/python -m execution.run --loop >> logs/execution_loop.log 2>&1 &
echo $! > logs/execution_loop.pid
```

4. **不要**默认打开：`EXECUTION_PAPER_ENABLED`、`EXECUTION_PAPER_TICK`、`PM_ENABLED` / `PM_*`。开闸需单独裁决；实盘另需确认。
5. 巡检：`python daily_check.py` 应含 execution 进程存活 + 今日台账 + 持仓概览。

若有 `EXECUTION_*` / `PM_*` env，备份到私密配置包，与 `.env` 同级保管，不进仓。

## 5. 其它层（各层跟进提交后）

只读 NiceGUI 看板在仓库根 `dashboard/`（与 `crawler/` 同级）。只读本地 SQLite（主库、执行账本、模拟盘、回测、新闻），不写库，不提供开闸开关。`API_SECRET`、`.env`、`*.db`、日志不进 Git。

依赖在仓库根 `requirements-dashboard.txt`（采集层用自己的 `crawler/requirements.txt`，两套 venv 分开）。`crawler/requirements-dashboard.txt` 内容相同，安装用根上这一份。

库文件在 `crawler/`（`dexscreener.db`、`execution_ledger.db`、`paper_trading.db`、`backtest_ledger.db`、`news_events.db`）。看板默认把包的上一级当数据目录，所以必须设 `DASHBOARD_DB_ROOT`。杀开关 `LIVE_TRADING_DISABLED` 也按这个目录找。`crawler/dashboard/` 是采集层带入的同内容副本；换机启动用仓库根这份。

```bash
cd /path/to/trade
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dashboard.txt
export API_SECRET='长随机密钥'          # 与登录框 / X-API-Key 相同；不要写入仓库
export DASHBOARD_DB_ROOT="$PWD/crawler"  # 不设则去仓库根找库，会空
python -m dashboard.app                  # http://127.0.0.1:8081 ，只绑 127.0.0.1
```

冒烟（不需要真实库）：

```bash
python -m dashboard.smoke_test
```

验收：

- `GET http://127.0.0.1:8081/health` 返回 ok；未设 `API_SECRET` 且未开 `ALLOW_INSECURE_API=1` 时，数据页关闭
- 浏览器打开 `http://127.0.0.1:8081`，密钥正确后能看到风险 / 持仓 / 信号流 / 执行记录
- 四面板只读；跑一段时间后 `crawler/*.db` 的 mtime 不变

细节：`dashboard/README.md`。

## 5. 其它层（各层跟进提交后）

- 看板：见上文「交易看板」
- 决策：`decision_engine/` → dry-run 指只读 `DB_PATH`
- 决策：仓库根 `decision_engine/` → [DECISION_ENGINE_RESTORE.md](../DECISION_ENGINE_RESTORE.md)
- 执行：`execution/` → `--once` 再 `--loop`（paper/PM 默认关）
- 复盘：`review/` → `python -m review.run --period 7d --report`

## 6. 验收

- [ ] `gh` 已登录且能 `git pull`
- [ ] `crawler/.env` 有密钥但未进 Git（`git status` 干净）
- [ ] 主爬虫心跳新鲜；`LIVE_TRADING_DISABLED` 存在
- [ ] 指标 loop + JEV loop 冒烟
- [ ] 看板：`/health` 为 ok，`python -m dashboard.smoke_test` 通过；`API_SECRET` 与 `*.db` 未进 Git
- [ ] 决策 / 执行 / 复盘按各自清单冒烟通过
- [ ] 指标 loop + JEV loop 冒烟；看板 / 决策 / 复盘按各自清单冒烟通过
- [ ] `cd crawler && python -m execution.run --once` 写入 ledger；常驻 loop 未加 `--paper-enabled` / `--pm-enabled`，`EXECUTION_PAPER_ENABLED` 与 `PM_ENABLED` 仍为关
- [ ] 实盘相关环境变量仍为关闭

## 7. 回滚

- 代码：`git revert` / 切旧 commit
- 数据：`backups/` 或换机前拷贝的 `dexscreener-*.db`
- 实盘：确保 `LIVE_TRADING_DISABLED` 在；勿设 `LIVE_TRADING_ENABLED=1`

## 决策引擎

Step1 dry-run 与 Step2 回测在仓库根 `decision_engine/`，JEV 周检为 `scripts/jev_validity.py`。换机步骤、烟雾命令和上线闸门见 [DECISION_ENGINE_RESTORE.md](../DECISION_ENGINE_RESTORE.md)。

不提交 `.env`、API Key、`*.db`（含 `dexscreener.db` / `backtest_ledger.db`）、`logs/`。虚拟环境用 `crawler/.venv`；主库用本机 `crawler/dexscreener.db`（或 `DB_PATH` / `DECISION_DB_PATH`）。
