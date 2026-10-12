# 交易监控看板

只读监控包，复制进已有的 `dexscreener-crawler` 项目根目录后使用。它直接读本地 SQLite（主库、执行账本、模拟盘、回测、新闻），不写库，也不提供开闸或下单开关。

```bash
cp -a dashboard requirements-dashboard.txt /path/to/dexscreener-crawler/
cd /path/to/dexscreener-crawler
pip install -r requirements-dashboard.txt
export API_SECRET='your-long-secret'
python -m dashboard.app
```

浏览器打开 http://127.0.0.1:8081 ，在登录框输入同一个密钥（或请求头 `X-API-Key`）。启动、鉴权和验收步骤见 [dashboard/README.md](dashboard/README.md)。
