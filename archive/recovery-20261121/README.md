# archive/recovery-20261121

这是 t587/t588 安全恢复归档，**不是生产安装版**。目录里的源码、测试、离线脚本和空模板只作字节保全。它们不进入 `decision_engine/`、`execution/`、`crawler/`、`dashboard/`，也不进入 `scripts/sync_deploy.sh` 的五类部署映射（`decision_engine`、`indicators`、`execution`、`crawler`、`dashboard`）。仓库没有 GitHub Actions。本目录不改 `DEPLOY_SHA`，不授权安装、重启、交易、借还、挂撤单、改账或改执行授权。

冻结说明 `README_RECOVERY.md` 曾建议父层使用 `archive/readonly-evidence-v4-t586/`。本次按父层指定写入 `archive/recovery-20261121/`：远端 `origin/main`（`f75761ded20804b4e4b3fb181a431de4b6610e39`）没有 `archive/`，也没有另一处非部署归档目录适合放这 10 件原字节。`docs/RECOVERY_REVIEW.md` 是另一份复盘角色记忆，本次未改。冻结文件的原文未改。

## 容器与白名单

来源容器为已审阅的 UTF-8 JSON，14 个文本，142005 字节，SHA256 `ca9a5bf427818626cba8adc1082fdecf9f755956a8cfad237ee7f2bb4634ec0a`。容器本身不入库。入库的是其中 14 个相对路径文本，加上本说明和 `REMOTE_RECONCILIATION.md`。

10 个载荷与 `ALLOWLIST_SHA256.json`、`FILE_REVIEW.json` 的路径、字节数、SHA256 一致，并与独立冻结记录一致。4 个说明/索引/清单是包内新编文本，同样按原字节保存。没有按本地绝对路径再去收集 handoff、补丁、报告、Tiingo 附件或生产树。

| 相对路径 | 字节 | SHA256 |
|---|---:|---|
| `payload/aave-readonly-v4/aave_readonly_candidate.py` | 35417 | `a283f2c23cb8ff5a450bb8d9ba3b1878adaab48c2d162870eb4fe588ab3af225` |
| `payload/aave-readonly-v4/pinned_ledger.py` | 5332 | `bff756a6da9b08fa3586751aaf137b6ef4cb66f441fc62c1876c8e00cd6b4c28` |
| `payload/aave-readonly-v4/aave_evidence_review.py` | 15251 | `02854a2af802f50afff6e615f1f2e5fdf3fbe29400d45cbaf92a79967d6c0632` |
| `payload/aave-readonly-v4/synthetic_harness.py` | 3038 | `980607ba35bb24c6980e50f7567251ae81c9308b6498f375ca69d012157956da` |
| `payload/aave-readonly-v4/test_aave_evidence_review.py` | 12093 | `90546c02d25d1c33549ae0781cd549cf43821881001fddabe26413d710e94cbf` |
| `payload/aave-readonly-v4/test_candidate_v3_regression.py` | 18071 | `befa74f93cb8b14ba706ede182e0eb961631c5e20871f58a690d0370a63afda5` |
| `payload/aave-readonly-v4/test_t584_fixes.py` | 13962 | `39040f61342c8ccddaa93831a738b7cf94f670305e4f7cb8ed042247e282be3a` |
| `payload/aave-readonly-v4/test_t586_oco.py` | 7097 | `87affcdeabeaeb725d648079ba8867c6cd11d45615da076cd796e3e0d85ecc0b` |
| `payload/aave-readonly-v4/run_offline_tests.sh` | 468 | `b1efa3cdf5a67d46c4e9a29f95d7cfd952edea1a5d271b91715fa61b4671d6f6` |
| `payload/aave-readonly-v4/approved-config.TEMPLATE.json` | 436 | `0ac630861877113936d41e98800b455f1a8f1c6777c9c7bff3ccd76bd0686ad6` |
| `README_RECOVERY.md` | 6185 | `1d9a5484168acff05312a7e165dfcd36c81aed70ae4efad03a39ca801eff0d9e` |
| `SAFE_ARCHIVE_INDEX.md` | 10813 | `27bfc1fd93a8e36da75a9dfb252be3d0733306e2253b76660df9f415024a01b9` |
| `FILE_REVIEW.json` | 6381 | `23dec1f060cb547c9230b13c4686d08e6f572fc09fc50d93059e585518f42d0b` |
| `ALLOWLIST_SHA256.json` | 1942 | `dbbc3fd3717ecffbb3745a13e1d4ff2ca6fadee674a15203419f1e875445f7ab` |

`run_offline_tests.sh` 的内容字节含 shebang，git 文件模式是 `100644`。内容 SHA 仍是上表的值。不要把它挂到 systemd、CI 或部署脚本。

## 停点

`README_RECOVERY.md` 记录的 173 PASS（作者 121 + 原独立 21 + 此前有效 19 + 新独立 12，0 FAIL/ERROR/SKIP）是 **2026-11-21 20:54 HKT 本机历史离线事实**。本云 VM **没有重跑**这 173 项，也不能把本包说成可独立重放这 173 项的完整包。

真实只读取证仍未授权。AAVE 当前方向、负债和 OCO 未核。生产既有三红未解除。Tiingo 仍是 NO-GO，最近 25 秒问题的原因未定位，也未在本次核对中新诊断。`origin/main` 不能代替本机 9 个 v4 目标 + 16 个未改基线的混合树。

## 依赖与 not_run

公开文档 fixture 与独立 52 项脚本不在白名单内，本次未打包：

- 缺 `docs/binance-official-rest-api.md`。冻结记录为 211871 字节，SHA256 `f1500faf7ede9e1b24087e31e3d2517eb56db28f1d936fdfde845ce418ae3ca8`。源测试仍引用它。断言未删、未改。
- 缺独立 52 项脚本和原始 `AUDIT_REPORT.md`。离线 runner 即使将来在依赖齐全时也只覆盖作者 121 项，覆盖不了 173 项。

本次这些测试的状态是 **not_run**。没有为了变绿而删除或放宽断言，也没有伪造通过标记。

## 本次实际做过的静态检查

- 核对容器大小和 SHA256。
- 核对 14 个文本的 UTF-8 字节数和 SHA256，路径均为相对路径，无绝对路径、无 `..`。
- 核对白名单 10 项与载荷、`FILE_REVIEW.json` 逐项一致。
- 对 8 个 Python 文件做 `ast.parse`；对模板和两份 JSON 做 `json.loads`。
- 静态模式扫描：未发现 PEM 私钥、云厂商访问密钥、GitHub token、Slack/OpenAI 形态 token、带用户信息的 URL、邮箱。模板里的 `api_key_env` / `api_secret_env` 是 JSON `null`。测试里的 `SYNTHETIC_SECRET`、`fake-key`、`MUST_NOT_EXPORT` 是合成反例标记。
- 保留功能所需的 UID 常量 `1275399777`、错配测试常量，以及本地路径字面量。路径字面量不授予读取权限。这次扫描不是形式化的无秘密证明。

## 本次没有运行

没有执行候选入口、`https_get`、签名请求、真实或本地账本 SQLite、`run_offline_tests.sh`、`bwrap` 或 unittest。没有读取凭证、env 值、生产库或原始日志。没有调用外部账户。没有安装、重启、下单或运行 `scripts/sync_deploy.sh`。
