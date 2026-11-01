# 插件专用 PyTauri 原生运行时

由启动器维护者维护。固定来源、补丁、策略及 Cargo 锁摘要在 `source.json`；更新须同时审阅源码和门禁，不跟随上游浮动分支。首次基线为 PyTauri `734d5a1c7834935abdd39c85b53bf990a563febe`，实际 Tauri 2.8.3 / Wry 0.53.1 / PyO3 0.25.1。原 PyPI Wheel 的 Tauri 2.8.5 与此基线不同，不能视作同一二进制。

## 构建与安装

需要 Python 3.11/3.12 与 Rust 1.99.0。Windows x64 使用 MSVC，Linux 使用 GTK 3 / WebKitGTK 4.1 开发包，macOS 使用 Xcode 命令行工具。从主仓库执行：

```powershell
python native/pytauri-runtime/build-wheel.py
python -m pip install --no-index --no-deps --require-hashes --find-links build/native-runtime/wheels -r build/native-runtime/install.txt
```

构建使用独立 venv、固定且校验摘要的构建工具、完整 upstream workspace 和 `cargo --locked`。脚本拒绝覆盖已有源码目录；再次构建可指定新的 `--output-directory`。`--source-repository` 可选择只读本地 Git 镜像，但检出的提交仍必须与固定 SHA 一致。不会修改其他项目或 site-packages；安装是后续显式命令。可用 `--target` 安装到验收目录。产物版本为 `pytauri` 与 `pytauri-wheel` 的 `0.8.0+ecl.2`，输出清单记录本次二进制摘要，不宣称不同路径或编译器环境产生逐字节一致的文件。原 PowerShell 入口转调同一构建程序。

## 边界和顺序

插件窗口只从 `about:blank` 创建，独立浏览器目录及拒绝代理在创建前绑定。全局 WebView2 浏览器参数、全局用户目录覆盖会阻止创建，以免破坏窗口隔离。原生控制安装完成后才允许一次精确受信壳导航；对应 NavigationId 成功完成后才启动 opaque iframe。窗口关闭或浏览器进程失败先撤销消息准入，再清理原生事件。

WebView2 的精确版本范围由 `source.json` 维护。Chromium 参数不是稳定的 WebView2 生产接口；Evergreen 更新到清单外的 build 时，普通主窗口继续工作，插件自定义窗口在执行任何插件语句前明确拒绝。新增 Runtime build 须重跑真实网络与生命周期门禁并审阅更新清单，不得只放宽版本比较。

原生层关闭通用 WebMessage/HostObject 入口，拒绝不许可导航、资源、外部 URI、新窗口、下载及权限，并拦截文件选择。单窗口本地 HTTP 代理只返回拒绝，不解析上游地址、不转发、不带 DIRECT 回退，覆盖 Chromium 隐含 loopback bypass；禁用 QUIC 与非代理 WebRTC UDP。CSP 继续限制插件页面，不能代替原生验证。

Windows CI 当前预装 WebView2 155.0.4283.62，本机使用 156.0.4314.58；两者作为明确的外部运行时验收范围写入版本清单，分别验证相同原生策略。清单只接受完整版本号，不接受通配或版本区间；新增版本须通过真实门禁才能标记验收完成。当前 155 的首次验收仍在进行。

WebView2 对本次 data 顶层文档的 `Source` 观测为空，不能据此鉴权。专用桥改为原生确认精确 URL 的 NavigationId，再只读取顶层 DocumentTitle 信号，并用原生 ExecuteScript 从不可变顶层闭包取出一次性消息。标题只携带窗口凭据及请求 ID，不携带业务正文。iframe 通过独立 MessagePort 请求，不能读取壳凭据或进入通用 IPC；窗内请求再由原始运行、许可和 Broker 白名单校验。

## 验收与许可

真实测试须同时有合法回传和服务端零请求，覆盖 WebSocket、worker、嵌套 frame、特殊协议、旁路、关闭及撤权；单独的 ready、超时或浏览器报错不构成通过。详细范围和当前结果保存在第二批交付记录，未验证的门禁不得标为完成。

`0.8.0+ecl.2` 接入 WebKitGTK 与 WKWebView，三平台原始 GUI 报告由 `Plugin Native WebViews` 工作流保留。POSIX 窗口在创建时启用独立临时数据上下文与固定拒绝代理，安装原生内容过滤后才加载宿主壳；禁止通用 IPC、文件选择、权限、下载和外部导航。桥接只从实际顶层文档读取宿主闭包，不采信 frame 名称。

WKWebView 公共接口没有关闭 PeerConnection 的等价选项，因此本项目使用版本固定的 WebKit 专用偏好接口，关闭 PeerConnection、媒体设备、屏幕捕获及可发现的 WebTransport 功能。该接口属于 SPI，不承诺跨 WebKit build 稳定；清单固定实际 `CFBundleVersion`，版本或必要接口不符时在空白窗口拒绝执行。更新须审阅接口并重新通过真实 GUI 和网络门禁，不扩大为任意系统版本支持。当前正在进行首次 POSIX 原生验收，结果以实施文档记录为准。

upstream PyTauri 使用 Apache-2.0，原文见 `UPSTREAM-LICENSE`；ECL 的原生策略使用主仓库 GPL-3.0。修改后的原生 Wheel 同时携带两份许可，发布时保留上游著作权和依赖声明；源码补丁、Cargo.lock 及构建清单须与产物一同保留。普通 Python 绑定保留 upstream Apache-2.0 许可。
