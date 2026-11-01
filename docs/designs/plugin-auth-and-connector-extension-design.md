# 第三方登录与联机会话扩展

状态：2026-11-01 已实现；验收与交付范围见[第二批记录](../plans/plugin-second-batch-delivery-2026-11-01.md)。本文件替代早期“仅联机状态补充”的草案，契约以当前 Worker Broker 为准。

## 登录提供方

`auth.register` 需要 `auth.provider`。提供方声明 `provider_id`、`title`、至多六个表单字段及 `authenticate_ref`、`resolve_ref`。宿主最终身份由安装标识和提供方 ID 共同组成；相同原始 ID 不允许接管另一安装的账户。

`authenticate(fields)` 只接收声明的字段，值必须为字符串且不超过 1024 字符；额外字段、缺少必填项、错误类型整体拒绝。返回账户使用现有模型：`id`、`alias`、`uuid`，可选 `skinUrl` 和 `data`，不使用旧草案的 `account_id/display_name/credentials` 结构。

`data` 是该提供方自己的私有字符串映射，至多 16 项，键长 1—64，单值不超过 16384 字符。宿主原子保存，但不通过账户摘要、日志或其他提供方接口公开。不能把 Python Worker 的进程隔离称为操作系统沙箱。

`resolve_credentials(account)` 接收该提供方当前账户及其已保存的私有 `data`，返回 `player_name`、`uuid`、`access_token`、`user_type` 和可选 `auth_server`。令牌不截断、不强制转型；未知字段及越界内容整体拒绝。回调前后校验原始许可、提供方身份和当前选中账户，失败保留账户与原凭据。原始插件异常不直接进入用户错误提示。

撤权、禁用或重载移除提供方。重新授权不会恢复旧回调；Worker 重新登记后才可使用。示例 [worker_auth](../../examples/plugins/worker_auth/README.md) 使用合成测试令牌，只验证认证契约，不提供真实第三方登录服务。

## 联机会话

`connector.extend` 仍只允许 `connector.register_status` 有界状态补充。新增独立的 `connector.manage` 允许下列操作，不能从基本启用或状态补充推导建房权限。

| 方法 | 参数与结果 |
| --- | --- |
| `connector.open` | `port` 或 `code` 二选一；返回自己的 `session_ref`、`operation_ref` 和 queued 状态 |
| `connector.get` | 传自身 `session_ref`；返回最小模式、房间码、本地端口及清理状态，不返回玩家、节点或机器标识 |
| `connector.leave` | 请求取消该会话任务；`requested` 不表示实际退出已经完成 |
| `connector.protocols.register` | `name` 和 `callback_ref`，每次运行至多八个；返回随机 `protocol_ref` |
| `connector.request` | 自身会话和协议引用、JSON `payload`；仅房客向房主请求，有界 JSON 返回 `value` |
| `connector.status.set` | 自身会话的有界文字，不能覆盖宿主字段 |

会话归属于安装、代码内容、运行及服务代次。应用只维护一个联机服务；用户已有房间时新插件请求明确拒绝。取消、撤权、崩溃和退出都清理原会话，不允许迟到清理关闭后来创建的用户房间。实际停止失败保留句柄和 `cleanup_pending`，供后续重试；任务在房间存续期间持续运行，并可通过可信任务界面取消。

私有协议按清单名称的哈希隔离，不接受插件自报来源；请求和响应均不超过 16 KiB。远程回调只接收 JSON 业务载荷，调用前后复核原许可和会话。扩展只对本来源房间参与协商，未协商的协议由底层拒绝。

`events.subscribe` 可订阅 `connector.changed`，事件只向所属 Worker 的有效订阅排队。`ready`、`joined`、`leaving`、`reset`、`closed` 表示不同阶段，关闭通知不能替代任务的实际最终状态。示例见 [worker_connector](../../examples/plugins/worker_connector/README.md)。

## 验收

回归包含：严格表单与返回模型、真实 Worker 登录及重启、撤权再授权不复活旧回调、私有材料不进入摘要；会话去重、用户房间保护、异源引用拒绝、启动期间撤权、清理失败后重试、旧代次隔离、有界私有协议与任务取消。实际网络和发行物的结果单独记录，不用服务替身测试代替真实联机验收。
