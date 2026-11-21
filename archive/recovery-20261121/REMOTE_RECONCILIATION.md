# 远端核对（2026-11-21）

范围是 `https://github.com/tspike630/trade` 在本次抓取时的全部现存 ref。结论只分三类：**已确认存在**、**限定搜索未找到**、**无法确认**。限定搜索未找到只说明下面的范围内没有命中，不能写成“从未上传”。同名文件或同一个币种字符串也不能当成字节相同。

本次只新增本目录的白名单载荷和安全摘要。历史补丁、原报告、Tiingo 脚本和报告附件没有外发，也没有用 `main` 拼出现场混合树。

## 搜索范围

- 抓取时间：2026-11-21。`git ls-remote --heads` 与本地 `refs/remotes/origin/*` 都是 **170** 个分支。
- `origin/main` = `f75761ded20804b4e4b3fb181a431de4b6610e39`。
- 标签 `c4-lock-20261021` 指向 `4020fa4bb3bd37285dc62a500f66bf15afb23d7e`，该提交在 `origin/research/c4-locked-eval` 上，已包含在 `--all` 搜索里。
- 使用的只读手段：`git log --all -S`、`git log --all --grep`、`git grep`、`git diff --name-only`、`git cat-file` 后重算 SHA256；GitHub 上读取 PR 224、PR 225、PR 183 和两个已知 commit 对象。
- GitHub code search 对若干查询返回 `incomplete_results: true` 且 0 条。负结论不采用它。
- 未覆盖：已删除且当前任何分支都到不了的对象、其它仓库、gist、本机 `/workspace/handoffs`、生产机磁盘、未进 git 的附件。这些保持 **无法确认**。

`origin/main` 上没有 `archive/`。`decision_engine/DEPLOY_SHA` 在 `.gitignore` 中，git 历史里也没有任何文件内容包含字符串 `67ba54d97b47c0f4e2bb22db758e1cf627be94e6`。

## 逐项

### PR 224 与全仓 v4 独验 / 装盘 / 加载

**已确认存在：** [PR 224](https://github.com/tspike630/trade/pull/224) 打开、未合并。head `e7c8b68140af6c893d8b9762b7707e76f5c33389`（分支 `cursor/integrate-three-blockers-d43b`），base `main` `f75761ded20804b4e4b3fb181a431de4b6610e39`，61 个提交，142 个文件。标题是显式保证金路由和重复拒绝占用。相对 `main` 的差异里包含 `decision_engine/core/cityu_institutional_alpha.py`、`decision_engine/core/regime_strategy_router.py`、`decision_engine/core/engine.py` 以及 `execution/margin_cross/` 下多个文件。

**限定搜索未找到：** `T542_V4`、`t542`、`t544`、`t546`、`DISK_ONLY_INSTALL`、`RESTART_RECEIPT`、`9v4`。没有这些名字的独验报告、补丁清单、装盘回执或加载回执。

**无法确认：** PR 224 里上述 cityu/regime 文件的字节是否等于本机 9 个 v4 目标的现场补丁。白名单没有那 9 个目标的字节或补丁 SHA，本次没有从 `main` 或 PR 224 抽出文件来冒充现场树。`origin/main` 上的五态模块是仓库源码，不能代替本机 9v4+16 混合树。

### 报忧 commit 4bbe8da

**已确认存在：** commit `4bbe8da5d6964cc788a9510de624d7c0fd1e3049`，2026-11-20 17:54:36 UTC，主题 `fix(watchdog): count adjacent observation cycles apart from source bars`。它是 `origin/cursor/readonly-watchdog-retry-aa15` 的尖端，也是 [PR 225](https://github.com/tspike630/trade/pull/225) 的 head。PR 225 打开、未合并，标题为失败告警重试且不批准安装。该 commit **不是** `origin/main` 的祖先。

对这个 commit 上的下列路径，本次用 `git cat-file` 重算了内容 SHA256，与 PR 225 正文列出的值一致：

| 路径 | 字节 | 内容 SHA256 |
|---|---:|---|
| `ops/readonly_alert_watchdog/readonly_alert_watchdog.py` | 41532 | `b4deb00b0dd510dc98902e191abf58fa8f39cd9857823651a6c41483698bb036` |
| `ops/readonly_alert_watchdog/readonly_live_preflight.py` | 89244 | `0a74bf7d1095975bd6a6d558f1f0e1ff74494a5e7b3323c496a031f5a4a3e628` |
| `ops/readonly_alert_watchdog/test_watchdog_unit.py` | 29862 | `b0710a8c6598483125d5d3421d5c3a6001bf7e09c40696724b8d3c55987fa2e1` |
| `ops/readonly_alert_watchdog/README.md` | 7498 | `33254ca365418ea597431b5abc0a6424ce2c08d08ada94bbb537cac114834049` |
| `ops/readonly_alert_watchdog/patches/watchdog.patch` | 50205 | `e8e48d6068141faaf2de60b0435c39478c9c1dcfb08e1c659f7874295f2ca4e6` |
| `ops/readonly_alert_watchdog/evidence/author_unit_result.json` | 913 | `0bf050435291ea9cb64113c705491124b0f19102693a700a866fe8a81ca1a8d2` |
| `ops/readonly_alert_watchdog/originals/readonly_alert_watchdog.py` | 19356 | `0e42dba33ee1ba3235f5bbacdfabe39332afa40fb371689f03764c8adb671357` |
| `ops/readonly_alert_watchdog/originals/readonly_live_preflight.py` | 73261 | `02acd508e6e3fb4c0dc938295b9aedea194071857ac2a3c9c11969672be347f0` |

同分支上更早的只读哨兵提交也在：`5c724ed5f4faa9c0130511bda6f8efb407ea8e2b`、`4dbaedeaba50a7ce4fe6f1e4a061b70e9b16c312`、`48bb759bbb0dfc50782c5c31fd5c02fe8d079b48`。PR 225 写明对 `48bb759` 的非作者独验仍是整体 FAIL，`4bbe8da` 把 `install_allowed` 保持为 false，不能安装。

**无法确认：** 这些远端字节是否等于本机已安装的报忧文件。白名单没有本机安装树，本次没有把 PR 225 的补丁或原件再复制进本归档。

同一仓库里还有别的 watchdog 路径，例如 `execution/live_bnb_watchdog.py`、`scripts/decision_watchdog.sh`、`scripts/live_bnb_execution_watchdog.sh`、`scripts/live_bnb_watchdog_dry.sh`。它们是其它文件，本次没有把它们的字节算成 `4bbe8da` 的哨兵源码。

### Watchdog v3.3 与更早套件

**限定搜索未找到：** `v3.3`、`v3_3`、`watchdog-suite`、`watchdog-v3`、`suite v3`、`3RED`、`phase2b1`、`t574`。因此 v3.3 候选源码、80 PASS 摘要文件和真实 3RED 回执都没有在本次范围内定位到。v2 / T5、v3 / T6、v3.1 / T7、v3.2 / T8 的套件目录名同样没有命中。

**无法确认：** 上一段的 `5c724ed` / `4dbaede` / `48bb759` / `4bbe8da` 是否对应本地某一次 v2–v3.2 套件。提交说明没有这些套件名。`4dbaede` 的主题是按源小时判断成交量，其中的 “v3 hash” 一词不能当成 suite v3.3。生产三红仍按本地索引视为未解除；这次远端核对没有新的真实 RO 结果。

### 旧 DEPLOY 标记 67ba54d

**已确认存在：** `67ba54d97b47c0f4e2bb22db758e1cf627be94e6` 是 [PR 183](https://github.com/tspike630/trade/pull/183) 的合并提交，2026-11-07 已合并，主题是保存脱敏后的币安拒单诊断。它是 `origin/main` 的祖先。该提交改了三份执行文件：`execution/executors/binance_spot.py`、`execution/gateway.py`、`execution/tests/test_binance_exchange_rejection.py`。

**无法确认：** 某台机器上的 `DEPLOY_SHA` 文件现在是否仍等于这个提交。git 里没有这份被忽略的标记文件。这个提交的文件清单不是 9v4+16 混合运行树。

### Tiingo v1–v4

**限定搜索未找到：** `tiingo`、`Tiingo`、`TIINGO`、`TIINGO_PRO`、`tiingo-pro`、`t579`、`NO-GO`。各版状态摘要、脚本和报告附件都没有命中。另有两处 `No-Go` 在 `docs/SURVIVAL_20k_METHODS_2026-10-20.md`（提交 `4774659`、`1d6df72`），内容是 20k 方法档位，不是 Tiingo。

**无法确认：** 这些材料是否曾以已删除 ref 或 git 以外的附件存在。本次没有补传脚本或报告。25 秒问题的原因仍然未定，本次没有发请求、没有装盘。

### AAVE t581–t586

**限定搜索未找到（本次提交之前的可达历史）：** `aave_readonly_candidate`、`readonly-evidence`、`EVIDENCE_REVIEW`、`approved-config.TEMPLATE`、`schema_row`，以及载荷 SHA `a283f2c23cb8ff5a450bb8d9ba3b1878adaab48c2d162870eb4fe588ab3af225`。t581–t585 的旧候选和审计原文不在本白名单，也没有在可达历史里按这些标记找到。

**已确认存在、但不是这份取证包：** `origin/main` 上含字符串 `AAVE` 的文件是 `crawler/data/prior/exports/cex_prior_join_20261012.json`、`crawler/scripts/prior_cex_listing_join.py`、`decision_engine/core/conviction_whitelist.py`、`decision_engine/core/portfolio_clusters.py`、`decision_engine/tests/fixtures/news_isolation/ab2d2eb-clusters.py`、`decision_engine/tests/test_conviction_whitelist.py`、`decision_engine/tests/test_portfolio_clusters_and_vol_sizing.py`、`decision_engine/tests/test_tier1_kelly_and_oco_tp.py`、`scripts/fetch_public_derivatives_and_counterparty.py`。UID 字面量 `1275399777` 出现在 `execution/README.md`、`execution/executors/binance_spot_gate.py` 和若干 execution 测试中（`main`、PR 224、PR 225 的树上都有）。那是既有执行绑定常量，不是 t586 只读候选。

**本归档新增：** 上表 10 个 V4 原字节文件。在本分支提交之前，可达历史里没有这些 SHA。

### 早期 Phase 回执与公开 fixture

**限定搜索未找到：** `PHASE1_VERIFY`、`PHASE2A`、`PHASE2B2`、`INSTALL_RECEIPT`、`START_RECEIPT`、`EXPORT_RECEIPT`、`binance-official-rest-api`，以及 fixture SHA `f1500faf7ede9e1b24087e31e3d2517eb56db28f1d936fdfde845ce418ae3ca8`。

## 留给父层的缺口

以下项不在这次白名单，本次没有推测内容，也没有从 `main` 组装：

1. 公开 fixture `docs/binance-official-rest-api.md`（211871 字节，SHA256 `f1500faf7ede9e1b24087e31e3d2517eb56db28f1d936fdfde845ce418ae3ca8`）和独立 52 项脚本、原始审计报告。
2. t542 九目标补丁、独验报告、t544 装盘回执、t546 加载回执，以及本机 9v4+16 混合树字节。
3. 报忧本机安装副本与 `4bbe8da` 的逐文件字节对照；v2–v3.3 套件源码和非敏感状态原文。
4. Tiingo v1–v4 的状态原文。脚本和报告附件仍不在授权内。
5. AAVE t581–t585 旧候选与独立审计原文。

## 本分支落点

载荷与本核对的首次入库提交是 `a04c0e819e914d9ee5c458cd59788cd507383a37`。PR 为 https://github.com/tspike630/trade/pull/226 ，分支 `cursor/recovery-archive-20261121-0a0f`，base 是 `main` `f75761ded20804b4e4b3fb181a431de4b6610e39`。该 PR 只增加本目录，未合并，也不部署。
