# 交易监控看板

本仓已放在仓库根。换机以 `docs/RECOVERY.md`「交易看板」为准：依赖是仓库根的 `requirements-dashboard.txt`，库目录用 `DASHBOARD_DB_ROOT` 指向 `crawler/`。

只读监控包。它直接读本地 SQLite（主库、执行账本、模拟盘、回测、新闻），不写库，也不提供开闸或下单开关。

```bash
cp -a dashboard requirements-dashboard.txt /path/to/dexscreener-crawler/
cd /path/to/dexscreener-crawler
pip install -r requirements-dashboard.txt
export API_SECRET='your-long-secret'
python -m dashboard.app
```

浏览器打开 http://127.0.0.1:8081 ，在登录框输入同一个密钥（或请求头 `X-API-Key`）。启动、鉴权和验收步骤见 [dashboard/README.md](dashboard/README.md)。
