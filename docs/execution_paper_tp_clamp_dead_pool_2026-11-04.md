# Paper TP fill clamp + dead-pool close (2026-11-04)

记账修正（不动入场闸 / live / #71 / SIZE_MULT）。

## 改动

1. **TP 成交价夹到触发价**（限价卖语义）  
   - 位置：`execution/pricing.py::limit_exit_fill_price`，由 `crawler/paper_trade.py::tick` 在 `_close_fill` 之后、`close_position` / jump-guard 之前调用。  
   - `fill = min(mid*(1-slip), entry*(1+tp_pct/100))`；仅 `exit_reason == take_profit`。  
   - SL / max_hold **不夹**（跳空按更差价记账）。

2. **死池平仓**  
   - tick 平仓时若 `liquidity_usd < EXECUTION_PAPER_CLOSE_MIN_LIQ_USD`（默认 1000），按 **entry** 平仓、pnl≈0，`exit_reason={reason}:dead_pool`，meta 标 `dead_pool_clipped` / `close_liq_usd`。

3. **审计字段**（tick actions）：`tp_clamped` / `tp_limit_price` / `dead_pool_clipped` / `close_liq_usd`。

## Env（默认开）

| Env | Default | Role |
|-----|---------|------|
| `EXECUTION_PAPER_TP_CLAMP` | **1** | TP 限价夹；`0/false/off` 关 |
| `EXECUTION_PAPER_CLOSE_MIN_LIQ_USD` | **1000** | 死池阈值；`≤0` 关 |

`execution/exits.py` 未改（回测共用触发逻辑不变）。`PaperExecutor._close_with_jump_guard` 仍在夹价之后；夹后 fill 再交给它，避免两层重复夹。
