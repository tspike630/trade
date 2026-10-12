# PRIOR_STEP5 审核补丁（3 处必改 + 2 处注意）

> 针对 `PRIOR_STEP5.md` 的审核意见，全部为**可粘贴代码**，合并进原方案后执行。
> 审核时间：2026-10-11

---

## 补丁 1：survival_ref 按 age_bucket 选参考列（替换原「同一对值」逻辑）

原方案把 `self_db.survive['1h'] / ['4h']` 同一对值写到所有行——**错误**。第四步联合表的正确对应是：<1h 段看 1h 存活率、1-24h 段看 4h 存活率、更长段因爬虫窗口覆盖上限标 N/A。

```python
# scripts/prior_tagger.py 内，survival_ref 赋值逻辑（替换原片段）
AGE_REF = {
    "<1h":   ("survive", "1h"),   # 全网观测跨度>1h 存活率
    "1-24h": ("survive", "4h"),   # 本段中位参考用 4h 存活率（第四步口径）
    "1-7d":  (None, None),        # 覆盖上限，N/A
    ">7d":   (None, None),        # 覆盖上限，N/A
    "unknown": (None, None),
}

def survival_ref_for(age_bucket: str, params: dict) -> float | None:
    """按币龄段取对应存活参考；无则 None（写 NULL，不写 0）"""
    spec = AGE_REF.get(age_bucket)
    if spec is None or spec[0] is None:
        return None
    try:
        return params.get("self_db", {}).get(spec[0], {}).get(spec[1])
    except Exception:
        return None

# 写入行时:
#   survival_ref = survival_ref_for(age_bucket, baseline_params)
#   INSERT ... survival_ref_1h / survival_ref_4h 两列都取该值（保留列结构），
#   但 <1h 行只填 1h 列、1-24h 行只填 4h 列，其余两列 NULL，语义才不错乱。
```

> 语义修正：`<1h` 行 → `survival_ref_1h=0.9271`、`survival_ref_4h=NULL`；
> `1-24h` 行 → `survival_ref_1h=NULL`、`survival_ref_4h=0.7529`；
> `1-7d / >7d / unknown` 行 → 两列均 NULL + `survival_ref_note='crawler 窗口覆盖上限'`。

## 补丁 2：周报消费端（第五步的自动化落点，原文档缺）

在 `review/data.py`（或周报数据层）加一个只读函数，周报 §8 直接调用：

```python
# review/data.py 追加
def get_prior_tag_buckets(prior_db: str, min_ts: str | None = None) -> list[dict]:
    """读 decision_prior_tags，按 age_bucket 分桶（供周报 §8 联合表）"""
    import sqlite3
    conn = sqlite3.connect(f"file:{prior_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT t.age_bucket,
               COUNT(*)                        AS n,
               COUNT(DISTINCT d.token_address) AS unique_token,
               SUM(CASE WHEN t.peak_window=1 THEN 1 ELSE 0 END) AS in_peak,
               SUM(t.survival_ref_1h IS NOT NULL) AS has_ref_1h,
               SUM(t.survival_ref_4h IS NOT NULL) AS has_ref_4h
        FROM decision_prior_tags t
        LEFT JOIN decision_log d ON d.id = t.decision_id
        {where}
        GROUP BY t.age_bucket
        ORDER BY CASE t.age_bucket
            WHEN '<1h' THEN 1 WHEN '1-24h' THEN 2
            WHEN '1-7d' THEN 3 WHEN '>7d' THEN 4 ELSE 5 END
    """
    where = "WHERE d.ts >= ?" if min_ts else ""
    rows = conn.execute(sql, (min_ts,) if min_ts else ()).fetchall()
    conn.close()
    return [dict(r) for r in rows]
```

周报 §8 在现有「JEV×币龄×先验」小节后追加一段：
```
### 先验标签自动分桶（prior_tagger 产物）
> 数据：decision_prior_tags（prior.db，只读）；peak_window=信号落在创建后 3 分钟内
| age_bucket | n | unique_token | 在见顶窗口内 | 有 1h 参考 | 有 4h 参考 |
```
> 分桶结果与前一小节手工表对账：**同一数据集应一致**；不一致即查 prior_tagger 的 age_bucket 口径。

## 补丁 3：验收线（追加到方案末尾）

| 检查 | 期望 | 入口 |
|------|------|------|
| 表存在 | `decision_prior_tags` | `SELECT name FROM sqlite_master WHERE name='decision_prior_tags'` |
| 行数 | **= decision_log 行数**（40） | `SELECT count(*) FROM decision_prior_tags` |
| unknown 占比 | ≤ 5%（<1h/1-24h 为主） | `SELECT age_bucket, count(*) FROM decision_prior_tags GROUP BY 1` |
| 幂等 | 重跑不翻倍（行数仍 40） | 连跑两次对比 count |
| survival_ref 语义 | <1h 行只有 1h 列非空；1-24h 行只有 4h 列非空 | 抽 3 行人工核对 |
| 隔离 | 业务四库 mtime 未变 | `ls -l *.db` |
| 周报对账 | §8 自动分桶与手工联合表一致 | 周报生成后比对 |

## 注意 2 条（实现时写进代码注释）

1. **单位换算**：`token_age_hours = (parse_epoch_ms(signal_ts) - pair_created_at_ms) / 3_600_000`
   `signal_ts` 是 ISO 文本（`decision_log.ts`），先 `datetime.fromisoformat(...).timestamp()*1000` 再减，**禁止字符串直接相减**。
2. **peak_window 口径**：3 分钟 = GMGN peak P50(1.0m) × 3 的经验窗口，标注「暂定，数据成熟后可调」；
   它表示「信号发出时 token 是否还在创建后 3 分钟内（见顶窗口内）」，不是「是否已见顶」。

---
*合并补丁后重跑 `python3 scripts/prior_tagger.py` 两次（验幂等），再按验收线逐条打勾。*
