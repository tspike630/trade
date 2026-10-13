# 交易监控看板

阶段一只读监控：风险、持仓、信号流、执行记录。手动确认交易（P4）不在本包内。

本仓把 `dashboard/` 放在仓库根（与 `crawler/` 同级）。这一份是真源。`crawler/dashboard` 是滞后副本，没有 `panels/evolution.py`，不要在那边改。SQLite 在 `crawler/`，不在看板旁边，所以启动前要设 `DASHBOARD_DB_ROOT` 指向 `crawler`（步骤见 `docs/RECOVERY.md`「交易看板」）。不设时，默认数据目录是本包的上一级（仓库根），读不到采集库。

## 启动

```bash
pip install -r requirements-dashboard.txt
export API_SECRET='your-long-secret'
export DASHBOARD_DB_ROOT="$PWD/crawler"   # 本仓：库在 crawler/
python -m dashboard.app
```

- 地址：http://127.0.0.1:8081
- 只绑定 `127.0.0.1`。`DASHBOARD_HOST` 若被设成其他地址会被忽略。
- 端口：`DASHBOARD_PORT`，默认 `8081`。
- 刷新：`DASHBOARD_REFRESH_SEC`，限制在 30–60 秒，默认 45。信号流和其余面板共用这个轮询。
- 数据目录：`DASHBOARD_DB_ROOT`。相对文件名再拼到这个目录上。

## 界面外壳

只读壳用 Obsidian 色：页面底 `#080808`，顶栏和底栏 `#111111`，强调色 `#d97706`。数据查询仍在 `dashboard/data.py`，连接保持 `mode=ro`。

- 顶栏：CRYPTO TERMINAL / 链上新币投研终端、实时点、进程心跳、UTC 时钟、刷新。
- 模块标签：DASHBOARD / MARKETS / AI CHAT。`/chat` 是同一套鉴权下的 AI CHAT。
- DASHBOARD 内子标签：OVERVIEW（风险 + 持仓）、SIGNALS、EXECUTION。注册表里新增的面板默认落在 OVERVIEW。
- 底栏：会话时长、库文件就绪数、进程心跳、距上次刷新的秒数。
- MARKETS 读 `token_indicators`，流动性取 `primary_pair_address` 在 `pair_history` 的最新值，覆盖率沿用信号流的口径。列只有 change_*、流动性、JEV、覆盖、档。不展示 RSI、MACD、BOLL。
- 本仓库没有 `dashboard.chat` 时，AI CHAT 只显示未挂载说明，不调用模型，不写 `dashboard_state.db`。挂上模块并提供 `render(store)` 后，同一页会调用它。

单库路径沿用爬虫里已有的环境变量，也可以单独覆盖：

| 库 | 默认文件 | 环境变量（按顺序） |
|----|----------|--------------------|
| 主库 | `dexscreener.db` | `DASHBOARD_MAIN_DB`、`DB_PATH` |
| 执行账本 | `execution_ledger.db` | `DASHBOARD_EXECUTION_DB`、`EXECUTION_LEDGER_PATH` |
| 模拟盘 | `paper_trading.db` | `DASHBOARD_PAPER_DB`、`PAPER_DB_PATH` |
| 回测 | `backtest_ledger.db` | `DASHBOARD_BACKTEST_DB` |
| 新闻 | `news_events.db` | `DASHBOARD_NEWS_DB` |

## 鉴权

与 `api_server` 相同：

- 环境变量 `API_SECRET` 已设置时，数据视图要求密钥一致。浏览器在登录框输入；程序化访问可以带请求头 `X-API-Key`。
- 比较使用 `secrets.compare_digest`（长度异常时改为等长摘要比较）。密钥不写进日志、页面、SQLite 或会话文件。会话里只存「已通过」布尔值，文件在 `NICEGUI_STORAGE_PATH`（默认临时目录），不在数据库旁边。
- `API_SECRET` 未设置且 `ALLOW_INSECURE_API` 不是 `1`：只显示配置说明，不读业务数据。
- `ALLOW_INSECURE_API=1`：本地无鉴权，页面会提示不要暴露到公网。
- `GET /health` 不需要密钥，只返回服务名和是否要求鉴权。

## 四个面板

闸门只展示，没有开关。

- **风险**：模拟盘 / 仓位闸优先读最新一条 `execution_log.receipt_json` 的 `paper_enabled` / `pm_enabled`（网关回执）。该行 `ts` 超过网关心跳窗口（`GATEWAY_HEARTBEAT_STALE_SEC`，默认 120 秒）时标「网关陈旧」，指示灯不沿用旧的 ON/OFF，回退到本进程环境变量并标「看板进程」。没有回执时同样只显示看板进程的值。杀开关文件默认 `LIVE_TRADING_DISABLED`（可用 `LIVE_KILL_SWITCH_PATH`）。实盘要同时满足 `LIVE_TRADING_ENABLED`、杀开关文件不存在、`LIVE_TRADING_CONFIRM` 等于公开短语、以及 `LIVE_EXCHANGE_API_URL` 非空，才会显示「开启」（与 `live_trade.live_gates_open` 同一组条件）。今日信号数 / 校验通过率 / 已执行率来自 `execution_log` 当日行（UTC 日）。近 7 日区块是决策粒度：每条 `decision_log` 只取最新一条 execution，比率分母是「有执行的决策」，和 `review.data.summarize_execution` 一致，窗口是滚动 168 小时，不是自然周。
- **持仓**：模拟盘 `status` 未平仓（或没有 `closed_at`）。现价是 `pair_history` 里该 `chain_id + pair_address` 的最新 `price_usd`，没有则显示「—」，盯市回退到 `size_usd`。浮盈：有 `qty` 时 `(现价-成本价)*qty`，否则 `size_usd * (现价/成本价 - 1)`。开仓时间列优先 `opened_at`，没有则用 `open_ts`。TP/SL 按库内原值加 `%`，不换算。
- **信号流**：`decision_log` 最近 50 条。`jev_strength` 顺序与 `review.data` 相同（`raw_json.extras`、`raw_json`、原因里的 `jev_strength=`），再回退 `indicator_history`。覆盖率优先 `jev_signals.coverage_pct`，否则 `token_indicators.jev_confidence * 100`，再否则 raw。流动性优先该交易对最新 `pair_history.liquidity_usd`。同一决策多条 execution 时取 `ts`、`id` 最新的一条。
- **执行记录**：最近 50 条，回执只显示摘要。`api_key`、`secret`、`password`、`private_key` 等字段会丢掉。近 7 日拒绝包含校验失败，以及 `status = rejected` 或回执状态 `pm_rejected` / `risk_rejected` / `paper_failed` / `validation_failed`。`validation_errors` 先按 JSON 数组取第一项，解析失败再按 `;` 或换行切。

进程心跳来自主库，不是猜进程名：`run_checkpoint.updated_at`（主爬虫）、`indicator_history.recorded_at`（指标）、`jev_signals.fetched_at`（JEV）、`decision_log.ts`（决策引擎）。这四项超过 `HEARTBEAT_STALE_SEC`（默认 900 秒）标「停滞」。执行网关看 `execution_log.ts`，窗口是 `GATEWAY_HEARTBEAT_STALE_SEC`（默认 120 秒，与 API 心跳一致），不跟 900 秒走。没有时间戳标「未知」。

日志里的 `SecretFilter` 挂在 handler 上。子 logger 传上来的记录不会经过 root logger 的 filter。

AI 聊天上下文取该会话最近 200 条（按 id 倒序再排回时间序），不是最早的 200 条。回测权益颜色相对该次 `start_cash`，不跟固定的 10000 比。

库或表缺失时，对应区块显示「数据不可用」和原因，进程继续跑。表结构用 `PRAGMA table_info` 探测，不按任务书里的别名硬编码（例如持仓时间是 `opened_at`）。

## 扩展

新面板加在 `dashboard/panels/registry.py` 的 `PANELS` 末尾，实现自己的 `render(store)`。不要改另外四个面板。所有 SQLite 访问留在 `dashboard/data.py`，连接必须是 `mode=ro`。

## 离线自检

不需要真实库：

```bash
python -m dashboard.smoke_test
```

脚本会在临时目录建空库和一份小样本，核对今日比率、7 日决策口径、持仓盯市、信号关联、拒绝分桶、缺库降级，以及只读连接拒绝 `INSERT` 且不改文件 mtime。

## 验收（对应任务书 §7）

- [ ] `python -m dashboard.app` 后打开 http://127.0.0.1:8081 。设置了 `API_SECRET` 时，未登录看不到持仓和信号；`/health` 仍可访问。
- [ ] 风险面板的模拟盘 / 仓位闸：回执新鲜时跟 `receipt_json`，并标明「网关回执」；回执陈旧或没有回执时跟本进程环境变量，并标明「看板进程」或「网关陈旧」。实盘未设 `LIVE_EXCHANGE_API_URL` 时不是「开启」。今日数字等于下面这条 SQL（UTC 日）。近 7 日「校验通过率 / 已执行率」与 `review.data.summarize_execution` 同一分母；和 `review.run --period 7d` 对账时先对齐窗口，滚动 168 小时不等于自然周。

```sql
SELECT COUNT(*) AS n,
       SUM(validation_ok = 1) AS ok_n,
       SUM(executed = 1) AS executed_n
FROM execution_log
WHERE substr(ts, 1, 10) = strftime('%Y-%m-%d', 'now');
```

- [ ] 模拟盘 0 持仓时持仓面板是空态，权益仍显示现金。有持仓时抽两条：现价等于该 pair 最新 `pair_history.price_usd`，浮盈按上面的公式手算。
- [ ] 信号流 50 条，每条的校验 / 已执行 / 执行器等于该 `decision_log_id` 最新一条 `execution_log`。多条时面板写「多条取最新」。
- [ ] 执行记录 50 条；拒绝分桶等于近 7 日校验失败的首段，加上 PM / 风控 / paper 拒绝（`status` 或回执状态）。校验通过且未拒绝的 dry-run 不进分桶。
- [ ] 跑一小时后四个库文件的 mtime 不变，日志里没有 `API_SECRET` 或交易所密钥。自检里的只读连接也会断言 mtime。
- [ ] 关掉任意一个库再打开页面：对应面板出现「数据不可用」和文件名，进程不退出。
