# 复盘周报 · 第 10 节「进化建议」

> 状态：轻量落地（2026-10-10）  
> 模块：`review/evolution.py` → `build_evolution_suggestions` / `format_evolution_section`  
> 接线：`review/run.py`（`--report`）· 渲染：`review/report.py` §10

## 边界（钉死）

本节**只建议**，从现有只读数据自动填「观察中」项；**不**自动改参、不写 config、不开 paper/live 闸。任何落地须走 **R4 人工确认**。

## 结构

每条建议为字典：`layer`（采集/指标/决策/执行/复盘）、`item`、`status`（观察中|建议落地|裁决）、`evidence`、`action_hint`。

自动观察项（有则填，无则占位）：采集 token_age 覆盖率（接 `pairs.pair_created_at`，观察 unknown 占比下降）与 `news_events.db` 条数；指标权重全 0 + 归因 liq 区分度；决策 JEV unique_token/门槛距离与退役规则提醒；执行 executed=0/paper 未开闸与拒绝分布；复盘 R3 skip 原因。

```bash
.venv/bin/python -m review.run --period 7d --report
# 可选：--optimize 以便 §10 引用真实 R3 skip reason
```

## §10 小节：基线滚动快照（PRIOR_STEP6）

`format_evolution_section` 在建议表后追加「基线滚动快照」：读 `prior.db.baseline_history` 最近 N 条（survive_1h/4h、窗长、稳定判定 `STABLE`/`SHORT`/`UNSTABLE_OR_SHORT`/`INSUFFICIENT_DATA`）。缺库/缺表则跳过并注明。**只观察不调权。**

## §10 小节：价格行为先验（PRIOR_STEP7）

`format_evolution_section` 在建议表后（baseline_history 附近）追加「价格行为先验」：读 `data/prior/exports/price_behavior.json`（窗长/SHORT、1h 收益、4h 拉升、mdd_4h、JEV 交叉一句）；建议表新增「价格行为（拉升/回撤）」行，状态=**观察中**。缺 json 则跳过并注明。**周报 §10 已挂载观察**；只观察不调权。

## §10 小节：指标加工层·周观察

`build_evolution_suggestions` 可并入指标层周观察行（`change_*` 区分度、`volatility_24h` 风险分层、`volume_strength`/`macd_hist` 可降权倾向、sentiment/spike 样本不足、本周无权重裁决）；`format_evolution_section` 表后追加「指标加工层·周观察」小节。**只观察不调权**；权重仍全 0；落地须走 R4。

## §6b：CEX 市场基准第二参照（CCXT 旁挂）

周报 §6「市场基准」后追加 **6b**：只读 `ccxt.db`（`ccxt_ticks` / `listings` / `ohlcv`），用最近一轮/所的 `change_24h_pct` 中位/均值与 §6 hot 并列对照。缺库/缺表优雅跳过。**只观察不调权、不进决策/告警。** 详见 `CCXT_SIDECHANNEL.md`。

## §8b：社媒突热 × 链上联动（B，只观察）

周报 §8 归因后追加 **「社媒突热 × 链上联动（只观察）」**：只读 `token_indicators.spike` / `spike_ratio`（A 层入库），汇总近窗 n_spike、有 JEV 数、change_1h/4h 上涨率。突热=滞后确认，非买入；**不调权、不告警、不进决策**。缺表/缺列/无行 → 显示「暂无数据」且 exit 0。详见 `SENTIMENT_SPIKE.md` · loader=`review.data.load_sentiment_spike_linkage`。
