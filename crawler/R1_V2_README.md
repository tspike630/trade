# Review R1 — JEV 切片对齐周检过滤后（增量 v2）

在你仓库落地版（你回传的 5 个文件）基础上做的增量改动：把第 4 节 JEV 切片从「仅 strength」升级为「周检过滤后同口径」。

## 改了什么
| 文件 | 增量 |
|------|------|
| `review/data.py` | ① `SignalChainRow` +`liquidity_at_signal` / +`jev_coverage`；② `_nearest_price`→`_nearest_snap`（返回 price+liquidity）；③ `annotate_forward_returns` 填 liq（history@t0±tol，缺则回退 `raw_json.liquidity.usd`）；④ 新增 `load_jev_coverage` |
| `review/metrics.py` | `slice_jev_filtered` +`min_liq`(50k) / `min_coverage`(10) / `min_price`(1e-6)，缺失即排除 |
| `review/report.py` | 第 4 节口径说明更新 + 数据来源说明 + 对账提示改指过滤后（71.4%/57.1%, n=14）；新增 `jev_slice_note` 可选参数 |
| `review/run.py` | +`--min-strength` / `--min-liq` / `--min-coverage` / `--min-price`（默认对齐周检）；链路加 `load_jev_coverage` |

## coverage 取数（对齐周检）
1. 主路径：`jev_signals.coverage_pct`，按 token 最新行（时间列 PRAGMA 探测：updated_at→recorded_at→ts→created_at→rowid）
2. 回退：`token_indicators.jev_confidence * 100`
3. 表/列不存在 → 不报错，该 token coverage=None → 切片排除（与周检「缺失即不通过」同口径）

## 本地冒烟（模拟库，非真实库）
40 条信号 → 单过滤：strength≥0.15 得 22 / liq≥50k 得 24 / cov≥10 得 35 / price 全过 → **四过滤交集 19**；
报告第 4 节显示 n=19、口径与数据来源说明正确；`exit 0`。

## 你仓库验收（重点：和周检过滤后对账）
```bash
python -m review.run --period 7d --report
```
- 期望第 4 节：**n=14**（与 `DECISION_ENGINE_JEV_VALIDITY_FILTERED.md` 过滤后一致），
  1h≈71.4%、4h≈57.1%（±容差/过滤来源差异可解释）
- 覆盖对照可试：`--min-liq 100000` 应得 n≈11

## 注意
- 旧版（仅 strength）的调用不受影响：三个新参数都有默认值
- `--dump-chain` 的 JSON 现在含 `liquidity_at_signal` / `jev_coverage` 字段（R2 归因可直接复用）
