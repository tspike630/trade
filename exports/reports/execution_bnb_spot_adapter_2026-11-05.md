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

入场 `newOrder` 不附带 OCO，回执仍是 `protective_order=not_supported_not_sent`。两种覆盖不要写成同一件事：

| `protection` | 意思 |
|---|---|
| `exchange_oco` | 交易所上有一张执行中的 OCO。进程停了，这张单还在。循环不再市价卖。 |
| `oco_pending` | 已经打算下 OCO，或 POST 超时，还没确认列表。这个状态不是覆盖。先按 `origClientOrderId=<listClientOrderId>` 查 `GET /api/v3/orderList`，不在同一拍市价再卖。 |
| `loop_fallback` | OCO 明确被拒，或列表结束且没有成交。循环才用绝对止损 / 决策当时的止盈做市价卖。 |
| `dust_abandon` | 状态 `closed_dust`。剩余数量卖不掉，留下 `residual_qty` / `residual_quote`。不是 `exit_submitted`，也不再每拍重试。 |

OCO 请求是 `POST /api/v3/orderList/oco`：`side=SELL`，`quantity` 为成交 base 按 `LOT_SIZE` 向下取整，`aboveType=LIMIT_MAKER`，`abovePrice` 为决策止盈，`belowType=STOP_LOSS_LIMIT`，`belowStopPrice` 为绝对止损，`belowPrice` 再低一个 tick，`belowTimeInForce=GTC`。数量不按未出现在成交回报里的手续费去改；只有 `fills` 写明佣金资产就是这个 base 时才从 `executedQty` 里减掉。

`LIVE_TRADING_DISABLED` 挡住新的买入，也挡住决策流里自己送进来的卖出。账本跟踪的保护卖出（OCO 或 fallback 市价）在杀开关打开时仍可发。`BINANCE_SPOT_BLOCK_SELL_ON_KILL=1` 连这些保护卖出也停。`BINANCE_SPOT_LIVE_ENABLED` 默认关，关着时不发单、不签请求。干跑不发 HTTP。

计划买入名义低于 20 USDT 拒绝。这是 viability 地板，不是每日软顶，也不改订单数量。策略只给 `size_pct` 时，名义是 `size_pct * BINANCE_SPOT_BOOK_USDT`（操作者设的账面，不是钱包余额）。没设账面就 `missing_book_usdt`，不猜一笔数量。显式 `quote_order_qty` / `quantity` 优先。再对照符号的 `LOT_SIZE` / `minNotional`，并用默认 10 bps（`BINANCE_SPOT_TAKER_FEE_BPS`）估计扣费后的 base 还够不够最小下单量。估不出来就不改数量。

生产看门狗是 `scripts/live_bnb_execution_watchdog.sh`。它只拉起这一条臂的 `execution.run --loop`，单实例，子进程退出后最多 3 秒再拉。杀开关还在、`BINANCE_SPOT_LIVE_ENABLED` 不是开、没有密钥、或没有账面，它拒绝启动，并且不改这些开关、不删文件、不把密钥写进命令行。`scripts/live_bnb_watchdog_dry.sh` 只会做同一套预检，不能进入 supervise。

取整灰尘和整笔都低于步长或最小名义的，返回 `dust_untradable`，归档为 `closed_dust` / `exit_reason=dust_abandon`。不在这里做 BNB 小额转换。本仓库不装 systemd，也不做进程看门狗。

有疑问：把 `LIVE_TRADING_DISABLED` 放回去，并取消 `BINANCE_SPOT_LIVE_ENABLED`。
