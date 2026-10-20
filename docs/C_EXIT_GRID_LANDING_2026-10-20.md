# C 出口格 · 最小落地说明（旁簿）· 2026-10-20

> **状态：已挂旁簿骨架（可逆）· 主簿 / `.env` / live 未动**  
> 口径：`SL∈{4%,5%} × hold∈{60,90}`，**TP=10% 不动**；独立 tag/簿；live / #71 / 权0 / 日亏200 钉死。

## 1. 挂旁簿怎么做

| 项 | 约定 |
|---|---|
| **主簿** | 现 `paper_trading.db` / 现网 loop：**仍用** TP10 / SL5 / hold60，零改 |
| **旁簿** | `paper_trading_c_exit.db`（四 `grid_tag` 分账户现金）；**禁止**与主簿共现金权益 |
| **信号源** | 同一 `decision_log` `purpose=live_candidate` 流只读消费；旁簿**不写**主 `execution_ledger` |
| **四格 tag** | `c_sl4_h60` · `c_sl5_h60`（=主参对照）· `c_sl4_h90` · `c_sl5_h90` |
| **size** | 与主簿同 `size_pct` / 名义口径，**不**借 C 格抬仓 |
| **风控** | 旁簿自带日亏帽 **200**；单笔 ≤−$500 / 累计 ≤−$400 → `halted`；**不**抬主簿日亏 |

## 2. 满几笔才比

| 门槛 | 值 | 否决 |
|---|---|---|
| **每格最少平仓** | **≥30** 笔（四格合计 ≥120；若入口空窗可延，不降门槛） | 未满就改主路径 |
| **主比指标** | ①ΣPnL ②止损占负（`stop_loss` Σ / 负贡献）③单笔止损峰值 ④胜率 | 只报胜率不报止损占负 |
| **对照** | 同窗主簿 `c_sl5_h60` 影子 vs 真主簿应一致（接线验收） | 旁簿对照格与主簿分叉大 → 先修接线 |
| **OOS 意识** | 前 50% 笔定候选、后 50% 验证；全窗样本内最优**不**直接升主 | 拿全窗最优当「已证明边」 |

## 3. 回滚 / 停旁簿条件（触发任一条即停该格或全停）

1. 任一格单笔止损 ≤ **−$500**（或相对名义 ≤ −15%）  
2. 旁簿累计实现 ≤ **−$400**（约 2×日亏帽）  
3. 接线污染主簿权益 / 误改 `.env` / 误开 live  
4. 入口长期 `above_threshold=0` 导致 72h 仍零成交 → **暂停扩格**，不降笔数门槛硬判  

回滚动作：停旁簿进程；主簿与 `.env` 保持 TP10/SL5/hold60；文档标「C 格中止 + 原因」。

## 4. 升主路径（另五层批 + 杨点头）

仅当：每格满笔 + 后半段 OOS 上「止损占负↓ 且 ΣPnL≥主簿对照」→ 提案改主簿**一格**参（一次只动 SL 或 hold 之一更稳）。**不**借升主顺手抬 MAX_OPEN / 松日亏 / soft-skip Phase1。

## 5. 启动（本机旁簿）

工作目录：`/workspace/dexscreener-crawler`（`LIVE_TRADING_DISABLED` 在位；**不要**停主 loop）。

```bash
# 一次性：建空旁簿 + 游标对齐主 execution_ledger
.venv/bin/python -m execution.c_exit_shadow_run --init

# 单步 / 常驻（写旁簿 only）
.venv/bin/python -m execution.c_exit_shadow_run --once
.venv/bin/python -m execution.c_exit_shadow_run --loop
# 建议后台：
# nohup .venv/bin/python -m execution.c_exit_shadow_run --loop >> /workspace/out/c_exit_shadow.log 2>&1 &

.venv/bin/python -m execution.c_exit_shadow_run --status
```

代码入口（均不改主路径行为）：

| 文件 | 作用 |
|---|---|
| `execution/c_exit_grid.py` | 四格常量 + 路径重放 helper |
| `execution/c_exit_shadow_ledger.py` | 旁簿 schema / 开平 / 日亏与回滚 halt |
| `execution/c_exit_shadow_run.py` | `--init/--once/--loop/--status` |
| `execution/c_exit_offline_replay.py` | 主簿已平 142 笔四格离线重放 |

离线对照报告：`docs/c_exit_grid_offline_2026-10-20.md`（仓内副本；本机亦可另存 `/workspace/out/` 同名文件）。看板回退数字与 `dashboard/fixtures/c_exit_grid_offline_2026-10-20.csv` 对齐。

## 6. 离线摘要（2026-10-20，粗路径）

| tag | Σpnl | Δ vs c_sl5_h60 | 止损占负 | TP/SL/MH |
|---|---:|---:|---:|---|
| `c_sl4_h60` | -1144 | +11 | 81.6% | 2/11/128 |
| `c_sl5_h60` | -1155 | 0 | 80.8% | 2/10/129 |
| `c_sl4_h90` | -538 | +617 | 87.6% | 10/17/115 |
| `c_sl5_h90` | -547 | +608 | 86.1% | 10/15/117 |

> hold=90 在本样本路径近似上 Σpnl 明显好于 60，但止损占负未必下降；**满笔 + OOS 前不升主**。`c_sl5_h60` 与 M2 基线重放一致（−1155）。

## 7. tip / trade PR

旁簿实验与看板卡分开落地。看板 PR 只挂只读 OVERVIEW 卡，不带密钥、不提交 `*.db`、不改主 TP/SL/hold。

## 8. 看板 OVERVIEW

只读卡标题「C 出口旁簿」。有 `paper_trading_c_exit.db`（或 `C_EXIT_SHADOW_DB`）且该格 closed ≥30 时用实盘；否则回退 §6 离线路径重放，并写明「路径重放非旁簿实盘」。

卡面始终钉：**假设旁簿≠已改主闸** / 只读旁证 · 不改 `.env` · 不升主。

回滚线在卡上可见：单笔≤−$500 / 旁簿累计≤−$400 / 污染主簿 → 停。离线重放的 Σpnl 不拿来触发这条实盘回滚。
