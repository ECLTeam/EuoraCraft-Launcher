# 启动器命令行启动参数（第一期）实施文档

> 状态：待用户审阅（工作流第 5 步）。审阅通过后按第 5 节步骤实施。

## 1. 背景与目标

启动器当前没有任何命令行参数处理：`main.py:17` 直接构造 `EuoraCraftLauncher`，
`--help`/`--version` 不存在，打印版本号也会走完整启动流程并写入日志文件。

本期目标：引入轻量的 CLI 参数解析层，覆盖排障、多沙箱与开发场景共 8 个参数；
纯后端改动，不涉及前端子模块。第二期的 `--launch`（实例快捷方式）与第三期的
单实例锁、`ecl://` 深链接不在本期范围内。

## 2. 参数定义

| 参数 | 说明 | 对应现状机制 |
|:---|:---|:---|
| `--help` / `-h` | 中文帮助文本，立即退出（退出码 0） | 新增 |
| `--version` / `-v` | 输出 `EuoraCraft Launcher <版本> (<版本类型>)`，立即退出（退出码 0） | 新增 |
| `--data-dir <路径>` | 指定本次运行的数据目录（多沙箱并存）；目录不存在时自动创建 | 等价于 `ECL_DATA_PATH`（runtime.py:72） |
| `--debug` | 本次会话开启调试模式（控制台 DEBUG、界面调试标签、解锁 debug 类 IPC） | 会话级覆盖 `launcher.debug` 配置 |
| `--log-level <debug\|info\|warning\|error>` | 本次会话的控制台日志级别 | 会话级覆盖 `launcher.debug_log_level` 配置 |
| `--disable-plugins` | 本次会话不加载、不启用任何插件（含系统插件 qomicex_compat）；插件页仍可看到已安装列表并手动启用单个插件 | 无等价物（排障刚需） |
| `--frontend-dist <url\|路径>` | 覆盖前端资源来源，支持 `http(s)://` URL 或本地目录路径；开发联调请使用 URL 形态（与 `ECL_CONFIG_TAURI_FRONTENDDIST` 语义一致，见第 7 节） | 等价于 `ECL_CONFIG_TAURI_FRONTENDDIST`（tauri.py:120） |
| `--dev-channel` | 本次会话开启开发者通道（插件开发工具箱 WebSocket） | 会话级覆盖 `launcher.dev_channel` 配置 |

说明：

- `--debug` 与 `--log-level` 同时给出时，`--debug` 优先（与现有
  `launcher.py:157-159`「debug 强制最高诊断级别」的逻辑一致）。
- `--data-dir` 指向已存在但不是目录的路径时报用法错误（退出码 2）；
  `--frontend-dist` 的路径形态必须是已存在的目录，否则同样报用法错误。
- 账户数据固定在 `~/.ECL/accounts`（application.py:378），`--data-dir`
  不迁移账户，与 `ECL_DATA_PATH` 现状一致，属已知边界。

## 3. 关键语义决策

1. **解析时机与轻量退出**：解析发生在 `main.py` 构造 `EuoraCraftLauncher`
   之前；`ECL/__init__.py` 仅引入版本常量，因此 `--help`/`--version`
   不导入 pytauri、不初始化日志、不创建任何文件。
2. **优先级链**：`CLI 参数 > ECL_CONFIG_* 环境变量 > setting.json > 默认值`。
   CLI 覆盖在 `apply_to_config`（application.py:326）之后叠加。
3. **会话内粘滞、不落盘**：CLI 覆盖只改内存中的 `state.config`，
   永不写回 `setting.json`；且在每次 `config:updated` 重算配置时重新叠加
   （application.py:515），避免用户改动任意设置项（如主题）后
   会话覆盖被静默清除。副作用是设置界面中的同名开关（debug、日志级别、
   开发者通道）当次会话不生效；为保持一致，`_get_effective_config`
   （bridge.py:559-568）叠加同一覆盖，使界面显示的即当前生效值。
   重新打开启动器（不带参数）即恢复存盘配置。
4. **用法错误退出码**：argparse 用法错误退出码为 2，与
   `LauncherExitCode.STARTUP_FAILED = 2` 数值相同但发生时机不同
   （前者在初始化开始前），不引入新的退出码。

## 4. 改动点明细

### 4.1 新增 `ECL/cli.py`

统一文件头（规范 3.2），内容：

- `@dataclass(frozen=True, slots=True) class LaunchOptions`：
  - `data_dir: Path | None = None`
  - `debug: bool = False`
  - `log_level: str | None = None`
  - `disable_plugins: bool = False`
  - `frontend_dist: str | None = None`
  - `enable_dev_channel: bool = False`
- `build_arg_parser() -> argparse.ArgumentParser`：标准库 argparse，
  帮助文本中文；`--version` 用 `action="version"`，`--log-level`
  用 `choices=("debug", "info", "warning", "error")`（与
  `resolve_log_level` 支持的白名单一致）。
- `parse_launch_options(argv: Sequence[str]) -> LaunchOptions`：
  `--help`/`--version` 由 argparse 打印并抛 `SystemExit(0)`；
  `--data-dir` 做 `expanduser().resolve()` 并校验「存在则必须为目录」；
  `--frontend-dist` 校验 URL 前缀或本地目录存在性（路径形态解析为绝对路径）。
- `apply_launch_overrides(config: dict[str, Any], options: LaunchOptions | None) -> dict[str, Any]`：
  把 `LaunchOptions` 折算成配置覆盖并叠加（`launcher.debug` /
  `launcher.debug_log_level` / `launcher.dev_channel` / `tauri.frontenddist`，
  `tauri` 分区不存在时创建）；`options` 为 None 时原样返回深拷贝。

### 4.2 `main.py`

- `run_launcher(argv: list[str] | None = None) -> int`：先
  `parse_launch_options`，再**在函数体内延迟导入** `ECL.launcher`，
  保证 `--help`/`--version` 快速退出；`EuoraCraftLauncher(options).run()`。
- 入口改为 `sys.exit(run_launcher())`（argv 缺省读 `sys.argv[1:]`）。

### 4.3 `ECL/common/runtime.py`

- `get_runtime_info(data_path_override: Path | None = None) -> RuntimeInfo`：
  数据目录优先级变为 `data_path_override > ECL_DATA_PATH > app_path / "ECL_data"`；
  覆盖值同样做 `expanduser().resolve()`。更新文件头公开接口清单与 docstring。

### 4.4 `ECL/launcher.py`

- `__init__(self, options: LaunchOptions | None = None)`：
  保存 `self.options = options or LaunchOptions()`；
  `get_runtime_info(data_path_override=self.options.data_dir)`；
  其余初始化顺序不变（`configure_logging` 仍使用已重定向的 `data_path`，
  目录由日志运行时 `mkdir(parents=True)` 自动创建，logging.py:161）。
- `run()`：`create_application(..., launch_options=self.options, ...)`。

### 4.5 `ECL/application.py`

- `create_application(runtime_info, *, launch_options=None, on_state_ready=None)`：
  - `state.launch_options = launch_options`（`ApplicationState` 新增字段）。
  - application.py:326 改为
    `state.config = apply_launch_overrides(environment.apply_to_config(config.get_config()), launch_options)`
    （在 `state.debug` 派生与 `on_state_ready` 之前，使 `--log-level`
    从启动最早阶段生效）。
  - application.py:452 改为
    `plugins.initialize(state.data_path, state.resource_path, auto_enable=not (launch_options and launch_options.disable_plugins))`。
  - `update_runtime_config`（application.py:515）重算配置时同样叠加
    `apply_launch_overrides(..., state.launch_options)`，实现会话粘滞。

### 4.6 `ECL/plugins/manager/base.py`（含 registry / lifecycle / packages 配套）

- `initialize(data_path, resource_path=None, *, auto_enable: bool = True)`：
  关键字参数带默认值，现有约 40 处测试调用不受影响。
  `auto_enable=False` 时：候选发现、安装事务恢复、禁用状态修剪、
  依赖解析照常执行（这些只读 `plugin.json`，不导入插件代码），
  填充 `_candidate_map`，但跳过 `_load_plugins_in_order` 与 `_enable_all`，
  并输出日志「已按启动参数跳过插件的加载与启用」。
- 实施中确认的两个配套缺口（保证「插件页可见且可手动启用」的承诺成立）：
  - `registry.list_plugins` 新增分支：安全模式下把「已发现、未加载、未禁用、
    无错误」的候选以 `unloaded` 状态补充进列表（i18n 已有全部语言的
    `plugins.unloaded` 标签；`Plugins.vue` 对非 `enabled` 状态本就渲染启用按钮，
    前端零改动）。
  - `lifecycle._enable` 的按需加载条件从「仅在禁用列表中」扩展为
    「禁用列表中或处于安全模式」，使插件页的启用按钮可触发按需加载。
- `packages._restore_package_plugins` 新增 `restore_workers: bool = True`：
  安全模式下仅完成归档插件发现与同名冲突登记，不启动任何归档 Worker 进程。

### 4.7 `ECL/api/bridge.py`

- `_get_effective_config`（bridge.py:559-568）在返回前叠加
  `apply_launch_overrides(config, self.launcher.launch_options)`，
  使前端设置页展示的与会话实际生效值一致（`launcher` 分区本就来自
  `state.config`，此改动兜底其余分区并保持单一覆盖入口）。

## 5. 实施步骤

1. 新增 `ECL/cli.py`（解析、校验、覆盖叠加）。
2. 修改 `runtime.py` → `application.py` → `launcher.py` → `main.py`（自底向上接通数据流）。
3. 修改 `plugins/manager/base.py` 的 `initialize`。
4. 修改 `bridge.py` 的 `_get_effective_config`。
5. 新增/更新测试（见第 6 节）。
6. 执行门禁：`ruff check ECL tests`、`ruff format --check ECL tests`、`pytest`；
   因涉及启动器运行流程，另执行 `cd frontend && pnpm check && pnpm build`。
7. 实际启动验证（见 6.3）后按规范提交一次 commit：
   `feat: 新增启动器命令行启动参数`（含文档、代码与测试）。

## 6. 测试计划

### 6.1 新增 `tests/test_cli_launch_options.py`

- 默认解析：无参数 → 全部字段为 None/False。
- 各参数长形态解析与取值；`--log-level` 非法值、`--data-dir` 指向文件、
  `--frontend-dist` 指向不存在路径 → `SystemExit(2)`。
- `--data-dir` 相对路径与 `~` 展开；`--frontend-dist` URL 原样保留、
  相对路径解析为绝对路径。
- `--help` / `--version` 打印内容并 `SystemExit(0)`（capsys 校验版本号文本）。
- `apply_launch_overrides`：各覆盖生效、`tauri` 分区自动创建、
  `options=None` 时配置不变、不修改传入的 config 对象。

### 6.2 更新既有测试文件

- `tests/test_runtime_paths.py`：新增
  `get_runtime_info(data_path_override=...)` 优先于 `ECL_DATA_PATH` 的用例。
- `tests/test_application_context.py`：`create_application` 传入
  `launch_options` 后 `state.config` 含覆盖、`state.launch_options` 已保存；
  `disable_plugins` 时（以 monkeypatch 侦测）`plugins.initialize` 收到
  `auto_enable=False`。
- `tests/test_plugin_disabled_state.py`：新增 `auto_enable=False` 用例：
  发现阶段完成（候选存在）但无任何插件被加载/启用。

### 6.3 手动验证（启动器运行流程，已于 2026-09-26 全部执行通过）

1. `python main.py --help`、`python main.py --version`：立即输出、退出码 0、
   指向全新 `ECL_DATA_PATH` 的目录未被创建（零副作用）。✅
2. `python main.py --debug`：控制台自引导阶段即输出 DEBUG，
   日志确认 `debug=True`。✅
3. `python main.py --data-dir <临时目录>`：全新目录自动创建，
   logs/setting.json/plugins/plugin_config/wardrobe 等完整布局生成。✅
4. `python main.py --disable-plugins`：日志出现「已按启动参数跳过插件的加载与启用」，
   对照运行（未带参数）正常加载 qomicex-compat，差异来自参数本身；
   未启用插件在插件页以 `unloaded` 状态可见并可手动启用（单元测试覆盖）。✅
5. `python main.py --dev-channel`：`ECL_data/dev_channel.json` 生成（含端口与令牌）。✅
6. `--frontend-dist`：环境变量指向死端口、CLI 指向运行中的 vite（5173），
   窗口成功加载真实前端并经 IPC 上报日志——同时证明该值到达 Tauri 且
   CLI 优先于 `ECL_CONFIG_*`。✅
7. 带未知参数运行：中文用法错误提示，退出码 2；`--data-dir` 指向文件同样退出码 2。✅

## 7. 风险与注意事项

- **粘滞语义**：带 `--debug` 启动后，设置页关闭调试当次会话不生效
  （界面显示值即为生效值，行为可预期）；若你希望改成「仅启动时生效、
  之后允许界面接管」，只需去掉 `update_runtime_config` 中的重新叠加，
  实施前请确认。
- `--frontend-dist` 本质是把一个 URL/路径注入 Tauri 配置，与现有
  `ECL_CONFIG_TAURI_FRONTENDDIST` 能力相当，不新增攻击面；
  CLI 输入按不可信处理（路径边界校验）。
- `--disable-plugins` 也会停用系统插件 qomicex_compat（合作项目兼容层），
  这是「安全模式」的预期行为；如需保留系统插件，实施时可改为仅跳过用户插件。
  归档插件 Worker 同样不启动（仅保留同名冲突登记），重新启用需在正常模式下操作。
- `--frontend-dist` 的路径形态语义与既有 `ECL_CONFIG_TAURI_FRONTENDDIST` 完全一致；
  实测在源码运行（pytauri-wheel）下路径形态不会从磁盘服务前端内容，
  仅 URL 形态可正常联调，开发时请使用 `--frontend-dist http://localhost:5173`。
  该行为属 pytauri-wheel 既有限制，不随本功能引入。
- Nuitka/PyInstaller 打包入口均为 `main.py`，参数透传无需改动打包配置。

## 8. 后续规划衔接

- 第二期：`--launch <实例>`（需与前端定义启动提示事件契约）及其全自动形态、`--server <地址>`。
- 第三期：单实例锁与参数转发、`ecl://` 深链接、整合包文件关联（独立规划文档）。
