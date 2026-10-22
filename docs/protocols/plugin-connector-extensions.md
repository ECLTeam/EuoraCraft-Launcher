# 联机扩展协议插件开发

ECL 的联机核心只实现通用 Scaffolding/Florolding 与 EasyTier 会话。启动器特有的协议应由插件通过
`Plugin.register_connector_extension()` 注册，避免把 `qml:*` 等第三方语义写死在宿主中。

## 清单与权限

普通插件需要声明写入联机扩展的权限；系统插件跳过权限校验，但仍使用同一注册接口。

```json
{
  "permissions": [
    { "scope": "connector", "action": "write", "resource": "example" }
  ],
  "contributes": {
    "connectorExtensions": [
      {
        "name": "example",
        "protocols": ["example:metadata"]
      }
    ]
  }
}
```

`contributes.connectorExtensions` 用于描述能力，实际处理器仍须在 `on_enable()` 中注册。插件禁用、
卸载或启用失败时，宿主会自动移除它拥有的全部联机扩展。

## 注册协议

```python
from ECL.plugins import ConnectorProtocolResponse, Plugin


class ExamplePlugin(Plugin):
    def on_enable(self) -> None:
        super().on_enable()
        self.register_connector_extension(
            "example",
            {"example:metadata": self.metadata},
            on_guest_joined=self.on_guest_joined,
            enrich_status=self.enrich_status,
            before_leave=self.before_leave,
            on_reset=self.on_reset,
        )

    def metadata(self, request):
        payload = request.json({})
        return ConnectorProtocolResponse.json({"received": payload})
```

协议名必须符合 `namespace:action`，仅使用小写 ASCII 字母、数字和下划线。处理器可同步或异步，
返回值支持：

- `ConnectorProtocolResponse`
- `tuple[int, bytes]`
- `bytes` 或 `str`（状态码为 0）
- JSON 可序列化值（自动编码为 UTF-8 JSON）

处理器收到 `ConnectorProtocolRequest`，可读取原始 `body`、调用 `json()`、读取
`peer_machine_id` 与房主 `game_info`。优雅离房等协议可以 `await request.remove_player(machine_id)`。
单个处理器抛出的异常会被隔离，并转换为状态码 255，不会终止联机服务。

## 房客会话钩子

`on_guest_joined`、`enrich_status`、`before_leave` 和 `on_reset` 接收
`ConnectorSessionContext`。房客可以使用：

```python
data = context.request_json("example:metadata", {"key": "value"})
```

`enrich_status(context, status)` 可以原位补充状态，也可以返回要合并的字典。会话钩子的异常会被
隔离；网络请求应自行容忍房主不支持对应扩展的情况，以保持不同启动器之间的基础联机兼容。

每次创建或加入房间时，宿主会对当时已注册的协议做一次协商快照。因此运行中新增协议会从下一个
房间开始生效。

## 内置 QomicEX 兼容

隐藏且不可禁用的 `qomicex-compat` 系统插件实现了 QomicEX 联机系统的四个扩展协议：

- `qml:game_info`：房主游戏版本与加载器信息
- `qml:player_icons`：玩家头像上传与映射交换
- `qml:player_leave`：房客优雅退出通知
- `qml:game_mods`：房主模组来源、项目 ID 与 SHA-1 清单

ECL 作为房主会响应这些协议；作为房客会在加入、状态刷新与退出阶段主动调用它们。对端不支持
任一扩展时，基础建房、加入和 Minecraft 端口映射仍可继续工作。
