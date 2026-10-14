# 复盘层换机恢复

## 纳入仓库
- `review/`（data / metrics / report / attribution / optimizer / evolution / run）
- `sources/news.py`（新闻 RSS 旁路）
- 文档：`docs/REVIEW_*.md`（含 `REVIEW_WARNING_CASES.md`）、`docs/EVOLUTION_SECTION10.md`、`docs/NEWS_SIDECHANNEL.md`、本文

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
- 警示事例见 `docs/REVIEW_WARNING_CASES.md`：10-19 交 P-2 / P-3 旁证与平仓三行。不算有效性。不盘中加第三个统计参
- 调研映射见 `docs/REVIEW_RESEARCH_MAPPING.md`：Alpha Illusion 支撑 R1 / R2 / R3 与 paper 观察期；`factor_loop` 只收指标已方向校准且已算子化的候选。M-1 指向周检 [#54](https://github.com/tspike630/trade/pull/54)。不算有效性
