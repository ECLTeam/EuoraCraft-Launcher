# 生命周期、SDK 与调用约定

## 生命周期

第三方基类为 `ECL.plugin_api.Plugin`，构造参数只有已绑定的 `HostClient`。通常覆盖 `on_load`、`on_enable`、`on_frontend_ready`、`on_disable`、`on_unload`；可使用同步或 async 方法。

`on_load` 注册定义，`on_enable` 完成业务启用；可信前端就绪后会调用 `on_frontend_ready`。退出回调是准备通知，不等于宿主已经结束线程、网络或进程。插件自己的资源应有明确释放路径，不依赖垃圾回收或无限等待。

生命周期有宿主时间上限。重载会创建新的 Python 进程，不保留模块全局变量、回调表或旧资源引用。不要把重载实现为再次 import 而留下旧线程。

## HostClient

```python
result = self.host.call("runtime.info", {}, timeout=5.0)
result = await self.host.call_async("runtime.info", {}, timeout=5.0)
```

调用返回 JSON 字典，明确失败抛出 `HostApiError`，通过 `error_code` 判断业务错误；协议、断线和超时可抛出 `ProtocolError`。调用方 timeout 只是等待预算，不能扩大宿主上限，也不能证明任务已经停止。

`call_async` 将 RPC 等待移到后台。取消等待协程不等于取消已发送变更。需要停止任务时使用对应任务取消接口并查询真实终态。

## 命令与回调

`define_command(name, callback, input_fields=..., output_fields=..., exported=False)` 将 Python 函数保留在 Worker，只把回调引用与字段定义交给宿主。

字段类型为 `string`、`number`、`integer`、`bool`、有界 `json`，支持必填、长度、数值范围和枚举。每组最多 32 个字段，单运行最多 64 个命令，本地回调最多 128 个；命令返回符合 schema 的字典，不能返回宿主对象或任意嵌套模型。

常用登记方法包括 `define_panel`、`define_html_slot`、`define_vue_slot`、`register_launch_hook`、`register_crash_analyzer`、`register_instance_source`、`register_auth_provider`、`define_vue_page` 和 `define_tool`。登记不代替当前宿主服务可用性检查；宿主缺少对应服务时调用明确失败。

## 跨插件命令

提供方显式使用 `exported=True`。调用方通过 `list_plugin_commands()` 发现，再用 `call_plugin_command(plugin, command, arguments)` 调用。底层为 `plugins.list/call`。

发现只返回已启用提供方的公开命令说明与字段，输入输出在目标 Worker 校验。不能调用自身、未导出命令或已经失效的目标。调用链有深度和总等待预算；不要在插件间构造相互等待。

目前没有通用跨插件事件发布/订阅。`events.subscribe` 只开放自身 `settings.changed` ；不能订阅全部宿主事件。

## 任务与幂等性

宿主长任务返回 `operation_ref`，使用 `operations.get/list/cancel`。任务接纳、请求取消、实际终态、业务效果 `effect` 和 `cleanup_pending` 分别表达，不把 `requested: true` 解释成实际停止。

受变更回执管理的方法使用运行内 `mutation_key` 去重；`mutations.get` 查询原提交的 pending/succeeded/failed。回执有容量上限且不跨运行恢复；不要保存引用到下次重载后继续调用。

资源、运行和授权身份由宿主维护。提交请求不能包含自行拼接的 PID、宿主任务编号、路径或 owner 以接管他人工作。详情见[业务接口索引](api-reference.md)。
