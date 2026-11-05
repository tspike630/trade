# 执行门 · MCP 经纪（2026-11-05）

父进程把一笔 FET 现货买当成了「先买、OCO 随后」。用户要的是买和原生 OCO 一起成立。连接器里没有 OCO 工具，这笔买是裸仓。这是范围被标错，不是可以事后补一张保护单。

## 已核对的事实

Trade 重新授权之后，官方 Binance MCP 仍是 72 个工具，没有 `orderList`，没有 OCO。`canTrade=true` 不会把缺的工具补出来。账户读数是 FET 可用 140.1，挂单为空。已成交单 `orderId=3735698106`，`clientOrderId=bnbfade-147831`，数量 140.1，报价成交 21.93966。这张单不在这里重下。

原生 OCO 只有文档里的 `POST /api/v3/orderList/oco`（operation `orderListOco`）。旧的 `POST /api/v3/order/oco` 不使用。Websocket 的 `orderList.place.oco` 不是 MCP 工具，父进程不得调用。没有握手可以绕过缺工具。

`original_api_key` 的可用性是 false。环境里的旧密钥未验证，本模块不读它，也不要新密钥。云端执行子进程调不到 MCP。本模块不发起后台任务，也完不成一笔真实 OCO。

## 门

`execution/executors/binance_mcp_broker.py` 的 `LiveMCPBroker.evaluate` 只返回拦住的父队列。`release` 和 `invoke` 都是 false。

| 请求 | 结果 |
|---|---|
| 新买 | 冻结。保护工具不在目录里，`resume_signals` 为 false |
| 卖 | 不卖。裸仓持有到原生 OCO 出现为止。不轮询 |
| OCO | 不承诺、不发送。队列里的工具名是空的，只记下文档路径 |

账本由调用方传入，本模块不扫决策日志。两层都能挡住新的 `decision_log_id`（游标停在 147840 挡不住后续新号）：

1. 同一 purpose `live_bnb_boll_fade` 上，符号已有 `held_naked` 仓
2. 该符号已有花费过的 `order_id` / `client_order_id`

报价 `quoteOrderQty` 向下取到 2 位小数，数量按调用方给出的步长向下取整，削减超过 5% 则记 `qty_reduce_exceeds_cap`。两种取整都不发单。没有步长就不套一个固定步长。

原来的 `BinanceSpotAdapter` 仍留在仓库里，不作为这条裸仓的通道，也不在这里改成轮询卖出。
