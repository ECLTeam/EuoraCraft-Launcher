# 插件界面开发

## 选择界面形式

| 形式         | 运行方式                                   | 使用建议                                 |
| ------------ | ------------------------------------------ | ---------------------------------------- |
| 声明式面板   | 宿主按有界节点树渲染，按钮调用自身命令     | 设置、列表、任务和简单工具优先使用       |
| 主窗口注入   | HTML/Vue 与主窗口同文档执行                | 仅用于明确需要主界面修改且用户信任的插件 |
| 独立原生窗口 | 包内单文件前端、隔离来源、原生控制及专用桥 | 需要复杂自有前端且不进入主窗口运行时     |

统一插件设置由宿主按 `settings.define` 渲染，无需插件为基本配置另造页面。独立前端仍可读写自己的相同设置。

## 声明式面板

`define_panel(key, title, nodes, description=..., data_callback=...)` 支持 text、group、button、setting、list、task。设置节点只能引用已定义键，按钮只能调用本插件已登记命令。

```python
self.define_panel("tools", "Tools", (
    {"type": "text", "text": "Configure and run the task"},
    {"type": "setting", "key": "greeting"},
    {"type": "button", "label": "Run", "command": "run"},
))
```

单面板最多 48 个节点、深度 3，单运行最多 16 个面板。列表与任务数据回调返回 `lists` 和 `tasks`，不返回 DOM、模板或函数。面板任务展示状态与宿主 `TaskView.state` 是不同模型，不能直接把后者原样塞入节点。

参考 [worker_panel](../../../examples/plugins/worker_panel/README.md)及[声明式面板设计](../../designs/plugin-declarative-panel-design.md)。

## 主窗口插槽

清单单独申请 `ui.inject`。通过 `define_html_slot(slot_id, html, key=..., context_key=...)` 或 `define_vue_slot(slot_id, component_name, template, script=..., style=..., key=..., context_key=...)` 注册。

已有插槽包含标题栏、侧栏、内容顶部/底部、实例区域、插件页、设置、任务队列等；是否挂载及其上下文由实际页面决定。稳定 key 用于更新同一内容，不要每次刷新制造随机插槽身份。

```python
self.define_html_slot(
    "plugin-slot-content-bottom", "<div>Plugin status</div>", key="status"
)
```

用户关闭主窗口注入总开关后撤销第三方内容；重新开启按当前宿主流程重载获准插件以重新登记。禁用、卸载和运行结束也会清理。

同页注入可访问主窗口 DOM 和通用桥，当前尚无完整通用桥隔离。`frontend/src/plugin-sdk` 的主题、DOM、控件、状态、路由和业务封装是主窗口工具，不属于 Worker 或独立窗口的契约接口。详见[注入决策](../../designs/plugin-main-window-injection-decision.md)。

## 独立原生前端

清单示例：

```json
{
    "name": "my_ui",
    "api_version": 1,
    "version": "1.0.0",
    "entry_point": "main:Plugin",
    "custom_ui": {
        "entry_file": "index.html",
        "title": "My UI",
        "width": 800,
        "height": 600
    }
}
```

作者预先构建并内联 JS、CSS 和其他资源到 HTML，入口最多 512 KiB。不能用远程 URL、绝对路径、父目录或动态 HTML 替换入口。宿主固定运行副本字节，修改源 HTML 后需经过热重载或重新启用。

Worker 调用 `ui.windows.open({})` 返回本运行 `window_ref`，再使用 `get/focus/close`。当前最多两个活动窗口、十六个累计引用；撤权和重载关闭旧窗口，引用不能跨运行恢复。

页面使用异步 `ECL.call(method, params)`。白名单为：

| 方法                               | 用途                                     |
| ---------------------------------- | ---------------------------------------- |
| `runtime.info` | 本运行最小摘要                           |
| `commands.call`                    | 自身命令；参数为 `command_ref` 和 `args` |
| `settings.get/set`                 | 本插件配置，写入带 expected_revision     |
| `operations.get/list/cancel`       | 当前运行自己的任务                       |

窗口桥不直接提供账户、配置分区、网络、文件路径或全部 IPC。业务操作由页面调用自己的命令，再由 Worker 通过运行准入访问 Host API。

维护版原生运行时在执行插件代码前安装导航、来源、文件选择、通用桥和网络控制。引擎及平台不满足条件时明确拒绝，不能换成普通 iframe 来绕过。完整例子见 [worker_custom_ui](../../../examples/plugins/worker_custom_ui/README.md)。

## 主题与可访问性

插件应保留清晰的文字和控件对比度、可访问名称、键盘交互与焦点。主窗口扩展沿用现有主题 token，不覆盖用户皮肤、亮暗、透明度或模糊设置。独立前端自有样式也需要验证最小窗口和长文本。

主题来源可通过 `config.public` 等有界接口读取。不要通过复制整个主窗口配置或共享主窗口 JS 对象实现主题同步。
