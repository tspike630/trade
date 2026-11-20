# 三 PR 组合三个阻断的限定修复

基线组合 `504880ffacf7cfc10bfc0ae99f9d8301408df722`，树 `2c61cce58bacb764998b0701d8d8a8f8dec20ff7`。来源是 PR223 `78e7064f`、PR221 `6508676c`、PR222 `170f2ef`。本修复提交在这个组合之上。

本轮开始时 PR221 远端已是 `6e1c9d0b0b83d4b989ab047272f8ac53fe7707b5`（`fix(lseg): separate the calendar path from the cross-asset file`）。那是 t367，没有并入本分支。PR222、PR223 远端没有新提交。原分支没有被推送。

## 1. 保证金调用链

`decision_engine/core/execution_account_route.py` 只保留信号里已经写明的 `binance_spot` 或 `binance_cross_margin`。没有这个字段就不是保证金。

`execution/account_dispatch.py` 把请求交给现货回调，或交给 `execution.margin_cross.entry.open_offline_routed`。后者调用真实的 `CrossMarginTrack.open_position`。未配置域名时不创建保证金轨道。`submit_spot` 和保证金域名同时出现则两边都不调用。`spot_book_full` 不是布尔就拒绝，不自选 8 策略或 3 空头。现货已满时只允许三个空头 id；未满时离线夹具 id 或八个已存在策略名可以通过拓扑检查。账户 uid 不一致不进轨道。没有 `CROSS_MARGIN_PARAMETERS_APPROVED=1` 时，`build_approved_position_plan` 返回 `parameters_not_approved`，不调用轨道。没有生成审批 JSON 或 HMAC。

`decision_engine/core/engine.py` 未改，安全钉仍是原哈希。生产 tick 不会因为这次修改默认走杠杆。

## 2. 现货执行器字节钉

合并后的 `execution/executors/binance_spot.py` 仍是 `ff8124f906841d2d28238af662a6dc292c82dfeb5a410720a282eb97db97fa76`。本轮没有改这个文件。它是 PR223 保护逻辑加上 PR221 的三行 `order_post_attempted`。

行为测试确认：`execute` 里先把标志设为假，再在 `new_order` 之前设为真；缺 `side` 的认领仍是 `oco_readback_side_mismatch`；已有 `oco_rejected:` 的错误不会被 `-2018` 读回覆盖。钉上增加这个已核对哈希，并保留生产包哈希 `87ef5ed7…` 和两边的旧哈希。别的第三份文件仍然失败。

## 3. `-1013` 与 Duplicate

修前 `504880ff` 的 `_proven_exchange_rejection` 在 `code in _DEFINITE_NEW_ORDER_REJECT_CODES` 时直接返回真，不看消息。`-1013` 加 `Duplicate order sent.` 因此会释放。

修后：消息含 duplicate / already / client order 时不证明“没有订单”；空消息也不证明。`-1013` 的 `Filter failure: LOT_SIZE` 和 `-2010` 的余额不足原文仍释放。`-1007`、非 400、字符串 code、已有成交号都不释放。网关测试在释放被拒绝后，第二次 `process_one` 的 `execute` 调用数是 0，状态仍是 `unknown`。这是合成回包，不是交易所观察。

## 测试

导入前封住 socket。

- decision：`Ran 529 tests in 6.019s`，通过 528，失败 0，错误 0，跳过 1。跳过的是默认路径没有城大快照 `data/lseg/lseg_macro.db`。工作簿测试使用已重建的 `55b2731a` 底库。
- execution：`Ran 527 tests in 6.687s`，通过 527，失败 0，错误 0，跳过 0。

没有部署，没有改生产环境或急停，没有启动 emitter。宏观全国家双侧钟点合同没有改。WallMid 默认 `None` 没有改。
