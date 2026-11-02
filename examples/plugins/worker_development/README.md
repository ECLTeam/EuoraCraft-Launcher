# 开发模式与统一配置示例

从本目录安装后审阅并启用。在插件页打开统一“插件设置”，选择此插件并修改问候内容；同一值也会出现在插件面板。

开启开发模式，使用“打开插件目录”，修改**实际安装目录** `main.py` 的 `code_version` 从 `v1` 为 `v2`。保存稳定后面板显示新版本，`status` 命令返回 `v2: <已保存问候>`。直接修改原导入目录不会同步。

在实际 `plugin.json` 增加 `"supports_hot_reload": false` 后继续保存 Python，当前运行不自动切换；手动重载仍可使用。改回 true 后恢复监听资格。另见 [worker_no_hot_reload](../worker_no_hot_reload)。

此插件不申请游戏、网络或前端执行权限。开发模式仅本次进程有效，关闭后停止开发 Worker，并恢复普通内容审阅。[开发手册](../../../docs/guides/plugin-development/README.md)说明授权变化、文件集、错误恢复和打包。
