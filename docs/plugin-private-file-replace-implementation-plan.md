# 插件私有文件按版本替换实施方案

日期：2026-10-22（Asia/Shanghai）。状态：路线B已实现，完成源码、最终发行物验收及原工作区本地main集成。下列第1—10节保留经批准的方案；实际结果见第12节。

调研基线：主仓库与原工作区本地main为 `d03ac01`，前端 `ae37c5f`；实施从方案提交 `5748c12` 开始。接续 [新建事务](plugin-private-files-implementation-plan.md)、[按版本删除](plugin-private-file-delete-implementation-plan.md) 和 [现行协议](eclplugin-package-format.md)。

## 1. 目标、路线和需要确认的取舍

目标：插件明确指定已有私有文件及expected_etag，上传有限新内容，版本不符时保留原文件；失败保留可恢复的原数据，不覆盖竞争者后来创建的文件。

| 路线 | 行为与代价 | 结论 |
| --- | --- | --- |
| A：严格条件的一次原子替换 | 校验版本并在一次不可被其他写入插入的操作中替换目标，目录名连续存在 | 目前测试的原语没有证明这一契约，继续专项研究时仍保持API关闭 |
| B：保留原对象，再不覆盖发布 | 用原句柄将旧文件移至同卷内部备份，随后把新文件不覆盖发布；失败尝试不覆盖恢复，冲突保留备份并提供恢复入口 | 推荐的可执行草案；需要接受目标名短暂缺失，以及跨重启恢复和管理界面的范围 |
| C：暂缓已有文件替换 | 继续唯一名称新建，下一功能改选实例资料写入等 | 改动小，但不能满足固定名称文件的更新需求 |

**B不承诺对外部路径读取者的一次原子覆盖。** 原文件移走到新文件发布之间，目标名称可能短暂不存在；异常中断时可能持续缺失到恢复完成。安装I/O锁只串行宿主自身操作，不是外部进程锁。不得把B描述为A已完成，也不使用原地截断写、普通check+replace、POSIX替换或覆盖式回滚来隐藏这一差异。

成功替换后旧版本不提供历史版本或撤销入口；备份只服务未完成事务及失败恢复。若需要每次成功后仍能撤销，须另行选择保留策略、配额和界面，本草案不包含它。

## 2. 当前代码和实际调用方

| 模块 | 已确认行为 | 替换需要补齐 |
| --- | --- | --- |
| `runtime/models.py` | FileWriteBeginRequest强制overwrite=false；FileRef平铺ASCII，单文件8 MiB | 新增独立替换输入，不修改新建及下载的禁止覆盖语义 |
| `runtime/file_api.py` | prepare要求目标缺失；write_chunk按原lease写暂存；commit使用应用任务发布 | 为已有目标准备暂存，在commit执行时核对原版本，接入唯一替换事务资源 |
| `runtime/file_handles.py` | 目录相对打开、不跟随重解析；普通发布不替换目标；删除可独占固定原对象 | 正式目标移到备份及不覆盖恢复；不能用暂存discard处置原文件 |
| `runtime/file_storage.py` | 上传/下载/删除共用安装内大小写不敏感名称占用；实际资源消失才归还配额 | 替换沿用同一名称占用，跟踪新暂存、旧备份、恢复记录与实际句柄 |
| `runtime/operation_adapters.py`、`services/operations.py` | 原始准入、接受前审计、unknown与cleanup_pending、终态清理重试 | 复用应用执行器；替换恢复不改写任务历史，不恢复旧run控制引用 |
| `worker_service.py`、`api/plugins.py` | 生命周期和主WebView权限管理；Worker及Dev Channel不能管理授权 | 安装启动前发现自身事务记录；可信主窗口查看和处理恢复，不依赖Worker仍能启用 |
| 前端插件管理 | 六语言权限弹窗及安装/启停流程 | 独立替换权限说明、恢复提示及处理弹窗 |

现有调用方为worker_snapshot新建、RuntimeDownloads新建下载和worker_file_cleanup删除；三者继续使用原契约。新增事务模块的当前调用方为RuntimeFiles、PluginFileStorage及PluginWorkerService/恢复管理入口，不创建通用文件系统框架或第二个任务执行器。

## 3. 原生调研结果

只在Git忽略的 `build/worker-manager/overwrite-plan-probe-d71b2a6d65e444899c12418b4fd835aa` 操作本轮人工文件，结果为result.json和checks.json；没有操作插件实际数据或修改生产代码。

探针使用当前相对目录句柄绑定，目标READ_DATA/READ_ATTRIBUTES/SYNCHRONIZE/DELETE，分别share=0或FILE_SHARE_DELETE；暂存经同一根句柄创建。测试FileRenameInformation（10）及FileRenameInformationEx（65）的replace和POSIX flags。

| 复现 | 本机结果 | 含义 |
| --- | --- | --- |
| legacy/extended普通replace，目标share=0或4 | 四项均拒绝；原字节仍在 | 持有当前这种目标句柄不能直接套用普通replace |
| POSIX replace，目标share=0 | 共享冲突，原目标保留 | 维持不共享删除无法用该方式发布 |
| POSIX replace，目标share=4 | 成功；旧句柄仍读到旧字节，目录名指向新对象 | 固定旧句柄不等于固定目标名称 |
| 外部POSIX替换插入最后检查之后，share=4 | 外部发布并关闭句柄后，候选再次replace可覆盖其新文件 | 版本检查与名称替换没有绑定为一个条件动作 |
| 校验后关闭独占目标，再由外部写入 | 普通replace成功覆盖后来字节 | 关闭句柄后再检查/替换仍有竞争窗口 |
| 原目标原句柄移至备份，再不覆盖发布 | 新目标为新字节，原字节保留在备份 | B的正常基本操作可行 |
| 移走原目标后模拟放弃，原句柄不覆盖恢复 | 原目标原字节恢复 | B的无竞争回滚基本操作可行 |
| 空位被外部创建后发布/恢复 | 发布和恢复均名称冲突；外部字节与旧备份都保留 | B必须有明确恢复入口，不能强行恢复覆盖外部文件 |

12项隔离原生复现符合各自预期，包括故意复现不安全替换的案例，**不是生产替换功能通过12项测试**。探针中的诊断性重查可以发现已发生的名称/链接变化，但不能消除下一次最终检查与替换之间的窗口。未证明所有Windows原语均不可能实现A，也未证明B的完整恢复、取消、配额和打包可用。

微软[FILE_RENAME_INFORMATION说明](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information)区分源句柄与目标名称，POSIX模式允许已打开目标继续存在；[ReplaceFileW说明](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-replacefilew)列出失败后原文件可能已移动或不再位于原名称的情况。因此不把ReplaceFile失败统一解释为没有副作用，也不使用它作为未经验证的备用路径。

## 4. 路线B拟定接口与授权

新增独立 `filesystem.replace`，声明和决定的resource_refs必须为空；不由filesystem.write/delete继承，不隐含读取结果。它授权替换当前安装自身私有文件及事务完成后的旧备份清理，不扩大现有“只新建”写许可。

拟定入口：

```python
root = host.call("resources.list", {"capability": "filesystem.replace"})["items"][0]["root_ref"]
upload = host.call("files.replace_begin", {
    "target": {"root_ref": root, "relative_path": "snapshot.json"},
    "total_bytes": len(payload),
    "expected_etag": original_etag,
}, mutation_key=begin_key)
# 继续使用files.write_chunk、files.write_commit、files.write_abort。
# 这些方法按实际上传绑定的原lease校验，不能把replace上传转成write授权。
```

replace_begin严格DTO只包含target、total_bytes和必须的64位小写十六进制expected_etag。不接受overwrite、force、真实路径、目录或额外字段；不支持“目标不存在时自动新建”。根发现不初始化空间。首轮仍只支持Windows x64、本地NTFS、普通单链接平铺文件；旧文件与新文件均至多8 MiB，块至多32 KiB。

begin最多5秒，绑定根和逻辑目标、登记占用并准备新暂存，不在上传期间持续独占旧文件。目标是否仍为指定版本，在commit实际执行时完整核验；begin不保证上传期间目标不变。上传继续60秒空闲过期。

commit返回TaskReceipt，类型plugin_file_replace；从commit接纳起30秒总预算，包含排队。实际执行及最终接受绑定原replace lease、取消与总截止；没有read仍可提交，但FileView结果为null。mutation回执保留原键查询，超时不代表未接受；只返回自身控制引用，不因重新授权而恢复旧上传。

候选稳定错误：版本不符PLUGIN_FILE_CHANGED，目标缺失PLUGIN_RESOURCE_NOT_FOUND，对象不合法PLUGIN_FILE_INVALID，旧文件超大PLUGIN_FILE_TOO_LARGE，竞争占用PLUGIN_FILE_BUSY，备份恢复被阻挡PLUGIN_FILE_RECOVERY_REQUIRED，恢复记录或阶段无法确认PLUGIN_FILE_RECOVERY_INVALID。错误码在实施时集中定义、文档和消费者同步；不得降级为默认成功或混用“目标不存在即成功”。

## 5. 所有权、占用和配额

- PluginFileStorage继续唯一维护安装空间、目标占用和字节预留。替换的新暂存使用StagedFile，替换资源记录引用同一个实例，不复制上传owner/偏移或任务state/effect。
- FileReplacement资源保存原expected_etag、根身份、原对象句柄及身份、随机内部备份名、新暂存和持久记录。正式目标先登记句柄所有者再验证元数据，任何失败不丢失资源。
- 上传/下载/替换/删除以同一安装及casefold名称互斥。旧备份或恢复记录未完成时持续占用目标；不能因新暂存已关闭就释放整个事务占用。
- 沿用每run4个上传、32 MiB预留、宿主128 MiB预留；替换计入上传数和预留，不再给它一套重复名额。单空间仍64 MiB、1024项，旧文件与新暂存同时计入；不提前扣除旧文件为替换腾出虚假额度。
- 备份已移走后仍计入真实容量；已登记的新暂存按预留计算一次，不重复计入。待恢复备份不因进程退出、task失败或lease撤销被忽略。
- 配额复核在持有独占旧句柄时，从同一已验证句柄计入旧对象的实际大小，不再按名称重开它造成自冲突；其他无法核实的占用对象明确拒绝，不按零字节跳过。重启后没有可恢复的上传预留，登记事务按其已核实的实际文件计入容量。
- 内部备份和暂存均为宿主生成且受记录约束的随机名称。files.list不暴露内部事务对象；未知旧残留仍计入配额，不能仅按前缀扫描删除。
- 锁序维持安装I/O锁→对象I/O锁；授权、引用、任务和存储全局短锁不执行哈希、重命名、关闭、恢复或记录落盘。

## 6. 执行、接受与恢复边界

1. 检查原lease、run、截止、取消及根。取得新暂存和原目标句柄，拒绝重解析、目录、硬链接、身份变化或超限。
2. 在同一原目标句柄计算完整SHA-256，核对初始/最终身份、尺寸、修改时间和expected_etag；在新暂存核对长度及上传完整SHA-256并flush。旧目标保持不共享读写和删除。
3. 再核对原准入和取消，通过context.finalize接受一次有限事务，先持久化既有committing审计。
4. 在破坏原名称之前原子写入严格事务记录prepared，包含当前安装和根身份、目标名称、旧/新物理身份及完整摘要、随机备份/暂存名、格式版本及revision。记录失败时不移动原目标。
5. 从原句柄把旧文件不覆盖移至备份；成功后保存old_saved阶段。不得按名称重开旧目标再移动，不修改旧文件字节。
6. 将新暂存按原根不覆盖发布至目标。发布成功后保存published阶段，再允许结束旧备份的保护；只删除记录证明的原备份对象。正式结果从发布前捕获的新快照构建，独立读lease控制交付。
7. 接受后不再把撤销、停止Worker或等待超时伪装为回滚。发布前失败尝试将同一个旧备份句柄不覆盖恢复原名称；若目标被后来对象占用，保留两者并要求恢复。
8. 原位置/备份/新暂存状态不确定时按身份观察，不盲目关闭可能会触发删除的旧句柄，不重复发布或覆盖式回滚。只有已证明原文件原位置保留/恢复且没有新发布，才用OperationCommitRejectedError确认none；其他未能确认的失败维持unknown。

不承诺断电时整个文件系统状态与单个记录同时提交。每个native动作前后的记录都可能因崩溃停在相邻阶段；恢复必须结合实际身份，而不是只相信阶段字符串。成功发布后记录失败不能删除新目标或声称已回滚。

## 7. 持久恢复与生命周期

新增安装所属的严格恢复记录目录，和插件可见文件及OperationManager审计分开。恢复记录是当前事务数据安全所需的新格式，不读取旧别名、迁移历史上传或扫描旧run文件。必要的逻辑名称保存在受信记录中，不进入操作日志；禁止真实路径和完整上传正文落盘到审计。

持久记录不保存可恢复的Worker控制权。启动后仅读取当前格式的登记记录，核对installation/root/文件身份及完整摘要；记录损坏或矛盾时保留文件并报告。未处理事务占用原目标，恢复入口不要求插件先启用；旧run的FileRef、write_ref、operation_ref始终失效。

| 实际可证明状态 | 允许的自动收尾 | 不允许的动作 |
| --- | --- | --- |
| 原目标仍是旧对象、没有旧备份，发布未发生 | 关闭原句柄；只清理记录证明的新暂存 | 把未知对象当暂存删除 |
| 原名称缺失，旧备份与原版本一致 | 不覆盖恢复原对象并确认身份，再清理自己新暂存 | 重启后继续发布旧上传 |
| 原名称是已登记新对象，发布证据一致 | 记录已发生发布，只清理原备份及记录 | 将已提交结果伪装为取消、回滚或恢复旧run |
| 原名称被后来对象占用，旧备份仍在 | 保留原备份和后来对象，转交用户恢复 | 删除后来对象、自动换名覆盖、重复执行replace |
| 身份/摘要/记录阶段矛盾 | 保留对象，明确恢复错误 | 猜测目录归属、忽略损坏记录或伪报none |

终态任务cleanup_pending从实际句柄、旧备份、新暂存、恢复记录及审计派生。一次真实native发布已确认但备份关闭/删除延迟时，effect可为committed而cleanup_pending仍为true；若发布结论本身未知则保持unknown。后续收尾不把unknown改成成功。

新暂存、旧备份和事务记录都完成必要收尾后，才能释放占用和名额。外部元数据句柄延迟删除继续使用既有身份观察；不得只凭自身CloseHandle成功归还容量。监管仅重试清理、恢复或观察，不重放替换。

未解决事务阻止同目标修改及该安装重新启用。禁用/退出只停止新执行并等待实际任务，不能删除备份或恢复记录。更新、卸载不能清除未完成恢复；即使插件代码或安装资格已被外部移除，主窗口也应能按持久安装/root身份列出其待恢复记录，保留用户数据，不依赖插件名称仍可解析。

## 8. 可信恢复管理和六语言界面

只有原生登记的主WebView可列出/处理恢复，复用_permission_caller边界；Worker、插件自定义窗口、Dev Channel和客户端伪造windowType均拒绝。拟增加plugin_file_recoveries_list及plugin_file_recovery_resolve，并在严格IPC模型、命令注册和IpcLogPolicy声明中文名称和查询/操作分类。

列表有界分页，返回当次宿主的recovery_ref、逻辑名称、可读恢复原因、revision、状态摘要和安全动作，不返回真实路径、原始异常或旧run控制引用。resolve提交recovery_ref、expected_revision、expected_etag及动作；另存时才允许新的平铺名称。执行前重新取得实际对象、核对视图版本和根，不能仅凭前端类型断言判断状态。

恢复工作复用应用唯一OperationManager，主界面取得既有用户任务回执；同一记录、版本及相同选择的重试返回同一已接纳任务，另一选择须刷新状态。恢复资源仍由事务服务维护，IPC等待超时不宣称恢复已停止，不重新提交同一恢复动作。这里不恢复旧插件任务的控制权。

主窗口复用插件页面和公共Modal，提供以下明确选择：

- 恢复原位置：仅在原名称仍缺失时不覆盖恢复，否则保留冲突。
- 将原文件另存为新名称：用户明确提供未占用的平铺名称，按原对象身份不覆盖移动；不自动猜名。
- 保留当前版本并丢弃原备份：清楚说明不可恢复，用户明确确认后才处理已登记原备份；不触碰当前目标。无法确认对象归属时不提供此动作。

不新增每次替换的确认弹窗；授权弹窗增加filesystem.replace，六语言说明自身私有、本地NTFS、成功替换旧版本不可恢复。恢复提示和操作文案亦同步六语言，覆盖加载、空数据、失败、冲突、提交和实际清理结果。提交期间锁住重复按钮，旧请求不得覆盖当前记录；焦点和关闭约束复用现有Modal机制。

## 9. 实际改动点与实施顺序

| 步骤 | 范围 | 完成门槛 |
| --- | --- | --- |
| 1. 正式原生与故障测试 | file_handles及tests新增保留原对象、备份、不覆盖安装/恢复、映射/元数据持有及竞争 | 三种B基本路径及动作完成但确认丢失均能正确分类；原文件不会交给discard |
| 2. 事务资源与记录 | file_storage；新增runtime/file_replacement.py集中严格记录、对象资源和恢复协调 | 准备/接纳/清理/审计及崩溃点全部可识别；没有重复任务状态或历史扫描 |
| 3. 生命周期及恢复管理 | worker_service、api/plugins.py、api/plugin_models.py、命令注册和日志策略 | 主窗口可在不启用Worker时查看/解决恢复；跨run和非主窗口全部拒绝 |
| 4. 权限和上传管线 | grant_models/store、models、broker、file_api、operation_adapters | 原replace lease贯穿begin/chunk/commit；新建和下载仍禁止覆盖；能力尚不标已实现 |
| 5. 前端 | Plugins.vue、pluginManagementApi、类型、权限弹窗和新增恢复弹窗、六语言 | 恢复冲突可处理，操作/状态/焦点/布局真实可用，不暴露实现路径 |
| 6. Worker示例及接线开放 | worker_file_replace示例、Host API、正式协议、总体设计 | 原生/恢复/授权三类门禁通过后才加入implemented_capabilities；无空成功或旧接口回退 |
| 7. 发行物及本地交付 | 源码、两种onefile、实际主窗口；子仓库先行提交和main集成 | 以最终产物覆盖失败恢复和真实删除清理，不推送或发布 |

新增模块只服务当前替换及恢复调用方。持久记录模型和资源状态归所属事务服务；平台专属ABI仅在运行时绑定。日志只输出中文动作、阶段、稳定错误码及耗时，不输出文件名、路径、etag、摘要、正文、内部记录/任务编号。

## 10. 验收矩阵

| 风险 | 必须验证 |
| --- | --- |
| 契约与权限 | replace独立许可和空resource_refs；无read结果隐藏；write/delete不代替replace；非主窗口恢复管理拒绝 |
| 输入和版本 | 必填严格etag、名称、额外字段；0字节/8 MiB；同对象字节变化、同名同内容重建、原位置缺失、根变化均保护原数据 |
| 路径和原生句柄 | junction/ADS/目录/硬链接/只读、映射及外部持有者；每个句柄首次校验前登记；不能关闭后按路径补删/补覆盖 |
| 竞争和预算 | 上传/下载/删除/替换同目标；源替换、备份名冲突、空位被外部新建；真实字节配额；排队取消/到期/30秒截止及执行器拒绝 |
| 接受和审计 | 原lease在接受前撤销则不移动；审计或prepared落盘失败零移动；接受后撤销仍反映实际结果；同mutation_key只一个事务 |
| 分阶段失败 | 移旧前/后、阶段落盘前/后、发布前/后、备份标记/关闭/消失确认失败；确认丢失不误报none或重放 |
| 崩溃恢复 | 每个native动作和持久记录之间真实终止进程、重启；仅回滚/清理，不自动继续旧上传；损坏记录、伪造名称/身份、孤立安装均保留数据 |
| 管理操作 | 三种恢复选择、陈旧revision/etag、重复点击、IPC超时、另存名冲突、撤销清理延迟；不能覆盖后来目标 |
| 退出和门禁 | pending阻止重新启用/丢弃记录；一个活动任务不阻止其他终态收尾；unknown不因最后清理成功改报成功 |
| 既有与产品 | 新建、上传abort、下载、删除、实例只读、设置及自身任务；真实Worker完整替换和恢复；PyInstaller/Nuitka分别验收 |

代码实施按 [AGENTS第6节](../AGENTS.md#6-测试与验收) 执行Ruff、完整pytest、Core lint和集成测试。前端先pnpm check再pnpm build；真实启动器覆盖Classic/Folia、亮暗、背景/透明度/模糊及默认、960×600、放大窗口，六语言长文案、末项、滚动、键盘焦点和操作反馈。适用主仓库集成及IpcLogPolicy覆盖测试不能由子仓库测试替代。

本机NTFS结果不能推导POSIX或其他文件系统支持；平台拒绝分支须自动测试，未执行的平台实机和远端CI独立记录。两种最终冻结产物都要验证正常替换、备份恢复冲突、实际主窗口恢复和重启，不能只复用本轮原生探针或既有删除验收。

## 11. 调研阶段归档

实施前完成现状检索、微软原生契约核对、12项隔离探针复现；文件句柄、Host文件、新删除及任务归属基线125 passed，22.70秒。这些结果属于调研基线，不代表本功能验收。

文档检查涵盖Markdown结构、相对链接、现行章节引用和git diff --check；以docs提交安全同步原工作区本地main，不推送。

用户随后选择推荐B并明确要求“开始实施”，接受目标名称短暂缺失、同卷事务备份与跨重启恢复、独立filesystem.replace权限和可信恢复界面的范围。

## 12. 实施与本地验收

已接入独立替换许可、replace_begin、原对象按版本备份、不覆盖发布、严格事务记录及可信主窗口恢复。登记恢复支持孤立安装；不继续旧上传、不开放旧run引用、不加入历史兼容或格式迁移。恢复动作沿用唯一OperationManager，页面只订阅实际状态。

真实进程终止覆盖prepared写入前/后、原对象移动后、old_saved后、实际发布后、published后、备份实际删除后及外部文件占位。新宿主只恢复旧原对象或确认已经发布的新对象；冲突另存保留外部字节。格式损坏及无法核实对象保留，不猜测删除。

Windows原生测试确认两项必须单独处理的事实：写句柄关闭后时间字段可能最终更新；NTFS名称复用可能继承原文件创建时间。恢复凭据使用明确的卷、文件ID及完整SHA-256，新对象创建时间只允许本事务记录的暂存值或旧名称值。此规则不改变公开FileRef的严格etag。同名同内容重建仍不能通过公开版本核验。依据见微软[文件时间](https://learn.microsoft.com/en-us/windows/win32/sysinfo/file-times)及[NTFS名称隧道行为](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/fltkernel/nf-fltkernel-fltgettunneledname)。

外部元数据句柄可能延迟旧备份实际消失；此时任务效果可已committed，cleanup_pending和原预留继续保留，新文件句柄先释放以允许正常读取。未知effect不会因为恢复后清理成功而改成成功。恢复提交另将极短任务的可变即时状态转换为稳定接纳回执，再由应用任务查询取得真实终态，覆盖真实WebView发现的快速任务竞态。

功能提交 `49c5170`，前端提交 `ffffd04cb469cdc3a98a6d9d0dad8ace7520c762`。先快进集成前端原工作区本地main，再将主仓库功能提交快进到原工作区本地main；没有覆盖无关文件，Core及Florolding指针保持原值。文档独立提交。

| 检查 | 最终本地结果 |
| --- | --- |
| 主仓库Ruff | `ruff check ECL tests`、`ruff format --check ECL tests`、`ruff check . --output-format github` 均通过；最后一项与CI旧选项等价，不改变工具版本 |
| 完整pytest | 2005 passed、22 skipped，215.93秒；随后补入既有私有权限声明循环的replace分支，相关用例1 passed |
| Core | 子仓库Ruff通过；主仓库执行Core测试16 passed |
| 前端 | pnpm check通过，含Prettier、ESLint、类型及931 passed / 1 todo；随后pnpm build通过 |
| 原生替换与恢复 | 严格输入、独立权限、无read结果隐藏、0字节/8 MiB、同内容重建、版本变化、排队取消/撤权/到期、去重并发、阶段记录失败、外部竞争、实际元数据延迟、三种恢复选择、陈旧审阅及刷新重试通过 |
| 实际进程中断 | 上述8个崩溃点以真实子进程终止后新宿主处理，未继续旧上传；无效记录及孤立安装处理通过 |
| PyInstaller / Nuitka Worker | 最终onefile分别20 passed，覆盖源码/冻结Worker的替换、取消、新建、删除、下载、本地TLS网络、任务与bootstrap |
| 源码及两种真实主窗口 | 每种144个权限布局加144个恢复布局；离线归档安装/重启、原生权限允许/拒绝、真实Worker替换、孤立安装冲突另存及实际用户任务取消均通过 |

三种真实主窗口各覆盖六语言、Classic/Folia、亮暗、背景开启/关闭（开启时透明度0.55、模糊12）及1200×800、960×600、1600×1000窗口，共864次布局组合检查。确认按钮、页尾、末项滚动、输入焦点和键盘站点End/Tab可达，无横向溢出及内部路径泄露；另存后的原文件为原字节，原位置后来文件保持不变。最小窗口新增按钮溢出已修复，真实布局回归纳入原生UI辅助检查。Playwright通过CDP连接实际WebView2，未用jsdom代替布局验收。

验收日志保留在Git忽略的 `build/worker-manager/replace-*.log`，截图及JSON矩阵在 `build/worker-manager/output/playwright/replace-ui-{source,pyinstaller,nuitka}`。正式打包器生成本地 `build/worker-manager/worker_file_replace.eclplugin`，未发布。

本机为Windows 11 x64 build 26300、本地NTFS、WebView2 155.0.4283.62；Python 3.11.9，PyInstaller 6.22.3，Nuitka 4.2.2，PyTauri/pytauri-wheel 0.8.0，Pydantic 2.13.5，Ruff 0.16.10。最终产物SHA-256：

- PyInstaller：`947fc2c8131dda0ba8c89269261c00913b0fd3a3f829d59ff07b0ab0fe5a3b63`。
- Nuitka：`105f5a2586f967e9039ece2ce179d29e98aaf605cb651ab576969e99bdca9418`。

平台拒绝与生命周期单元回归通过，未在POSIX实机或远端CI运行；不把本机结果扩展为其他文件系统支持。替换仍允许原名称短暂缺失，成功后旧版本不可撤销；损坏记录和无法确认的对象需人工排障，界面不提供猜测删除。没有推送、创建标签或发布，保留主仓库既有远端分歧。
