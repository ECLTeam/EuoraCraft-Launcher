# ECL 插件原生前端隔离增补方案

日期：2026-10-12，Asia/Shanghai。状态：用户已选择原生 frame/网络拦截方向，本详细增补待审阅；尚未创建原生原型、变更绑定或安装替代 Wheel。

本文件补充已获实施批准的 [Worker/API v2 设计](plugin-worker-api-v2-implementation-design.md)，只调整 P04/P18 的原生边界。Worker 与权限/API 方向不变，不迁移到 PyTauriX，也不修改本机另一套 PyTauri 项目。

## 1. 问题与源码证据

真实测试 `tests/test_plugin_webview_gate.py` 证明：sandbox iframe 自导航请求已抵达本地 canary，但 Python `on_navigation` 未观察到该子 frame 事件。CSP/sandbox 与当前绑定单独不足以满足 U09；测试结果不能转换成安全通过。

本机安装的 PyTauri 0.8.0 `ffi/webview.py` 暴露 `on_navigation` 和初始化脚本，但不暴露 frame 导航、WebResourceRequested、底层 CoreWebView2 controller 或 `with_webview`。主仓库没有 Rust Cargo workspace。发行依赖是 PyPI `pytauri-wheel>=0.8.0`，不能在 Python 中凭窗口 HWND 假定取得已有 WebView2 COM controller。

参考 [PyTauri 0.8.0 上游绑定源码](https://github.com/pytauri/pytauri/blob/734d5a1c7834935abdd39c85b53bf990a563febe/crates/pytauri-core/src/ext_mod_impl/webview.rs)；该上游提交是源代码研究定位，尚未证明与本机 Wheel 的全部 Rust 依赖完全一致，实际二进制错误信息含 Tauri 2.8.5。原生实施必须先建立可重现构建基线。

[WebView2 官方 frame 文档](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/frames) 提供 FrameCreated/Destroyed、frame NavigationStarting、frame WebMessageReceived，以及包含 iframe 来源的网络过滤接口。新 API 的存在不等于当前 Wheel 已暴露它，也不等于本机 Runtime 支持所有需要的 COM 接口。

## 2. 方案选择与维护代价

| 路线 | 取舍 | 决定 |
|---|---|---|
| Python 继续叠加 CSP/顶层 navigation 回调 | 无原生构建；已有实测证明挡不住子 frame 自导航 | 不采用 |
| 窄范围扩展现有 PyTauri 原生运行时 | 保留 ECL/SDK/前端；需要维护固定来源的 Wheel 构建及 Windows COM 生命周期 | 用户所选方向，本增补推荐 |
| 新建独立浏览器进程或迁移整个宿主框架 | 可重新定义浏览器边界，但显著扩大启动、IPC 和发行改造 | 本增补不实施 |

拟新增 `native/pytauri-runtime/`，保存固定源码 manifest、最小补丁清单、许可说明和 `build-wheel.ps1`。脚本只向专用 `build/native-runtime/` 获取固定提交并复核其身份，应用仓库记录的补丁；不依赖本机绝对路径，不使用浮动 main，不修改 site-packages。源码初始定位固定为 `734d5a1c7834935abdd39c85b53bf990a563febe`，调整基线必须说明与实际 Wheel 的差异。

已从本机 Git 对象只读确认该提交：workspace MSRV 1.82，Cargo.lock 为 Tauri 2.8.3、Wry 0.53.1、PyO3 0.25.1、webview2-com 0.38.0；Wheel 子项目在 `python/pytauri-wheel/`，用 setuptools/setuptools-rust 构建，根 `[patch.crates-io]` 才将核心 crate 指向修改后的工作区。因此构建必须从完整固定 workspace 运行，使用锁文件；不能单独复制 Wheel 子目录，误用未打补丁的 crates.io 核心库。

## 3. 原生接口及创建顺序

在 PyTauri `WebviewWindowBuilder` 边界提供仅受信 Python 宿主可设置的插件隔离策略；Windows 实现在独立 Rust 模块中，其他平台不引用 Windows API。策略只用于 `plugin-v2-` 专用窗口，主窗口沿用现有运行方式。

策略字段包括受信壳的精确 URL、仅本窗口可用的安装/运行代次，以及固定拒绝网络模式。首版插件前端不得直接联网；需要网络时经已授权 Host API。JS 不得修改策略，不接受来源前缀匹配，不授予 localhost、tauri 全站、file 或任意 blob URL。

创建顺序：

1. 仅创建不含插件代码的空白窗口，状态保持 `initializing`；Capability 不向 iframe 开放现有通用 pyfunc 或窗口管理命令。
2. 在 WebView2 UI/STA 线程取得实际 controller，查询必需 COM 接口并注册下述所有原生事件；注册失败则关闭窗口，返回稳定错误，不加载插件。
3. 核实事件均已安装、网络拒绝策略生效，再导航到精确受信壳。初始化顶层私密凭据只对当前壳文档有效。
4. 受信壳与宿主完成一次性 ready/代次确认后，才加入不可信 srcdoc iframe 和消息端口。首个插件语句执行前必须已经安装拦截；不能先 build 到插件 URL 再补事件。

如果当前 Tauri 创建接口无法证明上述顺序，应修改原生创建路径以延迟导航；不能用“事件一般很快注册”验收。

## 4. 必需的原生控制

| 控制点 | 行为与失败条件 |
|---|---|
| 顶层 NavigationStarting | 只允许精确受信壳及引导空白文档；任何重导航销毁旧凭据/代次，未经宿主重新初始化不得重新执行插件 |
| FrameCreated/Destroyed | 保留 frame 身份与事件 token；销毁后解除引用和事件；不从 JS 声明判断顶层身份 |
| frame NavigationStarting | 默认取消 HTTP(S)、file、宿主应用 URL、data/blob 等自导航；初始 srcdoc 由受信壳创建。官方 API 不支持取消 srcdoc 导航，须结合固定 CSP、消息端口身份和网络拒绝另测，不假设它可取消 |
| WebResourceRequested | 注册覆盖 Document 及所需 worker 来源的所有资源过滤，给未许可资源原生拒绝响应；不得把失败请求继续交给网络。需核实 iframe 来源过滤所需的 CoreWebView2 新接口及 Runtime 最低版本 |
| 原生命令来源 | 不可信 frame 的 native 消息不得进入 ECL 通用命令。专用插件桥只接受受信顶层、精确壳 URL、私密凭据及当前窗口/运行代次；Worker owner 和权限仍由 Broker 校验 |
| 新窗口/外部协议/下载/权限 | 拒绝弹窗、浏览器下载、任意系统 URI、设备/剪贴板/文件权限；不能用 Python 顶层导航回调代替这些独立出口 |
| 关闭及撤销 | 先在原生策略和 Broker 发布拒绝屏障，再撤销壳凭据与消息端口并关闭专用窗口；幂等解除事件和 COM 引用，不把已有远端副作用描述为可回滚 |

Python 回调不负责同步决定每一个网络请求；关键拒绝策略在原生 UI 线程直接执行，避免 GIL/异步 IPC 等待和事件循环死锁。日志仅记录白名单动作/拒绝类别，不记录完整 URL、凭据和插件消息体。

若当前 Tauri Invoke 链无法提供真实 frame 来源，N02 必须定位并在固定的 Tauri/Wry 原生链加最小来源控制补丁；不能把窗口 label 或 JS 自报 origin 当成 frame 身份。增加这类补丁时记录文件、依赖锁和原因，并维持所有既有可信主窗口路径的回归。

必须特别验证 WebSocket、service/shared/dedicated worker、重定向、嵌套 frame 和 Chromium 的特殊协议是否实际经过所选原生过滤。不能按 WebResourceRequested 名称假定它覆盖所有通道。遗漏出口时保持自定义 UI 禁用，并提交针对该出口的修订；不以修改安全宣传替代验收。

## 5. 分阶段代码、测试与回滚

| 阶段 | 具体改动 | 验收与阻塞 | 回滚 |
|---|---|---|---|
| N00 原生构建基线 | 固定 upstream 来源、原生 Cargo lock、Wheel 构建脚本和许可说明；隔离构建 venv；确认 Python 包与 ext_mod ABI | 不加安全功能先构建 Windows x64 Wheel，原有主窗口/IPC/打包测试通过；实际依赖无法复现则阻塞 | 不替换生产依赖，删除实验环境须限定专用目录，不动原项目 venv |
| N01 原生浏览器控制 | Rust frame/网络/导航模块、builder 策略入口和 Python stub；保留并清理所有事件 token | 将现有 canary 泄漏用例转为服务器 0 请求；故障注入事件注册失败时 0 插件语句；重定向、嵌套、worker/WebSocket 真实计数 | 自定义窗口保持关闭，可信主窗口沿用原 Wheel |
| N02 来源与生命周期 | 专用原生插件桥、顶层来源检查、壳代次；`ECL/api/bridge.py` 拒绝通用命令旁路；限定 Capability | U01—U08、U10/U11，直接 native invoke、伪造 origin/owner、错凭据、导航、撤销均无业务副作用；独立 positive control 必须成功 | 移除该运行代次的桥/端口/事件，不恢复旧主窗口第三方 JS 注入 |
| N03 发行集成 | `pyproject.toml` 与 CI 固定受控 Wheel 来源/版本/摘要；两种冻结打包；完整 P04/P18 门禁 | 在最终发行物实测 U01—U11、普通启动、权限撤销与残留资源；Ruff/pytest、pnpm check/build；核实 WebView2 最低 Runtime；任一遗漏不开放 custom | 恢复可信宿主依赖并保持 `ui.custom.execute` 关闭；保留授权和数据，不发布 |

Windows 专属安全能力缺失时拒绝创建用户自定义窗口，给出稳定错误 `PLUGIN_UI_ISOLATION_UNAVAILABLE`；第三方运行平台策略仍按 Windows x64 首版约束。其他平台的普通可信主窗口必须继续可构建运行。

测试必须真实启动 PyTauri/WebView2，带 native globals，服务器记录接收数量；不能用纯 Chromium iframe、mock、浏览器报错或 timeout 当作安全通过。增加 frame 创建/销毁循环、失败注册、关闭中请求和 revoke 竞态的资源测试；冻结产物记录 Wheel/Runtime/编译器版本和摘要。

## 6. 未确认风险与审阅边界

- 当前 Wheel 与 upstream commit 的 Rust 依赖差异，需要 N00 建立实际构建证据；不是顺带升级 Tauri/Wry/PyO3 的授权。
- 原生预加载注册顺序、iframe/worker 全来源过滤、WebSocket 等出口覆盖、Tauri 通用 IPC 在 iframe 中的路径，仍须源码定位及真实验收。
- 最低 WebView2 Runtime 版本由所需 COM 接口和验收确定；版本不足时 fail closed，不设置允许绕过的兼容回退。
- 原生增加一套受维护的 Wheel 发行链和供应链核验；所有远端推送/发布仍需单独授权。

需要审阅的是这一具体的原生绑定和发行依赖增补。原有 Worker/用户授权/Host API 实施批准继续有效。按根目录 AGENTS.md 第 0 节的详细文档审阅流程，以及主设计 P04 的阻塞处理，在本增补确认前不实施新增原生边界。
