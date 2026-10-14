# Indicators 加工层全流程审计（INDICATORS_AUDIT）

> 审计时间：2026-10-10（HKT）  
> 范围：indicator layer + 已有只读消费者。**不启用** scoring/alert 加权（`INDICATORS_WEIGHT_*=0`）。  
> **未停止** `main.py` / `indicators.run` / `sources.jev`；仅在需要时安全重启 `api_server`。

## 清单总览

| # | 项 | 状态 | 说明 |
|---|----|------|------|
| 1 | indicators package：technical / funding / sentiment / composite | **DONE** | tech/funding/sentiment 已实现；composite 原 stub → 本次补骨架（权重 0 时 no-op） |
| 2 | 表 `token_indicators` + `indicator_history` + `jev_signals` | **DONE** | 实库均存在；schema 由 `models/indicator_tables.sql` + migrate |
| 3 | 运行循环：`indicators --loop`、`sources.jev --loop`、stabilize main | **DONE** | 三进程在跑；daily_check 巡检 |
| 4 | API `/indicators` + `/top` + auth | **DONE** | 鉴权 `X-API-Key`；响应补齐 sentiment/composite 块 |
| 5 | grok_dd technical / funding / sentiment | **DONE** | `scripts/grok_dd_report.py` 三节齐全 |
| 6 | daily_check indicator / JEV / proc | **DONE** | 原有三进程+JEV+新鲜度；本次加 history / sentiment 覆盖 |
| 7 | `top.sh`、`--telegram` DD、admin Top board | **DONE** | `./top.sh`、DD `--telegram`、`static/admin.html` Indicators Top |
| 8 | AUTH_JEV / public mode（无错 key） | **DONE** | 见 `AUTH_JEV.md`；空 key=公开；改 key 只重启 jev |
| 9 | composite stub？hot history？on_indicator 门控？ | **DONE** | composite 骨架+权重 0；hot 已写 history；`INDICATORS_ON_UPDATE_HOOK` 默认关 |
| 10 | README / docs 索引 | **DONE** | README 增 Indicators 节 + 命令表链到本文 |

图例：DONE = 已完备；PARTIAL = 部分；MISSING = 缺失（审计后已补的标 DONE 并在「本次修复」列出）。

---

## 逐项详情

### 1. indicators package

| 模块 | 状态 | 备注 |
|------|------|------|
| `indicators/technical.py` | DONE | 分档 change / volatility / volume_strength；hot 写 history |
| `indicators/funding.py` | DONE | 只读 `jev_signals`；映射 `jev_*` |
| `indicators/sentiment.py` | DONE | 只读 `x_watch` + social alerts |
| `indicators/composite.py` | DONE（原 PARTIAL） | 加权合成骨架；`sum(weights)==0` 时整批跳过 |
| `indicators/base.py` | DONE | schema ensure、upsert、top 白名单 |
| `indicators/run.py` | DONE | `--full` / `--once` / `--loop`；tech→fund→sent→composite |

### 2. 表

- `token_indicators`：技术 + JEV + sentiment + composite 列（含 migrate）
- `indicator_history`：hot 归档（含 `jev_strength` / sentiment 快照列）
- `jev_signals`：由 `sources.jev` 写入，indicators funding 只读

### 3. 运行循环

```bash
# 当前期望（stabilize）
python main.py --helius-listen --sources x --loop --interval 60
python -m indicators.run --loop --interval 300
python -m sources.jev --loop --interval 900
python -m api_server   # 只读 API + admin
```

勿在改 JEV key / indicators 代码热修时停 main；api 变更可单独重启 api_server。

### 4. API

- `GET /indicators/top?chain=&sort=&limit=` — sort ∈ `change_5m|jev_strength|volume_strength`
- `GET /indicators/{chain}/{token_address}`
- 鉴权：与其它数据路由相同（`API_SECRET` → `X-API-Key`；`/health` 开放）
- 响应块：`technical` / `funding` / `sentiment` / `composite` + meta

### 5. grok_dd

`assemble_token_context` 拉取 `token_indicators`；markdown 含：

- 技术面 Technical
- 资金面 Funding (JEV)（可回落 `jev_signals`）
- 舆情面 Sentiment（无则「暂无数据」）

### 6. daily_check

`check_indicators_p0`：

- 三进程可见性（WARN 非 FAIL）
- `jev_signals` 覆盖
- `token_indicators` 新鲜度
- **新增** `indicator_history` 行数
- **新增** sentiment 填充计数

### 7. UX 只读消费者

| 工具 | 用法 |
|------|------|
| `./top.sh jev\|change5m\|volume [chain] [limit]` | 调 `/indicators/top` |
| `python scripts/grok_dd_report.py … --telegram` | 可选推 admin 频道 |
| Admin `/` → Indicators Top | 前端 `fetch` |

### 8. AUTH_JEV

- 未设 `JEV_API_KEY` → 公开扫描（与旧行为一致）
- 设 key 后按 `JEV_AUTH_MODE=header|bearer|query` 附加；日志脱敏
- **只重启** `sources.jev`，不停 main/indicators  
  详见 **AUTH_JEV.md**

### 9. Composite / history / hooks

| 项 | 行为 |
|----|------|
| `INDICATORS_WEIGHT_TECHNICAL/FUNDING/SENTIMENT` | **默认全 0** → composite no-op，不进 scoring/alerts |
| Hot history | technical / funding / sentiment（及未来 composite）在 hot 时 `insert_indicator_history` |
| `INDICATORS_ON_UPDATE_HOOK` | 默认 `0`；`auto_hooks.maybe_on_indicator` 门控 |
| `JEV_ON_NEW_LISTING` | 默认关；`auto_hooks.note_new_listing_for_jev`（已修缺失 import） |

### 10. 文档索引

- 本文 **INDICATORS_AUDIT.md**（运维入口；含「警示事例 → 指标层纪律」）
- STEP1 / STEP2 / STEP3 / P0 / LIGHT / AUTH_JEV（P-4 定价错误在 STEP2）
- **INDICATORS_FACTOR_CANDIDATES.md** — 机构供需 P-3 吸筹区回踩 / P-4 新鲜度衰减：factor_loop 候选登记（文档 only，不接线，权重 0，不算有效性）
- README「Indicators 旁路」节 + 命令总索引

---

## 本次修复（safe）

1. **`indicators/composite.py`**：实现加权合成骨架；权重全 0 时 `run_batch` 直接返回、不写库。
2. **`config.py`**：`INDICATORS_WEIGHT_*` 默认改为 `0`；新增 `INDICATORS_ON_UPDATE_HOOK=0`。
3. **`api_server.py`**：`_format_indicator_row` 补 `sentiment` + `composite`（及 `jev_funding_bias`）。
4. **`indicators/run.py`**：funding 尊重 `INDICATORS_WRITE_HISTORY_HOT`；composite 打日志；可选 hook 门控调用。
5. **`auto_hooks.py`**：补 `JEV_ON_NEW_LISTING` / `JEV_PENDING_PATH` import（原 NameError 被 pipeline 吞掉导致队列永无效）；加 `maybe_on_indicator`。
6. **`daily_check.py`**：`indicator_history` + sentiment 覆盖检查。
7. **`demo_indicators_p0.py`**：断言 API shape / DD 舆情节 / composite weight-0 no-op。
8. **README / env.example / deploy env**：Indicators 运维索引与权重说明。
9. **本文档**。

**未做（按范围）**：未改 `scoring.py` / 告警阈值；未把 composite 权重大于 0；未停 main/indicators/jev。

---

## 警示事例 → 指标层纪律

> 来源：执行单《预测市场经验落地执行单（Polymarket 指南 → 系统改进）》（2026-10-14）第四节警示案例，以及同单 P-2 / P-5。看板平仓旁证已在 PR #45。本节是指标 / 旁路层纪律，**不算策略有效性**。IC/IR 与统计参仍归 **10-19 周检**。不改 `INDICATORS_WEIGHT_*`、composite 默认、technical loop。

- **半胜仍亏 / 单笔抹盈**（书中约 51% 胜率仍大亏；本系统 paper 上单笔亦可抹掉全部盈利）：指标侧**不**用强信号当确定性。**信号强度 ≠ 胜率**（口径见 `INDICATORS_STEP2.md` P-4）。`INDICATORS_WEIGHT_*` 保持 **0**，直至 10-19 IC/IR + R3；不盘中追认统计参。
- **$0.95 黑天鹅 / 薄池打穿**（「那 5% 一旦出现，亏掉的不是 5% 而是 95%」；薄池单笔止损打穿、抹掉全部盈利）：tech 新鲜度与流动性字段是决策闸的输入。指标**不编造** liquidity（载荷缺 `liquidity` 就保持空）。`min_liq` fail-closed 归决策 [#41](https://github.com/tspike630/trade/pull/41)。本侧只保 hot tech 新鲜（[#40](https://github.com/tspike630/trade/pull/40) 已合）。
- **洗盘 / 单点操纵**（书中约 25% 交易量虚假，外加预言机投票权集中翻转）：多源旁路（DexScreener + CCXT prior，见 `CCXT_SIDECHANNEL.md`）是防单点，不是加权重救曲线。

### P-2（指针，不实现）

`review.factor_loop` 按因子 IC/IR 动态加权是 **design-after-10-19**：10-19 真库 IC/IR 出数之后才写设计稿（公式与阈值）。在此之前权重保持 0，不激活，不过门槛不生效。本文不实现权重更新代码。

### P-5（观察候选）

Polymarket 共识概率（例如「某币是否上币安」，价格 = 市场共识）只作为 factor_loop 的**观察因子候选**，须走验证流程，**不接入主决策**。书中警告：加密相关市场胜率约 60%，约 25% 交易量是洗盘，且有合规风险（中国大陆非法 / 香港灰色）；接入前先做合规评估。当前未接线。

---

## 运维速查

```bash
cd /workspace/dexscreener-crawler
source .venv/bin/activate

# 状态
pgrep -af 'main.py|indicators.run|sources.jev|api_server'
python daily_check.py

# 指标单次 / 全量
python -m indicators.run --once
python -m indicators.run --full          # 显式全量，无视 INDICATORS_ENABLED=0

# Top
./top.sh jev
./top.sh change5m solana 20

# API（密钥勿回显到日志）
curl -sS -H "X-API-Key: $API_SECRET" \
  "http://127.0.0.1:8080/indicators/top?chain=solana&sort=jev_strength&limit=5"

# 冒烟
python demo_indicators_p0.py

# 改 JEV key 后（只重启 jev）
pkill -f 'python -m sources.jev' || true
nohup .venv/bin/python -m sources.jev --loop --interval 900 >> logs/jev.log 2>&1 &

# 仅重启 API（指标响应格式变更后）
pkill -f 'python -m api_server' || true
nohup .venv/bin/python -m api_server >> logs/api_server.log 2>&1 &
```

### 环境变量（指标相关）

| 变量 | 默认 | 含义 |
|------|------|------|
| `INDICATORS_ENABLED` | `1` | loop/once 总开关（`--full` 仍可跑） |
| `INDICATORS_HOT/WARM/COLD_INTERVAL_SEC` | 300/1800/14400 | 分档刷新 |
| `INDICATORS_WRITE_HISTORY_HOT` | `1` | hot 写 `indicator_history` |
| `INDICATORS_WEIGHT_*` | `0` | composite 权重（全 0=关闭） |
| `INDICATORS_ON_UPDATE_HOOK` | `0` | 加工后 hook |
| `SENTIMENT_ENABLED` | `1` | 舆情加工 |
| `JEV_API_KEY` / `JEV_AUTH_MODE` | 空 / `header` | 见 AUTH_JEV.md |

---

## 验收建议

1. `python demo_indicators_p0.py` → `DRY_RUN_OK`
2. `python daily_check.py` → Indicators 相关无 FAIL
3. `./top.sh volume` → HTTP 200 + JSON items
4. 确认 `INDICATORS_WEIGHT_*=0` 且 `token_indicators.composite_score` 不因 loop 被批量写入
