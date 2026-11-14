# AGENTS.md / CLAUDE.md — 五态阶段章程（2026-11-14）

> 本文件替换 11-10 说明书里「凯利底座已经下发现网、355 项测试即代表实盘五态」的写法，也替换 OCO 修复阶段的临时锁：不改策略、把 24% 当成不可评审的冻结、四个新开关永久不许点名、以及用 main 旧执行器哈希（`d8c31aca` / `f7e2e3c6` / `6686b214`）当现网门禁。那些哈希不是 11-14 生产字节。生产树没有本文件。11-11 work-canonical 副本不是全部现行规范。

## 阶段

杨于 2026-11-14 授权从 OCO 修复阶段进入更新后的五态策略阶段，停止按旧逻辑继续开仓。本仓库改动只供审核。不部署、不合并、不打发布 tag、不下单、不启永续。

现网新开仓已由父级暂停：2026-11-14 11:16:32 HKT 创建 `LIVE_TRADING_DISABLED`，两个旧 emitter 于 11:16:39 退出，保护消费者继续。11:18 官方账户空仓、无挂单，仅 PEPE 0.40 尘。

## 安全基线（本 PR 不替换）

| 文件 | main 字节 | 11-14 生产字节 |
| --- | --- | --- |
| `execution/executors/binance_spot.py` | `d8c31aca…` 186699 | `87ef5ed7…` 310144（PR220 head `aaf918326b82`，09:47 安装） |
| `execution/executors/binance_spot_protect.py` | `6686b214…` 42031 | `70aecd69…` 46540 |
| `execution/executors/binance_spot_gate.py` | `f7e2e3c6…` 28458 | `5c3022e0…` 52312 |
| `execution/gateway.py` | `e99f6d6f…` | `06ee774e…` |
| `execution/ledger.py` | `5e07b430…` | `6039fd02…` |
| `execution/run.py` | `fc30c933…` | `ff44a21b…` |
| `decision_engine/core/engine.py` | `bac2a059…` | `2b578762…`（生产多一次下单后的 beta 复检；main 没有） |
| `decision_engine/core/live_buy_guards.py` | `6b3023ed…` | `a550474c…` |
| `decision_engine/core/protection_halt.py` | `947465bc…` | `b3c02ee7…` |

PR220 的 gate 重建值 `fa2fc3dd…` 不是生产 `5c3022e0`。不得用 PR220 或 main 覆盖生产执行器、gate、gateway、ledger、engine、guards、halt。PR218 与 PR220 都未合并。PR220 是 LIMITED-PASS：专项 3 项由失败变通过，execution suite 相对未打补丁生产基线仍是 58 失败/错误。

`decision_engine/core/regime_strategy_router.py` 生产与改动前 main 同为 `56a2df45`。本分支只在接受的买单上追加 `regime_classification`，不改分类、仓位或止盈公式。`decision_engine/run.py` 生产与改动前 main 同为 `a696ab8c`。本分支只增加 `--regime-v2-isolated-purpose`，不改默认的 `shadow_010` 隔离。

## OCO / kill 红线（保留）

- `LIVE_TRADING_DISABLED` 存在时不得启动 emitter。它只挡新买；`BINANCE_SPOT_BLOCK_SELL_ON_KILL=0`，保护性卖出不被杀开关本身挡住。
- 原生 OCO 回读、逐 purpose 账本、急停继续有效。`BINANCE_SPOT_TRAILING_RATCHET=0`，`BINANCE_SPOT_MAX_HOLD_SEC=0`，`BINANCE_SPOT_REANCHOR_OCO_ON_FILL=0`。
- 源码里上述开关以及 Kelly、EXPAND、永续、盘口、Maker、SOR 的默认都是未设置即关。历史 switches-off 行为不得靠改默认值放行。
- 干跑不调用私有交易 API，不碰生产账本。验收必须 `--dry-run` 且 `--output-db` 在 `/tmp` 或 `/var/tmp`。`--output-db` 与 `--db` 不同时，原逻辑会把 purpose 改写成 `shadow_010`，从而不走进五态路由。验收脚本额外传 `--regime-v2-isolated-purpose`，只在 dry-run、输出库隔离、且 purpose 为 `live_bnb_boll_fade` 或 `live_bnb_boll_classic` 时保留原 purpose。不带该旗标的旧隔离行为不变。这个输出库不是消费者输入。

## 策略入口与仓位（待裁定的保持原值）

没有一份完整新规范定义独立入场模块。13:08 裁定保留已安装路由：状态 3 薄盘口与巨量是联合条件，巨量但鲸鱼缺失仍放行；状态 1 鲸鱼牛向 1.08、熊向区间 `0.45 < whale_div <= 0.72`、浅跌 `boll_z > -2.25` 拒绝。playbook 字符串不是另一个 `strategy_id`。入场仍是 `tech_boll_fade_v1` + `live_bnb_entry_zone`。只改名不能通过验收。

2026-11-12 13:38 基准是账面的 24%（当时约 141.88 USDT，BOOK 591.18409897）。解除「24% 不动」的临时冻结没有指定新比例。`configs/live_regime_v2.env` 继续传 `--position-pct 0.24`，并标明 `pending_ruling`。不得自行改成 18.5%、20%、45%、35%、5%、95% 或三阶凯利。`USE_OPTIMAL_KELLY_MATRIX=0`。`EXPAND_REGIME_OCO_TP=0`（它只外扩状态 2/3，不会把状态 4 止盈收成 0.80）。状态 4 的 `tp_multiplier=0.80` 与 `require_perp_beta_hedge` 仍是盖章。0.80 未改写 `take_profit_price`。永续未授权：`ENABLE_PERP_SHORT_ENGINE=0`，`BINANCE_PERP_SHORT_LIVE_ENABLED=0`。盖章不是已对冲。

缺分类特征时，路由仍会落到状态 1 标准剧本。这是现有代码，不是新政策。验收把这种买单标成 `state1_unconfirmed` 并阻断主通道。13:08 已允许的缺项（状态 3 缺 `obi`、缺 `volume_z`、巨量但鲸鱼缺失）不因此改成拒绝。

## 显式接通的门禁

`configs/live_regime_v2.env` 只在被启动脚本 source 时生效：

| 开关 | 源码未设置 | 11-14 进程 | v2 配置 |
| --- | --- | --- | --- |
| Kelly / EXPAND / PERP / 白名单 / 逆波 | 关 | 0 | 0 |
| TRAILING_RATCHET / MAX_HOLD_SEC / REANCHOR | 关 / 0 | 0 | 0 |
| `BINANCE_SPOT_ORDERBOOK_GATE` | 关 | 未设置 | 1 |
| `BINANCE_SPOT_POST_ONLY_LIMIT` 与 `LIVE_BNB_POST_ONLY_LIMIT` | 关 | 未设置 | 1 |
| `BINANCE_SPOT_SOR_MAKER_FALLBACK` | 关 | 未设置 | 1 |
| `DECISION_MACRO_GATE_ENABLED` + `DECISION_MACRO_DB` | 关 | 未设置 | 1，`data/lseg/lseg_macro.db` |
| `PUBLIC_DERIVATIVES_DB_PATH` | 默认同名相对路径 | 未设置 | `data/counterparty/public_derivatives.db` |
| `NEWS_EVENTS_DB_PATH` | 仓库根 `news_events.db` | 未设置 | `news_events.db` |
| 账本 | 单数 `DECISION_EXECUTION_LEDGER` | 每 purpose 一个库 | 配置点名 fade 与 classic 两个路径；干跑改写到隔离目录 |

`DECISION_MACRO_CITYU_V2_GATE`、`DECISION_MACRO_MICROSTRUCTURE_GATE`、`LIVE_BNB_PORTFOLIO_BETA_GATE` 没有新的点名授权，配置里保持关闭。

## 特征新鲜度

文件 mtime 不是新鲜度。生产三库在现网存在。本机 checkout 没有这些库文件，证据里的新鲜样本不能当成这里已经有库。

11:34:40 HKT 补充包（`now_ms=1794627280536`）按源码已有窗口：

- OBI `obi_top5_source_ts_ms` 11:31:08、对手盘 `ts_ms` 11:28:51、`volume_z` 的 `source_close_time_ms` 10:59:59，都在 `BINANCE_INDICATORS_FRESH_SEC` 默认 3600 秒内。`computed_at` 11:11:57 也落在同一窗口，但读取用的是收盘时钟，不是 computed_at。
- 新闻 `fetched_at` 2026-11-13 19:33:42 HKT，超过 `NEWS_WINDOW_MIN=90`。当时没有新闻采集进程。
- LSEG `cross_asset_bars` 最新 2026-11-09 18:00:00 HKT，超过传染窗口 30 分钟。当时没有 LSEG 采集进程。
- COT 在 `cityu_institutional.db`，`lseg_cftc_cot_positioning.max_date=2026-11-03`。读取用 `due_cftc_asof`（周二报告、周五 15:30 纽约发布），源码没有秒级 TTL，这里不另设。
- `token_unlock_schedule` 没有采集时钟。`next_unlock_utc` 在读取时重算。mtime 不算新鲜。

`data_adapter`、`binance_indicators`、`cityu_institutional_alpha`、`macro_circuit_breaker`、`signal_bus`、`perp_microstructure_and_beta_hedge`、`volume_z_1h`、`wall_mid_pricing` 与补充包生产字节一致。`engine.py` 生产仍是 `2b578762`（beta 复检），本分支不覆盖它。

## 验收

```bash
python3 -m unittest decision_engine.tests.test_regime_v2_cutover
bash scripts/regime_v2_kill_dry_run.sh \
  --kill-file /tmp/regime-v2-kill/LIVE_TRADING_DISABLED \
  --output-db /tmp/regime-v2-isolated/decision.db
```

`cutover_allowed` 仅在没有阻断时为真。缺库、过期、无信号、未确认状态 1、或仓位等于未裁定凯利比例，都不得切主通道。

## 等待裁定

本地材料里没有独立的每态入场规范。路由器文档和实现（乘数 1.00/1.20/1.15/0.55/0）与 11-11 AGENTS 的凯利底仓、三阶棘轮、5% 和永续比例不是同一份契约。在杨确认哪一份是权威契约之前：不新建入场模块，不把 `tech_boll_fade_v1` 改名，不改 24% 基数，不开永续。状态 4 的 0.80 仍只盖章。

本机仍没有三份特征库和 `dexscreener.db`。验收报告同时记下「现网证据里库存在且部分时钟新鲜」和「这里的文件不存在」。后者继续阻断主通道。
