# 插件 Java 查询与校验实施方案

日期：2026-10-23（Asia/Shanghai）。状态：用户已选择方案 A 的分阶段路线，本文待审阅；尚未授权本文功能实施。

本轮仅制定第一阶段方案。用户本轮的“开始”承接“确认方向后编写详细实施文档”，不作为 Java 安装、游戏启动或整个 P11 的实施授权。本文审阅通过并明确开始实施后，才修改生产代码、测试、前端和子模块。

## 1. 目标与选定路线

目标：第三方 Worker 能读取用户明确选择的 Java 摘要，校验明确获准的 Java 是否可用及满足最低主版本，并查询宿主固定来源的可安装包；结果不包含本机路径、账户、启动参数或任意 URL。

| 路线 | 范围与取舍 | 本轮决定 |
| --- | --- | --- |
| A：分阶段开放 Java | 第一阶段查询与校验，第二阶段安装计划、安装任务及来源隔离；每阶段独立验收 | 用户选择；本文只细化第一阶段 |
| B：一次完成查询与安装 | 一次连通业务，但需同时解决共享目标、任务归属、取消、提交及登记回滚 | 不采用 |
| C：先做声明式界面 | 插件操作入口更直观，但仍缺 Java 等宿主业务能力 | 后续独立评审 |

第一阶段包含 `java.inventory`、`java.validate`、`java.catalog`，以及必要的资源发现、可信授权界面、运行引用、SDK 调用示例与错误契约。它覆盖总体设计 P07 的 Java 查询部分及 P11 的前置校验，**不代表 P07 或 P11 整体完成**。

第二阶段另行制定安装方案。当前 `JavaManager._submit_once()` 按动作和目标复用任务，未区分插件来源；本阶段不将现有安装回执交给插件，不开放安装、删除、启停、绑定、清理、游戏安装或启动。

## 2. 调研基线与已核实问题

主仓库基线 `67a5843`；记录的前端指针 `82c97eaef7aea681d49752384234ef4027840c11`，Core 指针 `e6b5c0f9fc1eb868e6a8dc6fd7654101f9504334`，Florolding 指针 `80d037e73923e93cf961119932a117d3a04c297b`。本工作树子模块尚未初始化，前端和 Core 的相关源码通过原工作区只读检查，并确认其 HEAD 与上述指针一致。

| 当前代码 | 已核实情况 | 实施要求 |
| --- | --- | --- |
| `ECL/plugins/runtime/broker.py` | 已开放实例、私有文件、网络、下载和资料修改；没有 Java 能力或分派 | 按明确方法接线，通过门禁后才加入能力白名单 |
| `ECL/java/manager.py` 的 `inventory()` | 扫描系统、探测执行文件，可能写扫描缓存和运行时登记，并拼装引用与使用信息 | 不直接用作插件只读列表，不先扫描所有 Java 再过滤授权 |
| `validate_selection()` | 持有登记锁进行强制探测；可能再经 `_refresh_record()` 探测，未接插件截止及撤销 | 抽取有界的单目标校验，不在全局登记锁下运行子进程 |
| `ECL/java/models.py` | `runtime_id` 是规范 Home 路径的摘要，原始模型含路径和安装文件；登记由 JavaRegistry 唯一维护 | 不用路径摘要作为插件凭证，不透传原始模型；运行时业务数据仍由 Java 领域维护 |
| Core `Utils/JavaScanner.py` | 公开 `probe()` 不扫描或写缓存，但底层 `subprocess.run(capture_output=True)` 无输出大小上限，固定十秒，继承环境，失败统一返回 None | 通过 Core 公开接口补齐受控探测的预算、输出、环境及实际进程收尾，复用现有解析 |
| `ECL/java/packages.py` | JavaCatalog 已有固定来源、字段校验及缓存；请求单次计时，releases/packages 没有共享调用截止 | 复用目录规则，补齐跨请求总预算及响应投影；不调用通用 `network.request` 绕过来源约束 |
| `resource_catalog.py`、`runtime/resources.py` | 现有资源模型只表达实例目录和实例 | Java 使用独立的领域资源身份，不把它伪装成实例目录或泛化整个注册框架 |
| `grant_store.py`、`worker_service.py` | 动态资源范围和存活更新目前只处理实例读写 | 为 Java 增加精确动态范围，保留现有其他能力的声明子集校验 |
| 可信权限 IPC 与前端 | 候选类型、请求校验和选项只支持实例；保存只有实例目录 revision | 增加 Java 候选及独立版本检查，混合授权不能用两种目录 revision 的最大值代替校验 |

本轮没有运行功能测试、构建或桌面验收。既有计划中的测试数量只属于其当时记录，不作为本阶段基线通过证据。

## 3. 数据归属与授权范围

### 3.1 单一 Java 数据来源

新增 `ECL/java/queries.py` 中职责明确的 `JavaQueries`，持有应用已有 JavaManager/JavaCatalog 的窄接口，服务插件授权候选和运行查询。JavaRegistry 继续是登记状态的唯一维护者，不新建第二份可修改运行时清单或新的 JavaManager。

新增登记快照读取入口：只读取经过校验的登记记录，不调用 `inventory()`、系统扫描、`_references()` 或进程使用枚举，不执行 Java、不更新有效状态、不写回登记。缺少清单是正常首次状态，损坏、超限或读取失败明确报错并保留原文件。可信授权候选也只来自已登记条目；首次没有候选时，引导用户到现有 Java 管理页扫描或登记，再返回刷新。

摘要里的 `validation_status` 表示登记中最后记录的状态，不代表本次已运行验证。`java.validate` 才返回本次探测结果，且不隐式写回登记、不修改启用状态、配置绑定或游戏使用租约。

### 3.2 精确选择与身份失效

- 新增 `java.read`、`java.use`、`java.catalog.read` 三项独立能力；清单只声明能力，不接受本机 Java 资源 ID、路径或插件自定目录源。
- `java.read` 和 `java.use` 均由可信主窗口逐个选择已有 Java；默认不选，不提供“以后新增的全部 Java”授权。两项选择互不自动继承。校验会执行本机 Java，界面必须明确说明。
- `java.validate` 的结果包含摘要，因此本阶段明确要求同一资源同时持有原始 `java.read` 和 `java.use` 租约。这是对总体设计原先仅列 `java.use` 的细化，实施时同步所有契约，不保留另一种调用规则。
- `java.catalog.read` 是宿主固定官方元数据服务的有限网络许可，无动态本机资源选择；它不依赖也不授予通用 `network.read`，不开放任意 URL、代理、请求头或凭据输入。
- 新增 `ECL/plugins/java_resources.py` 的 `PluginJavaResourceCatalog`，仅保存授权所需的随机持久 ID、对应 Java 登记身份和对象特征、存活状态及 revision。当前调用方只有可信权限审阅与 RuntimeJava，不保存版本、启用状态等第二份业务权威数据。
- 持久 ID 绑定规范 Home、实际探测执行文件及必要对象身份；处理 `javaw.exe` 实际使用同目录 `java.exe` 的关系。移除再登记、路径同名重建、执行对象或必要身份变化不自动继承许可。别名只在核实为同一实体时去重。
- 身份索引原子保存到宿主 `plugin_java_resources.json`，严格模型、重复 ID 检查、版本回退检查、8 MiB 和 4096 个资源上限；损坏保留并拒绝相关 Java 资源能力，不阻断不使用 Java 的插件。只退休失效身份，不自动清扫旧记录或用户 Java。
- 持久 ID 仅供宿主和可信界面；Worker 获得本安装/内容/run 所属随机 `runtime_ref`，不用路径摘要、原始 runtime_id 或持久 ID。引用不替代实时授权，跨安装、run、类型和失效代次拒绝。
- 对象身份核对用于阻止已观察到的替换和错误引用，不声称阻止同用户恶意修改整个 Java 安装、依赖库或所有原生加载行为，也不提供 OS 沙箱保证。

必要的资源能力没有有效选择时按现行机制禁止启用或停止 Worker；可选能力未获准则对应调用失败。资源变化只使涉及该资源的授权代次失效，不能由 Java 列表读取重新批准新身份。

## 4. Host API 与严格 DTO

以下为本阶段拟定契约；实施前仍未开放。请求拒绝额外字段、伪布尔、非法数值、裸路径、URL、宿主任务 ID 和客户端自报身份。未提供的可选字段使用下表默认值，显式 null 只在明确允许时接纳。

| 方法 | 请求 | 结果与授权 |
| --- | --- | --- |
| `resources.list` | `{capability:"java.read",cursor?:string,limit?:int}` | 当前获准资源的 `Page<{kind:"java_runtime",runtime_ref,label}>`；读取前后检查 java.read；不扩展为所有本机 Java |
| `java.inventory` | `{cursor?:string,limit?:int}` | `Page<JavaView>`；只投影 java.read 精确选择的已登记摘要，不执行扫描/探测 |
| `java.validate` | `{runtime_ref,required_major?:int}` | 本次新鲜 `JavaView`；同资源 java.read 与 java.use；required_major 为严格整数 1—99，含义为最低主版本，省略表示只校验可用性 |
| `java.catalog` | `{major_version?:int,runtime_kind?:"JRE"或"JDK",cursor?:string,limit?:int}` | `{recommended_major,items:JavaPackageView[],next_cursor}`；java.catalog.read；只查询当前宿主平台 |

`JavaView` 精确字段为 `{runtime_ref,vendor,major_version,full_version,runtime_kind,architecture,origin,is_enabled,validation_status}`，沿用现有枚举。版本/厂商字符串各至多 120 字符，architecture 至多 32 字符，异常超长领域输出明确失败；不截断身份字段。登记原有缺失/无效条目可显示最后已知信息，其状态不改写为成功。

`JavaPackageView` 为 `{package_ref,vendor,major_version,full_version,runtime_kind,platform,architecture,download_bytes,checksum}`；vendor 为当前固定发行源的 Temurin，full_version 来自已校验发布信息；checksum 为已校验 SHA-256。URL、文件名、安装根和原始来源响应不输出。package_ref 属于本 run 的短期包映射，本阶段没有接受它的安装入口。

目录默认 runtime_kind=JRE。省略 major_version 时先查询现有 releases，取 recommended_major，再只查该一个主版本，不展开全部主版本；显式主版本必须在 1—99，来源确实没有适用包时成功返回空列表。来源失败、响应畸形或推荐信息无效明确失败，不伪造包或回退到另一个主版本/JDK。recommended_major 始终来自同一次有效 releases 快照。

分页复用现有 `ResourcePages`：limit 默认 50、范围 1—100，业务响应至多 64 KiB，游标绑定 run、epoch、查询条件和资源/目录快照；16 个快照、120 秒到期。每个 run 最多 128 个 Java 资源引用和 256 个包引用，包引用不超过来源缓存有效期与 120 秒中的较短期限；容量不足明确失败，失效后需显式重新查询。

`permissions.list` 将 Java 读/用持久选择转换为本运行引用；验证只接受同时获准的同一目标。清空、撤销、内容变化、退出后不交付旧摘要、旧包或旧游标结果。

沿用通用 schema、越权、撤销、失效游标、限额及截止错误；新增明确 Java 错误 `PLUGIN_JAVA_QUERY_FAILED`、`PLUGIN_JAVA_INVALID`、`PLUGIN_JAVA_DISABLED`、`PLUGIN_JAVA_VERSION_MISMATCH`、`PLUGIN_JAVA_CATALOG_FAILED`。资源变化用现有资源不可用错误，网络/探测超时用现有截止错误；保留稳定原因，不返回底层路径、响应正文或 Java 原始输出。

## 5. 校验执行、网络预算与清理

### 5.1 单目标 Java 校验

1. 严格校验输入；在任何探测执行之前解析当前 run 引用并取得同一资源读/用租约。短锁内读取登记快照和启用状态，停用或未登记目标拒绝，不允许 Worker 提供可执行路径。
2. 在锁外核验已批准的 Home、bin、实际探测文件和必要身份；不使用其他安装目录或 PATH 搜索替代。发现对象变更则退休并要求重新审阅，不把同路径换文件当作新鲜结果。
3. 继续使用 Core 公开 `JavaScanner.probe()`，对其增加可选的调用截止、输出预算及受控环境输入，复用现有版本属性解析；当前真实宿主调用方继续使用其明确默认行为，不另建一套 Java 解析器，也不探测多种旧签名。受控调用能区分预算/收尾失败与普通无效 Java。
4. 子进程固定执行 `-XshowSettings:properties -version`，shell=False，关闭 stdin、Windows 隐藏窗口。复制并清理 Java 自动注入参数的环境变量（JAVA_TOOL_OPTIONS、_JAVA_OPTIONS、JDK_JAVA_OPTIONS、CLASSPATH），不修改宿主全局环境，不接受插件参数、环境或工作目录；探测不运行插件 JAR。
5. 校验总预算为 min(RPC剩余时间,30秒)，单进程至多10秒且取剩余值，stdout/stderr 合计至多64 KiB。读取达到上限即停止接收并终止本次探测，不在 capture_output 完整分配后才判断大小。超时、撤销或关闭均只操作本次创建的探测进程，并实际等待其结束和管道释放。
6. 同一 run 最多一个在途校验，宿主最多两个；满额立即限流，不合并到用户扫描/游戏的探测工作，不维护无界排队。实际工作继续占额度直到确认结束；不能把调用等待超时当成已释放额度。
7. 交付前复核原租约、启用状态和对象身份；即使探测成功，撤销或目标变化也不交付结果。并发安装/删除不能导致命中另一个对象；必要时使用 Java 领域现有生命周期租约阻止正常业务删除，finally 实际释放，不取得游戏启动权。
8. 主机迟到系统调用或进程未退出时，维持现行 cleanup pending，并阻止该 run 重启；不持有授权、资源目录或登记锁等待外部进程。正常返回也不写登记、扫描缓存或配置。

Core 的增强属于本文明确包含的子模块变更，仅修改公开探测所需边界及测试，遵循 Core 自身规范。若无法通过其公开接口兑现执行/收尾约束，保持 java.use 未实现并回到本方案修订，不直接调用 Core 私有方法或复制解析。

### 5.2 固定来源目录查询

复用 JavaCatalog 的字段校验和五分钟缓存，由应用已有客户端负责生命周期；不新建下载器、不关闭共享客户端、不复用共享 Cookie、认证头或宿主凭据。实施时核对应用客户端及重定向规则，保证插件查询只产生已定义的官方元数据请求，禁止插件改写目标；服务的有限网络行为不表示任意站点权限。

一次 java.catalog 总预算至多30秒，覆盖 releases、packages、排队和响应读取，逐请求/阶段取剩余预算且至多5秒。需要向 JavaCatalog 公共调用链贯穿同一绝对截止，不在每次请求时重新给满额预算。单次响应沿用2 MiB上限，最多解析256个包条目；超限明确失败。缓存有效可直接使用，失败不以过期数据冒充成功，不自动重试或更换来源。

每 run 一个在途目录查询，宿主至多两个，满额立即限流；额度跟踪实际网络操作与关闭，撤销/断线后不继续第二次请求，不交付迟到结果。网络/探测若使用后台线程，在 Broker wait_idle 及 Worker 清理中纳入真实归属，不把返回给等待方的截止错误当成线程已结束。日志只记录中文阶段、耗时与稳定错误，不记录私有路径、原始属性输出和响应正文。

## 6. 可信授权界面与混合资源版本

- 沿用仅原生主 WebView 可调用的 `plugin_permission_resources`，扩展能力为 java.read/java.use；Java 不接受 parent_ref 展开，不扫描实例目录。返回候选为严格 `kind:"java_runtime"` 分支，包含稳定候选 ID、简短厂商/版本/架构标签、可用/已选择状态，不返回路径。
- `PluginPermissionUpdate` 增加独立 `java_resource_revision`。原 resource_revision 继续只表达实例目录；同时保存实例与 Java 时分别检查两个已审阅版本，任一变化整次拒绝，不用 max 或单个目录的版本验证另一目录。仅撤销不强制读取失效目录。
- GrantStore 校验 Java 读/用选择必须属于对应宿主有效资源集合，禁止把实例 ID 用作 Java ID；java.catalog.read 不接受本机 resource_refs。清单、决定、运行准入、必要权限判断和失效通知同步修改，不放宽其他能力的子集规则。
- 在 `PluginPermissionsModal.vue` 增加 Java 读/用选择区，读取和校验单独说明。复用现有控件、Modal、请求作用域和保存执行锁；平铺 Java 列表不强行抽象实例树为通用资源框架。
- 切换插件、关闭弹窗和再次请求使旧响应失效；分别呈现加载、无登记、不可用、读取失败、候选变化及保存冲突。无登记时提供已有 Java 管理入口；插件自身不能触发全机扫描。
- 前端 `types/plugins.ts`、`pluginPermissionValidation.ts`、`pluginManagementApi.ts` 及相关测试同步严格判别联合和双版本字段，不以类型断言替代解析。所有新提示及可访问名称覆盖六种语言。
- 本轮优先扩展现有 IPC，更新相关中文操作说明与覆盖测试；若实际确需新增正式命令，必须同步 IpcLogPolicy、注册和类型，不增设绕过授权的通用命令。

## 7. 改动位置与实施顺序

| 位置 | 具体职责 |
| --- | --- |
| `ECL/java/queries.py`（新增）、manager/models/packages | 单一登记快照、受控校验、固定目录预算和白名单领域摘要 |
| `ECL/game/Utils/JavaScanner.py` 及其公开导出/测试 | 公开 probe 的预算、输出、环境与真实收尾；保留现有真实调用方 |
| `ECL/plugins/java_resources.py`（新增） | Java 授权身份索引、退休和目录 revision |
| `ECL/plugins/runtime/java_api.py`（新增）、models/broker | 当前运行引用、严格请求/结果、授权校验、分页及清理归属 |
| grant_models/grant_store、worker_service、manager/base、application | 动态精确授权、有效集合、唯一应用 Java 查询服务注入和关闭顺序 |
| `ECL/api/plugin_models.py`、`ECL/api/plugins.py` | 候选分支、独立 Java revision 及可信调用边界 |
| 前端插件权限组件、API、类型、六语及相关测试 | 精确选择、数据校验、并发/失效与实际界面反馈 |
| tests、Core tests、`examples/plugins/worker_java_query`（新增） | 主仓库集成、真实 Worker 示例和可复现边界回归 |
| 包格式协议、无头运行指南、总体设计和本文 | 同步现行能力、DTO修订、限制及独立验收记录 |

按以下顺序执行，前一步不能满足则不开放相关能力：

1. 初始化准确 gitlink 对应的子模块，读取各自规范；重核基线与无关改动，补充现状测试。
2. 完成领域摘要、Java 身份及严格请求/结果模型；先验证登记列表不扫描、不运行 Java、不写业务状态。
3. 完成 Core 有界 probe 与主仓库真实进程集成，验证预算、环境和实际收尾；再接单目标校验。
4. 完成固定目录共享截止、限额及响应投影；通过合成来源测试，不依赖公网瞬时结果作为自动测试条件。
5. 接 Java 动态精确授权、候选 IPC、双目录版本和前端；验证原实例、文件、网络授权不回归。
6. 接运行引用、Broker、撤销及停止；只有前置边界通过后将对应能力加入 implemented_capabilities，并对最终接线重验。
7. 同步协议和示例，完成下节全部适用检查及真实发行物/界面验收；记录实际通过、跳过与限制。
8. Core 与前端分别提交、先完成各自本地 main 集成，再提交主仓库指针并集成原工作区本地 main。保留用户无关改动；没有本轮明确推送授权，不推送、打标签或发布。

## 8. 测试与开放门槛

| 风险 | 必须证明的行为 |
| --- | --- |
| 只读列表 | 未授权零返回；仅访问获准条目；spy确认无系统扫描、probe、登记/缓存写、配置修改或用户任务提交；首次空与损坏明确区分 |
| 严格边界 | 额外字段、true充当版本、null、超长字段/输出、未知/错类型引用、路径/URL注入及不同安装/run重放均拒绝 |
| 身份与授权 | 未批准读/用、只批其中一项、目录换绑、同名执行文件替换、javaw对应目标替换、移除重加、损坏索引、过期候选和混合目录revision冲突不能扩大范围 |
| 探测行为 | 固定参数、清理注入环境、至多指定进程、最小版本、停用/缺失/无效、输出溢出及真实超时回收；原使用/注册/安装和游戏租约回归 |
| 撤销与并发 | 用屏障固定准入前、进程开始、探测结束交付前等位置；撤销后不运行新探测、不交付结果；迟到future继续占额度与cleanup pending；不影响用户扫描、其他插件或游戏 |
| 官方目录 | 默认只取推荐的单个主版本、JRE/JDK及宿主平台正确、真实空包、错误/畸形/过量响应、两请求共同截止、缓存到期、跨run包引用及分页失效 |
| 前端 | 双版本提交、精确双选择、旧响应失效、重复保存一次、失败可重试、无登记入口、必要/可选权限、撤销及cleanup反馈；现有实例权限选择不回归 |
| 真实 Worker/发行物 | 源码及最终 PyInstaller/Nuitka 单文件启动独立 Worker；摘要→获准校验→目录查询→撤销→停止/重启；校验不自动改配置或安装Java |

拟新增 `tests/test_plugin_java_queries.py`、`tests/test_plugin_java_resources.py`、`tests/test_plugin_java_worker.py`，并更新 `tests/test_java_manager.py`、grant/permission IPC、Worker生命周期、SDK边界及主仓库架构测试。Core 更新其 `tests/test_java_probe.py`；JavaCatalog 的截止和故障场景归 Java 领域测试，不仅测试 Broker 转发。

检查命令与顺序严格遵循 [AGENTS 第6节](../../AGENTS.md#6-测试与验收)：主仓库 Ruff check、format check、适用全仓 Ruff 和完整 pytest；Core 自身 Ruff、主仓库 Core 测试与集成测试；前端先 pnpm check，再 pnpm build，随后实际启动器验收。Windows/POSIX 分支自动覆盖；第三方执行仍只承诺已验收 Windows x64，不新增 Linux/macOS Worker 支持。

真实 WebView2 验证六语、Classic/Folia、亮暗及背景透明/模糊，窗口覆盖默认、960×600最小和放大；检查长标签、滚动末项、底部操作、可见焦点和关闭恢复。冻结产物必须实际运行而非只编译；记录来源、构建器/依赖版本、hash及实际收尾，不复用旧切片的验收数量。

## 9. 数据、失败处理与审阅状态

不做历史配置迁移、旧API转发或双格式读取，不改变 JavaRegistry 现有存储版本。新身份索引缺少属于首次使用；损坏或不支持的记录保留并失败，不扫描删除历史文件。恢复与备份继续遵循所属领域现行规则。

功能撤回只下线 Java Worker 能力，释放本阶段实际创建的查询/探测资源；保留登记、已安装 Java、用户配置和授权记录，不通过 Git 回退删除业务数据。第二阶段安装来源隔离及其恢复协议没有在本方案中获准。

当前交付仅为本文和文档索引，检查 Markdown 结构、本地链接、章节引用及 git diff --check；不运行与文档无关的功能测试。功能实施和本节验收均尚未开始；用户审阅后需明确确认“开始实施”。
