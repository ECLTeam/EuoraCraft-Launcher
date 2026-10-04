# 用户体验与命名语义调研（2026-10-04）

状态：完成首轮调研及前端专项补审，已另行整理[前后端实施方案](./ux-and-semantic-implementation-plan-2026-10-04.md)，待用户审阅确定范围并开始实施。本文保留审计问题、证据及候选方案，不代表修复已完成。

## 1. 结论与审查边界

建议先做“语义整理 + 现有能力的体验闭环”，再增加 Java 自动下载等能力。

本轮确认的问题同时涉及名字和实际行为：本地模组 ID 被当成在线项目 ID 使用、Java 类型字段混入供应商名称、应用任务仍使用游戏领域名称；依赖检测和后台任务反馈也存在不完整之处。单纯批量重命名不能解决这些问题。

审查方式：

- 对主仓库 `ECL/` 自行维护的 119 个 Python 文件完成 AST 盘点，涉及 1,601 个函数/方法、357 个类、6,476 个名称赋值位置和 4,041 个参数声明，未发现语法解析错误。数字是声明/赋值位置计数，不是唯一变量数量。
- 前端补审完成 96 个 Vue 文件、158 个生产 TypeScript 文件的 AST 盘点；沿实例、账户衣柜、终端、安装、插件、联机、配置及在线资源等用户流程核实状态与调用链。全量盘点不等于逐个符号完成人工语义验收，专项结论见第 9 节。
- 对主仓库固定的 Core 提交检查扫描、Java、下载器、安装器等边界；子仓库风格不按主仓库 Python 规范评判。
- 用原函数 AST 抽取和临时 Jar/JSON 样本验证七种行为；磁盘目录及扫描器依赖使用受控替代。
- 本轮没有逐个变量完成人工语义验收，也没有实际启动游戏/桌面 UI。因此不能宣称“所有变量已经检查正确”或“发现的问题均已通过真实桌面复现”。

## 2. 实际更新与版本基线

参考仓库先获取远端，再以 `merge --ff-only` 更新当前分支；核对各自上游，ahead/behind 均为 0。

| 项目 | 调研位置与分支 | 已核实提交 |
| --- | --- | --- |
| ECL | 当前工作树；主仓库基线 | `0ef6e2b45d4d40fbd45b0d5cc690b9e5285d1c2a` |
| ECL 前端 | 当前工作树；主仓库固定指针 | `acce1363ddfcd5116bf42cc49e184f24eb6c3926` |
| ECL Core | 当前工作树；主仓库固定指针 | `f8c7a6420d2d0c68fa40f0c5c87e287e9626b390` |
| Florolding | 当前工作树；主仓库固定指针 | `97cf71cdeb5a202906a2b27ee04ea465d0151208` |
| PCL-CE | `D:/Projects/PCL-CE`；`dev` | `fdacee794e85d8ce1d2fdc111a12ba8c653f783c` |
| HMCL | `D:/Projects/HMCL`；`main` | `77eee17d361996259a48cc7896006a57d2e34a2a` |
| Qomicex | `D:/Projects/Qomicex.Tauri`；`main` | `bcbfc2032098efa3dc66ae167bb544691418fa3d` |

HMCL 原有未跟踪文档保留。原始 ECL 检出中的前端 `ConnectToolsTab.vue` 另有未提交改动；没有改动或带入当前工作树。当前前端从匹配指针重新检出，审查以当前工作树为准。

Qomicex 的五个子模块尚未初始化；本轮对它的结论限于主仓库中可读取的界面、业务服务和调用契约，未验证其子模块内部实现或实际运行。

ECL 前端远端克隆发生连接中断，改用本机已有仓库的匹配对象完成检出。Core 远端明确拒绝获取上述固定提交；本机已有原仓库保存该对象，因此恢复了匹配版本。该提交也不是本次获取的 `origin/main`（`481bf4e6...`）的祖先。这是当前构建可复现性的实际问题，后续远端集成前必须核验全部子模块指针；本轮未推送任何内容。

## 3. 命名与职责问题清单

约束：依据 `AGENTS.md` 3.3，现有序列化协议字段、第三方属性和环境变量保持契约。以下新名称是内部领域名称建议；对外字段通过明确转换边界表达语义，不能全仓字符串替换。

| 编号 | 当前名称与证据 | 实际语义/偏移 | 建议与影响 |
| --- | --- | --- | --- |
| N01 | `resources.py:381/407/440` 的 `projectId`；`mods.py:74/88` 的 `project_id` | 从 Jar 元数据读取的加载器模组 ID；与在线平台项目 ID 不是同一标识 | 内部模型用 `mod_id`，另建 `source_project_id` / `source_version_id`；既有返回字段在适配层转换并注明旧含义 |
| N02 | `scan.py:641` 的 `java_type` | 有供应商时是 vendor，无供应商时变成 JDK/JRE；一个字段承载两种维度 | 内部 Java 模型分为 `vendor` 与 `runtime_kind` 或 `is_jdk`；原协议字段保留明确的兼容输出，前端展示独立信息 |
| N03 | `game/operations.py:17` 的 `GameOperationManager`；`base.py:178` 的 `_game_operations`；前端 `GameOperation` | 注册表由应用层统一拥有，自定义下载也使用；名字过窄 | 移除无实际生产引用的兼容别名、直接导入应用任务上下文；内部引用/前端领域类型采用应用任务名称，现有 IPC/事件名继续在边界映射 |
| N04 | `scan.py:119` 的 `isBroken` | 仅检测主 JSON 是否存在，无法概括版本 JSON 可解析性、继承链、Jar 等状态 | 内部增加 `InstanceHealth` 及具体问题类别；旧 `isBroken` 由明确规则生成，界面解释原因 |
| N05 | `GameInstanceRequest.instance_id`、`_RunningGame.instance_id`，以及扫描结果 `versionId` | 前者代表一次运行进程；后者代表磁盘实例目录名，而在线资源又有自己的 version ID | 内部区分 `game_process_id`、`instance_directory_name`、`minecraft_version`、`resource_version_id`；保留边界原字段 |
| N06 | `install_online_resource(... version_id, version_id_str ...)`，见 `resources.py:1435` | `_str` 说明类型，未说明在线资源版本与实例目录的区别 | 内部参数改为清楚的实例标识与 `resource_version_id`，同步关键字调用和 docstring |
| N07 | `useTaskQueue.ts:122` 的 `clearCompleted()`；面板 `completedCount` | 实际移除/统计成功、失败、取消三类已结束任务 | 内部命名改为 `clearFinishedTasks` / `finishedCount`；是否默认保留失败记录作为产品选择 |
| N08 | `resources.py:545` 的 `installed_ids` | 含禁用模组，不能表示运行时已提供依赖；名字掩盖规则缺陷 | 明确 `enabled_mod_ids` / `provided_mod_ids`；修正集合来源及依赖诊断，详见 U01 |
| N09 | 多处 `game_path`、`data_path`、`instance_path`；`ResolvedInstanceTarget` | 有的表示 `.minecraft` 根，有的表示隔离后的游戏数据目录；调用旧模组接口时尤其易混淆 | 以目标值对象统一解析；内部用 `minecraft_root_path`、`instance_path`、`game_data_path`，不重复归一化，不改变既有隔离目录行为 |
| N10 | `speed_mb` / `speed_limit_mb`；Core Downloader 与 `install.py` | 按 1024²换算，实际是 MiB/s；单位不清易产生重复换算 | 主仓库内部用 `speed_mib_per_second` / `speed_bytes_per_second`；第三方/Core 公开参数保留，在调用边界注明单位 |
| N11 | `scan.py:114—118` 的 `hasForge` / `hasOptiFine` 等 | 基于单一 `primaryLoader` 生成，实际表示主加载器分类，而不是所有已装组件是否存在；Forge+OptiFine 样本被标为 `hasOptiFine=False` | 内部区分 `primary_loader` 与 `installed_components`；已有布尔字段按真实组件集合生成，组件版本及展示同步处理 |

`data`、`result`、`item` 等短局部名不自动视为缺陷。只有跨领域、作用域较长、表示具体业务/路径或会产生错误理解时才进入改名清单。Python 布尔前缀、公共类型和标准文件头按模块后续变更顺带整理，避免无功能的大规模改名。

全项目后续语义审查按领域继续：账户/登录的账号标识与会话状态、配置默认/当前/有效值、插件包与环境/Worker 状态、网络/缓存/超时单位、媒体资源和文件路径、联机会话与成员标识。每个符号结合类型、赋值、消费方和副作用核实，并记录“符合、内部改名、模型拆分、契约保留、行为修复”之一；本轮没有完成这些领域的逐项语义结论。

## 4. 已有能力的体验问题

### U01：依赖信息需要统一、准确并可操作（优先级高）

现有 `ResourceCoordinator.list_resources()` 已返回重复项和缺失依赖，并非完全没有依赖检测，但存在这些具体问题：

- 禁用模组也进入依赖提供集合。样本中 `consumer` 需要 `required-lib`，只有 `lib.jar.disabled`，返回的 `missingDependencies` 仍为空。
- Fabric 依赖只保存 ID，丢弃版本范围。样本中要求 `>=2.0.0`，本地为 `1.0.0`，仍返回无缺失；现阶段不能把这种结果称为兼容性检查通过。
- Fabric `provides` 未解析；样本中容器明确提供 `provided-api`，使用者仍被标为缺失。
- NeoForge 只判断旧 `mandatory`，未识别现代 `type="required"`。样本的必需依赖返回为空。
- Forge/NeoForge 仅读取第一个 `[[mods]]`，未建立多模组和 Jar-in-Jar 提供集合；此项为代码核查结论，未构造完整内嵌加载器样本。
- 实际模组页使用 `game_instance_mods_list → list_instance_mods → _list_mods_at`，另一条资源清单才计算缺失依赖；界面与检测模型分开。

建议统一解析模型和诊断服务；模组页给出“缺失必需依赖”“版本不兼容”“已提供但被禁用”“暂无法判断”四类明确结果。第一批可先解决启用状态、必需依赖、提供 ID 与诊断展示；复杂版本谓词作为第二阶段，无法判定时明确展示范围，不判为通过。

参考：Qomicex `src/lib/modDependencies.ts` 明确只统计启用模组、识别提供 ID 并排除可选依赖；它明确没有版本区间校验，不能当作完整兼容性引擎。PCL-CE 最新加入 `ModJarInJarDependency.cs`、`McConstraintMatcher.cs` 和模组关系页，适合参考依赖关系与版本约束的呈现。

格式依据：[Fabric 元数据规范](https://docs.fabricmc.net/develop/loader/fabric-mod-json)、[NeoForge 依赖配置](https://docs.neoforged.net/docs/1.20.4/gettingstarted/modfiles/)。

### U02：整合包与其他后台操作需要持续可见（优先级高）

`modpackImportStore.ts:107` 提交导入/在线安装后忽略响应中的 `operationId` 并关闭对话框。实际中文提示是“整合包导入任务已创建”，并非“安装完成”。

`useAppRuntime.ts:288` 的应用任务事件处理却只接纳 `custom_download`；因此整合包任务未进入该统一任务面板。`importing` 标志也仅覆盖提交请求，不能表示真实安装是否在运行。

建议在任务回执后立即登记应用任务，保存类型、目标、名称、取消能力与重试入口；随后由执行方最终状态决定完成/失败提示。页面切换后仍能看见任务；区分等待执行、执行中、请求取消、实际取消、完成与失败。前后端现有 `failed/error`、`cancelled/canceled` 由集中适配处理，不修改协议。

在线整合包、导入导出、复制、修复、存档备份可以逐项纳入同一面板，任务种类不再被 `install | download` 限制。哪些任务可重试应由服务能力决定，不能给全部任务加同一个重试按钮。

### U03：本地模组的在线详情目标可能错误（优先级高）

`InstanceDetailModsTab.vue:298` 在无百科链接时，把 `mod.project_id` 直接拼成 `https://modrinth.com/mod/<id>`。该值来自本地加载器元数据，没有证据证明它是 Modrinth 项目 ID/slug。

建议先使用安装来源清单；对手动放入的 Jar 复用已有哈希反查，再生成与平台相符的详情链接。无法识别时提供按名称搜索或解释原因，避免把模组 ID 作为平台标识。

### U04：坏实例应可见并说明原因（优先级高）

Core `Utils/SearchMinecraft.py:256` 的扫描对异常采取跳过。使用只有损坏 JSON 的临时版本目录，原扫描函数返回空列表对应的空映射，而不是带损坏原因的条目。

建议保留已发现的磁盘实例目录，再分别给出缺少/损坏 JSON、继承链缺失、主 Jar 缺失等问题；列表显示可诊断条目，检查结果决定是否可修复和如何处理。不能因未知错误把整个实例静默隐藏，也不能统一引导删除重装。

涉及 Core 时须在 Core 子仓库单独设计并执行其测试，主仓库补集成测试。

### U05：Java 信息与缺少运行时的恢复路径（优先级中高）

当前 Java 选择器只有扫描、浏览和刷新；后端未找到所需 Java 时返回 `JAVA_NOT_FOUND` 或 `JAVA_VERSION_NOT_FOUND`。现有版本要求推断和验证可以复用。

先统一供应商/类型/版本/架构的展示，再新增符合实例需求的 Java 下载入口：显示需要的版本、目标平台、安装位置及进度，成功后探测并登记，用户保留手动选择能力。

参考 HMCL `HMCLCore/.../download/java/mojang/MojangJavaDownloadTask.java` 的平台选择、校验与任务管线；Qomicex `src-backend/qomicex-backend/src/endpoints/java.rs` 已有下载、解压、扫描登记管线。Java 下载属于新能力，实施文档需要覆盖 Windows/POSIX、架构、校验、取消及失败回滚。

## 5. 重新核实后的功能取舍

旧 `feature-comparison-and-optimization-plan.md` 是 2026-09-26 的快照，不能直接作为当前缺口清单。

| 能力 | 当前 ECL 状态 | 建议 |
| --- | --- | --- |
| 在线整合包安装 | 已有 Modrinth/CurseForge/FTB 安装入口与服务 | 优先改善进度、失败反馈和操作模型 |
| MCBBS/HMCL/MultiMC 格式 | `ModpackFormatPolicy` 已有六种格式的识别和解析分支 | 做回归和体验完善，不重复开发 |
| 整合包导出哈希反查 | `modpack.py` 已有 Modrinth/CurseForge 匹配 | 复用能力识别本地模组来源 |
| 资源更新检查/批量更新 | 已有 API 和服务 | 与依赖诊断衔接；资源更新不等于整合包整体升级 |
| 存档编辑、备份恢复 | 已有相关服务与 IPC | 先核实现有流程，不重复建设编辑器 |
| 包装命令、退出命令、环境变量、窗口和可见性 | 当前 `launch_settings.py` 已有契约 | 维持统一解析，核实易理解的文案与效果 |
| Java 自动下载 | 本轮检查未发现完整安装链 | 新增能力第一候选 |
| 模组依赖/冲突诊断 | 已有基础 ID 检测，存在准确性和界面衔接问题 | 现有能力第一批完善 |
| 在线资源收藏与收藏夹 | 实例收藏已有；在线资源领域未见对应模型/入口 | 新增能力第二候选，参考 Qomicex 收藏服务 |
| 整合包整体升级 | 当前未见完整清单差异、用户修改保护、回滚升级管线 | 后续专项，参考 Qomicex `modpack_update.rs` 与 HMCL `ModpackUpdateTask` |
| OS 深链接唤起 | 有 CLI/单实例转发，未见对应 OS 协议注册与动作解析 | 后续专项，参考 Qomicex 新深链接分发 |
| OptiFine 安装 | 有识别/图标/类型，当前安装查询分支不支持 | 独立功能候选；先由用户确定实际目标版本与组合需求 |
| Java 库内容缓存/下载恢复 | 已有下载源回退和校验；未见 HMCL 式完整内容寻址共享缓存 | 性能专项，需先测量磁盘/网络收益，再决定 |

推荐参考路径：

- PCL-CE：`Plain Craft Launcher 2/Modules/Minecraft/ModJarInJarDependency.cs`、`McConstraintMatcher.cs`、`Pages/PageInstance/PageInstanceCompJarInJar.xaml.cs`。
- HMCL：`HMCLCore/src/main/java/org/jackhuang/hmcl/download/java/mojang/MojangJavaDownloadTask.java`、`game/GameRepositoryDraft.java`、`modpack/ModpackUpdateTask.java`、`util/CacheRepository.java`。
- Qomicex：`src/lib/modDependencies.ts`、`src/lib/deepLink.ts`、`src/stores/favoritesStore.ts`、`src-backend/qomicex-backend/src/services/resource_favorite.rs`、`modpack_update.rs`、`endpoints/java.rs`。

## 6. 可选推进方案

| 方案 | 范围 | 收益与代价 |
| --- | --- | --- |
| A：语义与结构整理 | N01—N11 中内部命名、模型分层、docstring 和边界适配；保留已有行为 | 范围较可控，后续维护更清楚；对用户直接收益有限，已发现的检测问题仍需另行修复 |
| B：语义整理与体验闭环（推荐） | 按关联功能分批处理 N01—N11、U01—U04；U05 先整理 Java 信息展示 | 直接改善依赖提示、详情跳转、任务反馈、损坏诊断；跨前端/后端，部分内容涉及 Core，需要分批验证 |
| C：分阶段扩展能力 | B 完成后加入 Java 下载、资源收藏；整合包升级、深链接等再作为独立批次 | 能力更完整；测试矩阵和迁移范围更大，需要阶段交付，避免一次打包所有功能 |

也可以先选择 B 的一个子批次：

1. 模组身份与依赖诊断：N01/N06/N08 + U01/U03。
2. 应用任务与持续反馈：N03/N07 + U02。
3. 实例身份、路径、组件识别与损坏诊断：N04/N05/N09/N11 + U04。
4. Java 模型与安装恢复：N02 + U05；自动下载另行明确范围。

前端补审后，B 应先增加一个“保存与实例/账户切换可靠性”批次，处理 F01—F06，再推进以上前两批。F07—F12 按终端、安装、路径、联机和本地化分别归属后续批次。坏实例诊断需要 Core 协同；Java 下载作为新增能力单独交付。

## 7. 后续实施文档与验收要求

用户选择范围后，再按 `AGENTS.md` 第 0 节编写对应详细实施文档，列出具体文件、模型变化、兼容边界、步骤、失败处理和测试；文档审阅并明确开始实施后才修改功能代码。

需要的行为测试至少包括：

- 模组：本地 ID 不作为平台 ID；未知来源可搜索；禁用提供者、必需/可选依赖、提供 ID、多模组、内嵌 Jar；复杂版本范围明确“无法判断”，不能错误判为满足。
- 应用任务：提交不等于完成；排队、失败、取消请求/执行停止分离；切页后持续可见；失败记录清理规则明确；最终完成后才刷新目标清单。
- 实例：多个游戏根目录中的同名实例不混淆；运行 ID 与磁盘实例 ID 分离；坏 JSON/缺 JSON/缺 Jar/继承链错误保留诊断条目；隔离目录行为保持一致。
- Java：供应商、JDK/JRE、主版本、架构独立；缺少运行时的明确恢复路径；若下载则覆盖平台/架构、校验、取消、失败恢复、登记失败。

后端最低检查：`ruff check ECL tests`、`ruff format --check ECL tests`、相关行为 pytest，以及提交前 CI 范围检查（根目录 `ruff check .`、`python -m pytest --tb=short -q`）。

前端：`pnpm check`，涉及 UI/运行流程后接 `pnpm build` 和真实启动器/IPC 验证。涉及 Core 的批次执行 Core 测试和主仓库集成测试；平台行为覆盖 Windows/POSIX。

按功能归属分别提交中文 Conventional Commits。当前远端操作只授权更新参考仓库，不授权推送 ECL；发布前解决已发现的 Core 指针可获取性，并依项目规范核验全部子模块。

## 8. 本轮证据与未做事项

受控原函数探针七项结果：

| 样本 | 当前实际返回 | 解释 |
| --- | --- | --- |
| 必需依赖只有禁用 Jar | `missingDependencies=[]` | 漏报运行时缺失 |
| 需要 2.0，本地为 1.0 | `missingDependencies=[]` | 当前未校验版本约束 |
| 容器声明 `provides` | 仍报告缺少被提供 ID | 未解析提供 ID |
| NeoForge `type="required"` | `dependencies=[]` | 漏掉现代必需依赖 |
| 磁盘存在损坏版本 JSON | 扫描返回 `{}` | 损坏实例被静默跳过 |
| 已知供应商且为 JDK | `java_type="ExampleVendor"` | 字段混合供应商和类型 |
| 库清单同时有 Forge/OptiFine | `primaryLoader=Forge, hasOptiFine=False` | 存在组件被主加载器分类掩盖 |

探针、原始结果与全量后端名称位置清单保存在工作区外的审查产物目录：

`C:/Users/Wuchang325/.codex/visualizations/2026/10/04/01a106f1-483e-7223-b307-67b62c34afb3/`

包含 `semantic_audit_probe.py`、`semantic-audit-evidence.json`、`backend-symbol-inventory.json`。该探针用于验证审查结论，不能替代修复提交所需的 pytest/前端测试/真实桌面验收。

本轮没有修改功能代码、运行后端全量 CI、声明桌面验收通过或进行远端推送。前端专项补审已运行 `pnpm check` 和 `pnpm build`，结果见下文。

## 9. 前端专项补审

### 9.1 范围、方法与验证边界

补审以当前主仓库固定的前端提交 `acce1363ddfcd5116bf42cc49e184f24eb6c3926` 为准，不使用原始检出中的未提交文件。

全量 AST 盘点结果：96 个 Vue 文件、158 个生产 TS 文件；另有 128 个测试文件、2 个声明文件。生产代码中记录了 3,284 个函数式节点（包括箭头函数）、4,129 个变量绑定声明、2,703 个参数、259 个 interface、95 个 type alias，解析错误为零。上述数字是声明位置统计，不能当成独立业务变量数量。

人工复核重点为：实例目录切换、实例详情与自动保存、安装器状态、在线资源安装目标、账户和衣柜、插件动作、终端输出、联机扫描、共享设置、资源请求缓存与页面生命周期。路由、错误边界、主题和弹窗入口也做了检索；本轮未对所有样式、键盘/读屏交互和每个组件完成真实桌面验收。

行为证据来自当前源文件中的原 TS 模块/函数，经 TypeScript 转译后在隔离上下文运行；Vue 响应式使用真实库，IPC、计时器和组件生命周期使用受控替代，故可以验证请求乱序和状态变更，但不能替代真实组件挂载、Windows WebView2、网络或游戏启动验证。

### 9.2 前端命名与模型语义

下列问题应结合行为与调用方处理，不适合仅做字符串替换。主仓库 Python 命名要求不直接套用于前端；现有 IPC 字段保持协议，在前端业务模型中转换。

| 编号 | 名称与位置（相对 `frontend/src/`） | 实际语义 | 处理方向 |
| --- | --- | --- | --- |
| FN01 | `features/instances/stores/instanceInstallStore.ts:18/22/78` 的 `installingVersionId`、`isInstalling`、`install()` | 只覆盖提交安装请求；收到后台 `taskId` 即退出，任务仍可能运行 | 若只用于按钮提交态，采用 `submittingInstanceId` / `isSubmittingInstall` / `submitInstall`；实际安装态来自任务注册表。常规安装已有全局进度，不能据此声称没有进度 |
| FN02 | `components/instances/InstanceDetailProfileTab.vue:133/171` 的 `savedProfileSnapshot` | 名称承诺“已保存快照”，实际取的是响应到达时最新表单，而非本次已写入内容 | 先修复 F02，再使用准确的已确认提交快照；不能只改名掩盖未落盘状态 |
| FN03 | `types/instances.ts:85` 的 `ScannedVersion`、`versionId`；store 的 `selectedVersion` | 数据主体是磁盘实例，`versionId` / 选中值常为实例目录名；原版版本、资源版本又使用相近名称 | 业务模型区分 `ScannedInstance`、`instanceDirectoryName`、`minecraftVersion`、`resourceVersionId`，保留 API 适配边界；需结合 N05/N09 与同名跨目录实例处理 |
| FN04 | `features/terminal/composables/useProcessInstances.ts` 的 `instances`、`selectedId`、`outputs` | 表示运行进程及其输出；不同于磁盘实例清单 | 在共享模型/跨模块使用处明确 `processEntries`、`selectedProcessId`、`outputByProcessId`；组件内短名可保留，不强制全仓改名 |
| FN05 | `composables/useAsyncAction.ts:30` 的 `run<T>(): Promise<T \| null>` | 失败返回 null，成功的 void/undefined 也归一成 null；仅 false 阻止成功提示 | 改进动作结果契约，使成功、失败、取消、重复提交未执行可区分，并按动作管理等待状态；这是行为契约调整，需审阅后实施，见 F06 |

原报告 N03（应用任务被称为游戏任务）、N07（completed 实际涵盖全部结束状态）、U03（本地模组 ID 当在线平台 ID）也已沿前端消费方核实。`data`、`result`、`item`、局部 `selectedKey` 并不因名称短而自动成为问题，需看所在作用域与类型是否已经明确语义。

### 9.3 前端自身的行为问题

以下优先级是改进建议。标为“受控复现”的结果已经运行原函数探针；代码核查项不包装成桌面复现。

| 编号/优先级 | 位置（相对 `frontend/src/`） | 触发与后果 | 建议及证据 |
| --- | --- | --- | --- |
| F01 / 高 | `features/instances/stores/instanceStore.ts:202` | 连续切目录 A→B，B 先返回、A 后返回；旧请求重新选择 A 中实例，并把当前目录切回 A | 增加选择代次/目标校验，旧响应不得更新选择。受控复现：最终目录 A、实例 instance-a，而最后用户选择为 B |
| F02 / 高 | `components/instances/InstanceDetailProfileTab.vue:151` | 提交表单 A 期间继续编辑为 B，A 成功后将 B 记为已保存，并更新实例展示；后续关闭/卸载的脏状态判断可能跳过 B | 捕获目标和本次 payload/表单快照，只确认实际提交内容；后续编辑继续排队。受控复现：提交 saved-A，确认快照和展示却为 unsaved-B。若待保存计时器被卸载清理且快照已误确认，会跳过最终 flush；本轮未进行真实挂载后的磁盘丢失复现 |
| F03 / 高 | `features/accounts/components/WardrobeModal.vue:419/447` | 本地皮肤 old→new，old 迟到覆盖预览；账户纹理只在第一段 await 后校验，后续纹理请求仍可覆盖新账户 | 所有异步阶段使用相同目标/代次校验，完成后统一提交预览。两种受控复现：选中 new 却显示 old.png；选中账户 B 却显示 A 的皮肤和披风。未声称实际上传已错绑账户 |
| F04 / 高 | `components/instances/InstanceDetailModsTab.vue:197/309`；`views/instances/InstanceDetailModal.vue:39` | 模组加载不校验迟到响应；watch 仅比较 versionId，不含目录。父层页面 key 仅为 activeTab，组件复用时可能保留同名异目录旧内容 | 统一实例身份“根目录 + 实例目录名”，加载捕获目标并检查代次。受控复现：当前 B，晚到 A 列表最终显示 A.jar。存档/截图/服务器页主要 onMounted 加载，也应补查复用生命周期；截图取缩略图时读取当前 target，需保持与列表请求一致。正常关闭重开可能因切回 overview 而重新挂载，不能断言所有打开方式均触发 |
| F05 / 高 | `composables/useResourceInstallTarget.ts:113` | 每个资源类型先读整个 download 配置再整段 set；两个类型并发保存会基于同一旧快照互相覆盖，也绕过 settingsStore 的写入队列 | 使用统一配置更新入口；优先由后端提供字段级原子更新或明确的共享串行边界。受控复现：mod 与 resourcepack 并发保存，最终只剩 resourcepack 键。不能只给各 composable 各自加独立队列 |
| F06 / 高 | `features/plugins/stores/pluginStore.ts:65`；`composables/useAsyncAction.ts:38`；`views/Plugins.vue:229` | 重复插件切换被 store 短路返回 void；外层将“未执行”当成功并提示，第二次调用还提前结束共用 loading | 重复动作共享同一个在途 Promise，或返回明确未执行结果；按钮按插件动作禁用，成功提示由结果决定。受控复现：原操作仍在等待时已出现 1 次成功且 loading=false，随后原操作失败 |
| F07 / 中高 | `features/terminal/composables/useProcessInstances.ts:38`；`features/terminal/components/ProcessInstanceView.vue` | 进程退出清空 selectedId，输出即不可见；下次同步删除已退出缓冲，与“保留选中输出”注释不符 | 分离运行进程与保留的终端会话；显示已退出状态并允许查看/复制最后输出，设置明确的缓存上限。受控复现：退出后可见 0 行，下一同步后历史缓冲被删。指当前终端视图，未断言其他日志文件也丢失 |
| F08 / 中 | `features/instances/stores/instanceInstallStore.ts:24/45/66` | clearLoaderVersions 和空版本分支不使旧请求失效；清空后旧结果重新填入 store，读取序号保护只覆盖后发查询 | 清空/重置也推进请求代次；旧请求不得改变数据、loading 或当前错误反馈。受控复现：清空后迟到 Forge 列表重新出现 20 条 |
| F09 / 中 | `features/instances/stores/instanceInstallStore.ts:37/58` | 加载器和 Fabric API 列表强制 slice(0,20)，选择界面未找到继续加载入口 | 保留完整元数据，或提供明确展开/分页和版本检索。受控复现：API 给 30 条，store 只能提供 20 条；并非后端只返回 20 条 |
| F10 / 中高 | `utils/path.ts:8`；`features/instances/api/instanceWorkspaceApi.ts:43` | 路径身份无条件转小写；在大小写敏感文件系统中，不同目录 Pack/pack 合并成同一身份/缓存键 | 依据实际平台/文件系统语义统一身份，优先使用后端生成的稳定目标键；Windows 的大小写兼容也须保留。受控复现：/games/Pack 与 /games/pack 均归一成 /games/pack；本轮运行环境为 Windows，未做 Linux 实盘验证 |
| F11 / 中 | `components/instances/InstanceWorldsTab.vue:220/233`、`InstanceServersTab.vue:60/202`、`InstanceDetailProfileTab.vue:206` | 难度/模式/操作标签、服务器状态等用户文案直接写中文；切换其他语言仍夹带中文 | 接入现有 i18n，语言依赖的选项用 computed；业务值与显示文案分离。代码核查，未把全部中文日志或源码文字当成翻译缺陷 |
| F12 / 中 | `features/connect/composables/useConnector.ts:126/156/167` | stopPortScan 只清定时器，不阻止在途请求改状态；定时器也没有请求在途保护，慢响应可重叠 | 扫描停止/卸载使代次失效；每轮等待上一轮结束，限制并发，并检查当前会话。受控复现：scanning=false，但停止后迟到响应仍把 phase 改成 searching；未声称扫描定时器重新启动 |

F01/F03/F04 属于同一类异步目标一致性问题，但作用域不同：全局选择、账户/纹理、实例页面。应采用共同的检查原则，不能用一个全局 loading 或全仓请求序号替代各自的目标身份。

### 9.4 已有正确做法与工程整理方向

- `InstanceDetailSettingsTab.vue:516/562` 已捕获目标及序列化保存快照，使用 saveChain 排队，并检查请求/目标身份。资料自动保存应参考此模式，保留现有即时保存体验。
- `instanceStore.loadAll`、账户 reload 和在线资源搜索已有请求时序防护；插件列表还有在途请求合并。问题是具体路径保护不一致，不需要整体替换状态管理库。
- `InstanceResourcesTab.vue` 已有加载代次保护；模组、存档、截图、服务器页应统一审查目标切换、卸载、刷新与错误呈现，而非重复创建另一套资源架构。
- 插件过滤文案使用 computed 翻译，实例工具栏部分图标已有 aria-label。不能把局部硬编码文案泛化为“整个前端没有国际化/可访问性”。
- 大组件可按明确业务职责逐步拆分：`OnlineModSearch.vue` 1,846 行、`WardrobeModal.vue` 978 行；拆分检索、目标选择、预览加载和实际操作边界，比为行数统一拆文件更有价值。每次应随相关行为修复收敛，不新增第二套同用途 store/保存机制。

### 9.5 验证结果与审查产物

- `pnpm check` 通过：Prettier、ESLint、应用/Node 类型检查通过；128 个测试文件通过，683 个测试通过，1 个 todo。
- `pnpm build` 通过。构建提示部分压缩后 chunk 超过 500 kB；这是构建告警，尚无启动耗时/低配机器测量，不把它直接定性成已证实的性能瓶颈。
- 13 条受控探针记录成功产生，分别覆盖路径大小写、目录切换、清空请求失效、列表截断、提交状态、保存快照、两种衣柜乱序、终端退出历史、停止扫描、配置覆盖、插件重复提示和模组列表乱序。它们是审查证据，当前仓库原有测试通过不代表这些缺陷已修复。
- 工作区外产物：`frontend_audit_inventory.cjs`、`frontend-symbol-inventory.json`、`frontend_audit_probe.cjs`、`frontend-audit-evidence.json`，与第 8 节产物位于同一目录。
- 未修改前端生产代码或测试文件；前端子仓库保持干净。未实际启动桌面启动器，未声明真实 IPC/UI 验收通过。

后续若实施，需将对应探针场景转成仓库中的回归测试，而不是提交依赖外部绝对路径的审查脚本；优先覆盖“旧请求不能更新新目标”“保存快照与实际 payload 一致”“重复未执行不能报成功”“进程退出后输出仍可见”。组件生命周期相关行为需补真实挂载测试，再按项目要求进行桌面验证。

## 10. 工具页命名、结构与状态补审

### 10.1 范围与现状

本次沿 `/more/tools` 核查 `Connect.vue → ConnectToolsTab.vue → CustomDownloadCard / SkinAvatarToolCard / NatToolCard`，并检查下载 store、运行时任务事件、后端下载重试与校验、样式和现有测试。主仓库/前端 HEAD 与第 2 节一致，未修改功能代码。

此前工具页布局方案已存在；其历史授权与实现状态不作为本轮结构变更授权。当前代码已经有三张独立工具卡片，NAT 不再堆在页面脚本中；皮肤处理也已有独立 `features/tools/skinAvatar.ts`。本轮针对实际职责边界和状态一致性，不重复设计工具首页或调整透明度。

### 10.2 问题清单

| 编号/类型 | 证据与实际职责 | 改进方向 |
| --- | --- | --- |
| T01 / 命名与领域结构 | `/more` 外壳名为 `views/Connect.vue`，通用工具页为 `views/connect/ConnectToolsTab.vue`，下载/皮肤头像卡片与样式放在 `components/connect` / `styles/components/connect`；页面导入联机专用 `Connect.css`，工具样式使用 connect-tools-*，文案跨 connect.tools 和 advanced.download | 使用 More/Tools 的页面与展示组件目录，工具布局独立为 Tools.css；下载业务仍归 features/download，皮肤算法归 features/tools。NAT 的网络 API 属于 connector，不能因为卡片移目录就把业务接口也随意移动 |
| T02 / 壳层职责耦合 | `views/Connect.vue:24` 无条件 provideConnector(useConnector(...))；`useConnector.ts:181` mounted 会查询 connector 状态，成功后查询 EasyTier。直接进入工具或插件路由也挂载该外壳；connectorContext 注释声称工具与联机解耦，只描述了工具不主动 inject | 更多页面壳只负责导航；联机会话的初始化、状态保留和轮询由独立宿主持有，进入联机页或有有效会话时启动，切工具页不丢连接。不把后台状态查询夸大为自动创建房间或执行 NAT |
| T03 / 高，状态生命周期拆散 | `customDownloadStore.ts:6` 保留表单、operationId/operation；`CustomDownloadCard.vue:186/190/304` 的 starting/cancelling/error/timer 留在组件，track 在写共享 operationId 前不检查活动状态 | 提交与任务控制状态归会话级控制器，组件只负责显示及交互。受控复现：A 提交未返回时离开并重挂，busy=false，可提交 B；B 先返回，A 后返回后 trackedOperationId=A、displayedOperationId=B。不是只缺一个卸载布尔检查，而是共享提交状态不完整 |
| T04 / 高，任务状态来源与时序 | 卡片 poll/cancel 会并发查询，没有请求代次及结束态保护；`useAppRuntime.ts:288` 另将同一下载事件写入全局任务队列，但不更新下载 store | 共用应用任务状态与集中查询回退，卡片按 trackedOperationId 读取。受控复现：取消查询确认 cancelled 后，早先查询返回 running，卡片恢复 busy；应拒绝旧快照及终态回退，不能只核对相同任务 ID |
| T05 / 中高，查询失败恢复 | `CustomDownloadCard.vue:285` 的 poll 捕获异常只设置 error，不再安排刷新；operation 仍为 pending/running，因此表单保持 busy，失败/取消任务的“重试”条件也不满足 | 区分查询错误与执行失败，提供刷新状态及有上限的查询恢复。受控复现：remainsBusy=true、nextRefreshTimers=0、任务重试按钮不出现。不能把一次查询失败直接改成下载失败 |
| T06 / 中，前后端校验偏差 | `CustomDownloadCard.vue:207` 的设备名正则与 `ECL/services/custom_downloads.py:157` 的 DownloadPolicy 不一致 | 校验规则提取到明确的前端领域函数，并以共同样本约束两端。受控复现：CON .txt 的前端 validationError 为空，后端 is_valid_name=False；现有 CON.zip 测试没有覆盖该变体 |
| T07 / 体验语义，代码核查 | 下载失败后允许编辑当前表单，但 retry 只提交旧 operation_id；后端 `CustomDownloadService.retry:306` 重用原意图，且可能使用原已解析目标路径 | 分开可编辑表单与上次任务快照。明确“重试原任务”使用原参数，“开始下载”使用当前表单；这属于行为说明与呈现改进，不把后端正确重试旧任务描述为实现错误 |
| T08 / 局部命名清晰度，非全部缺陷 | 自定义下载的 headers 是含 UI id 的编辑行，并非 IPC headers 字典；starting 还等待首次 poll，cancelling 覆盖取消请求和查询；皮肤工具的 preview 是 PNG data URL，size 是像素边长，requestVersion 是选择请求代次 | 跨控制器/模型边界用 headerRows、headerByName、isSubmittingDownload、isRequestingCancel、avatarPngDataUrl、avatarSize、selectionRevision 等。短局部名本身不判错；不能将请求代次命名为皮肤内容版本，也不能称取消请求已获执行停止 |

### 10.3 建议结构与约束

推荐“页面领域整理 + 下载控制状态集中”的完整方案；只移动文件和改前缀可以改善导航，却不能解决 T03—T06。两者按独立功能提交，使用同一实施文档的 P3-T、P6-T 子批次追踪。

建议页面为 `views/More.vue`、`views/more/ToolsTab.vue`；展示卡片与工具专属样式位于 `components/tools`、`styles/components/tools`；工具布局使用 `styles/views/Tools.css`。下载 API、表单校验和会话控制继续放在 `features/download`，皮肤算法保留 `features/tools`，NAT 探测仍复用 connectorApi。通用布局规则在实际共享时复用，不为了迁目录复制 Connect.css 全文件。

不增加工具注册系统或额外中间选择页；保留当前三张卡片、折叠选项、布局、主题和准确的文件名预估说明。NAT 已有重复点击互斥与卸载响应保护；皮肤工具已有选择请求校验、解码取消、Object URL 释放和损坏/取消保留旧预览，继续复用这些正确实现。

### 10.4 验证结果

工具页、下载卡片、皮肤卡片、皮肤算法四个测试文件运行通过，46 个测试通过。它们主要覆盖参数、普通完成后的切页、NAT 状态、像素处理和导出，不证明本次新增边界场景已通过。

新增受控原函数探针四条：提交期间重挂与旧回执、查询失败停止刷新、取消查询乱序、文件名校验偏差；后端校验通过原 DownloadPolicy AST 单独验证。前端使用原 SFC 函数/计算表达式、真实 Vue refs，导航/IPC/计时器受控替代，未进行真实桌面或实际下载验证。

工作区外产物为 `tools_audit_probe.cjs`、`tools-audit-evidence.json`，与第 8/9 节位于同一审查产物目录。命名、壳层初始化和重试参数来源属于代码核查结论，与四条动态探针结果分开记录。
