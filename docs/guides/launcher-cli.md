# 启动器命令行与实例快捷启动

所有参数只影响本次运行，不写入全局或实例设置。游戏设置按“全局设置 → 实例独立设置 → 本次显式参数”的顺序生效；JVM 和游戏追加参数按此顺序保留。

Windows 安装版示例：

```powershell
& '.\EuoraCraft Launcher.exe' --launch 'D:\我的游戏\versions\1.21.1'
& '.\EuoraCraft Launcher.exe' --launch '1.21.1' --game-dir 'D:\我的游戏'
& '.\EuoraCraft Launcher.exe' --launch '1.21.1' --memory 6144 --windowed --width 1280 --height 720
& '.\EuoraCraft Launcher.exe' --launch '1.21.1' --java-path 'C:\Program Files\Java\bin\javaw.exe'
& '.\EuoraCraft Launcher.exe' --launch '1.21.1' --jvm-arg=-Dexample=true --jvm-arg=-Dother=value --game-arg=--demo
& '.\EuoraCraft Launcher.exe' --launch '1.21.1' --server 'example.com:25565'
& '.\EuoraCraft Launcher.exe' --launch '1.21.1' --world '我的存档'
& '.\EuoraCraft Launcher.exe' --open-page download
```

源码运行时将可执行文件替换为 `python main.py`。中文或包含空格的路径应加引号；值以 `-` 开头的追加参数使用 `--jvm-arg=值`、`--game-arg=值`。

| 参数                                | 用途                                                                                                         |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `--launch`                          | 版本 ID 或完整实例目录。版本 ID 在已配置的游戏根目录中查找；有歧义时给出完整目录或 `--game-dir`。            |
| `--account`                         | 为本次启动指定已有账户 ID，不改变默认账户；账户不存在时失败。                                                |
| `--start-minimized`                 | 新启动器窗口首次打开时最小化；已有窗口不能通过第二次启动修改此设置。                                         |
| `--language`                        | 本次界面语言：`zh-CN`、`zh-TW`、`en-US`、`ja-JP`、`ru-RU`、`de-DE`；不保存，也不改变游戏初始语言。           |
| `--game-dir`                        | 与版本 ID 配合，明确游戏根目录；不能与完整实例目录同时使用。                                                 |
| `--java-path`                       | 本次 Java 可执行文件。                                                                                       |
| `--memory`                          | 本次内存，512—65536 MB。                                                                                     |
| `--width`、`--height`               | 窗口大小，宽 320—16384、高 240—16384。                                                                       |
| `--fullscreen`、`--windowed`        | 本次全屏或窗口模式，二者互斥。                                                                               |
| `--isolation`                       | `inherit`、`enabled` 或 `disabled`；继承模式使用实例设置和全局隔离策略。                                     |
| `--process-priority`                | `idle`、`below_normal`、`normal`、`above_normal`、`high`。                                                   |
| `--lock-memory`、`--no-lock-memory` | 本次是否锁定初始内存，二者互斥。                                                                             |
| `--jvm-arg`、`--game-arg`           | 追加一个参数，可重复。                                                                                       |
| `--launcher-visibility`             | 游戏启动成功后 `none`、`minimize` 或 `quit`。                                                                |
| `--server`、`--world`               | 启动后快速进入服务器或世界，二者互斥。                                                                       |
| `--open-page`                       | 打开 `games`、`instances`、`download`、`settings`、`more`、`accounts`、`java` 或 `plugins`；与启动目标互斥。 |
| `--data-dir`                        | 本次启动器数据目录。已有实例转发只接受相同的数据目录。                                                       |
| `--debug`、`--log-level`            | 本次调试开关与日志级别。                                                                                     |
| `--disable-plugins`                 | 本次不加载插件。                                                                                             |
| `--frontend-dist`、`--dev-channel`  | 开发前端地址或目录、开发者通道。                                                                             |
| `--help`、`--version`               | 输出帮助或版本并退出，不启动桌面环境。                                                                       |

游戏参数需要同时提供 `--launch`。参数格式或组合无效时，命令行返回非零退出码。

长参数必须完整拼写，不接受缩写。`--account` 只绑定这一条启动请求，不临时切换全局账户；认证刷新仍可能按既有账户服务规则保存新凭据。失效或不存在的指定账户不会回退默认账户。

`--language` 单独覆盖界面运行语言，设置页保留原来的持久语言。即使保存其他设置，本次覆盖也不会写入文件；主动保存的新语言在下次不带参数启动时生效。`--start-minimized` 控制启动器初始状态，`--launcher-visibility` 控制游戏启动成功后的行为。

`--open-page accounts` 打开游戏页现有的账户全屏管理弹窗；`java` 打开 Java 设置，`plugins` 打开插件管理。重复账户请求不会创建多个弹窗。

启动时，如果游戏数据目录中的 `options.txt` 不存在、缺少 `lang` 键或其值为空，启动器会使用当前界面语言初始化游戏语言。已有语言选择始终保留，包括英文；之后修改启动器语言不会覆盖游戏内的选择。该行为同样适用于界面启动和快捷启动，并使用版本隔离设置确定的游戏数据目录。

语言代码按实际原版版本确定；无法可靠识别版本时跳过语言初始化并记录原因。读取游戏选项失败或文件编码损坏时保留原文件，跳过本次语言与全屏设置更新；原子写入失败时也保留原文件并告警。

开启单实例模式后，启动器已经运行时收到的快捷启动和页面请求会转发到当前窗口。初始化期间请求等待前端就绪，重复请求会合并；关闭时停止消费。已运行窗口不能重新应用插件、前端地址等进程级配置，携带此类选项的转发会被拒绝。转发成功只表示窗口接收了请求，游戏的最终启动结果在界面显示。

Windows 快捷方式位于“实例详情 → 设置 → 实例快捷方式”。可以保存到系统桌面或指定 `.lnk` 路径，使用实例当前图标和稳定 ICO 缓存，并记录实例完整目录与启动器数据目录。已有快捷方式不会自动覆盖。修改图标或移动启动器、实例后，应重新创建快捷方式。

“更多 → 联机房间 → 高级选项”可配置 EasyTier 节点：自动获取、追加自定义、仅使用自定义。每行填写一个带端口的 URI，例如 `tcp://127.0.0.1:11010`。支持 `tcp`、`udp`、`quic`、`faketcp`、`ws`、`wss`；仅使用自定义模式不回退公共节点，修改在下次创建或加入房间时生效。

“更多 → 工具 → 自定义下载”接受 HTTP(S) 地址和完整保存文件路径，复用启动器下载器与任务进度。取消、失败均保留原目标；覆盖需要显式勾选。离开页面后任务继续，回到页面可以取消或重试。任务不在重启后自动续传。

## 本次定向禁用插件

使用 `--disable-plugin <插件ID>` 可在本次运行中禁止指定插件装载和启用，重复指定可停用多个插件。同一 ID 自动去重，未知 ID 明确失败。该限制同时覆盖手动启用、重载、Worker 重启和开发热重载；需要调整参数并重启才能解除。

必要依赖被停用的插件一并阻断，可选依赖不传播限制。列表显示本次停用或依赖阻断的原因。参数不写入持久禁用偏好、不撤销授权；去掉参数重启后按既有偏好和内容审阅规则运行。与 `--disable-plugins` 联用不会恢复插件；已有进程拒绝通过第二次启动修改该参数。

## 插件开发模式

桌面启动支持 `--plugin-dev` 和 `--no-plugin-dev`，二者互斥；前者开启目录插件热重载，后者显式关闭。未指定时读取 `setting.json` 中可选的 `plugins.development_mode`，省略默认关闭。该字段只接受 JSON 布尔值，首次生成配置无需包含它。

命令行覆盖文件配置且不落盘。`--disable-plugins` 优先，不启动开发监听。文件配置改变后重启生效，运行中不能从插件页切换模式；第二次启动不能改变已有进程的开发模式。这两个参数不支持无头任务，与调试模式及开发者通道独立。

例如 `python main.py --plugin-dev --data-dir <独立测试目录>`。开发模式不自动批准插件，也不恢复上次的临时开发信任；完整流程见[开发模式与热重载](plugin-development/development-mode.md)。
