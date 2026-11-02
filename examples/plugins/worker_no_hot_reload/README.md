# 显式拒绝热重载

清单使用严格布尔 `supports_hot_reload: false`。安装、启用并开启开发模式后，修改实际安装目录的 `code_version`，保存不会自动加载新代码。使用插件行“重新加载”才加载新版；改回 true 可恢复自动换代资格。

该声明不禁止安装或手动重载，不授予额外权限，也不允许复用旧运行引用。详见[开发模式](../../../docs/guides/plugin-development/development-mode.md)。
