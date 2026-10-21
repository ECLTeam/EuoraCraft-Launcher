# 插件只读网络请求实施方案

日期：2026-10-21（Asia/Shanghai）。状态：用户已选择 A（GET/HEAD 只读请求），本文件待审阅与实施确认；本轮未修改功能代码。

基线：主仓库及原工作区本地 main 为 `c553e21`，前端为 `0ead9f3`。接续 [P09 A 私有文件](plugin-private-files-implementation-plan.md)，细化 [总体设计](plugin-worker-api-v2-implementation-design.md) 的 P10 首个切片。本轮不把完整 P10 或完整文件管理能力标为完成。

## 1. 已选路线与交付目标

| 路线 | 范围与取舍 | 决定 |
| --- | --- | --- |
| A | 插件经 Host API 向已审阅站点发送 GET/HEAD，返回有界响应；先验证真实连接、重定向和权限撤销 | 用户已选 |
| B | A 加下载到私有空间、文件暂存、摘要和任务提交；业务闭环更长，失败面更大 | 后续独立审阅 |
| C | 先补文件覆盖、删除、子目录及恢复；需另证条件替换和恢复语义 | 后续独立审阅 |

目标为 `host.call("network.request", {"method":"GET","url":"https://api.example.com/meta"})`。GET/HEAD 是本接口允许的 HTTP 方法，不承诺服务端零副作用或已发送请求可回滚，参见 [RFC 9110 安全方法](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.1)。

首轮只支持明确声明的公网 DNS 站点。HTTP 和 HTTPS 区分授权，协议、域名、端口三者共同确定范围。支持 DNS 返回的普通公网 IPv4/IPv6；IP 字面量 URL、局域网、回环、链路本地、特殊用途地址和任意代理不在本轮范围。第三方 Worker 继续仅 Windows x64，网络策略与连接资源测试覆盖 Windows/POSIX 分支，不扩大实际 Worker 平台承诺。

不开放 POST/PUT/PATCH/DELETE/OPTIONS、请求体、下载、WebSocket、HTTP/2、自定义证书/关闭验证、共享账户 Cookie 或通用宿主 HTTP 客户端。network.write、network.lan 仍不实现。文件 API 和 settings.set 保持现行契约。

## 2. 当前代码、实测与缺口

| 位置 | 当前事实 | 方案处理 |
| --- | --- | --- |
| runtime/broker.py | 业务 JSON 请求和结果各至多 65536 字节；network.read 尚不在 implemented_capabilities | 不提高全局限额；验证通过后才开放能力 |
| 总体设计 13.6 | 原拟 256 KiB 响应体、任意方法和 body_b64 | 本轮改为 32 KiB 响应及 GET/HEAD，不保留旧形态 |
| grant_models.py | resource_refs 仅允许字母数字、点、下划线、冒号、连字符，长度至多128 | 复用精确范围集合，定义专属站点键，不把 URL 字符串前缀当许可 |
| WorkerService.permissions | resource_refs 是有效范围，未授权时为空；selected_resource_refs 是用户决定 | 管理视图增加不同语义的 requested_resource_refs，展示请求范围；不冒充已批准范围 |
| PluginPermissionsModal.vue | 当前显示能力和实例选择，没有网络目标展示 | 在现行权限弹窗展示站点，整组允许/拒绝；六语言同步 |
| plugins/network.py | 可信系统插件留下的响应 DTO/辅助方法，体上限8 MiB，不是 v2 RPC 的连接授权边界 | 不转接、不回退到此入口，不为本轮重构系统插件 |
| utils/network.py、CustomDownloadService | 桌面下载具有自身代理与重试语义，不具备本切片逐次连接准入 | 不复用宿主账户/代理，不包装桌面下载器冒充网络边界 |
| 当前依赖 | 实测 httpx 0.28.1、httpcore 1.0.9；httpcore 具有公开 NetworkBackend/NetworkStream 和 pool 注入接口 | 使用公开接口，不修改 HTTPX 的私有 _pool，不做版本探测和多实现回退 |
| operation_logging.py、logging.py | 三类项目日志处理器统一挂 OperationLogFilter；HTTPcore DEBUG 会带原始响应头 | 增加按当前执行上下文生效的传输日志过滤，覆盖文件、控制台和前端历史 |

隔离探针保存在忽略目录 `build/worker-manager/p10-plan-probe/`：

- 32768 原始字节的简单结果编码为43785字节；262144原始字节编码为349621字节。原拟256 KiB不满足当前业务上限。
- 使用自己创建的回环 TLS 服务器和专用测试 CA，逻辑 URL 为 p10.example.test；公开 NetworkBackend 将连接交给固定数字地址，实际系统地址解析只见127.0.0.1，TLS SNI 和 Host 保留逻辑站点，证书主机名验证开启，读回正确 JSON。
- 回环映射只存在于探针，不能成为生产参数或环境开关。该探针验证接口可用性，不代表公网过滤、撤销竞态、DNS 超时或冻结验收已经通过。
- 本机 Python 对192.0.0.8、2002::1和含回环编码的NAT64地址给出 is_global:true。因此实现不能只依赖当前解释器的 is_global。

## 3. 声明、授权和管理视图

### 3.1 精确站点键

清单继续使用已有权限字段，例如：

```json
{
  "capability": "network.read",
  "required": false,
  "resource_refs": ["https:api.example.com:443"]
}
```

network.read 必须声明1—16个不同的站点键。格式为 `http:域名:端口` 或 `https:域名:端口`；域名使用规范化小写 ASCII DNS/punycode，端口为1—65535的规范十进制，明确写出默认端口，不含路径、查询、尾点、通配符或子域隐含授权。沿用单键128字符上限，超长站点明确拒绝；不扩大全部资源引用的语法。首轮不接受 IP 字面量键、单标签本地主机名或 URL 形式的 `https://...` 键。

定义一个领域值对象 NetworkOrigin，统一声明、授权决定、URL 和重定向目标的三元组解析与比较。实际调用方为权限声明/存储、请求模型和连接边界；不创建通用 URL 路由框架。运行 URL 可经公开 URL 解析器规范化，匹配其准确站点键；父域、子域、相似后缀、其他端口和 http/https 都不互相授权。

允许某个站点表示可向其任意路径/查询发起 GET/HEAD，不承诺路径级授权。必要能力仍要求声明范围完整，授权缩小、内容变更、到期和 run 结束沿用现有 epoch/lease；不得给旧请求换新租约。已有未实现的 network.read 声明若不满足新契约，明确报错，不迁移或猜测旧范围。

### 3.2 主窗口审阅

可信管理视图的每个 PermissionItem 增加必填 `requested_resource_refs:string[]`，直接取自已绑定清单。保留 resource_refs 的有效范围含义和 selected_resource_refs 的用户决定含义，三者不是兼容别名。同步后端管理生产方、前端类型/边界校验、相关测试夹具和文档；Worker permissions.list 继续表示当前实际许可。

权限弹窗在 network.read 行列出全部请求站点，按 `https://api.example.com:443` 可读形式呈现，并说明整组授权覆盖这些站点全部路径、仅公网 GET/HEAD。复用能力复选框；允许时提交全部已展示请求键，拒绝时提交空范围。首轮不新增站点编辑器、通配符、动态追加或独立选择弹窗。可信接口仍按已有集合规则校验决定，不能超出声明。

六种语言补充能力、范围说明和相关失败提示。长站点可换行/查看全文，键盘焦点、保存去重、主题、背景与最小窗口按根规范验收。站点清单最多16项，明确滚动区域，最后一项和保存按钮均可达。

## 4. 正式请求与结果

`network.request` 使用严格模型，拒绝未知字段和隐式类型转换：

| 方向 | 字段与约束 |
| --- | --- |
| 请求 | `{method:"GET"或"HEAD",url:string,headers?:dict[str,str]}`；method和url必填，无body_b64 |
| URL | 输入及规范化后 UTF-8 长度均至多4096字节；http/https，合法 DNS 主机；拒绝userinfo、fragment、反斜线、控制字符、无效端口及authority编码混淆 |
| 请求头 | 至多16项，总UTF-8字节至多4096，键至多64字符、值至多1024；键遵循HTTP token，值仅可打印ASCII，大小写折叠后重复拒绝，禁止CR/LF等控制字符 |
| 请求头白名单 | Accept、If-None-Match、If-Modified-Since、User-Agent、Authorization、X-API-Key；认证头仅 HTTPS，均为插件主动提供，不读宿主凭据 |
| 成功 | `{status_code:int,url:string,headers:dict[str,str],body_b64:string}`，url是通过完整校验的最终URL，不包含服务端未校验跳转值 |
| 响应 | GET完整实体至多32768字节；HEAD不读实体，返回空body_b64，允许其Content-Length描述更大资源；无自动JSON/文本解析或truncated成功 |
| 响应头 | 仅content-type、content-length、etag、last-modified、cache-control，输出键小写；合计UTF-8字节至多4096，非法控制字符或无法明确合并的重复字段明确失败 |
| 编码 | 宿主发送Accept-Encoding:identity；GET只接受无Content-Encoding或identity，不自动解压压缩体；HEAD的描述性编码头不触发实体处理 |
| 总预算 | `min(RPC deadline,开始时间+30秒)`，覆盖排队/解析/连接/TLS/所有重定向/收体/交付；单阶段I/O至多5秒，按剩余时间递减 |

响应大于限额、编码不支持、长度不符或中途断流均返回明确失败，不交付截断内容。HEAD、204、304等无体响应按协议处理，不能因Content-Length较大误报；其他4xx/5xx作为真实HTTP结果返回，不伪装网络异常，不自动重试。最终结果继续通过 Broker 64 KiB 约束，并以最坏URL/头转义开销测试。

首轮同步返回，不创建 OperationManager 任务或 mutation 回执，不接受通用 operations.submit。相同调用可能发送新的请求，不承诺网络“恰好一次”；不自动重放。下载的任务协议另行设计。

建议稳定错误：非法输入 PLUGIN_SCHEMA_INVALID；未获准站点 PLUGIN_RESOURCE_DENIED；不可访问地址 PLUGIN_NETWORK_TARGET_DENIED；解析失败 PLUGIN_NETWORK_RESOLUTION_FAILED；连接/TLS/协议失败 PLUGIN_NETWORK_FAILED；重定向拒绝 PLUGIN_REDIRECT_DENIED；超限 PLUGIN_RESPONSE_TOO_LARGE；压缩体 PLUGIN_NETWORK_ENCODING_UNSUPPORTED；截止 PLUGIN_DEADLINE_EXCEEDED；配额 PLUGIN_RATE_LIMITED。撤销/到期/会话关闭沿用现有授权错误码，不输出URL、请求头、原始库异常或底层IP作为错误文本。

## 5. 实际连接边界

采用 HTTPcore 公开 ConnectionPool + NetworkBackend/NetworkStream，HTTPX 仅用于公开 URL 解析及 `create_ssl_context(trust_env=False)`。显式声明直接使用的 httpcore 1.x 依赖（最低1.0.9、低于2），以当前接口编程；没有私有属性替换、猴子补丁或多版本探测。

每跳建立独立 pool，HTTP/1.1、连接上限1、keepalive0、retries0、proxy=None，无 Cookie jar、身份认证插件、共享连接或宿主账户上下文。禁止系统/启动器/游戏下载代理，不读取证书环境覆盖；TLS验证及原域名SNI始终开启。不能直接使用 HTTPcore.default_ssl_context 推断环境已隔离：本机实现先调用 ssl.create_default_context，可能读取证书环境，故显式采用已检查的 HTTPX trust_env=False 入口。

连接执行顺序：

1. 严格解析URL、头和站点；取得原始network.read lease和当前请求资源名额。
2. 接受一次有界DNS解析，在独立且有归属的解析池执行；取得结果后复核原lease、截止时间和关闭状态。
3. 将结果转换成明确IPv4/IPv6地址，去重，至多16个；任一不允许的地址使整跳失败，不筛掉内网地址后继续。
4. 只向选中的已验证数字IP与准确端口连接。首轮每跳只选一个结果，无隐藏重试或再次域名解析；连接失败明确返回。NetworkBackend收到的逻辑域名仍用于核对站点。
5. 注册真实流，核实peer与选择的数字IP/端口相同，复核原lease，再接受TLS阶段。TLS仍使用原域名作SNI与证书主机名验证，不把IP当作TLS站点。
6. TLS完成后再次登记真实流并核实peer、lease和关闭状态；HTTP Host由已批准域名/端口生成，插件不能覆盖。然后接受一次有限请求头发送，原始I/O锁不嵌入授权或全局状态锁。
7. 每次底层读写和每跳开始前检查原权限及总截止时间；最终交付前再检查。发送后撤销只能停止后续收发/交付，不能宣称撤回已发送字节。
8. finally关闭响应、pool和所有流，确认真实关闭后释放资源名额；失败保留可重试收尾，不据等待方超时提前宣布结束。

IP策略归属 NetworkTargetPolicy，使用不可变明确网段与测试，结合IANA特殊用途注册表，不能只调用解释器is_global。首轮拒绝整个特殊用途网段，即使某些例外被标为可全球到达；另拒绝组播、保留和IPv6非普通公网单播范围、IPv4映射/兼容、NAT64、6to4/Teredo及zone id。IPv6首轮仅2000::/3中排除特殊用途后的地址。链路本地云元数据等不因域名白名单而放行。网段在发布源码中固定并注明核对日期，不在启动时联网更新策略。

本轮核对来源：[IANA IPv4](https://www.iana.org/assignments/iana-ipv4-special-registry)、[IANA IPv6](https://www.iana.org/assignments/iana-ipv6-special-registry)、[Python 3.11 ipaddress](https://docs.python.org/3.11/library/ipaddress.html)。

连接和流封装只服务 RuntimeNetwork 当前调用方：固定数字地址、权限检查点、总截止、流关闭与收包预算。底层每次读至多8 KiB，每跳解密后HTTP原始收包总量至多256 KiB，包含状态行、响应头、临时1xx、分块/尾部和实体，防止仅约束最终body却无界读取头；实体仍独立限32 KiB。HTTP解析交由库，不自行实现HTTP解析器，不改库的全局常量。

库文档只支持接口可行性，不替代安全验收：[公开网络后端](https://www.encode.io/httpcore/network-backends/)、[HTTPX环境隔离](https://www.python-httpx.org/environment_variables/)、[HTTPcore日志内容](https://www.encode.io/httpcore/logging/)。若实际路径不能证明连接前固定IP、保留正确TLS站点或阻止未获准HTTP发送，则保持network.request及network.read未开放，不退回“请求前后检查”的实现。

## 6. 重定向和身份信息

关闭库自动跟随，由边界处理301/302/303/307/308，最多5次跳转；每跳重做URL、准确站点、DNS、数字IP、原lease和截止检查。相对Location按当前URL解析，缺失/超长/非法Location、循环或跳数超限明确拒绝。GET/HEAD保持原方法，HTTPS降级到HTTP拒绝。

跨站点跳转只有目标也已声明且获准才允许；跨站点永久移除Authorization和X-API-Key，后续即使返回原站也不恢复。Cookie、Proxy-Authorization、Host、Forwarded、连接/长度/传输和编码控制头不接受；Set-Cookie不存储、不返回、不转发。先关闭当前跳的响应再开下一跳，禁止读完整跳转实体浪费预算。即使用户批准域名，其DNS结果落入禁止范围也不能继续。

## 7. 生命周期、DNS和配额

WorkerService持有一个 PluginNetworkService，负责请求资源、名额及有界DNS工作者；每个Broker持有 RuntimeNetwork，负责当前run的lease和方法。查询执行仍在现有RPC线程，不新增业务执行器，不把同步网络查询伪装成OperationManager任务。

拟定上限：每run同时2个请求，宿主同时8个请求，DNS池最多4个实际工作且不累积无界待办。DNS等待每次至多5秒并受总预算约束；解析任务准入占用有限名额，额满直接限流，不排无限队列。不做请求/解析/连接缓存或自动重试。

socket.getaddrinfo的等待超时或Future.cancel不证明实际解析线程已结束。解析future必须独立跟踪，结果到达后不再继续已超时、撤销或关闭请求的连接；实际解析未结束时保留相应资源与pending。DNS上下文显式传播，关闭不无限join或强杀线程。所有名额在真实工作与清理完成后释放，不因RPC已返回提前归还。

可选权限变化只取消本run相关请求，必要权限撤销/断连/禁用/应用退出拒绝新接纳，尝试关闭所属流。TCP或TLS已接受且正在返回流时，返回后立即登记并按关闭状态收尾；不因尚未取得流对象误报已结束。无流可关闭的阻塞阶段受5秒I/O预算约束，实际仍未结束就pending，不承诺取消瞬间结束。

Broker.wait_idle同时检查真实RPC、文件/任务和网络资源；WorkerService监控只重试关闭与确认，不重做请求。清理pending时拒绝同安装重新启用。应用退出只等待有界时间，失败保留可观察pending，不把“停止等待”当作“DNS/传输实际结束”。这一路径是接线前硬门槛。

## 8. 日志与脱敏

运行时仅记录中文“插件网络读取”的开始、结束、耗时、方法和结果/稳定错误码等白名单；不写请求/来源编号、完整URL/查询、头、认证、响应内容或完整参数。

HTTPcore原始DEBUG可包含响应头，不能仅删Authorization键。扩展现有共享日志上下文与过滤器，在本请求作用域中、消息格式化前阻止httpcore/httpx原始传输日志进入所有项目处理器；默认上下文保持原行为，不全局关闭第三方日志，不影响用户下载、账户或其他插件。DNS线程传播上下文。预期网络异常转换稳定码；未预期异常保留脱敏调用栈，不记录传输原始异常值或造成重复堆栈。

本网络方法不是主窗口IPC，不向IpcLogPolicy伪加正式命令；权限管理沿用现有IPC日志规则。回归覆盖文件日志、控制台、前端历史，以及两个并发上下文之间不串过滤。

## 9. 实施步骤与文件

| 步骤 | 位置 | 完成条件 |
| --- | --- | --- |
| 1. 契约与授权 | 新增plugins/network_targets.py；grant_models.py、grant_store.py、runtime/models.py、WorkerService.permissions；前后端PermissionItem | 准确站点键、声明/决定校验、requested/selected/effective三类范围一致；无旧别名 |
| 2. 原生连接与预算 | 新增runtime/network_transport.py及相关测试；pyproject.toml直接依赖 | 公开NetworkBackend/Stream固定IP、TLS、peer、限包/总预算、关闭可验证；先不开放生产方法 |
| 3. 请求归属与DNS | 新增runtime/network_api.py；WorkerService、Broker、共享日志过滤 | 有界请求和DNS跟踪，撤销与关闭只收尾自身，pending真实，日志不泄漏 |
| 4. GET/HEAD接线 | broker implemented_capabilities/dispatch、严格request、重定向及结果投影 | 所有连接、身份、生命周期硬门槛通过后才开放network.read/network.request |
| 5. 权限UI | 前端权限校验/类型、PluginPermissionsModal、六语言及测试 | 能审阅完整站点范围，允许决定基于展示的声明；长列表/主题/键盘/保存去重通过 |
| 6. 示例和发行 | 新增examples/plugins/worker_network_read；包格式/总体设计、Windows产物CI与真实主窗口夹具 | 使用生产API完成小型元数据读取；TLS、超限和撤销在实际发行物中复验 |

Core/Florolding及桌面下载服务不改。若必须改它们才能满足本切片边界，先说明原因、修订范围并审阅，不偷偷引入依赖或旧实现回退。

## 10. 验收与停止条件

| 场景 | 必须结果 |
| --- | --- |
| 未授权、空/越范围决定、必要/可选、到期、旧epoch/run/内容 | 无未获准连接或结果交付；旧请求不换lease；站点编辑必须重新审阅内容 |
| scheme/端口/域后缀/大小写/punycode、userinfo/fragment/控制符/编码IP/无效键 | 严格解析并作准确三元组判断；无前缀/子域扩大 |
| DNS公私混合、重绑定、IPv4/IPv6/映射/转换地址、云元数据、peer不符 | 禁止目标没有TCP/HTTP业务连接；连接只使用已验证数字IP，无二次域名解析 |
| HTTPS固定IP、SNI/Host/证书、错误证书、代理/证书环境变量 | 逻辑身份保留、验证开启；不继承环境代理/认证/证书绕过 |
| 同站/跨站/相对跳转、未准站点、私网目标、降级、循环、6跳 | 逐跳检查，认证头不跨站，拒绝前不发送下一跳 |
| 32 KiB、32 KiB+1、无Content-Length、chunked、压缩、断流、巨大头、1xx/尾部 | 全部预算有限，返回完整体或明确失败；HEAD/204/304不误读实体 |
| DNS卡住、慢连接、慢TLS、慢头、持续小块、关闭失败 | 总截止生效，实际未结束则pending；容量不提前释放；不得后台继续连接 |
| DNS/TCP/TLS/发送/收体/交付阶段分别强制撤销先或接受先 | 顺序可确定；接受后的有限动作允许返回，后续动作必须重新检查，已发字节不称回滚 |
| 两插件/两run并发、run2/宿主8/DNS4满额、权限恢复/再次启用 | 仅影响自身；限流与真实收尾可观察；其他用户任务与文件行为不回归 |
| Authorization/API key、URL query、Set-Cookie、异常及并发日志 | 文件、控制台、前端历史均无私密值；非本请求日志保持原行为 |
| 主窗口授权与实际Worker/冻结产物 | 用户看见站点并保存后才可请求；真实GET/HEAD读取、超限、撤销及清理通过 |

使用事件屏障、可控DNS结果、实际本地HTTP/TLS服务器和本次专用测试证书，不能只用MockTransport证明连接边界。测试可注入受信解析/拨号/CA夹具，将逻辑公网目标映射到用例自己的回环服务器；生产清单、RPC、环境变量和发行资源不能选择这些测试能力。网络策略拒绝测试必须证明未调用拨号；真实流测试必须证明SNI/Host/peer/取消行为。额外外网验证只访问已知只读小型测试资源，不能使用个人账号或真实业务凭据。

按 [AGENTS.md 第6节](../AGENTS.md#6-测试与验收) 执行主仓库Ruff、格式、全仓及完整pytest和Core集成门禁；前端pnpm check后pnpm build，再真实主窗口Classic/Folia、亮暗、背景/透明/模糊和三种窗口尺寸。PyInstaller/Nuitka以最终代码构建，真实发行物检查导入边界、网络能力、现有文件/任务/实例/下载回归。区分源码/模拟平台/真实Windows/未执行POSIX实机及远端CI。

任何实际连接、TLS、权限接受顺序、DNS真实结束或日志隔离不能证明成立时，暂停对应接线并说明阻碍；不靠增加外层检查宣称实现。功能完成后按根规范独立提交功能/文档，前端本地main先集成，随后主仓库；本轮无远端推送或发布授权。

## 11. 本轮调研交付与待确认

本轮仅提交方案、公开文档核对、隔离TLS接口探针和现有功能基线结果。探针、专用测试密钥和日志只在忽略目录，不进入代码或发行资源。现有network.read/network.request保持未实现。

当前基线验证：授权存储、Broker、私有文件、任务归属、操作日志和Worker RPC相关用例共185通过、1跳过，见 build/worker-manager/p10-plan-baseline.txt；本轮未重跑完整主仓库、前端或冻结构建，不能把上轮验收记作本轮网络功能通过。方案本地链接和git diff --check通过。

实施确认覆盖：公网DNS站点的精确协议/域名/端口范围、管理requested_resource_refs字段、32 KiB完整响应、明确的请求头/编码限制、真实DNS/连接生命周期以及六语言站点审阅。确认后按第9节实施，验收通过后再讨论下载任务；不将本文件视为功能已经完成。
