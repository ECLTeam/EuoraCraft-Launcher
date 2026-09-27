# 启动命令类输入框改造实施方案（启动前命令逐行执行 / 包装命令与后退出命令文案与多行框 / 环境变量尺寸）

> 状态：待用户确认（AGENTS.md 工作流第 5 步）
> 日期：2026-09-27
> 前置文档：[docs/launch-advanced-options-plan.md](launch-advanced-options-plan.md)
> 已确认决策：启动前命令**前端多行框 + 后端逐行执行**；包装命令/后退出命令**标签名 + 说明 + 多行框**；环境变量**改大尺寸多行框**；实例详情页**同步修改**。

## 1. 问题与现状

### 1.1 实测发现：Windows 下多行输入当前是失效的

用 `subprocess.run(多行文本, shell=True)` 在本机（Windows 11）实测：

| 输入 | 实际结果 |
| --- | --- |
| `echo A\necho B` | 只输出 `A`，退出码 0 —— **第二行未执行且无任何报错** |
| `echo A & echo B` | 输出 `A`、`B`，退出码 0 |
| `echo A\r\necho B` | 只输出 `A`，退出码 0 |
| `exit 3\necho B` | 退出码 3 |

即：Windows 上 `shell=True` 只执行多行文本的**首行**，其余行被静默丢弃，退出码取自首行，用户完全无从察觉。POSIX 下 `sh -c` 会把换行当命令分隔符逐条执行，因此还存在跨平台行为不一致。

结论：用户提出的「支持换行表示一条命令」不是单纯 UI 诉求，而是修复一个既有的功能性缺陷。

### 1.2 各输入框现状

`frontend/src/views/settings/GameTab.vue`（设置 → 游戏，全局）：

| 字段 | 当前形态 |
| --- | --- |
| `pre_launch_command` | 单行 `NInput`，无尺寸类，渲染很窄 |
| `wrapper_command` | 单行 `NInput`，无尺寸类 |
| `post_exit_command` | 单行 `NInput`，无尺寸类 |
| `env_vars` | `textarea`，`minRows: 2 / maxRows: 6`，**无尺寸类** |

`frontend/src/components/instances/InstanceDetailSettingsTab.vue`（实例详情 → 设置 → 高级选项）有 `wrapper_command`、`post_exit_command`、`env_vars` 三项，形态与上表后三项一致（该页无 `pre_launch_command`，后端也仅从全局配置读取）。

同页已有的 `jvmArgs`、`gameArgsTail` 使用 `class="advanced-argument-input"`（`width: min(440px, 42vw)`）配 `type="textarea"`，即为本次要对齐的目标形态。

### 1.3 后端执行语义（`ECL/services/game/launch.py`）

| 字段 | 位置 | 语义 |
| --- | --- | --- |
| `pre_launch_command` | `_run_pre_launch_command`（L320-350） | 游戏进程创建前，在 `game_directory` 下 `shell=True` 整串执行；总超时 60s；超时 / 无法创建进程 / 非零退出码 → 抛错取消启动 |
| `post_exit_command` | `_run_post_exit_command`（L352-384） | 游戏退出后，在实例工作目录的独立 daemon 线程执行；总超时 60s；失败仅记日志 |
| `wrapper_command` | `_apply_wrapper`（L76-88） | 含 `{}` → 替换为完整启动命令；否则前缀拼接 |
| `env_vars` | `_parse_env_vars`（L54-73） | 每行 `KEY=VALUE`，空行忽略，无 `=` 或键为空的行告警跳过，重复键后写覆盖；优先级 系统环境 < 用户变量 < 插件 env |

## 2. 后端改动

### 2.1 新增模块级纯函数 `_split_command_lines`

`ECL/services/game/launch.py`：

```python
def _split_command_lines(text: str) -> list[str]:
    """
    把多行命令文本拆分为逐条命令。

    按行拆分并去除首尾空白，空行忽略；同一行内的 ``&&``、``&`` 等 shell
    连接符保持原样交由 shell 处理。Windows 下 shell 只会执行多行文本的首行，
    逐行拆分是该差异的规避手段。
    """
    return [stripped for line in (text or "").splitlines() if (stripped := line.strip())]
```

### 2.2 改写 `_run_pre_launch_command`：逐行执行 + 共享总超时

- 按行拆分，**按顺序**执行。
- 所有命令**共享同一个 60 秒总预算**（用 `monotonic()` 截止时间递减剩余额度）。这一点与现状一致：现在整串命令也是 60 秒总超时，不因行数放大等待时间。
- 任一条超时 / 无法创建进程 / 非零退出码 → 立即抛 `GameServiceError`，后续行不再执行，启动取消。
- 错误文案带行号，例如 `启动前命令第 2 条执行失败，退出码: 1`；错误码沿用 `PRE_LAUNCH_COMMAND_FAILED` / `PRE_LAUNCH_COMMAND_TIMEOUT`。
- 日志按条输出：`启动前命令第 N 条输出:\n...`。

### 2.3 改写 `_run_post_exit_command`：逐行执行，失败记录后继续

后退出命令本次也要改多行框，若不改后端，用户写三行清理命令只会执行第一行且**静默**（失败仅记日志），因此必须同步逐行化：

- 按行拆分，共享 60 秒总预算。
- 某条非零退出码 / 无法创建进程 → 记 warning 后**继续执行后续行**（后退出是尽力而为的清理语义，一条失败不应跳过其余）。
- 超时 → 记 warning 并停止剩余行（预算已耗尽）。
- 日志与告警均带行号。
- 保留现有 `exit_code` 形参（当前函数体内未使用），本次不动，避免牵动调用点与既有测试桩签名。

### 2.4 不做的事

- 不改 `_apply_wrapper`、`_parse_env_vars`、`_render_window_title`。
- 不改 `ECL/api/models.py`、`ECL/utils/config.py`（无新增字段）。
- 不给实例级新增 `pre_launch_command` 覆盖（超出本次范围）。

## 3. 前端改动

### 3.1 `frontend/src/views/settings/GameTab.vue`

四个输入框统一为 `type="textarea"` + `class="advanced-argument-input"` + `:autosize="{ minRows: 3, maxRows: 8 }"`，并补上 placeholder：

| 字段 | 改动 |
| --- | --- |
| `pre_launch_command` | 单行 → 多行框，加尺寸类与 placeholder |
| `wrapper_command` | 单行 → 多行框，加尺寸类与 placeholder |
| `post_exit_command` | 单行 → 多行框，加尺寸类与 placeholder |
| `env_vars` | 加尺寸类，行数由 `2/6` 提到 `3/8` |

`@blur="saveConfig"` 保持不变。

### 3.2 `frontend/src/components/instances/InstanceDetailSettingsTab.vue`

`wrapper_command`、`post_exit_command`、`env_vars` 三项统一为 `type="textarea"` + `class="argument-input"`（该页已有的多行框样式类，`width: min(430px, 48vw)`）+ `:autosize="{ minRows: 3, maxRows: 8 }"`，并补 placeholder。该页描述列沿用 `versions.detail.inheritGlobalDesc`（「留空时使用全局设置」），语义由标签名与 placeholder 承载。

### 3.3 CSS

**不需要新增尺寸类**，复用既有的 `.advanced-argument-input` / `.argument-input`。

可选（需用户确认，默认不做）：给 `GameTab.css` 的 `.advanced-argument-input :deep(textarea)` 加 `font-family: var(--font-mono)` 与 `font-size: 11px`，与实例页 `.argument-input :deep(textarea)` 一致，让命令类文本更易读。副作用：会同时改变 `jvmArgs`、`gameArgsTail` 两个既有输入框的字体。

### 3.4 i18n（6 个语言文件）

改动 / 新增键（`settings` 命名空间）：

| 键 | 处理 |
| --- | --- |
| `preLaunchCommand` | 标签不变，说明改为体现「每行一条、合计 60 秒、任一条失败即取消」 |
| `preLaunchCommandPlaceholder` | **新增** |
| `wrapperCommand` | 标签改名，如「包装命令」→「启动命令包装器」 |
| `wrapperCommandDesc` | 重写，补充用途举例与 `{}` 语义 |
| `wrapperCommandPlaceholder` | **新增** |
| `postExitCommand` | 标签改名，如「后退出命令」→「退出后执行命令」 |
| `postExitCommandDesc` | 重写 |
| `postExitCommandPlaceholder` | **新增** |
| `envVarsDesc` | 重写，点明「覆盖系统同名变量、插件同名变量覆盖此处」 |
| `envVarsPlaceholder` | 补充示例 |

文案草稿（zh-CN）：

```
preLaunchCommandDesc       启动游戏前在游戏目录执行；每行一条命令，按顺序执行，合计最多等待 60 秒；任一条失败或超时会取消启动
preLaunchCommandPlaceholder 每行一条命令，例如：echo 正在准备启动
wrapperCommand             启动命令包装器
wrapperCommandDesc         用外部程序包裹完整的 java 启动命令（如任务管理器、性能监控工具）；含 {} 时替换为完整启动命令，否则作为前缀拼接
wrapperCommandPlaceholder  例如：wrapper.bat {}
postExitCommand            退出后执行命令
postExitCommandDesc        游戏进程退出后执行（无论退出码）；每行一条命令；失败仅记录日志，不影响启动结果与崩溃分析
postExitCommandPlaceholder 每行一条命令，例如：echo 游戏已退出
envVarsDesc                每行一条 KEY=VALUE，空行忽略；会覆盖系统同名变量，插件提供的同名变量会覆盖此处设置
envVarsPlaceholder         KEY=VALUE，例如 JAVA_TOOL_OPTIONS=-Dfile.encoding=UTF-8
```

**强制注意**：`wrapperCommandDesc` 与 `wrapperCommandPlaceholder` 中的字面量 `{}` 必须转义为 `{'{'}{'}'}`，否则 vue-i18n 会报 `Empty placeholder` 编译错误（本次会话刚修复过同类问题）。placeholder 使用单行文本，textarea 的 placeholder 不渲染换行。

其余 5 个语言文件（zh-TW / en-US / ja-JP / de-DE / ru-RU）按同义翻译同步，键集合必须与 zh-CN 完全一致。

## 4. 测试计划

### 4.1 后端

`tests/test_launch_advanced_options.py` 新增用例（复用既有 `_make_launch_service` 桩，monkeypatch `launch_module.subprocess.run`）：

- `test_split_command_lines_drops_blanks_and_strips` —— 空行、首尾空白处理。
- `test_split_command_lines_keeps_shell_connectors_in_one_line` —— `"a && b"` 仍为一条。
- `test_run_pre_launch_command_executes_each_line_in_order` —— 多行 → 逐条调用，`cwd` 正确，顺序正确。
- `test_run_pre_launch_command_stops_on_intermediate_failure` —— 第 2 条返回非零 → 抛 `PRE_LAUNCH_COMMAND_FAILED`，第 3 条未执行。
- `test_run_pre_launch_command_shares_total_timeout` —— 每次调用传入的 `timeout` 之和不超过 60，且第二次小于第一次。
- `test_run_post_exit_command_continues_after_failure` —— 第 1 条失败后第 2 条仍执行，且不抛异常。

修复前这些用例必然失败（现状只执行首行、中间失败不检测），满足 AGENTS.md「缺陷修复需有修复前失败、修复后通过的测试」。

命令：

```
ruff check ECL tests
ruff format --check ECL tests
pytest tests/test_launch_advanced_options.py tests/test_game_service.py -q
```

### 4.2 前端

```
cd frontend && pnpm check     # Prettier + ESLint + vue-tsc + vitest
cd frontend && pnpm build
```

### 4.3 实际运行验证

1. 启动启动器，进入 设置 → 游戏，确认四个输入框均为撑宽的多行框，标签与说明文案、placeholder 显示正常。
2. 在「启动前执行命令」填入两行（如 `echo A` / `echo B`），启动一次实例，确认日志中出现**两条**命令的输出（修复前只会出现第一条）。
3. 故意让第二行为非法命令（如 `exit 1`），确认启动被取消且报错文案带行号。
4. 进入 实例详情 → 设置 → 高级选项，确认三项同样为撑宽多行框且文案一致。
5. 切换语言（中/英/日/德/俄/繁）确认无 i18n 编译报错、无 ErrorBoundary 报错。

## 5. 已知限制（将写入说明文案或在此备案）

- 同一行内仍可用 `&&` / `&` 连接，行内 shell 语义不变。
- 跨行引号（引号内含换行）会被拆断，属极端场景，不支持。
- 启动前命令与后退出命令均为 60 秒**总**预算，不随行数放大。
- 后退出命令失败后继续执行后续行，仅记录日志。

## 6. 改动文件清单

后端：

- `ECL/services/game/launch.py`（新增 `_split_command_lines`，改写两个执行方法）
- `tests/test_launch_advanced_options.py`（新增 6 个用例）

前端：

- `frontend/src/views/settings/GameTab.vue`
- `frontend/src/components/instances/InstanceDetailSettingsTab.vue`
- `frontend/src/i18n/locales/{zh-CN,zh-TW,en-US,ja-JP,de-DE,ru-RU}.json`

可选：

- `frontend/src/styles/views/settings/GameTab.css`（命令类文本改用等宽字体）

## 7. 提交计划

- 后端：`fix: 启动前与后退出命令改为逐行执行并修复 Windows 多行丢失`
- 前端：`feat: 启动命令类输入框改为多行大尺寸并重写命令类文案`