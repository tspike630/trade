# CODE SWEEP · 看板侧认领笔记（2026-10-13）

来源：群内 CODE SWEEP 审查报告 docx · tip `2c6cd94` · 只读，未改代码。

## 总览摘录
- 合计 65：P0=4 / P1=15 / P2=38 / 观察=8
- 看板 DataStore `mode=ro`：**已满足**（业务库只读；仅 `dashboard_state.db` 可写）
- 两树：`crawler/dashboard` 缺 `evolution.py`（根有）——与日检笔记一致

## 看板认领项

### P1-15 闸门指示灯读本进程 env，非网关真状态
- `dashboard/settings.py` + `app.py` 渲染
- 建议：读最新 `execution_log.receipt_json` 的 paper_enabled/pm_enabled；env 值标注「看板进程」

### P2（看板相关）
- P2-03 SecretFilter 应挂 handler
- P2-19 全量刷新含隐藏页签 / 全表扫
- P2-30 拒绝分布不看 status（漏 PM/风控/paper 拒）
- P2-31 聊天历史 LIMIT 最早 200 条
- P2-32 DB 路径 env 不一致、心跳阈值 900s vs API 120s、权益色写死 10000、registry 未渲染面板

## 非看板但会影响展示
- P0-1/P0-2 归采集/指标（密钥日志、updated_at 冻结 hot）
- 复盘/看板都不读 `order_events`/`status`（P2 链路）

闸门/权重不动；等主 Agent 排执行单后再改。
