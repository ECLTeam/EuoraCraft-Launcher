# 启动器快捷启动参数与单实例检测（第二期）实施文档

> 状态：待用户审阅（工作流第 5 步）。审阅通过后按第 5 节步骤实施。
> 前置：第一期命令行参数已落地（docs/cli-launch-options-plan.md，commit 4d85149）。

## 1. 背景与目标

在第一期参数框架上新增「快捷启动」能力：通过命令行直接拉起某个 Minecraft
实例（可选直达服务器/世界），并解决快捷方式场景下的双开问题。

用户已确认的决策：

- 触发形态选 **A：前端就绪后自动拉起**（不改动前端子模块）；
- 本期带**轻量单实例检测**（第二进程自行退出并置前已运行窗口），
  完整参数转发留第三期；
- 参数集锁定为 `--launch`（版本名 + 实例目录两种形态）、`--server`、`--world`。

## 2. 参数定义

| 参数 | 说明 |
|:---|:---|
| `--launch <目标>` | 快捷启动实例。目标两种形态：<br>① **版本名**：在候选游戏根中搜索 `versions/<名>/<名>.json`；<br>② **实例目录路径**：指向 `<游戏根>/versions/<名>`（可带尾分隔符），自动推导游戏根与版本名 |
| `--server <地址[:端口]>` | 配合 `--launch`：启动后经既有 `quick_target` 机制直连服务器（版本支持 `quickPlayMultiplayer` 时用之，否则降级 `--server/--port` 参数） |
| `--world <世界ID>` | 配合 `--launch`：经 `quick_target` 机制快速进入世界（版本需支持 `quickPlaySingleplayer`，否则报错弹窗） |

解析期校验（argparse 层）：`--server`/`--world` 必须与 `--launch` 同用，
两者互斥，地址/ID 非空且不含空白与 NUL 字符。
深层校验（版本能力、地址合法性、世界存在性）沿用 `quick_launch_arguments`
在启动时执行，失败经弹窗呈现。

## 3. 关键设计决策

1. **触发时机 = `frontend_ready` 之后**：主窗口就绪分支（bridge.py:755 起）
   末尾调度一次性启动任务。此时进度事件（`game:launch_progress`）可从 3%
   完整送达界面；启动失败经 `emit_popup_to_frontend`（`launcher:popup`
   在排队事件集合中，前端晚开也不丢）。以 `_cli_launch_dispatched` 标志
   保证每次进程运行只触发一次（窗口刷新重复调用 `frontend_ready` 不受影响）。
2. **完全复用 `game_launch`**：调度时组装 body 后调用既有
   `GameHandlers.game_launch`（api/game.py:468），与用户手点「启动」按钮
   行为一致（配置合并、文件补全、插件启动钩子、取消机制全部继承）。
   注意：`game_launch` 只从配置合并 width/height/fullscreen/jvm_args/
   game_args_tail/隔离策略等，**memory/lock_memory/process_priority 需由
   CLI 调度方从生效配置显式写入 body**（与前端从设置读取后传入的行为对齐）。
3. **目标解析推迟到调度时**：CLI 解析阶段配置尚未加载，`--launch` 只保存
   原始字符串；候选游戏根顺序 = `game.last_install_path`（最近使用）→
   `minecraft_paths`（兼容 `{path}` 字典与纯字符串两种条目，与
   bridge.py:731-735 的既有逻辑一致）。名称命中 0 个报「未找到」、
   多个报「存在同名实例」并列出候选。
4. **单实例检测采用「发现文件 + pid 存活校验 + 本地 TCP 握手」**，
   与 `dev_channel` 的既有模式完全对齐（dev_channel.py:130/250）：
   - 主实例：绑定 `127.0.0.1:0` → 原子写 `<data_path>/single_instance.json`
     `{port, token, pid, launcherVersion, protocolVersion}` →
     守护线程 accept，校验 token 后按 action 派发事件；
   - 第二实例：发现文件存在且 pid 存活、TCP 握手成功 → 发送
     `{"action": "focus", "argv": [...]}` → 打印提示并退出（退出码 0）；
   - 陈旧接管：发现文件存在但 pid 已死或连接被拒绝 → 删除发现文件，
     本次运行成为新的主实例；
   - 协议 `protocolVersion=1`，`action` 字段为第三期「参数转发」预留。
   （实施时相比本节初稿去掉了额外锁文件：发现文件 + pid 校验已覆盖
   接管场景，且与 dev_channel 保持同一套模式。）
5. **第二进程在日志初始化前探测**：探测点位于 `EuoraCraftLauncher.__init__`
   的 `configure_logging` 之前，移交成功即静默退出，避免两进程竞争
   同一日志文件的轮转句柄。
6. **置前走事件解耦**：主实例收到 focus 后 `events.emit("launcher:focus_request")`，
   适配器订阅该事件并调用既有 `focus_window()`（内部已处理无窗口时的
   `False` 返回，无需新逻辑）。
7. **新配置键 `launcher.single_instance`（默认 True）**：不加设置界面，
   可经 `ECL_CONFIG_LAUNCHER_SINGLE_INSTANCE=false` 关闭（多沙箱并存
   与测试场景使用）；关闭时双开行为与现状一致。

## 4. 改动点明细

### 4.1 `ECL/cli.py`

- `LaunchOptions` 新增 `launch_target: str | None`、`server_target: str | None`、
  `world_target: str | None`（非配置覆盖项，不经 `apply_launch_overrides`）。
- `build_arg_parser` 新增三个参数与解析期校验（配对、互斥、字符白名单）。

### 4.2 `ECL/utils/config.py`

- `default_config["launcher"]` 新增 `"single_instance": True`。

### 4.3 新增 `ECL/services/game/launch_target.py`

- `resolve_launch_target(target: str, roots: Sequence[Path]) -> tuple[Path, str]`：
  目标为目录时要求形如 `<根>/versions/<名>`（否则报错说明期望形态）；
  为名称时在 roots 中搜索首个/唯一命中，0 个与多个命中分别抛
  `GameServiceError`（稳定错误码 `LAUNCH_TARGET_NOT_FOUND` /
  `LAUNCH_TARGET_AMBIGUOUS`）。
- `candidate_roots(config: Mapping[str, Any]) -> list[Path]`：
  从 `game.last_install_path` 与 `minecraft_paths`（字典/字符串双形态）
  汇出去重后的候选根。

### 4.4 新增 `ECL/services/single_instance.py`

- `class SingleInstanceService(events, data_path, launcher_version)`：
  - `start()`：绑定随机端口、写发现文件（原子写）、启动守护监听线程；
    绑定失败时告警降级为无监听运行；
  - `close()`：关 socket、删发现文件（幂等）；
  - 服务线程为守护线程，accept 循环中每连接读单行 JSON、校验 token、
    `events.emit("launcher:focus_request")` 并回执；
  - 发现文件用既有原子写工具，未知 action 拒绝（协议位预留）。
- `probe_running_instance(data_path, argv, timeout=3.0) -> bool`：
  供启动器在日志初始化前调用；发现文件缺失 / pid 失效 / 握手失败时
  清理残留文件并返回 False，握手成功返回 True。

### 4.5 `ECL/application.py`

- `ApplicationContext` 新增 `single_instance: SingleInstanceService | None = None`；
- `create_application`：`launcher.single_instance` 为 True 时构造并
  `acquire()`（主实例才启动监听），加入 `created` 列表参与统一关闭
  （关闭顺序中最先创建、最后关闭，无资源依赖）。

### 4.6 `ECL/launcher.py`

- `__init__`：`get_runtime_info` 之后、`configure_logging` 之前，构造
  `SingleInstanceService` 并探测：
  - 已有主实例 → `notify_focus(sys.argv)` → 控制台提示「启动器已在运行，
    已激活已运行的窗口」→ 置内部标志；
  - 否则保留服务实例，交由 `create_application` 完成主实例接线
    （探测阶段只创建锁文件，监听线程在 `create_application` 中启动）。
- `run()`：标志置位时直接返回 `LauncherExitCode.SUCCESS`（不初始化日志
  文件、不构造后端）。

### 4.7 `ECL/api/bridge.py`

- `_FrontendState.__init__` 新增 `self._cli_launch_dispatched = False`；
- `frontend_ready` 主窗口就绪分支末尾：`state.launch_options.launch_target`
  存在且未派发过 → 置标志 → `asyncio.create_task(self._run_cli_launch())`；
- 新增 `_run_cli_launch()`：
  1. `candidate_roots(config)` + `resolve_launch_target(...)` 解析
     （异常 → `emit_popup_to_frontend` 错误弹窗并返回）；
  2. 从生效配置读取 `memory/lock_memory/process_priority` 写入 body；
  3. `--server`/`--world` 折算为 `quick_target` dict；
  4. `await self.game_launch(body)`，成功记录日志；失败（failure 响应或
     异常）→ `emit_popup_to_frontend({"id": "cli-launch-failed", ...})`。

### 4.8 `ECL/adapters/tauri.py`

- `_register_events` 新增
  `bus.subscribe("launcher:focus_request", lambda _payload: api.focus_window())`。

## 5. 实施步骤

1. `cli.py` 参数扩展与 `config.py` 新键。
2. 新增 `launch_target.py`（纯函数，先行实现+测试）。
3. 新增 `single_instance.py`（先行实现+测试）。
4. 接线 `application.py` → `launcher.py` → `bridge.py` → `tauri.py`。
5. 补齐测试（见第 6 节）。
6. 门禁：`ruff check ECL tests`、`ruff format --check ECL tests`、`pytest`、
   `cd frontend && pnpm check && pnpm build`。
7. 实际启动验证（6.2）后提交一次 commit：
   `feat: 新增快捷启动实例参数与单实例检测`。

## 6. 测试计划

### 6.1 单元测试

- `tests/test_cli_launch_options.py` 扩展：三参数解析；`--server`/`--world`
  缺少 `--launch`、两者同用、含空白地址 → 用法错误退出码 2。
- 新增 `tests/test_launch_target.py`：版本名命中/未命中/多命中；目录形态
  （`versions/<名>` 带尾分隔符）；候选根去重与字典/字符串双形态；
  非法目录形态报错。
- 新增 `tests/test_single_instance.py`（tmp_path 真实 socket）：
  主实例 acquire 成功（锁+发现文件+端口）；二次 acquire 返回 False；
  `notify_focus` 触发事件（token 正确）；token 错误被拒；pid 陈旧时接管；
  close 幂等清理文件。
- `tests/test_application_context.py` 扩展：`launcher.single_instance=false`
  时上下文不含该服务；默认开启时服务已创建。
- `tests/test_frontend_api_config.py` 扩展（沿用其 FrontendApi 伪造模式）：
  `frontend_ready` 带 `launch_target` 时恰调用一次 `game_launch` 且 body
  含配置中的 memory 等；二次调用不重复；failure → popup 调用。

### 6.2 手动验证（启动器运行流程，已于 2026-09-26 全部执行通过）

1. `--launch <不存在版本>`：日志出现 `LAUNCH_TARGET_NOT_FOUND` 解析失败
   警告，前端弹窗呈现，启动器正常运行。✅
2. `--launch 26.3-pre-2`（版本名，真实实例）：日志「正在通过命令行启动实例」
   →「命令行实例启动流程完成」，Minecraft 进程真实创建，随后终止进程。✅
3. `--launch <实例目录路径>`（`versions/<名>\` 形态，含尾分隔符）：
   正确推导游戏根与版本名并派发启动。✅
4. `--launch 26.3-pre-2 --server 127.0.0.1:25565`：quickPlayMultiplayer
   参数生成无报错，游戏进程创建后终止。首次尝试曾因 Microsoft 令牌刷新
   瞬时失败被拒（弹窗路径正确呈现，属既有登录链路的环境波动），重试成功。✅
5. 双开：实例 A 运行中再启动 B（含 `--launch`）：B 打印「启动器已在运行，
   已请求激活已运行的窗口」并立即退出（退出码 0），A 保持运行并收到
   置前请求。✅
6. `ECL_CONFIG_LAUNCHER_SINGLE_INSTANCE=false`：两个启动器进程共存，
   不产生发现文件。✅
7. `--world NoSuchWorld`（版本支持 quickPlay 但世界不存在）：
   启动流程在快捷目标校验处被拒（`RESOURCE_NOT_FOUND`），弹窗呈现，
   不创建游戏进程。✅

## 7. 风险与注意事项

- `memory/lock_memory/process_priority` 由调度方显式写入 body：与前端
  「读设置后传参」行为一致；若未来 `game_launch` 改为自行兜底配置，
  此处随之简化，文档需同步。
- 双开竞态窗口（亚秒级同时启动）理论上可产生两个主实例，v1 接受该概率；
  接管逻辑保证崩溃后不留死锁。
- 单实例语义按「同一数据目录」生效：不同 `--data-dir` 的沙箱互不干扰，
  属预期行为。
- `--world` 依赖版本清单声明 quickPlay 能力，报错走弹窗，不阻断启动器。
- 第三期预留：single_instance 协议 `action` 字段扩展为 `launch` 转发、
  `ecl://` 深链接、整合包文件关联。

## 8. 前端影响

无（形态 A 全部在后端完成；`launcher:focus_request` 事件仅由适配器
消费，不进入前端事件转发表）。
