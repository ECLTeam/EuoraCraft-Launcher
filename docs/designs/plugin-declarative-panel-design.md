# 第三方插件声明式操作面板设计

日期：2026-10-31，Asia/Shanghai。状态：已按本契约实现并集成本地 `main`（后端 `panels.register/list/data`、前端渲染器、示例插件与测试）。后续按[主窗口注入决策](plugin-main-window-injection-decision.md)并行增加插槽注入能力，本面板作为结构简单场景的通道保留。

本文件补充已获实施批准的 [Worker/API v2 设计](plugin-worker-api-v2-implementation-design.md)，不改变授权模型、变更回执和运行监管；面板只增加一种「插件向可信界面描述自己界面」的有界表达方式。

## 1. 目标与硬约束

插件需要在启动器里有自己的操作界面：显示说明、执行自己的命令、编辑自己的设置、展示列表与任务进度。当前第三方插件只有设置弹窗，界面能力为零。

硬约束来自现有边界，不因增加面板而放开：

- **不在主窗口执行第三方脚本。** 面板只由宿主渲染受限节点，插件不提供 HTML、CSS、JS、模板字符串或 Vue 组件；这与系统插件的 Vue 插槽机制是两条路径，不能混用。
- **不新增业务权限。** 面板注册、读取数据、执行动作都只要求 `plugin.base`；动作本身能否成功仍由被调用命令所在运行的原授权决定。
- **面板是运行期快照。** Worker 停止、禁用或授权撤销后，注册随之失效，管理页不再显示可操作面板，不保留可点击的旧引用。

## 2. 面板模型

`panels.register` 一次提交至多 8 个面板，单个面板至多 48 个节点、深度至多 3 层。

```text
PanelDefinition
  key            ^[A-Za-z_][A-Za-z0-9_.-]{0,63}$     本插件命名空间内的面板标识
  title          1..120                               面板标题
  description    0..1000                              可选说明，纯文本
  nodes          1..48                                节点树
  data_callback_ref  32 位十六进制，可选             数据回调标识
```

节点按 `type` 区分，全部字段都是标量或受限子节点，不接受可执行内容：

| 节点 | 字段 | 渲染结果 | 上限 |
| --- | --- | --- | --- |
| `text` | `text`、`tone`（default/muted/warning） | 纯文本段落，不解析 Markdown 或 HTML | 2000 字 |
| `group` | `title`、`children` | 带标题的分组容器 | 16 个子节点 |
| `button` | `label`、`command`、`args`、`tone`（default/primary/danger）、`confirm` | 按钮，点击经宿主执行该插件已登记命令 | label 60 字、args 16 个标量键 |
| `setting` | `key` | 复用该插件设置定义的控件，写入走现有 `settings.set` | 必须已在设置 schema 中 |
| `list` | `source`、`columns`、`empty_text` | 表格，数据来自 `PanelData.lists[source]` | 8 列、100 行 |
| `task` | `source` | 任务进度条，数据来自 `PanelData.tasks[source]` | 16 个任务 |

`button.command` 只接受命令名，不接受回调标识；宿主在执行时按「本运行已登记命令」解析，未登记即拒绝。这样面板无法指向其他插件或宿主内部入口。

## 3. 动态数据

静态节点不足以表达列表和进度。面板可声明 `data_callback_ref`，由宿主按需拉取：

```text
PanelData
  lists   {name: [{column_key: string}]}    至多 8 个列表、每个 100 行、每行 8 列
  tasks   {name: {state, percent, detail}}  至多 16 个任务
          state ∈ pending | running | succeeded | failed
          percent 0..100 整数，detail ≤ 200 字
```

拉取发生在 `panels.data` 请求内，预算 5 秒；超时按 `PLUGIN_DEADLINE_EXCEEDED` 失败，界面显示可重试错误，不显示半份数据。数据只在请求时拉取，不做后台推送，避免新增订阅通道与撤销竞态。

## 4. Host API 方法清单

| 方法 | 方向 | 授权 | 参数 | 结果 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `panels.register` | 插件 → 宿主 | `plugin.base` | `{panels: [PanelDefinition]}` | `{panel_refs: [str]}` | 变更方法，走回执去重；重复 key 或超上限拒绝 |
| `panels.list` | 插件 → 宿主 | `plugin.base` | `{}` | `{panels: [PanelDefinition]}` | 返回本运行已登记面板，供插件自检 |
| `panels.data` | 插件 → 宿主 | `plugin.base` | `{key}` | `PanelData` | 未声明数据回调时返回空结构，不报错 |

宿主到 Worker 的动作调用不新增插件可见 API：可信管理页经 IPC `plugin_panel_action` 让宿主执行 `broker.call_command(name, args)`，复用既有输入/输出 schema 校验、授权检查与回调超时处理。插件侧仍用 `commands.call` 调用自己的命令。

插件 SDK 新增 `Plugin.define_panel(key, title, nodes, *, description, data_callback)`，内部注册回调并提交 `panels.register`。

## 5. 可信界面与渲染

| 环节 | 实现 | 失败条件 |
| --- | --- | --- |
| 入口 | 插件管理页对已启用且登记了面板的插件显示「面板」按钮 | 插件未运行或注册为空时不显示，不显示禁用态占位 |
| 面板列表 | 弹窗内按 `key` 分页签，标题取 `title` | 注册后撤销授权：下次请求返回 `PLUGIN_PERMISSION_REVOKED`，关闭弹窗并提示 |
| 节点渲染 | 只使用项目现有控件（按钮、输入、表格、进度条、分组），文字统一走文本节点 | 未知节点类型整体拒绝渲染并提示版本不兼容，不静默跳过 |
| 动作 | 点击按钮 → `plugin_panel_action` → 成功后重取 `panels.data` 与设置 | 命令失败显示稳定错误码文案，不显示宿主异常堆栈 |
| 状态 | 加载、空、失败、无权限四种状态均有独立文案 | 不允许空白弹窗或永久 loading |
| 可访问性 | 焦点进入首个可操作节点，Esc 关闭，Tab 顺序与 DOM 一致 | 键盘不可达即验收不通过 |
| 主题 | 复用现有主题变量与卡片样式，亮暗、透明度、模糊设置下均验证 | 局部硬编码覆盖主题视为不通过 |

六种语言的文案与 `plugins.*` 命名空间一起维护，不在组件内写中文常量。

## 6. 失败与边界条件

| 场景 | 结果 |
| --- | --- |
| 节点数、深度、字符串长度越界 | `PLUGIN_SCHEMA_INVALID`，整批注册失败，不部分生效 |
| 同一 `key` 重复注册 | `PLUGIN_REGISTRATION_CONFLICT` |
| `setting.key` 未在设置 schema 中定义 | `PLUGIN_SCHEMA_INVALID` |
| `button.command` 未登记或已被撤销 | `PLUGIN_RESOURCE_NOT_FOUND`，按钮显示失败提示，不重试 |
| 数据回调超时或返回不合规 | `PLUGIN_DEADLINE_EXCEEDED` / `PLUGIN_SCHEMA_INVALID`，界面显示可重试错误 |
| 面板引用的设置在面板打开期间被改写 | 重取数据与设置后再渲染，不使用本地过期值 |
| Worker 停止、禁用、卸载 | 注册随运行清理；已打开弹窗在下次请求时收到明确失败并关闭 |

面板不是 OS 安全沙箱：插件进程仍可调用宿主已授权 API。面板只解决「界面由谁渲染」，不把渲染隔离描述成执行隔离。

## 7. 测试与验收

后端（pytest）：

- 注册成功返回引用；节点/深度/长度越界、重复 key、未定义设置键分别被拒。
- `panels.list` 返回已登记内容；运行关闭后拒绝。
- `panels.data` 在未声明回调时返回空结构；回调超时与不合规返回分别报稳定错误。
- 面板动作走 `call_command`：未登记命令拒绝，成功路径返回经输出 schema 校验的结果。

前端（vitest）：

- 六种节点渲染与未知节点拒绝。
- 加载、空、失败、无权限四态。
- 按钮动作成功后重新拉取数据；失败显示错误码文案。
- 键盘焦点与 Esc 关闭。

真实界面：在 Windows 桌面用示例插件完成一次「打开面板 → 改设置 → 执行命令 → 列表与进度更新」，并检查亮暗主题与 960×600 最小窗口。

## 8. 不在本次范围

- 自定义前端与独立窗口（本批后续项，按[原生隔离设计](plugin-native-webview-isolation-design.md)推进）。
- 面板内的富文本、图表、图片、表单校验表达式、任意布局容器。
- 面板事件回推（点击即命令，不做持续推送）。
- 跨插件面板互调、面板级独立权限。
