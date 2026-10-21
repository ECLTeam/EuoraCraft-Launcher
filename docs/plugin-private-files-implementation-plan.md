# 插件私有文件读写实施方案

日期：2026-10-21（Asia/Shanghai）。状态：P09 首个切片的调研与推荐方案，待用户审阅；尚未修改功能代码。

主仓库基线为 `d21c285`，接续 [P08 任务基础](plugin-operation-ownership-implementation-plan.md)。Core、Florolding、前端指针沿用该提交。本文件细化 [总体设计](plugin-worker-api-v2-implementation-design.md) 的 P09，明确首个真实文件提交任务、现行协议限制及未完成范围。

## 1. 目标和路线选择

让插件通过正式 Host API 在自己的数据空间读取文件、分块上传并发布一个新文件；发布成为 P08 管理的真实任务，可查询、取消，并验证撤销和 Worker 退出时的实际收尾。示例业务为导出一份带唯一名称的 JSON 快照。

| 方案 | 本轮范围 | 取舍 |
| --- | --- | --- |
| A，推荐 | 私有空间内的普通文件读取与新文件事务发布；首轮平铺文件名，不覆盖、不删除、不建用户子目录 | 能完成真实业务闭环，并用系统的“不替换已有目标”语义保护并发创建；适合导出及快照，不能当作完整文件管理器 |
| B，完整私有文件事务 | A 加覆盖、删除、子目录和与外部编辑竞争的版本条件替换 | 更实用，但必须先证明条件替换与恢复语义；普通 check + replace 不足，需要扩大原生验证及崩溃恢复范围 |
| C，共享资源及实例资料写入 | B 加用户选择目录、实例配置/profile 等领域适配 | 还需资源目录、选择界面、领域 revision 和其他服务写入协调；超出私有文件首轮范围 |

推荐 A，供本轮审阅。它收窄了上轮建议中的通用“写入前检查版本”：读取继续支持 etag；新文件发布始终禁止覆盖，覆盖旧文件的并发保护留给 B 专项验证。用户若选择 B/C，先修订本文件再实施，不把 A 的验收泛化为全部 P09 完成。

本轮不开放 `files.delete`、覆盖写入、任意宿主路径、用户选择目录、实例/profile 写入、网络下载或 Java/Minecraft 安装。`settings.set` 继续使用现行同步协议，不统一改为 TaskReceipt。私有指 Host API 按安装隔离，并不新增 Python 的操作系统文件沙箱。

## 2. 当前证据与契约缺口

| 当前位置 | 发现 | 方案处理 |
| --- | --- | --- |
| `plugins/runtime/broker.py`、`plugin_api/contracts.py` | 业务 JSON 最多 65536 字节，信封最多 1 MiB，整数绝对值最多 `2**53-1` | 保持全局限制，按限制修订本轮尚未开放的文件 DTO |
| 总体设计 13.6 | 原 256 KiB 原始分块编码后约 349606 字节，超出业务上限 | 读取和上传原始分块均限 32768 字节，典型上传 JSON 为 43770 字节 |
| 总体设计 FileView | `modified_ns` 的当前时间值超过安全整数范围，被 `_json_value` 拒绝 | 对外改为 `modified_ms:int`；纳秒值只用于内部身份/etag，不保留旧字段别名 |
| Broker 去重回执 | 本 run 最多 1024 条；64 MiB 文件以 32 KiB 分块需要 2048 次上传 | 首轮单文件 8 MiB，最多 256 次上传；保留回执上限，容量不足明确失败，主动放弃不占新回执 |
| `foundation/files.py` | 原子替换不带根句柄、精确目标版本或授权；受控复现中检查后发生的修改被替换覆盖 | 不直接把该工具作为插件文件提交边界，不改写全应用的原子写工具 |
| `utils/instance_reading.py` | 是实例 JSON 只读边界；读取后验证句柄不能代替创建文件前的定位保护 | 保留实例职责，新文件边界单独实现；不向实例读取模块添加通用写入 |
| `plugins/manager/storage.py` | 是可信系统插件配置/禁用状态存储，按名称组织 | 不复用它存放第三方文件，不迁移旧配置或按名称继承数据 |
| `RuntimeOperations` | 已有来源、原 lease、任务引用和投影；结果目前复核写入 lease 本身 | 文件结果另绑定独立读取 lease，不能将写权限当作读权限 |

本轮相关源码基线：`test_operations`、`test_plugin_operation_ownership`、`test_plugin_instance_queries`、`test_plugin_grant_store` 共 **96 通过、2 跳过**。这些是调研基线，不是文件 API 的实施验收。

忽略目录 `build/worker-manager/` 保存 `p09-plan-baseline.txt`、`p09-plan-probe.json`、`p09-protocol-probe.json`、`p09-native-rename-probe.json` 和 `p09-native-relative-probe.json`。探针只创建独立人工目录，没有访问个人实例、修改生产模块或提供生产 API。

## 3. 私有空间和授权

宿主从活动安装记录取得 installation_id，私有命名空间固定为 `plugin_data/<installation_id>/files/`。不能由插件传入真实根目录、安装 ID、owner 或 run。首次获准写请求可初始化受信空目录，初始化和暂存创建分别是有限的授权接受动作；读查询不创建目录。首次尚未初始化可返回逻辑空目录，已经绑定的物理根丢失或变化必须明确失败，不能把损坏冒充首次运行。

`PluginFileStorage` 同时维护独立的严格空间记录 `plugin_file_spaces/<installation_id>.json`，标记 preparing/ready 及 ready 根的物理身份，用于跨重启区分尚未初始化与资源丢失。它是当前文件服务的权威记录，不修改既有安装记录格式。首次准备在接受一次确定的空间初始化后执行有限的记录/目录步骤，ready 记录成功保存后才允许上传；中途失败保留明确的 preparing/失败状态，不自动收编来历不明的目录。记录缺少但目标目录已存在、记录损坏或 ready 根变化均拒绝，保留原文件，不自动重置或迁移。

同一安装的重启及代码更新保留私有数据，但新 run 必须重新取得引用，代码更新仍重新审阅权限。卸载重装使用新 installation_id，旧目录不自动继承、不迁移、不删除。当前 run 捕获根的物理身份；被替换后旧引用失效，不自动绑定同路径的新目录。

清单仅声明 `filesystem.read`、`filesystem.write`，二者首轮都只指当前安装的固定私有空间，不包含共享目录。两种能力的清单/授权 `resource_refs` 首轮必须为空，非空范围明确拒绝；精确根和目标由宿主文件边界另行绑定，不接受清单伪造宿主资源。写能力不隐含读能力，也不授予删除。

扩展 `resources.list` 的严格 capability 枚举，支持这两种能力，返回 `{items,next_cursor}` 中的私有根视图 `{kind:"private_directory",root_ref,label:"plugin_data"}`。root_ref 是当前 run 的随机引用；取得引用不表示同时获得另一项能力。授权 epoch 变化后重新发现，旧根引用及游标拒绝。不得把当前 P07 的实例资源表改成接受任意目录的通用入口。

主窗口沿用必要/可选、永久/会话和撤销流程，六种语言明确显示“读取插件自己的文件”和“在插件数据目录创建文件”，说明首轮不覆盖已有文件。无用户目录选择器，不增加新的主界面文件浏览器，也不向自定义前端开放方法。

## 4. 首轮正式 API 和限制

所有请求使用 `RequestModel` 的严格类型与 extra forbid。FileRef 为 `{root_ref,relative_path}`；根自身用空 relative_path，仅用于 stat/list。普通目标是单个 ASCII 文件名，1—120 字符，以字母或数字开始，其余只允许字母、数字、点、下划线、连字符，拒绝尾点、Windows 设备名及其扩展形式。不接收分隔符、绝对路径、UNC/设备路径、ADS、编码转换后路径或内部暂存名。

这是首轮明确子集，不把不支持的名称自动净化成另一个文件，也不兼容多种路径表示。后续目录和 Unicode 名称支持单独完善边界及测试。

| 方法 | 输入 | 结果与语义 |
| --- | --- | --- |
| `resources.list` | `{capability:"filesystem.read"或"filesystem.write",cursor?,limit?}` | 当前 run 获准的私有根；不泄漏其他安装 |
| `files.stat` | `{target:FileRef}` | FileView；读取能力，单次 5 秒 |
| `files.list` | `{directory:FileRef,cursor?,limit?}` | 根直属普通文件的 Page，limit 1—100、默认 50；5 秒 |
| `files.read` | `{target,offset:int>=0,count:int:1..32768,expected_etag?:sha256}` | `{data_b64,eof,etag}`；读取能力，30 秒，单文件至多 8 MiB |
| `files.write_begin` | `{target,total_bytes:int:0..8388608,overwrite:literal false}` | `{write_ref,max_chunk_bytes:32768}`；写能力，创建受控暂存，5 秒 |
| `files.write_chunk` | `{write_ref,offset:int>=0,data_b64:string}` | `{received_bytes}`；严格 Base64，解码至多 32768 字节，顺序偏移，不得超过 total_bytes；5 秒 |
| `files.write_commit` | `{write_ref,sha256}` | TaskReceipt，作为首个真实任务生产者；5 秒内接纳，执行预算 30 秒 |
| `files.write_abort` | `{write_ref}` | `{released:bool}`；只释放自己的上传会话，5 秒，失权后仍可减少资源 |

FileView 为 `{file_ref,name,kind:"file"或"directory",size,modified_ms,etag}`，不返回真实路径。etag 由已验证文件身份和完整内容摘要生成，不只依赖尺寸和时间；相同尺寸或手动恢复 mtime 不能绕过读取的 expected_etag。根目录 etag 单独表示其身份/目录快照，不用于覆盖文件。

stat/read 读取实际文件句柄，拒绝非普通文件、重解析点及多链接文件；获取稳定视图期间不得与写入/删除句柄共享，不可稳定取得时返回忙，不返回半份成功。完整摘要计算及读取有总预算，不无界缓存内容。list 保存固定名称集合、逐页复核根和许可，并按实际 DTO 字节数分页；游标最多 16 个、120 秒。已失效分页不得继续返回旧数据。

`write_begin`、chunk、commit 按 mutation_key 去重，同键不同载荷拒绝；写入累计值只由上传会话维护。回执满时不能淘汰旧条目使副作用重放；中途被限流仍可 abort。不会承诺每个 run 可以无限次写入，也不为了恢复 64 MiB 上限扩大所有方法的消息或回执限制。

commit 的回执只含任务引用和接纳状态 queued，实时状态通过 operations.get/list 查询。接纳状态不声称工作线程仍在排队。业务参数及请求先校验，通过后才能消耗上传会话；同键重试取同一回执。将 commit 的安全 TaskReceipt 分类为可在原业务权限变化后查询的控制回执，不放宽其他变更结果的授权过滤。Worker 已关闭时仍拒绝请求。

abort 不依赖新增回执容量，也不能创建资源；未知、已释放或其他 run 的 write_ref 统一返回 released:false。commit 已接纳后 write_ref 被消费，abort 返回 false，取消改走 operations.cancel；不会把 abort 解释成已撤销发布。

错误沿用稳定契约：非法输入为 `PLUGIN_SCHEMA_INVALID`，其他来源/失效引用统一 `PLUGIN_RESOURCE_NOT_FOUND`；目标存在为 `PLUGIN_FILE_EXISTS`，版本冲突为 `PLUGIN_FILE_CHANGED`，占用为 `PLUGIN_FILE_BUSY`，偏移/大小错误为 `PLUGIN_WRITE_SEQUENCE_INVALID` / `PLUGIN_WRITE_SIZE_EXCEEDED`，内容不符为 `PLUGIN_HASH_MISMATCH`。读取失败为 `PLUGIN_FILE_IO_FAILED`、保存失败为 `PLUGIN_STORAGE_FAILED`；配额不足新增 `PLUGIN_STORAGE_QUOTA_EXCEEDED`，内存条目/回执耗尽用 `PLUGIN_RATE_LIMITED`。覆盖 true 和不支持字段不降级，未开放的删除方法返回 `PLUGIN_API_METHOD_NOT_FOUND`。预期原生错误在文件边界转换，不把路径或 NTSTATUS 当作面向插件的错误文本。

## 5. 原生文件边界与发布条件

新增 `plugins/runtime/file_handles.py`，只封装当前调用方确需的目录/普通文件句柄、元数据、有限读取、暂存写入、句柄删除及不覆盖发布。实际调用方是文件存储服务；不建立通用文件系统框架，不改造全仓 Path 使用。

Windows x64 第一条新建文件副作用发生前，必须已经定位到验证过的目录对象。拟使用 NtCreateFile 的 RootDirectory 相对创建单个子文件，拒绝重解析；通过同一已验证根句柄将暂存文件按 NtSetInformationFile 的 FileRenameInformation 发布，ReplaceIfExists 为 false。源暂存一直由宿主持有，检查类型、文件身份、链接数及与根同卷；不得先按路径创建，再靠事后校验补救越界创建。

微软文档明确给出 [NtCreateFile 的 RootDirectory 相对名称语义](https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntcreatefile)，以及 [NtSetInformationFile 的 FileRenameInformation 和 DELETE 访问要求](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/nf-ntifs-ntsetinformationfile)。共享方式在句柄关闭前持续生效，具体限制见 [CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)。这些文档支持候选原语，不代替本项目的竞态验收。

本机独立探针验证：RootDirectory 下相对创建成功；句柄相对不覆盖重命名成功；目标已存在时返回名称冲突，源暂存保留。另一个 Win32 SetFileInformationByHandle 相对根探针返回错误 87，故不能把该路径宣称为已验证方案，也不设置运行时备用实现。当前原生探针仅验证命名与冲突，尚未证明完整字节写入、fsync、父级重解析竞争或冻结可用性。

实施第一步必须补齐真实句柄验证：初始化根及其受信祖先、查询和创建期间的根替换/重解析修改、暂存替换、目标突现、长路径和中文宿主数据目录。目标发布冲突时保留外部文件，仅清理身份吻合的本次暂存。不能可靠锚定目录或不覆盖发布时，停止文件写 API 接线并说明具体阻碍，不自动改用 Path.replace、TxF 或先写后检查。

POSIX 的请求模型、授权和不支持分支需测试；本轮不增加 Linux/macOS 第三方执行或未经验证的通用写实现。平台原语按运行时分支初始化，不在 import 时加载 Windows DLL。

首轮真实写入限已验收的 Windows x64 本地 NTFS 空间；网络共享和其他文件系统明确拒绝，不按 API 名称推断相同保证。该限制在授权能力说明和失败结果中可辨，未来扩大范围须补充相应实际文件系统验证。

## 6. 写入会话、任务与生命周期

应用的 PluginWorkerService 持有一个 `PluginFileStorage`，维护每安装的固定私有空间、目标占用、暂存及配额；Broker 持有当前 run 的 `RuntimeFiles`，负责严格方法、租约和引用。两者不创建线程池，不复制 OperationManager 的任务状态。上传会话只维护实际字节偏移/摘要、暂存身份、租约及“正在上传/已交给任务/已释放”的资源归属。

begin、chunk 和 commit 接纳均检查同一原始 lease；任何 epoch 变化都使旧会话失效，不能重新取新 lease 继续上传。offset 必须等于已接收字节；空块拒绝，零长度文件不上传块。每个块先校验原文长度、严格解码、校验剩余空间，再接受一次有限写入。磁盘动作不持有授权锁、全局存储锁或任务锁；接纳后的该块允许完成，之后若失权则只清理暂存，不发布。

commit 接纳时完成上传会话到宿主任务的单次所有权转移，失败要么仍可 abort，要么由已创建任务负责收尾，不能留下无人跟踪的暂存。RuntimeOperations.submit 绑定宿主来源与原始写 lease。工作函数校验总字节数、完整 SHA-256、根/源句柄身份、同卷、目标占用和取消检查点，然后通过 context.finalize 接受一次确定的不覆盖发布。committing 摘要保存成功后才执行；不进行无限重试。

原生发布成功后 effect 为 committed；失败按可证明的实际效果报告，不能把任何异常都解释成目标未改变。执行方先捕获最终暂存的身份、字节摘要、大小和时间，再接受发布；成功后保存明确的已发布描述，由投影生成提交时的 FileView 快照。它不声称目标后来仍未被修改，后续 read 继续使用 expected_etag。授权及原根身份在每次投影时复核。

发布后收集附加信息或保存摘要失败不能伪报文件回滚，按 P08 保留实际结果/收尾 pending，不重复发布。源句柄一旦成功发布，finally 只关闭它，禁止继续当作“暂存”删除。发布前的失败清理若实际删除了自己的唯一暂存，应通过受信清理路径报告 effect:none；允许该减少资源的报告在取消/失权后执行，不重新授予写入，也不能覆盖 committed/unknown。清理失败保留 staged/unknown 和 pending，不能假报无副作用。

扩展 RuntimeOperations 的结果准入契约：投影与读取 lease 必须明确配对，没有读 lease 则 result:null；文件写 lease 只控制执行。已有 P08 测试调用同步更新，不保留隐式“有写就能读”的默认。提交时没有读许可的任务不能因后来增权补发结果；按新授权自行 stat/read。

Worker 关闭、断连、禁用、必要权限撤销时，先拒绝新请求，再清理本 run 的上传会话，并由 P08 取消/等待自己已经接纳的任务。可选文件权限变化只失效相关旧会话；不影响用户下载或其他插件。正在执行的上传写入或清理未结束时保持 pending，不能只凭 Broker RPC idle 或已请求删除就报告 completed。根/暂存句柄有明确的 finally 关闭者。

## 7. 配额、残留与数据保护

拟定首轮固定限制：单文件 8 MiB；每 run 同时 4 个上传会话、聚合暂存预留 32 MiB；宿主所有上传聚合预留 128 MiB；每安装私有空间总量 64 MiB、目录枚举至多 1024 项。上传会话 60 秒空闲过期。限额属于同一存储服务维护的权威状态，预留按声明 total_bytes 计算，关闭或实际任务收尾后才释放；失败路径不能提前放开同一目标。

已完成文件、正在上传和崩溃遗留暂存均计入磁盘空间检查；外部异常增大文件或添加非普通对象后明确拒绝新写入。目录枚举、配额检查也要有项数/时间限制，不能无界扫描。配额失败保留已有数据，不自动删除旧导出或最旧文件。

正常 abort、过期、写入失败和停止只通过已确认的句柄/物理身份清理本次暂存，不能对客户端路径执行递归删除。删除失败保留有界资源记录和 pending，后续监控只重试清理，不重做发布。

宿主崩溃后不恢复 write_ref、不重放 commit，也不按通配符清扫私有目录。本轮旧暂存可保留并计入配额；超限明确返回存储已满，说明需由可信管理流程处理残留。首次目录准备失败不自动接纳无法确认来源的中间产物。是否加入跨重启暂存恢复或清理界面属于 B/后续方案，不能用忽略残留来绕过配额。

## 8. 实施落点和顺序

| 步骤 | 文件/当前调用方 | 交付和门槛 |
| --- | --- | --- |
| 1. 原生边界验证 | 新增 `plugins/runtime/file_handles.py`、`tests/test_plugin_file_handles.py` | 真正的相对句柄创建、读取、写入、同卷不覆盖发布与清理；危险竞争不产生越界副作用 |
| 2. 私有存储与输入 | 新增 `plugins/runtime/file_storage.py`、`file_api.py`；扩展 `runtime/models.py` | 单一存储服务、严格 FileRef/DTO、空间和会话限额，不扩大其他服务文件能力 |
| 3. 授权与发现 | `grant_models.py`、`grant_store.py`、Broker resources.list 分派 | fs 能力只表示当前安装私有空间；非空自报资源拒绝；epoch/根变化使引用失效 |
| 4. 真实提交与回执 | `services/operations.py`、`operation_adapters.py`、`broker.py`；`test_plugin_host_files.py`、任务用例 | commit 产生真实 TaskReceipt；执行/结果两种 lease、受信清理 effect、幂等、取消和保存失败验证 |
| 5. 运行收尾 | `worker_service.py` 及 Broker close/wait_idle、监控 | 上传资源和任务分别等实际结束；无孤立暂存、无误报 cleanup completed |
| 6. 用户审阅与示例 | 前端 `src/i18n/locales/*.json`、必要的权限说明/测试；新增私有文件快照示例 | 六语清晰表达读取/仅创建范围；示例执行真实写入、查询和读取，无生产测试后门 |
| 7. 文档/冻结 | 总体 DTO/API 表、包格式说明、当前方案、产物 CI 和夹具 | 同步 32 KiB、8 MiB、modified_ms、异步 commit 和不覆盖子集；无旧别名或迁移 |

不改 Core/Florolding 源码。前端变化需要先子仓库检查、提交与本地 main 集成，再更新主仓库指针；不能让主仓库指针意外包含其他工作。

## 9. 验收标准

| 场景 | 必须结果 |
| --- | --- |
| 未授权、只读、只写、必要/可选拒绝、会话许可、到期 | 按实际能力执行；写不隐含读；不能借发现接口扩展范围 |
| 插件 A/B、A 新 run、新内容、卸载重装 | 根/上传/任务引用严格隔离；旧数据不按显示名称自动继承 |
| 绝对/父级/UNC/ADS/设备名、大小写冲突、链接/重解析/硬链接 | 严格拒绝或冲突；无宿主真实路径及其他安装数据输出 |
| 根、父级、暂存或目标在检查后被替换 | 无越界创建/写入/清理；外部目标内容不被覆盖；不能以事后拒绝掩盖已发生的副作用 |
| 32 KiB 块、8 MiB 完整文件、零长度文件、非法 Base64/偏移/超量 | 正确字节与摘要；超限不追加数据；所有消息满足当前 JSON 限制 |
| modified_ms、etag 与同大小/同 mtime 内容变化 | 数字可精确传输，expected_etag 冲突明确，不返回混合数据 |
| 同键并发重放、同键不同块、回执耗尽 | 每个块/任务至多一次；无回执淘汰重放；即便限流也能 abort 自身上传 |
| T 先、接受先、取消先、发布后关闭/失败 | 按 P08 确定动作次数与真实 effect，不能把已发布文件标为取消或删除 |
| commit 回执超时或原权限变化 | 可查询安全的任务引用及状态；结果仍检查原读 lease；不重做发布 |
| 资源移交失败、保存摘要失败、清理失败、Worker 崩溃 | 明确资源所有者；实际未结束则 pending，已发布文件保留，不凭终态推断 future 完成 |
| 并发上传、同目标竞争、空闲过期、配额和残留 | 限额有界且失败保留数据；只清理身份吻合的当前暂存，不自动扫描/删除旧用户文件 |
| 当前设置、实例只读及用户长任务 | 原行为回归通过，文件撤销不能取消用户任务或其他插件任务 |

以 Event/barrier 和真实句柄控制竞争，缺陷用例先证明修复前失败。对原生根/重解析边界须用真实 Windows 用例，纯 mock 不作为成立证据；没有通过就不接线写入。

实施后执行 [AGENTS.md 第 6 节](../AGENTS.md#6-测试与验收) 的主仓库 Ruff、格式、全仓检查和完整 pytest，以及适用 Core 集成门禁。前端完整 pnpm check、随后 pnpm build；权限说明按 Classic/Folia、亮暗主题、默认/最小/放大窗口进行真实主窗口验收，不用 jsdom 代替布局。

PyInstaller 和 Nuitka 以最终代码重建，正式主窗口审阅 read/write 后，真实 Worker 用生产文件方法导出、查询任务并读回同样字节；覆盖只读/只写结果过滤、禁用中上传、提交中撤销、目标竞争和再次启用的旧引用拒绝。源码、平台分支、真实产物、主窗口和未执行的 POSIX 实机/远端 CI 分别记录。

按 [AGENTS.md 第 7 节](../AGENTS.md#7-git-提交与本地交付) 完成功能和独立文档提交，安全集成前端与主仓库本地 main，保留无关修改。远端推送、现有远端分叉处理和发布不在授权范围。

## 10. 待确认的实施边界

推荐确认 A：**只读私有文件 + 新文件事务发布**，采用 32 KiB 分块、8 MiB 单文件、modified_ms 和异步 commit，主动释放不受回执容量阻挡；写入任务执行和结果读取分开授权。首次明确限制为私有目录中的平铺 ASCII 文件名。

覆盖已有文件、删除、子目录/Unicode 文件名、共享目录、实例资料写入及跨重启暂存清理另行审阅。原生创建/发布边界必须先证明，再接入正式 API。按根规范第 0 节，本轮只交付调研与方案，功能修改等待用户确认本文件。
