# 统一配置管理

普通持久化配置默认交给宿主的专用插件设置全屏弹窗，插件不自行实现配置页或弹窗；官方插件的实现约束见其独立仓库内 AGENTS.md。当次操作参数仍留在业务流程中。

## 定义及默认值

在 `on_load` 调用 `settings.define`，提交本运行的完整定义；再次调用会替换完整定义集合，不是逐项追加。

```python
self.host.call("settings.define", {"definitions": [
    {"key": "enabled", "type": "bool", "default": True, "description": "启用提醒"},
    {"key": "greeting", "type": "string", "default": "Hello", "description": "问候内容"},
    {"key": "interval", "type": "number", "default": 10, "description": "间隔秒数"},
    {"key": "mode", "type": "select", "default": "quiet", "options": ["quiet", "normal"], "description": "提示方式"}
]})
```

最多 64 项，键为 `[A-Za-z_][A-Za-z0-9_.-]{0,63}`。默认值必须符合类型；number 为有限数字，bool 不接受整数替代，select 必须属于不重复的选项。string 和多行 text 当前最多 16384 字符；text 在标准设置中心使用多行输入并保留换行。readonly_file 由用户在可信界面明确录入外部文件，Worker 不能自行扩大选择。定义不携带可执行验证器或任意 JSON Schema。

## 读取与写入

```python
snapshot = self.host.call("settings.get", {"keys": ["greeting"]})
saved = self.host.call("settings.set", {
    "values": {"greeting": "你好"},
    "expected_revision": snapshot["revision"],
}, mutation_key="a-stable-key-for-this-exact-write")
```

`settings.get` 返回所选值及 `revision`。写入必须带读取时的 revision，同一精确变更重试使用原 mutation key。同键不同请求会失败；超时先查询 `mutations.get`，不要换键盲目重放。

版本冲突 `PLUGIN_REVISION_CONFLICT` 要重新读取并由调用者决定如何编辑，不能自动覆盖别人刚保存的值。宿主还以内容和 `schema_revision` 防止可信界面的旧配置表单写入新代码定义。

## 配置变化事件

```python
callback = self.register_callback(self.settings_changed)
self.host.call("events.subscribe", {"event": "settings.changed", "callback_ref": callback})

# 在类中定义：
def settings_changed(self, values, revision, sequence):
    # values 是本次已提交的补丁；完整状态可重新 settings.get。
    self.latest_revision = revision
```

事件只属于本运行，不是全局配置总线。队列与回调有上限和时限，回调应快速完成；失效或可靠事件交付失败会影响该运行的可用性。写入已经持久化后不会因为事件失败伪造回滚。

## 全屏设置界面

插件页统一“插件设置”打开全屏弹窗，左侧搜索及选择已安装插件，右侧按照同一 schema 渲染控件。行内设置按钮定位同一界面。内部使用紧凑导航及标准设置行，选中状态随主题强调色变化，卡片表面、圆角和边框沿用公共样式。

开关和选择项修改后保存，文本在提交/失焦时保存；保存结果、失败和冲突明确显示。切换插件不会把迟到值或提示写入另一插件。保存与 Worker 及面板共用一个配置服务及 revision。

禁用插件后仍可管理已经发布且匹配当前内容的定义。不曾登记或内容已变化时，界面要求先启用；宿主不会为了查看配置而暗中执行插件。

主窗口前端 SDK 的 `getPluginSettings(name)` 返回统一视图。写入使用 `updatePluginSetting(name, key, value, settingsView)`，必须保留读取时的视图，不能只传一个 revision 或自行拼内容摘要。

## 持久化与定义变化

值继续保存在 `plugin_settings_v2/<installation_id>.json`，保留既有 `schema_version/revision/values` 格式。schema 元数据单独存储，只有新版成功加载后发布。

重载和更新不生成新安装身份，不清空用户值；卸载重装是另一安装身份，不继承运行状态。新增键投影默认值；移除键保留在原值文件中，但不展示。类型不兼容时明确失败，保留原文件，不用默认值静默覆盖。

不要直接读写宿主配置文件、把配置放进代码目录或维护另一份 JSON 作为同一设置的权威来源。跨运行持久数据如与用户可编辑设置无关，应使用私有文件 API。

## 从插件页面打开

宿主编译 Vue 页面或插槽时注入当前插件绑定的 `SDK`；保存引用后在点击时调用：

```javascript
const sdk = SDK;

module.exports = {
    methods: {
        openSettings() {
            return sdk.ui.openSettings();
        },
    },
};
```

不需要自行传插件名，也不需要跳转离开当前业务页面。插件列表中的设置按钮和插件页面中的入口使用同一个全屏弹窗；声明配置不依赖 `ui.inject`，没有业务页面的插件仍可从插件管理页配置。

房间码、临时端口等当次操作输入仍放在业务流程中。只有标准设置中心不能表达且已获用户确认的特殊业务才另行设计界面，不以配置项数量少为理由另做弹窗。
