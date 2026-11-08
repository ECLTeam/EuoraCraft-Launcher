# 第三方登录与联机会话扩展

状态：2026-11-01 已实现；验收与交付范围见[第二批记录](../plans/plugin-second-batch-delivery-2026-11-01.md)。本文件替代早期“仅联机状态补充”的草案，契约以当前 Worker Broker 为准。

## 登录提供方

`auth.register` 需要 `auth.provider`。提供方声明 `provider_id`、`title`、至多六个表单字段及 `authenticate_ref`、`resolve_ref`。宿主最终身份由安装标识和提供方 ID 共同组成；相同原始 ID 不允许接管另一安装的账户。

`authenticate(fields)` 只接收声明的字段，值必须为字符串且不超过 1024 字符；额外字段、缺少必填项、错误类型整体拒绝。返回账户使用现有模型：`id`、`alias`、`uuid`，可选 `skinUrl` 和 `data`，不使用旧草案的 `account_id/display_name/credentials` 结构。

`data` 是该提供方自己的私有字符串映射，至多 16 项，键长 1—64，单值不超过 16384 字符。宿主原子保存，但不通过账户摘要、日志或其他提供方接口公开。不能把 Python Worker 的进程隔离称为操作系统沙箱。

`resolve_credentials(account)` 接收该提供方当前账户及其已保存的私有 `data`，返回 `player_name`、`uuid`、`access_token`、`user_type` 和可选 `auth_server`。令牌不截断、不强制转型；未知字段及越界内容整体拒绝。回调前后校验原始许可、提供方身份和当前选中账户，失败保留账户与原凭据。原始插件异常不直接进入用户错误提示。

撤权、禁用或重载移除提供方。重新授权不会恢复旧回调；Worker 重新登记后才可使用。示例 [worker_auth](../../examples/plugins/worker_auth/README.md) 使用合成测试令牌，只验证认证契约，不提供真实第三方登录服务。

## 联机插件

联机部分已由 [ADR-003](../ADR-003-connector-and-qomicex-external-plugins.md) 替代。宿主的 `connector.*` 与对应权限已移除；外置插件通过普通跨插件命令协作，当前契约见[外置联机扩展](../protocols/plugin-connector-extensions.md)。上面的认证契约继续生效。
