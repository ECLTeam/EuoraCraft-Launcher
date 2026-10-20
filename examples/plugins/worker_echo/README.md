# Worker 命令与设置示例

在启动器插件页选择“安装目录”并选中本目录。安装不会执行插件；打开权限页，允许四项必要能力并保存审阅，然后手动启用。

启用后可以修改自身 `prefix` 设置；已登记命令为 `worker_echo:greet`，参数为 `{"name":"Player"}`，结果为 `{"message":"ECL: Player"}`。命令可由可信管理接口或既有开发通道调用。开发通道不能替用户审阅权限。

在权限页撤销全部授权后，Worker 停止，命令不再可用。重新审阅后可以启用新的运行会话；修改代码后也必须重新审阅。仅支持已验收的 Windows x64 Worker。

制作归档时运行 `python packaging/build-eclplugin.py examples/plugins/worker_echo build/worker_echo.eclplugin`。本示例无第三方 Python 依赖；有依赖时使用完整锁和兼容 Wheel，见 [归档格式说明](../../../docs/eclplugin-package-format.md)。

Worker 以当前操作系统用户运行，不提供恶意 Python 的文件或网络沙箱。该示例不加载插件自定义前端。
