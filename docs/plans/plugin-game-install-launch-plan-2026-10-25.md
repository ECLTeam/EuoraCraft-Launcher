# 插件游戏安装与启动实施方案（P12）

日期：2026-10-25（Asia/Shanghai）。状态：**待审阅，尚未授权实施**。

本文细化总体设计第 16.2 节 P12 切片（Minecraft 安装与启动）。用户本轮已确认三项决定：范围覆盖全量 P12（只读、安装、启动、停止）；安装任务改造接入 `OperationManager`；插件启动保持全局单任务，与用户启动冲突时返回忙。本文审阅通过并明确"开始实施"后，才修改生产代码、测试与前端。

## 1. 目标与范围

### 1.1 目标

第三方 Worker 能在用户明确授权的范围内：查询宿主固定来源的游戏版本与加载器清单；在用户选择的游戏根目录安装一个版本；启动用户授权的实例并使用当前选中账户；查询自己启动的进程状态；停止自己启动的进程。全过程不返回本机路径、账户凭据、启动命令或任意 URL，不继承用户的启动租约。

### 1.2 本轮开放的方法

| 方法             | 一句话                    |
| -------------- | ---------------------- |
| `game.catalog` | 宿主固定来源的 Minecraft 版本目录 |
| `game.loaders` | 指定游戏版本的加载器版本列表         |
| `game.install` | 在获准游戏根目录安装一个版本，返回任务回执  |
| `game.running` | 查询获准实例当前的运行记录摘要        |
| `game.launch`  | 启动获准实例，返回任务回执          |
| `game.stop`    | 停止本运行启动的游戏进程           |

### 1.3 明确不做

- 不开放 `game.uninstall`、实例删除、实例复制、整合包导入导出。
- 不开放原始 JVM 参数、wrapper、pre/post 脚本、环境变量或 `javaagent`；`LaunchOptions` 只保留 `memory_mb`、`width`、`height`、`fullscreen`、`runtime_ref`。
- 不引入 `LaunchPatch` 与 required 钩子语义。现有 `LaunchHookRegistry` 全部为 best-effort（`ECL/plugins/launch_hooks.py:105-114` 逐钩子 try/except，异常只记日志），`PLUGIN_REQUIRED_HOOK_FAILED` 属 P14 扩展登记切片，本切片不实现，也不注册第三方钩子。
- **不改插件启动与用户启动的并发模型：全局仍然只允许一个启动流程。**
- **不开放 `game.running` 以外的跨插件进程可见性；不返回其他安装或用户启动的进程控制权。**

## 2. 调研基线与已核实问题

主仓库工作树当前 HEAD 为基线；下表所有行号对应当前工作树。

### 2.1 安装服务

| 现有代码                                                                      | 已核实情况                                                                                                                                 | 实施要求                                               |
| ------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------- |
| `install.py:141` `install_version(body, *, game_path, source, java_path)` | 同步函数，必须在运行中的事件循环里调用（`install.py:192` `asyncio.get_running_loop()`），内部 `loop.create_task` 后立即返回 `{"taskId","versionId","versionName"}` | 插件入口不能复用该签名；插件调用发生在 Broker 线程池，没有事件循环              |
| `install.py:193-196`                                                      | `task_id` 可由调用方 body 传入；仅按 `task_id` 去重                                                                                               | 不同 `task_id` 可并发写同一版本目录；插件不得参与 id 生成               |
| `install.py:233-248`                                                      | `to_thread.run_sync(..., abandon_on_cancel=True)`                                                                                     | 取消只让等待方返回，工作线程继续跑；不能作为插件任务的取消语义                    |
| `base.py:180-181`                                                         | 自有 `_install_tasks` / `_active_downloads`，纯内存、无持久化、无查询接口，只有 `game:install_progress` 事件                                                | 未接入 `OperationManager`，插件无法获得任务回执与权威状态             |
| `base.py:371-383` `_run_downloader_blocking`                              | `loop` 为 `None` 时用 `asyncio.run(downloader.run())` 在当前线程自建事件循环                                                                        | **可在线程池中运行**，这是安装接入的关键前提                           |
| `install.py:430-450`                                                      | 先 `build_*_download_list` 写 `versions/<save>/<save>.json` 与 `VersionInfo.json`，再下载                                                    | 提交点在下载之前；失败无回滚，残留为完整 json 加半套 `libraries`/`assets` |
| `install.py:513-530`                                                      | 下载失败按 `failed_entries` 汇总后抛 `GAME_DOWNLOAD_FAILED`                                                                                    | 已下载文件保留，属 `partial` 效果                             |
| `install.py:374` `install_blocking(...)`                                  | 签名含 `report`、`cancel_event`、`loop`、`include_fabric_api`；`_ensure_install_not_cancelled`（`install.py:340`）在阶段边界检查                      | 可原样复用，只需提供适配后的 `report` 与 `cancel_event`           |
| `install.py:437-439`                                                      | 清单为空即直接 `report("done")` 返回                                                                                                           | 增量补全与全新安装共用同一路径，天然支持"已有实例补全"                       |
| `api/game.py:126-130`                                                     | 下载源由全局配置解析（`resolve_mirror_source`）                                                                                                   | 插件不能改源；宿主固定为当前有效配置的源                               |

### 2.2 启动服务

| 现有代码                                                                                                  | 已核实情况                                                                                                                                           | 实施要求                                                            |
| ----------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| `launch.py:831` `launch_instance(...)`                                                                | 在 IPC 协程内直接执行完整启动流程，无 operationId、无持久化                                                                                                          | 插件启动需先获得任务回执，不能阻塞 Broker 线程                                     |
| `base.py:208` `_launch_cancel_event` + `launch.py:947-950`                                            | 全局单任务；非空即 `LAUNCH_ALREADY_RUNNING`，不排队                                                                                                          | 插件与用户互斥；插件侧必须返回 `PLUGIN_LAUNCH_BUSY` 而不是透传内部错误码                 |
| `launch.py:1272` `self.instances.create_instance(...)` → `InstancesManager.py:122` `subprocess.Popen` | 不可逆提交点                                                                                                                                          | 提交前最后一次复核授权；提交后不因撤销杀进程                                          |
| `launch.py:1227` 之后                                                                                   | 取消不再阻止建进程                                                                                                                                       | 取消边界必须如实说明，不得宣称"取消即未启动"                                         |
| `launch.py:1352` `cancel_launch()`                                                                    | 置位全局事件并 `downloader.stop()`；无法停止取令牌（`973`）、authlib（`1018`）、`check_files`（`1094`）、`options` 写入（`1163`）、命令组装（`1207`）、`pre_launch`（`1222`，最长 60 秒） | 插件取消只能作用于自己发起的启动；不得调用全局 `cancel_launch`                         |
| `launch.py:954` `accounts.current_account()`、`973` `get_launch_credentials()`                         | 取全局当前账户，无参数                                                                                                                                     | 插件不传账户；无当前账户时返回 `PLUGIN_ACCOUNT_REQUIRED`                       |
| `launch.py:1163-1170`                                                                                 | 用全局 `ui.locale` 写 `options.txt` 的 `lang`                                                                                                        | 属宿主行为，插件不可见、不可改                                                 |
| `launch.py:1295`、`1421`                                                                               | 写 `eclversion.json` 启动计数与时长                                                                                                                     | 插件启动同样计数；这是宿主统计，不暴露给插件                                          |
| `launch.py:451-458`                                                                                   | 写 HKCU GPU 偏好                                                                                                                                   | 宿主行为；文档需说明插件启动会触发同样的系统写入                                        |
| `base.py:82-97` `_RunningGame`                                                                        | 有 `token`、`version_id`、`instance_id`、`exited`、`exit_code`、`stopping`，**无 pid、无来源标记**                                                            | 需新增 `pid` 与 `origin` 字段才能支撑 `game.running` 与 `game.stop` 的所有者校验 |
| `launch.py:1562` `stop_instance(instance_id)`                                                         | 按 `instance_id` 停止，先通知后强制                                                                                                                       | 需在插件侧按 `game_run_ref` 反查并校验所有者                                  |

### 2.3 既有插件接入模式（复用基线）

`RuntimeOperations.submit`（`ECL/plugins/runtime/operation_adapters.py:58-134`）已经把"授权租约 → 任务准入 → 唯一 `OperationManager` → 引用映射 → 结果投影"这一套做完了：`check`/`accept` 回调、`OperationAdmission`、`result_lease`、`cleanup`、`deadline` 与 `submission_deadline` 分离、128 条引用上限、`is_idle`/`close` 收敛。`RuntimeInstanceProfiles`（`ECL/plugins/runtime/instance_profiles.py`）是"双租约 + 任务 + 投影"的最小范例，P12 两个写方法照此结构实现，不新建任务框架。

## 3. 能力与资源范围

### 3.1 新增能力

| 能力                  | 资源范围                     | 说明                                     |
| ------------------- | ------------------------ | -------------------------------------- |
| `game.catalog.read` | 无（声明 `resource_refs` 为空） | 宿主固定元数据源的有限网络许可；不依赖也不授予 `network.read` |
| `instances.install` | 具体游戏根目录                  | 用户在可信主窗口勾选目录；安装到该目录下的新版本子目录            |
| `instances.launch`  | 具体实例                     | 用户勾选具体实例，不提供"以后新增"                     |
| `instances.stop`    | 具体实例                     | 与 `instances.launch` 独立声明，不自动继承        |

`game.running` 复用已有 `instances.read`。`game.install` 对 forge/neoforge 需要宿主 Java 才能构建安装器清单，但那是宿主内部行为，**不要求 `java.use`**；`game.launch` 只有在请求里带 `runtime_ref` 时才额外要求 `java.use`（对应总体设计 13.7）。

### 3.2 能力白名单与校验点

新增能力必须同时进入三处，缺一即不可授予：

1. `PluginHostBroker.implemented_capabilities`（`ECL/plugins/runtime/broker.py:71-86`）。
2. 资源目录候选：`instances.install` 需要 `PluginResourceCatalog` 暴露可写根目录候选；`instances.launch`/`instances.stop` 需要实例候选。现有 `resource_catalog.py:147` `refresh` 与 `runtime/resources.py:343` `profile_target` 只服务读与资料写，需扩展为按能力返回候选。
3. `grant_models.py` 的动态范围校验：`PermissionDeclaration` 目前只对 `network.read`、`instances.read`、`instances.write` 和四个 `filesystem.*` 做特殊处理（`grant_models.py:84-93` `_host_selected_instances`），新增三项能力需按"声明 `resource_refs` 必须为空、范围由宿主绑定"的同类规则接入。

### 3.3 时限

| 方法             | 时限               | 所有者   |
| -------------- | ---------------- | ----- |
| `game.catalog` | Q30              | S     |
| `game.loaders` | Q30              | S     |
| `game.install` | T5 提交 / 1800 秒执行 | O+S   |
| `game.running` | Q5               | S     |
| `game.launch`  | T5 提交 / 180 秒    | O+S+L |
| `game.stop`    | T5 / 30 秒        | O+S   |

## 4. 分阶段切片

三个阶段独立验收、独立提交，前一阶段未通过不进入下一阶段。

| 阶段    | 内容                                           | 关键风险                                         | 预计改动面                                     |
| ----- | -------------------------------------------- | -------------------------------------------- | ----------------------------------------- |
| P12-A | `game.catalog`、`game.loaders`、`game.running` | 只读投影泄漏路径；`game.running` 的所有者过滤               | 小：新增查询门面 + Broker 分派 + 资源候选               |
| P12-B | `game.install`                               | 安装任务接入 `OperationManager`；并发互斥；取消与 effect 语义 | 大：改造 `InstallCoordinator` 任务模型            |
| P12-C | `game.launch`、`game.stop`                    | 全局单任务来源标记；提交闸门；所有者校验                         | 大：改造 `LaunchCoordinator` 与 `_RunningGame` |

## 5. P12-A 只读能力

### 5.1 `game.catalog`

数据来源固定为 `GameService.minecraft_versions_classified(source)`（`ECL/services/game/scan.py:848`），`source` 由宿主按当前有效配置解析（`api/game.py:126-130`），插件不能传入。现有返回项为 `{id, type, releaseTime}`（`scan.py:841-846`），需显式投影为 `{version_id, type, release_time}`，不透传 `id`/`releaseTime` 原始键，也不返回下载地址。

`type` 只接受 `release` 与 `snapshot`（对应总体设计 13.7）；请求其他分类返回 `PLUGIN_SCHEMA_INVALID`。分页复用 `ResourcePages`（`runtime/resources.py:77`），limit 1—100，游标绑定查询条件与 run。

### 5.2 `game.loaders`

数据来源 `GameService.loader_versions(loader_type, game_version, source)`（`scan.py:892`）。该方法返回 `list[str]`，没有稳定/非稳定标记；本切片**不返回 `stable` 字段**，改为返回 `{loader_version, is_latest}`，其中 `is_latest` 表示宿主列表中的首项。这是对总体设计 13.7 字段的收敛，需在协议文档同步，不能保留一个恒为 `true` 的假 `stable`。

`loader` 只接受 `fabric`、`forge`、`neoforge`、`quilt`；其余返回 `PLUGIN_LOADER_NOT_SUPPORTED`。`game_version` 必须是 `game.catalog` 可返回的版本 id，长度 1—64。

### 5.3 `game.running`

从 `_running_games`（`base.py:183`）投影，**必须按 `origin` 过滤**：只返回本运行发起的进程，以及（可选）用户或其他来源在同一实例上运行的进程摘要——后者只给 `running`/`exit_code`，不给 `game_run_ref`，避免插件获得他人进程的控制权。首版简化为：只返回本运行发起的进程，其他来源的进程完全不出现。

`pid` 需要新增：`_RunningGame`（`base.py:82-97`）当前没有 pid 字段，应从 `InstancesManager.instances[instance_id]["Instance"]` 读取并缓存在运行记录里，避免每次查询都去翻 Core 内部字典。

`game_run_ref` 是本运行随机引用，映射到 `_RunningGame.token`，与 `RuntimeOperations` 的 `operation_ref` 分开维护：启动任务可能已完成，但进程仍在运行，两者生命周期不同。

## 6. P12-B 安装接入改造

### 6.1 现状缺陷（必须解决，不能绕过）

1. 任务状态在 `InstallCoordinator` 私有字典，插件拿不到权威回执。
2. `task_id` 由调用方提供，两个不同 id 可并发写同一目录。
3. `abandon_on_cancel=True` 使"取消"只结束等待方。
4. 失败无回滚，且没有 `effect` 概念，插件无法判断"这次失败留下了什么"。

### 6.2 目标结构

新增 `ECL/plugins/runtime/game_install.py` 的 `RuntimeGameInstall`，职责与 `RuntimeInstanceProfiles` 同构：解析请求 → 取得根目录租约 → 校验目标版本名可用 → 通过 `RuntimeOperations.submit` 登记任务 → 返回 `{operation_ref, state:"queued"}`。

任务体不新建执行器，复用 `InstallCoordinator.install_blocking`：

```text
RuntimeGameInstall.submit(params, submission_deadline)
  ├─ GameInstallRequest.model_validate(params)          严格 DTO
  ├─ resources.install_target(root_ref, submission_deadline)
  │    └─ 返回 (target: InstallTarget, lease: GrantLease)
  ├─ 目标版本目录不存在（存在则 PLUGIN_INSTALL_BUSY 或 PLUGIN_FILE_EXISTS）
  └─ operations.submit("plugin_game_install", worker, lease, ...)
```

`worker` 内部只做三件事：把 `OperationContext` 的取消转成本地 `Event`；把 `report` 回调映射到 `context.progress`；调用 `install_blocking(..., loop=None)`。

### 6.3 进度与取消适配

`install_blocking` 的 `report(stage, message, done=…, total=…)` 是同步回调，`OperationContext.progress(percent, message, **details)` 也是同步的，可直接转：

- `percent = 0 if total <= 0 else min(99.0, done / total * 100)`，完成时由任务服务置 100。
- `details` 只透传 `stage` 与 `progress_type`；`speed`、`total_files`、`downloaded_files` 可作为附加字段，但必须在 `TaskView` 之外（`TaskView` 只有 `percent`/`stage`/`message`），不进结果 DTO。

取消适配需要一个小型 watcher 线程，模式与 `base.py:385-396` `_watch_downloader_cancel` 相同：

```python
cancel_event = Event()
finished = Event()

def watch() -> None:
    while not finished.wait(0.2):
        if context.cancel_event.is_set():
            cancel_event.set()
            return
```

`context.cancel_event` 是 `OperationContext` 的公开字段（`operations.py:59`），不需要新增接口。任务结束时 `finished.set()` 并 `join(timeout=2)`，与现有实现保持一致。

`install_blocking` 在阶段边界抛 `GameServiceError("INSTALL_CANCELLED")`（`install.py:340-343`）；worker 捕获后转换为 `OPERATION_CANCELLED`，让 `OperationManager` 走统一的取消终态，不新增错误码。

### 6.4 提交点与 effect

安装没有原子提交点，效果必须如实表达：

| 时刻                                              | effect         | 依据                |
| ----------------------------------------------- | -------------- | ----------------- |
| 接纳时                                             | `none`         | 尚未写任何文件           |
| `build_*_download_list` 之后（`install.py:430` 附近） | `partial`      | 版本 json 已落盘，可被扫描到 |
| 下载全部完成                                          | 终态 `committed` | 业务目标达成            |
| 失败或取消且已有写入                                      | 终态 `partial`   | 残留半套文件            |

`report_effect` 只接受 `none`/`staged`/`partial`/`unknown`（`operations.py:97-108`），`committed` 由 `finalize` 更新，因此 worker 在清单构建完成后调用 `context.report_effect("partial")`，下载完成时调用 `context.finalize(...)` 交给任务服务收尾。这与 P09 文件写入的语义一致，不引入新规则。

### 6.5 并发互斥

按 `(游戏根目录规范路径, 目标版本名)` 加互斥，不再依赖调用方 `task_id`。互斥表由 `InstallCoordinator` 持有（沿用其 `RLock`，`base.py:222`），在任务接纳点登记、在任务终态清理时移除。冲突返回 `PLUGIN_INSTALL_BUSY`。


这条规则同时约束用户界面安装：用户装同一个版本时，插件请求返回忙；插件在装时，用户在界面点安装应看到"该版本正在安装"，而不是两个下载器抢同一批文件。前端需要相应文案。

### 6.6 失败清理

本切片**不实现安装回滚**。理由是：`versions/<name>/` 下已有合法 json 与部分文件，删除会破坏用户可能已经开始的增量补全，也可能删掉其他来源写入的内容。做法是：

- 失败或取消后保留残留，`effect=partial`，由 `operations.get` 如实呈现。
- 不新增后台清理流程，不扫描、不自动删除用户文件（AGENTS.md 第 2 节）。
- 用户可重新发起同一版本安装，走增量补全路径（`install.py:437-439` 清单为空即返回完成）。

### 6.7 宿主固定行为

- 下载源：沿用当前有效配置解析结果，插件不可传。
- `include_fabric_api`：首版固定 `True`，与界面行为一致；不开放给插件选择。
- Java：forge/neoforge 由宿主 `_resolve_java_path`（`launch.py:389`，在 `install.py:183` 调用）解析，插件不可指定。
- 进度事件：`install_blocking` 内部通过 `report` 上报，不再额外 `emit("game:install_progress")`，避免插件任务在全局事件总线广播。

## 7. P12-C 启动与停止接入改造

### 7.1 来源标记（最小改造）

用户已决定保持全局单任务。因此不改 `_launch_cancel_event` 的结构，只增加两项状态：

- `LaunchCoordinator._launch_origin: OperationOrigin | None`——当前启动流程的来源；`None` 表示用户发起。
- `_RunningGame.origin: OperationOrigin | None`（`base.py:82` 新增字段）——该进程由谁发起。

`game.launch` 在进入 `launch_instance` 前检查 `_launch_cancel_event`：非空即返回 `PLUGIN_LAUNCH_BUSY`，不透传 `LAUNCH_ALREADY_RUNNING`。用户界面的 `game_launch` 遇到插件占位时，错误消息改为"插件正在启动 XXX"，由前端展示，不改变错误码。

### 7.2 任务形态

启动总时限 180 秒，但 `launch_instance` 在 `create_instance` 之后还要继续运行（`post_launch`、统计、事件，`launch.py:1289-1325`），不能把整个协程塞进 `OperationManager` 的同步线程池。做法是**两段式**：

1. 提交阶段：`RuntimeGameLaunch.submit` 通过 `RuntimeOperations.submit` 登记一个 `plugin_game_launch` 任务，任务体负责"到提交点为止"的全部工作（`launch.py:887-1272` 之前的阶段）。
2. 提交阶段成功后任务进入 `committing`，由 `context.finalize` 调用 `create_instance`；创建成功后任务终态为 `committed`，结果为 `GameRunView`。`post_launch` 与统计在任务终态之后继续，由 `LaunchCoordinator` 原有逻辑处理。

这需要把 `launch_instance` 中"提交点之前"的部分抽成一个可在线程池运行的同步函数 `launch_until_commit(...)`，返回一个待提交对象（含命令、环境、实例 id、`_RunningGame` 草稿）。抽取边界必须正好落在 `launch.py:1265-1272` 之间：之前全部是准备与校验，之后是进程创建。

这是本切片最大的改动，也是唯一能让"提交闸门"成立的方式。若不接受此改造，则 `game.launch` 只能降级为"触发式"（立即返回 `queued`，真实状态全靠 `game.running` 轮询），与总体设计 13.7 的 `TaskReceipt` + `GameRunView` 语义不符。

### 7.3 提交闸门

`admission.check` 在提交点前最后一次执行，复核：

- `instances.launch` 租约与实例物理身份仍有效；
- 若带 `runtime_ref`，`java.use` 租约仍有效且运行时身份未变；
- 当前账户仍存在（`PLUGIN_ACCOUNT_REQUIRED`）。

提交点之后撤销授权不杀进程，`game.stop` 仍可按原租约处理（清理类操作允许在授权撤销后减少资源），与总体设计 15.2 的状态矩阵一致。

### 7.4 取消边界

| 阶段 | 取消能否阻止创建进程 |
| --- | --- |
| 文件补全/下载（`launch.py:1102-1160`） | 能，经 `downloader.stop()` |
| 取令牌（`973`）、authlib（`1018`）、`check_files`（`1094`）、`options` 写入（`1163`）、命令组装（`1207`） | **不能**，只能等阶段结束 |
| `pre_launch` 命令（`1222`，最长 60 秒） | **不能** |
| `create_instance` 之后 | 不能，且不会杀已建进程 |

`operations.cancel` 返回的 `effect` 必须如实反映上述边界；`PLUGIN_COMMIT_STARTED` 在 `committing` 阶段返回，不伪报回滚。

### 7.5 账户

插件不传账户，也不获得账户信息。宿主使用 `accounts.current_account()`；无当前账户返回 `PLUGIN_ACCOUNT_REQUIRED`。`LaunchOptions` 不含账户字段，未来若需要"指定账户启动"，必须单独评审账户可见性。

### 7.6 `game.stop`

`game_run_ref` → `_RunningGame`，校验 `origin` 与本运行一致，不一致返回 `PLUGIN_OWNER_MISMATCH`（不透露对方身份）。停止动作复用 `LaunchCoordinator.stop_instance(instance_id)`（`launch.py:1562`），但需确认它作用于确切 token：当前按 `instance_id` 停止，若同一实例有多次运行记录，必须按 token 定位，否则会误停。这是实施前必须核实并修正的点。

停止是减少资源，允许在 `instances.stop` 授权撤销后完成；`release` 类语义按总体设计 13.2 的 C 类处理。

### 7.7 `game.running` 与启动任务的关系

启动任务进入 `committed` 后返回 `GameRunView`，此时 `game_run_ref` 才可被 `game.stop` 使用。任务未提交时 `game.running` 不返回该进程（进程还不存在）。进程退出后 `_RunningGame.exited` 置位，`game.running` 仍返回最后一次 `exit_code`，直到 run 结束或引用过期。

## 8. 逐方法契约

### 8.1 请求与响应

| 方法 | params | 成功 data | 额外错误 |
| --- | --- | --- | --- |
| `game.catalog` | `{type?:"release"\|"snapshot",cursor?,limit?}` | `Page<{version_id,type,release_time}>` | `PLUGIN_SCHEMA_INVALID`、`PLUGIN_CURSOR_INVALID`、`PLUGIN_GAME_CATALOG_FAILED` |
| `game.loaders` | `{game_version,loader,cursor?,limit?}` | `Page<{loader_version,is_latest}>` | `PLUGIN_LOADER_NOT_SUPPORTED`、`PLUGIN_CURSOR_INVALID` |
| `game.install` | `{root_ref,version_id,version_name?,loader?,loader_version?}` | `{operation_ref,state:"queued"}` | `PLUGIN_INSTALL_BUSY`、`PLUGIN_INSTALL_FAILED`、`PLUGIN_FILE_EXISTS`、`PLUGIN_LOADER_NOT_SUPPORTED` |
| `game.running` | `{instance_ref}` | `{items:GameRunView[]}` | `PLUGIN_INSTANCE_INVALID` |
| `game.launch` | `{instance_ref,options?}` | `{operation_ref,state:"queued"}` | `PLUGIN_LAUNCH_BUSY`、`PLUGIN_LAUNCH_FAILED`、`PLUGIN_ACCOUNT_REQUIRED` |
| `game.stop` | `{game_run_ref}` | `{operation_ref,state:"queued"}` | `PLUGIN_OWNER_MISMATCH`、`PLUGIN_GAME_STOP_FAILED` |

### 8.2 DTO

| DTO | 字段 |
| --- | --- |
| `LaunchOptions` | `{memory_mb?:512..65536,width?:320..16384,height?:240..4320,fullscreen?:bool,runtime_ref?:string}`；不接收命令、wrapper、脚本、env |
| `GameRunView` | `{game_run_ref,instance_ref,pid:int,running:bool,exit_code:int\|null}`；不返回 `game_path`、`game_directory`、java 租约、启动命令 |

`GameRunView` 的 `game_run_ref` 只在本运行内有效，跨 run、跨安装、跨内容摘要一律 `PLUGIN_RESOURCE_NOT_FOUND`。

### 8.3 结果投影纪律

所有结果必须显式构造，禁止 `model_dump()` 或直接返回服务结果。特别地：

- `game.catalog` 不返回 `id`/`releaseTime` 原始键，不返回下载地址、sha1。
- `game.install` 最终结果返回 `InstanceView`（复用 P07 的投影），不返回安装目录。
- `game.launch` 最终结果返回 `GameRunView`，不返回 JVM 参数或日志。
- `game.running` 不返回其他来源进程。

## 9. 代码边界与模块清单

| 文件 | 动作 | 内容 |
| --- | --- | --- |
| `ECL/plugins/runtime/game_queries.py` | 新增 | `RuntimeGameCatalog`：catalog/loaders/running 三个只读查询与分页投影 |
| `ECL/plugins/runtime/game_install.py` | 新增 | `RuntimeGameInstall`：安装请求校验、根目录租约、任务提交 |
| `ECL/plugins/runtime/game_launch.py` | 新增 | `RuntimeGameLaunch`：启动/停止请求校验、来源标记、提交闸门 |
| `ECL/plugins/runtime/models.py` | 修改 | 新增 6 个请求模型与 `GameRunView`、`LaunchOptions` DTO |
| `ECL/plugins/runtime/broker.py` | 修改 | 分派 `game.*`；新增能力进 `implemented_capabilities`；写方法进 `mutation_methods` |
| `ECL/plugins/runtime/resources.py` | 修改 | 增加安装根目录目标解析与实例启动目标解析（含物理身份复核） |
| `ECL/plugins/resource_catalog.py` | 修改 | 暴露可写游戏根候选与实例候选 |
| `ECL/plugins/grant_models.py` | 修改 | 三项新能力的动态范围校验 |
| `ECL/services/operations.py` | 修改 | 新增任务类型标题；`_RunningGame` 不在此文件，但需确认 `max_owner_operations` 足够 |
| `ECL/services/game/install.py` | 修改 | 抽出可在无事件循环线程调用的入口；按 `(root, version_name)` 互斥；移除插件路径对 `task_id` 的依赖 |
| `ECL/services/game/launch.py` | 修改 | 抽出 `launch_until_commit`；`_launch_origin`；`stop_instance` 按 token 定位 |
| `ECL/services/game/base.py` | 修改 | `_RunningGame` 新增 `pid`、`origin` |
| `docs/protocols/eclplugin-package-format.md` | 修改 | 新增"获准游戏安装与启动"章节 |
| `examples/plugins/worker_game_install/` | 新增 | 最小示例 |
| `examples/plugins/worker_game_launch/` | 新增 | 最小示例 |
| `frontend/src/features/plugins/` | 修改 | 权限弹窗支持游戏根与实例候选；"插件正在启动"文案（六语言） |

## 10. 测试计划

| 用例文件 | 覆盖 |
| --- | --- |
| `tests/test_plugin_game_queries.py` | catalog/loaders 分页与游标绑定、非法 type/loader、无能力拒绝、断线后不交付结果 |
| `tests/test_plugin_game_running.py` | 只返回本运行进程、跨 run 引用拒绝、退出后 exit_code 保留、pid 不泄漏路径 |
| `tests/test_plugin_game_install.py` | 无租约拒绝、目标已存在拒绝、并发同版本返回 BUSY、取消后 effect=partial、失败残留保留、增量补全成功、超时后 operations.get 真实状态 |
| `tests/test_plugin_game_launch.py` | 用户启动时 BUSY、插件启动时用户 BUSY、无账户 `PLUGIN_ACCOUNT_REQUIRED`、提交前撤销阻止创建、提交后撤销不杀进程、提交阶段取消边界如实 |
| `tests/test_plugin_game_stop.py` | 只能停自己启动的、跨 run 拒绝、停止后 exit_code 正确、重复停止幂等 |
| `tests/test_plugin_operation_ownership.py`（既有） | 回归：新任务类型不破坏 owner 隔离与配额 |
| `tests/test_game_service.py`、`tests/test_install*`（既有） | 回归：用户界面安装/启动行为不变 |

门禁按 AGENTS.md 第 6 节：`ruff check ECL tests`、`ruff format --check ECL tests`、相关回归、前端 `pnpm check` 与 `pnpm build`。涉及 Core 的改动按子仓库规范单独提交。

## 11. 阻塞条件

出现以下任一情况，停止当前阶段并提交修订，不得降级绕过：

1. `install_blocking` 在 `loop=None` 下与主循环行为不一致（例如下载器状态绑定主循环），且无法在不改 Core 的前提下修正。
2. `launch.py` 的"提交点之前"无法在不改变用户启动行为的前提下抽成同步函数。
3. `stop_instance` 无法按 token 精确定位，只能按实例停止——那样会误停用户进程，必须改为暂停 `game.stop`。
4. 新增能力无法通过资源目录做精确候选（例如只能给"整个磁盘"粒度），则 `instances.install` 保持关闭。
5. 真实 WebView2 桌面验收中，权限弹窗无法正确展示游戏根候选。

## 12. 回滚边界

- 代码回滚不撤销已安装的文件、已启动的进程、已写入的统计。`effect` 与 `exit_code` 是用户判断的依据。
- 撤回本切片时，`game.*` 方法移出 `implemented_capabilities` 与分派；插件清单中的相应能力变为"未实现"，已有授权保留但不可用，不解释为 allow。
- 不删除用户 `versions/` 下的任何内容，不删除 `eclversion.json` 统计，不重置 GPU 偏好。
- 前端权限弹窗对已失效能力显示"当前版本不支持"，不静默隐藏。

## 13. 实施顺序与审阅门

1. 本文审阅通过，明确"开始实施"。
2. P12-A 实施 → 局部回归 → 提交 → 更新总体设计第 29 节实施记录。
3. P12-B 实施 → 真实安装一次 Fabric 与一次 Forge 版本 → 验收记录。
4. P12-C 实施 → 真实启动/停止、并发冲突、撤销场景 → 验收记录。
5. 三阶段全部通过后，更新协议文档与示例，标记 P12 完成。

P12 完成不代表总体设计首版完成：P11（Java）、P13（工具进程）、P14（扩展登记）、P15、P16、P17 仍在范围内。
