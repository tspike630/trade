# Paper TP fill clamp + profit-side dead-pool close (2026-11-04)

记账修正（不动入场闸 / live / #71 / SIZE_MULT）。

## 改动

1. **TP 成交价夹到触发价**（限价卖语义）  
   - `execution/pricing.py::limit_exit_fill_price`，由 `crawler/paper_trade.py::tick` 在 jump-guard 之前调用。  
   - 仅 `take_profit`；SL / max_hold **不夹**。

2. **死池平仓（仅浮盈）** — v2  
   - Helper：`execution/pricing.py::is_dead_pool_close(naive_fill, entry, liq, min_liq)`。  
   - 条件：`liq is not None` 且 `liq < EXECUTION_PAPER_CLOSE_MIN_LIQ_USD`（默认 1000）**且** `naive_fill > entry`。  
   - 满足 → 按 entry 平、pnl≈0、`reason={reason}:dead_pool`。  
   - **浮亏不夹**（崩盘亏损照记）。`liq is None` 不动作。  
   - 审计：`close_liq_usd` + `close_liq_source`（`pair_raw` / `missing`）。

3. 死池分支优先于 TP 夹价（抵掉超限浮盈）。

## Env

| Env | Default | Role |
|-----|---------|------|
| `EXECUTION_PAPER_TP_CLAMP` | **1** | TP 限价夹；`0` 关 |
| `EXECUTION_PAPER_CLOSE_MIN_LIQ_USD` | **1000** | 死池阈值；`≤0` 关 |

重述 v2：`docs/paper_tp_clamp_restatement_v2_2026-11-04.md`。
