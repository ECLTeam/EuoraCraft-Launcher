# 快速开始

## 1. 创建插件

目录结构：

```text
hello_plugin/
  plugin.json
  main.py
```

`plugin.json`：

```json
{
    "name": "hello_plugin",
    "api_version": 2,
    "version": "1.0.0",
    "title": "Hello Plugin",
    "description": "命令、设置与面板示例",
    "entry_point": "main:Plugin",
    "permissions": []
}
```

`main.py`：

```python
from __future__ import annotations

from ECL.plugin_api import Plugin as BasePlugin


class Plugin(BasePlugin):
    def on_load(self) -> None:
        self.host.call("settings.define", {"definitions": [
            {"key": "prefix", "type": "string", "default": "Hello", "description": "问候前缀"}
        ]})
        self.define_command(
            "greet", self.greet,
            input_fields=({"name": "name", "type": "string", "max_length": 120},),
            output_fields=({"name": "message", "type": "string"},),
        )
        self.define_panel("main", "Hello", (
            {"type": "setting", "key": "prefix"},
            {"type": "button", "label": "问候", "command": "greet", "args": {"name": "Player"}},
        ))

    def greet(self, name: str) -> dict[str, str]:
        result = self.host.call("settings.get", {"keys": ["prefix"]})
        values = result["values"]
        if not isinstance(values, dict) or not isinstance(values.get("prefix"), str):
            raise ValueError("设置响应无效")
        return {"message": f"{values['prefix']}, {name}!"}
```

没有业务权限的插件也要确认当前内容后才能执行。`permissions: []` 不表示跳过首次启用审阅。

## 2. 安装并查看

1. 启动启动器，进入插件管理页。
2. 使用“安装 → 从目录安装”，选择上述目录。
3. 启用插件并完成当前内容确认。
4. 打开插件面板，修改前缀并执行问候命令。
5. 打开统一“插件设置”，左侧选择此插件，确认与面板看到相同配置。

从目录安装会复制文件到实际安装目录。原始导入目录后续的编辑不会自动同步。使用统一设置右侧的“打开插件目录”定位实际开发目标。

## 3. 开启开发模式

使用 `python main.py --plugin-dev` 启动，或手动加入可选配置 `plugins.development_mode: true` 后重启。按普通流程审阅并启用插件，等待监听状态，再编辑实际安装目录的 `main.py`。保存后宿主等待文件稳定、准备副本、停止旧 Worker 并启动新版。

修改 `greet` 的文字可以观察新版效果；重载后 `prefix` 的用户值应保留。语法错误会使新版本准备失败，已有效的旧副本可继续运行；修正后再次保存恢复。

添加权限、改变入口或依赖等敏感声明需要重新审阅，不能靠反复保存自动批准。退出后使用 `--no-plugin-dev` 重启可恢复普通模式，变化后的源码须重新审阅；命令行参数不会写回配置。

## 4. 制作归档

在主仓库环境执行：

```powershell
python packaging/build-eclplugin.py <hello_plugin目录> <输出目录>/hello_plugin.eclplugin
```

输出不能位于插件源目录内。无第三方库依赖时不需要锁文件；有依赖时先阅读[依赖与打包](packaging.md)。归档安装有独立的预检、来源确认、重启及内容审阅流程。
