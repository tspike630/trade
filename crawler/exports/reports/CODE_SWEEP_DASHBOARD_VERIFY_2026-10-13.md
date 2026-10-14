# CODE SWEEP 看板核实（2026-10-13 23:47 HKT）

对照 tip `2c6cd94` 报告 + 本机 `/workspace/dexscreener-crawler/dashboard/` + `execution_ledger.db`。

## 结论：认领项成立，改法可加强

| 项 | 报告 | 本机核实 | 更好改法 |
|---|---|---|---|
| P1-15 | settings 只读 env | ✅ `settings.py:120-121`；渲染 `app.py:1414+` | 优先读最新 `execution_log.receipt_json` 的 `paper_enabled`/`pm_enabled`（现网收据确有这两键）。**加陈旧判定**：最新行 `ts` 超心跳窗口则标「网关陈旧」，勿盲信旧 ON/OFF；env 仅作回退并标「看板进程」。`live_effective` 补 `LIVE_EXCHANGE_API_URL` 非空（对照 `live_trade`）。 |
| P2-30 | 拒因只看 validation_* | ✅ `data.py:1071` 未 SELECT status | SELECT `status`；拒因聚合含 PM/风控/paper。**另修** `parsing.py:152`：`validation_errors` 现网/ledger 为 JSON 数组，现按 `;` 切会错——先 `json.loads` 再回退 `;`。 |
| P2-03 | SecretFilter 挂 root logger | ✅ `app.py:880` | 挂到每个 handler；保留 root 亦可但 handler 必挂。 |
| P2-31 | 聊天最早 200 | ✅ `sessions.py` `ORDER BY id ASC LIMIT` | 改为最近 N：`ORDER BY id DESC LIMIT` 再正序拼上下文。 |
| P2-32 | DB env / 心跳 / 权益色 | ✅ execution 只认 `DASHBOARD_EXECUTION_DB`，**缺** `EXECUTION_LEDGER_PATH`（ledger.py 真源）；`HEARTBEAT_STALE_SEC` 默认 900；权益 `app.py:1981` 写死 10000 | 路径元组加入 `EXECUTION_LEDGER_PATH`；权益色相对初始/首点权益，不写死；心跳阈值勿一刀切改 120——网关行可用更严阈值，其它进程保持可配。 |
| 两树 | crawler 缺 evolution | 本机仅根 `dashboard/`（无 crawler/dashboard） | 根为真源；PR 注明即可。 |

## 现网抽样（只读）
- `execution_log`：134 行，`status` 全 NULL，`validation_ok=1` 全过；收据含 `paper_enabled`/`pm_enabled`（当前皆 false）。
- 最新 `ts`≈2026-10-12T12:49Z → **陈旧判定必要**，否则指示灯会长期显示停机前状态。

## 非本组但影响展示
- P0-2 指标 `updated_at` 饿死 → 看板 hot/RSI 等会冻；等指标 PR。
- 闸门默认值/权重：不动。
