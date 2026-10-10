# Phase 4 Result — 异动告警 + 监控心跳

**Time (HK / UTC+8):** 2026-10-10 ~01:17 HKT

## Design

### Alerts
- On pair upsert: load previous `raw_json` → `detect_pair_anomalies` → `emit_alert` (cooldown → insert `alerts` → WARNING log → optional webhook)
- On new token insert from `token-profiles/latest` → `new_listing`
- On new token insert from CTO list → `cto_new`
- Deduplicate by `(type, chain_id, token_address, pair_address)` within `ALERT_COOLDOWN_SEC`

### Heartbeat
- `_finalize_ops` after every cycle path (ok / degraded / error / interrupted)
- Writes `logs/heartbeat.json` + bumps durable counters + `last_success_at` on ok/degraded
- `python main.py --health` uses freshness + status

## Files

| File | Role |
|---|---|
| `alerts.py` | detect + emit |
| `heartbeat.py` | write / read / check_health |
| `config.py` | alert + heartbeat knobs; `ALERT_WEBHOOK_URL` from env |
| `store.py` | `alerts` table + helpers |
| `pipeline.py` | wire detect/emit + finalize heartbeat |
| `main.py` | `--health` |
| `demo_alert.py` | inject Δprice/Δvolume demo |
| `README.md` / `PHASE4_RESULT.md` | docs |

## Verification

### Live cycle (`--force-new --force-lists`)
- Emitted **13** alerts: `new_listing`, `price_change`, `volume_h24_jump`, `liquidity_drop`
- Heartbeat written `status=ok`
- Counters: `cycles_ok=1`, `alerts_emitted=13`
- Cycle completed; tokens=82, pairs=53

### demo_alert.py
- Detected 2 anomalies (price +50%, volume +150%) → emitted ids 14–15
- Second run suppressed by cooldown (log: alert suppressed)

### `--health`
```
OK: age=0s status=ok cycle_id=20261009T171758Z-...
```
With `--health-stale-sec 0` → FAIL (stale), confirming threshold logic.

### Sample heartbeat
See `logs/heartbeat.json` — timestamp, pid, stats, counters.

## Configure alerts

```bash
# thresholds in config.py, or edit defaults
export ALERT_WEBHOOK_URL="https://hooks.example/dex"   # optional
python main.py --force-lists
python main.py --health
```

## Not in scope
Phase 5 proxy pool / multi-source.
