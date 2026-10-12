# CCXT × Prior Step 2 — CEX listings 联合归因（观察 only）

> 状态：落地 2026-10-12  
> 对应：`CCXT_SIDECHANNEL.md` §7 延后项；QUANT/方案2 Step 2  
> **严格旁路**：只读 `ccxt.db` / `prior.db` / `dexscreener.db`；只写 `data/prior/exports/`  
> **默认关 / 不调权**：不接评分、决策、告警、周报自动挂载

---

## 1. 交付物

| 路径 | 作用 |
|------|------|
| `scripts/prior_cex_listing_join.py` | one-shot 只读 join 分析 |
| `data/prior/exports/cex_prior_join_YYYYMMDD.md` | 人读摘要 |
| `data/prior/exports/cex_prior_join_YYYYMMDD.json` | 全量 rows + 分布 |
| `data/prior/exports/baseline_params.json` → `cex_listing_join` | 可选 compact 摘要（不删其它键） |
| 本文 / `CCXT_SIDECHANNEL.md` §7 更新 | 口径与默认关声明 |

---

## 2. 口径

1. **CEX 事件时间** = `ccxt_listings.first_seen_ts`（epoch ms）  
   - 含义：旁路**首次见到**该 `(exchange, symbol)`  
   - **≠** 交易所官方上线公告日；冷启动会把 BTC/ETH 等存量也记成 first_seen
2. **Symbol 归一** = `BASE/QUOTE` 取 `BASE.upper()`；`USDT/USDC/TRY/BRL…` 作 base 时标记 `quote_like`，不计「新币可匹配」
3. **链上匹配**（主库只读）  
   - `upper(pairs.raw_json.baseToken.symbol) == BASE`  
   - 多撞车：canonical = `liquidity.usd` 最高，并列取最早 `pair_created_at`  
   - 报告 `pair_collision_n` / `pair_unique_mints`
4. **Prior 匹配**  
   - `upper(token_ohlcv.ticker) == BASE`；多行取最早 `discovery_ts`
5. **延迟**  
   - `cex_first_seen − pair_created_at`（及 prior `discovery_ts`），单位小时  
   - mega-cap / 高 collision 样本**无因果上市延迟**解释力
6. **上线后短窗收益**（15m / 1h / 4h）  
   - `ccxt_ohlcv`：以 first_seen 为 t0 的 forward close  
   - `pair_history`：matched canonical pair 的 `price_usd`  
   - 旁路刚启动时 forward bars 常为 0 → n 可为 0（如实报告）
7. **基线对照**  
   - 并列 `graduation_rate` / `same_day_death_rate` / gmgn peak / `price_behavior`  
   - **只观察，不调权、不算 uplift**

---

## 3. 运行

```bash
cd /workspace/dexscreener-crawler
.venv/bin/python scripts/prior_cex_listing_join.py
# 不写 baseline_params：
.venv/bin/python scripts/prior_cex_listing_join.py --no-merge-baseline
```

环境：`CCXT_DB_PATH` 可覆盖旁路库路径（与 Step 1 一致）。

---

## 4. 默认关 / 不做

- 根 `config.py` / `main.py` / `review.run` **不**自动调用本脚本  
- 不改 `INDICATORS_WEIGHT_*`、不进 trading / alerts / 业务四库  
- **周报 hook 留给复盘**：§6b 仍只做 CEX 市场基准第二参照；本 join 是否旁挂由复盘决定  
- 小 n、symbol 撞车、first_seen≠真上线 → 验证「链上新币 → CEX 上线 → 拉升」前禁止加权

---

## 5. Caveats（写进每次 export）

- `n_listings` 当前量级 ~40，不可外推  
- 同 ticker 多 mint（尤其 SOL/BTC/…）→ 匹配率高但语义脏  
- 抽样偏 CEX 高流动性现货，与 memecoin prior 总体不同  
- 需要真上线事件源（公告 / 交易所 listing API）后再谈因果与加权
