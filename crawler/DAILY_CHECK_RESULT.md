# Daily Check Result

**Time (HK / UTC+8):** 2026-10-10 ~02:25+ HKT

## Command

```bash
cd /workspace/dexscreener-crawler
source .venv/bin/activate
python daily_check.py
```

## Sample output

```
============================================================
DexScreener Crawler — Daily QA Check / 每日巡检
time_hk=2026-10-10 02:26:20 HKT
db=dexscreener.db  ALERT_MIN_SCORE=25.0  HOT_INTERVAL=300s
============================================================

[WARN] History 历史序列: 有 89 行但尚无相邻间隔样本 (多数 pair 仅 1 点) / 89 rows but no adjacent deltas yet; pairs=89 db=632.0 KB. 跑 --loop 热层后再验 ~300s±30s
[PASS] Scoring 质量分: 已评分 89 avg=60.7; top5=[85.0, 85.0, 83.5, 82.0, 82.0]; <25.0: 4; 分布 {'0-24': 4, '25-49': 23, '50-74': 34, '75-100': 28}; 日志跳过推送(24h) suppressed_push=1
[WARN] Multi-chain 多链: solana: tokens=124 pairs=103; robinhood: tokens=17 pairs=10; bsc: tokens=4 pairs=3; ethereum: tokens=3 pairs=2 | 非 ENABLED_CHAINS 残留: ['robinhood'] (pending/历史数据可能) | ENABLED=['solana', 'ethereum', 'bsc', 'base']
[WARN] Heartbeat 心跳: stale/unhealthy — FAIL: heartbeat stale age=280s > 120s status=ok (若未 --loop 属预期 WARN)
[PASS] Backup 备份: 最新 dexscreener-20261009-172844.db age=1.0h
[WARN] Logs ERROR: 近24h ERROR 行数=7 > 目标3
[PASS] Resources 资源: DB 632.0 KB (dexscreener.db); heartbeat pid=1534746 进程已不在 (RSS n/a)
------------------------------------------------------------
SUMMARY 汇总: PASS=3 WARN=4 FAIL=0 → exit=0
JSON → logs/daily_check-20261010.json
```

## Artifact

- JSON: `logs/daily_check-20261010.json`

## Notes

- History WARN when most pairs only have a single snapshot (need `--loop` for ~300s±30s adjacent deltas).
- Heartbeat WARN/FAIL if crawler is not currently `--loop`ing (expected for one-shot QA).
- Backup age WARN if newest backup >36h — run `python backup.py`.
- Exit code 0 iff no FAIL lines.
