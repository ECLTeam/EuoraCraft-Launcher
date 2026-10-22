# ECL 主题与插件外观扩展协议（V1）

日常操作和完整示例请先阅读 [ECL 可视化主题设计使用指南](../guides/theme-designer-guide.zh-CN.md)。

插件可在 `plugin.json` 的 `contributes` 中声明主题预设、效果、Token、节点和受控独立窗口。所有主题贡献都必须使用插件命名空间，且需要对应的 `theme/read/*` 权限；窗口需要资源粒度的 `ui/write/window:<id>` 权限。

```json
{
  "permissions": [
    { "scope": "theme", "action": "read", "resource": "preset:*" },
    { "scope": "theme", "action": "read", "resource": "effect:*" },
    { "scope": "theme", "action": "read", "resource": "token:*" },
    { "scope": "theme", "action": "read", "resource": "node:*" },
    { "scope": "ui", "action": "write", "resource": "window:inspector" }
  ],
  "contributes": {
    "themePresets": ["themes/aurora.json"],
    "themeEffects": ["themes/effects.json"],
    "themeTokens": ["themes/tokens.json"],
    "themeNodes": ["themes/nodes.json"],
    "windows": [
      {
        "id": "inspector",
        "title": "Example Inspector",
        "route": "/plugin/example/inspector",
        "singleton": true,
        "width": 720,
        "height": 560,
        "settings": ["accent"],
        "commands": ["refresh"],
        "events": ["example:data_changed"],
        "dataSchema": {
          "read": ["plugin.settings.accent"],
          "write": ["plugin.settings.accent"]
        }
      }
    ]
  }
}
```

主题预设 ID 必须形如 `plugin.<插件名>.<预设名>`；效果、Token 与节点 ID 必须以 `<插件名>.` 开头。插件窗口只能加载本地 `/plugin/<插件名>/...` 路由，且只能调用清单中声明的本插件命令和设置字段。

重复插槽通过 `contextKey` 定位单个业务实例。不传 `contextKey` 时注入该插槽的全部上下文：

```python
self.inject_html("task-queue-item-actions", "<button>详情</button>", context_key=task_id)
```

`.ecltheme` 可包含 PNG、JPEG、WebP、GIF、BMP、WOFF2 和 SVG。SVG 会在导入和读取时再次静态化：DTD、实体、脚本、事件属性、`foreignObject`、外部链接和危险 URL 会被拒绝或移除，因此不能用 SVG 执行业务逻辑。
