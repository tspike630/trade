# 自有 DB 观测跨度存活 20261019

> **只读** `dexscreener.db.pair_history`；不写业务表。
> **口径**：对每个 `(chain_id, token_address)`，`first_seen=MIN(recorded_at)`，`last_seen=MAX(recorded_at)`；**存活 t ⇔ (last_seen − first_seen) > t**。
> 这是 crawler **观测跨度**，不是链上真实寿命 / GMGN OHLCV / RED-PUMP 判定窗口。
> `token_indicators` 存在但未用于本报告（其为最新快照，无 first/last 历史跨度）。

## 1. 样本与 crawler 窗口
- DB: `dexscreener.db`（mode=ro）
- pair_history 行数: 3,448,895
- unique tokens (chain_id+token_address): **65,094**
- recorded_at 全局范围: `2026-10-09T12:00:00Z` → `2026-10-19T00:04:22Z`
- crawler 总观测窗口: **228.1h**（≈9.50d）
- 稀疏/短窗标记: no

## 2. 存活率（span > t）
| 上线后 | 存活率 | 样本(活过) |
|---|---|---|
| 1h | 99.4% | 64,713 |
| 4h | 98.2% | 63,894 |
| 24h | 57.7% | 37,579 |
| 7d (168h) | 8.6% | 5,577 |

## 3. 观测跨度分布（分钟）
| P25 | P50 | P75 | max | n |
|---|---|---|---|---|
| 1346.2m | 1478.5m | 4704.6m | 13299.6m | 65,094 |

跨度（小时）: P25=22.44h / P50=24.64h / P75=78.41h / max=221.66h

## 4. 反假象 / 口径提醒
- 未见经典 99.8%/1440 假象模式
- 假象扫描（99.8%/1440 类）: PASS
- 与 GMGN early-window 0% 不同：自有库若窗口≈1d，则 1h 存活可能很高而 7d≈0%，这反映 **crawler 覆盖**，须并排写明窗口长度。

## 5. 周报使用建议
- 本表仅作 PRIOR 旁路自检；默认不进风控（RISK_USE_PRIOR=0）。
- 与 CoinGecko 当天死 / RED-PUMP 毕业率 / GMGN early-peak 并排，勿单独当寿命真值。
- 待 crawler 连续跑满 ≥7d 后再解读 7d 存活。
