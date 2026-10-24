# WebView2 原生调试端口改为代码设标志实施方案

状态：已按方案 1 实施，等待冻结产物 CI 验证。

日期：2026-10-24。

## 1. 目标与验收结果

1. `Build Backend (windows-latest)` 的 `Smoke test packaged plugin directories` 步骤恢复通过。
2. 原生页面调试通道不再依赖 `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` 环境变量，改由启动器在窗口配置里以 `additionalBrowserArgs` 显式设置。
3. 不带新参数启动时，窗口配置与当前完全一致，发布产物行为不变。
4. 该步骤第一条 pytest 失败后不再连带吞掉后续 13 个 Windows x64 用例的执行。

## 2. 已核实的失败原因

| 证据 | 内容 |
| --- | --- |
| CI 日志（run #289，作业 `Build Backend (windows-latest)`） | `Failed: 等待原生页面调试通道超时：端口发现文件存在：False；页面来源：[]`，6 个 `msedgewebview2.exe` 子进程均无任何 `--remote-debugging-*` 参数。 |
| 同一次运行的应用日志 | `后端初始化完成` → `前端界面初始化完成` → `前端窗口已就绪` → `前端加载完成`，应用与 WebView2 本身正常。 |
| `tests/test_packaged_plugin_directories.py:195` | 用例 `skipif` 仅跑 Windows x64；ARM 作业该用例为 `1 skipped`，因此 ARM 通过不代表覆盖。 |
| `docs/plans/select-placement-theme-2026-10-24.md` 验收记录 | 本机同一流程 9.26 秒通过，并确认本机 WebView2 支持 `EBWebView/DevToolsActivePort`。 |
| Microsoft Edge 文档（WebView2 浏览器标志） | 提升权限的宿主应用忽略通过本地环境变量设置的浏览器标志，只认可代码中 `AdditionalBrowserArguments` 设置的标志。GitHub 托管 Windows 运行器以 `runneradmin` 身份运行，属提升权限环境。 |

结论：用例逻辑、端口竞争与目录复用都不是根因。根因是**浏览器标志的注入方式**——环境变量在提升权限宿主下被 WebView2 丢弃，`--remote-debugging-port=0` 从未到达浏览器进程，端口发现文件自然不生成。时间线上该用例自 run #279（`27c1c9b`）引入后从未在 CI 通过，run #278（`7e96e9f`）是最后一次成功。

## 3. 方案与职责

新增一个仅调试用、仅会话内生效的命令行参数，由启动器把它折算为主窗口的 `additionalBrowserArgs`。代码设的标志在提升权限宿主下依然生效，是唯一能同时兼容本机与 CI 的注入方式。

职责划分：

- `ECL/cli.py` 负责解析与校验该参数，不负责理解 WebView2。
- `ECL/host/tauri.py` 负责把端口折算为窗口配置字段，是唯一知道 `additionalBrowserArgs` 的位置。
- 测试只负责传入端口并读取浏览器自写的 `EBWebView/DevToolsActivePort`，不再触碰浏览器标志。

已确认的能力边界：

- `additionalBrowserArgs` 是 Tauri 窗口配置字段，打包扩展 `pytauri_wheel/ext_mod` 中存在该配置键，可直接经现有 `context_factory` 的配置字典生效。
- `dataDirectory` 只存在于 pytauri 的运行时构建器接口，**不是**配置字段（打包扩展中无该键），因此本方案不改浏览器数据目录，测试继续用 `WEBVIEW2_USER_DATA_FOLDER` 指定每个阶段的独立目录。该变量是目录设置而非浏览器标志，不受提升权限忽略规则影响。

## 4. 具体改动

### 4.1 `ECL/cli.py`

- `LaunchOptions` 增加字段 `webview_debug_port: int | None = None`，注释说明仅 Windows、仅会话内生效、不写入 `setting.json`。
- `build_arg_parser()` 增加 `--webview-debug-port`，`type=int`，`metavar="端口"`，帮助文本说明 0 表示由浏览器分配，且需与 `--dev-channel` 同时使用。
- 新增私有校验函数 `_validate_webview_debug_port(parser, args)`，在 `parse_launch_options` 中调用：
  - 取值不是 0 且不在 1024—65535 之间时报用法错误；
  - 未同时给出 `--dev-channel` 时报用法错误；
  - `sys.platform != "win32"` 时明确报错，不做静默忽略。
- `parse_launch_options` 把取值写入 `LaunchOptions`。
- `apply_launch_overrides` 不处理该字段：它不属于配置项，避免被写成存盘配置。
- 无头模式无需改动：`_validate_run_mode` 的允许集合不包含该参数，`--headless` 下会被拒绝。

### 4.2 `ECL/host/tauri.py`

- `Adapter` 增加类级默认 `webview_debug_port: int | None = None`，`__init__` 中从 `context.state.launch_options` 读取，未提供时为 `None`。类级默认保证既有以 `object.__new__` 构造的测试不受影响。
- `_build_config()` 构造完窗口字典后，若端口不为 `None`：
  - 写入 `window["additionalBrowserArgs"] = f"--remote-debugging-port={port}"`；
  - 以 `warning` 级别记录一次“已开启 WebView2 远程调试”及端口，便于从 CI 日志判断参数是否生效。
- 不修改 `decorations`、`transparent`、`shadow` 等既有窗口字段，避免调试运行与发布运行外观不一致。

需要说明的取舍：设置 `additionalBrowserArgs` 会替换 wry 的默认浏览器参数（`--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection`）。该参数仅在调试运行时存在，不影响发布产物；若审阅认为需要保持运行期行为完全一致，可把同一串默认参数一并拼入，代价是把 wry 内部默认值复制进本项目代码。默认按“只加调试参数”实施。

### 4.3 `ECL/host/single_instance.py`（实施时确认无需改动）

原计划把该参数并入「强制新进程」条件。实施时确认：`_dispatch` 已把 `enable_dev_channel` 列为强制新进程的条件，而本参数被校验要求必须与 `--dev-channel` 同时使用，因此该条件必然已命中，再追加一项属于不会被触发的死代码，故不改动。

### 4.4 `tests/test_packaged_plugin_directories.py`

- 启动环境只保留 `WEBVIEW2_USER_DATA_FOLDER`，删除 `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`。
- 启动参数追加 `--webview-debug-port`、`"0"`。
- `_native_channel` 的入参、端口文件路径、超时诊断与 90 秒截止时间保持不变。
- 保留现有诊断输出（端口发现文件状态、页面来源、浏览器调试参数、本地监听端口），失败时仍能直接定位。

### 4.5 `.github/workflows/build.yml`

- 修正第 366 行多余缩进（bash 下无害，但易误读为独立代码块）。
- 把 13 个插件 Worker/网络用例拆为独立 `run:` 步骤，与 `test_packaged_plugin_directories.py` 分开。当前第一条 pytest 失败即被 `set -e` 中断，x64 侧这 13 个用例在 CI 中从未执行；拆分后两者互不遮蔽。

### 4.6 文档

- 在 `docs/plans/select-placement-theme-2026-10-24.md` 的验收记录追加一条：根因、本次改动与 CI 最终结果。

## 5. 安全边界

- 参数只经命令行传入，不新增配置键，不写入 `setting.json`，不随会话持久化。
- 必须同时开启开发者通道；开发者通道本身已在数据目录写入带令牌的 `dev_channel.json`，两者同属开发期能力，不引入新的暴露面。
- 非 Windows 直接报错，不用空实现或默认值掩盖。
- 不改变默认浏览器数据目录，不触碰用户既有 WebView2 配置；每个阶段的独立目录仍由测试通过 `WEBVIEW2_USER_DATA_FOLDER` 指定。
- 远程调试端口只监听本机回环，由 WebView2 自身决定绑定地址。

## 6. 测试与验收

1. `tests/test_cli_launch_options.py` 增加用例：端口越界拒绝、缺少 `--dev-channel` 拒绝、非 Windows 拒绝（`monkeypatch` 替换 `sys.platform`）、无头模式拒绝。
2. `tests/test_frontend_api_config.py` 增加用例：带端口时窗口字典出现 `additionalBrowserArgs` 且值为 `--remote-debugging-port=<端口>`；不带端口时窗口字典不含该键。
3. 本地源码模式实跑：`ECL_PLUGIN_SMOKE_SOURCE=1` 下执行 `pytest tests/test_packaged_plugin_directories.py -q`，确认安装与重启两阶段均通过。
4. 冻结产物：本地 Nuitka 构建后以 `ECL_PLUGIN_SMOKE_EXECUTABLE=build-nuitka/main.exe` 复跑同一用例。
5. CI：推送后确认 `Build Backend (windows-latest)` 的该步骤通过，并确认新增的 13 个用例步骤真实执行（不再是 0 覆盖）。
6. 提交前 `ruff check` 与 `ruff format --check` 通过。

## 7. 风险与回退

| 风险 | 判断依据 | 处置 |
| --- | --- | --- |
| `additionalBrowserArgs` 被 Tauri 忽略或未透传到 wry | 用例仍超时，诊断仍显示“端口发现文件存在：False” | 改为固定端口 + 直连 `http://127.0.0.1:<端口>/json/list`，完全绕开端口发现文件；或改由 pytauri `WebviewWindow` 构建器显式创建窗口并设置 `additional_browser_args` 与 `data_directory`。 |
| `WEBVIEW2_USER_DATA_FOLDER` 在 CI 同样被忽略 | 端口文件出现在其它目录，用例读不到 | 与上一行合并处理：放弃端口发现文件，改用固定端口直连。 |
| 调试运行丢失 wry 默认浏览器参数 | 仅影响调试运行的外观相关特性 | 需要时把默认串一并拼入，或维持现状（调试期可接受）。 |

回退方式：删除 `--webview-debug-port` 参数与窗口字段、恢复测试的环境变量注入、撤回工作流拆分，即可回到当前状态；改动不涉及持久化数据与发布产物结构。

## 8. 本次不做

- 不把调试端口做成配置项或界面开关。
- 不为 macOS/Linux 增加等价的原生页面调试能力。
- 不重构窗口创建流程（仍由配置字典驱动）。
- 不调整该用例的 90 秒截止时间、页面来源筛选与插件断言范围。

## 9. 实施结果

已按第 4 节完成改动，唯一偏离是 4.3 的单实例判断（理由见该节）。

| 文件 | 实际改动 |
| --- | --- |
| `ECL/cli.py` | 新增 `LaunchOptions.webview_debug_port`、`--webview-debug-port` 与 `_validate_webview_debug_port`；`apply_launch_overrides` 不处理该字段。 |
| `ECL/host/tauri.py` | `Adapter` 增加类级默认 `webview_debug_port`，`__init__` 从 `context.state.launch_options` 读取；`_build_config` 改为先构造窗口字典再调用新增的 `_apply_webview_debug_options` 注入 `additionalBrowserArgs` 并记录警告。 |
| `tests/test_packaged_plugin_directories.py` | 删除 `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`，启动参数追加 `--webview-debug-port 0`；保留 `WEBVIEW2_USER_DATA_FOLDER` 与全部诊断输出。 |
| `.github/workflows/build.yml` | 新增 `Prepare offline plugin wheels` 步骤经 `$GITHUB_ENV` 共享轮子路径；原生安装验收与 13 个 Worker/网络用例拆成两个步骤，后者按 `runner.os == 'Windows' && matrix.os != 'windows-11-arm'` 执行；修正原多余缩进。 |
| 测试 | `tests/test_cli_launch_options.py` 增加端口取值、缺 `--dev-channel`、非 Windows 拒绝与无头模式拒绝；`tests/test_frontend_api_config.py` 增加注入与不注入两个窗口配置用例。 |

验证：

- `pytest tests/test_cli_launch_options.py tests/test_frontend_api_config.py`：216 通过、5 跳过。
- 后端全量 `pytest tests`（排除需冻结产物的用例）：2188 通过、21 跳过、6 失败。6 个失败均为符号链接相关用例（`test_java_manager`、`test_plugin_environment_pool`、`test_plugin_grant_store`、`test_plugin_instance_queries`、`test_plugin_package_preparation`），这些文件不引用本次改动的模块，属本机既有失败，与本变更无关。
- 本机源码模式实跑 `tests/test_packaged_plugin_directories.py`：日志出现「已开启 WebView2 远程调试：端口：0」，诊断显示「端口发现文件存在：True」，`/json/list` 返回页面列表，确认代码下发的标志已生效。用例本身仍因源码模式页面来源是 Vite 开发服务器（`localhost:5173`）而非 `tauri.localhost` 而未通过，该断言只对冻结产物成立。
- `ruff check` 与 `ruff format --check` 通过；`build.yml` 经 YAML 解析确认三个步骤归属 `build-backend` 作业。
- 冻结产物与 CI 结论待推送后确认。
