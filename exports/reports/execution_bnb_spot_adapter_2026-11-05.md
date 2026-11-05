# 执行 · Binance 现货适配器（2026-11-05）

杨令（≈14:34 HKT）：按实盘走币安 CEX，不走链上 DEX。本变更只把 `live_bnb_boll_fade` 的现货下单路径接上，**不打开实盘**。`LIVE_TRADING_DISABLED` 保持原样（本文件不删除、不改名）。未调用币安交易接口，未写入密钥。

## 接在哪

| 项 | 值 |
|---|---|
| purpose | 只此一条：`live_bnb_boll_fade` |
| 适配器 | `execution/executors/binance_spot.py`（`BinanceSpotAdapter`） |
| 网关 | `execution/gateway.py`：该 purpose 在本进程 allowlist 里时走适配器，不走 `PaperExecutor`，不调用 `LiveExecutor` |
| 符号 | 只用 `extras.binance_symbol`（`BASEUSDT`）。不用 `source_token_address`，也不把 `token_address` 当成另一套符号 |
| 订单 | 现货 `MARKET` / `LIMIT`，`BUY` / `SELL`。`LIMIT` 用 `GTC`。下单前看 exchangeInfo：`status=TRADING` 且计价 `USDT` |
| 密钥 | 环境变量 `BINANCE_API_KEY` / `BINANCE_API_SECRET`（没有则回退 `LIVE_EXCHANGE_API_KEY` / `LIVE_EXCHANGE_API_SECRET`）。不进仓库、不进回执 |

纸面 `race_tech_boll_fade` 与冻结的链上出口 `live_tech_boll_fade` 不走这个适配器。策略公式、阈值、出场、`local_score` 权重都没改。没有单笔名义软顶。

## 现在为什么不会下真单

live `POST /api/v3/order` 要同时满足下面每一条，缺一条就拒，并且不发请求：

1. 杀开关文件不存在（默认 `LIVE_TRADING_DISABLED`，或 `LIVE_KILL_SWITCH_PATH`）
2. `BINANCE_SPOT_LIVE_ENABLED=1`（显式开关，默认关。只设 `LIVE_TRADING_ENABLED` 不够）
3. 决策 `mode` 是 `live_stub`。`dry_run` 只回执、不提交
4. `extras.binance_symbol` 是合法的 `*USDT`，且 exchangeInfo 为 `TRADING`
5. 数量在信号 extras 里：市价买用 `quote_order_qty`（或 `quantity`），市价卖 / 限价用 `quantity`（限价还要 `price`）
6. 密钥在该进程的环境里

`dry_run` 在杀开关仍在时也可以被接受，但 `executed=0`，客户端不会被调用。

进程隔离（`execution/run.py`）：`--accept-purpose live_bnb_boll_fade` 必须单独占一个进程，账本不能是主 `execution_ledger.db`，不能 `--paper-enabled`，也不能指向 `paper_trading.db` 或 `paper_eq_tech_boll_fade.db`。

## 以后要开，只能人工做

代码不会代做这些步骤。③ 未绿、或下面任一步没做，适配器继续拒单。

1. 复核闸 ③ 绿。决策进程去掉 `--dry-run`，新行的 `mode` 才是 `live_stub`。信号上已经有 `extras.binance_symbol`。这一步仍是人确认，不是本 PR 的开关。
2. 确认要下的数量写在该行 extras（`quote_order_qty` 或 `quantity` / `price`）。没有数量，live 路径拒，不会按 `size_pct` 猜一笔金额。
3. 在运行进程的环境里设置密钥，不要写进仓库：
   - `BINANCE_API_KEY`
   - `BINANCE_API_SECRET`
4. 显式打开：`BINANCE_SPOT_LIVE_ENABLED=1`
5. 确认可以承受真单之后，再移走或改名 `LIVE_TRADING_DISABLED`。文件放回去，下一笔就会停。
6. 单独起执行进程（不要加 `--paper-enabled`）：

```bash
python -m execution.run --loop \
  --ledger execution_ledger_live_bnb_boll_fade.db \
  --accept-purpose live_bnb_boll_fade
```

不要 `--accept-purpose live_tech_boll_fade`。不要把纸面 peer 的 `--accept-purpose` 改成 `live_bnb_boll_fade`。不要和 `race_tech_boll_fade` 共用账本或 `paper_eq_tech_boll_fade.db`。

## 持仓保护（仍不打开实盘）

入场 `newOrder` 不附带 OCO / STOP_LOSS_LIMIT，回执仍是 `protective_order=not_supported_not_sent`。上面这个循环在处理新决策之后，会对本账本里已成交的多单调用 `protective_exit_reason`。入场回执没有 base `executedQty` 时先记成未确认，下一拍在闸打开的情况下用入场 `orderId` 再读一次；读到数量才允许卖。标记价触及绝对止损，或触及下单时记下的止盈（决策当时的 `boll_mid`），就按该符号的 LOT_SIZE / MARKET_LOT_SIZE 向下取整，市价卖出全部 base 数量。取整后低于最小数量或最小名义，不发单，仓位保持 open。卖单被拒或 HTTP 失败也不改成已平，下一拍用同一个 `bnbfade-x-<decision_log_id>` 再试，避免换号打出第二笔。干跑不记仓。杀开关文件还在，或 `BINANCE_SPOT_LIVE_ENABLED` 不是开，保护卖单同样不发。进程停着的时候，交易所上没有休息着的保护单。

有疑问：把 `LIVE_TRADING_DISABLED` 放回去，并取消 `BINANCE_SPOT_LIVE_ENABLED`。
