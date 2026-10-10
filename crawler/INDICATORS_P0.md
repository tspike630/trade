# Indicators P0 — 只读输出层（API + Grok DD）

## 原则

- **只读**：不写 `token_indicators` / `jev_signals`；不改 indicator 计算与主爬虫管线。
- **不杀进程**：勿停 `main.py` / `indicators.run` / `sources.jev`。
- **鉴权**：与其它数据路由相同，要求 `X-API-Key`（`API_SECRET`）。`/health` 仍开放。

## P0-1 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/indicators/{chain}/{token_address}` | 单币完整指标；无行 → **404** |
| GET | `/indicators/top` | Top N；`sort` 白名单；`limit` 1–100 |

### 响应结构（单币 / top 条目）

```json
{
  "chain_id": "solana",
  "token_address": "…",
  "technical": {
    "change_5m": 0.0,
    "change_15m": null,
    "change_1h": null,
    "change_4h": null,
    "change_24h": null,
    "volatility_24h": null,
    "volume_strength": 1.0
  },
  "funding": {
    "jev_raw_score": 96.0,
    "jev_score": null,
    "jev_signal": "bullish",
    "jev_confidence": 0.09,
    "jev_strength": 0.0864,
    "jev_updated_at": null
  },
  "tier": "hot",
  "primary_pair_address": "…",
  "sample_count": 12,
  "updated_at": "2026-10-09T19:30:34Z"
}
```

`/indicators/top` 外层另含 `count` / `chain` / `sort` / `limit` / `items`。

### Query（top）

| 参数 | 默认 | 约束 |
|------|------|------|
| `chain` | 无（全链） | 可选，如 `solana` |
| `sort` | `change_5m` | **仅** `change_5m` \| `jev_strength` \| `volume_strength` |
| `limit` | `20` | 1–100；非法 sort → 400 |

排序：`NULL` 置后，再按列 **DESC**，同分按 `updated_at DESC`。

### 示例 curl

```bash
# 单币
curl -sS -H "X-API-Key: $API_SECRET" \
  "http://127.0.0.1:8080/indicators/solana/<TOKEN_ADDRESS>"

# Top 20 by volume_strength
curl -sS -H "X-API-Key: $API_SECRET" \
  "http://127.0.0.1:8080/indicators/top?chain=solana&sort=volume_strength&limit=20"

# Top by JEV strength
curl -sS -H "X-API-Key: $API_SECRET" \
  "http://127.0.0.1:8080/indicators/top?chain=solana&sort=jev_strength&limit=10"
```

缺密钥且未 `ALLOW_INSECURE_API=1` → 与其它数据路由相同（401 / 503）。

## P0-2 Grok DD 升级

文件：`scripts/grok_dd_report.py`、`prompts/dd_report.md`。

新增组装字段（缺失不中断报告）：

- `indicators` ← `token_indicators`（`IndicatorStore.get_token_indicators`）
- `jev_signal` ← `jev_signals`（fallback）

Markdown 新增章节：

1. **技术面 Technical** — change 窗口、volatility、volume_strength、`trend_note`（根据正负号简述偏多/偏空/分化；无数据 →「暂无数据」）
2. **资金面 Funding (JEV)** — score / signal / confidence / strength；优先 `token_indicators`，否则 `jev_signals` 原始分；皆无 →「暂无数据」

原有 Snapshot / Risk / Market / Social / Verdict 等章节保留（prompt 编号已顺延）。

```bash
python scripts/grok_dd_report.py --chain solana --token <TOKEN>
# 输出 exports/reports/dd-*.md / .json
```

## P1 轻量（daily_check）

`daily_check.py` 追加三项（低覆盖 **WARN**，不因 JEV 少而 FAIL）：

| 检查名 | 含义 |
|--------|------|
| Indicators procs 三进程 | `pgrep -f` 看 `main.py` / `indicators.run` / `sources.jev` |
| JEV coverage 资金面覆盖 | `COUNT(jev_signals)`；0 或 <10 → WARN |
| Indicators freshness 指标新鲜度 | `COUNT` + `MAX(updated_at)`；>15min → WARN |

观察：

```bash
python daily_check.py
# 看输出中上述三行与 logs/daily_check-YYYYMMDD.json
```

## 本地验证

```bash
# 不依赖已启动 uvicorn 的 handler / 组装冒烟
python demo_indicators_p0.py

# 若需热加载路由：仅重启 api_server（勿杀 main/indicators/jev）
# 再用上方 curl 对真实端口 spot-check
```

## 改动文件

| 文件 | 变更 |
|------|------|
| `indicators/base.py` | `list_top_token_indicators` + sort 白名单 |
| `api_server.py` | `_format_indicator_row`、`GET /indicators/top`、`GET /indicators/{chain}/{token}` |
| `scripts/grok_dd_report.py` | 拉取 indicators/jev；技术面/资金面章节 |
| `prompts/dd_report.md` | 模板增加技术面/资金面 |
| `daily_check.py` | `check_indicators_p0` |
| `demo_indicators_p0.py` | 冒烟脚本 |
| `INDICATORS_P0.md` | 本文 |
