# 原生网络隔离实验

生产实现及当前验收见[原生运行时说明](../README.md)；下文记录早期实验阶段，不能作为当前功能状态。这里的补丁与测试是历史实验材料，不接入启动器、不提供生产隔离保证。实验 API `experiment_block_network` 没有事件撤销、窗口代次和生产桥接契约，禁止作为正式窗口接口使用。

两个补丁均独立应用于固定上游提交 `734d5a1c7834935abdd39c85b53bf990a563febe` 的完整 workspace，不相互叠加。补丁包括 Windows 专属依赖及相应 Cargo.lock 更新；原始第三方许可证仍适用。

- `http-request-gate.patch`：取得实际 WebView2 controller，要求 `ICoreWebView2_22`，对所有来源注册资源事件；事件内部复核 URI，仅拦截实验使用的 127.0.0.1 HTTP/WS 请求。必须复核 URI，因为 WebView2 的资源过滤器共享事件，Tauri 自身的资源请求也会进入该处理器。
- `offline-network-gate.patch`：在前者基础上增加原生 CDP 离线策略，并等待原生完成回调后才让宿主导航。它是单独的后续实验，不能假定对子 frame、worker 和所有其他网络出口均有效。
- `plugin_webview_gate_host.py` / `test_gate.py`：先加载无插件代码的空白页面，在后台线程等待原生事件注册成功，再导航到测试壳并运行 sandbox iframe；服务器接收数必须为零，允许的宿主调用必须成功。
- `websocket_host.py` / `test_websocket.py`：额外允许 iframe 发起 WebSocket，并由实际 HTTP 服务器记录握手请求；不得以 JS 报错代替服务器观测。

测试显式使用 `ECL_WEBVIEW_GATE=1`，并使用安装了对应实验 Wheel 的隔离解释器。测试不是默认 pytest 测试路径的一部分。HTTP 与 WebSocket 报告分别用 `ECL_WEBVIEW_GATE_REPORT` 指定不同文件。

2026-11-01 实测：资源事件原型通过 HTTP 导航测试，但 WebSocket 测试失败，服务器收到请求；[微软上游也跟踪了此类资源事件的 WebSocket 缺口](https://github.com/MicrosoftEdge/WebView2Feedback/issues/4303)。新增 CDP 离线策略并等待完成回调后，真实 WebSocket 测试仍失败，服务器记录到 `websocket /unapproved-navigation`。这是本机 Runtime 的实际结果，不能假定离线模拟等于安全断网。原始报告位于忽略目录 `build/native-runtime/http-gate.json`、`websocket-gate.json` 与 `offline-websocket-gate.json`。

这些实验尚不满足 N01/U09。后续必须先补充能阻断 WebSocket 的控制并做独立成功对照，再验证 worker、新窗口、权限、原生消息来源及生命周期，完成生产接线和发行物验收。当前启动器继续使用原运行时，不加载实验补丁或开放第三方独立窗口。

## 固定拒绝代理的后续实验

`proxy_host.py` / `test_proxy.py` 在独立 WebView profile 中配置固定的本机拒绝代理，移除 Chromium 默认的 loopback 代理绕过并禁用 QUIC。测试代理对 GET 和 CONNECT 返回 403，从不转发请求。这里使用浏览器原生启动配置，不修改 JavaScript 的网络 API。

已在无 CDP 离线策略的资源事件实验 Wheel 上验证：宿主正向调用成功，WebSocket 目标服务器接收数为零。`worker_proxy_host.py` / `test_worker_proxy.py` 额外创建 Dedicated Worker，确认收到 Worker 启动标记，再检查目标服务器未收到其 WebSocket 请求；该项也通过。原始报告为 `build/native-runtime/proxy-only-websocket-gate.json` 和 `worker-proxy-gate.json`。这两项不是完整的浏览器隔离验收；尚缺 Shared/Service Worker、WebRTC/其他出口、代理失效和关闭竞态，以及生产生命周期和桥接来源控制。

构建及试验解释器均在 `build/native-runtime/`，当前试验解释器为 `baseline-test-env/Scripts/python.exe`，安装的是 `prototype-wheel/` 下不带 CDP 离线模拟的 Wheel。生产 Python 环境没有替换。`http-request-gate.patch` 与该 Wheel 对应，`offline-network-gate.patch` 对应失败的离线模拟实验；两个补丁均已在固定源码副本中通过 `git apply --check`。
