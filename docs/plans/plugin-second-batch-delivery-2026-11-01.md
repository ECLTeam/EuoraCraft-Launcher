# 插件第二批交付与验收

日期：2026-11-01，Asia/Shanghai。范围为三批方案中的第二批；用户已明确授权完整实施、提交和推送；后续明确取消关机，交付结束后保持开机。本记录不表示第三批跨平台和在线分发已经完成，也不创建版本发布。

## 实现结果

已有声明式面板、主窗口注入及总开关、跨插件命令、实例识别、启动/退出钩子、崩溃分析与受管工具进程继续沿用当前实现。本次补齐最后三条业务闭环：

- 原生独立窗口：代码快照内单文件 HTML、独立浏览器目录、先安装原生控制再加载、专用桥、严格白名单、变更去重，以及撤权、关闭和 Worker 停止时的实际清理。
- 第三方登录：修复提供方注册接线和重启后自身私有数据交付；严格表单和凭据校验、安装身份隔离、回调前后复核及失权注册移除。解决 [#83](https://github.com/ECLTeam/EuoraCraft-Launcher/issues/83)。
- 所属联机：应用唯一联机服务维护来源与代次，插件只能创建、加入、查询和退出自己会话；私有 JSON 协议和所属事件有界，实际清理失败保留句柄。真实联机验收发现并修复绑定游戏端口破坏控制转发的问题，见 [#91](https://github.com/ECLTeam/EuoraCraft-Launcher/issues/91)。

前端补齐能力类型、授权审阅与六语言说明，并把活动插件房间纳入任务中心。三个示例 `worker_custom_ui`、`worker_auth`、`worker_connector` 可按现有目录或归档流程安装。认证示例使用合成令牌，不提供外部认证服务；联机示例不代启动 Minecraft。

## 真实验收

| 范围 | 实际证据 |
| --- | --- |
| 原生运行时 | 从固定 PyTauri `734d5a1c7834935abdd39c85b53bf990a563febe` 完整 workspace、锁文件与仓库补丁独立重建；Rust 1.99.0，Python 3.11，维护版 0.8.0+ecl.1 |
| 浏览器边界 | `tests/test_plugin_native_isolation.py`：真实 WebView2；HTTP/WS 与 worker、嵌套 frame、WebRTC UDP 的独立接收端为零；允许回传成功；主动取消 CDP 网络阻断后原生拒绝仍生效；全局配置覆盖和缺失保护明确拒绝 |
| 独立窗口生命周期 | `tests/test_plugin_custom_ui.py` 与真实 Worker：命令/设置往返、父文档和越权 API 拒绝、撤权实际关窗且 Worker 保持运行、重新授权后新引用、关闭实际完成；与原生门禁合计 15 项通过 |
| 账户 | `tests/test_plugin_auth_worker_roundtrip.py`：真实 Worker 登录、保留完整自身数据、重启解析、失权后旧回调拒绝、重登记恢复；错误类型、多余与超长字段整体拒绝 |
| 联机 | `ECL_CONNECTOR_NETWORK_GATE=1` 的两个真实 Worker，经实际本地 EasyTier 中继建房、加入、协商 JSON 回声、完成请求和确认退出；不替换房间操作、客户端、服务端或节点。此项不是公共网络或真实 Minecraft 联机验收 |
| 启动器界面 | 实际启动器加载生产前端，Classic/Folia × 亮暗 × 960×600、1200×800、1440×1000 共 12 种布局；另检查六语言最小窗口、透明度 60% 与模糊、键盘 Space 授权、启用、打开独立窗口及禁用，未发现横向溢出和操作区域裁切 |

本机详细报告位于忽略目录 `build/`：`plugin-native-final-tests.log`、`plugin-runtime-final/native-gate.json`、`plugin-connector-network.log`、`plugin-final-targeted-tests.log`、`plugin-final-regressions.log`，以及 `output/playwright/plugin-batch2/result.json` 和截图。该目录是本次本机证据，不作为远端已运行结果。

## 静态检查与回归

本次修改涉及 IPC、授权、账户、进程/窗口和共享服务，开发期间已执行后端全量：2683 passed、38 skipped；随后对实际修改与直接消费方补充回归，243 passed、6 skipped，最终收尾回归 196 passed、5 skipped。跳过项主要是显式冻结/原生门禁及平台专属用例，另行执行的结果单独列出。最初全量中发现命令行测试按 GBK 解码 UTF-8 的线程警告，已为该测试显式设置 UTF-8 并通过回归。

主仓库 `python -m ruff check .`、`python -m ruff check ECL tests`、格式检查通过。Florolding 只修改端口转发列表，按其既有格式保留；定向静态检查及主仓库默认回归、真实网络集成均通过，不借此整理子仓库其他代码。

前端 `pnpm check` 全量通过（1164 passed、1 todo），`pnpm build` 通过；新增授权测试及解析测试最终 41 passed，改动文件 Prettier/ESLint 通过。后续只调整六语言任务标题，使用定向检查，并完成最新前端构建，不重复运行相同代码的全量测试。远端普通质量检查、前端构建和 Showcase 构建通过；治理任务另发现 pnpm/action-setup 重复指定版本，已移除重复配置，见 [#94](https://github.com/ECLTeam/EuoraCraft-Launcher/issues/94)，工具安装阶段还发现 ubuntu-latest 已变为 Playwright 1.58.0 不支持的 Ubuntu 26.04，已把前端治理任务固定为 Ubuntu 24.04；最新报告的生产依赖 285 项、完整依赖 814 项均无公告命中；31 个界面场景没有确认违规。已查看八张变化场景截图并复核 12 个未决节点，清理失配旧定位，见[可访问性复核](../governance/accessibility-review-2026-11-01.md)；三个报告门禁均通过，未放宽失败条件。

## 冻结与平台范围

PyInstaller 完整启动器构建成功；使用该实际可执行文件作为 Worker 运行独立窗口调用及文件事务回归 14 passed，认证与真实联机 3 passed。冻结主窗口另通过与源码相同的 12 组主题/尺寸及六语言矩阵、授权打开和禁用流程，且既有账户文件指纹保持不变。原生门禁使用本次自行构建的维护版 Wheel，不复用其他工作树的预编译 Wheel。

Nuitka standalone 曾构建成功，其显式冻结 Worker 回归 17 passed；主窗口启用流程未完成验收。正式 onefile 构建遇到编译器内存不足，降低并行度重试期间，用户明确要求不再构建 Nuitka，已停止该构建及后续 Nuitka 验收。本次不将 Nuitka onefile 或主窗口标记为通过。用户取消该项验收后，按已通过的源码、真实原生运行时与 PyInstaller 结果交付第二批。源码、冻结 Worker、冻结主窗口和远端 CI 分别记录，不能互相代替。

当前第三方 Worker 与独立窗口支持范围仍为 Windows x64。自定义窗口只开放实测 WebView2 156.0.4314.58；其他 build、平台或普通上游 Wheel 在执行插件代码前明确拒绝。Linux/macOS 等价原生边界、在线发现、可信签名和分发属于第三批。

## Git 与复现

前端先提交并推送，再更新主仓库指针。Florolding 的修复已包含在当前子仓库提交中，保留同期其他修改；主仓库推送前复核全部 gitlink 在各自远端 main 可达，推送后全新浅克隆递归检出确认。

原生构建命令及哈希来源见[运行时说明](../../native/pytauri-runtime/README.md)，API 见[协议](../protocols/eclplugin-package-format.md#第二批独立窗口认证与联机契约)。运行真实门禁前，将 `ECL_NATIVE_UI_GATE=1`、`ECL_NATIVE_RUNTIME_PATH` 指向安装了本次 Wheel 的目录；冻结 Worker 验收另设 `ECL_WORKER_SMOKE_EXECUTABLE` 和 `ECL_WORKER_SMOKE_BUILDER=pyinstaller/nuitka`。这些变量仅控制测试，不放宽生产门禁。


## 远端核验

阶段二主仓库 `56ff246365c3ef8b5e4c4f2fc80d5913a4278fc8` 的 Backend Tests、Frontend Checks 与 Sync Submodule 已通过；远端 Checkout 实际检出了 Core、Florolding 和前端的全部记录指针。后续治理复核与固定修订另行提交，不把旧治理任务的失败写为成功。

本地全新浅克隆的主仓库检出已核实原生源文件、补丁与固定摘要一致；前端子模块下载遭遇 GitHub 网络 EOF，未把本次本地递归克隆标为成功。子模块交付依据采用 AGENTS.md 第 8 节允许的远端 CI 实际检出证据。

已核对并关闭完成的 #75、#83、#84、#91、#92、#94；#76 按最终复核与治理结果处理。Dependency Dashboard 作为持续依赖跟踪保留。
