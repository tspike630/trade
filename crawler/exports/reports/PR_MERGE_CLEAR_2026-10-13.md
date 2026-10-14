# PR 合并清账 + 观察期安排（2026-10-13）

> 用途：清掉挂账的 4 个 PR，合并后进入观察期；本单可原样转执行 AI。

---

## 一、合并指令（给执行 AI）

**按序 squash merge 以下 4 个 PR：**

| 顺序 | PR | 内容 | 验收状态 |
|------|-----|------|----------|
| 1 | PR22 | 方案 3：买卖因子解耦（12 策略注册） | 已验收（R1-R7 + 真库 R6） |
| 2 | PR23 | 方案 4：复盘因子分层（§11 + IC） | 已验收（7/7） |
| 3 | PR24 | 5-2a：order_events 订单事件流 | 已验收（6/6） |
| 4 | PR #28 | data_adapter 列名对齐（boll_upper/boll_lower） | 已确认一致 |

**说明**：
- 目录隔离（decision_engine strategies / review / execution / data_adapter），冲突风险低
- 若真冲突：以「两棵树最新语义 + 合前验收为准」解决并标注
- 合 PR 前现网行为不变

**合后冒烟（回传输出）**：
```bash
cd /workspace/dexscreener-crawler
.venv/bin/python -m decision_engine.run --strategy jev_strength_v1 --dry-run --once   # exit 0
.venv/bin/python -m execution.run --once                                              # 幂等 0 条
.venv/bin/python -m review.run --period 7d --report                                   # exit 0
# indicators loop 日志 err=0 确认
```

**回传**：4 个合并 commit 列表 + 冒烟结果。

---

## 二、合并后（5-2b 待落地）

合并稳定后按 `QUANT_REFACTOR_PLAN_05_STEP2B.md` 落地：
- backtest `_manage_exits` 改调 `execution/exits.scan_exits`（回测/实盘同引擎闭环）
- 验收 V0-V5（核心 V1 等价回归：bars/signals/opens/closes/reason 分布/final_equity 一致）

---

## 三、观察期时间线（自动触发，无需手动）

| 时间 (HKT) | 事件 | 首次 | 意义 |
|-----------|------|------|------|
| **10-14 周三 10:19** | JEV 信号采样 | ✅ 首跑 | 攒过滤后信号（喂 JEV 门槛） |
| **10-17 周六 09:15** | 先验基线成熟日重算 | ✅ | 基线更新（GMGN n=956 + self_survival n=9317） |
| **10-19 周一 09:15** | 周检首跑 | ✅ 首次带因子进化 | JEV 双门槛 + 真库 IC/IR + factor_loop 首跑真数据，一次全出 |

**到点由主 Agent 回传数字并更新登记表**（WAITING_ITEMS_REGISTRY.md）。

---

## 四、当前全部闭环状态

| 方案 | 状态 |
|------|------|
| 方案 1 指标层 pandas-ta（旁路版 + ADX/ATR） | ✅ 已验收（本次） |
| 方案 2 CCXT 旁路 | ✅ 闭环（Step1-4） |
| 方案 3 因子解耦 | ✅ 闭环（PR22 待合并） |
| 方案 4 复盘分层 + IC 历史 | ✅ 闭环（PR23 待合并 + 4-2 周检联动已挂） |
| 方案 5 执行层同引擎 | ✅ 5-1/5-2a 已验收；5-2b 待合并后落地；5-2c 实盘待开闸 |
| 方案 6 JEV 半自动进化 | ✅ 已验收 + 周检已挂 |

**待办只剩**：① 4 PR 合并 + 冒烟 ② 5-2b 落地 ③ 观察期数据积累 ④ 实盘（11 月中条件）。

---
*维护：主 Agent · 2026-10-13。观察期触发后更新本表与登记表。*
