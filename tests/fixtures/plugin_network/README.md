这里的自签名证书与私钥仅用于自动化 HTTP/TLS 测试，覆盖 p10.example.test 与 other.example.test。它们不是生产凭据，不用于任何外部服务。测试客户端只通过受信测试宿主显式加载该 CA；插件清单、RPC、环境变量和发行资源均不能选择它。
