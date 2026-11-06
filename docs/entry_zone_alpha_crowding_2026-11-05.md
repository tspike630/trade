# 现货 α 入场区列（2026-11-05）

杨 17:41 HKT 解除 `live_bnb` 这条配方的冻结。看板挂卡 `entry_zone_unfreeze_20261105.md`：决策把 live 入场改到三档 α 区，止损锚在针低，止盈在针那根的 `boll_mid`。纸面 `tech_boll_fade` 不动。执行仍等 dry emit → 复盘③ → 杀开关。

本变更只写 `binance_indicators` 的列。不改 `volume_strength`、BOLL(20, 2σ) 的算法、跌 2 / 量能 0.5 / 破下轨、TP10/SL5/60 分钟，也不碰杀开关。`list_binance_hot_tokens` 不把这些列抄进扫描行，所以现网策略读不到它们。决策的接线 PR 自己去读。

`action_zone_weight` 写死 0，`action_zone_state` 写死 `shadow`。这不是入场开关。

所有影子计算只用已经收盘的 5m K 线（`close_time_ms <= now`）。正在走的那根的影线不算针、不算 `no_new_low`、不算突破。

## 针

最近一根收盘 K 线，满足任意一条，就是针（卖 climax）：

- 这一根从本根的 BOLL 下轨穿下去：`low < boll_lower`，并且 `open >= boll_lower`，或上一根收盘还在上一根下轨之上。已经躺在下轨下面的后续 K 线不再算新的一次穿轨。
- `change_5m <= -2`（百分点，和现有列同一单位）。
- `change_5m_z <= -2`：这一根收益 ÷ 紧前 20 根收益的样本标准差。当前根不进分母。σ ≤ 1e-12 则为 NULL，不当无穷。这列本身不落库（拥挤 PR 另写 `change_5m_z`）。

针的 BOLL 中轨、下轨只用到这一根为止的收盘价。后面的 K 线不改这根的 `space_m` / `alpha_zone` / `zone_tier`。

| 列 | 含义 |
|---|---|
| `sell_climax_low` / `pin_low` | 针的最低价。两个数相同。`pin_low` 是复盘用的名字 |
| `hammer_close` | 针的收盘价 |
| `lower_shadow_rebound_pct` | `(close - low) / max(high - low, 1e-12) * 100`。十字星且收在最低是 0。`high < low` 为 NULL |
| `no_new_low` | 下一根收盘 K 线的 `low >= pin_low`。0/1。还没有下一根则 NULL |
| `action_complete` | 下一根 `low >= pin_low` 且 `close > pin_low`。0/1/NULL |
| `rebound_from_low_pct` | `(close_next - pin_low) / pin_low * 100`。没有下一根则 NULL |
| `natural_entry_low` | `pin_low` |
| `natural_entry_high` | 确认 K 线的**高点**（不是收盘）。观察区上沿 |
| `breakout_entry_price` | 同确认 K 线高点。后续收盘 K 线要向上穿过它 |
| `breakout_fired` | 确认之后某根收盘 K 线 `high > 确认高` 且 `low >= pin_low`，并且当时 `action_complete = 1`。先破针低则是 0 |
| `setup_invalidated` | 确认及之后、突破之前，任一收盘 K 线 `low < pin_low` 则 1。突破已经打出之后再破低，不把这一列改成 1 |

`action_complete` 和 `breakout_fired` 只供复盘消融。决策明确不把它们叠成三档入场的额外硬门。

固定猜 −5% / −7% 止损，量的是进场价往下的一段距离，和卖盘被吃掉的那根低点无关。针低被打穿，说明这一下没有承接完，论点不成立。所以止损锚是针低，不是进场后再猜一个百分比。

## 止损：口径是 L − 0.1×S

针上：

- `L` = 这一根的 `low`（`space_l`，也等于 `pin_low`）
- `M` = **这一根**的 `boll_mid`（`space_m`）。不是最新一根的中轨
- `P` = 这一根的 `close`
- `S` = `M − L`（`space_s`）。`S <= 0` 时下面整个 α 组都是 NULL

**口径止损**

```
stop_price_zone = L − 0.1 × S
tp_price        = M
```

杨的演算里出现过 $0.937。在 L=0.940、S=0.060 时，0.937 = L − 0.05×S。不要用那个数反推止损。口径是 0.1×S。同一组数字下 `stop_price_zone = 0.934`。

`natural_stop_price = pin_low × (1 − 10/10000)`，`stop_buffer_bps = 10`。这是次要变体，给复盘对比「贴着最低价会被影线扫掉」用。**不是口径，决策接线时默认读 `stop_price_zone`。** 正好挂在 `pin_low` 上的止损会被影线扫掉。

## 盈亏比是绝对距离，而且分母含缓冲

```
α        = (P − L) / S          可以 < 0 或 > 1，原样存 alpha_zone
rr_to_mid = (M − P) / (P − stop_price_zone)
          = (M − P) / (P − (L − 0.1 S))
rr_raw   = (M − P) / (P − L)    仅当 P > L，否则 NULL
```

`rr_raw` 只供消融。档位门槛看的是缓冲后的 `rr_to_mid`，不是 α 的剩余，也不是 `(M−P)/(P−L)`。

在 `stop = L − 0.1 S`、`P = L + α S` 时：

```
rr_to_mid = (1 − α) / (α + 0.1)
rr_raw    = (1 − α) / α
```

| α | rr_raw（旧的绝对距离，不含缓冲） | rr_to_mid（口径，含 0.1 S 缓冲） |
|---|---|---|
| 0.12 | 0.88/0.12 ≈ 7.333 | 0.88/0.22 = 4.0 |
| 0.28 | 0.72/0.28 ≈ 2.571 | 0.72/0.38 ≈ 1.895 |
| 0.40 | 0.60/0.40 = 1.5 | 0.60/0.50 = 1.2 |

所以 α=0.40 仍在 tier1 区间上沿里面，但缓冲后的盈亏比是 1.2，**不够 1.5，不是市价**。α=0.28 在 tier2 区间上沿，缓冲后约 1.895，**不够 2.5，也不是市价**。

缓冲后刚好打到门槛的位置：

- tier1，`rr_to_mid = 1.5` → α = 0.34
- tier2，`rr_to_mid = 2.5` → α = 0.75/3.5 ≈ 0.2143

## 三档

`trade_size_ratio` = 针这一根的 `quote_volume / trades` ÷ 紧前 **288** 根（5m 的 24 小时）同样口径的算术平均。当根不进分母。窗口里有一根没有可用笔数，比值是 NULL，不把窗口偷偷缩短。

这只是成交额相对自身近 24 小时变大或变小。**不是庄家身份，也不是「庄家必一锤见底」。** tier1 的意思是这一根的单笔成交额偏大、大单在承接。

`avg_trade_size` 是针这一根的 `quote_volume / trades`。拥挤 PR #164 若已把同名列写成「最新一根」，合并时保留已写上的值；`trade_size_ratio` 仍按针这一根当场算，不回读那一列。列已存在则不再 `ADD COLUMN`。

| `zone_tier` | `trade_size_ratio` | 叫法 | 区间 | `entry_signal` |
|---|---|---|---|---|
| 1 | `>= 1.3` | 大单在承接 | `[L+0.08S, L+0.40S]` | 收盘在区间内 **且** `rr_to_mid >= 1.5` → `market`。α<0.08 → `skip` / `barefoot`（还在跌的光脚）。α>0.40 → `skip` / `chased`。在区间内但盈亏比不够 → `skip` / `rr_below_bar` |
| 2 | `>= 0.85` 且 `< 1.3` | 混杂 | `[L+0.12S, L+0.28S]` | 在区间内 **且** `rr_to_mid >= 2.5` → `market`。收盘在区间上方 → `limit`，价 `L+0.20S`。α<0.08 → `barefoot`。在区间下沿之下但还没到光脚 → `below_zone`。在区间内但盈亏比不够 → `rr_below_bar` |
| 3 | `< 0.85` | 散户拥挤 | 无市价区。`entry_zone_low/high` 为 NULL | `skip`，`entry_skip_reason = retail_crowded`。可选猎手限价 `hunter_limit_price = L−0.50S`（第二锤清算区）。`entry_signal` 仍是 `skip`，不自动改成 `limit` |
| NULL | 比值 NULL | 无法分档 | | `entry_signal` NULL。坐标和 `stop_price_zone` / `tp_price` 在 `S>0` 时仍写出 |

`limit_buy_price` 只在 `entry_signal = limit` 时有数（tier2 的 `L+0.20S`）。tier3 的猎手价在 `hunter_limit_price`，不写进 `limit_buy_price`。

限价单的撤法不在本任务里：决策若挂 `limit_buy_price`，价先破 `L` 就撤。本列只给出价格。

### 数字例子

`M = 1.000`，`L = 0.940`，`S = 0.060`。

| 量 | 值 |
|---|---|
| `stop_price_zone` | 0.934（不是 0.937） |
| `tp_price` | 1.000 |
| tier1 区 | [0.9448, 0.9640] = [L+0.08S, L+0.40S] |
| tier2 限价 | 0.952 = L+0.20S |
| tier3 猎手 | 0.910 = L−0.50S |
| `ratio = 1.45` | tier 1，区仍是 [0.9448, 0.9640] |
| `ratio = 0.65` | tier 3，`skip` / `retail_crowded`，区为 NULL，猎手 0.910 |

α=0.20、ratio=1.45：`P = 0.952`，`rr_to_mid = 0.048/0.018 ≈ 2.667 >= 1.5`，`market`。

α=0.40、ratio=1.45：`P = 0.964` 仍在区内，`rr_to_mid = 1.2 < 1.5`，`skip` / `rr_below_bar`。`rr_raw = 1.5`。

## 状态机（决策要接的是这一条）

1. 针收盘。记下 L、M、S、α、`trade_size_ratio`、`zone_tier`。
2. `entry_signal`：`market` / `limit` / `skip` / NULL。
3. `market`：止损用 `stop_price_zone`（L−0.1S），止盈用 `tp_price`（这根的 M）。`natural_stop_price` 只是对照。
4. `limit`：价在 `limit_buy_price`。价先破 L 则论点失效，单应撤（决策侧）。
5. tier3：记下 `retail_crowded`。猎手价可选，不是市价。

`action_complete` → `breakout_fired` 是另一套观察（确认 K 线高点被后续收盘 K 线向上突破；先破针低则 `setup_invalidated`）。留列，不进三档硬门。

## 现货拥挤旗标（消融）

用 #164 的字段。K 线对象上还没有 `taker_buy_quote_volume` 时，三列都是 NULL，不当 0。

| 列 | 规则 | 算在哪一根 |
|---|---|---|
| `trades_climax` | `trades_spike_ratio >= 2`。针的笔数 / 紧前 12 根笔数的平均。缺笔数、不足 12 根、分母 ≤ 0 → NULL | 针 |
| `taker_buy_absorb` | `taker_buy_quote_ratio >= 0.55` | 针 |
| `avg_trade_size_spike` | 针的 `avg_trade_size` ≥ 2 × 紧前 12 根的中位数。便宜，所以写了。窗口不齐 → NULL | 针 |

## 合约拥挤出清

读 `exports/crowding_futures.db`（或 `CROWDING_FUTURES_DB`，或 `crawler/exports/` 下同名文件）。表 `ls_ratio`、`oi_hist`。文件、表或认不出的列不存在，则四列都是 NULL，不当 0。采集层负责把库填上，本管道不建表、不改那份库。

认的列名（大小写不敏感）：多空比 `global_long_short_account_ratio` / `long_short_ratio` / `ratio` / `ls_ratio`；持仓 `sum_open_interest` / `open_interest` / `oi`；时间 `timestamp` / `time_ms` / `open_time_ms` / `recorded_at`；符号 `symbol` / `pair`。样本时间晚于最后一根已收盘现货 K 线的丢掉，避免看未来。

窗口：最后一档，对比紧前 **3** 档的算术平均。

| 列 | 规则 |
|---|---|
| `ls_flush` | `(前3均值 − 最新) / 前3均值 × 100 > 5` 则 1，否则 0。样本不够或均值 ≤ 0 → NULL |
| `oi_flush` | 最新持仓 < 前 3 均值，且现货收盘低于「前 3 档里最早那一档时间之前（含）的最后一根现货收盘」。两边同时成立才是 1 |
| `crowding_cleared` | `ls_flush AND oi_flush`。缺一边是 NULL，不是 0；任一边明确为 0 则 0 |
| `crowding_crowded_long` | 多空比上升 **且** 持仓上升 **且** 价格下跌。抄底的人在往里挤，三档配方本身不读这一列做硬门 |

## 消融合成（不是入场）

`action_zone_ready = action_complete AND (setup_invalidated 不为 1) AND trades_climax AND taker_buy_absorb AND crowding_cleared`。

任一腿明确为 0 → 0。否则有 NULL → NULL。合约库读不到时 `crowding_cleared` 是 NULL，合成也是 NULL，不会因为「没有合约数据」变成 0。没有针时这一列保持 NULL，即使合约旗标能算出来。

## 老库

`ensure_schema` 对已有的 `binance_indicators` 只 `ALTER TABLE … ADD COLUMN`。不 `DROP`，不重建，不动 `binance_klines` 的结构，不动 `token_indicators`。新列在旧行上是 NULL，直到下次写入。`avg_trade_size` 若已被 #164 加上，这里跳过。

权重保持 0。杀开关仍是执行在复盘③之后的事。
