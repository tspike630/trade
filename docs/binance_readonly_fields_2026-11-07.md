# 币安现货只读列（2026-11-07）

生产者是 `indicators.binance_spot`。这一批列只记在 `binance_klines` / `binance_indicators` 上。`tech_boll_fade` 的跌 2、量能 0.5、破下轨、BOLL(20, 2σ)、`INDICATORS_WEIGHT_*` 都不改。决策层的透传和 `bnb_native` 的量单位不在这一处改。

老库只 `ALTER TABLE … ADD COLUMN`。已经有的列和行留下。`INSERT … ON CONFLICT DO UPDATE` 只改本模块自己的列，不把旁边多出来的列写成 NULL。第二次迁移是空操作。`token_indicators` 不动。

公开 K 线一次最多 1000 根（约 3.5 天的 5m）。这里不按 `startTime` 往回翻页。写入不删更早的 `(symbol, interval, open_time_ms)`。因子用该符号该周期在库里的全部已收盘 K 线，不是只拿这一次请求的那一截。单次跑不满连续 7 天时，`change_7d` / `volume_7d_avg` 保持 NULL。

只用已收盘的 5m：`close_time_ms <= now`。还在走的那根不进价格、不进下面八列，也不进 `quote_volume_24h`。周期不是 5m 时，这八列写 NULL（根数是按 5m 数的）。

## K 线

| 列 | 来源 |
|---|---|
| `taker_buy_volume` | `/api/v3/klines` 下标 9，可空 |
| `taker_buy_quote_volume` | 下标 10，可空 |

8 或 9 个字段的旧行仍能解析。下标 7 的 quote 若是空的或不是数字，这根 K 线留下，`quote_volume` 是 NULL，不写成 0，也不把这根从时间网格里拿掉。用到 quote 的窗口只要含这根，整段结果是 NULL，不把剩下的根加总来冒充。taker 下标坏了同样只让该格变成 NULL。

## 八列

都可空。最新一根指已收盘 5m 里 `open_time_ms` 最大的那根。

| 列 | 口径 |
|---|---|
| `taker_buy_quote_volume_5m` | 最新一根的 `taker_buy_quote_volume`。没有就是 NULL |
| `buy_ratio_5m` | 最新一根 `taker_buy_quote_volume / quote_volume`。taker 缺失、quote 缺失或 quote ≤ 0 → NULL。taker 为 0 且 quote > 0 → 0 |
| `buy_ratio_1h` | 含最新一根在内的连续 12 根已收盘 5m。`sum(taker_buy_quote_volume) / sum(quote_volume)`。少一根、中间有洞、任一根本身缺 taker quote 或 quote、或 quote 合计 ≤ 0 → NULL。缺的是 taker **base**（下标 9）时不因此作废。正好 60 分钟前开盘的那根不在这 12 根里 |
| `avg_trade_usd_5m` | 最新一根 `quote_volume / trades`。笔数缺失或 ≤ 0 → NULL。quote 为 0 且笔数 > 0 → 0 |
| `trade_size_ratio` | 见下一节 |
| `lower_wick_ratio` | `(close - low) / (high - low)`。这是收盘在整根高低区间里的位置，不是下影线。真下影线是 `(min(open, close) - low) / (high - low)`，这里不用。`high == low`（平棒）为 0。`high < low` 为 NULL。不夹到 `[0, 1]` |
| `change_7d` | 见 7 日 |
| `volume_7d_avg` | 见 7 日 |

`buy_ratio_5m` 和 `buy_ratio_1h` 都只看 taker quote 和 quote volume。taker base 不参与，缺了也不把比值打成 NULL。quote taker 的 0 是有值。NULL 不是 0。

## `trade_size_ratio` 怎么选的

当前根的 `quote_volume / trades`，除以紧前 **288** 根的 `sum(quote_volume) / sum(trades)`。

- 288 根不含当前根。最老一根的开盘时刻正好是当前开盘往前 24h，它的收盘正好是当前收盘往前 24h，算进分母。再早一根不算。
- 这 289 根（288 + 当前）必须是连续 5m，开盘间隔正好 300000 ms。少一根、多一个洞、分母里任一 quote 缺失、任一笔数缺失或 ≤ 0、当前笔数不可用、分母合计 ≤ 0 → NULL。不把窗口缩短来凑数。
- 分母是成交额合计 / 笔数合计，不是每根 `quote/trades` 再求平均。笔数不均时两个数不一样。

和已经在 main 上的口径：

- `live_bnb_entry_zone.trade_size_ratio_from_bars` 也是 `sum(quote)/sum(trades)`，当前根不进分母，收盘正好落在 24h 边界上的那根算进去。它按时间窗收，不要求满 288 根，中间缺笔数才失败。满 288 连续且字段齐全时，本列和那个函数是同一个数。
- 未合并的 PR #165 用的是紧前 288 根每根均额的算术平均，不是 sum/sum。本列不采用那个平均。PR #164 的 `avg_trade_size` 是当前根均额，没有 `trade_size_ratio`；列名不同，本模块不写 `avg_trade_size`。

主分支的 `list_binance_hot_tokens` 已经把非空的 `trade_size_ratio` 抄进扫描行。`resolve_trade_size_ratio` 看到有限数就直接用，不再用 K 线重算。所以本列一旦写成非 NULL，现有入场逻辑就会吃到它。阈值没改，输入会变。NULL 不会被抄进去，短历史仍走原来的时间窗回退。

不改决策文件时，能挡住这件事的只有生产者侧：

1. 继续写这个列名。部署指标任务之后，满 288 根连续 K 线的符号，入场用的是本任务算的 sum/sum，不再是决策当时从 K 线重算的数。这是现在的做法。
2. 不建、不写 `trade_size_ratio`。决策文件不用动，入场保持重算。八列少一列。
3. 改存一个决策层不读的列名。入场不动，但和点名的列名不一致。

决策侧若要保留本列、又不要它进门，得在扫描行上不要这个键。那是他们的文件，这里不动。

## 7 日

`change_7d` 和 `volume_7d_avg` 只在调用现成的 `change_pct(..., change_7d)` 和 `volume_7d_avg` 之前先看覆盖。

覆盖是最新一根往前数，开盘间隔正好 5m、含两端一共 2017 根（2016 步 = 7 天）。少一根、中间缺一根，两列都是 NULL。

这两个函数本身都有最多 6 小时的 skew。`volume_7d_avg` 还会丢掉空的日桶，只要最早一条 `volume_h24` 落在锚点后 6 小时内、并且至少两个日桶有数，就会出数。所以「差几小时」或「中间有洞」它们仍可能出数。生产者不收这种数，避免把不满的窗口当成 7 日。

`volume_h24` 只在该根已经有连续 289 根（含自己，开盘跨度正好 24h）时写成这 289 根的 quote 合计。更短的前缀是 NULL，不用 `volume_h1` 去乘，也不把第一天的残段填上。

因此这不是生产者多加的一天，是 `volume_7d_avg` 自己的规则：

- 它要最早一条 `volume_h24` 落在 `end - 7d` 之后的 6 小时内，并且把该字段当成已经算好的 trailing 24h。
- 连续正好 7 天时，第一根真实的 289 根 trailing 日额出现在开端之后约 24 小时，出了这 6 小时。只盖真实日额时，函数返回 NULL。`change_7d` 不依赖这个字段，所以可以有数。
- 若把开端那段不满 24h 的 quote 合计也写成 `volume_h24`，函数会出数，但第一个日桶留下的是这段短合计（每根 quote 为 100 时是 288×100，不是 289×100）。七个桶的均值不是七个完整 trailing 日。这是拿短窗冒充日额，不做。
- 连续大约 8 天（7 天再加前面 24h）时，锚点那根才有真实 trailing `volume_h24`，`volume_7d_avg` 等于函数的日桶均值。日桶取该桶最晚一条，不是 7 天 quote 加总再除以 7。

## 给决策层的读法

表还是 `binance_indicators`。老库没有这些列时，SELECT 不要点名它们。列在、值为 NULL 时，扫描行上不要写成一个非法数。

建议透传（有列才选，NULL 保持 NULL 或干脆不放这个键）：

`change_7d`、`volume_7d_avg`、`taker_buy_quote_volume_5m`、`buy_ratio_5m`、`buy_ratio_1h`、`avg_trade_usd_5m`、`trade_size_ratio`、`lower_wick_ratio`。

顺序：先按 `open_time_ms` 排已收盘 5m，最新一根是最后一根。`buy_ratio_1h` 是这根往前的 12 根（含本根）。`trade_size_ratio` 的分母是再往前的 288 根（不含本根）。7 日网格是含本根在内的 2017 根。

`lower_wick_ratio` 不要改写成下影线长度。名字照列名用。
