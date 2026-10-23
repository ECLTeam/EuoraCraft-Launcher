# 插件实例资料受控修改实施方案

日期：2026-10-22 制定，2026-10-23 实施（Asia/Shanghai）。状态：用户已确认方案A并授权实施；现行接口已接线，验收与本地交付记录见第9节。

方案调研基线：主仓库及原工作区本地main为 `850bf97`，前端 `ffffd04`。接续 [实例只读接口](plugin-instance-read-api-implementation-plan.md)、[任务归属](plugin-operation-ownership-implementation-plan.md) 及 [私有文件替换](plugin-private-file-replace-implementation-plan.md)。私有文件替换已获准的取舍不自动扩展为修改共享实例的授权。

## 1. 目标与可选路线

目标：插件只能修改用户明确选定实例的别名、描述、收藏及标签；版本冲突、撤权或失败时保护原资料，不修改游戏配置、存档及第三方启动器文件。

| 路线 | 行为与代价 | 建议 |
| --- | --- | --- |
| A：原对象备份、不覆盖发布、可恢复任务 | 先核对完整资料版本，再保存事务凭据、移动原对象、不覆盖发布；外部竞争保留后来文件与原备份。需要共享资料写入协调、持久恢复和管理界面；原名称可能短暂缺失 | 推荐，先证明共享实例路径的原生边界，再接线开放 |
| B：应用内乐观并发与原子替换 | 共用资料锁、版本检查及现有atomic_write；改动较小，能协调本进程的桌面/插件修改 | 不能防止外部写入插入最后检查与replace之间，不作为A的失败回退 |
| C：暂缓共享资料写入 | 保持只读，转向Java查询/安装等其他切片 | 数据边界小，但不能更新实例资料 |

A不承诺一次原子覆盖。旧 `instance.json` 移到事务备份与新文件发布之间，名称可能暂时不存在；进程异常时可能持续到恢复。宿主自己的资料读取和修改需要共用协调，不能在此窗口误读为首次未设置。成功后的旧版本不提供历史撤销。

相比此前“短写接口”的目录草案，本方案推荐返回任务回执：移动原对象后可能需要实际清理、重启恢复和用户处理，不应把一次RPC返回或等待超时解释成文件已保存或没有副作用。

## 2. 当前代码与已复现的问题

| 位置 | 当前行为 | 本切片必须解决 |
| --- | --- | --- |
| `services/game/instance_profiles.py` | 单一InstanceProfileStore及RLock，patch/reset/icon/reorder均写 `.ecl/instance.json`；read_profile损坏时返回默认值，过滤未识别字段 | 严格更新快照；所有同文件写入共用锁和待恢复检查；不得从默认值覆盖损坏原文件 |
| `services/game/instance_queries.py` | profile_etag仅对alias/description/favorite/tags的公开投影求摘要 | 不能把当前值直接作为整份JSON的并发版本；需同时更新读取及写入契约 |
| `services/game/profiles.py` | 桌面资料入口完成写入后失效扫描缓存、发送versions_changed | 插件复用同一领域协调；通知失败不能使已提交写入被重新执行 |
| `utils/instance_reading.py` | 读取前后验证目录及实际文件句柄，支持Windows/POSIX读取 | 是读取边界，不是可直接用于写入的目录相对原生提交设施 |
| `plugins/runtime/resources.py`、resource_catalog | 只实现instances.read，运行引用绑定物理身份和读许可；只读目录许可可覆盖未来实例 | 写许可独立选择具体实例，不能因读目录许可推导整个目录写权限 |
| grant_store、worker_service、管理IPC | 实例候选、资源校验、失效监管目前专门处理instances.read | 生产方和消费方同步扩展write范围及代次校验 |
| PluginPermissionsModal、PluginResourceSelector | 只有一组读资源选择，可选整目录 | 分别维护读/写选择；写选择只允许具体实例 |
| file_handles、file_replacement | 已验证私有根内原句柄移动、不覆盖发布、备份恢复及实际清理 | 只能复用低层原语；不能把共享实例塞入私有空间、借用filesystem权限或照搬安装内事务模型 |

在Git忽略目录 `build/worker-manager/profile-plan-probe-d4dd32157581437d8120d19c316b177f` 中，只使用人工实例调用当前真实服务，复现三件事：

1. pinned字段变化后当前profile_etag不变，说明它不是整份资料的写入版本。
2. 损坏JSON交给当前patch_profile后，会被默认资料加本次patch替换；插件写入不能直接委托这一流程。
3. 在read_profile返回旧快照前注入外部修改，现有进程内锁和atomic_write仍会覆盖后来外部字段。

上述是现状风险复现，不是新功能验收。现有实例资料与插件读取基线为34 passed、1 skipped，2.09秒；日志在 `build/worker-manager/profile-plan-baseline.log`。没有操作真实游戏资料、建立历史数据迁移或改动前端。

## 3. 首轮范围与授权

- 新增独立 `instances.write`。清单不能指定本机资源ID；用户在可信主窗口逐个选择已发现的具体实例，不提供写入整个目录及未来实例的选项。
- 写许可不隐含读许可。首轮patch要求对同一物理实例同时具有有效instances.read和instances.write；读可来自已批准目录或单实例，写必须是该具体实例。两者分别审阅、撤销和校验，不自动合并范围或补授read。
- 只开放alias、description、favorite、tags。alias去首尾空白后1—120字符；description至多1000字符，空字符串表示显式空描述；favorite严格bool；tags至多20项、每项去空白后1—40字符，沿用所属领域的大小写去重规则。
- patch必须至少包含一个允许字段。拒绝null、额外字段、类型转换、路径、owner、force、overwrite及客户端自报的持久资源ID。未提交字段保持原值，false和空标签列表必须真实保存。
- 不开放重置自动值、图标/封面、分类、隐藏/置顶、排序、外部资料来源、启动参数、游戏目录、存档和任意JSON覆写。这些字段仍需完整保留，不能因本次改四个字段而被过滤丢弃。
- 第三方执行仍限Windows x64，本轮插件写入进一步要求本地NTFS；其他文件系统明确拒绝。桌面既有POSIX资料功能不因插件写入原生门禁而禁用，也不增加POSIX第三方运行支持。

## 4. 拟定接口与版本

```python
before = host.call("instances.get", {"instance_ref": instance_ref})
receipt = host.call("instances.profile_patch", {
    "instance_ref": instance_ref,
    "expected_profile_etag": before["profile_etag"],
    "patch": {"description": "朋友服务器", "favorite": True},
}, mutation_key=saved_key)
# receipt为{operation_ref,state:"queued"}，后续查询operations.get。
```

运行引用仍来自现行只读发现/扫描；持有引用不等于有写许可。提交和实际执行分别解析引用，校验原根、实例身份、具体写选择及原始双lease。跨run、跨安装、实例同名重建、资源退休及任何旧授权代次都不能继续写。

profile_etag保持不透明64位小写十六进制，改为绑定实例与完整本地资料快照：实例身份、`.ecl`存在状态与身份、资料存在状态、文件物理身份、稳定元数据及完整原始字节SHA-256。文件缺失与已存在但公开字段相同的文件必须不同；其他字段、同名同内容重建也应使令牌失效。不把运行数量、扫描缓存或第三方外部资料纳入本地版本。

在现有InstanceProfileStore所属模块新增明确的资料快照/增量模型，由实例查询及写入共同使用，不维护第二份revision文件。已有profile_etag的语义、类型、调用方和测试同步更新；移除总设计中从未开放的expected_revision/profile revision草案，不添加别名、双格式读写或迁移。

首次确实缺少资料是未设置；损坏JSON、重复字段、未知显式schema版本、类型异常、超64 KiB或读取失败必须保留原文件并明确拒绝更新。当前可读资料中schemaVersion未设置的情况作为同一契约中的可选字段处理，不扫描或改写既有文件补版本。合并结果也不得超过64 KiB；保留合法的未修改字段，不执行外部兼容提供方或统计同步。

提交RPC最多5秒，仅校验、绑定来源并登记有界任务；不在RPC内修改正式资料。任务 `plugin_instance_profile_patch` 从提交起最多30秒，含排队，沿用应用唯一OperationManager及既有插件任务限额，不创建执行器、队列或重复状态表。稳定mutation_key只接纳一次；等待超时查mutations.get，不换键重放。

任务结果为无路径的 `{instance_ref,alias,profile:{description,favorite,tags},profile_etag}`，只从已提交快照投影，交付时再次核对创建时绑定的read lease及资源身份；失权后result为null，effect不改。写或读在接受前失效则不提交；接受后撤权、停止Worker或取消等待不伪造回滚。控制自身任务不要求恢复业务许可。

## 5. 路径、提交和恢复

### 5.1 原生边界先行

低层Windows目录相对句柄原语从 `plugins/runtime/file_handles.py` 移到主仓库公共工具位置（现为 `utils/windows_file_handles.py`），同步更新全部私有文件调用方和测试，删除旧模块，不保留转发。真实调用方是现有私有文件服务与新增实例资料事务；不建立可插拔文件系统框架。

实例写入从宿主批准的根对象逐层相对打开versions、具体实例及`.ecl`，拒绝重解析、ADS、目录伪装、硬链接、只读及无法核实的对象。不得用Path.resolve/check后普通路径replace充当安全提交。目录目录项和文件首次句柄校验前登记资源归属，失败仍有关闭路径。

资源目录的DirectoryIdentity与原生FileIdentity是不同模型，必须以真实句柄验证其对应关系，覆盖当前Python 3.11和支持环境；不能把device/inode与volume/index未经验证地强制相等。原根/实例/`.ecl`替换及父级链接竞争属于开放门禁；证明不足则保持instances.write未实现，不退回方案B。

### 5.2 唯一资料写入协调

InstanceProfileStore继续是资料权威维护者，新增实例资料事务组件（拟 `services/game/instance_profile_transactions.py`）只拥有原句柄、暂存、备份和恢复记录，执行状态仍归OperationManager。

资料读取、桌面patch/reset/icon/reorder及插件修改共用既有资料锁和待恢复检查。严格更新读取必须在图标复制/删除、排序等任何前置副作用之前完成，避免先改图片再发现资料损坏。查询快照不执行写入、统计或外部资料回调。

桌面现有操作仍使用所属领域入口，不由插件反向调用桌面IPC。Windows插件事务中的短暂缺名不能被宿主读取当成首次未设置；持久待恢复时，受影响实例的资料修改明确拒绝，其他实例和资料以外功能继续可用。锁只覆盖有界执行步骤，等待外部删除确认或用户恢复时释放锁并保留具体实例占用，不无限等待。

### 5.3 按真实效果提交

1. 出队后核对原引用、双lease、期限及取消，读取严格完整快照，确认expected_profile_etag；不允许从损坏默认值构造新资料。
2. 准备有限新JSON及自身暂存，确认全部目录身份。首次`.ecl`初始化是单独接纳的有限准备动作；失败不触及其他文件，不能承诺取消会回收所有准备目录。
3. 打开并固定原对象，核对稳定完整版本；首次缺失也必须保持缺失。保存任务committing审计及严格事务凭据后，才接受有限提交。
4. 已有资料按原句柄不覆盖移到内部备份，再不覆盖发布新暂存；首次资料直接不覆盖发布。外部抢占原名时保留其文件，不能覆盖式回滚。
5. 只有确定原对象原样保留且暂存实际结束，才报告零效果；不确定的已接受动作保持unknown及cleanup_pending。成功发布后旧备份、记录及句柄实际结束才归还占用；晚到清理不修改任务历史效果。
6. 成功或恢复后通过ProfileCoordinator失效扫描缓存并通知资料变化。通知与保存区分，通知失败只重试通知/刷新，不能重做写入。明确不承诺外部应用在缺名窗口读到连续文件。

每份共享实例资料只允许一个尚未释放事务；所有插件和桌面资料写入口共同遵守占用。原、新JSON均至多64 KiB；宿主同时跟踪至多32个未释放实例资料事务，并受现有任务及RPC限额约束。已有未知暂存或备份保留，不按名称批量清扫。

### 5.4 持久记录及可信恢复

独立登记目录拟为宿主数据内 `instance_profile_transactions`；记录绑定原根/实例/`.ecl`身份、原/新文件身份及摘要、阶段和明确恢复决定。共享实例位于应用数据目录外，记录可保存宿主批准的定位信息以供恢复；不得交付Worker、界面或日志。它不是新的资源授权来源，不因恢复记录存在重新授予插件访问资格。

新宿主只回滚或清理已登记事务，不继续旧patch，不继承旧run控制权。损坏、超限、重复字段、对象身份不符及根已移动均保留，不凭阶段、名称、尺寸或摘要单独授权删除。NTFS时间及文件名复用仅允许已证明的明确过渡，不把私有文件的专门处理机械放宽为任意身份匹配。

拟新增可信主WebView专用的 `plugin_instance_profile_recoveries_list`、`plugin_instance_profile_recovery_resolve`，沿用当前恢复分页、opaque引用、revision/etag和稳定接纳回执模式；Worker、Dev Channel及自报main均拒绝。恢复不依赖插件仍安装、启用或保有许可；配置目录被移除也不能自动改绑同名新根。

明确选择包括：原位置缺失时不覆盖恢复；在同一已核实`.ecl`目录中按用户给定后缀另存为受限的 `recovered-profile-<suffix>.json`；二次确认永久丢弃已登记原备份。禁止另存为instance.json、stats.json及其他已有业务文件，不写任意路径。所有动作执行前重新核对审阅版本及原对象，不覆盖当前资料。

桌面和插件修改遇到该实例待恢复时均明确失败；完成恢复后失效对应缓存。当前宿主内原插件任务的cleanup_pending可随实际收尾解除，unknown不改报成功；重启后的历史任务不被当成新run任务。卸载/更新不能丢弃该安装仍关联的恢复资源，但其他插件不受影响。

## 6. 权限界面、契约和改动位置

| 范围 | 具体改动 |
| --- | --- |
| 实例领域 | instance_profiles、profiles、instance_queries；新增严格快照和资料事务；复用同一存储与通知机制 |
| 原生工具 | 提取已有句柄模块并更新全部生产/测试引用；增加共享实例目录、`.ecl`首次创建及身份转换验证 |
| 授权与资源 | grant_models/store、resource_catalog、worker_service；读/写独立选择，写仅单实例；资源退休与监管覆盖两种能力 |
| Runtime | models、resources及专用实例写适配器（拟runtime/instance_profiles.py）；Broker变更去重、任务归属及白名单接线 |
| 应用装配 | application向Worker服务注入既有游戏领域修改接口及唯一OperationManager，不重新创建GameService或InstanceProfileStore |
| 管理IPC | plugin_models、api/plugins、registry、request_schema、IpcLogPolicy；候选请求显式区分读/写，新增恢复命令 |
| 前端 | PluginPermissionsModal及PluginResourceSelector分别维护读/写选择；写目录仅可展开，不能选整目录；同步API类型、运行时校验、模拟数据及测试 |
| 恢复界面 | 现有“文件恢复”内分私有文件与实例资料两个视图，复用公共Modal及应用任务服务；各自请求/游标/选择隔离，不混用恢复引用或取消跨页面任务 |
| 文档和示例 | 正式协议、总设计、worker_instance_profile示例；显式选择目标及保留mutation_key，加载/卸载不自动修改 |

六语言文案明确“仅所选实例的四个资料字段”，不暗示任意文件或游戏操作权限。两个选择器可独立加载/失败/重试，读目录不自动勾选写范围；过期候选保存失败并要求刷新。恢复界面覆盖空状态、损坏记录、名称冲突、提交未确认、实际执行/清理和失败后重试。

不新增每次patch的确认弹窗。日志沿用中文操作与稳定码，不输出资料正文、别名/描述/标签值、完整路径、etag、摘要或恢复记录内容；新增正式IPC由IpcLogPolicy统一覆盖。既有扫描事件对桌面正常工作，新增Worker事件不包含用户路径或未经筛选的原始payload。

## 7. 实施顺序和开放门槛

1. **共享路径原生探针与严格快照**：证明批准根/实例身份到句柄的绑定、`.ecl`创建、原对象备份及不覆盖发布/恢复；补损坏资料和完整etag用例。不能证明则暂停写API接线并修订方案。
2. **资料权威写入协调**：严格更新读取接入所有现行写入者；完成同文件占用、暂存/备份持有与失败清理，避免桌面绕过pending或在缺名期间写默认值。
3. **实际进程中断和恢复**：记录写入、移动、发布、备份删除前后真实终止进程；新宿主仅恢复或清理。恢复管理必须在开放插件写入前可用。
4. **独立范围与任务适配**：读/写候选、授权、运行引用及双lease；去重、任务提交、版本冲突和安全结果投影。能力此时仍不计为已实现。
5. **前端及示例**：六语言独立选实例、恢复视图、真实Worker流程；修改现行契约全部生产方、消费方和测试，移除未开放的旧草案字段。
6. **门禁通过后开放**：完成下列测试、最终两种冻结产物和真实主窗口验收，再加入implemented_capabilities并以最终接线重验。
7. **本地交付**：相关检查通过后提交，前端本地main先行、主仓库指针和原工作区main随后集成；保留现有远端分歧，不推送或发布。

## 8. 验收计划

| 风险 | 必须通过的行为 |
| --- | --- |
| 范围及权限 | 读目录/写单实例组合、无read或write拒绝、写目录ID拒绝、跨run/安装拒绝、任一原lease撤销及资源退休 |
| 输入及版本 | 四字段/空patch/null/布尔/长度/标签；false和空列表；其他字段变化、缺失变已存在、同名同内容重建、格式损坏及结果超64 KiB |
| 现行桌面行为 | patch/reset/icon/reorder及纯扫描与插件共用协调；损坏更新不落盘，图标前置副作用不先发生；只读查询不建资料/统计或调用外部提供方 |
| 路径与资源 | 根/versions/实例/`.ecl`链接及替换、ADS、硬链接、只读、首次创建竞争；批准身份和原生句柄对应；失败句柄首次校验前有归属 |
| 接受和任务 | 排队取消/截止/执行器拒绝、双lease接受前后撤销、committing审计失败、同键并发、超时查询回执、结果失权隐藏 |
| 实际提交 | 外部空位抢占、新版本稳定快照、移动/记录/发布/关闭/删除确认故障、外部元数据句柄及映射、配额和占用直到实际释放 |
| 崩溃及恢复 | 原资料缺失/已有两类；native动作和记录之间真实进程终止；根退休/孤立安装、损坏或伪造记录；三种恢复选择、陈旧决定、另存冲突及重复回执 |
| 反馈和回归 | 扫描缓存/通知及恢复后刷新；等待超时不重写、通知失败不重写；新建/替换/删除/下载及实例只读原契约保持有效 |
| UI与发行 | 六语言、Classic/Folia、亮暗、背景透明/模糊、1200×800/960×600/放大窗口；独立范围、滚动、焦点、旧响应及重复提交；源码、PyInstaller/Nuitka各自实际运行 |

拟新增tests/test_plugin_instance_profile_write.py、test_instance_profile_transactions.py、test_plugin_instance_profile_worker.py及真实进程中断辅助程序，更新现有实例读取/资料/授权/IPC/日志/路径和前端用例。修复缺陷必须保留可在修复前失败的回归。

检查命令统一遵循 [AGENTS第6节](../../AGENTS.md#6-测试与验收)：主仓库Ruff check、format check、适用全仓Ruff和完整pytest；Core lint及主仓库Core测试；前端先pnpm check再pnpm build及实际启动器。POSIX读取和桌面资料分支需自动覆盖，插件实际写入只承诺本机Windows x64/NTFS；不把本机测试描述成远端CI或其他平台实机验收。

方案提交 `efd82a6` 已独立同步本地main，随后用户明确授权“开始实施”。以下记录本次实现结果，不将前述基线探针或其他切片的旧验收充作本轮验收。


## 9. 实施与验收记录

### 9.1 实现和集成

2026-10-23 按已批准方案A实施。共享原生探针通过后，接入完整资料快照、严格四字段增量、原对象备份及不覆盖发布、持久收尾和可信恢复，再开放instances.write。未引入旧revision入口、历史资料迁移、第二个GameService或第二套执行器。

- InstanceProfileStore共用同一资料锁，桌面patch/reset/icon/reorder与插件事务均遵守待恢复屏障；损坏资料保留，图标副作用在严格读取之后执行。完整版本包含物理身份、存在状态及原始字节，合法未知字段保持原值。
- 受控写入固定批准根、versions、具体实例及.ecl目录；Python目录身份通过同一原生句柄的fstat核实。旧目录相对句柄模块已移到ECL/utils/windows_file_handles.py，现有私有文件调用方全部更新，旧模块删除。
- 独立读/写范围、原始双lease、任务去重、排队取消/总截止、失权结果隐藏及实际清理占用接入既有应用任务服务。发布确认丢失时根据真实对象确认效果，不能误报none；通知失败只重试通知。
- 新宿主只恢复或清理登记对象。原位置占名时保留后来资料及原备份，可信主窗口可另存或明确丢弃；陈旧选择拒绝，相同审阅的重复请求返回同一回执，失败后重新审阅才能再次接纳。未登记暂存及损坏记录不扫描删除。
- 前端六语言分别选择读/写范围；写目录仅供浏览，不能勾选整目录。“文件恢复”新增实例资料视图，旧响应不覆盖新视图，迟到回执保留原任务类型，关闭不取消应用任务。

交付前合入原本地main的27c1c9b，保留既有离线账户UUID预览、Core修复和文档目录调整。集成时发现新增账户查询漏配日志策略，独立修复提交9adc88d并补安静查询及隐私回归。前端最终提交1ac523f包含功能ea7720e、主分支集成bfb924f和恢复类型修复；主仓库功能提交08e25ce。

前端原工作区main先快进到1ac523f，随后主仓库原工作区main快进到08e25ce。Core沿用主分支e6b5c0f，florolding沿用80d037e。原工作区另有packaging/arch/euoracraft-launcher.desktop未提交修改，与本功能无重叠；合并前后SHA-256均为78DE642A34453783FB49996CF8296B617E2739DB49189D1163D681B14C039E5B，未覆盖或提交。文档另行docs提交及本地快进；本轮未推送远端、创建标签或发布。

### 9.2 自动化和真实发行物

环境为本机Windows x64、本地NTFS、CPython 3.11。下列结果来自合入新基线并修复集成缺口后的最终代码；旧基线和早期构建结果不替代这些结果。

| 检查 | 最终结果 |
| --- | --- |
| 主仓库ruff check ECL tests、ruff format --check ECL tests、适用全仓ruff | 全部通过，302个Python模块格式符合要求；全仓使用当前Ruff的--output-format github，与CI旧参数含义相同 |
| 主仓库完整pytest | 2085 passed、23 skipped；日志profile-delivery-full.log |
| Core自身ruff与主仓库Core测试 | 全部通过，21 passed；日志profile-delivery-core.log |
| 严格模型、共享路径、资料任务及恢复专项 | 71 passed，包含4个原生路径门禁及13个真实进程中断组合；日志profile-alias-after.log |
| 既有私有文件/下载等受影响路径回归 | 141 passed；日志profile-private-regression.log，并再次包含于最终完整测试 |
| 前端pnpm check，随后pnpm build | 全部通过；168个测试文件、941 passed、1 todo；日志profile-integrated-frontend-check.log及profile-integrated-frontend-build.log |
| 最终PyInstaller发行物关联Worker专项 | 22 passed，包含源码与该冻结产物分支；日志profile-integrated-pyinstaller-workers.log |
| 最终Nuitka发行物关联Worker专项 | 22 passed，包含源码与该冻结产物分支；日志profile-integrated-nuitka-workers.log |
| 正式插件打包器 | worker_instance_profile.eclplugin构建并重新校验，明确entry_point为main:Plugin、四项必要声明 |

真实中断覆盖已有原资料和首次创建：prepared记录之前/之后、原对象移动之后、旧对象记录之后、发布确认之前/之后、备份删除之后及外部占名；子进程实际终止，新Store从持久记录恢复，未续跑旧补丁。故障注入另覆盖记录写入失败、原生发布确认丢失、只读/硬链接、外部元数据句柄延迟释放、通知重试、陈旧恢复决定和重复回执。

两份最终单文件可执行文件均实际启动独立Worker及主窗口，关联测试不以编译成功代替运行成功。产物位于Git忽略目录build/worker-manager，SHA-256如下：

- profile-pyinstaller-dist/EuoraCraft Launcher.exe：E12B0B2CC5D93638BF6F39AF8C8A52ACB108B5CCCB012A2ACE8FECB6AAC10467。
- nuitka/main.exe：141D2234E715ADE5F6A392C2CA80A23AC088ACD1434D494495B44E66481592DF。

### 9.3 真实桌面验收与边界

源码、最终PyInstaller及最终Nuitka分别执行144组权限布局和144组恢复布局，共864组实际WebView2检查。每轮覆盖六语言、Classic/Folia、亮暗、背景透明及模糊开关组合，以及1200×800、960×600、1600×1000原生窗口；核对宽度、标签/操作区、底部按钮可达、滚动及键盘焦点。Playwright CLI实际连接本地WebView2、获取快照并断开，截图经人工查看。

矩阵还按实际innerWidth/innerHeight分组复核：每种语言、皮肤、明暗及背景组合确实包含三档尺寸和最小窗口。Nuitka重验时发现按全局进程名找窗口会受另一启动器干扰，本地辅助程序已限定为测试启动进程及其子进程，并等待原生外框、客户区和WebView实际尺寸一致且稳定；重新执行完整288组后通过，不以尺寸请求成功代替生效确认。

实际操作同时验证：读目录选择不自动选择写实例，写目录无整目录勾选；分别批准具体实例后真实Worker修改资料，完整未修改字段和未授权实例保持原值；结果新etag与重新读取一致；停止并重新启用不自动再写。通过主窗口处理孤立安装的资料冲突，保留外部占名文件，将原资料另存为recovered-profile-original.json，并等待应用任务及记录实际收尾。既有离线wheel安装、原生扩展、私有文件新建/删除/替换、设置及用户任务取消也通过同一实际启动器流程。

本机原始日志、矩阵和截图均位于Git忽略目录：build/worker-manager/profile-integrated-ui-{source,pyinstaller,nuitka}.log及build/worker-manager/output/playwright/profile-ui-{source,pyinstaller,nuitka}。自动化入口为tests/test_packaged_plugin_directories.py，本地矩阵辅助程序为build/worker-manager/profile_ui_acceptance.py。

本轮未作Linux/macOS实机或发行物验收，不新增这些平台的第三方Worker支持。资料原名称可能短暂缺失，成功提交不提供旧版本撤销；外部对象或记录无法核实则保留pending/invalid，不自动覆盖、删除或假报清理完成。SDK调用和恢复协议见[现行归档文档](../protocols/eclplugin-package-format.md#获准实例资料修改)，显式调用示例见[worker_instance_profile](../../examples/plugins/worker_instance_profile/README.md)。
