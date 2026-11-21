# t587/t588 安全恢复归档（仅本地准备）

生成：2026-11-21 21:02:39 HKT。目标仓库由父层指定为 https://github.com/tspike630/trade 。本轮未连接/写入GitHub、未提交/推送/创建PR，也未上传任何服务。

## 事实与不可越过的停点

- 已读 `/workspace/handoffs/readonly-evidence-t586-independent/AUDIT_REPORT.md`：2026-11-21 **20:54 HKT** 独立结论为离线PASS，原R1已闭合，实际173 PASS、0 FAIL/ERROR/SKIP（作者121+原独立21+此前有效19+新独立12）。这是方法执行计数，不是173个全新安全性质。
- **真实取证仍未授权、未执行；AAVE当前方向/子原因/余额负债/保护状态未核实；生产既有三红尚未解除。** 不能由NULL、ALL_DONE、protection_blocked或合成测试推账户安全/裸仓。
- Tiingo独立候选工作仍NO-GO，最近25秒问题原因未定位，**勿装盘**；既有采集进程与新候选不是一回事，不能据此自行停启任何进程。
- 旧运行树为本机特有9个v4目标+16个基线混合形态，**不能用GitHub main替代现场**。归档不是正式代码覆盖或部署，也不授权重启、交易/借还/挂撤单、改账/归档、改state/水位/门禁或倒游标。
- 后续真实只读取证必须另行明确批准配置及执行。这里的模板故意无凭证名/时间/有效审批，不能自动补全；不读取真实env、secrets目录内容或token。

## 本包到底包含什么

仅10件从t586原文件逐字节复制的源码、纯合成测试、离线脚本、无敏感值模板；当前SHA逐件与独立审阅冻结清单一致。另有新编精炼说明/历史索引及静态审查清单。没有重新运行候选/测试/API，没有将历史报告/附件转换成代码归档。

| 文件 | 字节 | SHA256（原字节） |
|---|---:|---|
| `aave_readonly_candidate.py` | 35417 | `a283f2c23cb8ff5a450bb8d9ba3b1878adaab48c2d162870eb4fe588ab3af225` |
| `pinned_ledger.py` | 5332 | `bff756a6da9b08fa3586751aaf137b6ef4cb66f441fc62c1876c8e00cd6b4c28` |
| `aave_evidence_review.py` | 15251 | `02854a2af802f50afff6e615f1f2e5fdf3fbe29400d45cbaf92a79967d6c0632` |
| `synthetic_harness.py` | 3038 | `980607ba35bb24c6980e50f7567251ae81c9308b6498f375ca69d012157956da` |
| `test_aave_evidence_review.py` | 12093 | `90546c02d25d1c33549ae0781cd549cf43821881001fddabe26413d710e94cbf` |
| `test_candidate_v3_regression.py` | 18071 | `befa74f93cb8b14ba706ede182e0eb961631c5e20871f58a690d0370a63afda5` |
| `test_t584_fixes.py` | 13962 | `39040f61342c8ccddaa93831a738b7cf94f670305e4f7cb8ed042247e282be3a` |
| `test_t586_oco.py` | 7097 | `87affcdeabeaeb725d648079ba8867c6cd11d45615da076cd796e3e0d85ecc0b` |
| `run_offline_tests.sh` | 468 | `b1efa3cdf5a67d46c4e9a29f95d7cfd952edea1a5d271b91715fa61b4671d6f6` |
| `approved-config.TEMPLATE.json` | 436 | `0ac630861877113936d41e98800b455f1a8f1c6777c9c7bff3ccd76bd0686ad6` |

`aave_evidence_review.py` 是历史core/helper；主候选入口是 `aave_readonly_candidate.py`。源码包含未来明确授权后的只读适配能力，但默认仍PLAN_ONLY；本轮没有执行它。

身份门控UID及错配测试的数字常量、官方域名、本地固定ledger路径、测试中的synthetic/fake秘密标记被原样保留：这些是功能/反例字节，不是API密钥、环境变量值、真实账户响应或凭证目录内容。路径字面量不等于授予读取权限。若目标仓库的可见性政策连这些必要定位标记也不允许，应停止该文件同步，另行审定，不静默改字节后仍称原173PASS版本。

## 恢复与测试依赖（不能冒称自包含173重演包）

1. 父层CloudAgent只可读取本归档白名单，在仓库新增独立 `archive/readonly-evidence-v4-t586/`（如已存在则先比对，不覆盖不明文件）。不要覆盖正式runtime文件、自动部署、合并或执行任何真实取证。
2. `ARCHIVE_BUNDLE.json` 是纯UTF-8文本的文件容器；每项有相对path、原bytes、sha256、text。恢复时只允许预定相对路径，拒绝绝对路径/`..`/symlink，对text的UTF-8字节重新计算SHA后再决定写入。它不是待执行脚本，不执行其中源码来“导出”。
3. 不修改通过的源码或测试，不把模板改成真实配置，不执行任何执行flag/production worker，不source环境文件，不读取凭证。GitHub远端比对由父层另行负责，本文未证明任何历史材料已经上传或未上传。
4. **包有意不携带原大公开文档fixture，也不携带独立52项脚本/原始审计证据**；故不能把本包称为完整173项自包含重放包。源测试31还依赖相邻 `docs/binance-official-rest-api.md`；其原公开fixture本地恢复位置为 `/workspace/handoffs/readonly-evidence-t586/docs/binance-official-rest-api.md`，独立冻结记录为 211871 B / SHA256 `f1500faf7ede9e1b24087e31e3d2517eb56db28f1d936fdfde845ce418ae3ca8`。本轮未复制/重新扫描该fixture。需另行只对这份公开依赖做安全审查，或找匹配版本公开资料；**缺依赖就明确报未运行，不删测试、不伪造标记、不降低断言凑绿**。
5. 离线脚本保留原字节，仅运行原作者121项，不是173项；本轮没有运行。保留脚本不代表可在任意机器/缺依赖环境复现，也不把文档或本地SHA当远端账户验收。

## 明确排除

不含原始日志、snapshots/JSON回执、生产DB/账户/余额/订单/新闻数据、真实API响应、state/alerts/水位、实际env/key/token、凭证目录内容、tar/zip附件、过去被安全检查阻断的材料、**任何Tiingo脚本/报告/附件内容**，也不附整目录或现网仓库。历史索引只写非敏感状态与本地定位；它不是授权按路径递归收集上传。

静态模式检查及对合成来源的审阅未发现实际secret/联系方式；这不是形式化无秘密证明。SHA只覆盖本轮这份小白名单和新说明，不读/扫描/hash任何生产主库。原t581–t586及独立目录未改字节。

若后续安全审查阻断任何同步，必须停止并报告，不拆分、改名、转码或换通道绕过；本归档本身不承载先前被阻的载荷。
