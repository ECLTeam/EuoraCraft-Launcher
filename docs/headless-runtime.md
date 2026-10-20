# 有头与无头运行

同一份启动器支持桌面启动、有限无 UI 任务和受宿主管理的无头插件子进程。默认进入桌面模式，显式使用 `--gui` 的效果相同。统一轻量入口按角色导入执行器，不预加载全部后端服务。

## 命令

```powershell
# 正常启动
python main.py
python main.py --gui

# 在独立进程中检查运行环境，stdout 输出 JSON
python main.py --headless --task runtime-check

# 隐藏控制台的 Windows 产物使用结果文件
.\ECL.exe --headless --task runtime-check --result-file .\runtime-result.json
```

`ECL.exe` 是示例名称，实际运行时使用下载或构建产物的文件名。macOS 自动化直接执行 `.app/Contents/MacOS/` 中的主可执行文件。

`--gui` 与 `--headless` 互斥。普通无头任务必须指定 `--task`，目前支持 `runtime-check`；任务完成后进程退出。`--task`、`--result-file` 只能用于普通无头任务。插件子进程使用下文的独立角色，不输出一次性探测结果。

普通无头任务可使用 `--data-dir`、`--debug`、`--log-level`。探测的数据目录只解析不创建，不读取用户配置、账户或插件。模式不保存到设置。

`--launch`、游戏启动选项、`--open-page`、`--frontend-dist`、`--dev-channel`、`--disable-plugins` 在普通无头任务下会报参数错误。当前没有无 UI 游戏启动或完整后端常驻功能。

## 结果与退出

`runtime-check` 实际导入并检查 `httpx`、`pydantic`、`psutil`，验证当前发行物的代表性依赖与运行环境，不发起网络请求。

结果协议版本为 1，JSON 字段如下：

| 字段 | 含义 |
| --- | --- |
| `protocol_version` | 结果协议版本 |
| `task` | 执行的内置任务名称 |
| `success` | 任务是否成功 |
| `error_code` | 失败的稳定错误码；成功时为 null |
| `message` | 可公开的失败说明；成功时为 null |
| `probe` | 成功时的运行摘要；失败时为 null |

`probe` 包括 `launcher_version`、`python_implementation`、`python_version`、`platform`、`architecture`、`abi`、`is_frozen`、`has_ui_modules` 和 `dependencies`。每项依赖含 `name` 与 `version`。`abi` 是 Python 实现的缓存标签，属于探测信息，不代替 wheel / 原生扩展兼容性验证。

结果不输出账户、配置、环境变量或完整路径。无结果文件时 stdout 只输出一份 JSON，人类日志单独输出到可用的 stderr。

指定 `--result-file` 后，将 JSON 原子写入尚未存在的文件，必要时创建父目录；stdout 保持安静。已有目标文件、目录或符号链接会被拒绝。调用者应为每个并发任务指定独立的新文件名，这个接口不提供同文件多进程结果合并。

没有可用 stdout 的发行物必须指定结果文件，否则返回失败而不执行任务。结果文件输出失败也返回失败，不能把任务执行成功当作结果已交付。标准错误流失效不会把日志混入结果输出。

| 退出码 | 含义 |
| --- | --- |
| 0 | 任务与结果输出均成功 |
| 1 | 初始化、任务或结果输出失败 |
| 2 | 参数错误 |
| 130 | Ctrl+C 后任务已停止 |

稳定错误码包括 `HEADLESS_INITIALIZATION_FAILED`、`HEADLESS_DEPENDENCY_UNAVAILABLE`、`HEADLESS_IMPORT_BOUNDARY`、`HEADLESS_TASK_FAILED`、`HEADLESS_TASK_UNSUPPORTED` 和 `HEADLESS_CANCELLED`。参数错误不生成任务结果文件；没有输出通道或输出失败时，调用者根据非零退出码判断失败。

普通无头任务不创建窗口、托盘、WebView，不依赖前端资源或前端就绪事件，不通知已有桌面实例，也不操作桌面单实例发现文件或共享日志轮转文件。同一数据目录下可以并行执行独立探测任务。

## 无头插件子进程

宿主复用当前 ECL 可执行文件创建插件子进程，命令形式如下，参数值由宿主生成：

```text
ECL.exe --headless --role plugin --pipe-address <宿主端点> --host-pid <PID> --run-id <运行标识>
```

源码模式复用当前 base CPython，以 `-S main.py` 进入相同分流。无需构建额外 Worker.exe、下载解释器或为插件创建完整桌面宿主。

插件角色仅支持 Windows x64，必须包含完整的连接参数；不能与 `--task`、`--result-file`、`--data-dir` 或桌面选项混用。模式与角色控制参数使用完整名称，插件角色的参数不得重复。旧 `--internal-plugin-worker` 入口已移除并明确拒绝。

命令行只定位连接，不表示授权，也不接受任意脚本入口。宿主先挂起创建并将进程加入 Job，再恢复运行；实际执行插件的连接进程必须通过内核 PID、映像、进程句柄和 Job 归属核验。插件执行器完成认证后仍等待宿主的用户审阅和内容加载许可，不通过初始化完整 ApplicationContext 获得服务。

插件生命周期与双向调用通过 IPC 交付，不使用 stdout 的探测结果协议。参数、连接或授权失败不会回退桌面。断连及关闭由宿主监管和会话资源清理处理；已经提交到宿主的业务任务须另按任务所有权和取消规则管理。

该角色已提供插件进程运行基础；正式 PluginManager 的第三方发现链路迁移仍未完成，当前安装的第三方插件不能据此宣称已全部隔离。用户授权管理界面及完整 Host API 也不由本次入口整合自动获得。自定义前端原生隔离保持暂缓。

## 扩展边界

`main.py` 通过 `ECL.bootstrap` 在宿主业务模块导入前确定角色。`ECL` 根包仅作包标记，不聚合导出版本或服务。插件角色按需加载 `ECL.plugin_api._process`；桌面和普通任务由 `ECL.cli` 校验业务参数，随后按需导入桌面入口或 `ECL.runtime.headless.run_headless`。普通无头上下文只拥有本次执行的路径与日志资源；新增任务须有明确的输入、结果与资源生命周期契约，不预加载无关服务。

复用的是发行物携带的解释器版本、代码、依赖和资源。不同进程的 Python 对象、模块缓存、线程和锁各自独立。启动器可执行文件也不是通用 `python.exe`，没有任意 `-m`、`-c` 或用户脚本入口。

无头插件角色沿用独立 SDK、受限 IPC 和内容绑定的依赖目录。普通探测任务不自动挂载插件依赖。不同进程仍各自占用解释器内存，onefile 引导与解包行为取决于实际构建器；统一入口不等于共享主进程对象或消除这些成本。

`--headless` 控制启动器 UI，不改变打包时的控制台策略，也不绕过 onefile 的打包引导与解压。开发产物仍可能显示终端窗口。

## Python 代码归属

根目录仅保留 `main.py` 作为统一进程入口。启动引导位于 `ECL/bootstrap.py`，轻量插件 API 位于 `ECL/plugin_api/`，插件导入使用 `from ECL.plugin_api import Plugin`。旧 `ecl_bootstrap` 和 `ecl_plugin_api` 导入路径已移除，插件源码需要同步调整；启动器不会自动改写已安装插件或用户数据。

直接导入 `ECL.plugin_api` 不初始化宿主插件管理器、桌面或业务服务。宿主内既有的 `ECL.plugins.Plugin` 接口沿用其自身契约，不能据此次目录迁移宣称第三方插件发现链路已经完成进程隔离。版本信息由 `ECL.foundation.version` 提供。
