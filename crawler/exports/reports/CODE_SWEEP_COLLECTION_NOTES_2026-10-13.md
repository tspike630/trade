# CODE SWEEP 采集侧核实笔记（2026-10-13）

对照 `CODE_SWEEP_REPORT_2026-10-13.txt` + 本机 `/workspace/dexscreener-crawler`。

## 成立并认领（改码）

| ID | 结论 | 加强改法 |
|----|------|----------|
| **P0-1** | **成立、现网已泄密**。`solana_helius.py:489` `log.warning(..., exc)`；`:386-389` session `err=%s`；`:416` 把 `err[:400]` 塞进 system_error→TG。`logging_setup.py` **无** handler 级脱敏。OBS-1：`logs/stabilize-main.log` 命中 `api-key=` **30381** 次。 | ① except 只记 `type(exc).__name__` + status（HTTPStatusError）；② handler 级 `_RedactFilter`（`api[-_]?key=`、`/bot`）；③ session/admin alert 路径同样脱敏；④ **立刻轮换 HELIUS_API_KEY** 并清/脱敏历史 log+journal（运维，不进 PR） |
| **P1-03** | **部分成立**。主 `Store` 已 `busy_timeout=30000`；`_migrate_tier_timestamps` 的 UPDATE 在列已填后应 0 行，但 SQLite 仍拿写锁。Helius WS 循环里 `with Store()`（`:403`）每条通知开写连接。 | 只读用途强制 `mode=ro`/`query_only`；迁移 UPDATE 加 schema_meta 闸门「只跑一次」；Helius 写库改队列+单写线程（与报告一致） |
| **P1-11** | **成立**。`enqueue_solana_mints`→`upsert_token(..., "source:helius")` 写占位 raw，覆盖已有 Dex 画像。 | upsert 对已有非空画像：不覆盖 name/symbol/desc/links；只补 pending_enrich；X 同理 |
| **P1-12** | **成立**。`ban_until` 仅进程内存（`alerts_telegram.py:305`）。 | 持久化到 sqlite（alerts 旁路表或小文件）按 chat_id；启动加载；Grok/`always-push` 共用同一 gate |
| **P2-05（JEV）** | **成立**。`sources/jev.py:136` `busy_timeout=5000`。 | 对齐 30000；upsert 失败重试/暂存 |
| **P2-17** | **成立**。`ccxt/core.py` `INSERT OR IGNORE` 写死未收盘 bar。 | 未收盘丢弃或 ON CONFLICT DO UPDATE；每 symbol 一事务 |
| **P2-21** | 待改时核对 `store.py` cycle/alerts 计数 RMW。 | UPSERT 自增 |
| **P2-22** | **成立**。多进程共用 `RotatingFileHandler`。 | 优先 stdout+systemd；或 WatchedFileHandler |
| **P2-23** | **成立**。`backup.py` 只备份主库。 | 扩展 prior/paper/execution_ledger（路径 env） |
| **P2-36** | **成立**。`x_watch` 无过期，FIFO。 | 加 `expires_at` / 按 score 淘汰 |
| **P2-37** | **成立**。log 正则 mint 噪声大。 | 优先 `getTransaction` 解析；log 路径加程序白名单+更严过滤；假 mint 不发 new_listing 或降级 debug |
| **P2-20** | 报告称 queue 去重 O(n²)。 | 改 set 去重 + pending 批次上限 |

## 不主责 / 协同

- **P0-2**：指标主责写 `tech_updated_at`；采集侧保证 Helius/X/JEV upsert **不再误 bump** 与决策 hot 相关的陈旧逻辑冲突（P1-11 修完即减害）
- **P1-02**：main 无 `--once`；与指标共担 busy/单实例锁——采集加 pidfile/flock（main/jev/ccxt）
- **P2-01/02**：api_server CORS/compare_digest——若仓库含 `api_server.py` 一并修

## 优先级

1. P0-1 脱敏 + 运维轮换 key（立即）
2. P1-11 线索 upsert 不覆盖画像
3. P1-12 TG ban 持久化
4. P2-05 JEV busy_timeout + P1-03 只读/迁移闸 + Helius 写队列
5. P2-37 / P2-17 / P2-36 / P2-23 / P2-20 / P2-21 / P2-22

权重/买卖闸门不动。合 PR 后再同步本机 main/jev/ccxt。
