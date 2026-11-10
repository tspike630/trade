# 指标供数契约（对照 main `da87020`，2026-11-10）

审阅锁定的远程 `main` 是 `da87020a47ddfa50048f74f6cf871a401e86fae2`。本文件只记录这版代码里指标任务实际写什么、决策层实际读什么。不把更早一版 PR 的结论当成这版的缺陷，也不改入场规则。

## 指标任务写进 `binance_indicators` 的列

`crawler/indicators/binance_spot.py` 的 `_INDICATOR_COLS` 在行情列之外多了 `taker_buy_quote_volume` 和 `taker_buy_ratio`。没有 `trade_size_ratio`，也没有 `change_7d`、`volume_7d_avg`、`buy_ratio_5m`、`buy_ratio_1h`、`avg_trade_usd_5m`、`lower_wick_ratio`。

`taker_buy_ratio_from_kline` 的现规则：quote 成交额大于 0 且 taker quote 占比在 `[0, 1]` 时用 quote；否则若 base 量大于 0 且占比合法，用 base。这是 `f25b397` 写上的订单流规则，决策层 `taker_buy_ratio_from_bar` 用同一顺序。本变更不改这条。

## 已收盘

REST 入库在调用 `persist_klines_and_factors` 时传入 `now_ms`，只留下 `close_time_ms <= now`。`_closed_klines(now_ms=None)` 表示调用方已经筛过，不再滤。WebSocket 只在 `k.x is True` 时把 `_closed_klines` 缓冲区交给回调，未收盘的 bar 留在 `_live_kline`，不进这次写入。

## `funding_z`

采集脚本把资金费率写进 `public_derivatives.db` 的 `binance_perp_counterparty`。`attach_counterparty_context` 只在扫描行上该字段是 `None` 时才从这张表补。K 线任务的 `INSERT OR REPLACE` 不包含 `funding_z`，所以下一轮会把指标行上的这个格子清成 `NULL`，决策侧继续用永续表里的新值。不把这次替换改成保留旧值，否则会挡住这次补数。

## `trade_size_ratio`

当前指标任务不写这一列。`list_binance_hot_tokens` 只在列存在且值不是 `NULL` 时把它抄进扫描行。`resolve_trade_size_ratio` 看到有限数就直接用，负值拒绝为 `trade_size_ratio_invalid`。因此这一列一旦被别的写入者填上有限数，就会进现有入场；本任务不填它。
