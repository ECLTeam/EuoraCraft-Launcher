# 插件任务归属与提交控制实施方案

日期：2026-10-21（Asia/Shanghai）。状态：方案 A 基础切片已实施并通过本轮本地验收，范围与结果见第 10 节。

基线为主仓库 `c56b45f`、Core `fce6b1018c41f66ec29bb3b61dec6547dedb5588`、前端 `d8d8fc55a71fb081477152e4d53243e219eac6d8`。本轮接续 [实例只读能力](plugin-instance-read-api-implementation-plan.md)，落实 [总体设计](../designs/plugin-worker-api-v2-implementation-design.md) 第 15 节和 P08 的任务基础。本轮实施按用户确认的方案 A 推进；既有产物和上一轮测试数字不作为 P08 实施验收。

## 1. 目标和推荐路线

目标：宿主能确定任务属于哪个插件的哪一次运行；插件只能查询、取消自己的任务。撤销与提交接受具有确定的先后顺序，取消请求、工作函数结束和资源清理分别可观察，已提交动作的真实失败不会被取消标志掩盖。

| 方案 | 做法 | 取舍 |
| --- | --- | --- |
| A，推荐 | 扩展应用现有 `OperationManager`，增加可信来源、独立执行阶段、短时提交准入；插件层负责运行引用和 DTO | 共用一个执行方和最终状态，用户任务与插件任务之间的隔离可直接验证；需要回归现有下载、Java 和实例操作 |
| B，独立插件任务服务 | 为插件再建线程池、状态表和取消实现 | 主任务服务改动较少，但两套状态、退出和日志规则容易分歧，现有实际服务任务还需再次适配 |
| C，连同真实业务一起接入 | 在 A 的基础上同时开放下载、Java 安装或 Minecraft 安装/启动 | 能立即提供实际长任务，但需扩大权限、资源、增量写入和子模块审查范围，难以独立验证基础竞态 |

按上一轮建议，本文件细化 A 供用户审阅。若改选 B/C，先修订本文件的范围、接口和验收标准，再实施。

本轮范围包括：任务来源、阶段和副作用模型；自身任务的 `operations.get/list/cancel`；授权与提交接受的互斥；Worker 停止时的宿主任务跟踪；现有命令/设置注册及设置短写的提交接受点。使用测试专属宿主任务验证整条链路，不新增生产 `operations.submit`、模拟任务按钮或插件任意执行宿主函数的入口。

真实文件下载、Java 安装、Minecraft 安装/启动、账户、联机、工具进程和自定义前端仍按后续切片逐项适配。本轮不改变 Core、Florolding、前端源码或 Windows x64 以外的第三方执行范围。P07 其他只读能力可独立继续，不是本基础层的前置条件。

## 2. 当前代码与已验证缺口

| 位置 | 当前行为 | 本轮处理 |
| --- | --- | --- |
| `ECL/services/operations.py` | 应用唯一线程池，提供 `submit/get/cancel/close`，没有插件来源或按来源查询 | 在既有任务记录中维护可信来源，补齐内部快照和按来源操作，不创建第二套执行状态 |
| `OperationContext.finalize` | manager 的内部回调持有全局 `_lock` 执行 `action()`，成功后才设 `can_cancel=False` | 在短临界区先接受一次确定提交、禁止取消，再于锁外执行动作 |
| `OperationManager._execute` | 捕获任意异常时，只要取消事件已设置就标记 cancelled；返回后再次检查取消 | 明确取消异常与真实失败，按执行阶段处理，提交后的成功或失败不改写成取消 |
| `OperationManager.close` | 请求取消，并以 `shutdown(wait=False)` 返回；排队 future 与执行中任务分别处理 | 延续非阻塞退出，补齐自身任务实际完成查询，不把 close 返回等同线程结束 |
| `custom_downloads.py` | 使用 `finalize` 原子替换、rename 或 POSIX hardlink 发布文件 | 保留用户下载协议与文件安全边界，验证提交时取消不会阻塞其他操作 |
| `ECL/java/installer.py`、`manager.py` | finalize 包括移动/登记事务；移除后还有清理；cleanup 用 finalize 确认不可取消阶段 | 保留既有业务事务语义，动作在 manager 锁外执行，不能机械假设 finalize 是任务最后一行 |
| `services/game/{worlds,modpack,workspace,resources}.py` | 共用 manager，但部分任务会增量写入，没有统一的原子提交 | 这些现行用户任务不因此变成插件可调用任务，也不承诺取消能整体回滚 |
| `grant_store.py` | 已有内容/运行/epoch lease 和拒绝屏障，但公开 check 与实际业务登记分开 | 增加可信提交接受入口，把“授权仍有效”与“本次动作已接受”排在同一锁序内 |
| `runtime/broker.py` | 有本 run 去重回执和在途请求计数；settings.set 在 check 后直接原子写，注册也先 check 再更新内存 | 将现有生产短写/注册接入确定的接受点，沿用原返回值与去重键 |
| `worker_service.py` | 监管 Worker 与 Broker 在途 RPC；任务尚未进入应用线程池 | 注入既有 manager，跟踪自己的宿主 future，cleanup 等待实际执行收敛 |
| 前端操作类型与 store | 认识 pending/running/completed/failed/cancelled，已经区分 canCancel 和 cancellationRequested | 桌面现有状态协议保持该映射；内部 committing 通过 running、不可取消及进度说明表达 |

当前相关基线检查：`tests/test_custom_downloads.py`、`test_operation_logging.py`、`test_plugin_grant_store.py`、`test_plugin_host_broker.py`、`test_plugin_worker_manager.py` 共 146 通过、1 跳过。它们没有覆盖下面新增的两个反例，不能据此宣称缺口已解决。

使用人工任务的受控复现结果：

- 一个任务进入 finalize 后暂停，在它退出前，另一个任务的 cancel 被全局任务锁阻塞。
- 另一个任务先收到取消请求，随后抛出独立执行错误，现状为 `status=cancelled`、`errorCode=GAME_OPERATION_FAILED`，状态与原因不一致。

调研证据在忽略目录 `build/worker-manager/p08-plan-baseline.txt` 和 `p08-plan-probe.json`。复现只使用人工任务和独立数据目录，没有下载文件或操作用户实例。

## 3. 任务状态和来源的唯一归属

### 3.1 可信来源

由 `OperationManager` 唯一维护任务执行状态。新增明确的不可变来源值对象，区分应用用户操作和插件操作；插件来源包含宿主确认的 `installation_id`、`content_digest`、`run_id`。权限 epoch、实际能力及资源范围保存在该任务的准入上下文中，不以来源对象本身代替授权。

来源仅由 `ApplicationContext`、Worker 服务和受信宿主适配器构造。Worker 请求模型拒绝 owner、operation_id、run_id、grant_epoch、路径和函数等额外字段。现有用户调用 `submit(kind, worker)` 仍表示应用用户任务；为插件提供显式的宿主侧带来源提交方式，不从线程上下文或清单名称猜测所有者。

插件层只维护 `{operation_ref -> 宿主任务标识}` 的引用和结果投影，不复制一套可独立修改的状态或 percent。引用为随机值，绑定当前安装、内容和 run；全局 operationId 即使被猜中，也不能转换为其他来源的引用。

`runtime/operation_adapters.py` 的窄职责是绑定本 run 来源、提交校验、控制引用、白名单视图和收尾查询。其实际调用方为 Broker 的三个控制方法、Worker 服务的停止/监控，以及 P08 测试夹具中的受信任务生产者。生产业务任务的创建适配在对应后续切片接入，不为测试增加生产后门。

### 3.2 执行阶段与最终状态

保留应用现有最终状态集合；内部独立维护 `execution_phase` 和 `effect`，由执行方更新，禁止调用方通过 progress 的 details 覆盖权威字段。

| 内部阶段 | TaskView.state | 是否可请求取消 | 说明 |
| --- | --- | --- | --- |
| queued | queued | 是 | 尚未调用工作函数；取消后工作函数不得开始 |
| executing | running | 是 | 到检查点退出，设置取消请求不表示已经停止 |
| committing | committing | 否 | 一次确定目标的提交已接受；实际动作可尚在写入或登记 |
| 执行结束 | completed / failed / cancelled | 否 | 工作函数已返回或抛出，清理是否完成另由真实 future/收尾确认 |

桌面 `game:operation_progress` 和 `game_operation_get` 继续按现行五态投影，committing 对应 running、`canCancel:false`。插件 TaskView 独立表达 committing。这里是两个明确消费契约，不添加历史状态别名、旧接口转发或第二份执行状态。

effect 使用总体设计中的 `none/staged/committing/committed/partial/unknown`。queued 为 none；受信适配器在产生暂存或外部副作用前报告对应 effect。未知行为不推断成 none；增量写入未适配事务时保持 unknown/partial。提交成功为 committed；提交抛错且不能证明回滚完成时为 unknown，不能仅因异常就声称原目标未变。

取消异常只在实际执行方确认安全退出时产生。仅设置 cancel_event 后出现的独立网络、磁盘或程序错误仍是 failed，保留真实错误码；没有正确的服务错误码时使用现行稳定通用错误。已经接受提交后，不再用通用的返回后取消检查改写结果。

## 4. 提交接受与锁序

沿用总体设计中的 T：授权服务发布新 epoch/拒绝屏障的时刻。新入口完成授权核验和“接受本次确定动作”的登记，不在授权锁内执行该动作。

1. 在锁外准备有限动作和确定目标，完成输入、版本、资源以及内容核验。文件锁、设置写互斥或领域事务准备在进入授权短临界区前处理。
2. 由宿主授权会话核对相同安装/内容/run、当前 epoch、期限及实际资源范围。必要时重做核验；不能把锁外的旧结果当成当前许可。
3. 在授权锁 → 对应任务/注册短锁的固定顺序中，确认没有取消、关闭或更早的提交；登记 committing、接受时间、确定目标摘要及不可取消状态。
4. 释放授权和全局任务锁，再执行日志持久化及业务动作。其他任务可同时 get、cancel、提交或撤销，不等待此动作结束。
5. 由执行线程报告真实结果、effect 和清理状态；后续另一次副作用必须重新准入，不能复用一次接受登记扩大范围。

| 强制竞态 | 必须结果 |
| --- | --- |
| T 先于接受登记 | 拒绝提交，业务动作次数为 0 |
| 接受登记先于 T | 允许这一次有限动作结束；撤销阻止之后的新动作 |
| 取消先于接受登记 | 不执行提交，等执行方收尾后 cancelled |
| 接受后调用取消 | 明确不可取消，不能设置会导致成功被改写的通用取消标志 |
| 接受后动作失败，同时发生撤销/关闭 | failed，保留实际错误和可证明的 effect |

当前 `PluginGrantStore` 还在全局服务锁中核验代码和读写记录。实施时须在相关准入路径局部调整准备与状态复核，使代码哈希、等待文件锁和业务落盘不进入新的短临界区；不能直接把 `session.check()` 与 action 包进同一全局锁。外部授权更新、内容变更、损坏记录和持久化失败的拒绝规则必须保留并通过完整授权回归。若无法满足锁序和复核规则，报告具体阻碍，不开放新写接口。

`OperationManager` 不导入插件模块。通过类型明确的宿主检查/提交接受回调连接授权；回调只登记受信状态，不执行 Worker 回调。当前实际调用是插件任务适配器，现有用户 finalize 无插件授权要求但仍使用相同的短时状态迁移。

现有命令注册、设置定义、订阅登记和 settings.set 同步接入该接受机制。注册只在接受后原子更新本 run 的登记；设置仍在其现有写互斥和文件锁下核对 expected_revision，再接受一次原子写，磁盘动作在授权锁外完成。不会把同一接受点用于一串后续设置写入。

## 5. 自身任务控制 API

Broker 新增严格请求模型，控制 API 不新增可用于取得别人任务的权限能力；所有权来自当前认证运行。原业务权限被撤销后，仍允许查询自己的脱敏状态和请求取消未提交的自身任务，结果数据另行核验原权限。

| 方法 | 请求 | 返回 |
| --- | --- | --- |
| `operations.get` | `{operation_ref}` | 本 run 的 TaskView；未知、其他 run 或其他来源引用统一拒绝，不泄露对方存在性 |
| `operations.list` | `{cursor?:string,limit?:int}` | 本 run 的 `Page<TaskView>`，不列用户或其他插件任务 |
| `operations.cancel` | `{operation_ref}` | `{requested:bool,state,effect}`；接受取消不表示任务已停止；提交已接受时 `PLUGIN_COMMIT_STARTED` |

TaskView 显式投影总体设计字段：operation_ref、kind、state、percent、stage、message、can_cancel、cancellation_requested、effect、result、error_code。阶段及说明来自允许列表，不透传 worker details、原始异常、路径、下载地址、账户信息或完整日志。result 只经登记的该任务结果投影，并在每次交付前复核相应读取许可；失权后 result 为 null，状态与实际 effect 仍可查询。

控制取消本身采用当前 run 的 mutation_key 去重；同键不同请求拒绝。重复 get/list 不创建新任务，分页不调用 worker。limit 1—100、默认 50；结果 64 KiB，游标绑定来源及查询条件，最多 16 个快照、120 秒。列表分页保存固定任务引用集合，每页查询当前快照并复核所有权，避免状态更新使遍历遗漏或返回旧的敏感结果。

拟定资源上限：每 run 最多保留 128 个任务引用，宿主同时跟踪的插件任务记录最多 1024 个；超限拒绝新提交，不能淘汰活跃任务后失去收尾能力。run 关闭后释放引用和分页，已完成且摘要落盘的插件记录可释放内存；未完成或摘要保存失败者仍占用限额并保持可观察状态。用户任务现有历史保留不在本轮重构范围。

本基础切片没有新的生产长任务创建入口，因此正常 Worker 的 operations.list 可以合法返回空列表。只有后续已验收业务适配器才能创建可见任务；实施报告不能把测试专属任务描述成已开放安装功能。

现有 `settings.set` 保持同步结果及 `mutations.get` 协议，不在本轮把全部短写转换成 TaskReceipt。等待方超时仍保留同键回执及实际结果，不能重放副作用；总体目录 W5 中统一异步 operation_ref 回执的完整业务接线另随真实生产任务审阅，不宣称本基础切片完成全部写接口任务化。

## 6. 撤销、运行结束和持久化

### 6.1 清理以实际执行为准

- 入队、出队、检查点、提交接受和结果交付均核验原任务准入；任何 epoch 变化使旧的未提交任务失效，增权也不自动复用旧任务。
- Worker 服务的更新、监控和停止路径请求取消本 run 的未提交任务；任一受控任务检查点发现失权也自行终止。可选权限失效不取消其他能力仍有效的新任务，旧 epoch 任务不能自动换新 lease。
- 运行停止、崩溃和断连先撤销准入、停止 Worker，再跟踪自己的宿主任务；不能调用应用全局 close/cancel 或其他用户任务的取消入口。
- committing 动作已接受时由宿主执行方收尾。Worker 退出不会杀掉宿主线程，也不补偿性删除已经保存的文件、Java 或实例。
- Broker 的 RPC idle 与自身任务 idle 都满足，且任务 future/必要收尾实际完成后，才报告 cleanup completed。terminal 状态已发布但 future 仍在落盘或清理时保持 pending，不能只看 status 判定结束。
- 等待有上限，沿用停止监控的 pending 和重试机制；不为旧 Broker 提前丢弃未结束任务。应用退出逐项请求清理，线程仍未退出时明确记录，不能宣称已经停止。

### 6.2 插件任务摘要

插件任务采用新的宿主审计摘要目录 `operations/plugin/`，与现行用户任务结果区分职责，不改写或转换既有用户结果文件。摘要采用有版本号的严格模型，由执行方按任务序列化原子写入；queued、running、提交接受和 terminal 的 revision 不得被并发旧写覆盖。每任务的写入互斥与授权/全局任务锁分离，拿到写入互斥后读取最新快照；进度通知不能覆盖任务身份、阶段、结果和保存状态。

摘要只保存来源、内部任务标识、能力/资源身份摘要、阶段、effect、取消原因、接受时间、最终错误码及必要的安全结果摘要，不保存完整参数、路径、配置值、令牌或原始异常。运行引用不持久化为下次运行的凭证。

在业务提交动作之前，必须成功保存本次 committing 摘要；保存失败则不执行动作，报告稳定的任务摘要保存失败并保留拒绝/失败状态。业务动作已经成功但最终摘要保存失败时，保留真实执行结果，并单独标记审计收尾未完成，不能误报业务回滚；重试仅重写摘要，不重做动作。

重启后旧 run 不能取回引用或控制旧任务，也不自动重放。若摘要停留在 committing，只能认定结果可能 unknown，不能推断 cancelled、成功或自动清理目标。摘要不作为授权来源；本轮不增加全盘扫描、历史记录迁移或后台恢复执行器。

## 7. 实施落点与顺序

| 步骤 | 文件 | 验收点 |
| --- | --- | --- |
| 1. 领域状态与反例 | `services/operations.py`，新增 `tests/test_operations.py` | 原子接受阶段、锁外动作、真实取消/失败分类、排队和实际完成分别可见 |
| 2. 授权接受 | `plugins/grant_store.py`，必要的 `grant_models.py`；授权与 Broker 回归 | T 两种顺序确定；磁盘/内容准备不扩大临界区；外部版本和持久化失败保持拒绝 |
| 3. 运行适配与摘要 | 新增 `plugins/runtime/operation_adapters.py`，`runtime/models.py`；新增 `tests/test_plugin_operation_ownership.py` | 来源隔离、引用限额、摘要顺序和保存失败、原权限结果过滤 |
| 4. Broker 控制与现有接受点 | `runtime/broker.py`；Broker/SDK 测试 | get/list/cancel、同键去重、严格请求、命令/设置/事件登记竞态及 settings 原子写 |
| 5. 应用注入和退出 | `application.py`、manager/base、`worker_service.py`；应用/Worker 测试 | 注入同一个 manager；停止只影响自己的任务，pending 到真实清理完成 |
| 6. 冻结与集成 | 既有 Worker 冻结夹具及测试、总体/API/本方案文档 | 两种 onefile 的真实控制与撤销；现有桌面设置、用户任务、实例读取无回归 |

只有受影响的生产 Python 模块按主仓库文件头、类型和中文多行文档要求收敛；不为风格统一修改无关服务。本轮默认无前端和子模块源码变更。发现真实服务适配必须扩大范围时，在改动前修订方案。

## 8. 测试和交付门槛

完整检查遵循 [AGENTS.md 第 6 节](../../AGENTS.md#6-测试与验收)，不能以以下新增用例替代主仓库 Ruff 和全量 pytest。纯方案阶段仅检查文档；功能实施后执行所有适用门禁。

| 场景 | 必须观察到的结果 |
| --- | --- |
| 用户、插件 A、插件 B、A 的另一次 run、A 的另一个内容摘要 | get/list/cancel 严格隔离；猜测 operationId 和伪造 owner 不赋权 |
| 取消排队任务 | worker 调用次数 0；释放自己的排队资源；不影响其他用户任务 |
| 取消执行任务 | 先 cancellation_requested，阻塞工作函数不结束则保持非 terminal/cleanup pending；到检查点并清理后 cancelled |
| 独立失败与取消同时发生 | failed 与实际稳定错误码一致，不能被 cancel_event 改写 |
| 提交长时间阻塞 | 其他任务 get/cancel/submit 及其他插件撤销及时完成；没有全局任务锁包围动作 |
| T 先 / 接受先 / 取消先 / 接受后取消 | 按第 4 节矩阵确定动作次数、state、effect，不用时间碰运气 |
| 接受后失败或成功，期间关闭/撤销 | 保留真实结果；不误报 cancelled，不执行未经新准入的下一动作 |
| 可选能力撤销、必要能力撤销、内容变化、资源退休、到期、保存失败 | 旧任务在检查点/提交处拒绝，失权后不交付敏感 result；其他来源任务不受影响 |
| Worker 崩溃/断连/重载，任务仍在宿主 | 只回收自己的任务，future/收尾没结束则 pending；新 run 不继承引用 |
| queued/committing/terminal 摘要保存失败、并发 progress | 提交前失败无业务动作；提交后不重放；旧 revision 不覆盖新摘要，原文件原子保留 |
| 分页、限额、重复 mutation_key、迟到响应 | 不泄漏别人任务、无无界引用/快照、同键一次副作用、旧结果不能冒充新请求 |
| 应用用户下载、Java 安装/移除/cleanup、实例及存档任务 | 原有操作可用；POSIX hardlink 与 Windows rename 分支均覆盖；取消和失败后数据保留语义正确 |

测试使用 Event/barrier 控制竞态，避免仅凭短 sleep 断言线程已经结束。新增缺陷回归必须先在当前实现失败，再在修复后通过。审计落盘期间可读取 terminal 的现行行为可保留，但 cleanup 必须等待真实收尾。

主仓库功能门禁：Ruff check/format、全仓 Ruff、完整 pytest；Core 集成测试按涉及范围和 CI 要求执行。支持分支至少覆盖 Windows/POSIX 的应用任务语义，POSIX 第三方执行仍拒绝。若实施扩大为前端修改，增加其完整 check/build 和主题/窗口门禁后才提交。

PyInstaller 与 Nuitka 均重建真实发行物，测试夹具通过同一正式 Broker 接入人工宿主任务，验证 Worker 自身状态查询、取消、跨运行拒绝、禁用/撤销与宿主任务实际结束；夹具只存在于测试，不进入生产方法白名单。正式桌面验证主窗口审阅、启用、设置保存、实例只读以及用户任务取消/退出的关联流程，使用隔离数据和人工资源，不操作个人库。

每类记录明确区分源码单测、平台分支、真实冻结和真实桌面验收。未执行的 POSIX 实机/远端 CI 如实报告。完成后按 [AGENTS.md 第 7 节](../../AGENTS.md#7-git-提交与本地交付) 提交中文 Conventional Commit，文档独立提交并安全集成本地 main；远端推送、处理现有远端分叉和发布均不在本轮授权内。

## 9. 已确认的决策

用户已通过“开始实施”确认方案 A 及以下边界：共用现有 manager；新增自身任务控制 API；修复全局锁及取消结果分类；现有短写/注册采用原子接受但保持既有返回；插件任务独立安全摘要；本轮只用测试任务验证创建和提交，不开放真实安装/下载生产入口。

本文件是 P08 基础实施边界，不表示 P08 全部生产任务接线或 P09—P16 已完成。本轮功能修改在用户确认本方案后开始。

## 10. 实施与验收记录

2026-10-21：用户明确“开始实施”后，按方案 A 完成基础切片。应用仍只创建一个 OperationManager；可信来源、执行阶段、副作用、取消原因、提交接受和审计收尾由该服务维护。插件运行适配器只保存自身随机引用及结果投影，Broker 开放严格的 operations.get/list/cancel，生产不提供任务创建方法。

提交动作移到全局任务锁外，只有明确取消异常且实际安全退出才标记 cancelled；独立错误保留 failed。任务终态和不可取消状态在同一临界区发布。已结束上下文不能继续进度或提交，同一任务并发提交被明确拒绝；这两项与原全局锁/错误分类缺陷均有修复前失败、修复后通过的回归用例。

授权按内容使用独立 I/O 锁准备和保存，代码核验、文件锁等待和落盘不占全局授权锁；拒绝屏障、失效版本和一次接受登记在短锁中排序。现有命令注册、订阅、设置定义和原子设置写入已接入接受点，保持原协议。授权文件保存与 close 竞争时不会复活已关闭会话，其他插件仍可撤销。

插件审计摘要严格保存到 operations/plugin，包含来源、能力/资源身份、任务类型、阶段、副作用、取消原因、接受时间和错误码，不保存原始参数、路径或业务结果。每任务写锁内捕获最新状态并原子保存。提交前摘要保存失败时零业务动作；最终摘要失败保留真实结果，等待真实 future 结束后仅重试摘要。旧 run 不从摘要恢复引用、不重放动作。

Worker 停止只取消自己的未提交任务；已接受提交由宿主执行方完成。清理要同时满足 RPC idle、实际 future 和审计保存结束；pending 时拒绝重新启用，完成后释放本 run 引用及已保存记录。分页固定引用、每页重新过滤结果，并按实际投影字节数切页；容量耗尽不淘汰活跃任务。

功能提交：`50aed72`（feat: 增加插件任务归属与原子提交控制）。

本轮最终检查：

- 主仓库 Ruff check、format check 和 GitHub 格式的全仓检查通过。本机 Ruff 使用当前的 --output-format github 参数，原规范中的 --format 参数不被该版本接受；检查范围未缩减。
- 主仓库完整 pytest：1628 通过、17 跳过；日志为 build/worker-manager/p08-backend-accepted.txt。未提供冻结环境变量的产物专属用例在源码全量中跳过，随后以实际产物单独执行。
- Core 子仓库 Ruff 通过；Core 16 项用例与任务归属 27 项用例合计 43 项通过，见 p08-core-task-final.txt。Core、Florolding 和前端 gitlink 均未修改。
- 前端完整 pnpm check 通过：166 个文件、912 通过、1 todo；随后 pnpm build 成功。两者作为启动器运行流程门禁执行，没有前端源码改动，也不把本轮原生 IPC 验证描述成重新完成主题和窗口布局矩阵。
- PyInstaller 与 Nuitka 均以最终代码重新构建；各 28 项真实产物测试通过，见 p08-pyinstaller-accepted.txt、p08-nuitka-accepted.txt。新 Worker 控制用例已加入 Windows x64 产物 CI 的测试列表，但本轮没有推送或执行远端 CI。
- 两种真实发行物均通过 Windows 主 WebView 原生 IPC 验证：归档安装、主窗口权限审阅、允许单实例、只读查询、插件设置保存、自身任务列表和撤销收尾；另用隔离的本地 HTTP 服务验证用户下载取消，确认最终 cancelled 且不发布目标文件。人工宿主任务通过正式 Broker 验证真实 Worker 的自身查询/取消、跨 run 拒绝、提交后撤销和停止 pending，不增加生产测试方法。
- Windows 与 POSIX 的应用任务/下载分支由相关用例覆盖；未运行 POSIX 实机或远端 CI，第三方 Worker 仍只支持 Windows x64。

原始日志、失败反例和构建产物保留在忽略目录 build/worker-manager/p08-*，不进入提交。已停止本轮测试创建的进程；没有操作个人实例或外部下载服务。

本轮只完成任务控制基础与现有短写接受点。真实下载、Java、Minecraft 等插件任务生产者、统一 W5 TaskReceipt、重启后的可信历史任务界面和自定义前端不在完成范围；后续按业务能力单独审阅。功能与文档分别提交，并按第 7 节安全集成本地 main；远端既有分叉保持原样，未推送或发布。
