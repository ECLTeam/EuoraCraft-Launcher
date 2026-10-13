# 有头与无头运行

同一份启动器支持桌面启动和有限的无 UI 任务。默认进入桌面模式，显式使用 `--gui` 的效果相同。

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

`--gui` 与 `--headless` 互斥。无头模式必须指定任务，目前仅支持 `runtime-check`；任务完成后进程退出。`--task`、`--result-file` 只能用于无头模式。

无头模式可使用 `--data-dir`、`--debug`、`--log-level`。数据目录只解析不创建，不读取用户配置、账户或插件。模式不保存到设置。

`--launch`、游戏启动选项、`--open-page`、`--frontend-dist`、`--dev-channel`、`--disable-plugins` 在无头模式下会报参数错误。本期没有无 UI 游戏启动或完整后端常驻功能。

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

无头模式不创建窗口、托盘、WebView，不依赖前端资源或前端就绪事件，不通知已有桌面实例，也不操作桌面单实例发现文件或共享日志轮转文件。同一数据目录下可以并行执行独立任务。

## 扩展边界

`ECL.cli` 在参数校验后选择桌面入口或 `ECL.runtime.headless.run_headless`。无头上下文只拥有本次执行的路径与日志资源；新任务通过明确的任务契约接入，不用可选字段拼成完整桌面上下文。

复用的是发行物携带的解释器版本、代码、依赖和资源。不同进程的 Python 对象、模块缓存、线程和锁各自独立。启动器可执行文件也不是通用 `python.exe`，没有任意 `-m`、`-c` 或用户脚本入口。

现有插件继续在主进程运行。本期不开放 `--role plugin-worker`，不自动挂载插件依赖目录。以后接入插件子进程时，需要另行定义专用 IPC、权限代理、任务所有者、宿主断开处理及进程回收，并在真实打包产物中验证 wheel 与原生扩展加载。

`--headless` 控制启动器 UI，不改变打包时的控制台策略，也不绕过 onefile 的打包引导与解压。开发产物仍可能显示终端窗口。
