# JEV 有效性过滤版 — 字段补丁（unique_token + 趋势）

**Time (HK / UTC+8):** 2026-10-10 13:26 HKT  
**脚本：** `scripts/jev_validity.py`（不改 dry-run / 采样 / 主链路）

## 新增

| 字段 / 产物 | 说明 |
|-------------|------|
| **unique_token** | 过滤后按 `(chain_id, token_address)` 去重计数；汇总表单独一行 |
| 有效样本门槛 | 优先 **unique_token≥10**（比过滤后 n≥15 更硬）；n≥15 但 unique_token<10 → 仍「样本不足」 |
| `exports/reports/jev_validity_trend.csv` | 每次跑追加一行：`date,raw_n,filtered_n,unique_token,up_1h_rate,up_4h_rate,median_1h,median_4h,n_019plus,up_1h_019plus,n_1h,n_4h` |
| 报告「对比上周」 | 对比 n / 1h 上涨率 / 0.19+ 层 n；无历史则写**首次** |
| 连续 2 周门槛 | 决策注明是否满足「连续 2 周 1h≥55% 且 unique_token≥10」 |

完整报告见 `DECISION_ENGINE_JEV_VALIDITY_FILTERED.md`；周检时序见 `DECISION_ENGINE_WEEKLY_CHECK.md`。
