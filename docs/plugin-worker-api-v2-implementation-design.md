# ECL 插件子进程隔离与用户权限管理实施设计 v1.2

> 目录约定更新：启动引导与插件 API 已按 [下沉实施方案](python-entrypoint-consolidation-plan.md) 归入 `ECL/bootstrap.py` 和 `ECL/plugin_api/`。当前插件使用 `from ECL.plugin_api import Plugin`；本文中的旧根模块、独立包路径和“禁止所有 ECL 导入”描述保留为对应阶段的设计记录，现行运行边界以 [无头运行说明](headless-runtime.md) 为准。

状态：用户已明确批准 v1.1 及管理器接入方案开始实施。Worker 基础运行层、v2 授权存储、第三方管理器分流及最小可信管理界面已实现，当前结果见第 21 节。真实 WebView2 网络门未通过，原生增补已获批准，但用户随后要求先越过该部分；自定义前端继续关闭。

日期：2026-10-12（Asia/Shanghai）；原始调研与 v1.1 日期为 2026-10-11。

源码调研基线：主仓库 `f18c11929d2bcf46a28720440db62ae83645e023`；前端 `6429f15c695f6a92a3742aa59f20c3a5292ae4d9`。

用户已确认：九项设计问题均选择方案①。最终方向为“同一 ECL 发行物、第三方插件独立 Worker、API v2、用户默认拒绝授权”，首版先验收 Windows x64。

本方案以当前实际源码和本轮用户选择为依据，不采用已取消的旧 Worker 草案作为实施或验收依据。保存本文件不代表用户已批准实施。

修订说明：第 1—17 节保留批准的总体设计和验收目标；第 18 节记录实际实施结果，不能将拟议接口目录视为已经实现。v1.2 补充原生前端阻塞证据及用户所选修订方向，不扩大首版安全承诺。

## 1. 已确定的范围与完成条件

| 决策 | 落地要求 |
|---|---|
| 安全目标 | 隔离 Python 运行状态，建立受控 Host API；明确不提供恶意 Python 的 OS 文件/网络沙箱 |
| 前端 | 主窗口仅接收第三方声明式 UI；自定义脚本进入独立受限环境 |
| SDK | API v2，允许破坏性升级，不兼容宿主对象和闭包共享 |
| 插件范围 | 目录插件、归档第三方插件全部 Worker；随发行物维护的系统插件暂留宿主 |
| 发行物 | 优先复用同一个可执行文件；受阻的平台可评估 Sidecar |
| 平台 | Windows x64 先验收；其他平台第三方插件暂不执行，不回退主进程 |
| 授权 | 默认拒绝；安装、更新集中审查；必要权限缺失则禁用，可选权限允许降级 |
| 故障处理 | 有界超时、进程树停止、资源统计；默认手动重启 |
| 撤销 | 按操作分类取消；关键认证/启动步骤失败，可选扩展跳过 |

首版完成必须同时满足：真实冻结 Worker 分流通过、第三方不在宿主执行、授权与撤销覆盖 Host API、前端不存在第三方主窗口脚本注入、进程树可回收、安装回归与项目门禁通过。

## 2. 目标结构与信任边界

```text
ECL 主进程
  ├─ 现有账户 / 游戏 / Java / 联机 / 下载服务
  ├─ 插件管理器：安装、授权、运行状态、宿主注册
  ├─ Host API Broker：身份、权限、数据校验、任务归属
  ├─ Worker Supervisor：创建、IPC、超时、退出、进程树
  └─ 前端
      ├─ 主窗口：宿主渲染声明式插件 UI
      └─ 受限插件窗口：自定义前端代码

每个第三方插件
  └─ 同一发行物启动的 Worker
      ├─ 轻量 API v2
      ├─ 自己的模块缓存与依赖路径
      └─ 插件代码及本地回调
```

系统插件身份由宿主的发行资源来源决定，插件清单不能自行选择“系统插件”或“主进程模式”。第三方也不能通过事件伪装成系统插件，申请主窗口脚本注入。

宿主仍保有全部业务服务。Worker 不调用 `create_application()`，不建立第二套游戏、账户或联机服务。

## 3. 启动入口、SDK 与依赖隔离

当前 [启动入口](../main.py) 在模块顶部导入正常 CLI；当前 [插件包入口](../ECL/plugins/__init__.py) 又导入管理器及多类扩展。直接沿用这些入口，会提前加载宿主依赖，削弱不同依赖版本的隔离。

拟采用独立、轻量的顶层包 `ecl_plugin_api`，第三方 v2 使用：

```python
from ecl_plugin_api import Plugin
```

该包仅依赖标准库，提供插件基类、结构化契约、IPC 客户端及内部 Worker 入口。系统插件继续使用当前 `ECL.plugins.Plugin`。这两个入口分别承担不同运行契约，不建立旧 SDK 自动识别或兼容适配。

具体改造：

- `main.py` 在导入 `ECL` 前识别内部 Worker 模式。
- 开发环境使用当前 Python 加项目入口；冻结环境使用经验证的当前发行文件路径。
- Worker 分支跳过正常 CLI、单实例、窗口、更新维护、宿主配置及主日志初始化。
- 启动失败必须返回明确错误，不能转入普通启动器模式。
- 正常启动器、更新重启、Worker 启动分别处理打包引导环境。
- Worker 初始环境仅保留运行必要变量；清理开发通道信息、无关凭据及导入路径变量。

依赖体系保留完整锁、Wheel 校验、共享下载缓存、ABI 目标和原子安装。调整冲突策略：

- 系统插件继续使用现有宿主依赖保护。
- 第三方 Worker 不再因为其他插件或宿主的包版本不同而统一拒绝。
- 标准库、API v2 名称仍受保护；不同包提供同一导入名仍需拒绝。
- 一个 Worker 仅绑定该插件完整锁对应的依赖目录。
- 不通过批量删除 `sys.modules`、替换已导入模块来模拟隔离。
- 冻结导入器若抢先加载发行物版本，必须在最早阶段建立明确的导入优先级，并以真实产物验证。

**冻结加载是第一道验收门。** 必须验证两个插件使用不同版本纯 Python 包，以及原生 Wheel、资源和包元数据；失败时不能继续宣称依赖隔离成立。

## 4. IPC 协议与身份绑定

Windows 首版采用本地 Named Pipe。管道使用显式访问控制，拒绝远程连接；POSIX 后续采用 Unix Domain Socket。首版不把现有 Dev Channel 或公共 TCP 端口当作插件通道。

连接身份由宿主启动记录确定：

1. 宿主建立本次 Worker 运行会话并创建进程。
2. 读取管道客户端的真实进程身份，核对启动记录及监管归属。
3. 冻结引导进程派生的实际 Worker，同样必须属于对应监管范围。
4. 握手建立协议版本和会话标识。
5. 所有请求使用连接上下文的身份；忽略或拒绝客户端提交的身份字段。

公开端点名可以用于发现，认证秘密不写命令行或共享发现文件。会话 nonce 用于拒绝旧消息，不能代替真实进程身份检查。

协议分开管理：

| 版本 | 首版 |
|---|---|
| `.eclplugin` 归档格式 | 保留 v1 |
| 插件 SDK | v2 |
| Worker IPC | 从 v1 开始 |

消息使用限长 JSON，包括握手、请求、响应、回调、事件、取消和关闭。采用明确的类型、稳定错误码；不接收 Pickle。

建议初始协议上限：

| 项目 | 初始值 |
|---|---:|
| 单帧 | 1 MiB |
| 单会话在途请求 | 16 |
| 排队请求 | 64 |
| 握手等待 | 5 秒 |
| 正常关闭宽限 | 5 秒 |

这些是待实测校准的协议保护参数，不是 CPU/内存硬限额。文件和大型响应采用有界分块，不能在读完整内容后才截断。

接收、发送和业务执行分离；等待插件回调时仍可接收其反向 Host API 请求。同步 SDK 调用不在通信接收线程执行；异步方法可等待同一协议响应，避免双向调用死锁。

## 5. Host API 与回调契约

新建插件专用 Broker，复用现有服务层，不向 Worker 暴露 `FrontendApi` 的完整命令表。

| 能力 | v2 契约 |
|---|---|
| 插件命令 | Worker 保存处理器；宿主登记名称、描述、回调标识 |
| 设置 | 插件自己的 schema 和值；宿主持久化 |
| 事件 | 白名单、结构化载荷；宿主保留事件名禁止第三方伪造 |
| 实例、Java、下载 | DTO 查询或提交宿主任务；返回归属当前插件的任务句柄 |
| 文件 | 授权资源范围内的读、写、删除；分别校验 |
| 网络 | 宿主代理，校验目标、重定向、响应大小 |
| 外部工具 | 宿主创建并绑定插件所有者；输入、查询、停止限制归属 |
| 插件互操作 | 显式命令/服务契约；调用方授权与接收方开放范围同时生效 |

长任务在宿主执行，Worker 不需要持续保持一个 RPC 等待。任务查询、取消和进度订阅都校验所有者。

回调改为可序列化请求与结果：

- **实例兼容**：上下文快照 → 元数据、监听资源描述。
- **联机**：协议载荷、会话句柄 → 结果；发送请求、移除玩家改为 Host API。
- **启动准备**：允许字段快照 → 参数补丁；宿主校验 JVM 参数、环境和工作目录。
- **认证提供方**：只处理其自身认证输入及账户材料；不能读取其他提供方或微软刷新凭据。
- **崩溃分析**：授权范围内的脱敏、限长报告 → 分析补丁。

关键认证失败则启动失败。必要启动钩子失联则本次启动失败；可选增强钩子超时则跳过并记录原因。任何迟到结果都不能继续修改已结束的操作。

## 6. 权限模型、更新与即时撤销

现有 [PermissionManager](../ECL/plugins/permissions.py) 的声明匹配拆成两个独立概念：

- `PermissionDeclaration`：插件请求的能力，含必要/可选标记。
- `PermissionGrant`：用户实际允许的动作、资源范围和有效期。

宿主保存授权，Worker 只能查询自己获准的能力，不能提交授权变更。管理接口只提供给可信主界面。

建议授权记录包含：

- 宿主生成的安装身份；
- 已审阅代码摘要；
- 能力、动作、资源范围；
- 允许/拒绝、授权版本、有效期；
- 审阅时间及变更记录。

文件授权使用宿主资源标识指向实际根目录；插件提交的目标须经过规范化、链接及边界检查。网络授权区分域名、端口、局域网，不沿用简单字符串前缀匹配。

默认行为：

- 未授权时拒绝 Host API。
- 必要权限未获准时不执行插件入口。
- 可选权限未获准时允许运行，由 SDK 返回明确拒绝结果。
- 支持永久授权、会话授权及手动撤销；首版没有按需弹窗。
- 安装依赖的 `allow_network` 与运行时网络授权分别展示、分别保存。

更新规则：

- 安装新版本不会自动授予新增权限。
- 无签名同名包不能被认定为同一作者；更新须重新审查代码来源和权限。
- 用户可在更新审查中确认保留已有授权范围；新增范围仍默认拒绝。
- 目录插件以宿主计算的内容摘要绑定授权；内容改变后启用或重载必须重新审查。
- 旧插件不自动获得 v2 授权，也不通过兼容模式回到主进程。

撤销时递增授权版本：

1. 立即拒绝新请求。
2. 作废尚未执行的请求和相关注册。
3. 请求取消可取消任务。
4. 在下一安全检查点停止。
5. 拒绝旧授权版本的迟到回调与结果。
6. 必要权限撤销时停止 Worker；可选权限撤销时清理相关能力。

已完成的写入不自动回滚；进入原子提交阶段的任务要报告真实最终状态。

授权文件、共享目录虽由宿主管理，**普通同用户 Python 仍可能直接访问它们**。首版不会将这些存储描述为防恶意篡改边界。

## 7. 生命周期、进程树与版本管理

把运行状态与安装状态分开：

```text
安装：已安装 / 待授权 / 待更新确认 / 不支持 / 已移除
运行：已停止 / 启动中 / 加载中 / 已启用 / 停止中 / 异常
```

每次启动使用新的运行会话，旧回调和窗口不得继承。

Windows 使用 Job 管理 Worker、冻结引导后代和插件工具进程。启动时在执行插件代码前完成监管分配，避免“启动后再加入 Job”的派生竞态。Job 创建或分配失败则拒绝运行第三方插件。

停止顺序统一用于禁用、卸载、断线和关闭：

1. 关闭请求入口，作废运行会话。
2. 撤销事件、命令、钩子、UI 和扩展注册。
3. 请求取消插件拥有的可取消任务。
4. 通知 Worker 执行有界关闭。
5. 超时后终止监管进程树。
6. 确认退出并关闭 IPC/监管句柄。
7. 释放版本和依赖目录租约，更新状态。

默认不自动重启。空闲插件不因单次心跳异常直接被杀；结合通信和请求超时判断失联。

不为 Worker 设置首版默认硬 CPU/内存限制。提供低频 CPU、内存、PID、最近错误统计；正常退出和强制终止必须能区分。

现有 `ProcessService` 的插件工具能力改为带所有者的监督入口，并保留现有实例终端展示。Minecraft 是宿主业务服务创建的独立任务，不机械归入 Worker Job。

安装与更新继续保留原子指针、旧版本和事务恢复。首版先保留“更新待重启”的成熟行为，停止整个宿主前先回收 Worker；不同时引入热更新或自动清理旧目录。卸载必须先确认 Worker 停止，再移除安装资格，保留用户数据。

## 8. 前端隔离与管理界面

主窗口第三方 UI 使用宿主定义的组件 schema：文本、按钮、表单、列表、状态、进度和受限动作。限定节点数量、深度、载荷与更新频率，不执行第三方模板表达式、JS、任意 CSS 或动态 Vue 组件。

自定义前端的目标形态是：

- 独立插件窗口中的可信宿主壳；
- 插件内容置于受限、不同源的 frame；
- 仅经绑定 frame 来源和会话的消息通道调用受控 API；
- 禁止直接访问主窗口 DOM、全局 SDK、通用 Tauri 命令；
- 不授予任意顶层导航、弹窗、下载或外部网络；
- 网络请求由授权代理执行；
- 禁用时关闭窗口并撤销通道。

**此路径必须单独验收。** 现有 `createIframeBridge` 只是界面事件桥，不能作为隔离实现；Tauri 的 Isolation Pattern 也不能直接当作插件沙箱。官方文档分别描述了 [Isolation Pattern](https://v2.tauri.app/concept/inter-process-communication/isolation/) 和 [窗口/WebView 能力控制](https://tauri.app/reference/acl/capability/)；具体 PyTauri 发行物是否允许子 frame 绕过桥接，需要真实验证。

如果无法确认原生 IPC 不会暴露给不可信 frame，先停止自定义前端路径，提交验证结果调整方案；不能退回主窗口注入。声明式 UI 部分可独立完成。独立窗口也不承诺每个插件前端拥有单独的 OS 渲染进程或 CPU 限额。

管理界面延续现有页面：

- 列表显示运行状态、PID、资源摘要、最近错误和停止/重启。
- 详情增加权限分类、范围、必要/可选标记及授权记录。
- 安装/更新弹窗增加权限审查。
- 不支持平台和旧 API 插件展示具体原因。
- 错误信息注明“等待超时”“取消已请求”“实际已停止”等真实状态。

系统插件的现有主窗口注入暂保留，但宿主和前端都根据可信来源区分，第三方事件不能进入该路径。

## 9. 模块级改动清单

下表路径均以当前工作区 `C:\Users\Wuchang325\.codex\worktrees\019b\EuoraCraft-Launcher` 为根，仅为拟议改动：

| 模块 | 具体职责 |
|---|---|
| `main.py`、`ECL/cli.py` | 最早启动分流，保持正常 CLI |
| 新增 `ecl_plugin_api/` | 标准库 SDK、契约、客户端、Worker 入口 |
| 新增 `ECL/plugins/runtime/` | Supervisor、Broker、会话、Windows 监管、版本租约 |
| `ECL/plugins/manager/{base,discovery,lifecycle,registry,packages}.py` | 系统与第三方分流；保存远程描述，统一清理 |
| `ECL/plugins/permissions.py` 与新增授权存储模块 | 声明/授权分离、资源范围、撤销版本 |
| `environment_pool.py`、`host_dependencies.py` | 保留安装池；分离宿主与 Worker 冲突策略 |
| `package_preparation.py`、`package_activation.py`、安装事务 | SDK 预检、审阅摘要、更新授权及状态 |
| 各扩展注册表 | 系统回调保持；第三方使用有界 RPC 适配 |
| `ECL/services/processes.py` | 插件工具所有者、监管与展示 |
| `ECL/services/operations.py` 及相关调用边界 | 所有者、撤销检查、真实取消结果 |
| `ECL/api/{plugins,models,registry,windows,bridge}.py` | 管理接口、窗口身份、权限和运行状态 |
| `ECL/host/frontend_events.py` | 白名单 DTO，禁止第三方伪造宿主注入事件 |
| 前端插件页面、类型、API、Bridge、SDK、窗口 | 权限 UI、声明式组件、受限自定义前端 |
| `capabilities/`、必要的宿主配置 | 独立窗口最小能力 |
| `pyproject.toml`、Nuitka 脚本、PyInstaller spec、CI | 收集轻量 SDK；真实冻结验收 |
| `tests/`、前端对应测试、SDK/包格式文档 | 新契约及回归覆盖 |

不预设修改 Core 或 Florolding 子模块。优先在主仓库边界适配；若实测发现必须改其接口，先说明具体原因和影响，再更新方案。

新增主仓库 Python 模块遵守标准文件头、类型、中文多行 docstring、原子写入和日志规范。Host API 操作同样使用中文名称、稳定错误码和脱敏记录；管理 IPC 纳入 `IpcLogPolicy` 覆盖测试。

## 10. 实施顺序与风险验收门

| 阶段 | 内容 | 进入下一阶段的条件 |
|---|---|---|
| 1 | 轻量 Worker 分流、IPC 骨架、Windows Job、最小 SDK | 真正 Nuitka onefile 无窗口/递归/单实例移交；进程树回收；详见第 12、16 节 |
| 2 | Worker 依赖加载 | 两插件不同版本、原生 Wheel、资源与元数据通过 |
| 3 | 授权模型、安装审查、Host API 基础能力 | 无授权拒绝；必要/可选权限与更新行为通过 |
| 4 | 生命周期、命令、事件、设置及各类回调 | 双向嵌套调用、超时、断线与撤销通过 |
| 5 | 前端管理与声明式 UI | 第三方不能进入主窗口执行路径 |
| 6 | 自定义前端受限环境 | DOM、原生 IPC、消息身份、导航与网络隔离通过 |
| 7 | 完整冻结回归、文档、平台提示 | 全部门禁及真实 Windows 验收通过 |

阶段 1、2 或 6 受阻时，先汇报可复现结果和受影响范围。Sidecar 是用户允许评估的备用方向，但具体构建布局仍须提交修订方案，不擅自增加一套运行时。

## 11. 测试、回滚与交付

测试覆盖：

- 同一发行文件启动 Worker；无普通启动流程副作用。
- 每插件 PID、模块缓存与依赖版本分开。
- 原生崩溃、死循环、子进程派生、断线、宿主异常退出。
- 伪造身份、旧会话、畸形帧、超限、并发和背压。
- 所有 Host API 的授权与所有者校验。
- 撤销时的排队请求、迟到回调、可取消及提交中的任务。
- 安装、更新、失败恢复、安全模式、离线与用户数据保留。
- 第三方主窗口脚本阻断和受限前端实际绕过检查。
- Windows/POSIX 分支；首版 POSIX 明确拒绝第三方运行。
- 直接 OS API 测试明确证明首版边界，不能把未阻断报告为安全通过。

按项目要求执行 Ruff 检查与格式、pytest 及集成测试；前端先 `pnpm check`，再 `pnpm build`，最后真实 Windows 启动器/WebView2 验收。记录冷启动、峰值内存、多插件并发和 IPC 耗时，本轮不编造性能目标或结果。

回滚使用 Git 功能边界和原有安装指针，保留插件版本、依赖缓存及用户数据。回退应用后不自动重新启用第三方主进程执行，须显式处理旧版运行风险。

完成的功能阶段通过测试后，按项目要求提交中文 Conventional Commit。远端推送、标签和发布不包含在此次实施批准内。

## 实施批准条件

用户已确认设计选项，并授权将本方案保存及修订到本地文件；尚未明确批准开始实施。

依据 [AGENTS.md 第 0 节](../AGENTS.md) 的要求：“未获用户确认前，禁止进入实施阶段。”详细实施方案仍需用户审阅并明确回复“开始实施”，之后才可编写代码、修改配置、开展原型及执行实施阶段验证。

## 12. 冻结发行物启动与 Windows 进程监管细化

### 12.1 本轮实际核查及证据等级

本轮没有构建或运行发行物。以下区分项目源码事实、第三方源码支持和运行待验证项，不能将后两者合并为“已可用”。

| 依据 | 核实内容 | 不能据此推断的内容 |
|---|---|---|
| `main.py:14`、`ECL/cli.py:318`、`ECL/launcher.py:75` | 当前正常入口会进入 CLI、单实例和宿主初始化 | Worker 分流尚不存在 |
| `.github/workflows/build.yml:278`、`packaging/build-nuitka.ps1:76` | Windows/Linux 使用 Nuitka onefile，CI Python 3.12；Windows ARM 的压缩选项不同 | 本机开发环境与实际发行物版本完全相同 |
| `EuoraCraft-Launcher.spec:213`、`:260` | macOS 是 EXE + COLLECT + BUNDLE；非 macOS spec 为 onefile | macOS 当前发行物是 onefile；它实际上是 onedir `.app` |
| `ECL/foundation/runtime.py:50` | 区分程序目录和资源目录；Nuitka 环境只恢复了原始目录 | 已提供可靠的原始发行文件名解析器 |
| `tests/test_packaged_plugin_directories.py:95` | 现有冻结测试断言插件与宿主 PID 相同，并覆盖外部原生 Wheel | 已验证 Worker 模式或 Job |
| 本机完整仓库 `.venv/Lib/site-packages/nuitka/` | 可读到 Nuitka 4.2.2 的 C 引导源码 | CI 实际构建必然采用 4.2.2 |
| 本机 PyInstaller 6.22.3、PyTauri/pytauri-wheel 0.8.0 元数据 | 可确认调研所读依赖版本；PyTauri 扩展文件为 cp311 Windows x64 | 实际 Python 3.12 发行包的依赖、Rust 链和 WebView2 已与其一致 |

`pyproject.toml` 对 Nuitka/PyInstaller/PyTauri 使用版本下限；本轮不能恢复某份发行产物的精确构建依赖。实施冻结测试时必须记录 Python、构建器、SDK、PyTauri wheel、产物摘要和 WebView2 Runtime 的实际版本，再决定是否需要固定构建依赖。版本漂移是验收风险，不在本轮修改配置。

第三方源码定位：本机 Nuitka `build/static_src/OnefileBootstrap.c:1052,1377,1407`、`CompiledCodeHelpers.c:2021,2070`、`MainProgram.c:1789`；PyInstaller 上游 v6.22.3 `bootloader/src/pyi_main.c:202,263`、`pyi_utils_win32.c:573`。上游链接见第 17 节。

### 12.2 进程拓扑和执行文件解析

用 H 表示宿主，B 表示宿主创建的启动根进程，W 表示执行受信 Worker 引导以及随后插件源码的进程。**不能要求 B PID 等于 W PID，也不能固定要求每种发行物都是两层。**

| 组合 | 源码支持的拓扑 | 拟议启动方式 |
|---|---|---|
| 开发环境 | H → 当前 Python 进程 W，B=W | 绝对 Python 路径 + 项目 `main.py` + 内部 Worker 标志；不使用 shell |
| Nuitka Windows onefile | H → B → W；DLL 模式可能重新执行外层 EXE并在 W 加载解包 DLL | 使用经确认的原始发行 EXE，建立独立引导环境 |
| PyInstaller onefile 独立启动 | H → B 解包 → W 初始化 Python | 原始 EXE + `PYINSTALLER_RESET_ENVIRONMENT=1`，初始基线不共享宿主临时目录 |
| PyInstaller onefile 复用环境 | 可能直接作为已解包环境的子进程 W | 仅作为后续优化候选；需验证生命周期和兼容版本，不为首版默认 |
| PyInstaller onedir / macOS `.app` | 启动应用内部可执行文件；实际层数由对应平台验证 | macOS 后续直接启动 `Contents/MacOS` 文件，不使用 `open -a`；首版不开放第三方执行 |

原始文件解析拟增加独立的启动描述 `ExecutableLaunchSpec`，区分 `outer_executable`、`resource_directory`、`builder_kind`、`builder_version`。在宿主启动时建立并核实，后续不接受插件覆盖：

- PyInstaller 使用其定义的 `sys.executable`；`_MEIPASS` 只用于资源，不是执行文件。
- 本机 Nuitka 4.2.2 的 `CompiledCodeHelpers.c` 提供 `__nuitka_binary_exe`；DLL 模式注释明确该值指向外层 EXE。它是构建器内部细节，不是跨版本稳定契约。
- Nuitka 同时核查原始 argv、`NUITKA_ONEFILE_DIRECTORY`、原生进程映像路径以及构建元数据；不能仅拼接目录与编译前的 `python.exe` 名称。若候选不唯一、不存在或位于临时负载且无法证实，不启动 Worker并返回 `WORKER_EXECUTABLE_UNRESOLVED`。
- `sys.executable` 是否指向发行文件必须由冻结测试确认，不以开发环境输出代替。

首版保守选择重新进入原始发行文件。它复用发行物自带的 Python，但可能再次解包；不承诺负载临时文件、私有内存或初始化成本都能共享。

### 12.3 引导环境和插件代码执行闸门

插件子进程通过正式无头角色 `--headless --role plugin` 启动，复用同一 ECL 发行物。命令行可以有公开端点名，不携带凭据，不携带可自行决定的插件身份或代码路径。原内部命令 `--internal-plugin-worker` 已移除并拒绝；最新入口整合见 `plugin-headless-process-integration-design.md`。

1. 重排 `main.py`：标准库分流先于任何 `ECL` 导入。
2. 启动根进程的工作目录设为受信目录；不得用插件代码目录作为启动 cwd。清理 `PYTHONPATH`、用户站点注入等开发入口，不调用插件目录的 `site.addsitedir()`，不执行 Wheel 的 `.pth`。开发环境需验证隔离启动参数及禁用site初始化的方式，并由受信入口显式加入SDK所在项目路径；不能以环境清理推断系统sitecustomize已经禁用。冻结环境按构建器实际初始化路径核查。
3. Nuitka 从新进程环境移除旧的 onefile 角色信息（已读源码中的 `NUITKA_ONEFILE_PARENT/START/TIME_US/RANDOM/DIRECTORY/ORIGINAL_ARGV0`），由新引导设置自身状态；确切列表按实际构建器复核。
4. PyInstaller 独立模式使用公开 reset 开关，不手工伪造 `_PYI_PARENT_PROCESS_LEVEL`。外部工具子进程的动态库环境净化与 Worker 复用环境分开处理。
5. W 只连接 IPC，核对宿主身份，发送 `hello`；尚不加入插件依赖路径，也不导入插件入口、装饰器或自定义前端。
6. 宿主确认 W 的系统身份、Job、SDK/协议、授权、代码摘要和依赖目录后，发送一次性 `load_authorized`。这才允许受信引导配置依赖并执行插件入口。
7. 缺少闸门、握手超时、版本不符或已撤销时，W 退出；不得进入正常启动器，也不得自行扫描并执行插件。

该闸门阻止正常实现中的提前执行；不是恶意同用户进程的防篡改沙箱。SDK、引导模块和必要依赖必须能从受信发行负载中取得，不从插件目录解析。

### 12.4 Job 分配顺序：不能先启动再补入

现有 `ProcessService.spawn()` 及 Core `InstancesManager.create_instance()` 不保留可供本方案控制的挂起主线程句柄，不能直接用于建立此启动保证。拟新增 Windows 原生创建适配器：

```text
创建本插件 Job J：KILL_ON_JOB_CLOSE，禁止 breakaway，句柄不继承
  → 建立命名管道、启动记录和必要输出句柄
  → CreateProcessW(原始 EXE, CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT, ...)
  → 获得 B 的进程/主线程句柄及创建时间
  → AssignProcessToJobObject(J, B)
  → IsProcessInJob(B, J) 必须为真
  → 原子检查启动会话尚有效
  → ResumeThread(B)
  → B 的正常 CreateProcess 后代从创建时继承 J
  → W 建连；核实其真实身份和 J 成员资格
  → load_authorized
  → 执行插件源码
```

`CREATE_SUSPENDED` 使主线程在恢复前不运行；Job 默认使正常 `CreateProcess` 后代继承。读到的 Nuitka/PyInstaller Windows 引导源码均通过 `CreateProcessW` 创建实际子进程，所读调用未指定 breakaway。这为上述顺序提供源码依据，但**实际编译分支、嵌套 Job 和发行启动环境仍须测试**。

- Job 不设置 `BREAKAWAY_OK` 或 `SILENT_BREAKAWAY_OK`，不把 Job 句柄传给 B/W，否则宿主退出后可能仍有最后句柄未关闭。
- 默认不继承句柄；若需 stdout/stderr，使用 `STARTUPINFOEX` 的句柄白名单，只传专用输出句柄。冻结引导二次继承仅能得到这份受限集合。
- 任一步在 Resume 前失败，终止并等待仍挂起的 B，关闭句柄，保留失败原因；绝不恢复无监管 B。
- Resume 后发现 W 不在 J 或身份错误，拒绝 `load_authorized` 并终止 J。
- 宿主已经处于某个 Job 时，验证嵌套 Job 分配；不使用 breakaway 逃出限制，不能分配则拒绝插件启动。

### 12.5 如何认定实际 Worker

宿主不能信任 `hello.pid`、名称、父 PID快照或第一个后代。命名管道用 `GetNamedPipeClientProcessId` 获得真实客户端 PID，打开进程并保留句柄；确认存活、创建时间、映像及 `IsProcessInJob(process, J)`。对一个运行会话仅允许一个实际 W 完成受信引导握手。记录 B 与 W 分别用于启动诊断和运行展示。

握手在执行第三方代码前完成，之后禁止以该身份建立第二条 Host API 连接；意外重连触发停止并手动重启，首版不实现会话恢复。PID 重用不能继承已绑定句柄和创建时间。Worker也核对命名管道服务器 PID是否为启动时指定的宿主，拒绝错误服务器。

Job 成员资格是会话绑定条件，不代表恶意同用户代码无法注入或攻击该进程。派生工具继承 J；由宿主代理创建的工具也先挂起加入 J，再恢复。已经获准由宿主启动的 Minecraft 不属于插件 J。

### 12.6 正常退出、异常退出及单文件临时目录

正常退出先要求 W 清理，允许 B 完成其引导清理，再确认 J 中无活跃进程。异常退出/超时调用 `TerminateJobObject`；不能仅等待 B PID或依赖 Job 通知，须复核实际成员。日志分别记录“请求停止”和“已确认停止”。

宿主异常退出时以不继承的 Job 最后句柄关闭触发回收，但强杀 onefile 引导可能留下临时解包文件。本方案不承诺强杀时零残留，也不扫描/删除不属于本次启动的系统临时目录。后续如需清理，只依据宿主持久化的可信启动记录和路径边界处理。

首版不支持 Worker自行使用 `multiprocessing` 创建解释器工作进程。自定义分流不等于 `freeze_support()`；插件依赖若必须依赖该模式，应明确报不支持并列入后续契约，而不是尝试普通 `-c/-m` 自执行。通过 Host API 的工具进程仍支持。

### 12.7 冻结专项测试及阻塞条件

以下为原设计的冻结验收载荷清单；已经执行的子集见第 18 节，其余仍待对应实施切片：

| 测试 | 观察点与通过条件 |
|---|---|
| F01：无插件引导 | 仅 `hello`；无窗口、无单实例通知、无宿主服务、无主配置/日志写入 |
| F02：闸门前挂起 | 在 `load_authorized` 前插件模块首条语句的标记不存在；强制取消仍无标记 |
| F03：Job 提前归属 | B 恢复前在 J；插件模块首条语句采集 W PID/Job；宿主以真实客户端身份复核 W在同一 J |
| F04：失败注入 | 创建/分配/确认/恢复/握手任一步失败，插件入口均不执行，所有已创建句柄及进程收敛 |
| F05：双层引导与环境 | 独立解包、包含空格/中文路径、改名、从其他 cwd启动、宿主已在 Job、控制台关闭版均通过 |
| F06：派生与回收 | 正常子进程、孙进程、尝试 breakaway；停止后 J 中无活跃进程；记录 W/B不同退出顺序 |
| F07：宿主异常终止 | 单独终止测试宿主，已监管进程被回收；临时残留独立记录，不误报失败回滚已完成 |
| F08：依赖组合 | 两插件不同包版本、原生 Wheel、资源、metadata；宿主和系统插件导入不受污染 |

Windows x64 Nuitka真实发行构建是首版必须项；Windows PyInstaller onefile为独立兼容研究测试，不因此承诺它是当前官方发行路径；macOS `.app`、Linux/ARM保留后续平台门。无法确保 F02/F03/F04 或执行文件解析时停止后续运行集成，不用绕过 Job 的临时方案继续。

## 13. 首版完整 Host API 契约目录

本节是拟议协议目录，不是已实现能力列表。未列方法一律返回 `PLUGIN_API_METHOD_NOT_FOUND`；不得注册通用 `frontend.invoke`、`getattr`、Python eval或任意服务调用入口。首版只开放下列服务范围，不镜像全部前端 API。SDK便利函数和装饰器最终必须归约到本目录的方法。

### 13.1 当前服务依据及必须改造的缺口

| 服务 | 已读源码 | 适配约束 |
|---|---|---|
| 任务 | `ECL/services/operations.py:94,157,207,286,314` | 无插件 owner；`finalize` 在全局任务锁内执行 action；需增加执行来源/授权上下文和独立提交状态 |
| 自定义下载 | `ECL/services/custom_downloads.py:272,347,378` | 有暂存文件和 finalize；无插件权限及重定向授权边界；不能原样传任意绝对路径 |
| Minecraft 安装 | `ECL/services/game/install.py:131,211,287` | 独立 asyncio任务；取消用 `abandon_on_cancel`，不能把等待方结束当作线程停止；需插件任务适配 |
| Minecraft 启动 | `ECL/services/game/launch.py:848,1133,1234` | `_launch_cancel_event` 是全局单任务；必须绑定来源，不能由插件取消用户启动 |
| Java | `ECL/java/manager.py:147,252,319,342,366` | 有内部计划和合并任务；跨来源同目标任务不能因相同 plan而赋予插件取消权 |
| 联机 | `ECL/services/connector.py:435,656,753,850` | 全应用单房间；同步建房/加入更新共享状态，未提供插件会话所有者或一致取消上下文 |
| 文件 | `ECL/api/files.py:311,328,359` | 现有读取处理属于可信前端接口；v2资源边界须独立建立，不直接调用任意路径 IPC |
| 回调 | `ECL/plugins/{instance_compat,connector,launch_hooks,auth_providers,crash_extensions}.py` | Python对象/闭包/同步回调须转换为数据及有界 RPC |

### 13.2 公共信封、字段及所有者

请求字段为 `protocol_version:1, kind:"request", request_id:string, method:string, params:object, timeout_ms:int, mutation_key?:string`。`request_id` 在连接内唯一、最多64字符；`mutation_key`在有副作用方法必填、最多64字符，位于信封而不是params。`params` 拒绝未知字段。`timeout_ms` 仅能缩短该方法上限。发送的 `plugin_id/owner_id/grant_epoch` 不能决定身份或授权；宿主在入队时附加自己的上下文。

成功响应为 `request_id, ok:true, data:<该方法DTO>`；失败为 `request_id, ok:false, error:{code:string,message:string,retryable:bool}, operation_ref?:string`。不得透出 Python异常对象、完整请求或含凭据堆栈。重试标记仅提示可重试条件，不自动重放有副作用请求。

所有对象标识由宿主生成，绑定 `(installation_id, content_digest, run_id)` 及资源类别。对象所有者与“获准访问共享实例/Java”是两个条件：共享资源不归插件所有，必须另有资源授权；插件拥有 task也不表示其最终写入资源永远获准。

| 所有者标记 | 语义 |
|---|---|
| O | 当前运行自己的注册、写入会话、进程、UI或任务；禁止跨插件/跨运行句柄 |
| S | 宿主管理的共享资源，按用户授权过滤；`instance_ref/root_ref/runtime_ref`不授权即拒绝 |
| L | 插件成功获取的独占业务租约，如游戏启动准备、联机房间；用户来源租约不可继承 |
| K | 正在执行的宿主回调授予的短期能力，绑定 invocation、资源、授权版本和截止时间 |
| C | 内部控制，只能自查询或清理自己的已有资源；不授予一般业务能力 |

O类任务的可见性限定当前 run；重启后的旧任务由可信插件管理页显示，不自动把旧任务控制权交给新 run。清理操作（unsubscribe、abort、stop、close、cancel、unregister）可在对应业务授权撤销后处理自己的已有句柄；只用于减少资源，不允许恢复或创建资源。

默认拒绝针对业务能力；握手、读取自身授权和主动释放自身资源属于必要控制协议。能力名是 v2新契约，不能将旧清单 scope自动转换为授权。

### 13.3 DTO定义与边界

下列数据全部为 JSON，不含 Callable、Path实例或服务对象；表中 `?` 表示可省略，`null` 只在显式允许时接受。名称长度1—64，标题最多120，说明最多1000；一般业务载荷最多64 KiB、嵌套深度16，协议单帧仍不超过1 MiB。

| DTO | 精确字段或约束 |
|---|---|
| `FileRef` | `{root_ref:32位小写hex,relative_path:string}`；P09 A 只允许当前安装固定私有根，平铺 ASCII 文件名至多120字节，空名称仅用于根 stat/list；拒绝绝对路径、父级、ADS、设备名、尾点和链接 |
| `FileView` | `{file_ref:FileRef,name:string,kind:"file"\|"directory",size:int,modified_ms:int,etag:string}`；etag绑定实际身份和完整内容摘要，不作为作者签名；纳秒仅用于内部版本计算 |
| `Page<T>` | `{items:T[],next_cursor:string\|null}`；limit 1—100，游标绑定查询、过滤范围及运行会话 |
| `TaskReceipt` | `{operation_ref:string,state:"queued"\|"running"}`；内部 Core taskId/OperationManager ID不作为授权依据 |
| `TaskView` | `{operation_ref,kind,state,percent:0..100,stage,message,can_cancel,cancellation_requested,effect:"none"\|"staged"\|"committing"\|"committed"\|"partial"\|"unknown",result:已登记结果DTO\|null,error_code:string\|null}`；不能返回其他资源路径或完整日志 |
| `ReleaseResult` | `{released:bool}`；重复释放返回false或已释放，不扩展控制范围 |
| `InstanceView` | `{instance_ref,version_id,alias,loader,game_version,profile:{description,favorite,tags},profile_etag:string,running_count:int}`；本轮只读实例接口不返回文件路径，etag 为公开资料摘要，不承诺单调递增或写入并发令牌 |
| `LaunchOptions` | `{memory_mb?:512..65536,width?:320..16384,height?:240..4320,fullscreen?:bool,runtime_ref?:string}`；不接收原始命令、wrapper、pre/post脚本、环境变量或令牌 |
| `GameRunView` | `{game_run_ref,instance_ref,pid:int,running:bool,exit_code:int\|null}`；所属插件启动结果与共享运行列表分开过滤 |
| `JavaView` | `{runtime_ref,vendor,major_version,full_version,runtime_kind:"JRE"\|"JDK",architecture,origin:"system"\|"manual"\|"managed",is_enabled,validation_status}`；不返回安装文件清单、全路径或其他实例引用 |
| `JavaPackageView` | `{package_ref,vendor,major_version,full_version,runtime_kind,platform,architecture,download_bytes,checksum}`；URL与安装目录由宿主掌握 |
| `SettingDefinition` | `{key,type:"bool"\|"string"\|"number"\|"select",default:对应JSON类型,description,options?:有界枚举}`；单插件最多64项，禁止可执行 validator或任意schema引用 |
| `CommandDefinition` | `{name,description,input_fields:FieldSpec[],output_fields:FieldSpec[],callback_ref,exported:bool}`；FieldSpec只支持明确的基础类型、必要性、长度/数值范围和枚举，不支持任意regex或远端 `$ref` |
| `UiNode` | 有界判别联合：`text{text}`、`button{label,command_ref,args}`、`field{setting_key,label}`、`list{items:UiNode[]}`、`progress{operation_ref}`、`group{children:UiNode[]}`；最多256节点/8层；纯文本，不执行表达式、HTML或任意CSS |
| `AssetRef` | `{relative_path:string,sha256:string}`，仅当前审阅代码快照内的已校验资源，不访问 `ECL_data`或任意绝对路径 |
| `AccountPublic` | `{account_ref,alias,uuid,type,is_current,skin_asset_ref?:string}`；不含email、账户文件、token或提供方不透明data |
| `ConnectorView` | `{session_ref,mode:"host"\|"guest",state,player_count,local_endpoint?:{host,port},room_code?:string}`；仅自己的L或明确授予的读取范围；房间码只在有`connector.room_code.read`授权时返回 |
| `PermissionView` | `{capability,required,decision:"allow"\|"deny",resource_refs:string[],expires_at:number\|null,grant_epoch:int}`；不暴露其他插件授权 |

如现有服务返回多余字段，Broker显式构造上述 DTO，禁止把整个服务结果、`model_dump()`或裸配置直接透传。

### 13.4 错误码、超时与取消的公共规则

所有方法均适用公共错误 E：`PLUGIN_API_METHOD_NOT_FOUND`、`PLUGIN_INVALID_REQUEST`、`PLUGIN_PERMISSION_DENIED`、`PLUGIN_PERMISSION_REVOKED`、`PLUGIN_OWNER_MISMATCH`、`PLUGIN_RESOURCE_OUT_OF_SCOPE`、`PLUGIN_CONTENT_CHANGED`、`PLUGIN_SESSION_EXPIRED`、`PLUGIN_DEADLINE_EXCEEDED`、`PLUGIN_HOST_BUSY`、`PLUGIN_RESOURCE_NOT_FOUND`、`PLUGIN_HOST_CLOSED`。协议畸形/超限分别为 `PLUGIN_PROTOCOL_ERROR` / `PLUGIN_MESSAGE_TOO_LARGE`，可关闭连接。

入队前已撤销返回 REVOKED；未获准返回 DENIED；入队后授权版本改变返回 REVOKED并要求重新提交，不自动沿用已排队请求。资源所有者不符时不给出对方身份。服务预期错误经固定映射返回，不把第三方异常文本当稳定错误码。

| 时限/取消标记 | 规则 |
|---|---|
| Q5/Q30 | 只读查询5/30秒；取消结束等待并作废结果；后台若未停止，仍跟踪，不杀宿主线程 |
| W5 | 修改/登记5秒；提交闸门前可取消；闸门后等待方超时只能返回 `PLUGIN_OUTCOME_PENDING`及操作句柄，不伪报回滚；需用operations.get查看真实结果 |
| T5/budget | 5秒内提交任务并返回TaskReceipt；执行预算见各行；预算到达请求取消、记录pending cleanup，不等同强杀宿主线程 |
| R30 | 网络30秒总预算；可终止宿主HTTP读取；已经发出的远端写请求不可回滚，结果可能unknown |
| CL5 | 释放/停止请求5秒；资源尚未收敛返回receipt或`released:false`及状态，不声明已停止 |
| CBn | 宿主→Worker回调最多n秒；可作废回调结果，若插件计算仍在执行，按超时策略停止其Worker |

所有 task执行预算从实际开始执行计时，排队预算30秒；参数上限为设计初始值，需实测校准。首次实现先固定，不引入用户资源策略编辑器。网络、文件和任务阶段必须有检查点，清理状态由执行方确认。

有副作用请求必须带连接内 `mutation_key:string`。宿主有限缓存回执并拒绝重复键配不同参数；只去重本run的提交，不承诺崩溃后的“恰好一次”。超时后查询回执，不无条件重发。原服务产生副作用而没有可查询回执时，必须先完成适配才开放对应方法。

### 13.5 Worker → Host：控制、命令、设置和事件

以下为穷尽目录的第一组；每行的公共错误E均适用，错误列只列额外错误。`[]`及DTO中的元素都有上述上限。

| 方法 | 请求params | 成功data | 必须能力/资源 | 额外错误 | 时限；owner |
|---|---|---|---|---|---|
| `runtime.info` | `{}` | `{api_version:2,protocol_version:1,launcher_version,platform,python_abi,run_state}` | C自查询 | `PLUGIN_RUNTIME_UNSUPPORTED` | Q5；C |
| `permissions.list` | `{}` | `{items:PermissionView[],grant_epoch}` | C自查询 | — | Q5；C |
| `logs.write` | `{level:"debug"\|"info"\|"warning"\|"error",message:string<=4096}` | `{accepted:bool}` | C，限频/脱敏 | `PLUGIN_RATE_LIMITED` | Q5；O |
| `commands.register` | `CommandDefinition` | `{registration_ref,command_ref}` | `commands.register`，自己名称 | `PLUGIN_REGISTRATION_CONFLICT`,`PLUGIN_SCHEMA_INVALID` | W5；O |
| `commands.unregister` | `{registration_ref}` | `ReleaseResult` | C，自己注册 | — | CL5；O |
| `interop.list` | `{target_plugin_ref?:string,cursor?:string,limit?:int}` | `Page<{target_plugin_ref,command_ref,name,input_fields,output_fields}>` | `interop.read`，用户允许的目标 | — | Q5；S |
| `commands.call` | `{command_ref,args:object}` | `{value:登记的输出DTO}` | `commands.execute`；跨插件再需`interop.execute`，接收方exported | `PLUGIN_COMMAND_FAILED`,`PLUGIN_CALLBACK_TIMEOUT`,`PLUGIN_TARGET_DISABLED` | CB30；S+K |
| `settings.define` | `{definitions:SettingDefinition[]}` | `{schema_revision:int}` | `settings.write`，自己的keys | `PLUGIN_SCHEMA_INVALID`,`PLUGIN_SETTING_TYPE_MISMATCH` | W5；O |
| `settings.get` | `{keys:string[]}` | `{values:object,revision:int}` | `settings.read`，自己的keys | `PLUGIN_SETTING_NOT_DEFINED` | Q5；O |
| `settings.set` | `{values:object,expected_revision:int}` | `{revision:int}` | `settings.write`，自己的keys | `PLUGIN_SETTING_TYPE_MISMATCH`,`PLUGIN_REVISION_CONFLICT`,`PLUGIN_STORAGE_FAILED` | W5；O |
| `events.subscribe` | `{event:string,callback_ref:string}` | `{subscription_ref}` | `events.subscribe`，指定事件及关联资源 | `PLUGIN_EVENT_NOT_EXPOSED` | W5；O+S |
| `events.unsubscribe` | `{subscription_ref}` | `ReleaseResult` | C，自己订阅 | — | CL5；O |
| `events.publish` | `{name:string,payload:object}` | `{accepted:bool}` | `events.emit`，自己的命名空间；跨插件需互操作授权 | `PLUGIN_EVENT_RESERVED` | W5；O |

宿主开放的订阅目录首版为：自己的`settings.changed`、`task.progress/task.finished`、`ui.action/window.closed`，以及按资源过滤的`instance.started/exited`、`connector.changed`。payload由对应DTO组成；不得订阅通用配置广播、完整账户变化、启动命令、原始日志或任意EventBus事件。事件顺序按订阅递增sequence；进度合并采样，最终状态不能静默丢弃，超额可靠事件导致明确断线而非无限队列。

### 13.6 文件、网络与下载

文件方法当前按 [P09 A](plugin-private-files-implementation-plan.md) 实施，仅限 Windows x64 本地 NTFS。filesystem.read/write 的声明和决定 resource_refs 必须为空，只控制当前安装固定私有空间；写不隐含读。下表的 delete、network、downloads 仍为后续设计，当前返回 PLUGIN_API_METHOD_NOT_FOUND。

| 方法 | 请求params | 成功data | 必须能力/资源 | 额外错误 | 时限；owner |
|---|---|---|---|---|---|
| `files.stat` | `{target:FileRef}` | `FileView` | `filesystem.read`，root/相对范围 | `PLUGIN_FILE_INVALID` | Q5；S |
| `files.list` | `{directory:FileRef,cursor?:string,limit?:int:1..100}` | `Page<FileView>` | `filesystem.read`，仅私有根 | `PLUGIN_SCHEMA_INVALID` | Q5；S |
| `files.read` | `{target:FileRef,offset:int>=0,count:int:1..32768,expected_etag?:sha256}` | `{data_b64:string,eof:bool,etag:string}` | `filesystem.read`，普通文件至多8 MiB | `PLUGIN_FILE_CHANGED`,`PLUGIN_FILE_IO_FAILED` | Q30；S |
| `files.write_begin` | `{target:FileRef,total_bytes:int:0..8388608,overwrite:严格false}` | `{write_ref,max_chunk_bytes:32768}` | `filesystem.write`，只新建确切目标 | `PLUGIN_FILE_EXISTS`,`PLUGIN_FILE_BUSY`,`PLUGIN_STORAGE_QUOTA_EXCEEDED` | W5；O+S |
| `files.write_chunk` | `{write_ref,offset:int,data_b64:string}` | `{received_bytes:int}` | 原write授权仍有效；顺序offset | `PLUGIN_WRITE_SEQUENCE_INVALID`,`PLUGIN_WRITE_SIZE_EXCEEDED` | W5；O |
| `files.write_commit` | `{write_ref,sha256:string}` | `TaskReceipt`，state固定queued；最终结果为FileView或null | 原始写lease控制执行，独立原始读lease控制结果；根/源身份及完整摘要校验 | `PLUGIN_FILE_CHANGED`,`PLUGIN_HASH_MISMATCH`,`PLUGIN_STORAGE_FAILED` | T5/30秒；O+S |
| `files.write_abort` | `{write_ref}` | `ReleaseResult` | C，自己的暂存 | — | CL5；O |
| `files.delete` | `{target:FileRef,expected_etag:string}` | `TaskReceipt`；最终`{deleted:true}` | `filesystem.delete`，单个文件 | `PLUGIN_FILE_CHANGED`,`PLUGIN_NOT_FILE`,`PLUGIN_STORAGE_FAILED` | T5/30秒；O+S |
| `network.request` | `{method:"GET"\|"HEAD",url:string<=4096字节,headers?:白名单对象}` | `{status_code,url,headers:白名单对象,body_b64}`，GET实体至多32KiB | `network.read`；精确协议/DNS域名/端口，仅普通公网 | `PLUGIN_NETWORK_FAILED`,`PLUGIN_REDIRECT_DENIED`,`PLUGIN_RESPONSE_TOO_LARGE`,`PLUGIN_NETWORK_TARGET_DENIED` | R30；S |
| `downloads.submit` | `{url,target:FileRef,overwrite:bool,expected_sha256?:string}` | `TaskReceipt`；最终`FileView` | `network.read`各目标 + `filesystem.write`目标 | `PLUGIN_DOWNLOAD_FAILED`,`PLUGIN_HASH_MISMATCH`,`PLUGIN_FILE_EXISTS`,`PLUGIN_FILE_BUSY` | T5/1800秒；O+S |

写入会话60秒空闲过期，未commit只触及自己的暂存文件；分块最大256KiB，offset必须等于已接收字节，commit须等于声明total_bytes。delete首版仅文件，通过同卷可恢复暂存后提交；不递归删除目录。不开放通用shell文件操作或绝对路径API。

`network.request`当前只开放GET/HEAD，使用独立HTTP/1上下文。GET完整实体至多32KiB，每次底层读最多8KiB、每跳原始HTTP收包最多256KiB；超限失败，不交付截断内容。HEAD及无体状态不读实体。只返回Content-Type/Content-Length/ETag/Last-Modified/Cache-Control，总4096字节。不解压、不共享Cookie或账户、不继承环境代理/证书设置。请求头白名单、准确站点键和稳定错误见 [现行协议](eclplugin-package-format.md#只读网络请求)。network.write、network.lan及下载仍未开放，不保留原拟写方法或body字段。

重定向逐跳重新校验scheme、域名、端口与DNS/IP，最多5跳；禁止HTTPS降级，跨站永久移除认证头。固定已准入数字IP连接并核实peer，原域名始终用于TLS SNI与证书校验；任何禁止解析地址拒绝整跳。全部动作使用原租约和总截止，接受后实际I/O不持授权锁。GET/HEAD发送也不承诺远端零副作用；撤销只能停止后续传输和结果交付。DNS等待取消仍保留实际future与名额，迟到结果不继续连接；关闭失败与阻塞连接/TLS如实保持pending。

下载适配不能仅用前后两个权限检查包裹现有Downloader；必须能校验每次连接/重定向，并在暂存写入和最终提交检查授权。若现有下载库不暴露这些控制点，对应API保持关闭并列为阶段阻塞；不假设它已经具备这些能力。

### 13.7 实例、启动、Java、账户和公开配置

| 方法 | 请求params | 成功data | 必须能力/资源 | 额外错误 | 时限；owner |
|---|---|---|---|---|---|
| `resources.list` | `{capability:"instances.read",cursor?:string,limit?:int}` | `Page<ResourceView>`，仅本运行获准目录/实例的引用 | `instances.read`，至少一个有效用户选择 | `PLUGIN_RESOURCE_UNAVAILABLE`,`PLUGIN_CURSOR_INVALID` | Q5；S |
| `instances.scan` | `{root_ref,force?:bool,cursor?:string,limit?:int}` | `Page<InstanceView>` | `instances.read`，用户批准的游戏根；单实例授权不能扫描父目录 | `PLUGIN_INSTANCE_SCAN_FAILED`,`PLUGIN_CURSOR_INVALID` | Q30；S |
| `instances.get` | `{instance_ref}` | `InstanceView` | `instances.read`，该实例 | `PLUGIN_INSTANCE_INVALID` | Q5；S |
| `instances.profile_patch` | `{instance_ref,patch:{alias?:string,description?:string,favorite?:bool,tags?:string[]},expected_revision:int}` | `{profile,revision:int}` | `instances.write`，实例及上述字段 | `PLUGIN_REVISION_CONFLICT`,`PLUGIN_INSTANCE_UPDATE_FAILED` | W5；S |
| `game.catalog` | `{type?:"release"\|"snapshot"}` | `Page<{version_id,type,release_time}>` | `game.catalog.read`，宿主固定元数据源 | `PLUGIN_GAME_CATALOG_FAILED` | Q30；S |
| `game.loaders` | `{game_version,loader:"fabric"\|"forge"\|"neoforge"\|"quilt"}` | `Page<{loader_version,stable:bool}>` | `game.catalog.read`，固定源 | `PLUGIN_LOADER_NOT_SUPPORTED` | Q30；S |
| `game.install` | `{root_ref,version_id,version_name?,loader?,loader_version?}` | `TaskReceipt`；最终`InstanceView` | `instances.install`，根与实例目标；明确包含宿主固定下载源行为 | `PLUGIN_INSTALL_FAILED`,`PLUGIN_INSTALL_BUSY`,`PLUGIN_LOADER_NOT_SUPPORTED` | T5/1800秒；O+S |
| `game.launch` | `{instance_ref,options?:LaunchOptions}` | `TaskReceipt`；最终`GameRunView` | `instances.launch`，该实例；runtime额外需`java.use` | `PLUGIN_LAUNCH_BUSY`,`PLUGIN_LAUNCH_FAILED`,`PLUGIN_ACCOUNT_REQUIRED`,`PLUGIN_REQUIRED_HOOK_FAILED` | T5/180秒；O+S+L |
| `game.running` | `{instance_ref}` | `{items:GameRunView[]}` | `instances.read`，该实例 | — | Q5；S |
| `game.stop` | `{game_run_ref}` | `TaskReceipt`；最终`{stopped:true,exit_code:int\|null}` | `instances.stop`，该实例，且本run创建；不授权停止用户来源游戏 | `PLUGIN_GAME_STOP_FAILED` | T5/30秒；O+S |
| `java.inventory` | `{}` | `{items:JavaView[]}` | `java.read`，获准可见运行时 | `PLUGIN_JAVA_QUERY_FAILED` | Q30；S |
| `java.validate` | `{runtime_ref,required_major?:int}` | `JavaView` | `java.use`，该运行时 | `PLUGIN_JAVA_INVALID`,`PLUGIN_JAVA_VERSION_MISMATCH` | Q30；S |
| `java.catalog` | `{major_version?:1..99,runtime_kind?:"JRE"\|"JDK"}` | `{recommended_major,items:JavaPackageView[]}` | `java.catalog.read`，宿主固定发行源 | `PLUGIN_JAVA_CATALOG_FAILED` | Q30；S |
| `java.install_plan` | `{package_ref}` | `{plan_ref,package:JavaPackageView,expires_at,estimated_free_bytes}` | `java.install`，指定主版本/包 | `PLUGIN_JAVA_PLAN_FAILED` | W5；O+S |
| `java.install` | `{plan_ref}` | `TaskReceipt`；最终`JavaView` | `java.install`，计划范围 | `PLUGIN_JAVA_PLAN_EXPIRED`,`PLUGIN_JAVA_INSTALL_FAILED`,`PLUGIN_HOST_BUSY` | T5/900秒；O+S |
| `accounts.public_list` | `{}` | `{items:AccountPublic[]}` | `accounts.public.read`，用户选择账户范围 | `PLUGIN_ACCOUNT_QUERY_FAILED` | Q5；S |
| `accounts.public_current` | `{}` | `AccountPublic\|null` | `accounts.public.read`，当前账户也须在授权范围内 | — | Q5；S |
| `launcher.config_get` | `{keys:string[]}` | `{values:object}` | `launcher.config.read`，明确字段集合 | `PLUGIN_CONFIG_KEY_NOT_EXPOSED` | Q5；S |

公开配置首版只提供归一化的 `language`、`theme_mode`、`accent_color`、`default_memory_mb`，由宿主从当前配置映射，不接受任意配置节。插件写入自己的settings；不开放通用启动器配置写入、账户选择/删除、Java全局删除/清理/绑定、游戏卸载或实例任意JSON覆写。所需新增业务能力以后单独评审。

宿主固定目录/下载源能力（game/Java catalog/install）明确包含该服务的有限网络行为，但不等于授予任意URL网络权限；客户端不能改写下载源、URL、安装根或checksum。game.install适配调用现有`install_blocking`并将实际线程完成接入自己的任务，不复用原async等待方的提前“已取消”结果。

game.launch只能获取自己的启动准备租约；取消携带operation_ref并校验对应来源，不能直接公开全局`cancel_launch()`。游戏创建是不可逆提交点，创建前最后校验；已创建的游戏不随权限撤销自动强杀，须由用户决定或在仍获准的stop API中处理。关闭Worker不影响用户已启动的Minecraft。

Java当前会合并同目标在途任务。Broker不得先调用install再发现它属于用户或其他插件；必须在提交和合并决策前确认所有者。不同来源忙时返回HOST_BUSY；同来源mutation_key可复用自己的回执，不获得其他任务取消权。

### 13.8 联机及工具进程

| 方法 | 请求params | 成功data | 必须能力/资源 | 额外错误 | 时限；owner |
|---|---|---|---|---|---|
| `connector.status` | `{session_ref?:string}` | `{state:"idle"\|"busy"}\|ConnectorView` | `connector.read`，自己的房间或明确读取授权 | `PLUGIN_CONNECTOR_UNAVAILABLE` | Q30；S+L |
| `connector.host_port` | `{port:1..65535}` | `TaskReceipt`；最终`ConnectorView` | `connector.host` + `network.lan`，确切端口 | `PLUGIN_CONNECTOR_BUSY`,`PLUGIN_CONNECTOR_FAILED` | T5/90秒；O+L |
| `connector.host_instance` | `{instance_ref,game_run_ref}` | `TaskReceipt`；最终`ConnectorView` | `connector.host` + `instances.read`，确切已运行实例/端口 | `PLUGIN_CONNECTOR_BUSY`,`PLUGIN_SERVER_PORT_NOT_FOUND` | T5/90秒；O+L+S |
| `connector.join` | `{room_code:string}` | `TaskReceipt`；最终`ConnectorView` | `connector.join`，用户允许的房间范围；不得日志记录room_code | `PLUGIN_ROOM_CODE_INVALID`,`PLUGIN_CONNECTOR_BUSY`,`PLUGIN_CONNECTOR_FAILED` | T5/90秒；O+L |
| `connector.leave` | `{session_ref}` | `TaskReceipt`；最终`{left:true}` | C，仅自己创建的L | `PLUGIN_CONNECTOR_CLEANUP_FAILED` | T5/30秒；O+L |
| `connector.request` | `{session_ref,protocol:string,body_b64:string}` | `{status:int,body_b64:string}` | `connector.protocol.execute`，该session和协议 | `PLUGIN_PROTOCOL_NOT_EXPOSED`,`PLUGIN_CONNECTOR_REQUEST_FAILED` | CB10；L或K |
| `connector.kick` | `{session_ref,peer_ref:string}` | `{requested:true}` | `connector.moderate`，自己的host房间 | `PLUGIN_NOT_ROOM_HOST`,`PLUGIN_PEER_NOT_FOUND` | W5；L |
| `connector.local_icon` | `{session_ref}` | `{png_b64:string\|null}` | `accounts.public.read` + `connector.read` | `PLUGIN_ICON_UNAVAILABLE` | Q30；L或K |
| `processes.spawn` | `{tool_ref,args:string[],cwd?:FileRef,stdin?:bool}` | `{process_ref,pid:int,state:"running"}` | `process.execute`，用户批准的tool及参数模板；cwd额外filesystem范围 | `PLUGIN_TOOL_NOT_AUTHORIZED`,`PLUGIN_JOB_ASSIGN_FAILED`,`PLUGIN_PROCESS_CREATE_FAILED` | W5；O+S |
| `processes.list` | `{}` | `{items:[{process_ref,pid,name,state,exit_code:int\|null}]}` | `process.read`，自己工具 | — | Q5；O |
| `processes.stdin` | `{process_ref,text:string<=4096}` | `{accepted:bool}` | `process.input`，自己工具 | `PLUGIN_STDIN_UNAVAILABLE`,`PLUGIN_PROCESS_EXITED` | W5；O |
| `processes.stop` | `{process_ref,force?:bool}` | `TaskReceipt`；最终`{stopped:true,exit_code:int\|null}` | C，自己工具 | `PLUGIN_PROCESS_STOP_FAILED` | T5/30秒；O |

tool_ref由管理页批准的可执行文件/代码包工具生成；不接受shell字符串、客户端指定任意可执行文件、PowerShell命令模板、任意env或系统解释器`-c`。需要此类能力的插件应声明更高风险的新契约，不能利用stdin绕过工具参数规则；批准交互工具时界面需明确其能力。

`host_instance`当前源码扫描全局Minecraft监听端口，未证明它会找到请求的实例端口。本API必须改为核对授权的game_run与端口；不能原样沿用后报告“按实例授权”。找不到可靠关联则返回SERVER_PORT_NOT_FOUND。

联机服务只有一个全局房间。新建/加入先取得来源租约，用户或别的插件已有房间时拒绝。旧任务取消、撤销后即使底层建房返回，也只能清理它自己的代次，不能覆盖或关闭后续用户房间。现有同步Florolding/EasyTier调用是否能及时停止，需要源码及实施验证；没有有界清理能力时暂停这些修改API，保留只读和扩展登记的独立验收。

### 13.9 UI、扩展登记和任务控制

| 方法 | 请求params | 成功data | 必须能力/资源 | 额外错误 | 时限；owner |
|---|---|---|---|---|---|
| `ui.publish` | `{slot_ref,nodes:UiNode[],revision:int}` | `{ui_ref,revision:int}` | `ui.declarative.write`，宿主允许的slot | `PLUGIN_UI_SCHEMA_INVALID`,`PLUGIN_SLOT_NOT_EXPOSED` | W5；O+S |
| `ui.remove` | `{ui_ref}` | `ReleaseResult` | C，自己UI | — | CL5；O |
| `ui.route_register` | `{name,title,icon?:内置图标名,nodes:UiNode[]}` | `{registration_ref,route_ref}` | `ui.declarative.write`，自己路由 | `PLUGIN_UI_SCHEMA_INVALID` | W5；O |
| `ui.window_open` | `{entry:AssetRef,title,width?:320..1920,height?:240..1080}` | `{window_ref,state:"opening"\|"ready"}` | `ui.custom.execute`，审阅的入口；必须通过第14节门 | `PLUGIN_UI_ISOLATION_UNAVAILABLE`,`PLUGIN_FRONTEND_NOT_READY` | W5；O |
| `ui.window_send` | `{window_ref,event:string,payload:object}` | `{accepted:bool}` | `ui.custom.execute`，自己窗口/事件schema | `PLUGIN_UI_EVENT_INVALID` | W5；O |
| `ui.window_close` | `{window_ref}` | `ReleaseResult` | C，自己窗口 | — | CL5；O |
| `ui.notify` | `{title,text,severity:"info"\|"warning"\|"error"}` | `{accepted:bool}` | `ui.notify`，限频3次/分钟 | `PLUGIN_RATE_LIMITED`,`PLUGIN_FRONTEND_NOT_READY` | W5；O |
| `instance_compat.register` | `{name,title,read_callback_ref,watch_callback_ref?:string,root_refs:string[]}` | `{registration_ref}` | `instances.compat.register` + 相应`instances.read` | `PLUGIN_REGISTRATION_CONFLICT`,`PLUGIN_SCHEMA_INVALID` | W5；O+S |
| `connector.extension_register` | `{name,protocols:[{name,callback_ref}],guest_joined_ref?,status_ref?,before_leave_ref?,reset_ref?}` | `{registration_ref}` | `connector.extension.register`，协议与房间范围 | `PLUGIN_REGISTRATION_CONFLICT`,`PLUGIN_PROTOCOL_RESERVED` | W5；O+S |
| `launch.hook_register` | `{name,instance_refs:string[],required:bool,prepare_ref?,pre_ref?,post_ref?,exit_ref?}` | `{registration_ref}` | `launch.hooks.register` + 对应实例范围 | `PLUGIN_SCHEMA_INVALID`,`PLUGIN_REGISTRATION_CONFLICT` | W5；O+S |
| `accounts.provider_register` | `{name,title,fields:[{key,label,type:"text"\|"password",required}],authenticate_ref,resolve_ref}` | `{registration_ref,provider_ref}` | `accounts.provider.register`，自己提供方 | `PLUGIN_SCHEMA_INVALID`,`PLUGIN_PROVIDER_CONFLICT` | W5；O |
| `crash.analyzer_register` | `{name,instance_refs:string[],callback_ref}` | `{registration_ref}` | `crash.analyzer.register` + `crash.read`实例范围 | `PLUGIN_SCHEMA_INVALID`,`PLUGIN_REGISTRATION_CONFLICT` | W5；O+S |
| `extensions.unregister` | `{registration_ref}` | `ReleaseResult` | C，自己的上述注册 | — | CL5；O |
| `operations.get` | `{operation_ref}` | `TaskView` | C自己任务；数据结果还需原资源读取授权 | — | Q5；O |
| `operations.list` | `{cursor?:string,limit?:int}` | `Page<TaskView>` | C，自己当前run任务 | — | Q5；O |
| `operations.cancel` | `{operation_ref}` | `{requested:bool,state,effect}` | C，自己任务 | `PLUGIN_COMMIT_STARTED`（不可取消） | CL5；O |

任何registry中的owner使用宿主上下文，不接受params中的owner。移除权限时立即使对应注册代次无效，即使Registry已有handler快照也须在实际调用前复核。首版没有通用HTML/CSS/JS注入API、全局主题覆写、其他插件设置修改、权限授予或用户文件选择弹窗API。

### 13.10 Host → Worker回调和受限前端桥

回调信封为 `{kind:"callback",invocation_ref,callback_ref,payload,remaining_ms}`；结果为 `{kind:"callback_result",invocation_ref,ok,data|error}`。invocation状态由宿主掌握，payload中的短期context_ref仅能用于该回调指定资源；收到结果时重新复核授权版本、内容摘要和资源代次。错误消息不包含凭据或房间码。

| 回调 | 请求payload | 成功data | 权限/所有者 | 时限及失败规则 |
|---|---|---|---|---|
| 生命周期 `load/enable/frontend_ready/disable/unload` | `{phase,run_state}`；load输入由受信引导取得 | `{completed:true}` | 运行会话，load须先通过闸门 | load/enable各15秒，ready5秒，disable/unload共5秒；超时停止Worker |
| 已登记命令 | `{args:登记输入DTO}` | 登记输出DTO | `commands.execute`、O/K | CB30；失败返回COMMAND_FAILED，迟到结果丢弃 |
| 订阅事件 | `{event,sequence,payload:白名单DTO}` | `{handled:true}` | 原订阅和资源仍获准 | CB5；进度允许合并，业务事件不可无限阻塞宿主 |
| 实例读取 | `{context_ref,instance:InstanceView,options:自己的设置}` | `{modified_ns:int,fields:{alias?,description?,icon_asset_ref?},stats:{play_count?,total_time_seconds?},warnings:string[]}\|null` | compat+实例读，K | CB2；可选来源超时跳过 |
| 实例监听 | `{options:自己的设置}` | `{targets:FileRef[]}` | 已授予filesystem.read范围 | CB2；宿主校验目标，禁止任意路径监听 |
| 联机协议 | `{context_ref,session_ref,protocol,body_b64,peer_ref?:string,game_info:{game_version,loader}\|null}` | `{status:int,body_b64:string}` | 原扩展、协议执行和会话授权，K | CB10；失败返回受控协议错误，结果最多256KiB |
| 联机会话钩子 | `{phase:"guest_joined"\|"status"\|"before_leave"\|"reset",context_ref,session:过滤ConnectorView}` | status可返回`{extra:命名空间JSON对象}`；其他`{handled:true}` | 原扩展和会话授权，K | CB2；可选钩子跳过，before_leave不阻止宿主清理 |
| 启动钩子 | `{phase,context_ref,instance:InstanceView,options:LaunchOptions,exit_code?:int}` | prepare为`LaunchPatch`；其他`{handled:true}` | 原钩子、相应实例范围，K | prepare/pre各5秒；post/exit2秒；required失败阻止创建，post失败不杀已启动游戏 |
| 自定义认证 | `{provider_ref,fields:用户主动提交的该提供方表单}` | `{local_account_id,alias,uuid,skin_asset_ref?,opaque_data?:有界JSON}` | 自己提供方，K；用户明确该提供方接收其表单 | CB60；失败不保存新账户 |
| 自定义凭据解析 | `{provider_ref,local_account_id,public:自己的AccountPublic,opaque_data:仅该提供方先前提交的材料}` | `{player_name,uuid,user_type:"msa"\|"yggdrasil"\|"legacy",access_token,auth_server?:string}` | 自己提供方，K；不传其他账户token | CB30；失败终止本次认证启动；结果只交宿主，不交前端/其他插件 |
| 崩溃富化 | `{context_ref,instance_ref,exit_code,output:脱敏文本<=64KiB,reasons:规范化摘要[],source_files:获准FileRef[]}` | `{reasons:[{title,detail,severity}],extra:自己命名空间JSON}` | analyzer+crash.read，K | CB5；失败跳过，禁止覆写宿主基础报告字段 |

`LaunchPatch`首版仅 `{jvm_properties:dict[str,str],game_options:{width?,height?,fullscreen?},env:dict[str,str]}`。properties只允许自己的`ecl.plugin.<namespace>.`前缀，env只允许自己的`ECL_PLUGIN_<namespace>_`前缀；数量各不超过32、值长度不超过4096。禁止javaagent、classpath、动态库路径、可执行命令、工作目录重定向、PATH、JDK_JAVA_OPTIONS、LD_PRELOAD和完整凭据。本轮将v1的“参数补丁”收敛为该范围；更多原始参数属于后续高风险接口评审，不靠调用进程API隐式获得。

自定义前端桥只允许：自己的settings.get/set、自己的已声明commands.call、窗口自身关闭、ui.action和已过滤的task/settings事件。禁止在前端直接调用全部Host API；需要业务服务时由Worker已注册命令调用Broker。桥接方法见第14节，具有独立来源校验，不把iframe声称的plugin_id作为身份。

### 13.11 可信管理页接口与授权变更回执

此组是Tauri管理IPC，不属于Worker Host API目录，也不属于iframe桥。身份来自native调用主体，不能从请求body的`window_type/main`声明取得。新增授权变更/审阅接口仅可信main可调用，缺少native主体、Dev Channel和插件窗口均拒绝；即使原生pyfunc允许抵达，也要在业务函数之前拒绝。

| 接口 | 请求body | 成功data | 额外错误/时限 |
|---|---|---|---|
| `plugin_permissions_get` | `{installation_id,content_digest?:string}` | `{installation_id,content_digest,revision,items:PermissionView[],required_satisfied:bool}` | `PLUGIN_MANAGEMENT_CALLER_DENIED`、公共E；Q5 |
| `plugin_permissions_update` | `{installation_id,content_digest,expected_revision,decisions:[{capability,resource_refs,decision:"allow"\|"deny",lifetime:"session"\|"permanent"}]}` | `{revision,grant_epoch,persisted:true,required_satisfied,cleanup_state:"not_needed"\|"pending"\|"completed"}` | `PLUGIN_GRANT_REVISION_CONFLICT`,`PLUGIN_GRANT_CONTENT_MISMATCH`,`PLUGIN_GRANT_PERSIST_FAILED`；5秒，未确认落盘则返回真实失败，不报已停止 |
| `plugin_permission_audit` | `{installation_id,cursor?:string,limit?:1..100}` | `Page<{time,capability,decision,resource_label,result,effect}>` | management caller、公共E；Q5；不返回凭据/完整个人路径/请求体 |
| `plugin_runtime_info` | `{installation_id}` | `{run_state,run_id?:string,pid?:int,cpu_percent?:number,memory_bytes?:int,last_error?:{code,message},cleanup_state}` | management caller、公共E；Q5，统计低频缓存 |

现有管理方法保留名称并更新响应/执行边界：`plugin_list/plugin_info`增加安装/运行/权限摘要；`plugin_enable`要求已审阅摘要、necessary权限和完整能力；`plugin_disable/plugin_unload`返回停止回执及实际状态；`plugin_reload`是停止确认后新run；`plugin_package_inspect/plugin_install`增加预检SDK/权限/摘要字段，安装确认不能自动执行入口或授予权限；`plugin_get_settings/plugin_update_setting`沿用相应型别校验及revision。旧脚本/Vue获取方法只返回系统可信注册，第三方使用v2 UI目录。

当前目录安装`_install_staged_plugin`会加载并启用新插件，必须改为事务提交后待审阅/待启用，不得在用户授权前用“验证插件能实例化”的理由执行入口。既有开发工具通道可按其明确主体策略继续安装/查询，但不能取得管理授权接口，也不能绕过启用所需批准；如何传递trusted caller由宿主入口决定，不在body中自授权。首版不新增独立开发工具仓库修改。

这一组及第14节专用原生命令均需注册在IpcCommandRegistry、IpcLogPolicy、Pydantic请求/响应及前端类型中；新增命令覆盖测试核对名称与日志分类，不用宽松dict静默接收参数。

## 14. PyTauri / WebView2 / Capability / iframe专项验证方案

### 14.1 源码调查得出的限制

1. 当前 `Tauri.toml` 设置 `withGlobalTauri=true`；前端 `src/api/transport/tauri.ts:36` 直接调用 `window.__TAURI__.pytauri.pyInvoke`。
2. 当前 `capabilities/default.toml:6` 同时匹配main和`plugin.*`，授予`pytauri:default`、dialog和多项core窗口权限，不能作为第三方v2的最小能力配置。
3. `ECL/api/registry.py:278` 仅在native `webview_window`存在时执行窗口授权；`bridge.py:535` 放行main，公共窗口操作先于插件目标检查。v2必须另外拒绝缺少可信调用主体及操作其他窗口的请求。
4. 当前插件窗口由`windows.py:135`加载主前端`index.html`，并将插件路由作为上下文。它不等于独立受限页面；不能原样复用主应用Bridge和全局SDK。
5. 所读PyTauri 0.8.0 Python接口的已知注入参数只有body、app_handle、webview_window、headers与states，未暴露可信frame_id或每次调用的文档来源。WebViewWindow.url是窗口信息，不能据此认定发起调用的子frame来源；JS headers也是客户端输入。
6. PyTauri对应上游源码以一个原生命令`plugin:pytauri|pyfunc`分派所有Python函数，函数名在`pyfunc`请求头；`pytauri:default`只授予`allow-pyfunc`。因此Capability不能直接按`plugin_enable`、`fs_read_file`等Python函数名逐个区分。
7. PyTauri 0.8.0对应上游tag的Cargo.lock为Tauri 2.8.3、tauri-runtime-wry 2.8.0、Wry 0.53.1、webview2-com 0.38.0。它是可定位的上游源码基线，不是本机wheel或未来发行物的构建证明。
8. Wry 0.53.1的WebView2源码注入`window.ipc.postMessage`，并从浏览器WebMessage Source构造原生请求。初始化脚本使用DocumentCreated路径。不能把“iframe没有__TAURI__变量”当成“原生IPC不可达”，也不能假设秘密只注入顶层。

这些限制不证明某个版本已经存在绕过漏洞；它们证明v1中的“不同源frame”还不足以静态确认边界。必须针对实际发行链做正向与反向测试。

### 14.2 拟议受限壳与原生授权双层设计

- 第三方窗口使用独立受信页面`plugin-shell.html`及独立前端构建入口；不加载主`index.html`、usePluginBridge、通用backend客户端、账户状态或全局插件SDK。
- 使用与旧`plugin.*`不重叠的新标签（`plugin-v2-<宿主随机标识>`），显式从默认Capability移除其匹配机会。实测 Tauri 拒绝点号，因此采用连字符前缀。可信shell只取得必需的PyTauri入口，默认无dialog、core:event或任意core:window能力；窗口操作经专用宿主方法完成。
- Python原生dispatch对v2窗口先做实际窗口身份与宿主注册状态检查，拒绝所有一般命令，只允许下表专用桥接命令。即使iframe自行构造pyfunc头或复制Tauri JS实现，仍不能调用完整前端命令表。
- 不能仅靠同一WebView标签区分shell与iframe。拟采用仅对受信顶层壳可见的随机桥接凭据，并绑定window、run、内容摘要与ui_session；每个专用命令都校验。凭据只由native顶层初始化提供，不通过可被任意frame调用的“获取token”API返回，不在URL、存储、事件或日志中出现。
- 初始化脚本必须显式检查顶层身份和准确受信URL；即使底层向所有frame注入，子frame也不得获得凭据。该机制是候选设计，必须验证默认注入行为、导航重注入及源码可见性；不能静态保证已成立。无法保证则需要可提供可信frame/origin的原生边界或调整宿主构建，并提交修订方案。
- 桥接凭据属于shell，不传给插件frame。frame只有有限MessagePort；shell校验初始`event.source`对应自己创建的frame，绑定代次，再转为固定允许的请求。拒绝客户端自选window/plugin/owner、方法名转发和未知字段。
- opaque-origin frame的`event.origin`可能是`null`；不能用`null`或`*`作为身份。只在必须给该已登记opaque frame传递MessagePort的握手使用目标`*`，不传宿主秘密，之后使用已经绑定的端口通信。

| 专用原生命令 | 请求 | 响应 | 限制 |
|---|---|---|---|
| `plugin_ui_ready` | `{ui_session,bridge_credential}` | `{ready:true,theme_tokens:白名单值}` | 实际v2窗口 + 正确凭据；不能返回凭据；5秒 |
| `plugin_ui_dispatch` | `{ui_session,bridge_credential,request_id,action:"settings.get"\|"settings.set"\|"command.call"\|"window.close",params}` | 对应第13节DTO/错误 | shell身份和凭据、frame代次、用户授权交集；params不携带owner；复用第13节时限 |
| `plugin_ui_poll` | `{ui_session,bridge_credential,after_sequence:int}` | `{events:[自己的过滤事件],next_sequence:int}` | 不开放core全局事件；最长等待15秒；有界队列及限长 |

桥接交给Worker的command必须属于当前插件且在自定义窗口清单中声明；不能从iframe直接调用interop或其他插件命令。重载、导航、窗口销毁、Worker停止或授权撤销后，凭据、MessagePort及事件队列立即作废。关闭自己窗口可保留为清理能力，不扩展到main或其他插件。

### 14.3 iframe、网络与导航不能靠单一CSP假定安全

拟采用`srcdoc`/受控资源加载及`sandbox="allow-scripts"`，不加allow-same-origin、allow-top-navigation、allow-popups、allow-downloads或allow-forms。插件内容资源仅来自审阅快照，模板不向主窗口执行；必要Vue/JS运行时在frame内的包资源提供，不从任意CDN加载。

frame的CSP使用deny-by-default：禁用connect、object、base和form，图片只允许有界data/blob；script/style仅按包内执行需要开放。可信shell另有严格CSP和固定资产列表，不共用frame的宽松script策略。

**CSP和sandbox不自动阻止frame自导航至外部URL，导航后新文档也可能不受原CSP约束。** 若首版要兑现“外部网络只能走授权代理”，必须验证并控制frame导航、所有资源请求、WebSocket/worker和自定义协议。所读PyTauri接口有on_navigation，但未静态证实它覆盖所有子frame；没有明确暴露可直接使用的frame资源策略。仅观察load之后关闭通道不能阻止已发送的网络请求。

因此将“原生IPC隔离”和“frame外部网络/导航限制”作为两个独立验收门：其中任何一项不满足，`ui.custom.execute`保持不可用，向用户提交具体缺口；不得把“多数请求被CSP拦截”作为完成。声明式UI可以单独交付，但需要明确报告自定义前端范围未完成，不能宣称全部首版已达成。

### 14.4 验证环境、探测路径和可观察结果

批准实施后，在隔离的测试数据目录内运行真实Windows ECL/PyTauri/WebView2；记录wheel、原生链和WebView2版本。普通Chromium/DOM测试只能验证schema与消息逻辑，不能证明native IPC边界。

测试用例覆盖顶层可信shell、opaque srcdoc、data/blob、受控外部origin、嵌套frame、错误增加allow-same-origin的负面对照，以及frame重载/导航后的文档。只使用测试canary资源和明确的无副作用探测；禁止访问真实账户或删除用户文件。

| 编号 | 探测方法 | 必须观察到的结果 |
|---|---|---|
| U01：合法链路 | shell凭据正确、绑定frame经端口调用自己的声明命令 | 正向成功；证明整个native链实际可达，避免“IPC全坏导致反向用例全失败”的假通过 |
| U02：DOM/全局隔离 | 读取parent/top DOM、全局SDK、localStorage、cookies及shell桥接凭据 | 未取得可信壳状态/凭据；允许读取自己frame内数据 |
| U03：公开PyTauri入口 | frame直接尝试pyInvoke，包含settings、fs_read_file、plugin_enable及管理授权接口 | 宿主业务handler调用计数为0；返回原生/宿主拒绝，不以JS变量不存在作为唯一证据 |
| U04：绕过SDK的原生路径 | 复制公开invoke代码；直接构造`plugin:pytauri\|pyfunc`与pyfunc头，测试window.ipc、chrome.webview.postMessage及实际链使用的IPC fetch通道 | 一般命令仍拒绝；专用桥无凭据/错凭据也拒绝；不得要求iframe不能发出任何消息才通过 |
| U05：跨对象攻击 | 伪造plugin/window标签、别的command_ref/operation_ref，关闭main或别的插件窗口 | 原生真实窗口身份与owner共同拒绝，目标状态不变 |
| U06：Capability隔离 | 使用core窗口、dialog、core事件和远程origin尝试调用；审计实际合并后的所有Capability | 无默认规则叠加授予；管理操作不能经另一插件/原生插件命令绕过Python门 |
| U07：消息欺骗 | 无source、错误source、`origin:null`的另一个frame、旧MessagePort、重复键、超长/深层数据、任意method转发 | 未绑定请求拒绝；正确端口仍只能当前owner的固定action |
| U08：初始化/导航 | 在顶层和所有frame检测初始化脚本及秘密可见性；导航到远端、tauri应用URL、about:blank、data/blob，尝试修改sandbox | 新文档不得获得secret或宿主能力；通道旧代次失效；主窗口不能被导航或污染 |
| U09：网络限制 | 本地测试HTTP/WebSocket canary计数器；fetch、XHR、sendBeacon、img、form、iframe自导航、worker、CSS URL、自定义协议 | 未授权请求服务端计数为0；不能仅以frame控制台报错判断未发请求 |
| U10：撤销/退出 | 请求在flight时撤销UI/command授权、关闭窗口、停止Worker，之后重放原生消息/端口消息 | 新请求拒绝，旧结果不交付，关联注册和队列清空 |
| U11：资源及日志 | 模拟事件洪泛、巨型schema、secret/token在payload及异常里 | 队列有界，日志脱敏且不回环，不泄漏shell秘密 |

原生层允许消息抵达也可以通过：关键是一般业务方法不执行，专用桥需受信来源凭据及业务权限。反之，若外层window标签被识别为main，或任意frame能取得桥接凭据，即使iframe有sandbox也失败。

当前Dev Channel不具备frame身份，而且token保存在同用户可读目录。本方案不将它用于frame桥；新增授权管理命令明确拒绝Dev Channel和未知native主体。CSP无法替代对整个开发通道的威胁分析；普通Python直接读取该token的风险仍属于首版非OS沙箱边界。

## 15. 权限即时撤销与内容摘要变更的严格规则

### 15.1 撤销生效点和锁边界

定义T为宿主在该插件授权锁内发布新授权版本和拒绝屏障的时刻。**“即时”指T之后不能入队或批准新的业务执行/提交，不指所有线程、远端请求和已发生副作用在同一时刻消失。** 管理界面收到撤销成功前，必须已建立屏障并完成授权持久化；资源清理状态另行显示。

每个请求/注册/任务关联 `installation_id, content_digest, run_id, grant_epoch, required_capabilities, resource_refs`。宿主在入队、出队、每个敏感阶段、提交准入、结果交付时检查；不能只在IPC入口检查一次。回调有效性由host的invocation登记决定，不采信Worker回传epoch。

使用每插件授权锁及短时资源提交准入，不在其中执行网络、磁盘长操作、RPC或用户回调。固定锁序：授权锁 → 插件任务/资源登记锁；服务执行后回到检查点，不在服务锁内反向等待授权锁。全局OperationManager锁不得包住长提交动作，也不得让一个插件撤销等待另一插件的长任务。

授权变更先冻结相关准入，再原子保存；失败时保留内存拒绝屏障，报告 `PLUGIN_GRANT_PERSIST_FAILED`，标记“已停止授权、保存失败”，不能误报成功或重新放行旧授权。恢复持久化前禁止重新启用。服务不依赖磁盘文件mtime轮询作为撤销机制。

### 15.2 各操作状态的处理矩阵

| 撤销时状态 | 宿主动作 | 结果/可见性 | 是否回滚 |
|---|---|---|---|
| 未受理/排队 | T之后不接受；移出队列或在出队拒绝，作废回调/注册代次 | REVOKED；effect=none；不得进入业务函数 | 不产生副作用，无需回滚 |
| 只读执行中 | 请求停止读取/计算，作废结果；不可中断线程仍跟踪 | 不向已失权插件交付数据；管理页可见pending cleanup | 不适用；已交付数据无法收回 |
| 执行中，仅暂存 | 设置取消及撤销原因，停止后续网络/文件阶段；待执行方清理临时产物 | cancellation_requested=true；确认收敛后cancelled，effect=staged或none | 清理本任务暂存；保留原目标；失败残留单列 |
| 进入提交前检查 | 与T在同一授权锁序列中竞争；若T先发生，禁止获得提交许可 | REVOKED或cancelled，不切换版本/文件/业务会话 | 清理暂存 |
| 已取得提交许可/提交中 | 该不可逆动作在T之前已被授权接受；不强行中断原子replace/登记/进程创建；禁止扩大原动作范围 | can_cancel=false；记录committing、撤销已发生；最终真实success/failed/partial | 只按该服务自身失败事务回滚；不因撤销假称回滚 |
| 已完成 | 停止继续使用资源/订阅和交付敏感结果；保留审计摘要 | 最终状态仍completed/failed；权限撤销不是“历史操作取消” | 不自动还原目标文件、退出游戏或撤回远端HTTP |
| 远端写请求已发送、结果未知 | 终止后续等待/读取，保留请求实际发送状态；禁止自动重放 | effect=unknown，记录可能已发生副作用 | 宿主不能承诺撤回远端行为 |

在提交许可已发出但线程尚未执行原子动作的窗口，v2将其视为“提交已被接受”，不能再承诺撤销会阻止动作。该许可仅对应一个确定目标、一个有限动作，不能用于一串后续操作。每个新的副作用/提交须重新准入。许可接受时间及effect均持久化到任务摘要；崩溃恢复时若无法确定提交结果，报告unknown并复核服务事务，而不是自动重放。

为实现上述语义，现有OperationManager需区分`execution_phase`和最终status，提交准入时先置`can_cancel=false`，再释放管理锁执行服务事务；失败时保留真实错误，不因为cancel_event被设置就把已提交/失败操作统一标为cancelled。取消API返回requested不是stopped。

### 15.3 不同服务的检查点与后果

| 服务 | 必需检查点 | 撤销后处理 |
|---|---|---|
| 文件写/下载 | 获取写会话、每次chunk/连接/redirect、最终路径/etag复核、提交准入 | 取消自己的暂存；T之后不批准新rename/replace；已提交文件保留 |
| Java安装 | 计划与来源、下载、解包、目标/引用复核、登记/目录事务准入 | 未提交可取消；已提交保留Java，不能删除共享运行时补偿；其他来源task不受影响 |
| Minecraft安装 | 每阶段、Downloader、备用源、写目标边界 | 现有安装会增量写目录/共享缓存，不能承诺整体原子回滚；取消后保留已有合法文件，effect可能partial；owner适配及可观察实际线程退出为开放条件 |
| 游戏启动 | 取得来源租约、补文件、每个钩子、prelaunch用户命令之前、创建游戏进程前 | 取消自己的准备；不能取消用户启动；创建已准入则保留已启动游戏，撤销不强杀 |
| 联机 | 取得房间代次、节点准备、创建/加入前、发布会话之前 | 底层创建属于可能已产生网络副作用；失权后不发布结果，按旧代次清理本任务会话；不能关闭新用户房间 |
| 工具进程 | 创建受控挂起进程、加入Job、恢复前准入 | 若尚未恢复则直接回收；已运行工具按owner主动停止/Job回收；外部副作用不回滚 |
| 回调/事件/UI | 实际调用前、结果合并前、每次UI dispatch/poll | 旧handler快照也失效；拒绝迟到patch；关闭受影响UI通道，不能交付旧事件 |
| settings/profile等短写 | 字段/版本检查、原子写前准入 | T之后无新提交；许可先取得则结束有限提交并报告真实结果 |

文件路径静态resolve校验不能防同用户恶意修改链接的全部TOCTOU；实施需针对受控API使用实际打开句柄/目标属性复核，不宣称普通Python OS访问受控。长任务每个循环的检查粒度应记录在验收结果中，不宣称线程可被立即杀死。

### 15.4 授权增加、缩小和过期

- 用户批准增加权限后，发布新epoch并持久化；插件收到自己的新PermissionView。新授权不自动执行旧排队请求，须明确新请求。
- 任何epoch变化使旧尚未提交请求及invocation失效，哪怕变化是增权，避免不同快照混用；用户可重新提交。
- 缩小范围按撤销矩阵处理相关任务/注册；必要能力不再满足则停止Worker，可选能力清理后允许降级。
- 永久授权与会话授权分别保存；会话授权不在应用重启/Worker新run后延续。到期由宿主时钟检查并发布撤销，UI显示实际有效期。
- 收到同一run的延迟“允许”通知不改变宿主决定，Worker缓存仅用于UI提示，Broker每次检查真实授权。

### 15.5 清单、代码摘要、依赖及更新

保留现有`manifest_sha256`作为归档清单的完整性依据；新增授权身份`content_digest`。建议由明确layout版本、包代码清单摘要、权限清单规范化摘要、完整锁字节和目标ABI组成。显示名称、作者字符串、版本号都不作为授权身份；目录插件用排序后的相对路径/字节摘要构造受控快照，不直接执行随时可变的开发源目录。

1. 安装预检、用户审阅和激活引用同一预检摘要。审阅后源包变化则重新预检，不能沿用确认。
2. 活动安装使用宿主安装身份，不按name复制别的安装授权；无签名不能确认作者连续性。
3. 新包可安装到新版本目录，保留“待重启”，不热切换Worker依赖。新代码/权限/锁摘要须重新审阅；用户在管理页可显式确认继承旧范围，新增权限默认拒绝。
4. 旧run若继续运行，仅保留针对其旧摘要已审阅的授权；新摘要的grant不能赋予旧run，旧run授权不能自动给新版本。撤销旧run按正常T处理。
5. 代码目录实际字节与清单不符、出现额外载荷、依赖清单校验失败时，不视为普通升级：撤销运行准入、停止Worker、标记CONTENT_CHANGED/完整性失败，需可信重装或重新生成快照，不能“接受新哈希”继续运行。
6. 启动时、授权提交时、`load_authorized`之前再次验证快照；共享依赖池复用前沿用ready/inventory校验。没有OS只读边界，运行期间恶意同用户修改字节不可能仅凭一次哈希验证被完全阻止，本期明确不提供该保证。
7. v2授权文件有schema_version和revision，原子更新，损坏时默认拒绝并提示；不迁移旧声明为allow。卸载和重新安装生成新安装身份，不继承已撤销的授权。备份/回滚的代码指针不自动恢复allow。

旧active.json当前`record_version:2/install_mode:"host-directory"`是安装存储版本，与SDK API v2不是同一个版本。新运行方式必须使用显式字段/存储版本，拒绝含糊混用；保留旧记录和用户数据，不恢复历史Worker兼容路径。

### 15.6 撤销竞态测试

批准实施后使用可控屏障/事件固定任务执行位置，不能靠sleep碰运气：

- V01：入队后、出队前撤销，业务handler计数为0。
- V02：文件分块/下载中撤销，原目标不变，取消已请求与线程实际结束分别可见。
- V03：提交准入竞争，分别强制T先和提交许可先；结果严格对应第15.2节。
- V04：提交中撤销且原子动作失败，状态为真实failed，不虚报cancelled或completed。
- V05：已完成后撤销，不更改历史结果或删除目标，敏感内容不继续交付。
- V06：registry已抓取handler快照后撤销，调用/结果合并被拒绝。
- V07：同目标Java用户任务、另一个插件task、全局用户游戏启动，插件取消均不得影响。
- V08：旧联机请求晚于取消返回；新用户房间存在，旧清理不得误关新代次。
- V09：增权/缩权/到期、旧epoch/旧run重放、新摘要/旧摘要授权交叉，均不得混用。
- V10：授权落盘失败，屏障保留且UI明确保存失败；重启损坏授权文件默认拒绝。
- V11：安装确认后包字节变化、目录额外文件、依赖篡改、取消后task重试，必须重新校验和授权。
- V12：长线程不能停止或网络副作用未知，系统报告pending cleanup/partial/unknown，不伪报已停止或已回滚。

## 16. 可独立验收的实施切片、阻塞与回滚

### 16.1 通用推进规则

以下 v1.1 切片已获得用户“开始实施”批准。每个切片先定义可观察行为，再改对应边界；通过局部回归及项目规定门禁才提交该完整切片，不把“增加大量空模块/未接线接口”作为功能完成。涉及原生绑定新增边界的 P04 修订仍须按项目流程提交详细增补方案审阅。

- P01/P02/P03 是高风险验证门，安排在大规模业务迁移前；测试载荷仅存在于tests及专用测试数据目录，不执行用户插件。
- 引入生产第三方v2运行分流时立即禁止其旧主进程执行分支；旧API、未验收平台、必要方法未实现的插件显式保持不支持/禁用，不降级绕过监管。
- 本轮设计最终目录中的方法只有各切片验收后才进入Broker白名单；临时缺失返回METHOD_NOT_FOUND，不以空success占位。最终交付必须覆盖第13节所有首版方法，除非用户批准明确的范围修订。
- 系统插件继续走原契约，针对QomicEX实例、联机及Vue设置卡片做回归；不能为了第三方v2移除其功能或重写子模块。
- 任一切片造成真实用户数据迁移、接口变更或跨子模块需求超出此稿时，停止相关工作并提交修订；不借实施许可自行扩大安全保证。
- 未通过的高风险门留在同一实现分支修复，不同步到主工作区/发布；局部完成不等于首版整体完成。

### 16.2 文件、测试、验收、阻塞和回滚矩阵

下列测试名称是设计时拟新增/更新的用例定位，实际落地位置及状态见第 18 节。路径以仓库根为准；P代表实施切片，不是面向人的日志编号。

| 切片与依赖 | 具体代码边界 | 测试用例 | 验收标准与阻塞条件 | 回滚 |
|---|---|---|---|---|
| P00：契约与身份模型 | `ecl_plugin_api/contracts.py`；`ECL/plugins/runtime/contracts.py`；宿主typed DTO与方法目录 | `test_plugin_v2_contracts.py`：每个方法schema、额外字段、限长/深度、bytes/NaN、owner伪造；现有API模型回归 | 第13节每个方法都可查到明确schema/策略；不改变现有运行路径；内部模型无长期Any。无法确定字段则先修订契约 | 撤回新模型和测试，不迁移数据 |
| P01：Windows原生监管，依赖P00 | `runtime/windows.py`、native创建/句柄封装，不接用户插件发现 | F03/F04/F06/F07；`test_plugin_windows_supervision.py`：挂起、assign失败、嵌套Job、句柄泄漏、breakaway；POSIX拒绝分支 | 从挂起到确认Job之前无受控载荷执行；失败不恢复；主退出回收。已有Job限制不允许绕开。不能证实则停止P02及运行迁移 | 关闭本切片创建的Job和handles，测试目录留档；撤回适配器，无用户插件数据变化 |
| P02：发行物分流，依赖P01 | `main.py`、`runtime/executable.py`、轻量`ecl_plugin_api/_process.py`、Nuitka/spec/pyproject收集入口 | F01/F02/F05；真实Nuitka x64；PyInstaller onefile专项；现有`test_cli_launch_options.py`、`test_single_instance.py`、`test_runtime_paths.py` | 明确记录B/W、原EXE、资源目录、运行版本；闸门前无插件导入；normal CLI/更新重启不变。无法解析EXE、重复主窗口或Job绕过则停止 | 撤回入口和构建收集；清理仅本次测试产物。保留失败日志，不修改发布标签 |
| P03：依赖实际隔离，依赖P02 | `ecl_plugin_api`导入上下文；`host_dependencies.py`分离worker策略；不全局替换宿主模块 | F08；更新`test_plugin_environment_pool.py`、`test_plugin_host_dependencies.py`及冻结测试：同包不同版本、原生库、importlib.resources/metadata、.pth不执行 | 各W使用自己的版本，宿主不污染；ABI不符拒绝；只有磁盘共享不伪报模块隔离。冻结finder/native依赖阻断则停止后续运行迁移 | 撤回worker加载策略，保留完整锁/cache/目录；无自动删除，无新venv |
| P04：前端原生边界早期验证，依赖P00 | 前端受信测试壳/fixture；现有`host/tauri.py`、Capability与native授权检查的最小验证接线；不启用用户自定义UI | U01—U09；真实WebView2；`test_plugin_ui_authorization.py`记录business handler计数 | 正向可达，所有绕过负向无副作用；证明凭据不进子frame，导航及网络门可控。不得用纯浏览器/无__TAURI__替代。缺可信来源控制则提交方案修订；不继续自定义UI实现 | 撤回测试壳及本切片配置；恢复可信主窗口功能，第三方自定义能力始终关闭 |
| P05：授权存储与内容绑定，依赖P00 | `permissions.py`、`grant_store.py`、preparation/activation摘要；管理接口caller分类 | V01/V09/V10/V11；`test_plugin_grant_store.py`：原子失败、损坏、会话到期、卸载重装、代码变更 | 声明不等于allow，授权只绑定本安装/摘要；管理接口拒绝非main/Dev Channel/未知主体。摘要不可绑定或失败时放行是阻塞 | 保留新授权文件/旧安装记录备份；功能关闭后默认拒绝，不回填旧声明为allow |
| P06：最小v2生命周期与控制，依赖P02/P03/P05 | manager discovery/lifecycle/registry、supervisor；SDK Plugin、命令、设置、事件；Worker状态 | `test_plugin_worker_lifecycle.py`、`test_plugin_worker_commands.py`：sync/async、嵌套反向RPC、超时、断线、disable/reload；现有shutdown/disabled/slots/sdk测试；V06 | 第三方不在宿主执行；注册全有run owner；线程超时不冒充已停止；只有已实现能力进入白名单，required未实现拒绝。死锁、残留handler或降级进程内是阻塞 | 停止本次W并撤销注册；回退时保留第三方禁用状态，不恢复其自动主进程运行 |
| P07：公共只读服务，依赖P06 | Broker实例/账户/公开配置、Java及游戏catalog查询门面；过滤DTO和资源ref目录 | `test_plugin_host_queries.py`：授权过滤、跨root/runtime/account、输出敏感字段、query timeout；现有accounts/java/scan回归 | 能查询获准资源，没有裸服务输出或隐私字段；断线后无结果交付。需要透传完整配置才工作则拒绝该接口并修订 | 撤下查询方法白名单/资源refs，原可信前端接口保持 |
| P08：任务来源与提交准入，依赖P05/P06 | `services/operations.py`、`runtime/operation_adapters.py`，注册/短写的提交上下文；先用测试任务验证 | V01—V05/V07/V12；`test_plugin_operation_ownership.py`；完整既有OperationManager测试 | pending/running/committing/terminal可区分；T与提交许可顺序可确定；不在全局锁执行长提交；自己的cancel不影响用户。任何旧服务任务回归失败是阻塞 | 撤回可回滚接口接线，不删除任务日志；已经提交的数据不通过Git回退还原 |
| P09：受控文件与配置短写，依赖P08 | `runtime/file_api.py`、同卷暂存、root/句柄校验；settings/profile原子revision适配 | V02/V03/V04；`test_plugin_host_files.py`：read分块、etag冲突、write/abort、delete恢复、junction/ADS、并发用户修改；settings/profile回归 | 撤销前后操作符合第15节；不越界、不覆盖冲突；failed清理保留原文件；控制句柄竞态未解决则阻塞相关写API | 仅清理自己的未提交暂存；保留用户目标；已提交效果由任务记录说明，必要恢复经用户操作 |
| P10 A：只读网络；下载待审阅，依赖P08/P09 | `network_targets.py`、`runtime/network_api.py`、`network_transport.py`；后续下载另行适配 | `test_plugin_network_targets/api/logging/worker/shutdown.py`：准确范围、真实HTTP/TLS、peer、逐跳认证、DNS重绑定、预算/撤销/实际清理；下载保持既有回归 | A仅GET/HEAD、32KiB；固定数字IP、TLS原域身份，无共享凭据。下载任务、hash和提交竞态仍未完成，不把本切片计为完整P10 | 关闭网络门面，收尾所属真实流；DNS未结束保留pending，迟到不继续连接；不影响用户下载 |
| P11：Java所有者与安装，依赖P08/P10 | `java/manager.py`、`java/installer.py`、Broker计划ref/任务去重 | V03/V04/V07；`test_plugin_host_java.py`：同源/异源目标合并、计划过期、取消解包/commit、租约引用；现有Java测试 | 计划URL/目标不可改，不能取得别人的task，实际安装/登记及回滚正确。来源在合并之后才检查是阻塞 | 保留已经登记的运行时；撤回插件门面，不自动删共享Java；取消未提交自己任务 |
| P12：Minecraft安装/启动，依赖P08/P10/P11 | `services/game/install.py`插件任务适配；`launch.py`来源租约、提交闸门；Broker最小LaunchOptions | `test_plugin_host_game.py`：增量安装partial、后台真实退出、用户启动并发、自己的cancel、创建前后撤销、已启动游戏保留；现有launch/install回归 | 不调用无owner的全局cancel；安装取消不得报原子回滚；真实线程/下载器结束可观察。需要Core新接口才保证时停止并修订子模块范围 | 不卸载已装合法文件/共享cache；保留已启动Minecraft；撤下插件接口，来源租约有界释放 |
| P13：工具进程，依赖P01/P08 | supervisor工具创建；`processes.py`带owner监督入口/终端适配 | `test_plugin_host_processes.py`：参数模板、stdin限制、tool digest、同Job、跨owner/跨run操作、停止、日志限频；现有process tests | 首条工具代码运行前在J；端口/工具身份不可伪造；终端显示兼容；不把游戏加入插件Job。需改Core接口时先评审 | 回收本插件工具；不停止用户Minecraft；撤回provider适配 |
| P14：实例/启动/崩溃扩展，依赖P06/P07/P08/P12 | 各registry的远程适配与回调DTO；LaunchPatch白名单 | `test_plugin_worker_instance_compat.py`、`test_plugin_worker_launch_hooks.py`、`test_plugin_worker_crash.py`；CB超时/迟到/V06；系统QomicEX回归 | 不能借Path/日志/patch扩权；required pre失败阻止创建，post失败不杀游戏；系统回调行为不变。同步链死锁或泄漏基础报告是阻塞 | 移除自己的远程注册；保留系统registry和宿主基础扫描/崩溃分析 |
| P15：自定义认证，依赖P06/P08/P10 | `auth_providers.py`、`services/account/accounts.py`的自有opaque data与远程解析 | `test_plugin_worker_auth_providers.py`：secret隔离、跨provider、失联、超时、持久化失败、提供方disable；既有accounts回归 | 只有自己的认证材料进入对应W；登录失败不保存；无通用宿主token接口；日志/前端不含credentials。旧公开account/opaque混用是阻塞 | 撤销提供方注册，保留账户数据，显示提供方不可用；不尝试旧SDK自动登录 |
| P16：联机租约和扩展，依赖P06/P08/P10 | `services/connector.py`来源/代次、房间控制与取消清理；`plugins/connector.py`有界RPC | `test_plugin_worker_connector.py`：用户房间busy、建房迟到/V08、实例端口关联、request嵌套、before_leave超时；主仓库及准确Florolding集成 | 旧代次不能覆盖/关闭新房间；同步native资源可有界清理或明确pending；按实例端口真实对应。底层不支持必要控制则暂停修改API并评审 | 清理自己的旧代次，不调用全局leave影响用户房间；保留系统联机扩展 |
| P17：声明式UI与管理页，依赖P05/P06/P07/P08 | 前端plugins页面、install modal、types/store/SDK、schema renderer；后端管理IPC/models/logpolicy | `pluginPermissions.test.ts`、`pluginDeclarativeUi.test.ts`、安装/更新/撤销UI；backend caller测试；真实桌面 | 主窗口不执行第三方JS/Vue/HTML/CSS；必要拒绝/可选降级、实际取消状态显示正确；pnpm check/build及桌面门禁 | 移除自己的UI注册，保留授权文件和用户配置；第三方不退回旧注入路径 |
| P18：自定义前端，依赖P04全部门通过及P17 | 受信plugin-shell独立构建入口、消息端口、专用native桥/Capability、UI事件轮询 | U01—U11全部；自己的settings/command正向、违规native无业务调用、导航/网络计数、撤销、关闭 | 所有来源/凭据/能力及网络门通过；不能静态确认的项提交真实证据。失败时不开ui.custom.execute，不宣称整个首版完成 | 关闭自定义窗口/凭据/端口；声明式UI保留；不改回主窗口任意代码 |
| P19：完整安装/发行回归，依赖上述首版接口全部通过 | package/lifecycle剩余集成、docs、CI真实冻结覆盖、非Windows提示 | 真实Nuitka x64全链：离线安装→授权→启用→业务→撤销→停止→更新→重启→卸载；安全模式、完整ruff/pytest、pnpm、WebView2 | 首版目录无缺项，性能/残留/安全限制都有实测记录；子模块集成测试通过。任一关键门未达或范围未经批准缩减，不能标完成 | 保留previous指针、grant备份、task结果和数据；应用回退维持第三方禁用；不推送/标签/发布 |

P14的各扩展、P15认证、P16联机互相不做一次性大改；每一类先接单个注册/回调和失败清理，再扩展到该类完整契约。P17可与已稳定业务切片在实施顺序上交错，但本轮未授权并行代理或额外任务。

### 16.3 项目门禁如何落到切片

- 每个后端功能切片执行 `ruff check ECL tests`、`ruff format --check ECL tests`、新增用例和相关既有回归；提交前执行CI要求的集成范围。涉及Core时按其自身规范，并执行主仓库集成测试。
- 每个前端切片先执行与其lint工作流一致的 `pnpm check`，通过后 `pnpm build`；UI/启动路径相关切片还需真实启动器/WebView2验收。
- 冻结切片保留实际构建器/依赖版本清单、产物hash、B/W记录、Job资格、入口闸门和退出结果；测试成功不能只引用fake Popen或mock Job结果。
- 合约测试不替代功能测试：每个开放的方法都要有允许、拒绝、错owner、超时/撤销与正确结果用例；关联共享状态还要有用户/另插件并发用例。
- 测试数据位于明确专用目录，测试进程只停止自己创建的宿主/Job，不扫描终止本机其他ECL/游戏进程。
- 功能完成后使用中文Conventional Commit；前端独立提交，主仓库gitlink仅包含对应已验证前端提交。不夹带旧设计文档之外的用户改动。无远端推送、标签或发布权限。

### 16.4 回滚的准确边界

代码回滚不等于业务效果回滚。每个TaskView保留effect和对应服务事务结果，用户可以判断是否需手工处理。文件/Java事务失败沿用可验证的恢复路径；已完成文件写入、已经发出的HTTP、增量安装、启动游戏和房间操作不能由Git revert撤销。

新增授权存储不覆盖旧plugin配置，安装指针变更保留previous及新旧摘要；回退旧代码时禁止把未知授权状态解释为allow。若旧应用本身不能识别禁用/新安装格式，回退前将第三方安装保持禁用并保留快照，要求用户显式确认旧运行方式。不得自动删除代码、Wheel、依赖目录、账户或存档。

## 17. 尚未验证的技术风险与引用索引

### 17.1 实施前不能声称已成立的事项

| 风险 | 静态调查结论 | 对应验收门/失败处理 |
|---|---|---|
| 原始EXE与冻结分流 | 有可定位入口与Nuitka内部原EXE字段，当前ECL无Worker实现 | F01/F02/F05，P02；无法唯一解析则拒绝启动 |
| onefile子进程继承Job | 所读引导采用CreateProcessW；Windows公开语义支持默认继承 | F03/F04/F06/P01；实际分支/嵌套Job失败即阻塞，不能仅按源码推定通过 |
| 宿主退出与解包残留 | Job关闭可回收成员，强杀引导可能绕过临时目录清理 | F07/P19；进程回收与文件残留分别报告 |
| 所有恶意派生都被Job控制 | Windows文档明确有非普通CreateProcess路径例外；Job不是OS沙箱 | 不承诺限制WMI/其他broker派生或同用户进程攻击；本期保证正常受控创建链，恶意限制需另行沙箱 |
| 同包不同版本/原生库 | 进程独立解决缓存共享，冻结finder和ABI仍有约束 | F08/P03，未通过不开放运行 |
| Native frame身份 | 当前PyTauri Python绑定没有直接可授权的frame/origin字段 | U03/U04/U08/P04；壳凭据不可安全隔离时需原生方案修订 |
| Capability细粒度 | pyfunc是共用原生命令，不按Python方法区分；现有默认权限过宽 | U05/U06/P04；必须宿主dispatch复核，不能只改Capability |
| iframe网络/导航 | CSP/sandbox单独不足；子frame控制点未静态证实 | U09/P04/P18；阻塞custom UI，声明式完成需单独标注范围 |
| 即时撤销与提交竞态 | 当前OperationManager没有授权/owner及独立提交准入 | V01—V05/P08；T及提交许可顺序不可确定就不开放写接口 |
| 任务真正停止 | 安装/联机可能在等待方取消后继续线程/native执行 | V07/V08/V12/P12/P16；保持pending cleanup，不报已停止 |
| 共享任务控制权 | Java按目标复用，game全局cancel，connector单会话 | 各来源适配门；不允许先共享后补owner |
| 运行时磁盘篡改 | 哈希校验能发现已发生变化，不能单独强制只读或防同用户攻击 | 内容绑定与失败拒绝；不扩大首版安全宣称 |
| SDK覆盖面/性能 | 完整目录尚未实现，未测冻结启动/多插件内存 | P19逐接口与实测；不存在本轮“已验证性能”结论 |

### 17.2 主仓库与前端源码索引

行号以调研基线为准，后续实现会变化；链接指向真实文件。初始调研时工作区子模块未初始化，前端及 Core 使用本机完整仓库的准确 gitlink 提交只读调查。批准实施后已在本工作区初始化到这些准确提交，未修改子模块源码或指针。

| 主题 | 源码 |
|---|---|
| 启动/单实例/路径 | [main.py](../main.py)、[cli.py](../ECL/cli.py) `run_launcher:318`、[launcher.py](../ECL/launcher.py) 单实例探测`:75`、[runtime.py](../ECL/foundation/runtime.py) `get_runtime_info:50` |
| 打包/依赖约束 | [build.yml](../.github/workflows/build.yml) `:278,330,343`、[Nuitka脚本](../packaging/build-nuitka.ps1) `:76`、[PyInstaller spec](../EuoraCraft-Launcher.spec) `:213,260`、[pyproject.toml](../pyproject.toml) `:53,59,65` |
| 当前进程内执行 | [discovery.py](../ECL/plugins/manager/discovery.py) `_create_instance:162`、[plugin.py](../ECL/plugins/plugin.py) `__init__:185`、[registry.py](../ECL/plugins/manager/registry.py) `call_command:415` |
| 权限/安装摘要/事务 | [permissions.py](../ECL/plugins/permissions.py) `PermissionManager:138`、[package_preparation.py](../ECL/plugins/package_preparation.py) `verify_prepared_code:92`、[package_activation.py](../ECL/plugins/package_activation.py) `_payload:97,activate:114`、[lifecycle.py](../ECL/plugins/manager/lifecycle.py) `_install_staged_plugin:388` |
| 任务/提交 | [operations.py](../ECL/services/operations.py) `_Operation:94,submit:157,finalize:207,cancel:314`、[custom_downloads.py](../ECL/services/custom_downloads.py) `_download:347` |
| 游戏/Java/联机 | [install.py](../ECL/services/game/install.py) `install_version:131,_run_install:211,install_blocking:287`、[launch.py](../ECL/services/game/launch.py) `cancel_launch:1234`、[Java manager](../ECL/java/manager.py) `install:342,_submit_once:366`、[connector.py](../ECL/services/connector.py) `host_port:656,host_instance:726,join:753,leave:850` |
| 原生窗口/IPC | [tauri.py](../ECL/host/tauri.py) `Adapter.run:51`、[registry.py](../ECL/api/registry.py) `dispatch:278`、[bridge.py](../ECL/api/bridge.py) `authorize_window_command:525`、[windows.py](../ECL/api/windows.py) `window_open:135`、[Capability](../capabilities/default.toml)、[Tauri.toml](../Tauri.toml) |
| 开发通道旁路 | [dev_channel.py](../ECL/host/dev_channel.py) 发现文件`:328`、`frontend.invoke:587` |
| 前端基线 | `frontend@6429f15` 的 `src/api/transport/tauri.ts:36`、`src/composables/usePluginBridge.ts:299,351`、`src/plugin-sdk/ui.ts:565`、`src/features/windows/PluginWindowShell.vue` |
| Core进程基线 | `ECL/game@467b58c` 的 `Core/InstancesManager.py` `create_instance:87,stop_instance:200` |

本机依赖源码根为 `D:\Projects\EuoraCraft-Launcher\EuoraCraft-Launcher\.venv\Lib\site-packages`。初始调研只读 Nuitka 引导、PyTauri 接口及 dist-info 元数据；批准实施后已执行真实冻结构建、独立测试 frame 和普通启动器验收，结果见第 18 节。

### 17.3 官方资料及精确上游源码

- [Windows Process Creation Flags](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags)：CREATE_SUSPENDED在ResumeThread前不运行主线程。
- [Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects) 与 [AssignProcessToJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject)：正常子进程继承、nested Job、breakaway、关闭句柄与非普通派生例外。
- [Windows进程创建与句柄继承](https://learn.microsoft.com/en-us/windows/win32/procthread/creating-processes)：显式句柄白名单与句柄关闭。
- [GetNamedPipeClientProcessId](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getnamedpipeclientprocessid)：由管道句柄取得真实客户端PID；它只提供连接进程身份，不单独证明插件运行会话或沙箱安全。
- [PyInstaller v6.22.3 pyi_main.c](https://github.com/pyinstaller/pyinstaller/blob/v6.22.3/bootloader/src/pyi_main.c#L202)：reset及parent process level；[pyi_utils_win32.c](https://github.com/pyinstaller/pyinstaller/blob/v6.22.3/bootloader/src/pyi_utils_win32.c#L573)：onefile Windows子进程创建。
- [PyInstaller运行时路径](https://pyinstaller.org/en/stable/runtime-information.html) 与 [子进程/冻结问题](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html)：sys.executable与复用解包环境的生命周期。运行测试以记录的实际版本源码为准。
- [PyTauri 0.8.0对应commit的原生命令](https://github.com/pytauri/pytauri/blob/734d5a1c7834935abdd39c85b53bf990a563febe/crates/tauri-plugin-pytauri/src/commands.rs)、[Python IPC绑定](https://github.com/pytauri/pytauri/blob/734d5a1c7834935abdd39c85b53bf990a563febe/crates/pytauri-core/src/ext_mod_impl/ipc.rs)、[默认权限](https://github.com/pytauri/pytauri/blob/734d5a1c7834935abdd39c85b53bf990a563febe/crates/tauri-plugin-pytauri/permissions/default.toml)、[JS pyInvoke](https://github.com/pytauri/pytauri/blob/734d5a1c7834935abdd39c85b53bf990a563febe/crates/tauri-plugin-pytauri/guest-js/src/index.ts)：共用pyfunc入口和函数头。
- [该commit的Cargo.lock](https://github.com/pytauri/pytauri/blob/734d5a1c7834935abdd39c85b53bf990a563febe/Cargo.lock)：用于定位上游native链，不作为发行二进制的构建证明。
- [Wry 0.53.1 WebView2实现](https://github.com/tauri-apps/wry/blob/wry-v0.53.1/src/webview2/mod.rs#L803)：window.ipc、浏览器Source及DocumentCreated注入；需结合实际版本测试frame行为。
- [WebView2安全指导](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/security) 与 [frame机制](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/frames)：来源校验、初始化脚本和frame原生消息的独立边界。
- [Tauri Capability](https://tauri.app/reference/acl/capability/)、[Isolation Pattern](https://v2.tauri.app/concept/inter-process-communication/isolation/)、[HTML iframe sandbox标准](https://html.spec.whatwg.org/multipage/iframe-embed-object.html#attr-iframe-sandbox)：配置、frame限制与其能力边界，不能替代宿主接口授权。

**v1.1 已明确获准实施；整体首版尚未完成。以下记录仅证明已验收的基础边界，不证明所有 Host API、用户权限管理或第三方 UI 已实现。**

## 18. 实施记录与前端原生增补（2026-10-12）

### 18.1 实际改动边界

- `main.py` 在导入 `ECL`、单实例和桌面服务前分流内部 Worker；错误的内部参数不会回退普通启动器。
- `ecl_plugin_api/` 是只依赖标准库的独立包，已落地 JSON 信封、大小/深度/Unicode 校验、内容快照、命名管道与加载引导。它还不是完整的公开 v2 Plugin SDK。
- `ECL/plugins/runtime/windows.py` 使用真实 `CREATE_SUSPENDED`、Job 分配与验证后再恢复；分配失败不恢复，关闭 Job 回收普通派生链，宿主异常退出由最后一个 Job 句柄关闭触发回收。它不是恶意代码的 OS 沙箱。
- `runtime/supervisor.py` 核实命名管道内核客户端 PID、保留的进程句柄、创建时间、实际映像和精确 Job 归属，完成握手后仍不运行插件。只有内部调用方给出内容快照后才能许可加载。
- `runtime/executable.py` 源码运行使用实际 base CPython 加 `-S`，避免 Windows venv redirector 的额外进程和未授权 site 导入。冻结模式复用当前原始发行入口；环境只复制白名单，PyInstaller 重置解包角色，Nuitka 不继承旧 onefile 角色变量。
- 现有依赖池不重新下载解释器；各 Worker 的导入 finder 将锁定模块优先解析到自己的依赖目录，同时提供对应 metadata/resources/DLL 搜索目录。标准库、SDK 和宿主保留名称冲突时拒绝加载。
- Nuitka、PyInstaller 与 setuptools 打包加入独立 SDK；Windows x64 发行 CI 增加真实 Worker 和依赖验证。

当前只开放引导验证方法 `runtime.info`；未实现的调用拒绝。**用户插件发现、安装、授权界面和业务 API 尚未接线，既有第三方插件仍走旧路径，本阶段不能宣称第三方插件已全部隔离。** 不自动把旧插件包装成 v2。

### 18.2 已取得的实测证据

| 范围 | 实际环境及结论 | 仍未证明的内容 |
|---|---|---|
| 后端门禁 | Python 3.11.9，Ruff check/format 全仓库及独立 SDK 通过；最新完整 pytest：1370 通过、7 跳过（显式启用真实 Nuitka 产物、原生 Wheel 和 WebView2 现状用例） | CI Python 3.12 及远端 CI 尚未运行 |
| 原生 Job | 挂起闸门、分配失败、普通子树、异常宿主退出和句柄清理的 Windows 实测通过 | broker/WMI 派生与恶意同用户攻击不在 Job 保证内 |
| PyInstaller onefile | 6.22.3，真实 EXE 启动、许可前不执行、许可加载、异常拒绝、退出及双版本/原生 Wheel 测试通过；实际冻结 VM 中运行入口解析器，得到同一原始 EXE | 本机仅 x64，不证明其他平台或任意 Wheel 可用 |
| Nuitka onefile | 4.2.2，MSVC 14.3，真实 EXE 完成相同 10 项引导与依赖验收，含实际冻结 VM 中的原始 EXE 解析 | 当前构建布局之外的解包/ABI 分支仍须按对应发行配置复测 |
| 依赖隔离 | 同名 idna 测试 Wheel 两个版本在两个 Worker 中并存，metadata/resources 一致，宿主 idna 未改变；共享池 xxhash 3.6.0 原生 Wheel 成功 | 不承诺共享文件不可被同用户写入，也不承诺不兼容 ABI 能运行 |
| 前端工程 | `pnpm check`：851 项通过、1 todo；`pnpm build` 通过；真实 PyInstaller 普通启动器页面和 PyTauri 桥就绪，验收进程已回收 | 不是所有新 UI 的验收，也不开放用户自定义 UI |

生成证据位于忽略的 `build/acceptance/`。冻结产物和测试缓存不提交。P00 完整方法 schema、P05—P19 均不能仅凭上表标记完成。

### 18.3 U09 实测失败及准确解释

复现用例为 `tests/test_plugin_webview_gate.py`，辅助壳为 `tests/plugin_webview_gate_host.py`。启用 `ECL_WEBVIEW_GATE=1` 后运行真实 PyTauri/PyTauri-wheel 0.8.0，WebView2 Chromium/Edge 154；Capability 仅允许本测试窗口的 pyfunc。窗口标签必须用 `plugin-v2-gate`：实际 Tauri 二进制拒绝带点的标签，因此生产标签前缀统一调整为 `plugin-v2-`。

真实结果：顶层合法请求被接受 1 次；sandbox iframe 不能读取顶层 DOM 或凭据。iframe 中存在 PyTauri/Tauri 注入入口，其两个错误凭据调用在观察窗口内未到达业务处理器，但表现为 timeout，**不能据此断言原生 IPC 永远拒绝或完整 U03—U08 已通过**。

iframe 执行 `location.href` 导航至本地 canary 后，服务器收到 `/unapproved-navigation` 1 次。Python `on_navigation` 只观察到顶层 `http://tauri.localhost/`，未见子 frame 导航。即使 CSP 为 default/connect/frame deny 且 sandbox 仅 allow-scripts，这个网络出口仍成立。报告明确保存 `network_gate_passed=false`。

测试中的现状复现断言通过只表示此限制可稳定观察，**不表示 U09 安全验收通过**。当前生产 Capability、前端和用户插件运行路径均未按这个测试壳修改。

### 18.4 用户选择及下一道审阅门

用户已选择“增加原生 WebView2 frame 导航与请求拦截，再开放自定义前端”。具体新增绑定、发行依赖及测试方案见 [原生前端隔离增补方案](plugin-native-webview-isolation-design.md)。它不改成另一启动器框架，也不把实验 Wheel 安装到现有全局/主项目环境。

原生增补已经获得详细方案批准，随后用户要求先越过这部分。当前暂停这条未通过门禁的自定义前端路径；已批准的 Worker、权限与 Host API 方向继续有效，不开放 `ui.custom.execute`。

## 19. P05 授权存储实施记录

`ECL/plugins/grant_models.py` 与 `grant_store.py` 新增独立 v2 授权服务，不转换旧 PermissionManager 的声明，也未改变既有插件运行路径。永久记录按宿主安装身份及完整内容摘要分别保存；必要/可选声明、实际决定、精确资源范围、期限、审阅时间、持久化 revision 和运行 epoch 均分开建模。

内容身份包括全代码快照、规范化权限清单、完整锁字节及目标 ABI；归档准备结果先通过既有清单校验，再建立联合摘要。无授权、未实现能力、必要资源不完整、未知主体、损坏或回退记录均拒绝。原生自定义前端不在已实现能力目录中，保存用户 allow 也不能使其执行。

永久许可只针对本安装/摘要；会话许可只保存在当前 run 内存中，切换为 session 会删除对应永久允许。拒绝始终持久化，不能通过 session-deny 在重启时恢复旧永久允许。到期发布新 epoch；本宿主一旦发现到期，时钟回拨不会让该运行重新获得许可。

任何授权更新使本内容所有运行的旧准入记录失效。拒绝屏障在原子替换前建立；写入失败保留旧磁盘文件，同时当前宿主中的所有对应运行保持 deny，只有显式重试审阅更新才恢复。**保存失败不表示新撤销已持久化：应用重启后仍可能读取之前成功保存的记录；界面必须报告真实保存失败，不能宣称永久撤销成功。** 任务取消、提交互斥和 Worker 停止尚待 P06/P08 接线。

更新使用服务锁及有界跨进程文件锁，以 expected_revision 拒绝覆盖其他进程的新决定；查询检测外部新版本，损坏、同版本内容变化和版本回退时建立屏障。Windows 使用受控绝对长路径形式，避免完整摘要及原子临时文件名触发 MAX_PATH，不截断授权身份。

管理服务只接受宿主传入的 main-webview 入口分类；Worker、Dev Channel、unknown 和伪造字符串均拒绝。正式管理 IPC 尚未注册，后续必须由真实原生上下文生成该分类，不从请求体读取。安装身份的持久化、卸载/回滚资格、管理界面、API 审计历史与任务实际取消仍需后续切片，不把这些基础模型称为完整用户权限功能。

验证：`tests/test_plugin_grant_store.py` 覆盖默认拒绝、永久/会话、跨运行、精确资源、到期、保存失败、记录损坏、身份/摘要/锁/ABI 变化、真实跨进程锁等；相关回归 79 通过、2 跳过。完整后端 1406 通过、13 跳过；Ruff check/format 全仓库与 SDK 通过。Windows 符号链接权限不足的真实链接测试由 POSIX CI 分支继续验证，不能宣称本机已通过。

后续推进 P06 的生命周期、双向 RPC 与最小 Host API，并在接入第三方分流时按 P07 关闭旧进程内分支。用户已明确授权：全部改动完成并通过验收后推送；此前不推送阶段性实现。

## 20. P06 生命周期与双向 RPC 基础

已实现 `ecl_plugin_api.Plugin/HostClient/HostApiError` 的标准库轻量入口。插件仅接收当前运行的 HostClient，不取得 Launcher、PluginManager、EventBus、配置或桌面 Python 对象。同步及 async 生命周期、同步及 async 本地回调均由 Worker 执行；函数不跨进程传输。

RPC 接收与回调执行分离，使用有界等待表、执行队列及线程数；等待回调结果期间可以处理反向 Host 请求。SDK 生成 host/worker 独立的单调请求标识，接收方拒绝重放和倒序；标识不决定身份或权限。JSON 大小、类型、错误信封均校验。超时移除该等待记录，迟到响应不能覆盖另一请求；关闭唤醒等待方，不宣称未收敛的处理器已经停止。

HostClient 为提交产生本运行去重键，明确错误抛出 HostApiError，不自动重试；去重回执和业务提交语义尚需 Broker/任务切片实现。底层 RPC 不把超时或收到停止确认当成任务实际取消。

`WorkerSupervisor.authorize_load` 现要求宿主持有的 PluginGrantSession、本运行身份、已审阅记录和必要许可满足；没有业务权限声明的插件仍须显式审阅记录，revision=0 不执行入口。原先用于冻结、内容和依赖验证的原始加载方法改为私有 `_authorize_snapshot`，只供内部风险门禁用例，不作为用户插件加载通道。引导消息明确区分 `code_digest` 与授权联合 `content_digest`。

许可执行的代码使用本 Worker 的包命名空间，支持相对导入和资源读取；生命周期按状态校验，不在状态锁内运行用户代码，避免嵌套 RPC 死锁。旧基类不适配，生命周期错误不输出插件私有异常文本。关闭后不能用同一监管对象重启旧 run。

实测：SDK RPC 17 项通过；PyInstaller 实际发行物的生命周期/引导/依赖 17 项通过；Nuitka 实际发行物与全量后端 1435 项通过、8 跳过，Ruff 全仓库及 SDK 通过。真实 Windows 子进程覆盖审阅/必要授权前不执行、不同 run 拒绝、同步/异步钩子、反向调用、相对导入、资源读取、失效回调和旧基类拒绝。

本阶段结束时尚未接入用户插件管理器，默认 Host 分派拒绝全部未接线方法。后续接入结果见第 21 节；其余业务适配和自定义前端仍需后续验收，不能据本章宣称首版整体已完成。

## 21. 第三方管理器与最小 Host API（2026-10-20）

按 [管理器接入实施方案](plugin-worker-manager-integration-plan.md) 落地持久化安装身份、严格 API v2 清单、目录及归档安装只登记、内容绑定审阅、独立 Worker 生命周期和运行清理。系统插件继续使用可信发行来源；用户目录不能借同名清单获得系统身份。旧第三方 SDK 不适配、不回退宿主执行、不迁移旧授权和设置。未支持平台明确拒绝第三方运行。

`PluginWorkerService` 维护唯一运行状态、授权会话及监管对象，监控实际 Worker 句柄、断连、超时和到期。必要授权撤销停止运行，可选授权撤销清理对应注册；授权先阻止下一次提交，已接受的宿主写入仍按实际结果完成。进程停止与宿主在途提交分别收敛，未结束时报告 cleanup pending，不能提前报告清理完成。

本阶段 `PluginHostBroker` 开放本安装的命令、设置、自查询和自身设置变化订阅，校验严格 DTO、授权 epoch、回调归属和输入输出；有界回执按 mutation_key 去重，不自动重放副作用。后续实例只读能力见第 22 节。完整契约子集、回执查询及限制见 [包格式和 API 说明](eclplugin-package-format.md#第三方-api-v2)。

授权管理 IPC 从原生注入的已登记主 WebView 识别调用主体，拒绝 Worker、其他窗口、Dev Channel、未知入口及请求体伪造。第三方脚本、Vue、HTML、CSS 注入仍关闭。前端提供六语权限审阅、永久/会话选择、撤销、实际清理状态与失败重试；保存审阅与启用分开操作。

源码、真实冻结与桌面验证记录集中在 [本轮验收记录](plugin-worker-manager-integration-plan.md#10-实施与验收记录)，不复用第 18—20 节的历史数字作为当前证据。OS 文件/网络沙箱、账户/实例等业务 API、声明式页面和自定义前端均不由本切片完成。

## 22. P07 实例只读能力（2026-10-20）

按用户批准的 [实例只读实施方案](plugin-instance-read-api-implementation-plan.md) 开放 `resources.list`、`instances.scan`、`instances.get`。清单仅请求 `instances.read`，可信主界面明确选择整个目录（含以后新增实例）或单个实例；不默认批准任何资源。宿主持久身份与每次运行引用分离，物理目录替换、配置移除、授权变更、内容或 run 变化使旧引用及游标失效。同名重建不继承旧单实例授权。

查询从已验证实际文件句柄有界读取本地实例元数据与资料，调用 Core `InstanceMetadata.from_documents` 纯解析；不复用会写入的桌面扫描链路，不初始化统计或外部提供方，不停止游戏。摘要显式投影，使用 `profile_etag` 表达本地公开资料快照；不返回路径、账户、完整配置或持久资源 ID。`permissions.list` 的实例资源亦转换为运行引用。

可信 `plugin_permission_resources` 提供按目录展开的有界候选；保存同时检查内容摘要、授权 revision 与资源目录 revision。所有读取和最终交付均复核许可及物理身份，扫描 30 秒、单项/资源列表 5 秒，分页最多 16 个快照、120 秒、每页 1—100 项且结果受 64 KiB 限制。完成的单页查询立即释放分页容量。

源码、真实冻结与 WebView2 验证见 [本轮验收记录](plugin-instance-read-api-implementation-plan.md#9-实施与验收记录)。这只完成 P07 的实例读取切片；其他查询、真实业务任务提交、实例写入和自定义前端保持后续推进；自身任务控制基础见第 23 节。

## 23. P08 任务归属与提交控制基础（2026-10-21）

按用户确认的 [实施方案](plugin-operation-ownership-implementation-plan.md)，复用 ApplicationContext 的唯一 OperationManager，以不可变安装/内容/run 来源隔离任务。插件层只维护运行引用和结果投影，开放 operations.get/list/cancel；首个生产任务为第 24 节的私有文件新建，下载、Java、Minecraft 等实际适配仍待后续切片。控制没有新业务权限，敏感结果逐次核验显式的原始读取准入，旧 run 不从审计文件恢复控制权。

执行阶段和副作用由任务服务统一维护，桌面五态保持现行投影。取消请求不会掩盖真实异常；提交接受排序在短授权锁和任务锁内完成，业务动作与落盘均在全局锁外执行。设置短写与命令、定义、订阅注册使用同一授权接受边界，保持原同步结果及去重回执；W5 全部业务统一 TaskReceipt 不由本切片完成。

插件任务摘要存入 operations/plugin，按每任务写锁获取最新 revision 后原子保存，不包含完整参数、路径和结果。提交前保存失败不执行动作；动作完成后的保存失败保持真实结果、收尾 pending，只重试摘要。Worker 停止后等待本 run 的实际 future 与审计保存，pending 时拒绝重新启用，完成后释放引用和记录。不会自动重放、迁移或删除用户资源。

源码、真实 Worker、冻结产物与主窗口验证结果集中在 [本轮验收记录](plugin-operation-ownership-implementation-plan.md#10-实施与验收记录)。

## 24. P09 A 私有文件与新建事务（2026-10-21）

按已确认的 [实施方案](plugin-private-files-implementation-plan.md)，开放当前安装固定私有空间中的 stat/list/read 和新文件分块事务。原始块 32 KiB、单文件 8 MiB、时间为 modified_ms；FileRef 只支持平铺 ASCII 名称。读写声明及决定的资源范围必须为空，实际根和精确目标由宿主绑定。首次只读不创建空间；首次授权写入使用独立 preparing/ready 记录，损坏和失踪空间明确拒绝，不自动接纳或迁移。

PluginWorkerService 维护共享存储、上传占用和配额；RuntimeFiles 维护本 run 的代次引用和严格请求。Windows x64 本地 NTFS 通过目录句柄相对创建和原生不覆盖重命名，拒绝重解析和硬链接，无 Path.replace 备用路径。新暂存先登记句柄所有者再校验元数据，校验或清理失败仍由同一上传记录负责，实际释放之前不提前归还配额。

files.write_commit 是首个生产任务入口，返回 queued 接纳回执。最终任务状态沿用 P08，执行绑定原始写 lease，结果必须显式配对独立原始读 lease。快照在发布前捕获，发布后只关闭句柄；后续读取使用 expected_etag 复核当前版本。abort 无需新增回执或业务权限，只减少自己的上传；交给任务之后使用自身任务取消，已接受发布不会被撤销改写为取消。

任务资源清理属于准入生命周期，排队取消、审计失败和工作线程结束均执行，失败仅重试清理和保存，不重做发布。成功清理能确认 staged 消失，不能覆盖 committed/unknown。固定配额计入活跃预留、已有文件和崩溃残留，不自动清扫旧文件。覆盖、删除、子目录、共享空间、实例资料写入和跨重启暂存恢复仍待单独审阅。

现行调用说明与示例见 [归档文档](eclplugin-package-format.md#私有文件与新建事务) 和 [worker_snapshot](../examples/plugins/worker_snapshot/README.md)。源码、两种冻结发行物、真实 WebView2 权限显示与本地交付结果集中在 [P09 验收记录](plugin-private-files-implementation-plan.md#11-实施与验收记录)。

## 25. P10 A 受控只读网络（2026-10-21）

按用户选择并确认的 [实施方案](plugin-network-read-implementation-plan.md)，开放精确站点的 GET/HEAD。NetworkOrigin统一声明、决定和URL；可信管理PermissionItem新增requested_resource_refs，弹窗显示最多16个站点，整组允许覆盖全部路径。selected与effective范围保留各自语义，不增加旧字段适配。六语言同步。

PluginWorkerService拥有唯一PluginNetworkService，每个Broker持有RuntimeNetwork。每run同时2个请求，宿主8个请求、4个真实DNS工作者；不创建OperationManager任务、请求缓存或重试。解析在有界守护工作者中执行，超时不释放实际DNS名额，不无限join；原请求实际返回、DNS结束和全部流关闭后才释放请求。Broker空闲、停止pending及重新启用门禁纳入网络资源。

HTTPcore公开NetworkBackend/NetworkStream仅向已准入数字IP连接，发送HTTP前核实peer；TLS验证、SNI和Host保留原站点。每跳独立池，HTTP/1、无代理、无复用、无Cookie。租约接受点仅记录有限动作，真实I/O在锁外；总30秒/单阶段5秒，原始收包及完整实体分别限额。最终清理后再次检查原租约与截止，撤销不通过新租约恢复旧请求。

网络调用作用域在格式化前过滤httpcore/httpx原始日志，DNS显式传播上下文，不影响其他任务；异常只记录稳定码或安全堆栈位置。公开示例、契约与验收见 [归档文档](eclplugin-package-format.md#只读网络请求)、[worker_network_read](../examples/plugins/worker_network_read/README.md) 与 [P10验收记录](plugin-network-read-implementation-plan.md#12-实施与验收记录)。下载任务和实例写入继续独立审阅。
