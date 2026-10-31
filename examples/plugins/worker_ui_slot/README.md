# 界面注入 Worker 示例

将本目录按 [v2 包协议](../../../docs/protocols/eclplugin-package-format.md)安装到 Windows x64 启动器。清单声明 `ui.inject`，启用前必须在授权界面单独勾选「修改启动器界面」；未批准时加载直接失败，不会静默降级。

## 演示内容

| 方法 | 插槽 | 内容 |
| --- | --- | --- |
| `define_html_slot` | `plugin-slot-content-bottom` | 一段静态 HTML 片段 |
| `define_vue_slot` | `plugin-slot-plugins-toolbar-after` | 一个带响应式数据的 Vue 组件 |

内容与启动器同页渲染，因此该权限与业务权限分开审阅。示例不读取任何宿主数据，也不调用其他 Host API。

## 对应契约

- [主窗口注入决策](../../../docs/designs/plugin-main-window-injection-decision.md)
- 单段内容上限 60000 字符；`key` 相同则原位更新，省略则追加
- 禁用、卸载或关闭总开关后，宿主撤销该插件注入的全部内容

## 可观察结果

1. 批准 `ui.inject` 并启用：插件页底部出现「来自插件的 HTML 片段」，工具栏右侧出现「来自插件的 Vue 组件」。
2. 关闭插件页的「允许插件修改界面」：两处内容立即消失，插件仍处于启用状态。
3. 重新打开总开关：需要重新启用插件才会再次出现，不会自动恢复旧内容。
