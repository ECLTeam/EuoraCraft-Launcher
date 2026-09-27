# 启动高级选项补全实施方案（wrapper / 后退出命令 / 环境变量 / 窗口标题 / 启动器可见性）

> 状态：待用户确认（AGENTS.md 工作流第 5 步）
> 日期：2026-09-27
> 来源：[docs/feature-comparison-and-optimization-plan.md](feature-comparison-and-optimization-plan.md) P1-10（对标 HMCL `LaunchOptions` / PCL 实例设置同类项）
> 已确认决策：**全局 + 实例覆盖**；可见性**不动作/启动后最小化/启动后退出**三档；窗口标题**模板占位符**；wrapper **占位符 + 前缀兼容**。

## 1. 功能定义与语义

| 功能 | 语义 | 对标 |
| --- | --- | --- |
| 包装命令 `wrapper_command` | 包裹 java 命令：含 `{}` 占位符时替换为完整命令（如 `optirun {}`）；无占位符时作为前缀拼接（`wrapper java ...`） | HMCL `wrapper` |
| 后退出命令 `post_exit_command` | 游戏进程退出后（无论退出码）在实例工作目录异步执行，不阻塞退出结算与崩溃分析；失败仅记录日志 | HMCL `postExitCommand` |
| 自定义环境变量 `env_vars` | 多行 `KEY=VALUE` 文本；非法行（无 `=`、键为空）记录警告并跳过。合并优先级：系统环境 < 用户变量 < 插件 env（插件覆写语义保持不变） | HMCL `environmentVariables` |
| 窗口标题 `window_title` | 模板占位符：`{instance}`（实例名）、`{version}`（MC 版本）、`{account}`（玩家名）；留空不修改。启动后等待游戏主窗口出现（最长 60s）后改写标题 | PCL `LaunchArgumentTitle` |
| 启动器可见性 `launcher_visibility` | `none`（不动作）/ `minimize`（启动成功后最小化主窗口，可从任务栏恢复）/ `quit`（启动成功后优雅关闭启动器，保留运行中的游戏实例；崩溃检测与运行统计随之失效——与竞品行为一致） | HMCL/PCL `LauncherVisibility` |

窗口标题的平台策略：Windows 用 `ctypes` 直调 user32（`EnumWindows` 按 PID 匹配可见主窗口 → `SetWindowTextW`），**零新依赖**；POSIX 检测到 `xdotool` 时按 PID 搜索并改名，否则静默跳过。

## 2. 配置模型

### 2.1 全局设置（默认值）

`ECL/utils/config.py` game 分区新增：

```python
"wrapper_command": "",
"post_exit_command": "",
"env_vars": "",
"window_title_template": "",
"launcher_visibility": "none",   # none / minimize / quit
```

同步：`frontend/src/types/config.ts`、`settingsStore.ts` 默认值、`GameTab.vue` 游戏分区 UI（三个文本域 + 标题输入框 + 可见性下拉）、六语言 i18n。

### 2.2 实例覆盖（`.ecl/settings.json` + `instanceSettings.ts`）

新增同名字段：`wrapperCommand / postExitCommand / envVars / windowTitle / launcherVisibility`，**留空 = 回退全局**（与现有 `jvmArgs` 空值语义一致，不做显式"禁用"档）；`launcherVisibility` 实例侧用 `"inherit"`（回退全局）/ `none` / `minimize` / `quit`。UI 加到实例详情设置 Tab。

### 2.3 合并规则

前端沿用现有模式：`useInstanceManager.ts` 组装 `game_launch` 载荷时，实例字段非空用实例值，否则用全局值；后端 `LaunchRequest` 新字段全部带默认空值，缺省时不改变现有行为。

## 3. 后端改动

| 位置 | 改动 |
| --- | --- |
| `ECL/api/models.py` | `LaunchRequest` 新增 `wrapper_command: str = ""`、`post_exit_command: str = ""`、`env_vars: str = ""`、`window_title: str = ""`、`launcher_visibility: Literal["none","minimize","quit"] = "none"`；文件头清单同步 |
| `ECL/services/game/launch.py` | ① `launch_instance` 新参数接收与归一化；② 命令拼装处应用 wrapper（`{}` 替换或前缀）；③ `launch_context.env` 初始化为解析后的用户变量（插件钩子可覆写，合并点 `launch.py` 现有的 `{**os.environ, **launch_context.env}` 不变）；④ `on_instance_exit` 回调追加 `_run_post_exit_command`（后台线程 fire-and-forget，工作目录为实例目录）；⑤ 新增 `_start_title_rewriter`：启动成功后 daemon 线程等待游戏主窗口（60s 超时、进程退出即停）渲染模板并改标题；⑥ 启动成功且可见性非 `none` 时 `events.emit("launcher:visibility", {"action": ...})` |
| `ECL/services/frontend_events.py` | 桥接表新增 `"launcher:visibility"` 事件 |
| `ECL/api/models.py` 头部 | 新字段注释 |

环境变量解析、wrapper 拼装、模板渲染实现为 `launch.py` 内可单测的静态/模块级纯函数（`_parse_env_vars`、`_apply_wrapper`、`_render_window_title`）。

## 4. 前端改动

| 位置 | 改动 |
| --- | --- |
| `src/types/config.ts` | game 分区 5 个新键 |
| `src/features/settings/stores/settingsStore.ts` | 默认值 |
| `src/views/settings/GameTab.vue` | 游戏"启动行为"区新增：包装命令、后退出命令、环境变量（NInput textarea 多行）、窗口标题模板（含占位符提示）、可见性下拉 |
| `src/features/instances/model/instanceSettings.ts` | `VersionLaunchSettings` 新增 5 字段 + `createDefaultVersionSettings`/解析 |
| 实例详情设置 Tab | 5 个覆盖输入（留空=继承全局） |
| `src/composables/useInstanceManager.ts` | 载荷合并：实例非空 → 实例值，否则全局值 |
| `src/App.vue`（或 `useAppRuntime`） | 监听 `launcher:visibility`：`minimize` → `desktopWindow.minimize()`；`quit` → 复用现有 `desktopWindow.close()` 优雅退出路径（跳过退出确认弹窗——由启动器自动触发的退出不再反问） |
| i18n 六语言 | 新键 |

## 5. 测试计划

- **后端 pytest**（`tests/test_launch_advanced_options.py` 或并入现有）：
  - `_parse_env_vars`：合法行/非法行（无 `=`、空键、重复键后者生效）/空白行。
  - `_apply_wrapper`：含 `{}` 替换、无 `{}` 前缀拼接、空 wrapper 不变。
  - `_render_window_title`：三个占位符渲染、未知占位符保留原文、空模板返回空。
  - 启动链路集成（mock create_instance）：env 合并顺序（系统 < 用户 < 插件）、wrapper 前缀生效、`launcher:visibility` 事件按档位发射（none 不发）、退出回调触发 post_exit 命令（含非零退出码也执行）。
  - 实例覆盖合并：前端职责不测后端，但校验 `LaunchRequest` 默认值零行为变化。
- **命令**：`ruff check ECL tests`、`ruff format --check`、全量 `pytest`；前端 `pnpm check`、`pnpm build`。
- **实机验证**：① 设置窗口标题模板启动一次，确认游戏窗口标题被改写；② 可见性=最小化/退出各验证一次（退出后游戏仍运行）；③ wrapper 用 `cmd /c` 语义验证前缀拼接；④ 实例覆盖值与全局回退各验一次。

## 6. 实施步骤（每步独立提交）

| 步骤 | 内容 | 类型 |
| --- | --- | --- |
| 1 | 后端：config 默认值 + `LaunchRequest` 字段 + 三个纯函数 + 单测 | feat |
| 2 | 后端：启动链路接线（wrapper/env/post-exit/标题改写/可见性事件）+ 集成测试 | feat |
| 3 | 前端：全局设置 UI + 实例覆盖 + 载荷合并 + 可见性事件处理 + i18n | feat |
| 4 | 文档：junsi-dev-docs API 规范更新 + ADR-019 | docs |

## 7. 风险与说明

| 风险 | 说明 |
| --- | --- |
| `quit` 档退出后崩溃检测/统计失效 | 竞品一致行为；设置项描述中明示 |
| POSIX 窗口标题依赖 xdotool | 未安装则静默跳过，不影响启动 |
| 游戏窗口匹配按 PID | 部分加载器会创建中间进程；以 psutil 子进程树 PID 集合匹配（含自身与直接子进程），60s 未匹配放弃 |
| `quit` 与退出确认弹窗 | 自动触发的退出跳过用户协议退出确认，直接走 `desktopWindow.close()` |
