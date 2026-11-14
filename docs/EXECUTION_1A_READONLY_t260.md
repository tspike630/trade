# 1A 只读核查（t260）

本文件是研究说明，不是新的设计契约，也不改 `docs/EXECUTION_PROTECTION_REVIEW_t250.md`。没有改执行器、配置或测试，没有跑验收，没有下单。

止盈公式 B 维持已批。止损锚仍待审，这里不选。

## 1. 两个版本必须分开

### 封板前缀 `aca99a95`：字节不在手

查过这些地方，都没有这份 `binance_spot.py`：

- 本克隆的 git 对象：`aca99a95` 不是 commit，也不是 blob。
- 两个现场包、`/tmp/prod-combo-verify`、本机检索。
- GitHub 代码、议题、PR 检索 `repo:tspike630/trade aca99a95`：0 条。

只在文字里出现过：现场包 `PACK_README.md` 第 8 条写「不可把帖中旧 PR218 隔离树或 aca99a95 PASS 当现时事实」；`AGENTS.md` 写生产执行器不退回这个前缀。

因此缺：完整哈希、文件字节、函数名、行号、`NEW` / `PARTIALLY_FILLED` 的摘录。不能用这个前缀描述流转，也不能把它当成 git commit 去 `git show`。

### 现场包才是下面所有行号的出处

| 文件 | 完整 sha256 | 字节 |
| --- | --- | --- |
| `execution/executors/binance_spot.py` | `87ef5ed7ed6566bab4f6b7ff0abd4fd33a96811f03cf8f7477a5eb03bede2f00` | 310144 |
| `execution/executors/binance_spot_protect.py` | `70aecd695825a7b71216b336d2a71c3c1b3bcb7302370cae8dd5ac44405c1ad9` | 46540 |
| `execution/executors/binance_spot_gate.py` | `5c3022e0553b6a74452577418098ea7a0dfc78870a745fd0b99a0b0411932d28` | 52312 |

包说明写明 spot/protect 是 2026-11-14 09:47 HKT 的 PR220 安装，含 maxhold r2 与 PR220 回读。这不是 `aca99a95`。下面的行号只属于这三份现场字节。

## 2. 现场入场现在怎么走

维护循环没有用户数据流的成交通知。`execution/run.py` 的 `DEFAULT_POLL_SEC` 读 `EXECUTION_POLL_SEC`，缺省字面量是 `5`（L63）。现货用途再被夹到不超过 `MAX_OCO_MAINTENANCE_REFRESH_SEC = 60`（gate L53，run L67–76）。部分成交要等下一拍 `GET /api/v3/order` 才看得到。

`reconcile_resting_entry`（spot L5988 起，分支在 L6040–6085）：

1. `GET /api/v3/order`。超时或非对象：本拍不撤、不挂，错误原样返回。
2. `PARTIALLY_FILLED`，或者 `NEW` / `PARTIALLY_FILLED` 且已超过入场 TTL：立刻 `_cancel_resting_entry`。注释写明不等 TTL 把已成交部分晾着（L6058–6060）。
3. 状态已在 `FILLED`、`CANCELED`、`CANCELLED`、`EXPIRED`、`REJECTED`、`EXPIRED_IN_MATCH`：`_adopt_terminal_entry`。
4. 未到期的 `NEW`：返回 `entry_resting`，不撤。

入场 TTL 的键只有 `BINANCE_SPOT_ENTRY_TTL_SEC`（protect L1050–1076）。缺省 `180`，解析失败也是 `180`，下限 `max(5, 值)`。本核查不另定一个更短的秒数。零成交才等这个钟；部分成交不等。

`_cancel_resting_entry`（L5900–5977）：

- 先把账本写成 `maker_cancel_unknown` / `maker_cancel_pending`（L5903–5908），然后才 `DELETE`。
- `DELETE` 抛错，包括 `-2011`：只写 `maker_cancel_unknown` 并返回（L5924–5936）。这一拍不再 `GET`。
- `DELETE` 没抛错：回执丢掉，接着 `GET`。`GET` 失败，或状态仍是 `NEW` / `PARTIALLY_FILLED` / `PENDING_NEW`：继续未知，不发 OCO（L5937–5968）。
- `GET` 进入上面的终态集合，并且 `myTrades` 与 `executedQty` 一致：`_adopt_terminal_entry`，再进 `maintain_held_position` 去挂保护。

`-2011` 的代码注释写的是 cancel 的 “Unknown order sent”（spot L244–248）。`single_order_absent` 只在 `GET /api/v3/order` 收到 `-2011` 或 `-2013` 时为真（L260–265）。`order_list_absent` 只认列表的 `-2018`（L251–257）。撤单抛出的 `-2011` 不走这两个函数，所以既不是「没成交」，也不是「撤单已成功」。

下一拍因为状态已是 `maker_cancel_unknown`，`maintain_held_position` 会再进 `reconcile_resting_entry`（L5334–5335），重新 `GET`。那时若是 `FILLED` 且成交明细一致，才进入唯一一张 OCO。卡点就是：收到 `-2011` 的那一拍故意不再查，要等下一次轮询。

净数量仍是 `executedQty` 减去基础币手续费；报价币或非基础币的 BNB 不扣（gate `account_order_base` L1231–1310）。两次请求对不上则 `fee_unverified`，不挂、不卖（spot `_adopt_terminal_entry` L5731–5741）。重启靠已经写入的 `entry_order_id` 和稍后的 `oco_list_client_id` 再 `GET`，不靠内存。

### 配置键不是同一个开关

| 键 | 谁读 | 作用 |
| --- | --- | --- |
| `LIVE_BNB_POST_ONLY_LIMIT` | 入场区 `live_bnb_entry_zone.py` `post_only_limit_enabled` L112–114 | 只把中性档从 `LIMIT` 改成 `LIMIT_MAKER`。大单档仍写 `MARKET`（L904–907）。 |
| `BINANCE_SPOT_POST_ONLY_LIMIT` | protect `post_only_limit_enabled` L1048–1058；执行器 `_post_only_limit_enabled` L1756–1759 | 已是 `LIMIT` 的买单可再改成 `LIMIT_MAKER`（spot L4662–4667）。不读 `LIVE_BNB_POST_ONLY_LIMIT`。 |

执行器 `_apply_recommended_order_type`（L404–428）：建议类型只有 `LIMIT` 或 `LIMIT_MAKER` 才会覆盖。信号上已经是显式 `LIMIT` 时保留，不改成建议的 `LIMIT_MAKER`。空建议或 `NONE` 留下信号自己的类型；入场区大单的默认是 `MARKET`。

状态 1 契约把建议写成 `LIMIT_MAKER`（本分支 `regime_strategy_router.py` `_apply_v2_contract` L834–838）。那是决策建议。现场执行器仍按上一句决定盖不盖。状态 4 契约现在关闭买点，类型 `NONE`（L852–856），不是已经在发现场深跌 Maker 单。

## 3. 方向 1A：收口后仍是一张 OCO

1A 不要求放宽「同币一张 OCO」，也不要求中途撤改已有保护。顺序是：Maker 买单留在簿上；第一次看到部分成交，或零成交的入场 TTL 到了，撤剩余；官方 `GET` 进入终态且 `myTrades` 与累计成交一致；这时确定还没有有效 OCO，才首挂一张。公式 B 用在这一张的止盈。已有有效 OCO 仍不撤不改。

举例只说明闭环，不是成交记录。买单 10，已成交 4，余 6。撤单期间余 6 全部成交：随后的 `GET` 应为 `FILLED`，`executedQty` 为 10。明细一致则按净数量挂一张，不第二张。若 `DELETE` 返回 `-2011`，本拍停在未知；下一拍 `GET` 若为 `FILLED` 且明细为 10，同样只挂一张。`-2011` 本身不触发 OCO，也不触发卖出。

确认期间的暴露是诚实的，不是零风险：从部分成交被这一拍看见，到终态 `GET` 加上 OCO 被回读确认，这段没有保护。`-2011` 还要再等至少一轮轮询。轮询缺省字面量是 5 秒、上限 60 秒，这是现有循环，不是新批的短 TTL。官方断联时这一段没有自动保护。

### 已能复用

- 部分成交当拍撤余量，零成交才等入场 TTL。
- 撤单前先落 `maker_cancel_unknown`。
- 终态才用 `myTrades` 定净数量和 `fill_average_price`。
- 无列表时 `maintain_held_position` 已会挂一张；有 `oco_unknown` 时先回读，`-2018` 才允许用同一客户号再提交。
- 有效列表只回读销记。超时不市场卖。

### 真正缺口（仍不在本轮写码）

- `-2011` 当拍不跟随 `GET`。闭环要靠下一拍，不是同一函数里完成。
- 首挂止盈仍不是公式 B。止损锚未决，不能顺手改。
- 没有成交通知，只有轮询。
- `closed_dust` 不能当成这段的成功保护。

### 碎单

`size_exit_quantity`（spot L858–890）优先 `MARKET_LOT_SIZE`，否则 `LOT_SIZE`。名义地板 `_min_notional`（L845–855）：先看 `NOTIONAL`，仅当 `applyMinToMarket is False` 才跳过；否则用其 `minNotional`。没有 `NOTIONAL` 才看 `MIN_NOTIONAL`，这段不读 `applyToMarket`。低于台阶、`minQty` 或名义，返回 `exit_qty_below_min` 或 `exit_below_min_notional`，不发单。

`auto_flatten` 在这个错误上走 `dust_archive_fields`，账本变成 `closed_dust` / `dust_abandon`，`submitted=False`（spot L3468–3486）。它不卖出。函数开头写明 `closed_dust` 和 `oco_partial` 不再进入 flatten（L3343–3349）。OCO 腿成交后的残量若被判为尘埃，同样写成 `closed_dust`，`quantity` 和 `unprotected_qty` 留着残量，`manual_action` 为 `residual_below_min_is_not_a_complete_exit`（L6681–6696）。这是账本标记，交易所上的币还在。

`partial_dust_residual` 把任何 `closed_dust` 视为未终了（gate L505–512）。`_TERMINAL_POSITION` 只有 `closed`、`oco_filled`、`closed_canceled`（L103），不含尘埃。同一 purpose 上只要有一行 `closed_dust`，`buy_exposure_block` 直接返回 `unprotected_position_blocks_buy`（L828–829），下一只币也被挡住。

原生 OCO 的下腿是 `STOP_LOSS_LIMIT` 且 `belowTimeInForce=GTC`（protect `build_spot_oco` L651–658）。官方把 `STOP_LOSS_LIMIT` 列为 algo 类型，所以市价卖并不自动免掉最小名义；代码对退出数量仍走上面的台阶和名义检查。

本检出里已有、本轮没跑的断言：`execution/tests/test_binance_protective_exit.py` 在尘埃路径上期望 `closed_dust`、`dust_abandon`、卖单列表为空（约 L430–435），以及 `exit_below_min_notional`（L511–513）。`test_binance_s6_residuals.py` 约 L610–615 同样期望尘埃标记。`test_execution_safety_persistence.py` 约 L659 期望 `maker_cancel_unknown`。这些测的是本检出，不是刚刚对现场字节 `87ef5ed7` 重跑的结果。现场包的 `execution/tests` 里没有 `closed_dust` 这个字符串。

## 4. 1B 与 1C 只作比较，不是本次选项

官方出处：

- 过滤器：<https://github.com/binance/binance-spot-api-docs/blob/master/filters.md>
- 下单与 OCO：<https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md> 中 “New order” 与 “New Order list - OCO”
- 状态枚举：<https://github.com/binance/binance-spot-api-docs/blob/master/enums.md>

文档写的计数，不是账户额度：

- `MAX_NUM_ORDERS`：某符号上账户的最大挂单数，algo 与普通单都计入。示例 JSON 写 `maxNumOrders: 25`，这是文档示例。
- `MAX_NUM_ALGO_ORDERS`：algo 指 `STOP_LOSS`、`STOP_LOSS_LIMIT`、`TAKE_PROFIT`、`TAKE_PROFIT_LIMIT`。示例写 `maxNumAlgoOrders: 5`，同样只是示例。`LIMIT_MAKER` 不在这份名单里。
- `MAX_NUM_ORDER_LISTS`：某符号上的订单列表数。文档说 OTOCO 算一个列表。示例 `maxNumOrderLists: 20`。
- `EXCHANGE_MAX_NUM_ORDERS` 示例 `1000`；`EXCHANGE_MAX_NUM_ALGO_ORDERS` 示例 `200`。OCO 那一节写的过滤器名字是 `EXCHANGE_MAX_ORDERS`（少了 `NUM`），与过滤器页的 `EXCHANGE_MAX_NUM_ORDERS` 不是同一拼写。两边都照录，不合并成一个数。
- OCO 端点写：OCO 向 `EXCHANGE_MAX_ORDERS` 和 `MAX_NUM_ORDERS` 加 **2** 张订单；Unfilled Order Count 为 2。没有一句写「每条腿各算一张 algo」或「整张 OCO 算 1 张 algo」。本封装的下腿类型属于 algo 名单，上腿 `LIMIT_MAKER` 不属于。具体加几张 algo，文档没有单独给 OCO 的数字。

本机请求 `https://api.binance.com/api/v3/exchangeInfo` 返回 HTTP 451。BTCUSDT、PEPEUSDT、SOLUSDT 的现场 `maxNumOrders` / `maxNumAlgoOrders` / `minNotional` 都未知，不填。

1B 分片多 OCO：每增一段成交再挂一张。账本一行只有一个 `oco_order_list_id`、一个 `oco_above_order_id`、一个 `oco_below_order_id`，客户号来自同一个 `decision_log_id`（protect `oco_list_client_id` L136–138，前缀 `bnbfade-ol-`）。第二张没有落点。增量若低于该符号的最小名义，会变成又一块尘埃，而不是第二张保护。手续费仍只在基础币上减数量。后到的成交在新列表提交未知时没有数量锁。这要另批「同币多张」契约，不是 1A。

1C 撤掉已有有效 OCO 再合并：现场棘轮路径 `_maybe_ratchet_working_oco` 会 `DELETE` 列表，但只有追踪打开才走到（spot L5437–5442）。已定 `TRAILING_RATCHET=0`，这条不作为 1A。撤列表若未确认，现有代码停在 `oco_cancel_unconfirmed`，不发新列表（L7100–7108）。撤成之后、新列表回读之前是裸窗。超时不能卖。若为了重挂去换一个新的 `ratchet_seq` 客户号，而旧列表其实还在，就会两张并存。1C 打破「不撤改已有有效保护」。

### 180 秒回读门与跨 purpose

回读新鲜度键是 `LIVE_BNB_OCO_READBACK_TTL_SEC`，其次 `BINANCE_SPOT_OCO_READBACK_TTL_SEC`，两个都不存在时用 `180`（gate L50–52、L907–920）。它只进决策侧 `decision_unresolved_halt` → `persisted_oco_readback_reason`：缓存证据比 TTL 旧则 `oco_readback_stale`（L1026–1030、L1054–1086）。执行侧注释写明继续用当场 REST，不信任缓存列（L1063–1064）。这不是「等 180 秒再开下一只」。

跨 purpose：现场 `gateway.py` 买前把本账本其他 purpose 的行和 `_required_peer_books` 的对端账本放进 `peer_buy_blocks`（L1391–1407），再调用 `cross_arm_symbol_block`（spot L2320–2335）。对端账本 uid 或注册表对不上时返回 `ledger_account_unbound` 等错误并挡住买（gateway L82–126），不是静默忽略。同一 purpose 的同币由 `buy_exposure_block` 挡。这是现场字节里的调用，不用本分支缺不缺来推断生产。

| | 1A | 1B | 1C |
| --- | --- | --- | --- |
| 已有有效 OCO | 不撤不改 | 不撤第一张，但要第二张 | 必须先撤 |
| 同币列表数 | 收口后一张 | 多张 | 成功时仍想是一张，未知时可能两张 |
| 确认窗 | 有，直到终态 GET 和 OCO 回读 | 每一段增量都有自己的未知窗 | 撤与重挂之间整段裸着 |
| 尘埃 | 不卖，标记并挡同 purpose 新买 | 每段都可能再产生尘埃 | 与 1A 相同的最小名义 |
| 是否本次可执行 | 建议的后续方向；本轮仍不写码 | 否，另批契约 | 否，另批契约 |

## 5. 建议与未验证

建议保持方向 1、否决全局 IOC/FOK，后续若获准写码，只做 1A 的缺口：`-2011` 当拍是否补一次 `GET` 仍由杨定，但不新造秒数；首挂止盈用已批的 B；不把 `closed_dust` 写成平仓；不撤已有有效 OCO。最小范围停在现场 spot 的撤单回读和首挂价格。不改决策路由，不改入场类型。

未验证：`aca99a95` 全文；本机取不到的符号过滤器数值；`LIMIT_MAKER` 与 IOC/FOK 的交易所组合；OCO 到底向 `MAX_NUM_ALGO_ORDERS` 加 1 还是加别的数；`GET` 与 `myTrades` 两次快照之间又成交时的交易所时序。查询接口即使补上，也只多一次观测，不是原子保证。

批准后才跑、本轮没跑的验收：

1. `PARTIALLY_FILLED` 当拍只发一次 `DELETE`，不发 OCO。
2. `DELETE` 得到 `-2011` 后，同一拍零次 `GET`、零张 OCO、零笔卖；下一拍 `GET=FILLED` 且明细数量为 10 时只挂一张。
3. 撤单期间余量全部成交，`GET` 的 `executedQty` 与 `myTrades` 一致，净数量扣基础币手续费，止盈等于公式 B。
4. 报价币手续费不减数量；缺手续费不挂。
5. 低于 `LOT_SIZE` 或名义地板：零笔卖，状态 `closed_dust`，同 purpose 下一只币被挡。
6. 已有有效 `orderListId` 的下一拍仍回读，不撤、不第二张。
7. 入场 TTL 与回读 TTL 分开断言，都不放行另一只币。
