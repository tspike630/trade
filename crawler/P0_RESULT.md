# P0 Result — 生产加固

**Time (HK / UTC+8):** 2026-10-10 ~01:28–01:30 HKT

## Added

| # | Item | Files |
|---|---|---|
| 1 | API access control | `api_server.py`, `config.API_SECRET` / `ALLOW_INSECURE_API` |
| 2 | SQLite backup | `backup.py` → `backups/dexscreener-*.db` (retain 7) |
| 3 | systemd / cron | `deploy/*.service`, `*.timer`, `crontab.example`, `env.example` |
| 4 | Proxy health | `proxy_pool.py` probe loop, 3-fail eviction, `proxy_pool_low` alert; `demo_proxy_health.py` |

## Verification (this machine)

### Backup
```
BACKUP_OK backups/dexscreener-20261009-172844.db  (331776 bytes)
```

### API auth (`API_SECRET=test-secret-p0`)
| Request | Result |
|---|---|
| `GET /health` (no key) | **200** open |
| `GET /stats` (no key) | **401** invalid or missing X-API-Key |
| `GET /stats` wrong key | **401** |
| `GET /stats` + `X-API-Key: test-secret-p0` | **200** tokens=84 pairs=57 |
| No secret + no ALLOW_INSECURE | data routes **503** |
| `ALLOW_INSECURE_API=1` | data routes 200 (dev only) |

### Proxy eviction demo
```
PROXY_HEALTH_DEMO_OK evicted=3 alerts_delta=1 proxy_pool_low=1
```
(Mocks `_probe_one` → False ×3; writes `alerts.type=proxy_pool_low`.)

### systemd units (shipped, not installed)
- `deploy/dexscreener-crawler.service`
- `deploy/dexscreener-api.service`
- `deploy/dexscreener-backup.service` + `dexscreener-backup.timer`

## How to run in production
1. Deploy tree to `/opt/dexscreener-crawler`, create user `dexscreener`
2. Copy `deploy/dexscreener-crawler.env.example` → `/etc/dexscreener-crawler.env` with strong `API_SECRET`
3. Firewall: allow only ops IPs to API port (or bind 127.0.0.1 + reverse proxy)
4. `systemctl enable --now dexscreener-crawler dexscreener-api dexscreener-backup.timer`
5. Cron alternative: `deploy/crontab.example`

## Caveats
- Unit paths are placeholders (`/opt/dexscreener-crawler`)
- Do **not** enable `ALLOW_INSECURE_API` publicly
- Proxy health loop only runs when `USE_PROXY=1` and proxies exist (default off)
