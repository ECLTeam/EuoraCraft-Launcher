# 插件界面开发

## 选择界面形式

| 形式         | 运行方式                                   | 使用建议                                 |
| ------------ | ------------------------------------------ | ---------------------------------------- |
| 声明式面板   | 宿主按有界节点树渲染，按钮调用自身命令     | 设置、列表、任务和简单工具优先使用       |
| 主窗口注入   | HTML/Vue 与主窗口同文档执行                | 仅用于明确需要主界面修改且用户信任的插件 |
| 独立原生窗口 | 当前不提供，打开请求按契约拒绝             | 不要使用；改用声明式面板或主窗口注入     |

统一插件设置由宿主按 `settings.define` 渲染，无需插件为基本配置另造页面。

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

参考[声明式面板设计](../../designs/plugin-declarative-panel-design.md)。

## 主窗口插槽

主窗口注入对所有插件开放，无需申请权限。通过 `define_html_slot(slot_id, html, key=..., context_key=...)` 或 `define_vue_slot(slot_id, component_name, template, script=..., style=..., key=..., context_key=...)` 注册。

已有插槽包含标题栏、侧栏、内容顶部/底部、实例区域、插件页、设置、任务队列等；是否挂载及其上下文由实际页面决定。稳定 key 用于更新同一内容，不要每次刷新制造随机插槽身份。

```python
self.define_html_slot(
    "plugin-slot-content-bottom", "<div>Plugin status</div>", key="status"
)
```

禁用、卸载和运行结束会清理插件注入的主窗口内容。

同页注入可访问主窗口 DOM 和通用桥，当前尚无完整通用桥隔离。`frontend/src/plugin-sdk` 的主题、DOM、控件、状态、路由和业务封装是主窗口工具，不属于 Worker 或独立窗口的契约接口。详见[注入决策](../../designs/plugin-main-window-injection-decision.md)。

## 独立原生前端（不再提供）

独立窗口隔离已停止支持。`ui.windows.open` 在当前运行时一律返回 `PLUGIN_UI_ISOLATION_UNAVAILABLE`，不创建窗口；`pytaurix` 0.1.0a1 尚未提供顶层导航锁定与消息 URI 拦截，原自维护 wheel 的隔离安装入口已随运行时退役，按契约显式拒绝，也不退回无隔离的窗口实现。

需要自定义界面时使用声明式面板或主窗口插槽。清单 `custom_ui` 声明与窗口消息协议作为目标契约保留在[插件包格式](../../protocols/eclplugin-package-format.md)，待运行时具备等价隔离原语并重新验收后再开放。

## 主题与可访问性

插件应保留清晰的文字和控件对比度、可访问名称、键盘交互与焦点。主窗口扩展沿用现有主题 token，不覆盖用户皮肤、亮暗、透明度或模糊设置。

主题来源可通过 `config.public` 等有界接口读取。不要通过复制整个主窗口配置或共享主窗口 JS 对象实现主题同步。
