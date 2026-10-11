# Social 突热（spike）— 观察素材，非买入信号

## 定位

- **突热 = 滞后确认**：`spike_ratio` / `spike` 是指标加工层的**观察标记**，不是入场触发。
- **不接线**：不发告警、不推 Telegram、不进 decision_engine；`INDICATORS_WEIGHT_*` 保持 0。
- **先验提醒**：社交热度见顶极快；对照 prior 的 `peak_window`（创后极短窗口）经验——突热出现时价格峰往往已过或正在过。`peak_window` 先验命中率接近 0 的语境下，更应把突热当复盘素材，而非追涨依据。

## 公式

```
mentions_1h   = sentiment_mentions_1h   (≈ x_watch.mention_count_last)
mentions_24h  = sentiment_mentions_24h  (见 INDICATORS_STEP3 estimate_mention_windows)

mentions_24h_hourly_avg = mentions_24h / 24.0
spike_ratio = mentions_1h / max(mentions_24h_hourly_avg, 1.0)
spike       = 1  iff  spike_ratio >= SENTIMENT_SPIKE_MIN   (默认 3.0)
            = 0  otherwise
```

分母下限 `1.0`：避免 24h 均值接近 0 时比值爆炸；此时 `spike_ratio ≈ mentions_1h`。

与告警侧 `X_MENTION_SPIKE_RATIO`（`sources/x_api` → `social_spike` alert）**无关**——本标记只写 `token_indicators`，不读/不写 alerts 表以外的决策路径（读 alerts 仍仅用于既有 heat/polarity 偏置）。

## 配置

| 变量 | 默认 | 含义 |
|------|------|------|
| `SENTIMENT_SPIKE_MIN` | `3` | `spike=1` 阈值 |

## 写入范围

- `token_indicators.spike_ratio` / `token_indicators.spike`
- 热档 `indicator_history` 同名字段（可选快照）
- **不写** paper / execution / news / ccxt 等业务库

## 复盘辅助查询（可选；非周报 § B）

```sql
-- 最近写入的突热 token（观察清单）
SELECT chain_id, token_address,
       sentiment_mentions_1h, sentiment_mentions_24h,
       spike_ratio, spike, sentiment_heat_score,
       sentiment_updated_at, updated_at
FROM token_indicators
WHERE spike = 1
ORDER BY spike_ratio DESC, updated_at DESC
LIMIT 50;
```

周报 § B（复盘汇总）**未实现**——留给复盘流程单独接。
