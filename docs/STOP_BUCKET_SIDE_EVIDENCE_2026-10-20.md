# 止损桶入口特征旁证 · 2026-10-20

> 只读：`paper_trading.db` × `dexscreener.db.decision_log`。  
> **不算有效性、不改闸、不改 SL/门槛。** 供五层互批对照用。

## 0. Join 结论

| 链路 | 结果 |
|---|---|
| `positions.meta_json.decision_log_id` → `decision_log.id` | **成功 10/10** |
| `decision_log.raw_json.extras`（`jev_strength` / `jev_signal` / `jev_score`） | **可得** |
| `meta_json`：`liquidity_usd` / `slippage` / `size_pct` | **可得** |
| `jev_signals` 按 token+chain 近邻 `analyzed_at` | **时序缺口**：最近行距决策 **~3–9 天**（`dt` ≫ 1h），**不可当作入场时刻 coverage**；本旁证**不用**该 coverage 做对照结论 |

预先落盘抽样：`exports/stop_bucket_features_2026-10-20.csv`（与下表同源，只含 meta 侧）。

## 1. 止损 10 笔明细（已 join）

| dlid | closed_at (UTC) | pnl_usd | size_usd | liq_usd | slip | jev_strength | reason 摘要 |
|---:|---|---:|---:|---:|---:|---:|---|
| 184 | 10-14 08:34 | −84.33 | 500.69 | 11,995 | 0.020 | 0.1547 | strength≥0.15 · score=91 |
| 214 | 10-14 14:13 | −30.94 | 497.26 | 2,869,948 | 0.003 | 0.1914 | score=87 |
| 216 | 10-14 14:48 | −189.89 | 496.65 | 54,473 | 0.010 | 0.1581 | score=93 |
| 268 | 10-15 12:58 | −30.00 | 488.66 | 116,340 | 0.010 | 0.1547 | score=91 |
| 278 | 10-15 17:07 | **−491.72** | 492.18 | 145,844 | 0.010 | 0.1634 | score=86 |
| 290 | 10-16 06:26 | −58.77 | 469.22 | 60,661 | 0.010 | 0.1892 | score=86 |
| 315 | 10-17 00:20 | **−313.91** | 473.52 | 7,724,303 | 0.003 | 0.1817 | score=79 |
| 338 | 10-18 03:36 | −27.23 | 457.85 | 3,203,443 | 0.003 | 0.1914 | score=87 |
| 353 | 10-18 19:45 | −24.66 | 453.32 | 3,002,953 | 0.003 | 0.1914 | score=87 |
| 485 | 10-19 09:07 | **−289.45** | 451.21 | 7,583,855 | 0.003 | 0.1817 | score=79 |

- 桶合计：**n=10 · Σpnl ≈ −$1,540.89 · wr=0% · 均亏 ≈ −$154.1**  
- 策略一律 `jev_strength_v1`；`size_pct=0.05`；`mode=dry_run`；`purpose=live_candidate`（决策日志口径，**非**实盘）。

## 2. 与其他出场桶对照（入口可得字段）

| 出场 | n | Σpnl | med liq | med slip | med jev_strength（decision_log extras） |
|---|---:|---:|---:|---:|---:|
| `stop_loss` | 10 | −1,541 | ≈1.51M | 0.0065 | **0.1817** |
| `max_hold` | 129 | −192 | ≈2.90M | 0.003 | **0.1817** |
| `take_profit` | 3 | +438 | ≈7.55M | 0.003 | **0.1817** |

### 可读（弱）方向

1. **strength 无区分度**：三桶 med strength 同为 **0.1817**——现门槛附近的 strength **不能**解释「为何止损 vs hold vs TP」。  
2. **止损桶 liq 中位偏低、离散极大**：含 1 笔 `<50k`（dlid=184，低于常见 `DECISION_MIN_LIQ=50k` 叙事；属历史/漏网观察，**不在此提案改 min_liq**）；也含多笔 **≥1M 仍大亏**（−314 / −289）——**不是「低流动性单因子故事」**。  
3. **大额止损 ≠ 低 liq**：峰值 −492（liq≈146k）、−314（liq≈7.7M）、−289（liq≈7.6M）——出血在**退出路径/波动**，入口 strength/liq 标签不够。  
4. **slip 中位止损略高**（0.0065 vs hold/TP 的 0.003），但与最大亏损单并不单调对应（大亏单多为 slip=0.003）。

### 不可读 / 缺口（勿编造）

| 缺口 | 说明 |
|---|---|
| 入场时刻 `coverage_pct` | `decision_log.raw_json.extras` **无** coverage；`jev_signals` 近邻行过旧（数天），**不能**当 t0 coverage |
| 技术指标截面（RSI/MACD 等） | 止损入场 raw_json **未带**；权重全 0，本旁证不补造 |
| token_age / pairCreatedAt | 本批 extras **未见**；未强行从 pairs 表二次推断写入结论 |
| 因果/改门槛建议 | **不做**；只列观察对照 |

## 3. 给群批的一句话

止损 10 笔 **能**稳定 join 到 `decision_log` 并读出 strength/liq/slip；**不能**可靠 join 到入场时刻 coverage/宽指标。现有入口特征**解释不了**止损桶金额结构——继续只观察，**不**据此盘中改 SL / min_liq / strength。

---

*生成：决策执行助手只读复算 · 2026-10-20 HKT · 本文不改任何运行参。*
