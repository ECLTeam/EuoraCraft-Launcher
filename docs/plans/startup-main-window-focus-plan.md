# 主窗口启动即获取焦点实施方案（已实施）

## 目标与验收

- 启动器主窗口首次出现时主动请求成为前台窗口，并可立即接收键盘输入。
- 只在本次启动的首次窗口创建阶段请求一次焦点；切换页面、前端重新加载、插件窗口创建及后续后台事件不重复抢焦点。
- 纯自绘、自绘标题栏加系统边缘、系统原生标题栏三种主窗口模式行为一致。

## 当前项目情况

- `ECL/adapters/tauri.py` 创建主窗口时使用 `visible: True`，但没有显式调用 `set_focus()`；`app.run_return()` 当前没有事件回调。
- Tauri 窗口配置的 `focus` 默认是 `true`，仅再写一遍该配置无法解决启动后焦点被其他窗口占用的问题。项目已有可用的 `WebviewWindow.set_focus()` 调用，登录回调与子窗口显示时使用该方法。
- `frontend_ready` 要等待路由、配置、运行信息和字体加载后才发送；若把启动聚焦放在这里，主窗口已经显示一段时间，不符合“立刻”的要求。
- 当前安装的 PyTauri 提供 `App.run_return(callback)`、`RunEvent.Ready`、`Manager.get_webview_window()` 和 `WebviewWindow.set_focus()`，可在宿主首次就绪事件里直接处理。

## 方案比较与选择

| 方案 | 改动 | 取舍 |
| --- | --- | --- |
| A. 首次宿主 `Ready` 事件聚焦（已选择） | 主窗口创建并进入事件循环后立即调用一次 `set_focus()`。 | 最接近窗口出现的时刻；只改后端，需为事件回调补充测试。 |
| B. 前端 `frontend_ready` 时聚焦 | 前端完成初始化后复用现有窗口聚焦接口。 | 接口复用较多，但会比窗口出现晚，且页面加载慢时延迟明显。 |

## 实施步骤

1. 在 `ECL/adapters/tauri.py` 为 `app.run_return()` 注册宿主事件回调，仅响应本次运行的首次 `RunEvent.Ready`。
2. 从事件回调提供的 `AppHandle` 按标签 `main` 获取主窗口，在主线程调用 `set_focus()`。找不到窗口或宿主拒绝时记录非敏感日志，不中断启动；其他事件直接忽略。
3. 保持主窗口现有 `visible`、装饰、阴影配置和 `frontend_ready` 行为；不添加 `alwaysOnTop` 或周期性聚焦。
4. 在 `tests/test_frontend_api_config.py` 补充回归测试：首次 `Ready` 聚焦 `main` 一次，其他事件或重复 `Ready` 不重复聚焦，缺失主窗口及宿主调用失败时启动流程仍可继续。既有三个窗口模式配置测试继续通过。
5. 执行 `ruff check ECL tests`、`ruff format --check ECL tests`、相关 `pytest`；再用隔离数据目录在 Windows 11 实际启动，使用前台窗口句柄或键盘输入确认主窗口出现后获得焦点，并确认切到其他程序后不会再次抢焦点。
6. 测试通过后提交一次只包含本功能的主仓库 commit；不推送远端。

## 边界与风险

- Windows 对后台程序抢占前台焦点有限制，系统可能拒绝这次请求；实现遵守系统判定，不使用置顶或模拟输入绕过。启动器从用户操作直接启动时应优先验证。
- 宿主 `Ready` 代表原生窗口可用，并不表示前端页面已经完成加载；本功能只聚焦窗口，不自动聚焦某个输入框。

## 参考

- [Tauri 窗口配置中的 `focus` 默认值](https://v2.tauri.app/reference/config/#windowconfig)
- [Tauri `RunEvent::Ready` 事件](https://docs.rs/tauri/latest/tauri/enum.RunEvent.html#variant.Ready)
- [Windows 前台窗口限制](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow)

## 实施与验证记录

- `Adapter.run()` 在 Tauri 主循环注册事件回调；只在首次 `RunEvent.Ready` 中按 `main` 标签获取窗口并调用一次 `set_focus()`。窗口不存在或宿主调用失败时记录日志，启动流程继续。
- 回归测试覆盖非 `Ready` 事件、重复 `Ready`、主窗口缺失及宿主拒绝焦点。`ruff check ECL tests`、`ruff format --check ECL tests` 通过；完整后端测试为 583 项通过、1 项跳过。
- Windows 11 上用隔离数据目录实际启动，主窗口出现时成为系统前台窗口（启动后约 3.0 秒）。手动切回 ChatGPT 后观察 2 秒，前台窗口未再次被启动器夺回；测试实例已关闭，原有实例保留。
