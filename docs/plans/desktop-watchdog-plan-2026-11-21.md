# 桌面看门狗子进程实施方案（2026-11-21）

## 1. 背景与目标

主进程（pytaurix 事件循环 + asyncio portal）一旦真卡死，用户只能面对无响应窗口且无任何现场证据。本方案引入一个看门狗子进程：主进程事件循环定期发心跳，超过 5 分钟无心跳时，看门狗先写卡死现场报告、再终止主进程并自行退出。

**覆盖边界（明确定义，不做扩大解释）**：看门狗检测的是「Python 业务层/事件循环停转」（含 GIL 被 C 扩展占死）。WebView 原生层冻结而 Python 侧仍活着的情况不触发，属于不覆盖范围。

## 2. 现状调研结论

- 进程角色分流集中在 `ECL/bootstrap.py`：`ProcessRole` 已有 `plugin` / `plugin-guardian`，`run_process` 按角色延迟导入入口。看门狗复用同一分流模式。
- 发行入口解析可复用 `ECL/plugins/runtime/executable.py` 的 `ExecutableLaunchSpec`（源码 / PyInstaller / Nuitka 三态均已处理）。
- Windows 已有 `WindowsJob`（`ECL/plugins/runtime/windows.py`）：挂起创建、先行入 Job、KILL_ON_JOB_CLOSE，可直接复用来杜绝看门狗孤儿进程。
- 心跳必须从事件循环侧发出（asyncio portal 任务），不能放普通线程——否则主循环冻死时心跳线程仍活着，检测永远不触发。
- 日志目录为 `data_path/logs`（`ECL/utils/logging.py`）。
- 数据安全依赖项目既有机制：原子写入、事务恢复、安装失败回滚均为既有行为；游戏进程独立于主进程存活。

## 3. 设计

### 3.1 进程角色与参数（ECL/bootstrap.py）

- `ProcessRole` 新增 `watchdog = "watchdog"`。
- 新增 `WatchdogOptions`（frozen dataclass）：`host_pid: int`、`run_id: str`。
- `select_process_route` 扩展：`--headless --role watchdog --host-pid N --run-id <32位hex>`；
  校验规则对齐插件角色（参数完整、不重复、PID 范围、run-id 格式），endpoint 不参与看门狗角色。
- `run_process` 分发到 `ECL.desktop.watchdog_child.run_watchdog`。

### 3.2 协议（新模块 ECL/desktop/watchdog_protocol.py，仅标准库）

宿主与看门狗经 stdin/stdout 管道交换长度前缀 JSON 帧（4 字节大端长度 + JSON），与 `_guardian.py` 同风格：

- 宿主 → 看门狗：`{"hello": {"timeout_seconds": int, "log_path": str}}`（连接后第一帧）、
  `{"heartbeat": true}`（每 10 秒）、`{"shutdown": true}`（正常退出）。
- 看门狗 → 宿主：`{"ready": true, "pid": int}`（就绪后第一帧）。

`timeout_seconds` 由宿主下发（默认常量 300，范围钳制 60—3600），便于测试用短超时。

### 3.3 宿主侧（新模块 ECL/desktop/watchdog.py）

`WatchdogSession`，由 `EuoraCraftLauncher` 持有（进程生命周期归属启动器）：

- `start()`：
  - 通过 `ExecutableLaunchSpec` 构造看门狗启动参数（`executable_prefix()` 新方法返回
    `[exe, (源码模式追加 "-S main.py")]`，看门狗模块自行拼接角色参数，不改动插件语义的 `arguments()`）。
  - Windows：用 `WindowsJob` 挂起创建 → 入 Job → 恢复；宿主死亡时 Job 句柄关闭连带终止看门狗，杜绝孤儿。
    POSIX：直接 `subprocess.Popen`（宿主死亡 → 管道 EOF → 看门狗静默退出）。
  - 等 `ready` 帧上限 5 秒；任何失败只记 warning 并放弃监管，**绝不阻塞或让启动失败**（看门狗是尽力而为的安全网）。
  - 启动一个写线程：每 10 秒检查一次「事件循环存活信号」，有新信号才写心跳帧；写失败则标记失效并静默结束。管道 IO 全部在写线程，异步路径零阻塞。
- `notify_alive()`：事件循环侧调用，仅在锁内更新一个单调时间戳（非阻塞）。
- `stop()`：发 `shutdown` 帧 → 关闭 stdin → 有界等待（2 秒）→ 关闭 Job（Windows）/ terminate（POSIX）。幂等。

看门狗开关：新增配置 `launcher.watchdog_enabled`（默认 `true`）。关闭时既不起子进程也不发心跳；`notify_alive` 退化为空操作。

### 3.4 心跳接入点（ECL/desktop/tauri.py）

- `Adapter.__init__` 新增可选参数 `watchdog: WatchdogSession | None = None`（`EuoraCraftLauncher.run()` 传入，默认 None 保证既有测试兼容）。
- `Adapter.run()` 内，进入 `start_blocking_portal` 后用 `portal.start_task_soon` 启动心跳协程：
  `while True: watchdog.notify_alive(); await asyncio.sleep(10)`，退出 portal 时协程随之结束。
- 主进程正常退出路径（`_shutdown`）中调用 `watchdog.stop()`。

### 3.5 看门狗子进程（新模块 ECL/desktop/watchdog_child.py）

仅依赖标准库 + `ECL.bootstrap`，复用统一文件头规范：

1. 校验 `os.getppid() == host_pid`，不符退出码 2（防误连与复用）。
2. 读 `hello` 帧（10 秒上限），钳制并保存 `timeout_seconds` 与 `log_path`（须为绝对路径）。
3. 回 `ready` 帧；之后主循环每 0.5 秒 tick：stdin 读线程更新最后心跳时间戳。
4. 任何帧读失败 / EOF（宿主正常退出或崩溃）→ 退出码 0，**不做任何终止动作**。
5. 收到 `shutdown` → 退出码 0。
6. 最后心跳距今超过 `timeout_seconds` → 依次执行：
   - 向 `log_path`（`data_path/logs/watchdog-hang.log`）追加纯文本卡死报告：时间、宿主 PID、超时值、看门狗 PID；
   - 终止宿主（Windows：ctypes `OpenProcess(PROCESS_TERMINATE)` + `TerminateProcess`；POSIX：`os.kill(SIGKILL)`）；
   - 退出码 3。

### 3.6 分层归属

三个新模块均在 `ECL/desktop/`（内核域）；`watchdog.py` 复用 `ECL/plugins/runtime/`（同域依赖，允许）。不引入任何 `ECL.minecraft` 依赖。

## 4. 改动文件清单

| 文件 | 改动 |
| --- | --- |
| `ECL/bootstrap.py` | 新增 watchdog 角色、`WatchdogOptions`、路由校验与分发 |
| `ECL/desktop/watchdog_protocol.py` | 新增：帧编解码（仅标准库） |
| `ECL/desktop/watchdog.py` | 新增：`WatchdogSession`（宿主侧） |
| `ECL/desktop/watchdog_child.py` | 新增：`run_watchdog`（看门狗入口） |
| `ECL/plugins/runtime/executable.py` | 新增 `executable_prefix()` 方法 |
| `ECL/desktop/tauri.py` | `Adapter` 接受看门狗会话并在 portal 内启动心跳协程 |
| `ECL/launcher.py` | 启动/停止看门狗会话，读取配置开关 |
| `ECL/utils/config.py`（默认配置） | 新增 `launcher.watchdog_enabled`，默认 `true` |
| `tests/test_desktop_watchdog.py` | 新增测试 |

## 5. 测试计划

1. **路由解析**：watchdog 角色参数合法/非法组合（缺参、重复、格式错误）。
2. **协议**：帧编解码 round-trip、坏帧拒绝。
3. **子进程集成**（短超时 2 秒）：
   - 宿主停发心跳 → 看门狗写报告文件并终止替身宿主进程（`python -c sleep` 占位进程），退出码 3；
   - `shutdown` → 退出码 0；
   - 宿主直接 EOF → 退出码 0 且无终止动作；
   - ppid 不符 → 退出码 2。
4. **宿主侧**：start 失败（无法解析的入口）只告警不抛出；`notify_alive`/写线程节拍；`stop` 幂等。
5. **配置默认值**：`launcher.watchdog_enabled` 默认为 `true`（并入 `tests/test_config_defaults.py`）。
6. 全量：`ruff check` + `pytest tests/`（结构性改动，按规范跑全量）+ 架构边界测试确认分层。

## 6. 风险与既定取舍

- **误杀**：下载/安装等长任务都跑在事件循环的异步任务里，正常执行期间心跳持续；只有事件循环整体停转才触发。阈值 300 秒留足余量。
- **终止时的数据安全**：主进程被强杀依赖既有原子写入/事务/回滚机制；游戏进程独立存活，「运行中」状态追踪丢失（已在方案讨论中确认接受）。
- **不覆盖原生层冻结**：见第 1 节边界。
- **不自动重启**：按既定决策只留报告，重启策略待观察实际卡死频率后另行评估。
