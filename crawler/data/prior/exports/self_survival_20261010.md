# 自有 DB 观测跨度存活 20261010

> **只读** `dexscreener.db.pair_history`；不写业务表。
> **口径**：对每个 `(chain_id, token_address)`，`first_seen=MIN(recorded_at)`，`last_seen=MAX(recorded_at)`；**存活 t ⇔ (last_seen − first_seen) > t**。
> 这是 crawler **观测跨度**，不是链上真实寿命 / GMGN OHLCV / RED-PUMP 判定窗口。
> `token_indicators` 存在但未用于本报告（其为最新快照，无 first/last 历史跨度）。

## 1. 样本与 crawler 窗口
- DB: `dexscreener.db`（mode=ro）
- pair_history 行数: 250,013
- unique tokens (chain_id+token_address): **9,317**
- recorded_at 全局范围: `2026-10-09T12:00:00Z` → `2026-10-10T15:45:08Z`
- crawler 总观测窗口: **27.8h**（≈1.16d）
- **稀疏/短窗标记**: YES — 下列存活率受观测覆盖限制，勿当市场寿命基线过度解读

## 2. 存活率（span > t）
| 上线后 | 存活率 | 样本(活过) |
|---|---|---|
| 1h | 92.7% | 8,638 |
| 4h | 75.3% | 7,015 |
| 24h | 0.0% | 0 |
| 7d (168h) | 0.0% | 0 |

## 3. 观测跨度分布（分钟）
| P25 | P50 | P75 | max | n |
|---|---|---|---|---|
| 247.3m | 758.5m | 1030.5m | 1283.5m | 9,317 |

跨度（小时）: P25=4.12h / P50=12.64h / P75=17.17h / max=21.39h

## 4. 反假象 / 口径提醒
- ⚠ crawler 总观测窗口仅约 27.8h（2026-10-09T12:00:00Z → 2026-10-10T15:45:08Z）；7d 存活必然受覆盖上限约束，≠ 链上真实 7d 死亡率
- 假象扫描（99.8%/1440 类）: PASS
- 与 GMGN early-window 0% 不同：自有库若窗口≈1d，则 1h 存活可能很高而 7d≈0%，这反映 **crawler 覆盖**，须并排写明窗口长度。

## 5. 周报使用建议
- 本表仅作 PRIOR 旁路自检；默认不进风控（RISK_USE_PRIOR=0）。
- 与 CoinGecko 当天死 / RED-PUMP 毕业率 / GMGN early-peak 并排，勿单独当寿命真值。
- 待 crawler 连续跑满 ≥7d 后再解读 7d 存活。
