# Indicators 轻量 UX（Priority-2）

只读、旁路、不改评分/告警默认值，也不改 Step3 情绪面。勿停 `main.py` / `indicators.run` / `sources.jev`。

## 新增内容

| 文件 | 作用 |
|------|------|
| `scripts/top.sh` + `./top.sh` | 一键查询 `GET /indicators/top` |
| `scripts/grok_dd_report.py --telegram` | 可选把 DD 报告推到 Telegram **admin** 频道（默认关） |
| `static/admin.html` | 增加 Indicators Top 看板（纯前端调 API） |
| `INDICATORS_LIGHT.md` | 本文 |

## 1) `./top.sh` 一键 Top

依赖已运行的 `api_server`（默认 `http://127.0.0.1:8080`）。鉴权与其它数据路由相同：从环境变量或项目根 `.env` 读取 `API_SECRET`，以 `X-API-Key` 发送；**脚本不会打印密钥**。

| 子命令 | 对应 `sort` |
|--------|-------------|
| `local` | `local_score` |
| `change5m` | `change_5m` |
| `volume` | `volume_strength` |

默认：`chain=solana`，`limit=20`。

```bash
# 项目根目录
./top.sh local
./top.sh change5m
./top.sh volume

# 可选：chain、limit
./top.sh local solana 10
./scripts/top.sh volume solana 20

# 帮助
./top.sh --help
```

相关环境变量（均可来自 `.env`，未设置用默认）：

- `API_SECRET` — 必填（api_server 开启鉴权时）
- `API_HOST` — 默认 `127.0.0.1`
- `API_PORT` — 默认 `8080`

等价 curl：

```bash
curl -sS -H "X-API-Key: $API_SECRET" \
  "http://127.0.0.1:8080/indicators/top?chain=solana&sort=local_score&limit=20"
```

## 2) DD 报告推 Telegram（可选）

DD 报告只读输出含技术面 / 资金面 / **舆情面**（`sentiment_*`；缺失则「暂无数据」），不改评分或告警。

```bash
# 仅写文件（默认）
python scripts/grok_dd_report.py --chain solana --token <TOKEN>

# 额外推送到 admin 频道（TELEGRAM_CHAT_ID_ADMIN，否则 TELEGRAM_CHAT_ID）
python scripts/grok_dd_report.py --chain solana --token <TOKEN> --telegram
```

- **默认关闭**：不加 `--telegram` 且未设 `GROK_REPORT_TELEGRAM=1` 时不推送。
- **安全降级**：未配置 `TELEGRAM_BOT_TOKEN` / chat id 时只打日志，报告仍写出，退出码仍为 0。
- 实现走 `alerts_telegram.TelegramAlerter.send_message`（与现有告警同一套 Bot API）。

## 3) Admin Top 看板

打开 `http://127.0.0.1:8080/`（需在页面「Set API key」填入与 `API_SECRET` 相同的密钥）。新增 **Indicators Top** 卡片：

- 下拉切换 `jev_strength` / `change_5m` / `volume_strength`
- 可改 chain / limit，点 **Load Top** 或随 Refresh 刷新
- 纯前端 `fetch('/indicators/top?…')`，不改后端默认值

## 原则回顾

- 不停止 crawl / indicator / jev 循环
- 不改 `scoring.py`、告警阈值默认值、Step3 sentiment
- Top 与 DD 推送均为只读或旁路输出
