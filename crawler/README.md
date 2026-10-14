# DexScreener Crawler — Phase 1–5 + P0–P5

公开 API 爬虫全链路 + **P0 加固** + **P1.1 Telegram** + **P1** Telegram/Helius/X + **P2** history/score/admin UI/multi-chain。

> 代理默认关闭。优先 batch/分级轮询，再开 `USE_PROXY=1`。

## 安装

```bash
cd /workspace/dexscreener-crawler   # 生产可放到 /opt/dexscreener-crawler
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp deploy/dexscreener-crawler.env.example /etc/dexscreener-crawler.env  # 并填写 API_SECRET
```

## P0：API 访问控制

- 环境变量 `API_SECRET`：数据路由要求请求头 `X-API-Key: <secret>`
- `/health` **不鉴权**（给监控探活）
- 未设置 `API_SECRET` 且未开 `ALLOW_INSECURE_API=1` → 数据路由 **503**
- 本地调试：`ALLOW_INSECURE_API=1`（禁止公网）
- **建议**：防火墙 / 安全组仅放行运维 IP；API 绑定 `127.0.0.1` + 反代

```bash
export API_SECRET='long-random-secret'
python -m api_server

curl -s http://127.0.0.1:8080/health
curl -s -H "X-API-Key: $API_SECRET" http://127.0.0.1:8080/stats
# 无 Key → 401
```

## P0：数据库备份

```bash
python backup.py                  # → backups/dexscreener-YYYYMMDD-HHMMSS.db
python backup.py --retain 7       # 默认保留最近 7 份
```

使用 `sqlite3.Connection.backup()` 做一致性拷贝。定时：见 `deploy/crontab.example` 或 `deploy/dexscreener-backup.timer`。

## P0：systemd（仅提供单元文件，不在本机安装）

路径占位：`/opt/dexscreener-crawler`、用户 `dexscreener`、环境文件 `/etc/dexscreener-crawler.env`。

| 文件 | 作用 |
|---|---|
| `deploy/dexscreener-crawler.service` | `python main.py --loop`，`Restart=always` |
| `deploy/dexscreener-api.service` | `python -m api_server` |
| `deploy/dexscreener-backup.service` + `.timer` | 每日备份 |
| `deploy/crontab.example` | crontab 备选 |
| `deploy/dexscreener-crawler.env.example` | 环境变量模板 |

```bash
# 示例（需 root；本仓库验证环境不执行）
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dexscreener-crawler.service dexscreener-api.service
sudo systemctl enable --now dexscreener-backup.timer
journalctl -u dexscreener-crawler -f   # syslog/journal；应用仍写 logs/crawler.log
```

## P0：代理池健康

启用代理后自动启动后台探测线程：

- 周期探测 `PROXY_HEALTH_PROBE_URL`（默认 DexScreener profiles）
- 连续 `PROXY_PROBE_FAIL_EVICT`（默认 3）次失败 → **永久驱逐**
- 可用率低于 `PROXY_AVAILABILITY_ALERT_RATIO` → WARNING + `alerts` 表 `type=proxy_pool_low`
- `python demo_proxy_health.py` 本地演示驱逐（无需真代理）

## 爬虫 / API 常用命令

```bash
python main.py --force-lists
python main.py --loop --interval 60
python main.py --health
python main.py --sources helius,x --skip-lists
python main.py --helius-listen --loop --interval 60
python demo_helius.py
python demo_x.py
python -m api_server
python backup.py
```


## P1.1 Telegram 原生告警

复用现有 `detect_pair_anomalies` / cooldown / `alerts` 表，仅增加 Telegram 投递通道（可与 `ALERT_WEBHOOK_URL` 并行）。

| 变量 | 用途 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | 启用 Telegram（未设置则与以往一样：日志 + 可选 webhook） |
| `TELEGRAM_CHAT_ID_MAIN` | 新盘 / CTO |
| `TELEGRAM_CHAT_ID_ALERT` | 价格 / 成交量 / 流动性异动 |
| `TELEGRAM_CHAT_ID_ADMIN` | 代理池、系统错误等 |
| `TELEGRAM_CHAT_ID` | 未分频道时的统一回退 |

路由：`new_listing`/`cto_new`→MAIN；`price_change`/`volume_h24_jump`/`liquidity_drop`→ALERT；`proxy_pool_low` 等→ADMIN。

```bash
export TELEGRAM_BOT_TOKEN='123:ABC'
export TELEGRAM_CHAT_ID_MAIN='-100...'
export TELEGRAM_CHAT_ID_ALERT='-100...'
export TELEGRAM_CHAT_ID_ADMIN='-100...'
# 或只要一个群：
# export TELEGRAM_CHAT_ID='-100...'

python demo_telegram.py          # 干跑格式与路由
python demo_telegram.py --send   # 有 token 时发一条测试到 ADMIN/回退
```

成功发送后 `alerts.delivered=1`；失败只打 ERROR，不中断爬虫周期。

**Stabilize 说明**：MAIN/ALERT 可暂只配 ADMIN（或统一 `TELEGRAM_CHAT_ID`）——路由会回退；不必强行拆三个群。

## P1.2 Helius Solana 新盘监听

无 `HELIUS_API_KEY` 时为 **no-op**（`--sources helius` 仅打日志；`demo_helius.py` 干跑解析）。  
有 Key 时 `--helius-listen` 后台 `logsSubscribe`（mentions = DEX program IDs）→ 解析 signature / mint → `pending_enrich` → 既有 batch enrich / validate / upsert / `new_listing` 告警。

| 变量 | 用途 |
|---|---|
| `HELIUS_API_KEY` | 启用 live WSS + HTTP getTransaction |
| `HELIUS_WS_URL` | 可选完整 WSS 覆盖（默认 `wss://mainnet.helius-rpc.com/?api-key=…`） |
| `HELIUS_HTTP_URL` | 可选 HTTP RPC 覆盖 |
| `HELIUS_PROGRAM_IDS` / `HELIUS_MENTIONS` | 逗号分隔 program ID；默认见 `sources/solana_helius.py` |

默认订阅（公开文档，生产前请再确认）：Raydium AMM v4 / CLMM / CPMM、Pump.fun、PumpSwap、Meteora DLMM / pools、Orca Whirlpool。

```bash
export HELIUS_API_KEY='...'
# 可选收窄：
# export HELIUS_PROGRAM_IDS='6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P,675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8'

python demo_helius.py                 # 干跑 fixture 解析 + import
python main.py --helius-listen --loop --interval 60
# 或：python main.py --sources helius --skip-lists   # 无 key 时 no-op marker
```

断线：指数退避重连 + websockets ping（默认 120s，Helius 空闲约 10min）；连续失败 ≥ `HELIUS_DISCONNECT_ALERT_AFTER` 且 Telegram 启用 → `system_error` 走 ADMIN。  
去重：既有 `(chain_id, token_address)` upsert。

**Helius 未实现：** 完整 Raydium ABI 解码（best-effort log + getTransaction）。

## P1.3 X social sentiment

无 `X_BEARER_TOKEN` 时 **no-op**。有 token 时 `--sources x` 在每周期内：

1. **Early watch**：近期新盘（`X_EARLY_WATCH_SEC`）写入 `x_watch`，`tweets/search/recent` 查 `$SYMBOL` / 地址  
2. **Hot track**：热层（`HOT_SEEN_WINDOW_SEC` 且 `has_pairs=1`）跟踪 mention 速度；突增 → `social_spike` / `sentiment_anomaly`  
3. 若同 token 近期已有价格/量/流动性告警 → payload `dual_price_social` / `high_priority`  
4. `since_id` + mention 计数持久化到 SQLite，避免重复计费翻页  
5. Token bucket + **日/时硬熔断** `X_DAILY_BUDGET_REQUESTS` / `X_HOURLY_BUDGET_REQUESTS`

**永不把 bearer 打进日志。**

```bash
export X_BEARER_TOKEN='...'          # 勿提交
export X_DAILY_BUDGET_REQUESTS=100
export X_HOURLY_BUDGET_REQUESTS=20

python demo_x.py                     # 干跑 fixture
python main.py --sources x --loop --interval 60
```




## P3 风险 / 回测 / Grok 导出 / Worker

### 链上风险 `risk.py`
启发式扫描（薄流动性、买卖失衡 honeypot-like、高换手等）从质量分扣分并打 `risk_*` 标签。  
可选 `RISK_API_URL` + `RISK_API_KEY`（GoPlus/RugCheck 等；**需自行申请 key，切勿写入日志**）。

```bash
python demo_p3.py
export RISK_API_URL='https://your-scanner/...'   # optional
export RISK_API_KEY='...'
```

### 回测 `backtest.py`
```bash
python backtest.py --buy-after 5 --hold 30
python backtest.py --buy-after 5 --hold 30 --seed-demo --out exports/backtest.json
```

### Grok Bot / 蜘蛛爬虫上下文
```bash
python -m tools.grok_context --format both --out exports/grok_context
curl -H "X-API-Key: $API_SECRET" "http://127.0.0.1:8080/export/context?format=markdown"
```
Bot 可定期拉取 JSON/Markdown 作为工具上下文。

### Worker 骨架 `worker/`
默认 SQLite 队列（单机）；可选 Redis。
```bash
python -m worker.scheduler --once
python -m worker.run_worker --once   # 或 --loop
# 文档: worker/README.md
```


## P4 Grok 报告 / GoPlus 风险 / 多策略回测 / 模拟盘

### Grok 报告自动化
- 系统提示：`GROK_SYSTEM_PROMPT.md`；任务：`prompts/hourly_report.md` / `daily_report.md`
```bash
python scripts/grok_report.py --mode hourly --incremental
python scripts/grok_report.py --mode daily --telegram   # 需 Telegram；或 GROK_REPORT_TELEGRAM=1
# → exports/reports/report-{mode}-*.md|json 与 latest-*.md
```

### GoPlus 风格风险 API
```bash
export RISK_API_URL=https://api.gopluslabs.io   # 启用远程；未设则仅启发式
export RISK_API_KEY='...'                       # 可选；勿提交/打日志
export RISK_PROVIDER=goplus                     # 默认
python demo_p4.py                               # 无 key 干跑 + fallback
```

### 多策略回测
```bash
python backtest.py --strategy hold --buy-after 5 --hold 30
python backtest.py --strategy tpsl --tp 10 --sl 5 --hold 60 --seed-demo
python backtest.py --strategy grid --grid-step 3 --grid-levels 3 --hold 60 --seed-demo
```

### 模拟盘 paper_trade
```bash
python paper_trade.py init                         # PAPER_START_CASH default 10000
python paper_trade.py init --cash 1000             # small stabilize book
python paper_trade.py init --cash 1000 --reset-cash
python paper_trade.py open --chain solana --pair PAIR --size-usd 100 --tp 10 --sl 5
python paper_trade.py tick
python paper_trade.py status
python paper_trade.py demo
# Optional auto-open (default OFF): PAPER_AUTO_OPEN=1 PAPER_AUTO_MIN_SCORE=70 PAPER_AUTO_SIZE_USD=50
```

### Execution gateway（decision_log → ledger；Step B Paper / Step C PM）
```bash
# Step A 默认：只校验+写 execution_ledger（executed=0）；常驻 loop 保持关闸
python -m execution.run --once
python -m execution.run --loop

# Step B smoke：必须用临时 paper DB，勿默认开生产 paper_trading.db
SMOKE_PAPER=/tmp/paper_exec_smoke.db
python -m execution.run --once --paper-enabled --paper-db "$SMOKE_PAPER" \
  --ledger /tmp/execution_ledger_smoke.db --since-id 0 --limit 1
# 直接 executor 烟雾：BUY→SELL→EQUITY 见 execution/README.md

# Step C：PM_* 默认全关；开 PM_TOKEN_DEDUP 烟雾见 execution/README.md
# PM_ENABLED=0 时 check_open 透传
```
开关默认：`EXECUTION_PAPER_ENABLED=0`、`EXECUTION_PAPER_TICK=0`、`PM_ENABLED=0`（及各 `PM_*` 规则 off）。详见 `execution/README.md`。



## P5 深化：实盘骨架 / 新策略 / 价差信号 / 深度 DD

### Live trading（默认关闭）
```bash
python live_trade.py status          # gates_open=false
python live_trade.py intent --side buy --symbol DEMO --size-usd 5
python live_trade.py checklist       # paper → small live 清单
```
硬默认：`LIVE_TRADING_ENABLED=0` + 仓库内 `LIVE_TRADING_DISABLED` kill-switch。  
实盘还需 `LIVE_TRADING_CONFIRM=I_UNDERSTAND_THE_RISKS`、API URL、去掉 kill-switch；密钥仅 env，永不入库。

### 更多回测 / 纸上策略
```bash
python backtest.py --strategy dca --seed-demo
python backtest.py --strategy martingale --martingale-max-adds 3 --seed-demo
python backtest.py --strategy trend --trend-lookback 3 --seed-demo
python paper_trade.py simulate --strategy dca --size-usd 50
```

### 跨所价差（仅信号）
```bash
python arb_scan.py --dry-run
# 写入 alerts type=arb_opportunity — 不执行桥接/下单
```

### Grok 深度 DD
```bash
python scripts/grok_dd_report.py --chain solana --token TOKEN
# prompts/dd_report.md + 分数/风险/历史/社交/告警汇编
```

## 每日巡检 Daily QA

```bash
python daily_check.py
# → 终端 PASS/WARN/FAIL；logs/daily_check-YYYYMMDD.json（香港日历日）
# exit 0 = 无 FAIL；1 = 有 FAIL
```

检查项：pair_history 热层间隔、质量分 top10/分布、多链计数、heartbeat/备份/24h ERROR、DB 体积与可选 RSS。

## P2 历史 / 评分 / 管理台 / 多链

### 历史序列 `pair_history`
成功 upsert pair 后按 tier 间隔快照 `priceUsd` / volume / liquidity：
- hot ≈ `HISTORY_HOT_INTERVAL_SEC`（默认 300s）
- warm ≈ 1800s；cold ≈ 7200s

```bash
curl -H "X-API-Key: $API_SECRET" \
  "http://127.0.0.1:8080/history?chain=solana&pair=PAIR_ADDR"
curl -H "X-API-Key: $API_SECRET" \
  "http://127.0.0.1:8080/pairs/solana/PAIR_ADDR/history"
```

### 质量分 `scoring.py`
0–100（流动性 / 成交量 / 年龄 / txns / X social heat）；标签 `new` `high_liq` `high_social` `low_quality`。  
写入 `tokens.quality_score`。`ALERT_MIN_SCORE`（默认 25）以下：**入库告警但不推 Telegram/webhook**。

```bash
python demo_p2.py
export ALERT_MIN_SCORE=30
```

### 管理台
浏览器打开 `http://127.0.0.1:8080/` → 「Set API key」写入 sessionStorage，JS 用 `X-API-Key` 头拉 `/dashboard/summary`（不把 key 放 URL）。

### 多链
```bash
export ENABLED_CHAINS=solana,ethereum,bsc,base   # 或 CHAINS=
# 列表摄取与 enrich 队列会跳过未启用链
```
文档化默认：`solana`, `ethereum`, `bsc`, `base`。

```bash
python demo_p2.py
python main.py --skip-lists --flush-pending   # 触发 enrich → history + score
python -m api_server   # 打开 /
```


## Indicators 旁路（技术面 / 资金面 / 舆情面）

独立进程，只写 `token_indicators` / `indicator_history`，**不改** `scoring.py` 与告警默认权重（`INDICATORS_WEIGHT_*=0`）。

| 文档 | 内容 |
|------|------|
| **INDICATORS_AUDIT.md** | 全流程审计清单 + 运维（推荐入口；含 M-1 功效脚注） |
| INDICATORS_OPERATORIZATION.md | M-3 时间序列算子硬规则（防 look-ahead） |
| INDICATORS_FACTOR_CANDIDATES.md | factor_loop 候选（供需 P-3/P-4；艾略特 E-1/E-4/E-5，N-1..N-6 不采用）+ M-2 方向校准 |
| INDICATORS_STEP1.md | 技术面 |
| INDICATORS_STEP2.md | JEV 资金面（信号强度 ≠ 胜率） |
| INDICATORS_STEP3.md | X 舆情面 |
| INDICATORS_P0.md / INDICATORS_LIGHT.md | API Top / UX |
| AUTH_JEV.md | JEV API Key / 公开模式 |

```bash
# 旁路循环（勿与 crawl 混跑在同一进程）
python -m indicators.run --loop --interval 300
python -m sources.jev --loop --interval 900

# 一键 Top（需 api_server）
./top.sh jev
./top.sh change5m solana 10

# DD（含技术/资金/舆情；可选 --telegram）
python scripts/grok_dd_report.py --chain solana --token TOKEN
python scripts/grok_dd_report.py --chain solana --token TOKEN --telegram

# 冒烟
python demo_indicators_p0.py
python daily_check.py
```

API：`GET /indicators/top`、`GET /indicators/{chain}/{token}`（需 `X-API-Key`）。Admin：`http://127.0.0.1:8080/` → Indicators Top。

## Phase 摘要

1. 正确 API + batch + 去重 SQLite  
2. 重试 / 日志 / checkpoint  
3. 分级轮询 + async + 校验  
4. 异动告警 + 心跳  
5. 可选代理 / 只读 API / 多源 stub  
**P0** 鉴权、备份、systemd、代理健康  
**P1.1** Telegram  
**P1.2** Helius Solana listener  
**P1.3** X social sentiment  
**P2** history / scoring / admin UI / multi-chain  
**P3** risk / backtest / grok export / worker  
**P4** grok report / goplus / multi-backtest / paper trade  
**P5** live skeleton / dca-martingale-trend / arb signals / deep DD

## 目录

```
api_server.py  backup.py  proxy_pool.py  demo_proxy_health.py  demo_helius.py
sources/solana_helius.py  sources/x_api.py  fixtures/helius_*.json  fixtures/x_*.json
demo_x.py
deploy/*.service  deploy/*.timer  deploy/crontab.example
```

## 命令总索引 (Master command index)

| Area | Command |
|---|---|
| Install | `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt` |
| Crawl once | `python main.py --force-lists` |
| Crawl loop | `python main.py --loop --interval 60` |
| Stabilize | `./stabilize_run.sh` → see **STABILIZE.md** |
| Health | `python main.py --health` |
| Daily QA | `python daily_check.py` |
| API | `python -m api_server` / `curl -H "X-API-Key: $API_SECRET" http://127.0.0.1:8080/stats` |
| Backup | `python backup.py --retain 7` / `./scripts/backup_once.sh` |
| Telegram dry | `python demo_telegram.py` |
| Helius dry | `python demo_helius.py` |
| X dry | `python demo_x.py` |
| Proxy health | `python demo_proxy_health.py` |
| P2–P5 demos | `python demo_p2.py` … `demo_p5.py` |
| Backtest | `python backtest.py --strategy hold\|tpsl\|grid\|dca\|martingale\|trend --seed-demo` |
| Paper | `python paper_trade.py init\|tick\|status\|demo` |
| Live gates | `python live_trade.py status\|checklist` (keep kill-switch) |
| Arb signals | `python arb_scan.py --dry-run` |
| Grok context | `python -m tools.grok_context --format both --out exports/grok_context` |
| Grok report | `python scripts/grok_report.py --mode hourly --incremental` |
| Grok DD | `python scripts/grok_dd_report.py --chain solana --token TOKEN` (`[--telegram]`) |
| Indicators | `python -m indicators.run --loop --interval 300` / `--once` / `--full` |
| JEV enricher | `python -m sources.jev --loop --interval 900` — **AUTH_JEV.md** |
| Indicators Top | `./top.sh jev\|change5m\|volume` → `/indicators/top` |
| Indicators audit | **INDICATORS_AUDIT.md** |
| Indicators demo | `python demo_indicators_p0.py` |
| Worker | `python -m worker.scheduler --once` / `python -m worker.run_worker --once` |
| Cron examples | `deploy/crontab.example` / `deploy/crontab.stabilize.example` |
| Ops runbook | **STABILIZE.md** / audit **AUDIT_AND_OPT.md** / **INDICATORS_AUDIT.md** |

Optional hooks (default **OFF**): `DD_AUTO_*`, `PAPER_AUTO_*` — see env.example / STABILIZE.md.
