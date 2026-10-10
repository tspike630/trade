# 复盘层换机恢复

## 纳入仓库
- `review/`（data / metrics / report / attribution / optimizer / evolution / run）
- `sources/news.py`（新闻 RSS 旁路）
- 文档：`docs/REVIEW_*.md`、`docs/EVOLUTION_SECTION10.md`、`docs/NEWS_SIDECHANNEL.md`、本文

## 不纳入
- `*.db`、`.env`、Key、`exports/reports/` 运行产物、`news_events.db`

## 恢复步骤
1. 装仓库依赖（与主仓同一 venv / requirements）
2. 准备四库只读路径（或符号链接）：`dexscreener.db` / `execution_ledger.db` / `paper_trading.db` / `backtest_ledger.db`
3. 冒烟：`python -m review.run --period 7d --report`（exit 0；报告写 `exports/reports/`）
4. 可选：`python -m sources.news --once`（独立 `news_events.db`）
5. 可选 routine：周一 09:30 周报+预回测；新闻 RSS 工作日半小时

## 注意
- 只读；不改策略参数 / 实盘闸门
- R3 `--optimize` 在历史不足时会 skip
- 落地走 R4；实盘闸门永远人工确认
