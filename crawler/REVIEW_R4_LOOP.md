# 复盘优化层 · R4 迭代闭环机制

> 状态：机制定稿（2026-10-10）
> 定位：把 R3 参数建议表 → 机制自动裁决 → 落地配置，全程留痕、可回滚；**实盘闸门永远人工确认**
> 原则：防过拟合靠「机制闸」而非「人工逐条确认」；人只对「开实盘」表态

---

## 1. 闭环总览

```
R3 建议表 (in/out-sample 分开)
   ↓
机制裁决（自动，三道闸）          ← 取代逐条人工确认
   ├─ REJECT：样本外不达标 / 稳健性差 / 跨量级变更 → 丢弃 + 留痕
   └─ ACCEPT：达标微调 → 写入配置（仅 dry-run/模拟盘生效）
   ↓
params_audit 留痕 + 旧配置备份
   ↓
每周摘要：本周变更一行（你不回 = 默认继续）
   ↓
开实盘闸门 → 永远人工确认（唯一需要你表态的点）
```

## 2. 三道自动裁决闸（机制判断，无需人看）

| # | 闸 | 规则 | 结果 |
|---|----|------|------|
| 1 | 样本外闸 | out_of_sample 关键指标（胜率/收益）不显著优于 in_sample 或低于阈值 | **REJECT** |
| 2 | 稳健性闸 | 相邻网格点核心指标相对跳变 ≥25%（`score_robustness` 高风险） | **REJECT** |
| 3 | 量级闸 | 参数变更跨量级（如 threshold 0.15→0.30、sl 翻倍、结构变更） | **REJECT**（转人工） |

达标微调（如 threshold 0.15→0.17、tp 10→12，样本外验证通过）→ **ACCEPT**，自动落地。

**裁决日志**：每次裁决写一行 `params_audit`：`decision=ACCEPT|REJECT` + `reason` + `param_changes`。

## 3. 审计与回滚（自动，不用人管）

### 3.1 params_audit 表（或 audit JSON，二选一按仓库习惯）
| 列 | 示例 |
|----|------|
| `ts` | 2026-10-17T09:30:00Z |
| `source_run` | R3 run_id / 建议表 id |
| `decision` | ACCEPT / REJECT |
| `reason` | out_of_sample_fail / robustness_high_risk / magnitude_change / ok |
| `strategy_id` | jev_strength_v1 |
| `old_value` / `new_value` | 0.15 → 0.17 |
| `confirm` | auto / manual |
| `note` | 可选理由 |

### 3.2 回滚
- 每次 ACCEPT 前自动备份旧配置：`configs/params_backup_<ts>.json`
- 回滚命令（或手动替换）：`apply_params.py --rollback <backup_file>`
- 保留最近 N=20 份备份

## 4. 落地范围（灰度）
| 层 | ACCEPT 后是否生效 |
|----|-------------------|
| dry-run / 模拟盘（paper） | ✅ 立即生效（默认） |
| 决策引擎 live 信号 | ⏸ 下一轮周检确认后再谈 |
| 实盘闸门（paper_enabled / pm_enabled / live） | ❌ **永远人工确认** |

## 5. 每周摘要（你唯一要扫的东西）
周报新增一行：
```
参数变更：本周 ACCEPT 2 项（jev_strength_v1.min_strength 0.15→0.17, tp 10→12；样本外通过），REJECT 1 项（稳健性差）；无实盘闸门变更。
```
- 你不回 = 默认继续，无需任何操作
- 想看细节 → 翻 `params_audit`

## 6. 实盘闸门人工确认（唯一硬性人工点）
触发条件（全部满足才出现）：
1. R3 正式寻优出建议表，样本外验证通过
2. 模拟盘实跑 ≥2 周，表现与回测偏差可解释
3. 五层闸门 + 网关 + position_manager 验证过
4. 小资金灰度（单笔 5U、单日 20U）先跑

**此时系统会明确问你**，你回一个字才开；其他任何情况都不动闸门。

## 7. 与 R3 的关系
| | R3 | R4 |
|---|---|---|
| 产出 | 建议表（in/out 分开）+ 稳健性评分 | ACCEPT/REJECT 裁决 + 落地 + 留痕 |
| 触发 | 历史≥7d + 周检跨门槛 | R3 出表后自动 |
| 人工 | 无 | 仅实盘闸门 |

## 8. 验收
- [ ] 三道闸规则可测试：构造「样本外差 / 稳健性差 / 跨量级」三种建议 → 均 REJECT 且留痕
- [ ] 达标微调 → ACCEPT + 备份 + params_audit 三行齐全
- [ ] 实盘闸门在无人工确认时永不开启（代码层硬约束）

## 9. 警示事例不进盘中改参

不在当次会话另加第三个统计参。`factor_loop` 的 `accepted` 不是 `activated`。10-19 只接受指标已方向校准（M-2）且已算子化（M-3）的候选，见 `docs/REVIEW_RESEARCH_MAPPING.md`。临时 `purpose=live_candidate` 且强度 0.15 的样本单独分桶，不写成 accepted。映射与 10-19 的 P-2 / P-3 见 `docs/REVIEW_WARNING_CASES.md`。M-1 功效见周检「统计功效标注」（[#54](https://github.com/tspike630/trade/pull/54)），此处不重抄。C3 趋势出场只观察；要立项先走 R3 回测，不盘中追退出参数。**不算有效性。**

---

*R4 · 机制定稿 · 代码随 R3 建议表落地时实现（apply_params.py）*
