# 只读报忧哨兵

这是独立于交易闸和 PR224 的报忧组件。本目录不读取生产库、不写仓库、不配置真实通知渠道。

## 结论

哨兵本体的通知确认、相邻源桶和入场态势已有离线单测。`readonly_live_preflight.py` 不在本仓库 `main`（`f75761d`）、全部 git 历史和已上传附件里。原套件因此不能通过，**不允许安装**。作者跑过的单测不是非作者独验。

## 仍须补传

把真实模块放在本脚本同一目录，且至少导出：

- `Finding`
- `PreflightReport`
- `fmt_ms_utc`
- `inspect_system`

原套件还会调用 `eval_due_cftc_asof`、`eval_nyse_cash_market_open`、`KNOWN_FILE_HASHES`。不要用测试替身充当这个模块。预检必须自己保证 `mode=ro` 与 `PRAGMA query_only`，并且不得创建缺失数据库。本哨兵不打开 SQLite。

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
| 报价 | `quote_turnover_stale` / `quote_turnover_clock_absent` | policy，同上 blob |
| OBI | `obi_top5_source_stale` / `obi_top5_source_clock_absent` | policy，同上 blob |

`main` 的 `decision_engine/core/volume_z_1h.py`（blob `be1a5553b970a841e0a3cc2d4e66c0ab9668f752`）用 `source_close_is_fresh(source_close_time_ms)`，窗口默认 3600 秒。`computed_at_ms` 只是写入时刻。acceptance 把 `volume_z_computed_at_not_the_reader_clock` 记为观察，哨兵不把它当闭锁。

故意不升入闭锁名单：

- `turnover_stale`、`turnover_time_missing`：`main` 的 `live_bnb_turnover.py` 宇宙过滤，不是上面的全行失败关闭
- `obi_top5_source_ts_gap`、`obi_top5_source_ts_missing`：采集错误；`main` 上缺 OBI 仍可放行
- 上传原件里的 `lseg_nvda_smh_stale_market_open`、`deribit_gamma_stale_or_invalid`、`whitelist_news_stale` 与 policy 字符串不是同一个名字。原名前缀保留，避免旧预检词汇漏报；这不表示 `main` 正在发出这些码

连续 5m 桶只认发现明细里的源 K 线收盘：`source_bar_close_ms`、`entry_kline_5m_close_ms`、`kline_5m_close_ts_ms`。只有量能小时戳时用 `source_close_time_ms` / `volume_z_source_close_ms`。同一根不累加，中间缺桶则重置。`now_ms`、`computed_at_ms`、`collected_at_ms` 不算源桶。没有源戳就不计连续闭锁。

正常无入场信号（`signal_funnel.posture=no_entry_signal`，或已评估且 `buy_count=0` 且 `data_lockout=false`）是 `NO_SIGNAL`，不是红灯，也不累计闭锁。同一报告里若有数据闭锁拒因，态势是 `data_lockout`。

## 通知

配置的渠道全部成功才写入 `notified_*` 与 `last_success_ms`。失败、部分成功、未配置渠道都保持未确认，下一轮重试。部分成功只重试尚未成功的渠道。恢复通知失败不清除已确认红灯，下一轮仍发 `RESOLVED`。同一代码出现新的 `symbol` / `intent_id` 等对象会再次告警。

## 安装与回滚（由你在本机执行，此处不部署）

1. 确认下面的单测命令仍通过，并且你已把真实 `readonly_live_preflight.py` 放进副本。在那之前不要安装。
2. 把整个 `ops/readonly_alert_watchdog/` 复制到仓库外，例如 `/opt/trade-readonly-watchdog`。不要放进交易仓库、账本目录或密钥目录。
3. 状态和 JSONL 放在另一个目录，例如 `/var/lib/trade-readonly-watchdog/`。路径不能是指向仓库的符号链接，也不能以 `.db`、`.sqlite`、`.py`、`.env`、`.pem` 结尾。
4. 不要设置 `WATCHDOG_*` 渠道变量，直到你自己要接通通知。不要把 `--main-db` 指到生产库，直到预检被非作者验收。
5. 手工启动示例：`python3 /opt/trade-readonly-watchdog/readonly_alert_watchdog.py --repo-root <repo> --state-file /var/lib/trade-readonly-watchdog/state.json --alert-jsonl /var/lib/trade-readonly-watchdog/alerts.jsonl`。缺预检时进程应以状态 RED 退出码 2，而不是 0。

回滚：停掉你手工启动的进程，删除 `/opt/trade-readonly-watchdog` 和 `/var/lib/trade-readonly-watchdog`。本变更没有 systemd 单元，也没有写入交易进程、采集器或账本。若尚未合并，丢弃本分支即可。

## 作者自测（不是独验）

```bash
python3 ops/readonly_alert_watchdog/test_watchdog_unit.py
python3 ops/readonly_alert_watchdog/originals/test_proactive_watchdog.py
```

第二条会因缺少 `readonly_live_preflight` 失败。这是阻断，不是哨兵本体回归。
