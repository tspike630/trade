# 执行保护改动待杨先审

状态：只设计，不实现。本文件不修改 `binance_spot.py`、`binance_spot_protect.py`、`binance_spot_gate.py`，也不改生产 `engine.py`。

生产字节仍是 spot `87ef5ed7`、protect `70aecd69`、gate `5c3022e0`。下面的行号来自现场包里的这些文件，不是 main 的较短副本。

## 1. 再挂靠不是「成交均价首挂一次」

`reanchor_oco_prices_to_fill`（`binance_spot_protect.py` L509–578）：

- 只在原止盈止损带包不住价格时才改价（调用点 `binance_spot.py` L3790、L5843）。带内成交仍用信号原价。
- 锚点是 `min(成交, 标记价)` 和 `max(成交, 标记价)`，不是单独的成交均价。
- 止损距离沿用信号入场价到止损价的比例，下限 50 bps（`MIN_REANCHOR_STOP_GAP`）。
- 止盈若已在较高锚点之上，只做价格档对齐，不乘状态倍率。若止盈落到锚点之下，才按信号入场到止盈的比例重算，下限同样 50 bps。
- 已看到的调用在首挂保护之前。本轮没有证明持仓途中不会再次进入这段。也没有证明失败重试不会撤掉已经挂出的 OCO。

因此草案把 `BINANCE_SPOT_REANCHOR_OCO_ON_FILL` 留在 0。定案要的 1 记在 `REGIME_V2_REANCHOR_RULING`，状态是 `blocked_behavior_mismatch`。

若要改，最小范围是：仅当还没有 `orderListId` 时，用 `fill_average_price`（同文件 L581–589，`cummulativeQuoteQty / executedQty`）做一次挂单；已有官方 OCO 的 tick 直接返回；失败不撤已挂列表。这要单独批准后再写。

## 2. 静态止盈倍率还不能写进订单

决策侧现在只把倍率写成 `regime_tp_multiplier`：状态 1/2/3/4 在契约打开时是 1.00 / 1.35 / 1.25 / 0.85。`take_profit_price` 仍是入场模块的原值（fade 为布林中轨，classic 为入场价 +10%）。

成交均价上的距离没有唯一现成定义。下面三个式子都说得通，未选，也不会下单。

设信号参考价 `E`，计划止盈 `P`，成交均价 `F`，倍率 `m`。

| 式 | 公式 | 算例 `E=100`，`P=110`，`F=101`，`m=1.35` | 算例 `m=0.85` |
| --- | --- | --- | --- |
| A | `F + (P - E) * m` | `101 + 10 * 1.35 = 114.5` | `101 + 10 * 0.85 = 109.5` |
| B | `F * (1 + (P / E - 1) * m)` | `101 * (1 + 0.10 * 1.35) = 114.635` | `101 * (1 + 0.10 * 0.85) = 109.585` |
| C | `E + (P - E) * m`，不用 `F` | `113.5` | `108.5` |

A 与 B 都以成交均价为原点，差在绝对距离还是百分比。C 在决策时就能算，但不符合「相对真实成交均价」。部分成交时 `F` 会变，要先规定用累计均价还是最后一笔。

## 3. 部分成交、超时和幂等

需要批准后才写的测试，不是本轮行为：

- 部分成交：OCO 数量用已证明的净成交，不把未成交的限价剩余算进保护。重复 tick 不得把剩余再开成第二张 OCO。
- 超时：现有入场 TTL 默认 180 秒（`DEFAULT_ENTRY_TTL_SEC`）和 OCO 回读 TTL 默认 180 秒（`binance_spot_gate.py` 的 `DEFAULT_OCO_READBACK_TTL_SEC`）是两个不同的钟。回读通过不等于已经允许下一只币。
- 幂等：同一 `decision_id` 的 client order id 应固定。重试如果先撤再挂，就会违反「不撤已有 OCO」。
- 同币：本仓库 `buy_exposure_block` 在多币旗标打开时只挡同一 purpose、同一 symbol 的健康持仓。另一 purpose 的行不在这个函数里读。跨 purpose 互斥若只存在于生产 gate `5c3022e0`，不能用 main 的 333 项通过来代替。本轮不改这个函数。

## 4. 建议的批准后验收

1. 带内成交不改价。
2. 带外成交只按选定的一种均价公式改一次，并留下改前价格。
3. 第二轮 tick 看到已有官方 order list 时不撤、不改。
4. 部分成交的剩余不成第二张保护单。
5. `TRAILING_RATCHET` 与 `MAX_HOLD_SEC` 仍为 0，`AUTO_FLATTEN_ON_OCO_FAIL` 仍为 1。
6. 对比基线是生产字节，不是 main 的执行测试全绿。
