# 只读报忧哨兵

这是独立于交易闸和 PR224 的报忧组件。本目录不读取生产库、不写仓库、不配置真实通知渠道。

## 结论

真实预检原件已放入 `originals/readonly_live_preflight.py`（SHA256 `02acd508e6e3fb4c0dc938295b9aedea194071857ac2a3c9c11969672be347f0`）。组件内的副本补了三件事：1h 量能只认 `source_close_time_ms`、漏斗把「正常无入场」和「数据闭锁」分开、没有代码树时不再把整份哈希清单报成红灯。`code_integrity_v3_verified` 只表示字节相同，v3 未验收、未部署，**不是上线许可**。通知渠道仍未指定，**不允许安装**。这里的测试是作者自测，不是非作者独验。

当前 git `main`（`f75761d`）对这 25 个基线：0 个命中现网哈希，0 个命中 v3 哈希，16 个文件不在树上，9 个漂移。不要把这次比对说成现网已是 `V3_PATCHED`。

## 预检边界

- SQLite 只走 `file:...?mode=ro` 和 `PRAGMA query_only = ON`。文件不存在就不打开，不创建。
- Deribit 新鲜度用 `ts_ms`，不用 `collected_at_ms`。未来钟（早于现在超过 120 秒的反向年龄，或 LSEG `utc_ms` 落在未来）是红灯。
- 1h 量能读 `decision_volume_z.source_close_time_ms`。`computed_at_ms` 即使是刚才写入的也不算新鲜。表不存在时不假装已经验过。
- 报价钟只读两处已证明的源戳：`ccxt_ticks.ts`（`quote_volume_24h`，`exchange=binance`；秒值只按 `live_bnb_turnover` 放大一次）和 `symbols.quoteVolume_collected_at`（可在主库或已存在的 `exports/binance_spot_universe.db`）。`updated_at`、`binance_indicators.quote_volume_24h`、以及并不存在的表 `quote_turnover.quote_ts_ms` 都不算源钟。缺表是黄灯 `quote_turnover_source_unobserved`，不假装已检查新鲜，也不计连续桶。缺戳、未来（超过 120 秒）和超过 3600 秒是红灯。没有源戳时连续桶保持 0。
- LSEG 5m 门禁用 30 分钟，与 `decision_engine/core/macro_circuit_breaker.py` 的 `MAX_CROSS_ASSET_AGE_MS` 相同。导入器开盘 15 分钟标签是另一套口径，这里没有改成 15 分钟。

## 最小依赖

- Python 3 标准库：`json`、`pathlib`、`urllib.request`、`sqlite3`（只接异常类型）
- 无第三方包
- 无交易所、无凭证、无生产 DB
- 通知环境变量默认空：`WATCHDOG_WEBHOOK_URL`、`WATCHDOG_TELEGRAM_BOT_TOKEN`、`WATCHDOG_TELEGRAM_CHAT_ID`、`WATCHDOG_BARK_URL`

## 源时钟覆盖（只认代码里真有的拒因）

持续闭锁升级名单新增了 `origin/cursor/integrate-three-blockers-d43b` 上 `check_row_and_tick_freshness` 的失败关闭字符串。该分支不是 `main`。`main` 上这些字符串出现次数为 0。

| 族 | 名单中的代码 | 证据 |
| --- | --- | --- |
| 5m | `entry_5m_kline_stale` / `entry_5m_kline_clock_absent` / `entry_5m_kline_clock_unit_mismatch` | policy blob `43a49cde1a6a1e2fac166342626660c99e50f182` |
| 1h 量能源棒 | `volume_z_source_close_stale` / `volume_z_source_close_absent` | 同上；新鲜度看 `source_close_time_ms` |
| 量能缺钟 | `volume_z_close_clock_absent` | acceptance blob `9222ef12493313a5b5a610339dacd68beab4e4fd` |
| 报价 | 预检不发出 `quote_turnover_stale` | 未覆盖。policy 字符串只留在哨兵名单里，避免以后有发现码时漏报 |
| OBI | `obi_top5_source_stale` / `obi_top5_source_clock_absent` | policy，同上 blob |

`main` 的 `decision_engine/core/volume_z_1h.py`（blob `be1a5553b970a841e0a3cc2d4e66c0ab9668f752`）用 `source_close_is_fresh(source_close_time_ms)`，窗口默认 3600 秒。`computed_at_ms` 只是写入时刻。acceptance 把 `volume_z_computed_at_not_the_reader_clock` 记为观察，哨兵不把它当闭锁。

故意不升入闭锁名单：

- `turnover_stale`、`turnover_time_missing`：`main` 的 `live_bnb_turnover.py` 宇宙过滤，不是上面的全行失败关闭
- `obi_top5_source_ts_gap`、`obi_top5_source_ts_missing`：采集错误；`main` 上缺 OBI 仍可放行
- 上传原件里的 `lseg_nvda_smh_stale_market_open`、`deribit_gamma_stale_or_invalid`、`whitelist_news_stale` 与 policy 字符串不是同一个名字。原名前缀保留，避免旧预检词汇漏报；这不表示 `main` 正在发出这些码

连续桶只认明细里的源戳：`source_bar_close_ms`、`entry_kline_5m_close_ms`、`kline_5m_close_ts_ms`、`source_close_time_ms`、`volume_z_source_close_ms`、`quote_source_stamp_ms`。同一根不累加，相邻下一根才加，中间缺桶重置。`now_ms` 不再充当轮询槽。只有 `computed_at_ignored`、没有 `source_close_time_ms` 的 `volume_z_source_close_absent` 仍然告警，连续桶是 0，不会升成连续闭锁。

空 `decision_log` 且没有数据闭锁码时，漏斗态势保持 `unspecified`，不是 `no_entry_signal`，也不是健康。

正常无入场信号（`signal_funnel.posture=no_entry_signal`，或已评估且 `buy_count=0` 且 `data_lockout=false`）是 `NO_SIGNAL`，不是红灯，也不累计闭锁。同一报告里若有数据闭锁拒因，态势是 `data_lockout`。

## 通知

配置的渠道全部成功才写入 `notified_*` 与 `last_success_ms`。失败、部分成功、未配置渠道都保持未确认，下一轮重试。部分成功只重试尚未成功的渠道。恢复通知失败不清除已确认红灯，下一轮仍发 `RESOLVED`。同一代码出现新的 `symbol` / `intent_id` 等对象会再次告警。

## 安装与回滚

`run_watchdog_once` 的 `install_allowed` 固定为 false。本目录没有 systemd 单元，也不能当成已经可以启用。真实接收渠道还没有指定，不要设置 `WATCHDOG_WEBHOOK_URL`、`WATCHDOG_TELEGRAM_BOT_TOKEN`、`WATCHDOG_TELEGRAM_CHAT_ID`、`WATCHDOG_BARK_URL`。

非作者对这一提交重新独验通过之后，才由你在本机安装到仓库外的独立目录，例如 `/opt/trade-readonly-watchdog`。两个模块必须在同一目录。状态和 JSONL 放在另一个目录，例如 `/var/lib/trade-readonly-watchdog/`。路径不能是指向仓库的符号链接，也不能以 `.db`、`.sqlite`、`.py`、`.env`、`.pem` 结尾。不要写开机服务，不要把 `--main-db` 指到生产库，除非你自己决定这么做。

回滚：停掉你手工启动的进程，删除 `/opt/trade-readonly-watchdog` 和 `/var/lib/trade-readonly-watchdog`。没有交易进程、采集器或账本可回滚。若尚未合并，丢弃本分支即可。

## 作者自测（不是独验）

```bash
python3 ops/readonly_alert_watchdog/test_watchdog_unit.py
python3 ops/readonly_alert_watchdog/originals/test_proactive_watchdog.py
```

作者结果：第一套 14 项通过。第二条是未修改的原件配对，7 项里 `test_07` 失败：没有代码树时原预检把缺失文件报成红灯，数据恢复后 `should_push` 仍为假。`test_03` 因本机没有 `pr224_worktree` 跳过。把修正后的两个模块和原测试放在同一临时目录再跑，7 项里 6 项通过、1 项跳过。网络在测试里被阻断，没有外发通知。
