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

| 参数 | 用途 |
| --- | --- |
| `--launch` | 版本 ID 或完整实例目录。版本 ID 在已配置的游戏根目录中查找；有歧义时给出完整目录或 `--game-dir`。 |
| `--game-dir` | 与版本 ID 配合，明确游戏根目录；不能与完整实例目录同时使用。 |
| `--java-path` | 本次 Java 可执行文件。 |
| `--memory` | 本次内存，512—65536 MB。 |
| `--width`、`--height` | 窗口大小，宽 320—16384、高 240—16384。 |
| `--fullscreen`、`--windowed` | 本次全屏或窗口模式，二者互斥。 |
| `--isolation` | `inherit`、`enabled` 或 `disabled`；继承模式使用实例设置和全局隔离策略。 |
| `--process-priority` | `idle`、`below_normal`、`normal`、`above_normal`、`high`。 |
| `--lock-memory`、`--no-lock-memory` | 本次是否锁定初始内存，二者互斥。 |
| `--jvm-arg`、`--game-arg` | 追加一个参数，可重复。 |
| `--launcher-visibility` | 游戏启动成功后 `none`、`minimize` 或 `quit`。 |
| `--server`、`--world` | 启动后快速进入服务器或世界，二者互斥。 |
| `--open-page` | 打开 `games`、`instances`、`download`、`settings` 或 `more`；与启动目标互斥。 |
| `--data-dir` | 本次启动器数据目录。已有实例转发只接受相同的数据目录。 |
| `--debug`、`--log-level` | 本次调试开关与日志级别。 |
| `--disable-plugins` | 本次不加载插件。 |
| `--frontend-dist`、`--dev-channel` | 开发前端地址或目录、开发者通道。 |
| `--help`、`--version` | 输出帮助或版本并退出，不启动桌面环境。 |

游戏参数需要同时提供 `--launch`。参数格式或组合无效时，命令行返回非零退出码。

开启单实例模式后，启动器已经运行时收到的快捷启动和页面请求会转发到当前窗口。初始化期间请求等待前端就绪，重复请求会合并；关闭时停止消费。已运行窗口不能重新应用插件、前端地址等进程级配置，携带此类选项的转发会被拒绝。转发成功只表示窗口接收了请求，游戏的最终启动结果在界面显示。

Windows 快捷方式位于“实例详情 → 设置 → 实例快捷方式”。可以保存到系统桌面或指定 `.lnk` 路径，使用实例当前图标和稳定 ICO 缓存，并记录实例完整目录与启动器数据目录。已有快捷方式不会自动覆盖。修改图标或移动启动器、实例后，应重新创建快捷方式。

“更多 → 联机房间 → 高级选项”可配置 EasyTier 节点：自动获取、追加自定义、仅使用自定义。每行填写一个带端口的 URI，例如 `tcp://127.0.0.1:11010`。支持 `tcp`、`udp`、`quic`、`faketcp`、`ws`、`wss`；仅使用自定义模式不回退公共节点，修改在下次创建或加入房间时生效。

“更多 → 工具 → 自定义下载”接受 HTTP(S) 地址和完整保存文件路径，复用启动器下载器与任务进度。取消、失败均保留原目标；覆盖需要显式勾选。离开页面后任务继续，回到页面可以取消或重试。任务不在重启后自动续传。
