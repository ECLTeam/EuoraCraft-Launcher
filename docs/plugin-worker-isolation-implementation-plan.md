# 插件 Worker 隔离与资源限制实施方案

历史草案：用户已取消 Worker 和资源限制方向。当前实现依据 [安装目录隔离方案](plugin-install-directory-isolation-plan.md)，本文件不作为实施或验收依据。

## 1. 决策与目标

本方案将插件从启动器宿主进程中移出：所有普通插件均运行于独立的 Python Worker 子进程，宿主只通过本地 IPC 向它们暴露经过权限校验的能力。当前插件生态尚未起步，允许破坏性变更，不保留旧的进程内插件 API 或兼容模式。

系统内置插件是否 Worker 化由宿主固定决定；第一期保留它们在宿主进程中运行，因为它们与发行物同源、不可由用户安装或禁用。本方案的安全目标是限制第三方插件造成的 CPU、内存、进程数量、卡死和崩溃影响，并使禁用/卸载能够终止整个插件进程树。

资源限制不等同于权限沙箱。Worker 仍以当前用户身份运行，第一期不能阻止它直接读写当前用户可访问的文件或自行访问网络；这部分列入后续受限令牌/AppContainer 阶段，不能在产品文案中宣称已经实现文件或网络隔离。

## 2. 现状与问题边界

当前 `PluginManager` 使用 `importlib` 将 `plugin.json` 的入口模块载入启动器主进程。`Plugin` 实例直接持有 `framework` 和 `processes`；即使声明式权限会校验框架 API，任意插件代码仍可导入 Python 标准库或 ECL 内部模块并自行建立进程、网络或文件访问。

现有 `ProcessService` 是面向游戏与插件外部工具的通用子进程清单，未记录插件所有者，未在调用 `spawn()` 时校验权限，且未限制 CPU、内存、活跃进程数或进程树。插件命令超时目前只会让调用方返回超时，不会停止仍在宿主线程池里执行的代码。

因此，单独扩展 `ProcessService` 不能隔离插件本体；必须先把插件解释执行移入受限 Worker，随后让宿主成为唯一的能力代理。

## 3. 目标架构

```text
普通插件代码
    │ SDK 调用 / 生命周期回调
    ▼
Plugin Worker（每插件一个 Python 子进程）
    │ 认证的本地 IPC；消息大小、并发和超时受限
    ▼
PluginWorkerSupervisor（宿主）
    ├─ PermissionManager：按 plugin.json 校验 capability
    ├─ PluginManager：注册路由、命令、事件、设置与扩展点
    ├─ ProcessService：仅通过宿主代理创建可见的插件工具进程
    └─ Windows Job Object：限制并管理 Worker 及其全部子进程
```

每个插件的 Worker、由 Worker 直接派生的子进程、以及经宿主代理启动的插件工具进程必须属于同一个 Job Object。插件禁用、卸载、Worker 协议错误、超时或启动器关闭时关闭/终止该 Job，从而回收整棵进程树。

## 4. 新的插件契约

### 4.1 清单

`plugin.json` 不再接受进程内运行模式。普通插件必须声明 `apiVersion: 2` 与入口点，宿主拒绝加载旧格式。系统插件的运行方式不由清单控制。

```json
{
  "name": "example",
  "apiVersion": 2,
  "entryPoint": "main:ExamplePlugin",
  "permissions": [
    {"scope": "events", "action": "subscribe", "resource": "game:instances_changed"},
    {"scope": "process", "action": "execute", "resource": "tool:*"}
  ]
}
```

资源上限不得由不可信插件清单声明；它们由宿主的固定默认策略或用户在启动器设置中调整的全局策略决定。

### 4.2 SDK 与回调模型

新增仅供 Worker 使用的 SDK，上层不再得到 `PluginManager`、`EventBus`、`ProcessService`、HTTP 客户端或任何服务对象的引用。SDK 的每一个能力调用都形成结构化 IPC 请求，宿主先进行类型、大小、资源路径和权限校验，再调用内部服务。

装饰器保留“声明式注册”的使用体验，但注册内容改为可序列化的 `callback_id`：

- 生命周期、命令、设置、路由和 UI 注入改为 SDK 请求；
- 事件订阅在宿主注册一个非阻塞转发器，事件没有返回值；
- 认证、实例读取、联机、启动钩子、崩溃富化等需要返回值的扩展点改为“结构化请求/响应”回调，禁止跨进程传递 Python 对象与闭包；
- 所有参数和结果均限制为 JSON 数据模型，拒绝 `Path`、任意对象、可调用对象和未声明字段；
- 命令、认证及同步扩展点超时即终止对应 Worker，并将插件标为 `runtime_failed`，不会留下宿主线程继续运行。

事件通知不等待插件完成；同步扩展点按能力使用独立的短超时，避免一个 Worker 卡住启动、账号或游戏启动流程。

## 5. IPC 与运行时设计

### 5.1 传输与认证

新增 `PluginWorkerSupervisor`，由它创建每插件独占的 Windows 命名管道端点，并以一次性随机认证密钥启动 Worker。传输使用 Python 标准库的 `multiprocessing.connection`（Windows `AF_PIPE`）或等价的封装；不使用 TCP 监听端口。

协议使用带 `request_id` 的 JSON 信封：`hello`、`request`、`response`、`notification`、`callback`、`error`、`shutdown`。实现必须强制：

- 首帧只允许认证握手，名称、API 版本与随机 nonce 必须匹配；
- 单帧最大 1 MiB，嵌套层数、字符串长度、数组长度和未识别字段受限；
- 单 Worker 同时最多 16 个宿主请求，超过时立即返回过载错误；
- 协议解析错误、认证失败、未知回调 ID 或连续超时均断开并终止 Worker；
- Worker 日志走独立的限速通道，不能污染 RPC 帧，单条和累计输出均截断并标记。

### 5.2 生命周期与状态

状态改为 `starting → loaded → enabled → stopping → disabled`，并新增 `runtime_failed`。管理器在 Worker 建连并完成 `on_load` 后才登记插件实例；`on_enable`、`on_frontend_ready`、`on_disable`、`on_unload` 都由 Supervisor 调用并有明确超时。

禁用、卸载和应用关闭遵循同一顺序：拒绝新请求、撤销宿主注册项、发送有界 `shutdown`、等待短暂宽限、`TerminateJobObject` 兜底、关闭管道与 Job 句柄、更新状态。Worker 异常退出采用同一清理路径，不能留下路由、事件订阅、认证提供方或工具进程。

## 6. 资源限制策略

第一期在 Windows 上以每插件一个 Job Object 实现硬限制。默认值应集中在不可由插件修改的 `PluginResourcePolicy`：

| 资源 | 默认值 | 强制方式 |
| --- | ---: | --- |
| CPU | 全机 CPU 时间的 25% | `JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP` |
| 总提交内存 | 512 MiB | `JOB_OBJECT_LIMIT_JOB_MEMORY` |
| 单进程提交内存 | 384 MiB | `JOB_OBJECT_LIMIT_PROCESS_MEMORY` |
| 活跃进程数 | 4 | `JOB_OBJECT_LIMIT_ACTIVE_PROCESS` |
| 同步命令 | 30 秒 | Supervisor 超时后终止 Job |
| 生命周期/同步扩展 | 3–10 秒，按能力配置 | Supervisor 超时后终止 Job |
| IPC 单帧 | 1 MiB | 协议解析前检查 |

Job 配置还应使用 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 并禁止 breakaway，使 Worker 派生的子进程默认继承同一 Job。CPU 限速按 Job 中全部进程合并计算；内存限制是提交内存而非 Python 对象大小。设定值必须经过 Windows 版本能力检测；若 Job 创建、限额设置或进程分配失败，普通插件不得降级为无约束运行，而是标记为 `runtime_failed` 并显示原因。

Linux/macOS 适配器不属于第一期验收范围。后续 Linux 实现应优先使用 cgroup v2 的 `cpu.max`、`memory.max` 与 `pids.max`；没有可验证的内核能力时也必须拒绝启动受限普通插件，不能静默放行。

## 7. 进程能力收紧

新增 `PermissionScope.PROCESS` 与 `PermissionAction.EXECUTE`。插件只能通过 SDK 的 `spawn_process()` 启动经宿主代理的工具进程：

- 每个实例强制绑定插件 owner；查询、写入 stdin、停止和日志订阅均只允许其 owner；
- `type` 固定为 `plugin:<plugin_name>:<purpose>`，不能由插件伪造其他插件或 Minecraft 类型；
- 参数必须为 argv 数组，拒绝 shell 字符串；工作目录必须位于插件数据目录或获授权的目录；
- 环境变量采用宿主白名单并删除启动器敏感配置；
- 工具进程分配进该插件 Job，复用同一资源预算；
- 前端进程页面仅展示经过脱敏的 owner 和实例信息，不能跨插件发送 stdin 或停止进程。

Worker 若绕过 SDK 自行调用 `subprocess`，其后代仍受 Job 的资源限制和关闭回收；但宿主不会把该进程视作受支持的工具实例。文件与网络访问仍不是本期可强制限制的范围。

## 8. 实施文件与步骤

1. 新增 `ECL/plugins/runtime/contracts.py`、`policy.py`、`protocol.py`：Pydantic/数据类协议、状态、错误码与资源策略；不允许 `Any` 穿过解析边界。
2. 新增 `ECL/plugins/runtime/supervisor.py`、`worker_runner.py`、`worker_sdk.py`：Worker 启动、命名管道、认证、RPC 调度、回调分发、日志限流和生命周期状态机。
3. 新增 `ECL/plugins/runtime/limits/windows_job.py`：用 `ctypes` 调用 Windows Job Object，封装句柄、结构体、错误翻译、关闭与能力检测；不额外引入 pywin32 依赖。
4. 重写 `ECL/plugins/plugin.py` 与 `manager/discovery.py`：替换 `importlib` 宿主载入、`framework` 注入和旧 `Plugin` 基类；拒绝 API v1 普通插件。
5. 改造 `manager/lifecycle.py`、`registry.py` 和扩展注册表：以 Worker proxy 与 `callback_id` 代替本地函数引用；同步扩展调用必须受 Supervisor 超时管理。
6. 改造 `ECL/services/processes.py` 与 `ECL/game/Core/InstancesManager.py`：加入 owner、Job 分配、严格 argv、操作鉴权和安全的进程树终止；游戏实例不进入插件 Job。
7. 更新 `ECL/plugins/permissions.py`、API 模型和前端插件管理页面：展示 Worker 状态、资源策略、异常退出原因与受限进程归属。
8. 迁移 `resources/system_plugins` 至新 SDK 或明确系统内置运行适配器；删除旧进程内普通插件加载路径与相关测试夹具。
9. 更新插件开发文档、权限参考和威胁模型，明确资源隔离边界与未来 OS 权限沙箱路线。

## 9. 测试与验收

后端测试必须新增并覆盖以下行为：

- API v1、无效入口、认证失败、超限 IPC、未知 callback、Worker 崩溃和连续超时均不会载入插件；
- 权限在宿主代理端校验，伪造 owner、跨插件停止/输入/查看工具进程均被拒绝；
- 生命周期清理能撤销路由、UI、事件、账户、联机、启动钩子和工具进程；
- 命令无限循环或回调卡死会被超时终止，启动器主进程保持可用；
- Windows Job Object 测试验证 Worker 与其子进程被分配、关闭插件会杀掉树、活跃进程数/内存/CPU 配置被正确传入；资源极限测试在 CI 不稳定时用可注入的 Win32 API 假实现验证调用参数，并在 Windows 集成测试中验证杀树；
- ProcessService 回归测试覆盖 owner 隔离、argv 拒绝、环境清理和 Job 分配；游戏实例回归测试保证不被插件关闭；
- 完成 `ruff check ECL tests`、`ruff format --check ECL tests`、全部相关 `pytest`；涉及前端时执行 `cd frontend && pnpm check`、`pnpm build` 和实际启动验证。

验收通过的最低标准是：一个故意无限循环、持续分配内存并派生子进程的测试插件，不能使宿主失去响应；禁用它后 Worker 与后代全部结束，其他插件和 Minecraft 实例仍继续运行；其所有宿主能力请求均可在权限、owner 和协议边界被审计与拒绝。

## 10. 后续安全阶段（不属于本次实施）

在 Worker 稳定后，再评估 Windows 受限令牌/AppContainer、每插件数据目录 ACL、网络 capability 与可信签名/插件商店。只有该阶段完成并经真实发行环境验证后，才可将“文件/网络权限隔离”作为安全承诺。
