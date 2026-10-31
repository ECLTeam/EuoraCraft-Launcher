# 第三方插件 P12：游戏目录、安装与进程控制

现行权限调整见[权限简化方案 B](plugin-permission-simplification-plan-2026-10-30.md)；本文件保留 P12 当时的实施与验收记录，独立游戏许可说明不再作为新实现依据。

状态：用户于2026-10-26明确批准开始实施、无人值守验收和提交推送，2026-10-30继续工作并取消关机。实现与本机验收已完成；本文件记录现行实现，替代原草案中的历史接口与行号。远端交付以实际Git和CI结果为准。

基线为主仓库 `510ebe5`、前端 `43d51d7`、Core `e8aacb5`。授权不包含创建发布标签或Release；必要检查或验收存在未解决阻碍时保留现场，不强行推送。

## 1. 目标和范围

让第三方Worker使用宿主当前有效来源查询版本和加载器，在用户选择的游戏根中安装新实例，启动获准的具体实例，查询与停止自己本次运行创建的确切进程。可信任务中心显示真实阶段、下载量和取消结果。

开放六个固定方法：`game.catalog`、`game.loaders`、`game.install`、`game.launch`、`game.running`、`game.stop`。四项新增能力为 `game.catalog.read`、`instances.install`、`instances.launch`、`instances.stop`，保留独立 `instances.read`。

不包含卸载、删除、复制、整合包操作、账户读取、任意网络地址、第三方启动钩子或任意命令。当前分支没有第三方Java使用租约API，严格拒绝 `runtime_ref`，不借P12扩展P11。启动只接受有限的内存、宽、高和全屏配置。第三方执行及实际目录写入维持Windows x64、本地NTFS范围。

## 2. 权限及资源

- 清单只声明能力，不指定宿主持久化资源标识。可信主窗口分别选择安装根、可启动实例及可停止实例。
- 安装只批准根目录；启动和停止只批准具体实例。读取根可以覆盖以后新增实例，不能据此派生写入或进程控制许可。
- 运行内只使用随机资源、任务和进程引用；拒绝跨安装、跨内容摘要或跨运行复用。授权变化使原租约失效，重新允许不会复活旧任务读取结果。
- 资源目录、GrantStore、API类型、六语言权限弹窗和运行适配器同步更新；不增加历史别名或自动迁移。

## 3. 查询

目录沿用GameService既有来源解析和Core查询，不接收URL、源配置、代理、Java路径或账户。只交付 `version_id/type/release_time` 和 `loader_version/is_latest`，不返回下载地址、摘要或底层原始对象。

分页默认50、每页1—100、总量最多10000，游标绑定运行、完整查询及授权代次，120秒过期。RPC和结果交付都核验原许可与截止，单次最多30秒。加载器降序与安装最高版本选择共用比较规则，NeoForge 1.20.1仅在比较时移除Maven游戏前缀，安装保留原串。

## 4. 安装

`RuntimeGameInstall` 只负责请求、原始许可、目标占位和结果投影。`PluginGameCoordinator` 组合到应用已有GameService中，供当前实际调用方使用，不增加执行器、下载器或另一套任务状态。

1. 严格校验根引用、游戏版本、加载器和新实例名称，拒绝路径、ADS、设备名、尾点和归一化歧义。
2. 准入时按根物理身份和实例名取得共享安装占位，Windows名称按大小写折叠比较。普通界面、整合包基础安装和插件共用此占位。
3. 使用原生目录句柄固定根、versions及目标实例，并在阶段边界复核原始许可、取消和物理身份。
4. 复用 `install_blocking`、Core安装器与 `InstallationProgress` 权威快照；显式加载器版本须存在于对应目录，缺省时沿用最高兼容版本选择。
5. 不覆盖已有用户实例；只允许同一运行对先前创建且物理身份、版本和加载器请求完全一致的未完成目标继续。
6. 失败文件保留，写入后失败如实报告partial；完整结束才committed。完成结果只在原独立读取根许可仍有效时投影为InstanceView。
7. 实际线程退出和原生句柄关闭前保留占位，关闭失败由既有任务清理监管重试。普通安装首次执行前取消也通过任务完成回调释放占位；回调不误删后来的占位。

安装接纳RPC最多5秒，总截止1800秒，含排队。取消沿用已有shield等待真实工作线程退出，不恢复草案中的abandon-on-cancel实现。

## 5. 启动、查询及停止

启动复用既有异步流程、账户HTTP客户端和宿主Tauri事件循环。适配器工作线程通过run_coroutine_threadsafe等待真实执行结束，不另建事件循环访问共享认证客户端，也不把future取消视作实际停止。

1. 取得具体实例的独立读取和启动原始许可，准入时占用全局单启动位置；用户和插件互相返回忙。
2. 保持目录句柄，执行既有文件检查与补全。宿主选择当前账户、Java、有效下载源及游戏语言。
3. 在进程创建前复核取消、原始许可、物理身份及当前账户；短进程创建动作置于OperationContext.finalize内，接受提交后如实committed。
4. GameService的正常运行记录和插件索引引用同一个RunningGame对象，不维护两份可独立修改的进程状态。后续统计失败仍保留已创建进程的用户可见性和所有者记录。
5. Worker只得到GameRunView的随机引用、实例引用、PID、running和实际exit_code。最多保留128条本运行记录，不交付其他来源进程。
6. 停止需独立实例停止许可，只操作同所有者、同token、同Core实例且相同Popen对象，不通过PID或名称猜测。有限等待后仍未确认退出则明确失败，重复停止已退出的自身进程幂等成功。
7. 插件关闭或进程创建后撤权不会杀游戏；正常游戏管理仍保留用户控制权，新Worker不接管旧引用。

启动接纳最多5秒、总截止180秒；停止接纳最多5秒、总截止30秒，提交动作内先terminate并有限等待，必要时kill后再次有限等待。提交前准备可能已经补全文件，未创建进程也可能产生partial，不伪造无副作用。

## 6. 任务中心和失败

继续由应用唯一OperationManager拥有任务。三类插件游戏任务通过现有主窗口事件显示，Worker只通过自己的operation_ref查询和取消。业务stage来自执行方；安装详情使用同一InstallationSnapshot，没有额外推算的总体百分比。

主窗口事件显式隐藏内部安装/进程结果，避免dataclass、路径或Popen进入前端协议；Worker结果逐字段构造并复核原读许可。权限、任务状态、启动准备和停止说明同步六语言。稳定错误包含目录版本无效、安装或启动忙、缺少账户、账户变化、外来进程及停止超时；不把接纳或取消请求显示成成功或已停止。

## 7. 文件及调用边界

| 范围 | 主要实现 |
| --- | --- |
| 不可信请求与有限选项 | runtime/game_models.py |
| 目录与分页 | runtime/game_queries.py |
| 安装和进程适配 | runtime/game_install.py、game_launch.py |
| 共享领域执行 | services/game/plugin_games.py、install_targets.py及既有install/launch/base |
| 独立授权与物理范围 | grant_models/store、resource_catalog、runtime/resources、worker_service |
| 当前应用绑定 | application、PluginManager、Tauri宿主 |
| 真实任务与投影 | services/operations、runtime/operation_adapters |
| 可信权限及任务UI | PluginPermissionsModal、PluginResourceSelector、TaskQueuePanel、六语言 |
| 公开示例 | worker_game_install、worker_game_launch |

## 8. 验证与交付

这是IPC、共享状态、并发、安装和权限边界变更，执行全量门禁。

- 后端：Ruff全量检查及格式、主仓库全量pytest、Core自身测试与主仓库集成回归。
- 前端：pnpm check及pnpm build；权限独立选择、目录/实例范围、三类任务和业务阶段回归。
- 真正Windows子进程：重复提交单次创建、外来引用拒绝、撤权/取消提交竞争、账户变化、退出保留、句柄清理失败继续占位、关闭插件保留游戏。
- 真正Worker：源码、PyInstaller和Nuitka发行入口，公开示例完整经过RPC，不把Broker直调用例视作Worker验收。
- 实际启动器：独立数据、账户及游戏目录，通过可信原生主窗口授权和插件命令真实安装Fabric与Forge，再启动游戏至渲染/音频初始化并停止。只复制既有公共缓存，不改原游戏或账户。
- 实际WebView2：Classic/Folia、亮暗主题、六语言、背景/透明/模糊、960×600最小窗口、默认及放大窗口；核验原生窗口/客户区/WebView尺寸、权限列表末项和操作按钮可达。
- 完成检查后提交前端并集成本地main，再提交主仓库及其指针；逐个核验所有gitlink远端可达，先推子模块再推主仓库，干净递归检出及本次CI全部通过后关闭本次测试进程，保持电脑开机。不创建发布标签或Release。

本机验收结果如下。P12不代表总体插件方案完成，P11与P13以后切片仍需按当前代码另行核实范围。

### 本机验证记录（2026-10-30）

本次属于IPC、权限、安装写入和共享并发状态变更，采用全量门禁；后续仅权限说明文案修订按局部改动重跑受影响检查。

- 后端 `ruff check ECL tests`、`ruff format --check ECL tests`、根目录Ruff检查通过。当前Ruff命令使用 `--output-format github`，等价于规范中旧版 `--format github` 参数。
- 查询修复后的主仓库全量pytest为2519通过、33跳过；跳过包括POSIX专用分支与需要显式冻结产物的测试，不把Windows本机通过泛化为其他平台通过。
- Core子模块Ruff及主仓库中的Core测试通过，41项通过；本次未修改Core指针。
- 前端全量 `pnpm check` 为182个测试文件、1092通过、1个todo；最终权限提示修订的Prettier、ESLint、组件回归10项及含类型检查的 `pnpm build` 通过。构建只有已有的分块体积提示。
- 最终PyInstaller和Nuitka单文件产物均构建成功；分别显式传入真实产物执行Worker引导、依赖、生命周期、安装与启停测试，各57通过、3跳过。包含公开示例的真实RPC、确切子进程停止及关闭Worker保留游戏；不将其描述为打包主窗口全流程验收。
- 实际源码启动器使用独立数据、账户、游戏目录和WebView2配置。288组权限布局检查覆盖Classic/Folia、亮暗、六语言、背景/透明/模糊及960×600、1200×800、1440×1000客户区，核对实际原生窗口和WebView尺寸；另补六语言下启动/停止实例提示与末项可达验证。
- 真实Fabric安装暴露同实例目录内基础版本声明的读取问题，登记[#72](https://github.com/ECLTeam/EuoraCraft-Launcher/issues/72)。修复前回归失败，修复后同目录父声明、损坏文件保留失败及安装结果读取通过；不扫描其他实例、不回退损坏声明。
- 外部下载曾因少数公共资源或Forge依赖超时失败，确认任务报告partial并保留文件；按官方元数据SHA-1校验补齐独立验收缓存后，以新去重键重试同一运行创建的目标。原游戏目录和账户不参与写入。
- 实际Worker在获准根中完成Minecraft 1.20.1的Fabric 0.20.1和Forge 47.4.28安装，完成结果可正常读取。Forge真实处理器执行成功；随后通过启动示例创建Fabric游戏，核对可见原生窗口客户区854×480，并等待渲染/音频初始化。
- 停止示例操作同一运行的确切游戏引用，任务完成、effect为committed、cleanup_pending为false，实际running为false且观察到退出码1（请求终止后退出）。验收没有用接纳回执代替安装、启动或停止成功。
