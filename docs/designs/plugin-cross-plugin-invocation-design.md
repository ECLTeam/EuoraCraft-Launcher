# 跨插件命令调用契约

日期：2026-11-01，Asia/Shanghai。状态：已实现并集成本地 `main`（发现、调用、失效），事件跨插件订阅尚未开放。

本文件补充 [Worker/API v2 设计](plugin-worker-api-v2-implementation-design.md)与[三批方案](../plans/plugin-three-batch-implementation-plan-2026-10-31.md)第二批「跨插件调用与事件」，只增加插件之间的显式调用通道，不改变授权模型与运行监管。

## 1. 准入模型

双方各自表态，缺一不可：

| 角色 | 条件 |
| --- | --- |
| 提供方 | 命令以 `exported=True` 登记，表示允许其他已启用插件调用 |
| 调用方 | 自身运行已启用、`plugin.base` 授权有效 |
| 宿主 | 两边都处于 `enabled`，且目标命令确实已导出 |

导出是提供方的显式选择，不是默认行为；未导出命令不出现在发现结果里，也无法被调用。

## 2. 方法

| 方法 | 参数 | 返回 |
| --- | --- | --- |
| `plugins.list` | 无 | `{"plugins": [{"plugin", "command", "description", "input_fields", "output_fields"}]}` |
| `plugins.call` | `{"plugin", "command", "args"}` | `{"value": 目标命令输出}` |

发现结果只含公开描述：插件名、命令名、说明与字段 schema。不含回调标识、安装路径、凭据或任何宿主对象。

## 3. 边界

- 调用在目标插件进程内执行，输入输出都按目标命令自己的 schema 校验，调用方无法绕过。
- 调用链深度上限 3，超过返回 `PLUGIN_CALL_DEPTH_EXCEEDED`，用于阻断 A→B→A 互相等待。
- 不允许调用自身，返回 `PLUGIN_TARGET_INVALID`。
- 目标未启用或已卸载时返回 `PLUGIN_TARGET_DISABLED`；旧引用不会命中新运行。
- 目标命令未导出时返回 `PLUGIN_TARGET_COMMAND_NOT_EXPORTED`。
- 超时沿用调用方剩余时间，上限 30 秒；超时按命令执行失败处理，不返回半份结果。
- 宿主未提供跨插件通道时（例如受限运行环境），两个方法都返回 `PLUGIN_API_METHOD_NOT_FOUND`。

## 4. 未开放部分

- 跨插件事件订阅仍只允许插件订阅自身 `settings.changed`；`plugins.emit` / `plugins.subscribe` 未实现，避免在未定义订阅归属与清理语义前先开口子。
- 没有「全局命令表」的广播刷新：发现按调用时实时拉取，插件重载后重新调用即可看到新集合。

## 5. 验收

`tests/test_plugin_cross_plugin.py` 覆盖：只导出可见、缺通道拒绝、转发参数不变、真实双 Worker 调用成功、目标禁用后调用被拒。示例见 `examples/plugins/worker_provider` 与 `examples/plugins/worker_caller`。
