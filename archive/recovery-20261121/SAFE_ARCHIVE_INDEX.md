# SAFE_ARCHIVE_INDEX：t587/t588 历史恢复盘点

更新时间：2026-11-21 21:02:39 HKT。本地只读盘点/整理，**本轮无GitHub调用、无上传**。本表不含历史附件、报告或Tiingo代码的载荷；“本地存在/安装回执”绝不等于“已上传”或“未上传”。只可同步本文及下述明确白名单，不按本地路径自动收集文件。

## A. 唯一可同步的实际文件白名单

- `payload/aave-readonly-v4/` 下恰为 `ALLOWLIST_SHA256.json` 列出的10个文本文件：主候选、FD helper、core、synthetic harness、4份作者合成测试、原离线runner、无值配置模板。每文件保留t586原字节/SHA并匹配独立冻结记录。
- 新编 `README_RECOVERY.md`、本 `SAFE_ARCHIVE_INDEX.md`、`FILE_REVIEW.json`、`ALLOWLIST_SHA256.json`；主交接容器 `ARCHIVE_BUNDLE.json` 仅收录这14件文本。外部 `PACKAGE_SHA256.json` 记录容器及明文文件SHA，不自包含散列自身。
- 未复制任何原始审计报告/日志/JSON证据；173PASS只在新说明中作准确摘要。其本地真源是 `/workspace/handoffs/readonly-evidence-t586-independent/AUDIT_REPORT.md`。
- 历史补丁、旧报告、Tiingo各版、生产代码与tar/zip均**不在本轮载荷白名单**；下表只有非敏感恢复指针。需要以后同步某个历史安全源文件，必须先另列明确小白名单逐件审查，不能用“补档”绕过此前附件阻断。

## B. 本地历史索引（状态来自已存回执，不是新生产探测）

| 组件/阶段 | 已存结论与限制 | 本地恢复/索引位置 | 本轮可同步内容 | 远端状态 |
|---|---|---|---|---|
| PR224 最近全仓专家v4 t542 | 独立整体PASS；按固定源包+现场cityu补丁验证；作者套件实测15/15，不能复述成50/50。适用9个v4目标+16个未改基线，非滚动main。 | `/workspace/handoffs/pr224-expert-t542-v4/T542_V4_INDEPENDENT_AUDIT.md`；同目录manifest/runtime.patch/cityu.patch仅作本地指针 | 仅本行状态，不含补丁或原报告 | 待CloudAgent按目标哈希/提交历史核对 |
| 全仓v4磁盘安装 t544 | 历史回执记9目标受控装盘；当轮不代表进程加载，不改DEPLOY_SHA、不合并。 | `/workspace/handoffs/pr224-t544/DISK_ONLY_INSTALL.md` | 仅摘要；不复制回执/部署脚本/备份 | 待核；本地安装不能证明远端已同步 |
| 全仓加载 t546 | 随后回执记发射器/维护受控重启以加载v4；没有把混合树改为git仓库或同步版本标记。不能推当前进程状态。 | `/workspace/handoffs/pr224-t546/RESTART_RECEIPT.md` | 仅摘要；无PID/命令行/账户原始载荷 | 待核 |
| 报忧4bbe8da基线 | T541回执记独立目录文件安装，随后start回执记启动；t561导出确认仍是三基线源文件。不是suite v3.3已上线；当前存活未新查。 | `/workspace/handoffs/watchdog-20261121/INSTALL_RECEIPT.md`、`START_RECEIPT.md`；`/workspace/handoffs/watchdog-4bbe8da-export-t561/EXPORT_RECEIPT.md` | 仅摘要/版本引用；不带包、state、alerts、env或旧附件 | 完整commit仅是本地manifest引用，远端待核 |
| Watchdog suite v2 / T5 | t561导出回执明确v2未安装；T5独立反例后来用于后续套件。不能恢复旧包上线。 | `/workspace/handoffs/watchdog-suite-v2-t556/`（仅索引）；T5脚本留本地 | 不复制 | 待核 |
| Watchdog v3 t562 / T6 | 原结论FAIL：严格真实RO新闻库打开失败、没有有效JSON，不能称0RED；未安装/重载。 | `/workspace/handoffs/phase2b1-watchdog-v3-20261121/V3_INDEPENDENT_AUDIT.md`；候选 `/workspace/handoffs/watchdog-suite-v3-t562/` | 仅状态，不带候选/原报告 | 待核 |
| Watchdog v3.1 t564 / T7 | 原16通过、新8中test06失败；按硬停未进入后续真实预检/安装。 | `/workspace/handoffs/phase2b1-watchdog-v3_1-20261121/T7_INDEPENDENT_AUDIT.md`；`/workspace/handoffs/watchdog-suite-v3_1-t564/` | 仅状态 | 待核 |
| Watchdog v3.2 t567 / T8 | 原16+新10+T5 29通过，但独立并发guard 22PASS/1FAIL；未真实预检/安装。 | `/workspace/handoffs/phase2b1-watchdog-v3_2-20261121/T8_INDEPENDENT_AUDIT.md`；`/workspace/handoffs/watchdog-suite-v3_2-t567/` | 仅状态 | 待核 |
| Watchdog v3.3 t574 | 离线80PASS/0FAIL/1OBS；已存严格真实RO结果3RED/3YELLOW/4GREEN；不是0RED，未装/未重载。三红未解除，>64MiB专项覆盖不能从小样本外推。 | `/workspace/handoffs/phase2b1-watchdog-v3_3-20261121/V3_3_INDEPENDENT_AUDIT.md`；`/workspace/handoffs/watchdog-suite-v3_3-t574/` | 仅计数/停点，不带真实JSON/findings或候选附件 | 待核 |
| 早期Phase1/2A与2B2 | 顶层历史回执路径存在；本轮不读取其原始响应/行情/账户证据，不仅凭存在重判完成或当前安全。 | `/workspace/handoffs/phase1-readonly-verify-20261121/PHASE1_VERIFY_RECEIPT.md`；`/workspace/handoffs/phase2a-unlock-20261121/PHASE2A_RECEIPT.md`；`/workspace/handoffs/phase2b2-plan-c-20261121/PHASE2B2_RECEIPT.md` | 仅指针；任何相关受阻Tiingo材料仍排除 | 待安全摘录/远端核对 |
| Tiingo Pro v1 t566 | 本地报告头状态NO-GO/FAIL；不是装盘许可。 | `/workspace/handoffs/tiingo-pro-v1-t566/TIINGO_PRO_INDEPENDENT_AUDIT.md` | **仅本行状态和位置，无报告/脚本/响应/附件内容** | 未核，不猜上传与否 |
| Tiingo Pro v2 t568 | 本地报告头状态NO-GO/FAIL；不带入GitHub作绕传。 | `/workspace/handoffs/tiingo-pro-v2-t568/TIINGO_PRO_V2_INDEPENDENT_AUDIT.md` | 同上，全部载荷排除 | 未核 |
| Tiingo Pro v3 t576 | 本地报告头状态NO-GO/FAIL；不因下一版本存在反推已验收。 | `/workspace/handoffs/tiingo-pro-v3-t576/TIINGO_PRO_V3_INDEPENDENT_AUDIT.md` | 同上，全部载荷排除 | 未核 |
| Tiingo Pro v4 t579 | 本地报告头状态NO-GO/FAIL；按当前交接约束，最近25秒问题原因仍未定位，**勿装盘**。未作新诊断/请求。 | `/workspace/handoffs/tiingo-pro-v4-t579/TIINGO_PRO_V4_INDEPENDENT_AUDIT.md` | 同上，全部载荷排除 | 未核 |
| AAVE取证骨架 t581 | 33项mock PASS；仅计划/骨架，不是生产取证。旧版不作为推荐恢复版本。 | `/workspace/handoffs/readonly-evidence-t581/EVIDENCE_REVIEW.md` | 只摘要；最终V4依赖core已列白名单 | 待核 |
| AAVE候选V2 t582 | 作者73PASS，不能替代独验；后来发现F1/F2/F3。 | `/workspace/handoffs/readonly-evidence-t582/EVIDENCE_REVIEW_V2.md` | 只摘要，不上传旧候选 | 待核 |
| t583独立审计 | 原独立21项17PASS/4FAIL，归并F1/F2/F3三根因；另有真正RO换链实证。 | `/workspace/handoffs/readonly-evidence-t583-audit/AUDIT_REPORT.md` | 只摘要，不上传原探针/日志 | 待核 |
| AAVE候选V3 t584 | 作者103PASS；原非作者后续确认原F1/F2/F3闭合，但保留R1 OCO父子冲突。新增raw20中另1个loan疑点已撤回，不能把raw两个FAIL都说成缺陷。 | `/workspace/handoffs/readonly-evidence-t584/EVIDENCE_REVIEW_V3.md`；`/workspace/handoffs/readonly-evidence-t584-independent/AUDIT_REPORT.md` | 只摘要，不能推荐旧V3 | 待核 |
| AAVE最终V4 t586 | 只改schema_row闭合R1；独立173PASS（121+21+19+12），0失败/错误/跳过。真实取证未授权、AAVE当前未核；默认plan-only。 | `/workspace/handoffs/readonly-evidence-t586/`；独验 `/workspace/handoffs/readonly-evidence-t586-independent/AUDIT_REPORT.md` | 仅A节10件实际原字节文件+新说明。不是原始报告/证据外发 | 此次仅本地备好；待父层CloudAgent确认归档PR/commit |

## C. 已知GitHub引用与证据强度

1. 用户明确目标： https://github.com/tspike630/trade 。历史小审计 `pr224-t470/AUDIT_34dd807_pr224.md:6` 也引用 https://github.com/tspike630/trade/pull/224 。这是已有来源链接，**本轮没有访问，不能由链接文本认定PR当前状态或现场补丁已进入远端**。
2. T541安装回执的包内manifest记录完整报忧commit：`4bbe8da5d6964cc788a9510de624d7c0fd1e3049`。CloudAgent可将此作为待核commit，在远端确认对象/文件哈希；本地包记录不是新远端存在证明。
3. t544/t546旧回执记录根DEPLOY_SHA为 `67ba54d97b47c0f4e2bb22db758e1cf627be94e6`，但该标记当时被冻结，**不是9v4+16混合运行树的当前真源**。不可把它或远端main当已安装全量版本。
4. t542的track/entry SHA、patch SHA都是文件/载荷摘要，不是git commit SHA；需CloudAgent在远端逐文件比对，不能把SHA256当作已上传提交。
5. **本轮已确证远端内容项：0（没有进行远端读取）**。这不等于“0项曾上传”，也不等于“历史都未上传”。没有足够既有回执证明每项材料的远端归档位置，统一列待核，不猜。

## D. 父层CloudAgent后续只读核对清单（不是本轮执行）

- 在已授权仓库读取PR224/相关历史提交及archive目录；确认4bbe8da对象是否存在，记录可点击commit/PR及具体文件，而非仅报告main的HEAD。
- 按t542混合树的固定源/九目标清单查远端补丁与审计/部署摘要是否已有；同名文件不等于同字节。不要重构现场、checkout/安装main或改DEPLOY_SHA。
- 查报忧4bbe8da与v3.3是否已有安全源码/非敏感归档；区别“已装旧报忧”与“新候选80PASS但真实3RED未准入”。不要自动带出原报告或附件。
- 查Tiingo**状态摘要**是否已有；本轮及后续本授权均不把其受阻脚本/报告/附件上传。不要把“缺归档”当作装盘/重跑/绕传授权。
- 查AAVE t581–t586演进是否已有摘要；仅归档本白名单V4源字节和新说明。若远端已有相同SHA，记录其commit即可；如需补档，只新增独立archive目录，不覆盖正式代码、不合并、不触发部署。
- 每项输出已确认存在（明确commit/path/字节）、未找到（限定查询范围）、无法确认三类；“未找到”也不要扩大成“从未上传”。将最终PR/commit链接回写后才叫GitHub同步完成。

## E. 不足与排除边界

这是最近相关交付的恢复索引，不声称穷尽全部历史聊天/磁盘/仓库。未递归复制handoffs、仓库或生产树；只列顶层回执并读取少量指定状态/结论。Tiingo只分类前25行状态，不复制任何原句载荷；未读取secrets/真实env/DB/API。任何原始账户/快照/日志、历史受阻材料、依赖不明的旧部署代码均留本地，不因写进索引就进入上传白名单。

V4包保留原源码/测试字节，但完整原173项执行上下文未打包；公开文档fixture依赖及独立52脚本未列本轮载荷，详见README。缺依赖报未运行，不删测试凑绿。保存“离线PASS/当前未授权”的边界，比制造“全部已同步/可直接上线”的记忆更重要。
