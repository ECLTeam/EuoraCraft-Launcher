# 普通外置插件

两个插件均由 ECLTeam 维护，版本为 **v1.0.0-beta**，使用 API v2 Worker。

| 插件               | 职责                                                      | 可选权限                                       |
| ------------------ | --------------------------------------------------------- | ---------------------------------------------- |
| ECL-OnlineGame     | 建房、加入、玩家管理、端口检测、NAT、节点设置及原联机界面 | `ui.inject`、`instances.read`、`accounts.read` |
| Qomicex-Compatible | 只读 QomicEX 实例索引，提供四个 `qml:*` 协议              | `instances.read`                               |

两个插件不声明彼此依赖，可分别安装、禁用和卸载。联机页位于原来的「更多 → 联机」，仅简体中文显示，需要批准界面注入。未授权账户时使用插件设置中的玩家名称；未授权实例时仍可手动输入端口建房。

QomicEX 索引在插件设置的 `instances_index` 中由用户填写。宿主记录用户选择；Worker 不能自行更改选择，也不能请求任意文件。索引保持只读。普通插件没有原系统插件的自动读取用户目录特权。

从主仓库运行：

```powershell
python package_external_plugins.py --download
python package_external_plugins.py --install-dir ECL_data/plugins
```

下载按 `locks/*.txt` 校验版本和哈希；wheel 放在忽略的 `wheels/<目标>/`，不向主程序安装依赖。已经准备好 wheel 时省略 `--download`。产物为 `output/plugins/*.eclplugin`；安装参数只创建不存在的同名目录，不覆盖已有插件、不自动批准权限。

当前准备的目标为 Windows x86_64 CPython 3.11/3.12、Linux x86_64 CPython 3.12；原生加载与桌面验收仅在 Windows x86_64 CPython 3.11 执行，其他平台未验收。插件需要本次更新后的宿主，旧版宿主不具备新增的页面与查询接口。

联机 UI 直接迁移原 `ConnectRoomTab`、玩家列表、头像和 NAT 的模板及样式，复用宿主公共 Ui 控件和 Modal。玩家名、节点策略和自定义节点统一由宿主插件设置全屏弹窗承载，遵循[独立插件开发约束](AGENTS.md)。Florolding 源码来自原子模块固定提交，许可证保存在 `ecl_onlinegame/Florolding-LICENSE`；EasyTier 原生库仅在插件依赖目录加载。

联机扩展契约见 [插件联机协议](../docs/protocols/plugin-connector-extensions.md)。公开网络双端 Minecraft 与 QomicEX 对端实测仍待执行；Worker 测试和本地页面验收不代替该项。
