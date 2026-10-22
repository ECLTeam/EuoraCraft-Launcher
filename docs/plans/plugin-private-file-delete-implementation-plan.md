# 插件私有文件按版本删除实施方案

日期：2026-10-22（Asia/Shanghai）。状态：用户已确认路线A并实施，`files.delete` 已开放；验收与交付见第11节。第2、10节保留方案阶段证据。

基线：主仓库及原工作区本地 main 为 `f209f93`，前端为 `320cc0d`。接续 [P09 A 私有文件](plugin-private-files-implementation-plan.md)、[P10 B 下载任务](plugin-download-task-implementation-plan.md) 和 [总体设计](../designs/plugin-worker-api-v2-implementation-design.md)。目标为补齐私有空间文件清理，不同时实施覆盖写入、实例修改或声明式 UI。

## 1. 路线与需要确认的取舍

| 路线 | 行为与代价 | 建议 |
| --- | --- | --- |
| A：原始句柄按版本删除 | 独占打开已存在文件，校验 etag 后接受内核删除；实际删除前的可恢复失败清除删除标记，目录项消失才报告成功；不增加回收站和跨重启恢复记录 | 推荐作为首个删除切片，本方案细化此路线 |
| B：同卷备份/隔离区事务 | 原目标改名到内部隔离区，需持久化阶段记录、回滚冲突处理、崩溃后用户恢复入口和额外界面；可提供更强的恢复能力 | 需要保留已提交文件的恢复能力时另行细化 |
| C：覆盖和删除一起完成 | 增加替换新旧对象的原子条件与恢复，关联暂存和目标的双重版本竞争 | 暂不采用 |

总体设计13.6原有“同卷可恢复暂存后提交”的删除设想属于路线B。用户已确认先采用A及其恢复范围收窄，总体设计和现行协议同步更新。

A的恢复范围为仍持有原目标句柄、尚能撤回删除标记的失败。成功删除不进入回收站，不提供撤销删除；提交接受后的崩溃或原生状态不确定不能承诺恢复。需要跨重启恢复或已删除文件找回时，应选择B，不能把A的验证当作B完成。

## 2. 当前实现与本轮调研证据

| 位置 | 已确认行为 | 本切片缺口 |
| --- | --- | --- |
| `runtime/file_handles.py` | 目录句柄相对打开；读句柄没有DELETE访问；创建暂存拥有读写和DELETE，不共享写/删除；`discard`清理未发布暂存 | 不能把暂存discard当作正式目标删除；需明确的目标删除句柄和状态观察 |
| `FileInformation.etag` | 组合物理身份、大小、修改时间与完整SHA-256 | 删除必须在同一稳定句柄上重算并比较，而非只查路径/时间 |
| `runtime/file_storage.py` | 安装目录I/O串行、唯一暂存归属、目标占用、磁盘配额 | 须跟踪删除占用与真实句柄，统一拒绝上传/下载/删除竞争同一目标 |
| `runtime/operation_adapters.py`、`services/operations.py` | 有原始lease、任务引用、提交接受、cleanup和审计；任务结果须显式绑定result_lease | 删除结果仅为控制确认；另需将已有真实清理状态投影为cleanup_pending |
| `grant_models.py`、`grant_store.py`、Broker、`resources.list` | 私有文件只开放read/write，resource_refs必须为空 | 增加独立delete许可及同样的私有空间范围约束 |
| 前端权限弹窗及六语言 | 按能力键显示文案，未知键会退回原始能力字符串 | 增加删除授权文案，说明适用范围及成功删除不可恢复 |

仅在Git忽略的`build/worker-manager/p09-delete-plan-probe-*`中创建人工文件，使用当前相对目录原生绑定，生产模块和用户数据未变。最终探针`p09-delete-plan-probe-e38723c14229499b9e1df25785c603dc/result.json`确认：

- READ_DATA + READ_ATTRIBUTES + SYNCHRONIZE + DELETE、share=0的目标句柄可阻止普通内容读取、写入及重命名。
- 设置删除标记后可查询pending；在句柄未关闭前清除标记，再关闭，原字节及etag保持不变。
- 没有其他持有者时，标记后关闭目标句柄会移除目标。
- 已有仅READ_ATTRIBUTES、允许共享的元数据句柄仍可与上述句柄共存。宿主目标句柄关闭后，DeletePending仍为true且原名称仍被目录枚举；直到元数据句柄关闭才真正消失。

这只是原生可行性证据，未验证完整RPC、回滚故障、发行物或正式删除业务。现有相关基线测试为131 passed、1 skipped，21.98秒，涵盖授权、私有文件、原生句柄及任务归属，不作为新功能通过证据。

原生依据为微软的[删除信息](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntddk/ns-ntddk-_file_disposition_information)、[共享模式](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)、[标准文件信息](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_standard_info)。只按文档和本机NTFS实测的范围设计，不推导其他平台或文件系统同样可用。

## 3. 请求、权限与结果

```python
receipt = host.call(
    "files.delete",
    {"target": file_ref, "expected_etag": file_view["etag"]},
    mutation_key=delete_key,
)
# {"operation_ref": "...", "state": "queued"}
```

严格DTO只允许`target:FileRef`及必须提供的64位小写十六进制`expected_etag`。名称非空，沿用平铺ASCII文件名及设备名、ADS、父级、尾点和链接拒绝规则；不接受目录、通配符、递归、force、真实路径、安装/run主体或额外字段。

只允许当前安装私有根，保持Windows x64本地NTFS范围；对象至多8 MiB，超限返回`PLUGIN_FILE_TOO_LARGE`，不扩大现有文件读写承诺。删除不初始化首次空间；目标缺失明确失败，不当作幂等成功。

新增独立`filesystem.delete`能力。声明和用户决定的resource_refs必须为空，不从filesystem.write或read继承。`resources.list(capability="filesystem.delete")`可发现本run私有根，无读取或创建副作用。删除不要求当前仍有read许可，但调用方须已持有正确版本；内部完整摘要校验不返回文件内容或元数据。

提交RPC最多5秒，只验证授权、根引用和登记目标占用后接纳任务，不在RPC中执行完整摘要或长期持有目标句柄。任务总预算从首次处理提交起30秒，含排队和成功确认，截止后不接受新的删除提交；系统调用或延迟清理仍按实际状态跟踪。

任务种类为`plugin_file_delete`，沿用自身operations.get/list/cancel和mutation去重。原始删除lease绑定执行、检查点及最终接受，禁止换新lease继续旧任务。同键同请求返回同一回执，不重复删除；不同请求冲突。等待超时用原键查mutations.get，不假设操作已经停止。

成功仅返回`{"deleted":true}`，表示已确认所选版本的目录项移除。以原始删除lease作为这个不含文件信息的控制结果准入，不借它投影FileView或正文。后来失权时result为null，已提交状态不变；没有独立read能力仍能使用安全的自身状态和取消入口。

## 4. 占用、句柄和版本边界

应用唯一PluginFileStorage增加职责明确的删除资源记录，保存目标、原lease、原根/文件身份、内部引用、句柄及删除/回滚确认信息，不复制任务state/effect。不能把正式目标伪装成StagedFile，或交给暂存discard自动删除。

上传/下载与删除记录在同一存储短锁下检查安装及大小写不敏感的精确名称占用。同一目标冲突返回PLUGIN_FILE_BUSY。删除最多每run4个、宿主32个未释放记录，与上传字节预留独立，不要求另有8 MiB空闲才能删除。活跃删除不提前扣除原文件大小或跳过目录项来释放配额。

排队只登记占用，不提前打开目标。实际执行在安装目录I/O锁下验证原根，再相对该目录打开原目标的读+DELETE专用句柄，share=0，禁止重解析、目录和硬链接。句柄必须在第一次元数据验证前登记所有者；验证/关闭失败也不能丢失跟踪。

读取初始信息、完整摘要及最终信息，在同一个句柄上验证身份/长度/修改时间稳定，计算etag与expected_etag比较。同名重建、相同字节但物理对象不同、实际内容修改都拒绝。任务排队期间目标可变化，因此入队时任何预检都不代替执行时复核。

锁序沿用授权锁→运行引用/任务/存储短锁；目录与单对象I/O锁使用安装目录→删除资源。任何授权、任务或存储全局锁内均不得打开、查询、哈希、标记、关闭或写审计。保留必要的安装目录I/O串行，不依赖路径先检查后unlink。

## 5. 删除接受与失败恢复

执行顺序为：原始准入/取消/总截止 → 验证根及目标占用 → 打开并登记原句柄 → 完整版本核验 → 同一原lease和任务锁接受提交 → 持久化committing审计 → 内核标记并关闭 → 按原对象身份确认目录项移除。

提交接受之前不设置删除标记。取消、撤销、截止、版本冲突或提交前审计失败时只关闭已打开目标并释放占用，原文件保持。接受之后不再接收取消；后续撤销、Worker退出或等待超时不伪造回滚。

新增原生操作明确区分：设置删除标记、查询实际pending、清除标记、确认自身句柄关闭，以及按根句柄查询名称对应的物理身份。不得复用普通Path.unlink、按名称重开补删、宽泛重试或Path.replace回退。

设置标记或关闭失败、且原句柄仍可确认有效时，先尝试清除标记并确认pending=false，再关闭。只有已证明未删除/已回滚才使用OperationCommitRejectedError收敛为零业务效果；回滚、查询或关闭仍失败时保留unknown和cleanup pending，不能不加判断地finally close而触发删除。

自身CloseHandle成功只是必要条件。对普通目标名称的有界原生目录查询应提供物理身份（卷、文件ID、创建时间等与当前FileIdentity一致的信息），在同一根上确认原版本是否还存在；可使用受信过滤的FileIdBothDirectoryInformation等原生信息，实施时需对真实结构布局和返回结果单独验证。不能通过一个“名称不存在”路径检查替代根/身份边界。

原目录项消失或已是明确不同物理对象，均表示原版本已移除；不得删除后来同名新对象，也不得把新对象的存在当作原删除失败再执行一遍。

若元数据句柄等因素使原版本仍处于待删除，任务在剩余预算内进行有限观察。截止后不能交付deleted:true：任务返回`PLUGIN_FILE_DELETE_PENDING`，effect保留unknown，cleanup_pending=true。原已接受的内核删除仍可能在外部句柄释放后完成；调用方不能把该失败理解为文件已保留或换键重删。

本方案不承诺阻止同用户进程的句柄复制或内核/文件系统故障；这是Host API事务边界，不新增OS沙箱或跨重启恢复。宿主退出导致已接受标记的句柄被内核关闭时，删除仍可能完成，审计不能据此宣称回滚。

## 6. 真实清理与状态投影

在现有OperationSnapshot和插件TaskView增加`cleanup_pending:bool`，由真实future、领域cleanup与审计状态派生，不创建新的可独立修改任务状态。非终态保持现行执行阶段；终态仍有待清理工作时该字段为true。同步生产方、消费方、测试、示例和文档，无旧字段别名。

监管和自身查询需要有界地重试已结束任务的清理/审计，并刷新该投影；一个任务尚未结束不应阻止其他终态任务收尾。只重试资源释放、撤回未完成的删除标记或观察已提交的原对象，不重跑工作函数、重新标记原路径或重新计算并删除后来对象。

清理动作按资源已知状态选择：

| 已知状态 | 允许的收尾 | 结果边界 |
| --- | --- | --- |
| 尚未标记 | 关闭原句柄、释放占用 | 不删除原文件 |
| 需回滚且原句柄有效 | 清除标记并确认，再关闭 | 回滚未确认前不无条件关闭，不覆盖unknown |
| 已确认自身关闭，原版本仍待删除 | 通过原根观察原版本目录项 | 不再设置删除标记，不触及同名新对象 |
| 原版本已移除 | 释放句柄/占用与清理记录，重试审计 | 只有这里才确认相应文件资源释放 |
| 身份、句柄或内核状态无法确认 | 保留记录、unknown和pending，明确错误 | 不伪造none、成功或自动按路径修复 |

RuntimeFiles.is_idle、PluginFileStorage.close与Worker停止/重新启用门禁都纳入删除记录。保留原根目录句柄直到实际收尾结束。未释放的独占句柄可能让目录列表或配额查询返回FILE_BUSY；不跳过这些文件或使用默认零大小规避占用。其他可独立访问文件保持现有读操作语义。

探针发现的元数据句柄延迟也影响“删除暂存后立即归还名额”的假设。实施时对当前run已登记的上传/下载暂存补相同回归；若现有discard清理仅凭关闭成功放开资源，针对同一已知暂存身份补齐消失确认，不扩展为扫描历史或自动清理用户文件。

## 7. 前端与实际改动范围

- `grant_models.py`、`grant_store.py`：delete独立能力及空范围约束，必要/可选授权、内容绑定和原lease规则沿用现有实现。
- `runtime/models.py`、`broker.py`、`file_api.py`：严格FileDeleteRequest、resources.list支持、files.delete去重与控制回执、任务接纳和结果投影。RPC等待超时不取消任务；提交接受点核验RPC及任务各自截止。
- `runtime/file_handles.py`、`file_storage.py`：目标删除专用打开、原生标记/撤回/身份观察、资源归属和跨上传/下载目标占用；保留现有读/创建语义，新增入口不是对旧接口的兼容转发。
- `services/operations.py`、`runtime/operation_adapters.py`及监管接线：中文“插件私有文件删除”、原始删除准入、真实cleanup_pending及终态清理重试；不增加执行器，不改变桌面任务五态或伪造完成。
- 前端六语言增加`plugins.capabilities.filesystem_delete`，说明只删除自身私有文件、仅本地NTFS且成功删除不可恢复。复用现有权限弹窗和保存，不增加每次删除都弹窗的交互；补文案及授权状态测试，界面尺寸/主题按AGENTS验收。
- `tests/`新增删除API、原生故障、真实Worker和冻结用例；新增`worker_file_cleanup`示例，必须明确传入目标、etag和保留的mutation_key，不在加载/卸载钩子自动扫描或删除文件。
- 正式协议、总体设计及本方案补验收记录。Core、Florolding、网络传输策略和下载预算不改；不加入覆盖、目录递归、兼容、迁移或恢复旧run控制。

新增或修改的正式管理IPC如确有必要才按AGENTS声明IpcLogPolicy；此路线计划只增加Worker Host API及现有权限文案，不新增恢复管理命令。业务日志只记录中文动作、阶段、稳定码和耗时，不记录文件名、路径、etag、摘要、完整参数或内部任务编号；必要审计按现有白名单原子保存。

## 8. 实施步骤与开放门槛

1. 先把已确认探针转换为正式原生回归，补已有内容/元数据持有者、映射、只读文件、回滚失败、关闭失败和目录身份查询ABI。特别验证pending、原目录项与同名新对象的区分；不能证实则保持files.delete关闭。
2. 接入独立权限、根发现、严格DTO和同目标占用，验证删除许可不隐含读写，预留及执行器拒绝的资源都能释放。
3. 实现有限删除任务、原始etag/准入检查、提交接受和回滚分类；补齐cleanup_pending和监管收尾，再加入实际API路由与mutation回执。
4. 补六语言文案、例子和正式协议；只在原生、业务、清理三类门禁通过后将filesystem.delete列为已实现能力，不能用空成功占位。
5. 完成下节全部适用检查及最终代码两种冻结产物验收；提交功能、验收文档并按子仓库先行顺序安全集成本地main，不推送或发布。

## 9. 测试矩阵与验收

| 领域 | 必须验证的行为 |
| --- | --- |
| 输入/权限 | 严格etag、名称及额外字段拒绝；read/write不代替delete；声明和决定资源范围均为空；非主WebView不能管理授权 |
| 版本/路径 | 0字节和8 MiB正常；原字节修改、同内容同名重建、根替换、junction/ADS/硬链接、目录及超大对象拒绝，原文件保留 |
| 排队/并发 | 排队无文件副作用；与上传/下载同目标竞争；4/run、32/host限额；排队取消、执行器拒绝、同mutation_key并发仅一次动作，跨安装/run拒绝 |
| 提交与审计 | 取消/撤销/到期/截止先于接受则不标记；接受后保持实际结果；提交前审计失败零删除、成功后审计失败不重删；没有read也只得到安全确认 |
| 原生失败/延迟 | 标记/查询/清除/关闭故障的明确结果；外部只读元数据句柄延迟删除；己方关闭但目录项存在不成功、不放配额；同名新对象不被补删 |
| 清理与退出 | 清理失败只重试允许动作；晚到真实删除、unknown不伪造回滚；pending阻止重新启用；源未标记时崩溃保留文件、接受后崩溃允许真实删除，旧run引用不恢复 |
| 现有行为 | 文件新建/上传abort/下载清理、GET/HEAD、实例查询、设置和自身任务回归；补当前暂存的元数据持有者延迟确认 |
| 产品与发行物 | 六语权限允许/拒绝/撤销及明确文案；实际Worker创建文件→取etag→删除→确认缺失/取消/延迟状态；PyInstaller/Nuitka分别验证最终产物及真实主窗口 |

按 [AGENTS.md 第6节](../../AGENTS.md#6-测试与验收) 执行主仓库Ruff检查和格式、全仓GitHub格式检查、完整pytest、Core既有lint与集成测试。Ruff旧--format不受当前版本支持时记录实际拒绝及等价--output-format github结果，不修改工具或跳过检查。

前端先pnpm check，再pnpm build；真实权限弹窗覆盖Classic/Folia、亮暗、相关背景/透明/模糊及默认、960×600最小、放大窗口，六语言长文案、Tab/焦点/末项和保存操作可达。以当前真实约束验收，不把DOM测试当作布局验收。

Windows/POSIX公共分支及拒绝策略测试应通过；正式文件删除及Worker只承诺真实Windows x64/NTFS。明确记录没有执行的POSIX实机或远端CI，不能把方案探针或既有下载验收当作本切片完成。

## 10. 原方案阶段交付记录

本轮只新增实施文档及总体设计中的待审阅链接。原生人工探针证明关键的可恢复标记与延迟删除边界，基线131 passed、1 skipped；正式删除API、六语言文案和TaskView字段均未修改。

提交前检查本地链接/章节、Markdown结构及git diff --check；文档独立提交并快进同步原工作区本地main。frontend/Core/Florolding不改指针，保留无关改动和现有远端分歧，不推送。

上述方案随后获用户“开始实施”确认，按第8节实施路线A，结果如下。

## 11. 实施与本地验收

功能提交 `b6ba117`；前端提交 `ae37c5ff6c0ae9d8ccefabaa4be2bef2d801a4d6`。主仓库及前端均先完成检查和提交，再快进集成到原工作区本地main。Core和Florolding指针未变；无迁移、兼容入口、历史文件扫描或远端推送。

已实现严格删除DTO、独立权限及根发现、跨上传/下载名称占用、30秒含排队预算、原句柄完整版本核验、接受前审计、删除标记撤回分类和按根目录文件ID确认实际移除。正式目标始终使用删除资源记录，不交给暂存discard。等待外部元数据句柄期间释放安装I/O锁，保留原对象及名称占用；仅重试观察，不重开名称补删。

OperationSnapshot与TaskView增加派生cleanup_pending。监管和自身查询重试终态清理/审计；一个活动future不阻止其他终态任务收尾。撤回或晚到清理完成不覆盖unknown；已关闭目标但原目录项尚存时保留资源并阻止重新启用。当前上传/下载暂存同样确认已知原身份消失才归还预留，未新增旧残留清理。

六语言权限说明明确自身私有、本地NTFS及删除成功不可恢复；复用现有权限弹窗。新增worker_file_cleanup示例，显式要求目标、etag与保留的mutation_key，加载/卸载均不自动删除。已用正式打包器生成本地 `build/worker-manager/worker_file_cleanup.eclplugin`。

| 检查 | 本地结果 |
| --- | --- |
| 主仓库 Ruff | `ruff check ECL tests`、`ruff format --check ECL tests`、`ruff check . --output-format github` 均通过；CI旧 `--format github` 被当前版本明确拒绝，使用等价选项，不修改工具 |
| 完整pytest | 1954 passed、21 skipped，192.19秒；最终改动仅补公开接口docstring说明后，相关回归68 passed |
| Core | 子仓库 `ruff check .` 通过；主仓库运行Core测试16 passed |
| 前端 | pnpm check通过：格式、ESLint、类型及921 passed / 1 todo；随后pnpm build通过 |
| 原生及删除边界 | 目录身份ABI、真实独占和元数据延迟、撤回标记、映射占用、只读/目录/硬链接拒绝、同内容重建、8 MiB边界、关闭/标记/查询/撤回故障、暂存延迟归还及进程终止行为通过 |
| 任务与授权 | 去重并发、名称竞争、run限额、排队取消/截止、执行器拒绝、接受前撤销/审计失败、接受后撤销、无read安全确认、unknown及晚到清理、活动任务不阻止终态收尾通过 |
| PyInstaller / Nuitka Worker | 两种onefile重建，各18 passed，覆盖源码和冻结Worker的删除、新建、下载、本地TLS网络、任务及bootstrap |
| 实际启动器 | 两种原生主窗口均验收离线归档安装/重启、权限保存、真实Worker创建→取得etag→删除→确认、设置/实例/原生扩展及撤销停止；每种144个WebView2布局组合，共288个 |

界面矩阵覆盖六语言、Classic/Folia、亮暗、背景关闭/开启（透明度0.55、模糊12）及1200×800、960×600、1600×1000。检查文件标签与复选框不重叠、无横向溢出、页尾/保存可达、末项可滚动、键盘焦点和站点列表End/Tab；真实主窗口核对filesystem.delete允许和撤销后的实际决定。Playwright通过CDP连接实际WebView2，截图与矩阵保留在Git忽略的 `build/worker-manager/output/playwright/delete-ui-{pyinstaller,nuitka}`。最终仅docstring补充后的产物重建再次验证相同界面和删除流程；布局文案不变，复用已通过的完整矩阵。

源码全量和冻结Worker日志位于Git忽略的 `build/worker-manager/delete-*.log`。额外公网下载探测曾遇到PLUGIN_NETWORK_FAILED（连接或响应失败）；没有放宽网络策略或重试逻辑。最终原生界面验收不依赖公网下载，本地TLS下载/网络源码和两种冻结Worker回归均通过。该次公网探测不计作成功。

正式删除仅承诺本机Windows x64、本地NTFS；POSIX显式拒绝分支已自动测试，未做POSIX实机删除或远端CI。成功删除不进回收站、不可撤销；未标记进程退出保留文件，已标记进程退出可能完成删除，不提供跨重启恢复。保留unknown是实际可知效果的边界，不能因资源最终释放改报成功。

本地集成保留原有远端分歧，仅更新本地main；没有推送、标签或发布。本轮文档以独立docs提交交付。
