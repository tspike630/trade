# 现货拥挤旁证列（2026-11-05 ≈17:00 HKT）

杨令，群「我要赚米」：只要指标层扩列。`tech_boll_fade` 入场阈不动（`change_5m` ≤ -2、`volume_strength` ≥ 0.5、价格破下轨）。`INDICATORS_WEIGHT_*` 仍是 0，`local_score` 权仍是 0。下面这些列是影子旁证，不进决策硬门，也不表示「必砸第二锤」。

合约旁表 `exports/crowding_futures.db`（`ls_ratio` / `oi_hist`）这次不读不写。`token_indicators` 不改。

## 列

| 表 | 列 | 口径 |
|---|---|---|
| `binance_klines` | `taker_buy_quote_volume` | 公开 `GET /api/v3/klines` 数组**下标 10**（从 0 起）：Taker buy quote asset volume。下标 9 是 taker buy **base** volume，不写入这一列。缺列、空串、非数字 → `NULL`，这一根 K 线仍入库 |
| `binance_indicators` | `avg_trade_size` | 当前一根的 `quote_volume / trades`。现网任务是 5m，所以是当前这根 5m。`trades` 为 `NULL` 或 ≤ 0 → `NULL` |
| `binance_indicators` | `trades_spike_ratio` | 最近 1 根 `trades` / 紧挨着的前 **12** 根 `trades` 的算术平均。当前根不进分母。现网 `interval=5m`，这 12 根是 1 小时。按 `close_time` 排序的已入库 K 线，不按墙钟补洞 |
| `binance_indicators` | `taker_buy_quote_ratio` | 当前一根 `taker_buy_quote_volume / quote_volume`。不截断到 1 |

`trades_spike_ratio` 在这些情况是 `NULL`，不当 0、也不当无穷：序列不足 13 根；当前或前 12 根里任一 `trades` 为 `NULL`；`trades` < 0；前 12 根均值 ≤ 0。当前 `trades` 为 0 且分母 > 0 时，比值是 0。

`taker_buy_quote_ratio` 在 taker 为 `NULL`、taker < 0、或 `quote_volume` ≤ 0 时为 `NULL`。

`avg_trade_size` 在报价为 0、笔数 > 0 时是 0。笔数空着或是 0 时是 `NULL`，不抛。

## 影子用途

可以拿来看这一根里：主动买报价成交占报价量多少、笔数相对前一小时是否突然变密、单笔报价有多大。高占比或尖峰只说明这一根的成交结构变了。它不是「下一根必砸」的开关，也不是新的入场条件。`list_binance_hot_tokens` 不把这三列抄进扫描行，`TechBollFadeStrategy` 仍只看跌幅、量能、下轨。

`volume_strength` 公式不动：仍是 `indicators.technical.volume_strength`，样本还是每根 K 线往前 1 小时的 quote volume 合计，再比过去 24 小时这些读数的均值。笔数和 taker 不进这个比。

## 老库

现网库已经在跑（同一文件里还有其它表）。`ensure_schema` 对已有的 `binance_klines` / `binance_indicators` 只 `ALTER TABLE … ADD COLUMN`。新列在旧行上是 `NULL`。不 `DROP`，不重建，不动旁边的表。
