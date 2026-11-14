# 执行保护改动待杨先审

状态：只设计。本轮不改执行器、配置、测试，不跑验收，不撤急停。

证据是现场包生产字节：spot `87ef5ed7`、protect `70aecd69`、gate `5c3022e0`。行号按这些文件。客户端实际发出的私有请求只有：

| 方法 | 路径 | 代码 |
| --- | --- | --- |
| POST / DELETE / GET | `/api/v3/order` | `new_order` / `cancel_order` / `get_order` |
| GET | `/api/v3/myTrades` | `my_trades` |
| POST | `/api/v3/orderList/oco` | `new_oco` |
| GET / DELETE | `/api/v3/orderList` | `get_order_list` / `cancel_order_list` |

没有 `openOrders`，没有 `cancelReplace`，也没有改数量的接口。下面不把未实现的接口写成能力。`DELETE /api/v3/order` 的回执被丢掉，随后的 `GET /api/v3/order` 才是状态（spot `_cancel_resting_entry` L5900–5968）。取消请求返回成功，不等于订单已终态。

## 已批准，本文件不再改

止盈公式 B，只用于尚无有效 OCO 的那一次首挂，带内成交同样使用：

`新止盈 = F × [1 + (P / E − 1) × m]`

`F` 是 `cummulativeQuoteQty / executedQty`（protect `fill_average_price` L581–589），单位是报价币/基础币。手续费不进入这个商。示例（不是成交）：`E=100 USDT`，`P=110 USDT`，`F=101 USDT`，`m=1.35` 得 `114.635 USDT`；`m=0.85` 得 `109.585 USDT`。

已查到的有效 OCO 不撤、不改、不另挂。同币只保留这一张。提交结果未知时不再 POST，也不市场卖。

## 买单回查：什么才算数

`GET /api/v3/order` 的 `status`。终态集合是 `FILLED`、`CANCELED`、`CANCELLED`、`EXPIRED`、`REJECTED`、`EXPIRED_IN_MATCH`（gate `_FINAL_ORDER_STATUS` L1207–1209）。`final_executed_qty` 只在这个集合里返回 `executedQty`；`NEW`、`PARTIALLY_FILLED`、`PENDING_NEW` 返回空（L1212–1228）。

净数量只来自该 `orderId` 的 `myTrades`：各笔 `qty` 之和必须等于 `executedQty`，每笔都有 `commission` 和 `commissionAsset`。买单净数量 = `executedQty − 基础币手续费`。`commissionAsset` 等于该交易对基础币才扣；报价币或其他币不扣（gate `account_order_base` L1231–1310）。缺手续费字段是 `fee_unverified`，按 0 处理没有依据。

费用示例（设计数字，不是成交）：

| 交易对 | executedQty | 手续费 | 净数量 |
| --- | --- | --- | --- |
| PEPEUSDT | 1000 PEPE | 1 PEPE | 999 PEPE |
| PEPEUSDT | 1000 PEPE | 0.01 USDT | 1000 PEPE |
| PEPEUSDT | 1000 PEPE | 0.0001 BNB | 1000 PEPE |
| BNBUSDT | 1 BNB | 0.001 BNB | 0.999 BNB |

BNB 只有在它就是基础币时才扣。代码里没有单独的 BNB 抵扣接口。均价示例：报价 34.10 USDT、成交 1000 PEPE，`F=0.03410 USDT/PEPE`，上表手续费都不改这个商。

剩余活动量沿用腿上的减法：`origQty − executedQty`（gate `_leg_remaining` L1131–1142）。两个字段缺一，活动量就是未知。这个差只在下面的终态里当成 0；非终态的差只说明当前回查还有余量，不作为 OCO 数量。

| 回查 status | 可以确认 | 收口 |
| --- | --- | --- |
| `NEW` | 不是终态。`executedQty` 不能当作已锁定成交。 | 未到入场 TTL：不撤、不挂。已到 TTL：`DELETE` 同一 `orderId`，再 `GET`。 |
| `PARTIALLY_FILLED` | 仍活动。当时的 `myTrades` 只是这一拍的净数量，下一笔会变。 | 当拍 `DELETE` 余量，再 `GET`。不挂 OCO，不卖。 |
| `PENDING_NEW` | 现有撤单分支不处理它（spot L6061 只对 `PARTIALLY_FILLED`，以及超时的 `NEW`/`PARTIALLY_FILLED`）。数量和活动量都未证实。 | 证据不足。冻结新买，下拍只 `GET`。 |
| `FILLED` | 终态。净数量与 `F` 都要上面的证明。活动量按 0。 | 证明齐且确定无 OCO：首挂一张。 |
| `CANCELED` / `CANCELLED` / `EXPIRED` / `EXPIRED_IN_MATCH` | 终态。`executedQty>0` 且证明齐：按有仓，活动量 0。`executedQty` 为 0 且成交页为空：无仓。 | 有仓则一张 OCO；无仓则关掉入场，不挂。 |
| `REJECTED` | 同样只认终态集合和成交证明。代码没有另写「拒绝一定是 0」。 | 证明出净数量就按有仓；证明出 0 就无仓；对不上就停在未核。 |
| 超时、空体、非对象、其他异常 | 状态、净数量、均价、活动量都未知。 | 见下文「证据不足」。 |

竞态只靠「同一 `orderId` 的 `DELETE` 可重复，结果只认之后的 `GET`」。`GET` 变为 `FILLED`：撤单没赶上，整笔成交是仓位。`GET` 变为取消类且 `executedQty` 更小：余量停了，净数量按该终态的成交。`GET` 仍是 `NEW` 或 `PARTIALLY_FILLED`：撤单还没生效，继续撤和查。未知期间新来的成交，要等后来的终态 `GET` 加上覆盖该 `executedQty` 的 `myTrades`，才进入数量。

尘埃不是平仓。低于最小数量或名义时，现有代码写成 `closed_dust`，并留下 `residual_below_min_is_not_a_complete_exit`（spot L7170–7189）。正的 `residual_qty` 仍挡新买（gate `row_unresolved_reason`）。

持久化在发请求之前：撤余量先写 `maker_cancel_unknown`（L5903–5908）；OCO 先写 `oco_unknown` 和 `oco_list_client_id`（L7202–7228）。重启后这两类只走已存的 `orderId` 或 `origClientOrderId` 做 `GET`。`maintain_held_position` 对 `oco_unknown` 先 `_reconcile_oco`（L5394–5398）。列表 id 存在，也不跳过这次回读。

## OCO 三类，不能合成一种未知

1. 确定无 OCO：账本没有列表客户号，或 `GET /api/v3/orderList` 返回本代码唯一承认的缺失 `-2018`（spot `order_list_absent` L244–257）。`-2011`、`-2013`、400 裸状态、429、超时都不是缺失。
2. 提交结果未知：已经写入客户号，而这次 `GET` 失败，或既不是 `-2018` 也不是下面的有效列表。维持 `oco_unknown`。不再 POST，不市场卖。下拍只回读。
3. 已查到有效 OCO：`listOrderStatus=EXECUTING`，`listStatusType` 为 `EXECUTING` 或 `EXEC_STARTED`，列表 id 和两条腿 id 与账本一致，两条腿各自的 `origQty−executedQty` 都不少于持仓净数量（gate `oco_readback_covers` L1145–1164）。此后只做回读和销记。不撤、不改价、不第二张。

`POST` 超时或无法解码的异常，现有代码留在 `oco_submit_timeout` / `oco_unknown`，不卖（L7245–7258）。解码后的 `BinanceRequestError` 现会被写成 `oco_rejected`（L7260–7268）。本设计不把这次 POST 错误本身当成「确定没挂上」。要再 `GET`：得到 `-2018` 才进入「确定无 OCO」；回读失败就留在第 2 类。

`AUTO_FLATTEN=1` 只走已经存在的失败路径，并且要同时满足：卖出数量来自已证明的净数量、这一张 flatten 客户号 `bnbfade-fl-` 回读为不存在、本次不是超时或空响应。尘埃和 `oco_partial` 现有代码本来就不进 flatten（L3343–3347）。flatten 提交再超时则停止，不第二张市场卖（L3527–3532）。

客户端没有活动单总查询。因此「没有别的卖单」只能核对这张已登记的 flatten 客户号和账本里的退出订单，核对不到账本外的手工卖单。这项在补上官方活动单查询之前闭合不了。

## 已确认成交能不能先挂一张，并盖住后续余量

不能，在已批准约束内做不到两头。OCO 数量在 `POST /api/v3/orderList/oco` 时写死，客户端不能改。余量后来再成交时，要盖住新增基础币，只有撤掉这张有效 OCO 再挂，或者再挂第二张。前者要另批「允许撤改已有有效 OCO」，后者要另批「允许同币第二张」。两样都不在本轮已批准范围内。

把 OCO 数量写成原始买单全量也没有依据：未成交部分还不是持仓，现有客户端不会用账户余额去放大卖单。

所以推荐路径是：余量 `GET` 进入终态、净数量和 `F` 都证明齐、并且属于「确定无 OCO」之后，首挂一张。在此之前不挂。这一段不是批准的持仓方式，也不是成功状态。每拍冻结新买（现有 `maker_cancel_unknown` 已挡）、重复 `DELETE`+`GET` 或只 `GET`、发告警。等待上限本设计不填秒数，由杨另批；没有这个上限，就不能把该状态当成可上线行为。超过上限、进程重启后仍是同一事件、或官方持续断联，都维持阻断上线。人工接管只是事件处理，不把账本写成 `oco_working`。断联期间没有零风险保证。

现有注释写「部分成交当拍就撤，不把无保护等到 TTL」（spot L6058–6060）。实现在回查未知时停在 `maker_cancel_unknown` 且不挂 OCO（L5994）。本设计沿用「先撤再认终态」，同时把这段未收口定为上线阻断，而不是一条可接受规则。

## 请选一张

| 方案 | 条件 | 剩余风险 | 是否突破已批准约束 |
| --- | --- | --- | --- |
| 推荐。买单终态且 `myTrades`、均价证明齐，又确定无 OCO 时，首挂一张。止盈用公式 B。止损锚用 `F`，距离用信号原百分比，下限为现有 `0.005`，价格档 `ROUND_DOWN`；对齐后仍不低于锚则再减 2 个 tick（protect L541–549 的距离和档位，锚从 `min(成交,标记)` 改为 `F`）。活动中或证据不足：冻结、回读、告警，等待上限留空，并阻断上线。 | 不新增接口。有效 OCO 只回读销记。市场卖只在确定拒绝且 flatten 客户号确认不存在时走现有路径。 | 终态 `GET` 到来前，已成交部分没有 OCO。官方断联时这段没有自动保护。flatten 仍看不见账本外的卖单。止损向下取整会略远于公式；原距离小于 50 bps 时被现有下限推远。标记价低于 `F` 时，按 `F` 算出的止损可能被交易所拒绝。 | 不突破。拒绝若因价格档，与网络超时分开：前者在 `-2018` 之后才允许现有 flatten；后者留在未知。 |
| 其他可行。`PARTIALLY_FILLED` 的当时净数量先挂一张；后来的新增成交要先撤这张，再按新净数量重挂。 | 撤列表之后的 `GET` 必须证明旧列表已结束，才能 POST 新的一张。撤列表未知时不能重挂。 | 撤和重挂之间没有保护。 | 突破「不撤改已有有效 OCO」。要另批。 |
| 其他可行。新增成交另挂第二张，只卖新增净数量。 | 两张列表都要能被现有回读认领。客户端今天按一个 `oco_list_client_id` 记账。 | 两张卖单叠加可能超过持仓。交易所是否拒绝同币第二张，本客户端没有证据。 | 突破「同币单笔单 OCO」。要另批。 |
| 止损备选。距离和 50 bps、向下档位都与推荐相同，锚用 `min(F, 标记价)`。 | 沿用现有 `reanchor_oco_prices_to_fill` 的锚。 | 锚不是单独的真实均价。 | 不突破公式 B，也不突破 OCO 约束。 |

推荐第一行。没有等待上限、并且活动单总查询仍缺失时，第一行也不能上线。上线阻断是结论，不是把无保护等待批成规则。

现有止损距离：`g=(E−信号止损)/E`；`E` 不大于止损或止损非正时，`g` 用 `MIN_REANCHOR_STOP_GAP`。然后 `g=max(g, 0.005)`。`0.005` 是 protect L43 的源码常数。首挂止盈不用 L553–562 的旧止盈重算，只用公式 B。

## 最小改动边界（批准后才写）

只动保护首挂和入场终态分类：终态证明齐才按 B 和所选止损锚 POST 一张；`oco_unknown` 与有效列表继续官方回读和销记；超时不卖；尘埃保持「不是完成退出」。不改决策路由，不改环境里的秒数，不修另外 49 失败 / 11 错误，不调用 `_cancel_working_oco` 去追新增成交。追踪棘轮保持关闭，避免那条现有撤列表路径。

批准后要跑、本轮没跑的验收：

1. `PARTIALLY_FILLED` 后 `GET` 仍活动：零张 OCO、零笔卖，新买被挡，账本不是成功保护。
2. 随后 `GET` 为 `CANCELED`，`executedQty` 与 `myTrades` 一致：只 POST 一张，数量是扣完基础币手续费的净数量，止盈等于公式 B。
3. `DELETE` 回执写成 `FILLED` 或 `CANCELED`、随后 `GET` 相反：以 `GET` 为准。
4. 报价币或 BNB（非基础币）手续费不减数量；基础币手续费要减。缺手续费不挂。
5. `closed_dust` 仍带未完成退出标记，并继续挡新买。
6. 已有列表 id 的下一拍仍 `GET /api/v3/orderList`。有效则不撤不改；回读超时留未知，不卖、不第二张。
7. `POST` 超时与重启：用已存客户号回读。`-2018` 才允许再 POST 同一客户号。不新造客户号。
8. 入场 TTL 与 OCO 回读 TTL 分开断言，都不放行另一只币。
9. 等待上限未配置时，活动余量或证据不足路径不能被标成可上线。
