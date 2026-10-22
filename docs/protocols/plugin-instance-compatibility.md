# 插件实例兼容扩展点

ECL 插件可以注册只读的 Minecraft 实例元数据来源。该扩展点用于兼容其他启动器的实例索引或单实例配置，不应修改第三方文件。

## 注册读取器

普通插件需在 `plugin.json` 中声明实例读取权限：

```json
{
  "permissions": [
    { "scope": "instances", "action": "read", "resource": "example" }
  ]
}
```

在 `on_enable` 中注册来源：

```python
from ECL.plugins import ExternalInstanceMetadata, Plugin


class ExampleCompatibilityPlugin(Plugin):
    def on_enable(self) -> None:
        super().on_enable()
        self.register_instance_compatibility(
            "example",
            "Example Launcher",
            self.read_instance,
            self.watch_paths,
        )

    def read_instance(self, context):
        config_path = context.instance_path / "example.json"
        if not config_path.is_file():
            return None
        return ExternalInstanceMetadata(
            source="example",
            modified_ns=config_path.stat().st_mtime_ns,
            fields={"description": "Example metadata"},
        )

    def watch_paths(self, options):
        return []
```

`context` 提供：

- `game_path`：Minecraft 根目录。
- `instance_path`：当前版本目录。
- `version_id`、`vanilla_name`、`primary_loader`：宿主扫描结果。
- `options`：宿主按来源名分组的兼容配置。

`ExternalInstanceMetadata.fields` 可提供 `description`、`favorite`、`pinned`、`hidden`、`categoryId`、`icon` 等展示字段；`stats` 可提供 `launchCount`、`totalRunDurationSeconds` 和 `lastLaunchedAt`。ECL 本地用户覆盖始终具有更高优先级。

## 监听路径与生命周期

`watch_paths` 返回的文件会加入版本扫描快照。文件修改后，宿主会自动使缓存失效并发出版本变化事件。

插件禁用、卸载或重载时，宿主会自动撤销该插件拥有的所有实例兼容来源。单个读取器抛出异常时，异常会被转换为来源警告，不会中断其他插件或整体版本扫描。

## QomicEX 系统插件

`resources/system_plugins/qomicex_compat` 是该扩展点的内置实现。它支持手动 `instances.json` 路径、`QOMICEX_HOME` 环境变量、`.qomicex-bootstrap` 引导文件与默认数据目录，并按文件签名缓存索引解析结果。
