# 全仓冗余属性与方法清理实施方案

状态：用户已确认开始实施；清单中的生产代码清理、自动检查和 Windows 桌面验收已完成。

## 目标与判定标准

移除像 `ConnectorService.available`、`easytier_available` 这样没有实际检测或业务行为、却长期暴露状态接口的代码。范围同时包括确认失去调用的私有实现、重复兼容别名和占位 API，随之清理调用方、协议声明、日志、导入、测试替身及文档。

每个删除项须至少具备一项明确证据：

1. 固定返回“可用/已安装/完成”，既不检测状态也不执行所宣称的动作。
2. 纯空实现且无实际回调或接口职责。
3. 私有实现没有代码调用、回调登记或动态引用，且不是框架约定的重写点。
4. 仅保留旧名称的重复转发，项目调用可以直接使用现有权威接口。

函数体短、返回空集合或只转发一次调用本身不能证明冗余。返回实际业务数据、承担正式协议边界、生成独立默认对象或实现必需生命周期的接口，按其实际职责判断。

## 审计范围与结果

已检查 158 个 Python 生产文件，包含主仓库、Core、Florolding、根入口、打包/维护脚本及内置插件；检查 272 个前端 TS/Vue 文件及另 8 个配置/脚本 JS/TS 文件。使用语法树识别固定返回值、空函数、直接转发和重复属性，并结合全项目引用检查。测试代码用于核对调用和契约，不将测试替身当成生产功能删除。

以下是当前确认的清理清单。实施时复核动态引用及继承关系；相同标准下发现的连带死代码纳入同一清理，新增业务范围则先补充本文。

### 1. 固定可用状态和占位联机接口

| 位置 | 删除项 | 当前证据与同步改动 |
| --- | --- | --- |
| `ECL/services/connector.py` | `available`、`easytier_available` | 恒为 True，仅用于日志；同步初始化、建房日志和测试替身 |
| 同上 | `get_easytier_status` | 固定返回 installed/progress=100，未查询安装过程或运行状态 |
| `ECL/api/connector.py` | `connector_easytier_status`、`connector_easytier_download` | 两者都返回上述固定状态，“下载”接口没有下载行为 |
| 同上 | `connector_match_instances` | 固定返回空 mods/instances，没有实现匹配 |
| `frontend/src/features/connect/api/connectorApi.ts` | `easyTierStatus`、`downloadEasyTier`、`matchInstances` | 同步删除这三条占位命令的客户端入口 |
| `frontend/src/features/connect/composables/useConnector.ts` | `refreshEasyTier` 及仅支撑该轮询的 easyTier 状态 | 初始化额外请求固定状态；移除这条状态链 |

同步清理：

- 正式 IPC 注册表、`IpcLogPolicy`、相关模型/契约测试、前端请求与响应类型、命令名称表。
- `EasyTierStatus`、`ConnectorMatchResult` 及仅支撑退休命令的展示模式模拟状态和响应。
- `ConnectRoomTab.vue` 的 `serviceReady` 改为使用真实的联机状态查询结果 `availability`，移除恒为 True 的 installed 条件；保留查询失败提示、重试、操作 busy 状态和实际节点/房间状态。
- `easytier_version` 继续提供真实版本信息；创建、加入、退出房间、NAT 检测、端口检测和节点配置接口继续履行既有职责。

### 2. 已无调用的 Python 私有实现

| 位置 | 删除项 | 证据 |
| --- | --- | --- |
| `ECL/game/Utils/SearchMinecraft.py` | `_find_ver_from_server_class` | pass 空实现，未被调用 |
| `ECL/services/game/resources.py` | `_join_authors`、`_parse_mod`、`_parse_pack` | 没有项目调用；现有资源解析使用 Core 的解析和验证原语 |
| 同上 | `_safe_json` | 仅被待删除的 `_parse_pack` 使用，连带删除 |
| `ECL/services/game/mods.py` | `_mod_path` | 无调用；现有方法使用 `_mod_path_at` 与实例路径解析 |
| `ECL/services/game/instance_profiles.py` | `_parse_datetime` | 没有代码调用或动态登记 |
| `ECL/host/app_update.py` | `_binary_suffix` | 没有调用；现有资产选择直接使用实际筛选逻辑 |
| `ECL/api/bridge.py` | `_game_runtime_options`、仅供其使用的 `runtime_option_fields` | 旧启动参数拼装没有调用；现有启动入口使用独立设置解析服务 |
| Florolding 的 `Florolding/F_Server.py` | `_parse_request` | 无调用；服务器使用有界的 `_read_request` 读取实际协议帧 |
| Florolding 的 `Florolding/F_Client.py` | `_parse_response` | 无调用；客户端在 `send_request` 中读取实际响应帧 |

随方法删除清理只由这些实现使用的导入、常量与说明，保持现有实际解析、校验和网络边界。

### 3. 重复兼容别名

- `ResolvedInstanceTarget.game_path`、`version_id`、`data_path` 只是分别转发 `minecraft_root_path`、`instance_directory_name`、`game_data_path`。调整主仓库联机、快捷方式、截图、原理图等实际调用与测试后，删除三个旧别名；保留实例身份键和实际路径解析。
- `frontend/src/composables/useUserAgreement.ts` 顶层导出的 `acceptUserAgreement`、`rejectUserAgreement` 是重复兼容包装。App 实际从 `useUserAgreement()` 获取方法，没有导入这两个顶层包装；复核后删除，保留 composable 内真正的协议接受/撤销操作。
- 请求 JSON、配置字段与第三方库属性按既有协议处理，不能按相似名称机械替换。

## 经审计保留的职责

- `_default_stats`、`_default_profile` 和内置插件的默认信息工厂有实际调用，负责生成互不共享的默认数据。
- `system_ping` 的固定响应是实际 IPC 心跳协议。
- `InstancesManager._noop` 用于默认日志/退出回调，满足调用方的可调用契约。
- 浏览器 WebSocket、展示模式和不可用传输层的 `convertFileSrc` 返回 null，表示该传输没有原生文件映射能力，是传输接口的有效结果。
- SDK 卸载回调、弹窗退场回调的初始空函数、明确处理失败的 catch 回调，以及界面注入的默认权限状态有实际调用或控制流程职责。
- 业务 API 与服务门面中的短转发负责协议转换、领域参数/路径处理、状态访问或公共接口组织，不按函数行数删除。

上述说明用于确保删除的是冗余，而不是项目仍依赖的接口行为。

## 方案比较

| 方案 | 范围 | 权衡 |
| --- | --- | --- |
| 完整清理，建议采用 | 全部确认冗余项、连带死代码、协议调用链和重复别名 | 满足用户全项目清理要求，覆盖三个子模块中的相关仓库及主仓库 |
| 仅清理固定状态 | 联机两项属性及固定安装/下载/匹配状态链 | 改动集中，但已确认的死方法和重复别名仍存在 |

本文按用户“全部移除”的要求细化完整清理方案。

## 实施步骤

1. 用户审阅并明确确认开始实施后，重新检查工作区、所有受影响子仓库和原始本地 main，保留无关改动。
2. 为固定 EasyTier 状态造成的虚假等待/可用判断、退休 IPC 契约及别名迁移添加必要回归；不为纯死代码删除编写镜像实现的测试。
3. 删除联机固定状态和占位 API，完整调整前端初始化、可用性判断及协议声明。
4. 删除无调用私有实现和连带内容，调整旧别名调用及测试；使用实际对象类型区分同名字段。
5. 搜索旧符号、退休命令和遗留模拟数据，复核每项删除的调用、注册、继承与公共接口影响。
6. 执行主仓库 Ruff、格式检查与全量 pytest；执行 Core 自身 Ruff 和测试，以及 Florolding 的对应现有测试/集成检查。
7. 前端依次执行 `pnpm check`、`pnpm build`；实际启动 Windows 启动器验证联机页初始化、状态查询失败与重试、NAT/端口入口、实例信息和用户协议流程。用合适的本地受控验证覆盖 Florolding 请求/响应，不依赖远程公共节点作为唯一证据。
8. 分仓以中文 Conventional Commit 提交已验证改动；涉及的子仓库先同步原始工作区本地 main，再同步主仓库 gitlink 与 main。远程推送需用户另行明确要求。
9. 在本文补充最终删除清单、验证结果、未覆盖条件和提交记录，汇报完成边界。

## 影响与验收要求

- 退休的三条 IPC 命令将从正式注册与客户端声明中删除，调用方需使用现存真实联机操作；不保留返回固定成功的兼容入口。
- 删除伪安装状态后，联机页面以真实状态查询成功为基础，不应再等待一个没有实际安装过程的“已安装”响应。
- 架构上保证正常创建/加入/退出和真实失败仍经既有错误转换、日志与前端提示处理。
- Core 和 Florolding 按自身规范修改，主仓库同时执行集成验证。
- 最终清理与验证结果见下文。

## 实施结果（2026-10-10）

### 最终删除范围

- 清单中的 20 个 Python 属性/方法/函数及 6 个前端方法/包装已删除，共 26 项；同时删除仅供旧启动参数方法使用的 `runtime_option_fields`。
- 三条占位 IPC 从处理器、注册表、操作日志、前端命令/参数/响应声明及展示模式模拟中完整退休。删除 EasyTier 安装状态和联机匹配响应类型及其附属条目类型。
- 联机初始化只请求真实 `connector_status`；页面使用真实 availability 控制可用性，不再请求或等待固定的安装完成状态。
- 实例目录别名的所有实际调用和测试已使用明确字段，IPC 请求 JSON 和其他领域对象的同名字段保持原协议。
- 清理无调用私有实现、重复协议接受包装及其独占导入；Core 与 Florolding 保持原有文件风格，Florolding 两个文件仅删除 37 行旧解析方法。
- 重新执行全仓 Python 私有函数引用扫描，没有剩余的未引用私有候选。退休符号仅在必要的契约负向覆盖中保留；有实际职责的默认工厂、心跳、回调和业务门面继续使用。

### 自动检查

1. 主仓库 `ruff check ECL tests`、`ruff format --check ECL tests`、相关仓库 diff 检查通过。
2. 主仓库全量 pytest：1314 项通过、7 项跳过。
3. Core 全仓 `ruff check .` 通过。Core 与联机协议、实例工作台、IPC 相关的针对性运行：161 项通过，其中包括 Core 的全部 12 项测试及现有 Florolding 请求/响应分帧集成测试。
4. 前端 `pnpm check` 通过：153 个测试文件、806 项通过、1 项既有 todo。`pnpm build` 通过，仍有既有的分包大小提示。
5. 新增的初始化回归在清理前失败，证明之前会额外请求固定安装状态；清理后通过。既有 API 契约、节点配置、房间状态/操作、路径隔离、快捷方式、截图/原理图及协议读取测试通过。

### Windows 实际验收

- 使用本次构建产物运行真实 Tauri/WebView2，使用隔离数据和独立 WebView2 用户目录，关闭验收进程的展示模式。
- 在验收辅助启动脚本中注入受控状态查询失败，联机页面正确禁用输入。释放故障后调用会话现有重试操作，正式状态查询成功，页面恢复输入可用；真实后端日志记录查询恢复。
- 通过真实 PyTauri 调用验证三条退休命令均拒绝执行；实际端口探测 IPC 成功，NAT 工具页入口可见。NAT 的真实公网网络类型和实际跨机器建房/入房未作为本轮验收完成声明。
- 通过正式用户协议要求事件打开协议界面，点击“同意”后经实际保存 IPC 持久化接受状态，界面正常关闭。独立数据中的接受状态通过正式读取 IPC 验证。
- 本地受控协议分帧与生命周期由现有集成测试覆盖，未依赖公共中继节点作为成功依据。

### 提交与同步

验证后分别提交 Core、Florolding、前端和主仓库，受影响子仓库先同步原始工作区本地 main，再同步主仓库 gitlink 与 main。远程推送仍需用户另行明确要求。
