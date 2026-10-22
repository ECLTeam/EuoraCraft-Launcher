# 插件私有空间下载任务实施方案

日期：2026-10-21（Asia/Shanghai）。状态：用户以“开始实施”确认方案，功能与第12节验收已完成。

调研基线为主仓库及原工作区本地 main `87db961`，前端 `320cc0d`。本方案接续已验收的 [P10 A 受控 GET/HEAD](plugin-network-read-implementation-plan.md)，采用 [P09 A 私有文件](plugin-private-files-implementation-plan.md) 与 [P08 自身任务控制](plugin-operation-ownership-implementation-plan.md)。方案阶段“开始下一步”用于形成下载切片方案；其后用户以“开始实施”明确确认功能实施。

## 1. 路线与验收目标

| 路线 | 改动及取舍 | 本轮决定 |
| --- | --- | --- |
| A：私有空间单文件下载 | 复用受控网络传输和原生文件事务；至多 8 MiB，只新建，无自动重试 | 已确认实施 |
| B：复用桌面/Core 下载器 | 须先为每次连接、重定向、重试及写入增加授权钩子，影响现有下载业务及子模块 | 延后单独审阅 |
| C：先补文件覆盖与删除 | 须确定条件替换、冲突和恢复协议，不能直接沿用不覆盖发布 | 延后单独审阅 |

验收目标：独立 Worker 向已批准公网站点提交一个下载，立即取得自身任务引用，经 `operations.get/list/cancel` 观察真实进度、结果与取消。成功时私有目录只有完整的新文件；失败或提交接受前取消不产生目标文件，暂存、实际网络工作和审计未释放时继续报告 cleanup pending。

本切片不支持覆盖、续传、Range、多段下载、自动重试、后台任务恢复、文件名推断、解压、实例目录或任意用户路径。HTTP 请求固定 GET，无请求体或自定义请求头；沿用现有精确站点授权。Worker 运行和私有文件仍限 Windows x64 本地 NTFS；不扩大平台支持。

## 2. 当前实现与必须解决的缺口

| 已核对模块 | 当前行为 | 本轮改动理由 |
| --- | --- | --- |
| `runtime/network_api.py`、`network_transport.py` | 固定数字 IP、原域名 TLS、逐跳验证；查询完整实体 32 KiB、总时长 30 秒、每跳原始收包 256 KiB | 提供共用流式执行入口，下载使用独立预算，保留查询限制 |
| `runtime/file_storage.py`、`file_api.py`、`file_handles.py` | `FileUpload.total_bytes` 同时充当声明大小与配额预留；原生暂存、不覆盖发布、失败清理 | 下载允许未知长度，须将预留上限与最终实际大小分开；复用目录/文件句柄边界 |
| `grant_store.py`、`runtime/operation_adapters.py` | 一个任务保存一个执行 lease；结果已有独立读取 lease | 下载须原子核验原始网络和写入两份 lease，不能先后检查后无条件提交 |
| `services/operations.py` | 唯一任务执行器；取消 Event、原子提交接受、真实 future/清理/审计收敛 | 复用任务状态，增加下载中文名称，不新增下载线程池或独立状态机 |
| `runtime/broker.py` | 变更按 mutation_key 去重，文件提交的控制回执独立于敏感结果 | 新下载入口必须接入同一回执生命周期，不能超时后重放 |
| `services/custom_downloads.py` | 使用桌面代理/重试与普通目标路径发布，未提供本切片所需的逐连接授权和原生相对路径边界 | 本轮不包装或修改这条链路 |

新增 `RuntimeDownloads` 仅负责把上述三个现有领域接到一个真实下载调用方，维护任务对应的网络/暂存清理引用；它不拥有执行器、权限记录或可独立修改的任务状态。共用网络和文件入口有查询/下载、上传/下载两个当前调用方，抽取只覆盖这些实际复用。

## 3. 请求、回执与结果契约

```python
receipt = host.call(
    "downloads.submit",
    {
        "url": "https://downloads.example.com/data.bin",
        "target": {"root_ref": root_ref, "relative_path": "data.bin"},
        "overwrite": False,
        "expected_sha256": expected_sha256,
    },
    mutation_key=download_key,
)
# receipt == {"operation_ref": "...", "state": "queued"}
```

`url` 使用 P10 A 的严格规范和 4096 字节上限。`target` 是本 run 的私有根 `FileRef`，名称非空，平铺 ASCII 名称及保留名拒绝规则沿用 P09 A。`overwrite` 必须是严格布尔 `false`，拒绝 `true`、缺省及伪布尔，不默默降级。`expected_sha256` 可省略，提供时必须为 64 位小写十六进制；显式 null、额外字段、headers、body、method、路径或主体替代字段均拒绝。

提交 RPC 最多 5 秒，仅验证、准备暂存和接纳任务，不等待 DNS 或 HTTP。任务总截止为开始处理首次提交时的 monotonic 时间加 1800 秒，包含排队、跳转、收包和提交前校验；网络各阶段最多 5 秒，并取总截止剩余值。排队期间不占网络/DNS名额；执行开始即检查总截止，超时不再开始网络或发布。截止不承诺磁盘、系统调用和清理已在该时刻实际结束。

`mutation_key` 使用现有有界回执表：同一 run、同一键和相同规范请求返回同一任务；不同请求冲突。已接纳回执在权限变更后仍可查询任务控制引用，不据此恢复执行权限。等待超时使用既有 `PLUGIN_OUTCOME_PENDING` 和 `mutations.get`；不同键是新提交，仍受目标占用和配额约束。SDK 不增加自动重发。

任务种类为 `plugin_download`。查询结果保持现有任务 DTO，不返回桌面内部任务标识、URL、响应正文、服务器错误体或真实路径。成功结果沿用 `FileView`：`file_ref/name/kind/size/modified_ms/etag`，以发布前捕获的文件快照投影。提交时可选捕获独立原始 `filesystem.read` lease；没有读权限，或该原始 lease 后来失效，`result` 为 null，已提交状态仍保持 completed。后来重新允许读取不使旧结果 lease 复活，可另用当前文件 API 读取。

已知有效 Content-Length 时按接收比例上报至 90%，未知长度时接收阶段保持 0%，完整收包后 90%、校验后至多 95%、发布成功由任务服务置为 100%。复用既有节流；任务阶段字段仍为 queued/executing/committing/terminal，不新增伪进度或任务事件订阅。

## 4. 授权、锁序与任务接纳

提交时捕获原始 `network.read` lease（当前已获准的精确站点集合）和 `filesystem.write` lease，两份必须属于相同安装、内容、run 和授权代次。不要求 filesystem.read，不添加 downloads 权限或网络写入权限。首站及每跳必须在捕获的站点集合内，不能换取新 lease 继续旧下载。

在 `PluginGrantSession` 增加供当前双权限调用方使用的 `check_all`/`accept_all`：先在授权锁外刷新内容和磁盘状态，再于一次短授权锁内检查全部原 lease；`accept_all` 最后执行纯内存登记。现有单 lease 调用继续有效，不增加旧契约适配。禁止在一个 accept 回调中嵌套另一个 accept，因为后者可能在已持授权锁时执行磁盘检查。

`RuntimeOperations.submit` 增加显式的附加执行 lease 元组，文件任务仍只传原写 lease。下载的接纳、执行检查点、权限刷新和最终提交接受均检查两份执行 lease；读取结果仍只用独立读 lease。主能力元数据可保留 filesystem.write，附加 lease 只在当前 run 内存保存，不能把网络 URL、目标或完整请求写入任务审计。所有执行授权的权威仍是 GrantSession。

锁序保持授权锁 → 运行引用锁 → 任务/配额登记锁。目录及暂存 I/O 遵守现有安装目录 → 暂存对象顺序；不得在授权、任务、网络服务全局锁内执行 DNS、连接、读写、哈希、关闭或审计落盘。网络取消检查在全局锁外调用任务检查点；有限 I/O 的接受点只检查原始双 lease、取消 Event 和截止并登记，避免从授权锁回调进入磁盘。

提交顺序为：严格校验 → 双 lease/首站授权/目标验证 → 接受必要的私有根初始化 → 预留目标与大小 → 独占创建原生暂存并先登记句柄所有者 → 同一双 lease 接受点提交任务并转移资源归属 → 返回回执。任务接受点须在短登记回调中再次核验提交 RPC 的 5 秒截止，超过截止不得新建任务；已经接纳后的等待超时不取消或重放任务。准备阶段失败只清理自己的暂存；若清理失败，资源继续归属本 run，绝不通过抛出异常遗失引用。任务接纳失败同样走该清理路径，不能留下外部无法 abort 的活动上传。

## 5. 流式网络与限额

抽取 P10 A 的共同逐跳执行函数，由查询收集有界实体、下载将实体流交给文件写入方。每跳重新验证 origin、DNS 全部结果和实际 peer；原域名 TLS/SNI/Host、禁止 HTTPS 降级、最多 5 次跳转、循环拒绝、无 Cookie/代理/环境证书/连接复用/HTTP2/重试均保持。下载固定宿主构造 Host、Accept-Encoding: identity、Connection: close，不加入 Range、账户或认证头。

下载仅最终 HTTP 200 进入文件收包。允许 0 字节的 200 文件；204、206、304、非重定向的其他状态及错误响应均失败，不把错误体或部分实体发布为文件。Content-Disposition 不参与名称选择。Content-Length 缺省时按 EOF 判定；存在时严格校验非负十进制、无歧义且不超过上限，最终实体必须与声明一致。拒绝非 identity Content-Encoding，不解压；传输分块解码由现有 HTTP/1 实现完成，截断、协议歧义按失败处理。

| 预算 | network.request | downloads.submit |
| --- | --- | --- |
| 完整实体 | 32 KiB | 8 MiB，即 8388608 字节 |
| 每跳原始 HTTP 收包，含头和分块开销 | 256 KiB | 9 MiB，即 9437184 字节 |
| 每次底层 socket 读取 | 最多 8192 字节 | 最多 8192 字节 |
| 暂存单次写块 | 不适用 | 最多 32768 字节，不聚合全文件到内存 |
| 总截止 | 原 RPC 与 30 秒取较早值 | 首次提交起 1800 秒 |
| DNS/连接/TLS/单次读写 | 最多 5 秒及剩余总截止 | 相同 |

将 transport 的硬编码原始收包上限改为宿主构造的显式预算；查询默认值仍为 256 KiB，Worker 无法自定义预算。下载的 9 MiB 同样约束重定向/信息响应和分块元数据，原始预算耗尽即失败，可能拒绝分块开销过高但实体未满 8 MiB 的响应；不无限放宽协议开销。每跳重建计数，全部跳转受同一总截止，最多 6 个实际响应。

网络请求名额仍为每 run 2 个、宿主 8 个；DNS 实际工作者最多 4 个、无解析队列。查询与下载共享这些名额，任务开始时名额不足明确失败，不添加第二层等待队列或内部重试。DNS 等待取消不结束实际系统解析，迟到结果只释放资源，不能继续连接。

网络错误在进入 OperationManager 异常处理前转换为固定消息与稳定码，丢弃可能包含 URL/头的原始异常文本和异常链。保留 P10 A 的传输日志上下文过滤及 DNS 上下文传播。用户取消产生 OPERATION_CANCELLED；撤销和截止保留自身稳定错误，不把任意关闭异常都伪装为取消，也不让取消掩盖原有 I/O 失败。

## 6. 暂存、配额、校验与发布

将内存中的 `FileUpload` 调整为上传和下载共用的 `StagedFile`，明确 `reserved_bytes`、可空的 `expected_bytes`、`received_bytes`，同步全部调用方和测试，不保留旧类型别名或旧字段双写。普通上传的预留与期望大小均来自已批准的 total_bytes；下载提交时固定预留 8 MiB，响应长度仅用于实际接收校验，不缩小预留。

沿用现有每 run 4 个暂存、32 MiB 预留，宿主 128 MiB 预留，安装私有空间 64 MiB、目录 1024 项上限。下载和上传共用目标占用、暂存计数及配额；排队的下载也占暂存和预留，直到其实际资源释放。固定预留意味着私有空间可用不足 8 MiB 时，即使真实小文件可以容纳也拒绝本次下载，这是本轮明确的保守取舍。已有文件和崩溃残留继续计入空间，不增加自动清扫或恢复流程。

抽取 `RuntimeFiles` 中准备暂存、校验和发布的受信领域入口供上传与下载使用，下载适配器不访问真实 Path 或复制 Windows 原生逻辑。网络等待期间不持安装目录锁；每次有限写块复核原始双权限、取消、截止和大小边界，只持对应暂存 I/O 锁完成写入，持续维护实际 offset 和 SHA-256。

收到完整实体后先结束 HTTP 响应/连接并确认本调用实际网络资源已关闭；关闭失败不进入发布，保留清理归属。随后重新打开并验证同一私有根身份、暂存身份、实际长度、当前空间及目标占用，读取暂存重算摘要与流式摘要核对；如有 expected_sha256 再核对期望值。该文件校验采用任务截止与当前时间加 30 秒的较早值，不能重新获得超过任务总截止的发布预算。没有期望摘要时也执行完整性计算，但不宣称因此确认远端文件可信。

flush、最终文件信息与摘要在提交接受前取得。最终 `context.finalize` 使用原始双 lease 排序取消/撤销与提交：若取消、撤销或截止先赢得接受点，不发布；若提交接受先发生，则完成一次短时原生不覆盖发布，之后撤销不改写已提交事实。截止检查也须包含在最终准入中，不能只在哈希之前检查。

发布采用现有目录句柄相对的原生不覆盖操作；目标在途中被外部创建时返回 PLUGIN_FILE_EXISTS 并保留原目标。源/根替换、重解析、ADS、硬链接和目录不支持均沿用当前拒绝策略。发布后只使用捕获的快照，不重新打开目标制造“已发布却读取失败”的假失败。

## 7. 取消与真实清理

`RuntimeDownloads` 的记录只关联任务取消 Event、网络调用、暂存和截止，不复制任务 state/effect。复用 WorkerService 现有监管循环，在取消/权限刷新/停止时关闭对应活动流；不新增每下载一个监管线程。网络 guard 在 DNS 等待、连接/TLS 返回、每次读写前后同时检查任务取消和原始双权限。已开始的有限系统调用仍按真实返回收敛。

任务 cleanup 回调分别尝试网络和暂存清理，再汇总结果，不因一方 pending 而跳过另一方清理。队列取消和审计保存失败也执行同一清理。文件句柄确实关闭/暂存确实删除后才归还文件名额；网络名额须实际调用结束、DNS future 完成、流均关闭才归还。任务 owner_idle 还要求实际 future、全部清理和审计完成。资源释放按各自真实状态进行，不能把任务 terminal 或 Worker 已停止当成释放证明。

初始 effect 为 staged，准备尚未接纳任务时由本 run 文件资源记录承担归属。成功发布为 committed；未发布且确认暂存消失才收敛为 none；无法确定的发布失败保持 unknown。取消后不删除已发布目标，不回放任务或自动恢复旧 run 控制权。清理失败由现有监管周期重试清理，不再次请求 HTTP、写文件或发布。停止 pending 时继续阻止同安装重新启用。

## 8. 错误与日志

| 情况 | 对外处理 |
| --- | --- |
| 请求字段、摘要格式、overwrite 或文件名无效 | PLUGIN_SCHEMA_INVALID |
| 原始授权失效或站点未批准 | 沿用 GrantSession / PLUGIN_RESOURCE_DENIED |
| DNS 含禁止地址、peer 不匹配、跳转拒绝 | 沿用 P10 A 的目标/重定向稳定码 |
| 单阶段连接/传输失败、协议响应损坏 | PLUGIN_NETWORK_FAILED |
| 最终状态非 200 | PLUGIN_DOWNLOAD_FAILED，不返回状态文本或错误体 |
| 实体或原始收包预算超限 | PLUGIN_RESPONSE_TOO_LARGE |
| 内容编码不支持 | PLUGIN_NETWORK_ENCODING_UNSUPPORTED |
| 摘要不符 | PLUGIN_HASH_MISMATCH |
| 文件已存在、目标占用、配额不足 | 沿用 PLUGIN_FILE_EXISTS / PLUGIN_FILE_BUSY / PLUGIN_STORAGE_QUOTA_EXCEEDED |
| 截止、主动取消 | PLUGIN_DEADLINE_EXCEEDED / OPERATION_CANCELLED |
| 文件身份、写入或审计失败 | 沿用 P09 A / P08 固定错误码，不用下载失败覆盖 |

业务日志使用“插件私有文件下载”中文操作名称，记录提交、执行、真实结果、耗时和采样百分比；不输出完整 URL、查询串、重定向地址、请求头、私有文件名/路径、任务编号、插件原始输入或异常原值。原有 OperationManager 记录真实任务结果，领域日志不重复打印同一堆栈。无新增正式前端 IPC，因此不创建新日志通道或改动 IpcLogPolicy 的命令目录。

## 9. 文件范围与实施顺序

1. `grant_store.py`、`runtime/operation_adapters.py`：多原始 lease 的检查/接受与任务生命周期；先补双权限、撤销竞争、结果 lease 的回归测试，保证原单权限任务语义。
2. `runtime/file_storage.py`、`file_api.py`：共用暂存模型，预留与期望大小分离，抽取两个真实调用方所需的文件事务入口；保留原文件 API 的严格 DTO、60 秒上传空闲期和任务所有权。下载交给任务后不套用上传 60 秒空闲清理。
3. `runtime/network_api.py`、`network_transport.py`：显式预算和共用逐跳流式执行，接入下载双 lease/任务取消检查，维持 GET/HEAD 的全部预算与语义。
4. 新增 `runtime/download_api.py`：严格提交模型、资源准备、OperationManager 接纳、流式有限写入、校验发布和聚合清理。`runtime/models.py` 增加 DownloadSubmitRequest；不引入通用下载框架。
5. `runtime/broker.py`：downloads.submit 路由、去重、控制回执及监管/停止/idle 接线；`services/operations.py` 增加中文任务种类。WorkerService 继续注入现有网络/文件/任务服务，仅在实际需要处调整接线。
6. `tests/` 增加下载领域、真实 Worker、取消/停止、原生文件与打包回归；复用网络的可信本地 HTTP/TLS 夹具。新增 `examples/plugins/worker_download`，只下载测试配置明确给出的站点，不在钩子中自行联系公网。
7. 同步 `eclplugin-package-format.md`、总体设计相关状态与契约、示例和本方案验收记录；清查旧暂存类型和字段，不加历史持久化兼容、迁移或自动清理。

前端、Core、Florolding、桌面下载服务和依赖版本计划均不修改。如果必须变动它们或现有安全边界才能完成本切片，先给出具体原因并修订方案，不以旧下载链路回退绕过验收。

## 10. 测试与开放门槛

| 层次 | 必须覆盖的可观察行为 |
| --- | --- |
| 协议与准入 | 非法 DTO、严格 false、跨安装/run 引用、未获一项权限、原始双 lease 同代次、接纳与撤销排序、同 mutation_key 并发只创建一次任务及暂存、回执丢失后查询 |
| 流式完整性 | 0 字节、固定/未知长度、正常 chunked、超过 32 KiB 的下载成功而查询仍拒绝、精确 8 MiB 成功及多 1 字节拒绝；错误/歧义长度、截断、编码、巨大分块开销及信息响应洪泛均无目标文件 |
| 网络权限 | 公网 IPv4/IPv6 策略、混合禁止 DNS、数字拨号/peer/TLS/SNI、跨站未授权/降级/循环/超 5 跳、跳转重新解析、无代理/证书环境/账户/重试；每跳和总预算均不可借跳转重置 |
| 文件事务 | 可选摘要正确/错误、写入/flush/哈希故障、根/源身份变化、junction/ADS/硬链接、目标并发创建、上传与下载竞争同目标、配额共享；未知长度的完整预留不能超售 |
| 取消和清理 | 排队/解析/连接/TLS/持续小块接收/文件写入/哈希/提交接受点前后取消与撤销；永久阻塞 DNS、迟到 connect/TLS、关闭/删除失败保持 pending，重试后释放；取消一 run 不影响另一 run |
| 任务与审计 | 提交前审计失败不发网络或发布；提交后审计失败保留 committed；只有写权限仍可完成但结果 null，后来读取许可不能复活原结果；终态不等于 owner_idle，旧 run 不能恢复控制 |
| 回归与发行物 | 当前文件/任务/GET/HEAD/实例/设置/命令及桌面用户下载回归，真实 Worker 通过；PyInstaller 和 Nuitka 最终产物分别验证下载、取消、撤销、停止 pending 与原生私有文件发布 |

按 [AGENTS.md 第6节](../../AGENTS.md#6-测试与验收) 执行全部适用检查：主仓库 `ruff check ECL tests`、`ruff format --check ECL tests`、`ruff check . --format github`、完整 `python -m pytest --tb=short -q`，以及 CI 要求的 Core Ruff 与集成测试。平台分支覆盖 Windows 与 POSIX 的策略/拒绝测试，真实 Worker/原生 NTFS 发布使用 Windows x64；没有 POSIX 实机证据时明确标注。

下载接线会改变启动器的运行流程，因此功能实施的验收包含先 pnpm check、再 pnpm build，然后实际启动启动器验收原生权限审阅、提交/查询/取消和停止后的清理状态，分别验证两种冻结产物，不能以纯 Worker 单测代替。若实施中实际修改前端界面，再补第6节规定的主题、窗口尺寸和布局验收。方案编写阶段未提前执行这些功能实施门禁；当前实施结果见第12节。

任一原始双 lease、逐跳传输、原生发布、资源归属或取消竞争缺少实际控制点，downloads.submit 保持关闭，不以方案、模拟成功或历史 P10 A 测试结果宣称完成。实施完成后记录实际命令、源码/真实 Windows/冻结/未运行远端 CI 的区别，再提交功能与验收文档。

## 11. 本轮方案验证与交付

本节只记录方案阶段验证，不代表下载已经实现。

- 方案阶段 Windows x64 基线：文件、原生文件句柄、文件 Worker、任务归属/Worker、网络站点/API/日志/Worker/退出测试共 261 passed、4 skipped，耗时 48.73 秒。跳过项保留 pytest 的当前平台与条件要求，不能据此宣称 POSIX 实机或下载已经验收。
- 本轮改动只含方案文档及总体设计的现行状态/计划入口；提交前核验 Markdown 本地链接、章节和 git diff --check，不执行无关构建或改动生产代码。

功能完成后按 [AGENTS.md 第7节](../../AGENTS.md#7-git-提交与本地交付) 提交并安全集成原工作区本地 main，保护无关修改；若没有子模块改动则不改其指针。本方案文档同样独立提交并同步本地。远端 main 的既有分歧保留，不自动合并远端、推送或发布。


## 12. 实施与验收记录

用户确认实施后按本方案开放 downloads.submit。实际实现复用网络服务的原始双lease与任务取消 Event，资源引用由任务 cleanup 回调持有；Broker原有 network/files/operations 空闲判定即可聚合真实状态，无需再建立下载注册表或独立生命周期状态。新增 refresh_pending_work 供权限变更与既有监管循环共同使用，不新增监管线程。

本轮不修改前端、Core、Florolding、桌面下载服务及依赖版本；不加入兼容、迁移、覆盖、恢复和重试。

| 检查 | 本轮实际结果 |
| --- | --- |
| 主仓库质量门禁 | ruff check ECL tests、ruff format --check ECL tests、全仓 GitHub 格式检查全部通过；全仓使用当前 Ruff 支持的 --output-format github，规范文字中的旧 --format 选项已被工具拒绝 |
| 完整源码后端 | PYTHONUTF8=1 的最终 python -m pytest --tb=short -q：1918 passed、20 skipped，186.85秒，无警告；含57项下载领域测试及真实源码Worker |
| Core | 子仓库 ruff check . 通过，主仓库 python -m pytest ECL/game/tests --tb=short -q：16 passed |
| 前端 | pnpm check 通过：915 passed、1 todo；随后 pnpm build 成功，未修改前端代码或gitlink |
| PyInstaller | 最终生产代码构建成功；指定实际产物后引导/文件/网络/任务/下载Worker组合16 passed；最终示例明确mutation_key后的下载Worker复验2 passed |
| Nuitka | 最终生产代码onefile构建成功；同一Worker组合16 passed，最终示例下载Worker复验2 passed |
| 两种真实主窗口 | 各运行test_packaged_plugin_directories.py，分别1 passed；真实WebView2主窗口保存站点许可，经正式原生IPC调用独立Worker下载Microsoft公开连接测试文本，校验摘要、相同键不重复创建、读回文件、取消并确认无目标文件；依赖/实例/快照/设置及撤销停止回归同时通过 |
| 作者交付 | worker_download经正式packaging/build-eclplugin.py制作成功；正式协议、示例与总体设计本地链接、章节和git diff --check通过 |

下载领域测试使用受信宿主本地HTTP/TLS夹具覆盖精确8 MiB与多1字节、未知长度、分块实体、9 MiB信息响应开销、非200/编码/截断、摘要、双lease竞争、排队配额、提交接受前后取消、迟到DNS、实际关闭/删除失败及审计故障。系统调用未结束时如实保留pending，清理重试未重复HTTP或发布。冻结Worker的确定性故障由当前受信测试宿主驱动；真实冻结宿主的下载、去重、读取、取消和权限管理另由主窗口用例执行，二者不混称。

首轮完整源码测试通过但有一个旧作者CLI用例的GBK子进程解码警告，最终设置PYTHONUTF8=1完整复验后无警告。Nuitka首次因自身版本检查缓存原子替换返回WinError17而中止，按已安装工具公开的--update-check=never重建成功；没有修改工具、依赖或项目逻辑绕过测试。构建保留既有体积/依赖提示，未以此扩大改动范围。

本轮真实平台为Windows x64/NTFS/WebView2；另执行现有POSIX策略及拒绝分支，未做POSIX实机Worker或远端CI/发布。检查与发行物日志保存在被Git忽略的build/worker-manager/p10b-*，可安装示例包为build/worker-manager/worker_download.eclplugin。

本地交付：功能提交0fb5cd7，协议与验收文档单独提交docs。提交前确认原工作区本地main为ac90c88且干净；提交后仅快进集成本地main。frontend仍为320cc0d、Core为fce6b10、Florolding为80d037e，未改子模块指针；远端原有分歧保留，不推送或发布。
