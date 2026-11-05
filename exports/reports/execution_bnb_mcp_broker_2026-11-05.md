# 执行门 · MCP 经纪（2026-11-05）

授权状态先说清楚：Trade 权限已经是连接器上选中的范围，不需要再走一轮授权。缺口是工具名单，不是密钥坏了。最近一次 HMAC 用的是当时提交的 key 和 secret，返回 401。权限补上之后这条原始路由可以再验证。本模块不把这把 key 标成作废，也不读环境变量里的密钥。

`original_api_key` 只表示原始适配器这条路由被承认。`original_api_key_healthy` 另算，这里仍是未验证。空握手时保护工具按失败关闭。父进程注入 `names()` 之后，可用性按那份名单重算。上次人工看到的 72 个工具只是空握手时的备注，不是写死的全局目录。

## 裸仓

父进程把 FET 现货买当成了「先买、OCO 随后」。要求是买和原生 OCO 一起成立。成交单 `orderId=3735698106`，`clientOrderId=bnbfade-147831`，FET 可用 140.1，挂单为空。仓是裸的。`HOLD_NAKED_NO_AUTO_SELL` 开着：不轮询，不卖。

原生 OCO 是文档里的 `POST /api/v3/orderList/oco`（operation `orderListOco`）。名单里出现 `spot.orderList.oco` 或 `spot.orderListOco` 时，队列把这一条标成父进程可调用（`release=true`，`invoke=false`）。本进程不调用 `invoke`，也不声称已经下出 OCO。Websocket 的 `orderList.place.oco` 不是 MCP 工具。

裸仓还在时，新的 USDT 买不放行，包括新的 `decision_log_id`。花费过的 client id 只挡住同一个 id。平仓之后同一符号可以再交易；花费过的 order 不把符号永久退役。这一版不另写平仓重置状态机。

报价 `quoteOrderQty` 向下取到 2 位小数。5% 只约束这次报价取整，构造时可以关掉。基础数量按给出的步长向下取整，不受这道 5% 约束。两种都不会由本模块发出。

父进程集成就是读 `parent_calls(evaluate(...))`。返回的行要由父进程自己去调 MCP。这里没有服务，没有后台任务。

`BinanceSpotAdapter` 仍是那条未验证的原始路由，本变更不改它，也不把它换成轮询卖出。
