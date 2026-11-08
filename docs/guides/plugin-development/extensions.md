# 扩展开发

所有回调在 Worker 执行，只传有界 JSON。注册前检查权限，回调前后复核原始许可；禁用、重载、卸载和会话结束使旧注册失效。

## 启动钩子

```python
self.register_launch_hook("prepare", self.prepare, required=True)

def prepare(self, version_id, loader, version_isolation):
    return {"jvm_args": ["-Dexample.feature=true"], "game_args": [], "env": {}}
```

阶段为 prepare、pre_launch、post_launch、exit，当前第三方注册使用已批准的游戏管理范围 `instances.manage`；不要声明旧注释中的独立 `launch.hooks` 权限名。

上下文只有版本、加载器与隔离标记。prepare 可以按宿主数量、长度及字段规则补充 JVM/游戏参数和环境变量；其他阶段不提供同样的修改通道。必要前置回调失败可阻止本次启动，启动后/退出失败仅记录。

参考 [worker_launch_hook](../../../examples/plugins/worker_launch_hook)。

## 崩溃分析

`register_crash_analyzer(callback)` 需要实例查看或管理范围。回调参数包含 report_id、version_id、exit_code、detected_by、reasons 和最多 8000 字符 output，不包含任意文件路径。

返回 `{summary?, reasons?}`，原因只接纳 title、detail、severity、source，最多 16 项，每段文本受限。它增强现有分析，不直接替换整个报告；某插件失败不会中止其他分析器。

参考 [worker_crash_analyzer](../../../examples/plugins/worker_crash_analyzer)。

## 实例识别来源

`register_instance_source(source, title, callback)` 需要实例查看或管理范围。宿主只对获准、已索引实例投递 root_ref、instance_rel、version_id、vanilla_name、primary_loader 等有界上下文；返回 `{fields: {键: 字符串}, warnings?: [...]}`，字段数量和文本长度受限。

该接口不等于可信系统插件的带 Path 上下文和监听文件解析器的接口，也不授予任意第三方配置文件读取权限。不要把 `ECL.plugins.ExternalInstanceMetadata` 的内部示例直接复制到 Worker。

参考 [worker_instance_source](../../../examples/plugins/worker_instance_source)。

## 受管工具

`define_tool(name, title, executable, args=())` 声明包内可执行文件。`run_tool(name, args=())` 返回 process_ref，`stop_tool(process_ref)` 只控制本插件所属工具。

声明和运行基于基础启用，不额外制造不存在的权限项。可执行文件必须在包内，路径、参数数量和文本长度有边界；没有 shell 字符串解释器或任意系统程序选择。不同平台需提供实际兼容二进制并验证退出行为。

宿主未接入通用进程服务时返回 `PLUGIN_API_METHOD_NOT_FOUND`。工具进程与游戏进程是不同来源，不能用工具引用停止游戏或其他插件的进程。

## 第三方登录

使用 `register_auth_provider(provider_id, title, fields, authenticate, resolve_credentials, description=...)`，声明 `auth.provider`。

表单最多六个 text/password 字段。`authenticate(fields)` 只收到声明字段，返回 id、alias、uuid，可选 skinUrl 和 data。data 是本提供方自己的有界字符串映射，由宿主分区保存，不进入公开账户摘要。

`resolve_credentials(account)` 在启动时收到自身账户与已保存 data，返回 player_name、uuid、access_token、user_type，可选 auth_server。不能伪造其他安装的提供方或读取其他账户凭据；撤权保留用户文件但停止旧回调。

[worker_auth](../../../examples/plugins/worker_auth/README.md) 使用合成令牌，只验证协议，不代表外部认证服务已实现。精确字段与限制见[认证/联机契约](../../designs/plugin-auth-and-connector-extension-design.md)。

## 外置联机与兼容协议

宿主不再维护联机网络或提供 `connector.*`。普通插件 ECL-OnlineGame 自己持有网络资源，Qomicex-Compatible 可选提供兼容协议，二者均可独立禁用和卸载。

通过已导出的 `onlinegame.extension.v1` 普通跨插件命令接入，详见[联机协议](../../protocols/plugin-connector-extensions.md)及[外置插件说明](../../../external_plugins/README.md)。
