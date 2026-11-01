# 关于页图标连点开启调试模式与调试信息弹窗实施方案

日期：2026-11-01  
状态：已实施

## 目标与现状

在「设置 → 关于」中，启动器图标目前只是静态图片。需求是：连续点击图标 7 次时通过通知提示剩余次数，第 7 次自动开启调试模式并弹出调试信息弹窗，弹窗展示启动器图标、当前系统信息和启动器信息；调试模式已开启后，单击图标直接打开弹窗。

现状要点：

- 调试模式即配置项 `launcher.debug`，设置页「启动器」已有开关，`useDebugMode()` 负责读写，保存后由 `config:changed` 事件同步到 `appRuntime.isDevMode`（标题栏 DEBUG 徽标、调试工具窗口均依赖它）。
- 关于页已通过 `launcherInfoQuery()` 获取 `launcher_info`，但只返回版本、版本类型、调试状态和窗口模式，没有系统信息。
- 项目已有公共弹窗组件 `components/modals/Modal.vue`（`type="content"` 可自定义标题与页脚），以及统一的 `useLauncherMessage()` 通知封装。

## 选定方案

1. **后端补充系统信息**：扩展 `launcher_info` 的返回数据，新增 `system` 对象（系统名称、系统版本、架构、Python 版本、物理内存总量 MB）。Windows 依据 `sys.getwindowsversion().build` 区分 10 / 11（`platform.release()` 在不同 Python 版本下不一致），macOS 取 `platform.mac_ver()` 而不是内核版本，其他系统沿用 `platform` 原始字段。前端据此展示真实系统信息，不用浏览器 UA 推测。未新增 IPC 命令，避免为一次展示扩充注册表、日志策略与请求模型。
2. **连点计数放在独立组合式函数**：新增 `features/settings/composables/useDebugModeUnlock.ts`，负责点击计数、超时重置、剩余次数提示与开启调试模式；次数与计时器只存在内存中，离开页面即清理，不写入配置。计数达到 7 次时通过 `setDebugMode(true)` 持久化 `launcher.debug`，与设置页开关走同一条写入路径。
3. **提示策略**：前两次点击静默，从第 3 次开始用 `message.info` 提示「再点击 N 次开启调试模式」；每次新提示先销毁上一条，避免连点时通知堆叠。开启成功提示「调试模式已开启」，保存失败提示错误并保持未开启。
4. **弹窗独立成组件**：新增 `features/settings/components/DebugInfoModal.vue`，基于公共 `Modal`，分「系统信息」与「启动器信息」两组字段。每次打开都重新读取 `launcher_info`，避免内存与版本是启动时的旧快照；浏览器与演示模式下后端无数据时统一显示占位符 `—`，不伪造内容。调试状态由父组件传入，保证刚开启调试模式时弹窗立即显示「已开启」。
5. **关于页接线**：图标由 `div` 改为 `button`（`type="button"` + `aria-label`，图片 `alt` 置空避免重复朗读），点击时若调试模式已开启（配置已开启或后端运行时已开启）直接打开弹窗，否则进入连点计数流程。调试模式已开启时图标在悬停或键盘聚焦时顺时针旋转一圈，移出后沿原路径转回；未开启时保持静态，不暴露连点入口。系统开启减少动态效果时不做旋转。弹窗内的启动器图标同样支持悬停旋转（弹窗本身只在调试模式下出现）。
6. **弹窗命名**：弹窗标题统一为「信息」，六个语言分别使用对应译名，开启成功 / 失败通知沿用同一标题。
7. **弹窗打开时仍可拖动窗口**：遮罩保持原有层级与位置（仍覆盖整个窗口、顶部栏同样变暗），只把顶部栏高度范围内的点击放行给窗口拖动。实现为 `Modal` 新增 `titlebarInteractive` 开关：遮罩自身 `pointer-events: none`，弹窗容器恢复可交互，内容区补一层透明阻挡层（`inset: var(--titlebar-h) 0 0 0`）继续拦截背景点击，避免弹窗打开时误点页面按钮。默认关闭，仅「信息」弹窗启用。

## 修改范围

- `ECL/api/system.py`：新增 `_describe_platform()` / `_system_summary()`，`launcher_info` 返回 `system`。
- `tests/test_frontend_api_config.py`：新增 `test_launcher_info_reports_current_system_summary`。
- `frontend/src/types/system.ts`：新增 `SystemSummary`，`LauncherInfo.system` 为可选（浏览器 / 演示模式无此字段）。
- `frontend/src/api/transport/showcase/index.ts`：演示模式补一份示例系统信息。
- `frontend/src/features/settings/composables/useDebugModeUnlock.ts`（新增）及其测试。
- `frontend/src/features/settings/components/DebugInfoModal.vue` + `frontend/src/styles/views/settings/DebugInfoModal.css`（新增）及其测试。
- `frontend/src/views/settings/AboutTab.vue`、`frontend/src/styles/views/settings/AboutTab.css`：图标按钮、悬停旋转、连点处理、弹窗挂载；同时修正 `runtimeMode` 注入形态（根组件提供的是只读 ref，原代码按字符串比较，导致演示模式被当成浏览器模式）。
- 六语言文案 `settings.aboutTab.debugInfo.*`（zh-CN / zh-TW / en-US / de-DE / ja-JP / ru-RU）。

## 验证

- 前端：`eslint`、`prettier --check`、`vue-tsc`、`vitest run` 全量通过（193 文件 / 1185 用例）。
- 后端：`ruff check`、`ruff format --check` 通过；`pytest tests/test_frontend_api_config.py tests/test_operation_logging.py tests/test_api_models.py` 通过。
- 新增用例覆盖：前两次静默与第三次起提示、第 7 次开启并提示成功、间隔超时重新计数、开启失败提示、卸载清理；弹窗字段展示、后端缺失占位、调试状态、每次打开重新读取、关闭按钮；关于页图标按钮语义、七次点击开启并弹窗、已开启时单击直接弹窗；悬停旋转与减少动态效果样式。
- 演示模式实际界面验收：无头浏览器连点七次弹出「信息」弹窗，字段与占位符显示正常；重新加载后调试模式仍为已开启，单击图标直接打开弹窗；悬停时图标顺时针旋转、移出后转回。

## 风险与边界

- 连点依赖同一页面内的内存计数，切换页面或超过 1.5 秒间隔重新计数，避免零散点击误触发。
- 调试模式是持久化配置，重启后仍然有效，可在「设置 → 启动器」关闭；弹窗底部已提示关闭位置。
- 系统信息来自后端运行环境；浏览器 / 演示模式无后端数据时只展示启动器版本与运行形态，其余字段显示占位符。
