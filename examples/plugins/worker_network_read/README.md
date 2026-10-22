# 只读网络请求示例

在插件页安装本目录，审阅权限后启用。网络行显示精确站点 `https://connectivitycheck.gstatic.com:443`；允许表示可以读取该站点全部路径，仍只支持普通公网 GET/HEAD。可单独拒绝网络能力，插件加载本身不发送请求。

调用 `worker_network_read:read`（无参数）读取 Google 的公开连通性测试状态，正常结果为 `{"status_code":204,"body_bytes":0}`。不提交账户、身份头或本机资料。第三方站点不可用、系统 DNS 返回特殊用途地址或权限被撤销时明确失败，不自动重试。

`read_metadata` 演示正式 `network.request`：可指定 GET/HEAD，接收 HTTP 状态与完整实体的长度。实际 URL 必须匹配已审阅协议、域名和端口；GET 实体至多 32 KiB，HEAD 不返回实体。插件只能显式传送自己的凭据，宿主不会提供账户 Cookie 或认证。完整协议见 [归档文档](../../../docs/protocols/eclplugin-package-format.md#只读网络请求)。

使用 `python packaging/build-eclplugin.py examples/plugins/worker_network_read build/worker_network_read.eclplugin` 制作归档，无第三方依赖。Worker 仅支持 Windows x64，以当前系统用户运行；受控 Host API 不提供恶意 Python 的操作系统网络沙箱。
