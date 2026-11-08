# 外置联机插件的扩展协议

联机网络由普通 Worker 插件 **ECL-Plugin-OnlineGame** 持有。宿主不再提供 `connector.*` 接口、联机权限或系统级协议注册。可选兼容插件通过普通的导出命令接入，不需要成为联机插件的清单依赖。

## 发现与运行绑定

提供方导出 `onlinegame.extension.v1`，输入、输出分别为有界 JSON 字段 `payload`、`result`。联机插件发现已启用且导出该命令的插件，发送 `{"event":"describe"}`。提供方返回：

```json
{"name":"example","version":1,"binding":"本次 Worker 的随机标识","protocols":["example:metadata"]}
```

其余请求携带原 `binding` 和本房间随机 `session`。重新启用后的提供方拒绝旧绑定。提供方退出时，联机插件移除它的协议；缺少任何可选提供方均不阻止基础建房和加入。

## 请求与生命周期

事件包括 `protocol`、`joined`、`enrich`、`leaving`、`reset`。网络请求由联机插件投影为协议名、对端机器标识、JSON 请求和必要的房间公开上下文。提供方只返回数据及后续请求描述，不在同步回调中反向等待联机插件。

普通跨插件命令维持原 16 KiB 校验；大头像和列表使用绑定同一运行、房间的分块传输，每块 8192 字节，单个完整对象最多 1 MiB。临时传输有总容量、引用数量和 30 秒过期限制。具体字段以插件仓库源码为准：[remote_extensions.py](https://github.com/ECLTeam/ECL-Plugin-OnlineGame/blob/main/onlinegame/remote_extensions.py) 与 [main.py](https://github.com/ECLTeam/ECL-Plugin-QomicexCompatible/blob/main/main.py)。

## QomicEX 兼容

普通插件 **ECL-Plugin-QomicexCompatible** 提供 `qml:game_info`、`qml:player_icons`、`qml:player_leave`、`qml:game_mods`。它同时提供独立的 QomicEX 实例来源；停用联机插件不影响实例来源。退出协议核对真实对端身份，不能代替其他玩家退出。

两个插件均为 ECLTeam 的 v1.0.0-beta，安装、锁定依赖和可选权限见各自仓库说明。本地 Worker 和页面验收不等于公开网络及真实 QomicEX 对端验收。
