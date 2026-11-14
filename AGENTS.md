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

`decision_engine/core/regime_strategy_router.py` 的生产字节是 `56a2df45`。本分支在那个分类器之后加了 `REGIME_V2_CONTRACT`。未设置时仍是生产分类和 0.55、鲸鱼分支。`decision_engine/run.py` 生产字节是 `a696ab8c`。本分支只多了隔离干跑旗标。

## t250u 草案（2026-11-14 11:56 HKT）

这是规则草案，不是已切主通道的实现。本次没有新策略类，没有改执行器，没有改生产 `engine.py`。源码默认仍是关。只有 `REGIME_V2_CONTRACT=1` 时，路由才使用定案仓位乘数，并记下定案止盈倍率。它不改 `take_profit_price`。成交均价上的止盈公式见 `docs/EXECUTION_PROTECTION_REVIEW_t250.md`，未批准前不产生订单。

杨的定案：单笔目标名义 = 实时可用或权益基数 × 24% × 入场档位 × 状态乘数。24% 是现场 CLI 事实，不是新的凯利批准，也不是把历史余额写死。纯现货，不对冲。状态 4 乘数改为 0.30。状态 1 为 1.00 / 止盈 1.00，取消此前鲸鱼 1.08 / 0.85 与止盈 1.22 / 0.72。状态 2 仓位 1.20、止盈 1.35，且必须走已实装的逼空入场。状态 3 仓位 1.15、止盈 1.25，保留已批准的联合盘口与巨量条件。状态 4 只允许最严苛的布林深跌 `LIMIT_MAKER`，止盈 0.85。状态 5 仓位 0，禁止新开多。必需数据过期或缺失要闭锁，不能默认状态 1。

退出保护已锁，不再作为待问：`TRAILING_RATCHET=0`，`MAX_HOLD_SEC=0`，`AUTO_FLATTEN_ON_OCO_FAIL=1`。棘轮改单和定时强平不再采用 11-11 AGENTS 的写法。生产执行器保持 `87ef5ed7` / `70aecd69` / `5c3022e0`，含 maxhold r2 与 PR220 回读，不退回 `aca99a95`。

## 配置能做的和做不到的

| 定案 | 草案配置 | 现有代码 | 结论 |
| --- | --- | --- | --- |
| 24% × 入场档 × 状态乘数 | `REGIME_V2_POSITION_PCT=0.24` 只被干跑脚本当成 `--position-pct` | fade 臂：`live_bnb_entry_zone.py` 大单乘数 1.0（L912）、中性 0.5（L643、L961）。路由在乘数 ≠ 1 时再乘状态乘数（生产路由 L639–640） | 比例相乘这条路径存在。实时权益不在决策路由里读。执行侧用 `BINANCE_SPOT_BOOK_USDT`，启动脚本另把 `BINANCE_SPOT_REST_USDT_FREE` 设成同一个数。两套口径不唯一，本草案不私选 |
| 板块簇和总敞口 | `LIVE_BNB_CLUSTER_CAP_ENABLED=0` | `portfolio_clusters.py` 只在旗标打开时按簇计口数，不是名义总敞口。默认阈值未按定案给定 | 不打开，避免猜上限 |
| 同币单笔单 OCO、跨 purpose 同币互斥 | 不改执行器 | `live_buy_guards.py` 在账本可读时挡住同一标的的第二笔买。PR218 的跨臂互斥在生产执行器里，本 PR 不改那些文件 | 保持，不在本草案里重写 |
| 180 秒 OCO 回读后才允许异币 | 不新设旗标 | `binance_spot_gate.py` 默认回读 TTL 180 秒。回读通过后是否允许下一笔异币，本 PR 没有再证 | 阻断：不能把 TTL 常数说成已经实现「顺风横向开仓」 |
| 永续关闭 | 两个永续旗标 = 0。契约打开时 `require_perp_beta_hedge` 改为 False | 永续引擎只在 `ENABLE_PERP_SHORT_ENGINE` 打开时读这个标签。Beta 上限函数不读它，因此标签本身不放宽 beta。现网该引擎为 0 | 标签不再当成已对冲。执行器未改 |
| 再挂靠 = 1 | 旗标保持 0，`REGIME_V2_REANCHOR_STATUS=blocked_behavior_mismatch` | 生产 `binance_spot_protect.py` L509–578：只在原止盈止损带包不住 `min/max(成交, 标记价)` 时，按信号价相对距离重算，止损距离下限 50 bps。不是每笔都按成交均价重锚，锚点也不是单独的成交均价。L3790 与 L5843 发生在首挂保护，不是持仓中途反复改单 | 阻断。只开旗标会谎称符合 |
| 止盈倍率写进价格 | 草案记下 1.00 / 1.35 / 1.25 / 0.85 / 不适用 | `_expand_regime_take_profit` 只在 Kelly 或 `EXPAND_REGIME_OCO_TP` 打开时外扩状态 2/3，且不用 `tp_multiplier`。这两个开关保持 0。成交均价上的倍率公式没有定义 | 阻断。不把倍率乘到绝对币价上猜 |
| 状态 1 取消鲸鱼分支 | `REGIME_V2_CONTRACT=1` 时不再使用 1.08 / 0.85 和 1.22 / 0.72 | 未设置该变量时，L355–391 的旧分支仍在 | 已接线。默认路径的回归测试仍覆盖旧分支 |
| 状态 1 PostOnly 与盘口门 | 两个 PostOnly 旗标和 `BINANCE_SPOT_ORDERBOOK_GATE=1` | PostOnly 只把中性限价从 `LIMIT` 改成 `LIMIT_MAKER`（入场区 L630）。大单仍是 `MARKET`（L907） | 部分。不能说状态 1 全部变成 PostOnly |
| 状态 2 逼空入场 | 契约打开时该路由 `allow_buy=False`，原因 `state2_entry_module_not_live` | `funding_z_fade_v1` 的阈值是绝对值 `>= 2.0`（`funding_z_fade.py` L28），不是 `<= -1.8`，也不读 COT。现网发射器不调用它。生产包的 `strategies/` 没有这个文件 | 闭锁。没有把 fade 标签当成逼空实现，也没有改 2.0 |
| 状态 3 前 65% / 后 35% 与联合条件 | 不新设阈值 | 代码拒绝 `xs_rank_pct_1h < 0.35`（L293）。薄盘是 `obi <= -0.25` 且吃单缺失或 `<= 0.45`（L308）。巨量用无历史时 3.80，且要鲸鱼有值并 `<= 0.65`（L322） | 名次线与「剔除后 35%」一致，前提是名次越大越强。联合条件是 13:08 已批准的实现，定案正文没有改写它们 |
| 状态 4 乘数 0.30、深跌档 | 契约打开时该放行路由关闭，原因 `state4_deep_entry_undefined`，倍率记下 0.30 / 0.85 但不发出买单 | 未开契约时仍是 0.55。`low - 0.5 * span` 不是已测入场档 | 闭锁。没有把那条 deep 价格当成入场 |
| 状态 5 与过期闭锁 | 契约打开时，缺分类输入的状态 1 不再放行，原因 `regime_required_inputs_missing`。危机路由仓位乘数为 0 | 未开契约时缺输入仍落到状态 1。没有名为「12 道门禁」的清单 | 缺输入闭锁已接线。12 道仍只列得出的真实模块，不凑数 |
| 止盈倍率 | 契约打开时状态 1/2/3/4 的 `regime_tp_multiplier` 为 1.00 / 1.35 / 1.25 / 0.85 | `take_profit_price` 保持入场模块原值 | 倍率已驱动。价格未改，避免未定义的成交均价公式进入订单 |

状态 5 代码里实际会停多的条件（生产路由 L130–183），不是 12 道的证明：宏观 `macro_halt`、解锁黑窗、跨所错位 `>= 35` bps、`xs_systemic_crash`、多头清算瀑布、负 gamma 且 `dvol_z >= 2`、NVDA `<= -2%`、SMH `<= -2%`。新闻危机覆盖是另一层（`_decision_after_news`）。广度 `< 12%` 写在模块文档 L37，if 里没有。其余「12 道」没有出处，不能宣称已经齐。

## OCO / kill 红线（保留）

- `LIVE_TRADING_DISABLED` 存在时不得启动 emitter，也不得删除这个文件。它只挡新买；`BINANCE_SPOT_BLOCK_SELL_ON_KILL=0`，保护性卖出不被杀开关本身挡住。
- 原生 OCO 回读和逐 purpose 账本继续有效。`BINANCE_SPOT_TRAILING_RATCHET=0`，`BINANCE_SPOT_MAX_HOLD_SEC=0`，`BINANCE_SPOT_AUTO_FLATTEN_ON_OCO_FAIL=1`。再挂靠旗标保持 0，原因见上表。
- 源码里这些开关未设置时的默认不变。历史 switches-off 测试不得靠改默认值放行。
- 干跑不调用私有交易 API，不碰生产账本。验收必须 `--dry-run` 且 `--output-db` 在 `/tmp` 或 `/var/tmp`。隔离输出库不是消费者输入。无信号只说明这次影子没有候选，不是五态验收通过。

## 显式接通的门禁

`configs/live_regime_v2.env` 只在被启动脚本 source 时生效：

| 开关 | 源码未设置 | 11-14 进程 | v2 配置 |
| --- | --- | --- | --- |
| Kelly / EXPAND / PERP / 白名单 / 逆波 / 簇上限 / SOR | 关 | 0 或未设置 | 0。定案没有打开 SOR |
| TRAILING_RATCHET / MAX_HOLD_SEC | 关 / 0 | 0 | 0 |
| AUTO_FLATTEN_ON_OCO_FAIL | 未设置则关（保护文件 L74–78） | 1 | 1 |
| REANCHOR_OCO_ON_FILL | 未设置则关 | 0 | 0。定案目标是 1，行为不符，见上表 |
| `BINANCE_SPOT_ORDERBOOK_GATE` | 关 | 未设置 | 1。只覆盖执行侧盘口检查 |
| `BINANCE_SPOT_POST_ONLY_LIMIT` 与 `LIVE_BNB_POST_ONLY_LIMIT` | 关 | 未设置 | 1。只改中性限价，不改大单市价 |
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

## 数据刷新（只报告，不执行）

- 新闻最新 `fetched_at` 是 2026-11-13 19:33:42 HKT。当时没有新闻采集进程，停因未在包里写明。单次补数命令是 `.venv/bin/python -m sources.news --once --db /workspace/dexscreener-crawler/news_events.db`。本环境不跑。
- LSEG 最新 bar 是 2026-11-09 18:00 HKT。现网脚本是 `import_lseg_bars_incremental.py`，需要一份含 `lseg_cityu_core_refresh_bars.csv` 和 `lseg_cityu_incremental_bars.csv` 的新 CSV 目录。fetch 脚本不在现网，也没有已验证的 CodeBook 会话。不新登录、不订阅。
- FRED 在 `public_derivatives.db` 的 `macro_net_liquidity`，最新 as-of 是 2026-11-13。PID 1664610 每 300 秒仍在采集。这不是停更，不开第二个 loop。

## 仍未切正式服务

本机没有三份特征库和 `dexscreener.db`。影子干跑只能证明隔离和缺文件，不能代替现网新鲜样本上的五态验收。要在现场做只读干跑，最小输入是：一份脱敏的指标库（含 `decision_volume_z` 与 K 线）、`public_derivatives.db`、`news_events.db`、`data/lseg/lseg_macro.db`，以及每个 purpose 的只读账本副本。不要把这些生产文件复制进本 PR。
