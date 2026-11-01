# Linux/macOS 文件事务与自定义窗口实施细化

用户于 2026-11-01 明确要求先完成这两项。本文件细化已批准第三批范围，继续使用 `codex/plugin-posix-worker` 隔离工作树，不重复询问实施。Nuitka 和关机继续取消；不实现商店、签名分发或额外产品能力。

## 文件边界

当前调用方为私有文件与下载、实例资料、游戏资源、整合包和安装目标服务。抽取共同的错误及物理身份模型，增加统一的平台入口；Windows 保留原有 NTFS 实现，POSIX 采用独立后端，不修改子模块内部风格。

- 打开根目录逐组件拒绝链接，保留祖先描述符及绑定；每次操作核对绑定，目录替换后拒绝继续。文件相对描述符打开，验证普通、单链接、同设备对象；不跟随链接或回退路径打开。
- Linux 身份使用设备、inode 和 statx 出生时间；macOS 使用设备、inode 和出生时间。没有可靠出生时间或不是允许的本地文件系统时明确拒绝，不用会随写入变化的 ctime 冒充创建身份。
- 新建发布使用内核不覆盖重命名，替换沿用已核对版本后的原子发布；写入及目录变化刷新后才确认结果。进程内领域锁和文件锁覆盖合作调用，并保持不是恶意 Python 的 OS 沙箱这一边界。
- POSIX 删除标记在本对象内维护，实际关闭前不删除。关闭时将对象移入独占、私有隔离目录，验证物理身份后才移除。写前记录原名称与目录/文件身份；失败可不覆盖恢复，不能清理被替换的未知对象。
- 中断恢复只处理本实现的严格事务记录，安全时恢复尚存对象，不恢复旧 Worker、授权或业务任务；身份不符、名称被占或记录损坏时保留全部数据并明确待恢复。不能引入扫描/删除用户历史文件的后台流程。
- 删除已发生但后续元数据清理失败时必须保留实际效果和重试责任，不误报原文件仍保留。取消、撤权和重复提交仍由原 OperationManager 与原始租约控制。

## 原生窗口

继续维护固定 PyTauri/Tauri/Wry 源码及窄原生扩展，不更换宿主框架，不另建浏览器宿主。原生策略只适用于专用插件窗口，普通主窗口保持现有路径。

- Linux 在插件语句前配置独立 WebKitGTK 数据上下文、原生网络拒绝、WebRTC 禁止、导航/下载/权限/文件选择控制及顶层专用消息桥。
- macOS 核对 WKWebView 的公开可用接口，独立数据存储、原生网络和来源边界须达到原有要求；不把 CSP 或 JavaScript 包装冒充底层拦截。
- 更新构建入口、固定来源和锁摘要；只在独立构建/验收目录安装原生 Wheel，不修改全局环境。明确实际 OS、处理器、WebKit 与 Wheel 版本。
- 缺少必需控制时保持拒绝。真实浏览器验收须同时有合法回传和 HTTP/WebSocket/UDP canary 零请求，并验证嵌套 frame、worker、导航、撤权、关闭及重开。

## 顺序与验收

先文件模型和后端，再接入实际领域及 Worker 回归，随后完成两种原生窗口的控制与构建。开发中运行定向回归，涉及共享安全/持久化和原生构建的最终交付执行受影响仓库完整检查。真实 Linux/macOS 文件和 GUI 门禁使用对应 runner，必需测试为空、失败或跳过不得通过。源码、真实 GUI 和 PyInstaller 分别记录，不运行 Nuitka。

用户已明确授权本轮文件事务与跨平台窗口在 `codex/plugin-posix-worker` 分支持续推送、运行三平台 CI 并修复失败；不推送默认分支，不发布版本。

## 文件事务验收记录

实现提交 `908e59a`，平台集成修正 `eba689a`。完整本地回归 2702 项通过；后续目录身份和 macOS ARM64 可变参数调用修正的相关回归 46 项通过。真实三平台运行 [41284878230](https://github.com/ECLTeam/EuoraCraft-Launcher/actions/runs/41284878230) 全部成功：三个平台各 128 项文件领域和真实源码 Worker 测试；Linux/macOS 各 10 项原生文件测试，Windows 9 项原生文件测试。必需覆盖没有跳过；源码进程与传输门禁也保持通过。

补齐实际文件调用方后的 [41286035439](https://github.com/ECLTeam/EuoraCraft-Launcher/actions/runs/41286035439) 三平台继续全绿：各 191 项共享事务、游戏资源、安装、整合包与真实 Worker 用例通过，另有上述原生文件用例。移除了已失效的 POSIX 一律拒绝预期；不支持文件系统的拒绝、目录换绑和链接越界仍有明确回归。加入窗口适配后的本地完整回归 2709 项通过、61 项按环境跳过，后续测试范围调整的相关 63 项通过。

当前文件事务验收以 [41311269611](https://github.com/ECLTeam/EuoraCraft-Launcher/actions/runs/41311269611) 为准，三平台全部成功：每平台 191 项文件领域及真实 Worker 用例，Linux/macOS 各 10 项原生文件用例、Windows 9 项；每平台另有 139 项源码共享用例，Linux/macOS 各 11 项原生监管与传输用例、Windows 5 项。必需测试均非空、无失败和跳过。冻结产物用例不计入源码门禁。

本轮采用平台、安全、持久化及构建边界变更的全量验证。在隔离工作树合并预演当前本地 `main`（`5a65176`）与功能提交 `be1e673`，Ruff 检查和格式检查通过，完整 pytest 为 2731 项通过、59 项按环境跳过；必需的 POSIX 与 GUI 用例由上述原生 runner 单独验收，未用本机跳过项充当证据。

## 原生窗口验收记录

- Linux WebKitGTK 2.52.6 已在 [41286814282](https://github.com/ECLTeam/EuoraCraft-Launcher/actions/runs/41286814282) 通过 3 项真实 GUI 门禁，包含合法回传、服务端 HTTP/UDP 零请求、通用 IPC 拒绝和 Worker 撤权、关闭、重开；原始报告见该运行的 `plugin-native-ubuntu-latest` 产物。
- Windows 本机 WebView2 156.0.4314.58 使用 CI 构建的 `0.8.0+ecl.2` Wheel，在独立目录通过 4 项真实 GUI 门禁。合法主窗口 IPC 与插件桥均成功；HTTP/UDP 到达数为零，撤销 CDP 网络拒绝后仍由固定代理阻断；Worker 回传、撤权关闭与重新打开通过。本地报告位于忽略目录 `build/native-win-acceptance/`。Wheel 来自运行 `41285850953`，Windows 策略和 runtime.patch 与后续受验源码相同；不把该 Wheel 当作后续 macOS 策略的验收产物。
- macOS ARM64 在 [41311269541](https://github.com/ECLTeam/EuoraCraft-Launcher/actions/runs/41311269541) 通过 3 项真实 GUI 门禁。实际 WKWebView build 为 `21624.5.1.11.3`，Wheel 为 `0.8.0+ecl.2`；合法插件回传、主窗口 IPC 对照、嵌套 frame 与 worker 均实际运行，HTTP 请求与 UDP 包为零，通用桥旁路为零。真实 Worker 的消息往返、撤权关闭、重新打开及关闭均成功，原始报告保存在该运行的 `plugin-native-macos-latest` 产物。Linux 同一运行也通过 3 项必需 GUI 用例。
- 同一运行的 Windows x64 通过 4 项必需 GUI 用例，三平台最终均成功。Windows 使用微软固定 WebView2 `156.0.4314.58`；Linux x86_64 使用 WebKitGTK `2.52.6`。三个平台均从本轮固定源码构建原生 Wheel，报告、二进制摘要及来源清单随该运行产物保留。
- macOS 关闭 PeerConnection 采用固定 WebKit build 的专用偏好接口，原因及版本不符时的明确拒绝见[原生运行时说明](../../native/pytauri-runtime/README.md)。这些证据只覆盖实际 runner 的处理器、引擎和源码路径，不扩大为其他引擎版本、处理器或冻结发行物通过。

本轮要求的 Linux/macOS 文件事务与三平台自定义窗口已经完成。未构建新的 PyInstaller 发行物，也未运行 Nuitka、关机、推送默认分支或发布版本；第三批在线发现、签名和分发闭环继续按总方案处理。[#98](https://github.com/ECLTeam/EuoraCraft-Launcher/issues/98) 与 [#99](https://github.com/ECLTeam/EuoraCraft-Launcher/issues/99) 的修复已在远端功能分支验收，随后按用户明确要求关闭并回读确认；尚未合入远端默认分支，关闭不表示已发布。

### 通信与门禁时序回归

真实 GUI 验收确认 WKWebView 的代理委托须保留固定 Wry 的 commit/finish 生命周期，否则其宿主 eval 队列无法释放；仅转发精确宿主文档的生命周期，不转发导航或权限决策。

另已独立复现 [#99](https://github.com/ECLTeam/EuoraCraft-Launcher/issues/99)：POSIX 接收线程在可读通知后被延迟时，会将已经完整到达的消息误判为超时。修正为先非阻塞读取已到达字节，只有等待缺失字节才检查原截止；消息上限、同一等待预算和真正半帧超时终止均保留。使用真实流 socket 和调度时序回归验证前失败后通过，并在三平台源码门禁运行；不将尚未定位的其他通信中断直接归因于此。

Windows DevTools 端口文件存在到可读取之间有短暂共享锁窗口；门禁仅对文件缺失、共享锁及未完整发布做有界等待，不重试浏览器拒绝或忽略失败结果。
