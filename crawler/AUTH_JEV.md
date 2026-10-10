# JEV API Key 配置说明

## 摘要

- 未设置 `JEV_API_KEY`（空字符串）时行为与原来一致：**公开扫描、无鉴权**。
- 设置密钥后，按 `JEV_AUTH_MODE` 附加到请求；**永远不要把 key 写进日志或提交到 git**。

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `JEV_API_KEY` | `""` | API 密钥；空 = 公开模式 |
| `JEV_AUTH_MODE` | `header` | `header` → `X-API-Key`；`bearer` → `Authorization: Bearer …`；`query` → `?api_key=…`（日志经 `redact_url` 脱敏） |
| `JEV_BASE_URL` | `https://jevs.fun` | API 根，可覆盖 |
| `JEV_MAX_PER_MIN` | `7` | 客户端每分钟上限 |

建议限速（按套餐自行调整，注释参考见 `env.example`）：

- 公开 / 无 key：`JEV_MAX_PER_MIN=7`（IP 约 8/min）
- 鉴权 basic：`15`
- 鉴权 pro：`30`

示例（写入项目根 `.env` 或 export）：

```bash
# .env
JEV_API_KEY=your-real-key-here
JEV_AUTH_MODE=header
# JEV_BASE_URL=https://jevs.fun
# JEV_MAX_PER_MIN=15
```

完整模板见仓库根目录 `env.example`。

## 只重启 sources.jev

改 key / auth 后**只需**重启 JEV enricher，**不要**停 `main.py` 或 `indicators.run`：

```bash
cd /workspace/dexscreener-crawler
# 找到并结束旧 loop（示例）
pkill -f 'python -m sources.jev' || true
# 重新加载 .env 后启动
nohup .venv/bin/python -m sources.jev --loop --interval 900 >> logs/jev.log 2>&1 &
```

验证（可选）：

```bash
.venv/bin/python -m sources.jev --once --force --limit 1
tail -n 30 logs/jev.log
```

确认日志里 URL 无明文 key；`query` 模式下应看到 `api_key=***`。
