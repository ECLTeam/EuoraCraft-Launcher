# EuoraCraft Launcher × HMCL 全量功能对比报告

> 生成日期：**2026-10-02**
> 对比基线（两侧均已拉取到当日最新 main）：
> - **EuoraCraft Launcher**：主仓 `9a21dc7`（2026-10-02）＋ 子模块 `ECL/game` `481bf4e`、`ECL/services/florolding` `97cf71c`、`frontend` `58b6fba`
> - **HMCL**：`77eee17d3`（2026-09-30/"更新README中contributor的人数至130人以上"），已 `git pull` 到最新
>
> 方法：对两侧仓库逐文件代码考古，**每条结论附证据路径**（ECL 相对本项目根，HMCL 相对 `E:\Projects\HMCL`）。无法从代码确证的推断一律标注「未核实」。
> 本文取代并修正 `docs/feature-comparison-and-optimization-plan.md`（2026-09-26）中关于 HMCL 与 ECL 的结论；该文档中至少 5 项结论因 ECL 侧 92 个新提交而失效。

---

## 0. 摘要与结论速览

| 维度 | 结论 |
| --- | --- |
| 体量 | HMCL 是 ECL 的**约 4 倍**：HMCL 937 个 Java 文件 / 174,764 行（含空行）；ECL 139 个 Python 文件 / 43,621 行（不含前端），前端另有 368 个 TS/Vue 文件 |
| 成熟度 | HMCL 已迭代十年以上、支持 **23 种 CPU 架构**、10 种语言、114 个持久化设置项；ECL 处于 `0.0.1-alpha`，实质支持 Windows + Linux/macOS 分支 |
| ECL 已补齐（旧文档判定缺失但现已实现） | **整合包在线安装**（Modrinth/CurseForge/FTB）、**启动器自更新下载与替换**、**启动高级选项**（wrapper/后退出命令/环境变量/窗口标题/可见性/内存锁定/进程优先级）、**崩溃分析管道**（含移植 HMCL 规则）、**自定义下载与任务管理** |
| ECL 仍然缺失的硬功能（P0） | **Java 运行时自动下载**、**OptiFine / LiteLoader / Cleanroom / LegacyFabric 安装**、**账户令牌加密**、**插件签名校验** |
| ECL 缺失但价值高（P1） | 下载 ETag 条件请求缓存、内容寻址硬链接库缓存、下载断点续传、镜像规则表（HMCL 有 24 条重写规则）、QuickPlay 三类、渲染器与原生库选项、依赖一键补齐、模组批量更新的缓存与定时、ChunkBase 种子地图、Deep link（两侧皆无） |
| ECL 独有优势 | **插件系统**（6 类宿主扩展点 + 30 个前端插槽 + Worker 隔离 + 事务安装）、**联机房间体系**（EasyTier/自定义节点/NAT 检测/端口探测）、**原理图 3D 查看器**、**单实例与 CLI 快速启动**（HMCL 完全没有）、**服务器列表与截图管理**、**光影包本地管理**（HMCL 无此类型）、**全局数据包管理**、**真实生效的 Windows 进程优先级**（HMCL 该平台分支被注释）、**开发者通道 WebSocket**、**Showcase 演示运行时**、**第三方实例（PCL/HMCL）兼容读取**、**本地衣柜（皮肤/披风库）** |
| 架构分野 | HMCL = 纯逻辑引擎（HMCLCore，420 个主源文件）+ JavaFX UI，无插件、无进程隔离；ECL = 单 Python 进程内嵌 Tauri，业务全在后端，前端为可热插的 Web 层，插件可跑独立 Worker 进程 |
| 许可 | 两侧均为 **GPL-3.0**（各仓库根 `LICENSE`，均无第三方许可聚合清单于 ECL 侧未核实）；互相借鉴代码需保持同许可并保留版权声明 |

---

## 1. 对比基线与方法

### 1.1 基线与增量

| 项目 | 值 | 证据 |
| --- | --- | --- |
| HMCL 拉取结果 | `59bcc7fe6..77eee17d3`，共 5 个新提交（含 `83ffc91a8 重构 DownloadProvider (#6919)`、`4118fa972 添加实例文件夹后自动选中 (#6912)`、`fdf883d09 更新 Chunkbase 种子地图 (#6892)`、`342bcc543 Bump Gradle to 9.8.0`） | `git log --oneline 59bcc7fe6..HEAD` |
| ECL 主仓增量 | 距离 2026-09-26 的 `16341c0` 已前进 **92 个提交** | `git log --oneline 16341c0..HEAD` |
| ECL 子模块增量 | `frontend` 前进 **50 个提交**；`ECL/game`、`ECL/services/florolding` 指针各更新 1 次 | `git ls-tree` 对比 |

### 1.2 旧文档中已失效的结论（必须修正）

| 旧文档结论（2026-09-26） | 现状（2026-10-02） | 证据 |
| --- | --- | --- |
| 「ECL 整合包在线安装缺失」 | **已实现**：`modpack.py` 59 KB，支持 Modrinth / CurseForge / FTB 三源在线安装，另有 6 种本地格式解析 | `ECL/services/game/modpack.py:713`（`install_modpack_online`）、`:580`（`build_ftb_plan`） |
| 「ECL 无启动器自更新下载替换」 | **已实现**：`app_update.py` 27 KB，分阶段下载 → 摘要校验 → 落 pending → Windows/POSIX 引导脚本延迟替换 → 失败回滚 | `ECL/services/app_update.py:183`（`UpdateApplier`）、`:577`（`_win_bootstrap`）、`:615`（`_posix_bootstrap`） |
| 「ECL 无 wrapper / post-exit / 环境变量 / 窗口尺寸启动项」 | **已实现**：wrapper、后退出命令、自定义环境变量、窗口标题、启动器可见性全部接入启动链路 | `ECL/services/game/launch.py`；IPC `game_version_settings_*` |
| 「ECL 无内存锁定 / 进程优先级」 | **已实现**：`-Xms=-Xmx` 内存锁定与 `idle/below_normal/normal/above_normal/high` 五档进程优先级 | `ECL/game` `d448a2b feat: 支持内存锁定（-Xms=-Xmx）与游戏进程优先级设置`；`docs/launcher-cli.md:29-30` |
| 「ECL 无崩溃分析」 | **已实现**：崩溃捕获模块化 + 三源 Mod 索引 + 堆栈分析器 + **移植 HMCL 精选规则**（支持命名捕获组） | `ECL/services/game/crash/`；`7816d5b feat: 崩溃规则目录支持命名捕获组并移植 HMCL 精选规则` |
| 「ECL 无 CLI 快速启动」 | **已实现**：完整命令行 + Windows 快捷方式 | `docs/launcher-cli.md`（全表）、`ECL/cli.py:107-164` |

---

## 2. 项目概览对照

| | EuoraCraft Launcher（ECL） | HMCL |
| --- | --- | --- |
| 定位 | 插件生态优先的现代启动器，Web 技术栈 | 老牌跨平台启动器，引擎成熟度与平台覆盖优先 |
| 技术栈 | Python 3.11+（pytauri）+ Tauri v2 + Vue 3 + naive-ui | Java 17+ + JavaFX + JFoenix + MonetFX（Material You） |
| 架构 | 单 Python 进程内嵌 Tauri 主循环；插件可跑独立 Worker 进程 | `HMCLCore`（纯逻辑 420 个主源文件）+ `HMCL`（JavaFX UI 438 个）+ `HMCLBoot`（7 个） |
| 规模 | 139 个 Python 文件 / 43,621 行；前端 368 个 TS+Vue 文件、429 个 `src/` 文件 | 937 个 Java 文件 / 174,764 行（非空行 151,120） |
| IPC / 接口面 | **216 条 IPC 命令**（`ECL/api/registry.py:34-251`） | 无 IPC 层，UI 直接调用 HMCLCore |
| 测试 | 82 个 pytest 文件（含插件、崩溃、联机、打包隔离） | 69 个 `*Test.java`（Core 41 / HMCL 27 / Boot 1） |
| 语言 | 6 种（zh-CN/en-US/ja-JP/ru-RU/de-DE/zh-TW），各 1538–1693 键 | 10 种（含文言文 `lzh`、生成语言 `en-Qabs` 倒置英文），默认语言 1499 键 |
| 插件 | **有**：6 类扩展点 + 30 个前端插槽 + `.eclplugin` 归档事务安装 | **无** |
| 平台 | 声明 win32/linux/darwin；打包以 Windows 为主（Nuitka、PKGBUILD 另有 Linux） | **23 种 CPU 架构**（含 MIPS/PPC/S390/RISC-V/LoongArch 新旧世界） |
| 自更新 | 已实现下载与延迟替换（含摘要校验、回滚、pending 清理） | 已实现（`IntegrityChecker` 自签名校验、`--apply-to` 外部替换、`ExecutableHeaderHelper` 保留 exe 头） |
| 发布 | semantic-release + `vMAJOR.MINOR.PATCH-beta.N+YYYYMMDD` | 稳定版 / 开发版 / 预览版三通道 |
| 许可 | GPL-3.0 | GPL-3.0 |

---

## 3. 逐域功能对比矩阵

图例：✅ 完整实现；🟡 部分/受限；❌ 无。

### 3.1 账户与认证

| 功能 | ECL | HMCL | 说明与证据 |
| --- | --- | --- | --- |
| 账户类型 | ✅ 5 类：offline / microsoft / authlib / **plugin（插件注册的登录表单）** + 提供者定义 | ✅ 3 个工厂：offline / microsoft / authlib-injector（`ClassicAccount` 仅为抽象父类） | ECL `ECL/services/accounts.py:350-553`；HMCL `HMCL/.../setting/Accounts.java:71` |
| 微软设备码流 | ✅ 唯一实现方式 | ✅ 有 | ECL `ECL/game/Core/MicrosoftAuth.py:86-87,219-287` |
| 微软授权码 + PKCE + 本地回调 | ❌ | ✅ NanoHTTPD 本地服务器，端口 29111–29115 顺延，`SecureRandom` 生成 `state`(32B)/`code_verifier`(64B)，SHA-256 + URL-safe Base64 | `HMCL/.../game/OAuthServer.java:45,56,63-76,188-198` |
| 设备码 `slow_down` / `expired_token` 处理 | ✅ **均已处理**：`slow_down` → `interval += 2` 秒后 continue；`expired_token` → 抛 `MicrosoftAuthError("设备码已过期，请重新尝试")`。轮询缺省间隔 5 秒，先 sleep 再请求；超时用服务端 `expires_in`（缺省 1800 秒）。**注意**：`authorization_declined` / `bad_verification_code` **无显式分支**，落入 else 抛「授权失败: {error}」 | ✅ `authorization_pending`、`expired_token`、`slow_down`（间隔 **+5000 ms**）均有分支；间隔取服务端 `interval` 秒转毫秒；**超时压到 `min(expires_in, 900)` 秒**（比 ECL 的 1800 秒更保守）。⚠️ **HMCL 存在缺陷**：`authorization_declined` / `bad_verification_code` **无分支且会走到成功返回分支**，被当作成功返回 `Result` → 用户拒绝授权时可能误判为登录成功 | ECL `ECL/game/Core/MicrosoftAuth.py:244-269,287`；HMCL `auth/OAuth.java:116-167` |
| 离线账户 | ✅ UUID 派生/自定义 | ✅ 且**独门方案**：内置本地 Yggdrasil server + javaagent 注入自定义皮肤 | ECL `ECL/services/accounts.py:717-733`；HMCL `auth/offline/YggdrasilServer.java`、`OfflineAccount.java` |
| `userType` 取值 | `offline → legacy`、`microsoft → msa`、`authlib → yggdrasil`；**唯一改写**是 yggdrasil → `"mojang"`，msa/legacy 原样透传；离线账户 access_token 固定为字符串 `"None"`。**⚠️ 缺陷**：替换表**不含 `clientid` 与 `auth_xuid`**，若某版本 JSON 含 `${clientid}` / `${auth_xuid}` 占位符将不被替换 | `msa`/`mojang`/`legacy` 三值；**离线账户刻意使用 `msa`**（而非 `legacy`）以免服务器报 invalid session | ECL `ECL/services/accounts.py:658-684,700-702`、`ECL/game/Core/ECLauncherCore.py:345-347,349-367,377-386`、`launch.py:962`；HMCL `auth/AuthInfo.java:33-35`、`OfflineAccount.java:104-105` |
| authlib-injector | ✅ 服务器历史 + 元数据 | ✅ 服务器列表分发 `authlib-injectors.json` + 元数据缓存 + 悬空账户清理 | ECL `ECL/services/authlib.py`；HMCL `HMCL/.../setting/AuthlibInjectorServers.java:84-93`、`Accounts.java:480-492` |
| 皮肤上传 / 重置（微软 API） | ✅ | ❌（仅离线注入） | ECL `ECL/api/accounts.py` |
| 披风装备 / 卸下 | ✅ | ❌ | 同上 |
| 本地皮肤库 / 衣柜 | ✅ 哈希去重、导出、同步应用 | 🟡 离线皮肤经本地 Yggdrasil 注入 | ECL `ECL/services/wardrobe.py` |
| 3D 皮肤预览 | ✅ skinview3d（Web） | ✅ 自研 JavaFX `SkinCanvas`（含动画） | ECL `frontend/src/components/...SkinViewer3D.vue`；HMCL `ui/skin/` |
| 多账户管理 | ✅ 收藏 / 置顶 / 刷新 / 切换 / 自动选当前 | ✅ 全局/便携**双存储** + 跨目录迁移 + 每实例启动时选账户 + `CharacterSelector` | ECL `ECL/services/accounts.py:554-946`；HMCL `Accounts.java:185-262` |
| 账户私有数据分离 | 🟡 元数据与令牌分文件但仍同目录 | ✅ `accounts.json`（元数据）与 `AccountPrivateData`（令牌）分离 | HMCL `Accounts.java:265-274` |
| 令牌加密 | ❌ **全明文 JSON**（grep `encrypt/Fernet/keyring/DPAPI/AES/PBKDF2` 零命中） | 🟡 **ChaCha20-Poly1305 但使用硬编码内置密钥**，源码自述「weak portable protection…不应视为设备绑定的密钥存储」 | ECL `ECL/services/accounts.py:295-337`、`ECL/game/Core/MicrosoftAuth.py:125-151`；HMCL `setting/ProtectedPayload.java:98,107` |
| 账户文件原子写 | ✅ `atomic_write_text` | ✅ `FileUtils.saveSafely` | 两侧均有 |

> **安全结论修正**：旧文档把 HMCL 记为「AES/GCM 强加密」。实测为 **ChaCha20-Poly1305 + 仓库内硬编码密钥 + 4 lane 加 63 项 padding 混淆**，属**混淆级**而非设备绑定加密。因此两侧在「令牌保护」上都不是强安全实现，但 HMCL 至少避免了直接可读，ECL 是**直接明文**。

### 3.2 下载引擎与镜像

| 功能 | ECL | HMCL | 说明与证据 |
| --- | --- | --- | --- |
| 镜像源数量 | 🟡 **仅 2 个**：official / bmclapi | ✅ 官方的**规则表重写**，**24 条** `MirrorRule`（20 条常规 + 4 条 `fallback=true` 的 Modrinth/CurseForge 加速规则） | ECL `ECL/services/game/download_sources.py:34-43`；HMCL `HMCLDownloadProvider.java:123-150` |
| Provider 架构（最新重构） | 简单双客户端（`PreferredApiClient`） | ✅ **`83ffc91a8 重构 DownloadProvider`**：删除 `MojangDownloadProvider`/`BMCLAPIDownloadProvider`/`AutoDownloadProvider` 三个子类 + Wrapper（约 −609 行），改为 **`DownloadCandidates` 有序候选列表 + `MirrorRule` 前缀重写表**；新增镜像源只需加一行规则 | `git show --stat 83ffc91a8`；`download/DownloadProvider.java`、`DownloadCandidates.java` |
| 第三方镜像 | ❌ | ✅ 腾讯云 Maven、`alist.8mi.tech`（Cleanroom/未上架版本）、`mod.mcimirror.top`（Modrinth/CurseForge，**强制优先**） | `HMCLDownloadProvider.java:139-149` |
| 源选择策略 | 🟡 两源互备 | ✅ `DownloadSource` 三值 `DEFAULT/OFFICIAL/MIRROR`，`DEFAULT` 由 `LocaleUtils.IS_CHINA_MAINLAND` 决定候选顺序；**版本列表源与文件源可独立配置** | HMCL `DownloadSource.java:25-32`、`HMCLDownloadProvider.java:42-43,49-79` |
| 候选 URL 回退 | ✅ 元数据层换源 + 文件层用备用源**重新生成 URL** 重下 | ✅ 单文件多候选顺序尝试，某候选成功即返回；全败时 `addSuppressed` 保留完整失败链；**404 短路**不再重试该候选。**已核实**：`DownloadCandidate` 的 `retry`/`connectTimeout`/`readTimeout` 三字段**机制已接线**（`FetchTask.java:113-115` 消费，分别回退到 `DEFAULT_RETRY=5` 与 `NetworkUtils.TIMEOUT=10s`），但**全仓无任何调用方填充它们**（只有两个工厂方法，均传 `-1, null, null`）→ 当前恒取默认值，属「已建能力、尚未使用」 | ECL `ECL/services/game/download_sources.py:61-99`、`base.py:307-341`；HMCL `task/FetchTask.java:113-115,104-142,488-490`、`download/DownloadCandidate.java:22-43`、`util/io/NetworkUtils.java:50` |
| 任务级回退 | ❌ | ✅ `FallbackTask`：Forge/NeoForge 版本列表因 BMCLAPI 结构不同而无法做 URL 重写，改为任务级回退 | `HMCLDownloadProvider.java:156-189,252-288` |
| ETag 条件请求缓存 | ❌ | ✅ `etag.json`：按去 query 的 URL 归一化键、仅带 `etag` 头才缓存、`no-store` 不缓存、`If-Modified-Since` 已注释停用、**篡改检测**（lastModified 变化则重算 SHA-1）、`ReentrantReadWriteLock` + `FileChannel.tryLock` 并发保护、多实例 `joinETagIndexes` 合并 | `HMCLCore/.../util/CacheRepository.java:177-207,225-229,279-306,338-398` |
| 内容寻址硬链接缓存 | ❌ | ✅ `createLink`：`cache/<算法>/<hash前2位>/<hash>`，校验通过后把游戏目录文件**硬链接**到缓存，磁盘仅一份；另有 `index.json` 记录 Forge 变体库 | `CacheRepository.java:103-106,165-170`；`DefaultCacheRepository.java:62,127-207` |
| 缓存清理 | ❌ 无缓存层 | ✅ 设置页「清理缓存目录」（整目录含索引，异步 + 防重入 + spinner）；**无 LRU/容量淘汰** | `HMCL/.../ui/main/DownloadSettingsPage.java:355-376` |
| 并发模型 | ✅ 默认并发 + **AIMD 自适应**（不限速时启用） | ✅ `DEFAULT_CONCURRENCY = min(核数*4, 64)`（64 是上限非默认）；Java 21+ 用虚拟线程 + `Semaphore`，否则回退线程池；设置项 `autoDownloadThreads` 默认 true，手动滑块 1–256 | ECL `ECL/game/Core/Downloader.py`；HMCL `task/FetchTask.java:680-738`、`LauncherSettings.java:500-514` |
| 并行分片下载 | ✅ **有**（按文件大小算片数、只重试失败片） | ❌ **无分片**（确认全仓库无多连接下载） | ECL `ECL/game/Core/Downloader.py:402-421,577-598`；HMCL `FetchTask.java:246-294` |
| 断点续传（字节级） | ❌ **不支持**：临时文件 `"wb"` 截断、无 `Range`、无持久化 `.part` 索引，进程重启从零开始；异常时 `.tmp` 不清理 | ✅ **支持**：`HttpResumeContext` 记录总长/强 ETag/Last-Modified，发 `range: bytes=N-` + `if-range`；前置条件 `accept-ranges: bytes` + identity 编码 + 强校验器；`canResume` 校验 206 + `Content-Range` 精确匹配，任一不满足即从 0 重下；416 丢弃上下文重试 | ECL `Downloader.py:491-519,613-622`；HMCL `FetchTask.java:148-239,340-343,398-404` |
| 重试策略 | ✅ 三层：分片 3 次 / 文件轮次 3 轮（指数退避 2s、4s）/ 元数据 GET（可重试状态码集合 + `retry_delay*2^n`）；httpx 传输层 `retries` 0–5 钳制 | ✅ `DEFAULT_RETRY=5`（共 6 次尝试）+ **固定 200ms**、无指数退避无抖动；416/304 场景额外加一次机会且不 sleep | ECL `Downloader.py:733-760`、`ECL/utils/network.py:30,47-79`；HMCL `FetchTask.java:52,317,398-404,499-502` |
| 限速 | 🟡 有限速器但**未接线**（全仓无 `speed_limit_mb=` 实参，实际均不限速） | ❌ 无 | ECL `Downloader.py:73-118`、`ECL/services/game/base.py:103` |
| 代理 | ✅ **双通道分离**：启动器通道 + `ECL_DOWNLOAD_PROXY` 下载通道（`trust_env=False`，不继承系统代理） | ✅ `ProxyManager` 通过 `ProxySelector.setDefault` 全局注入 JVM；`ProxySelectorWrapper` 保证回环地址不走代理；支持 HTTP/SOCKS/系统代理 | ECL `ECL/utils/network.py:31-44`、`Downloader.py:684-692`；HMCL `setting/ProxyManager.java:37-126,155-159` |
| HTTP/2 | ✅ 下载与预检客户端均启用 | ❌ 未核实（使用 `HttpURLConnection`） | ECL `Downloader.py:686` |
| 完整性校验 | ✅ SHA-1 | ✅ SHA-1/SHA-256 流式计算；`.zip/.jar` 额外做 zip 挂载测试；可插自定义校验钩子；**`x-bmclapi-hash` 响应头可直查本地缓存跳过下载** | ECL `Downloader.py`；HMCL `FileDownloadTask.java:136-138,238-243,282-298`、`FetchTask.java:355-363` |
| 进度与速度上报 | ✅ 字节/文件双模式 + 独立分发线程避免阻塞事件循环 | ✅ `Timer` 每秒广播 `SPEED_EVENT` | ECL `Downloader.py:615-653`；HMCL `FetchTask.java:588-603` |
| 暂停 / 恢复 / 停止 | ✅ `pause()/resume()/stop()` | 🟡 仅取消 | ECL `Downloader.py:806-845` |
| 重定向 | ✅ 已修复开启跟随 | ✅ 手动跟随最多 20 跳，非 HTTP 直接报错 | ECL `c08a236 fix: follow redirects in downloader requests`；HMCL `FetchTask.java:365-393` |

### 3.3 Java 运行时管理

| 功能 | ECL | HMCL | 说明与证据 |
| --- | --- | --- | --- |
| **Java 运行时自动下载** | ❌ **完全不存在**（无任何 java 下载模块；仅有 `JavaScanner.py` 扫描） | ✅ **双来源**：`download/java/mojang/`（Mojang piston-meta，`MOJANG_JAVA_PREFIX = "mojang-" + component`）+ `download/java/disco/`（foojay Disco API，根地址可被 `-Dhmcl.discoapi.override` 覆盖） | ECL §5.1 结论；HMCL `download/java/`（11 个文件）、`JavaDownloadDialog.java:446-464` |
| 可选发行版 | ❌ | ✅ **7 家**：Eclipse Temurin / BellSoft Liberica / Azul Zulu / Oracle GraalVM / IBM Semeru / Amazon Corretto + Mojang 官方；每家带平台×架构矩阵与文件类型（JDK/JRE/JDKFX/JREFX），Liberica 还排除 `-lite` 精简包 | `DiscoJavaDistribution.java:39-81,129-136` |
| 安装目录布局 | — | `<root>/<platform>/<name>/` + **同级 `<name>.json` manifest**；`isInstalled` 即看 manifest 是否存在；Mojang 路径先下到 `.tmp/<name>` 再同盘 move / 跨盘 copy+delete | `HMCLJavaRepository.java:52-80`、`MojangJavaDownloadTask.java:147,178-190` |
| 跨平台安装校验 | — | ✅ Disco 路径解压后校验 `info.getPlatform() == 目标 platform`，不符抛 `Platform is mismatch` 且**不写 manifest、不注册** | `HMCLJavaRepository.java:193-194` |
| 32/64 位与架构兼容 | 🟡 x86 内存上限保护 | ✅ 三层架构概念（系统架构 / 当前 JVM 架构 / 目标 Java 架构）+ `isCompatible` 含翻译层判定（Windows x86_64↔x86、arm64↔x86_64、macOS Rosetta 2）；`forceX86` 场景（ARM64 + 1.6 以下）只保留 x86 Java | `JavaManager.java:116-153,321-334` |
| 版本要求推断 | 🟡 读 `javaVersion.majorVersion` + 追溯 `inheritsFrom` | ✅ **`JavaVersionConstraint` 13 个声明式约束**（强制/建议两级）：VANILLA、GAME_JSON、MODDED_JAVA_7/8/16/17/21、CLEANROOM、LAUNCH_WRAPPER（必须 ≤Java 8）、VANILLA_JAVA_8_51、VANILLA_LINUX_JAVA_8、VANILLA_X86、MODLAUNCHER_8（JDK-8273826 小版本上限） | `JavaVersionConstraint.java:33-223` |
| Java 选择策略 | ✅ 有探测与校验 | ✅ `JavaVersionType` **4 值**（AUTO/VERSION/DETECTED/CUSTOM）；`findSuitableJava` 两级择优：先找「零建议违规」，退化到「零强制违规」，同主版本取更新补丁版 | `JavaVersionType.java:32-42`、`JavaManager.java:301-359` |
| 本机扫描来源 | ✅ `JavaScanner` | ✅ 注册表 4 键 + Program Files 双目录 + Linux `/usr/lib{,32,64}/jvm`、`/usr/java`、SDKMAN + macOS JavaVirtualMachines/Homebrew/Xcode + Minecraft 官方 runtime 目录 + PATH（**Windows 跳过 Oracle 自动更新存根**）+ `HMCL_JRES` + `~/.jdks` + 用户手动添加 + 当前 JVM；**无 WMI** | `JavaManager.java:376-484` |
| 扫描缓存 | ✅ 结果缓存于内存 | ✅ `javaCache.json`：**廉价变更检测键** = java 可执行文件 `size`+`mtime` + `release` 文件 SHA-1（无 `release` 时退化用 `rt.jar`），键变才重探，**不 spawn 子进程**；版本门控 + 单条损坏隔离 + 只在需要时原子写回 | `JavaManager.java:514-723` |
| 扫描并发 | ✅ **单线程串行**（与 HMCL 相同）：`scan()` 顺序调用平台扫描 → PATH 扫描 → 用户路径循环 → 当前 JVM，无线程池；失败项单独记账不中断 | 🟡 扫描内部**单线程串行**；启动扫描异步（不在 FX 线程）+ `CountDownLatch` 阻塞等待 + `refresh()` 忽略缓存全量重扫 | ECL `ECL/game/Utils/JavaScanner.py:61-91`；HMCL `JavaManager.java:156-207,503-508` |
| Java 手工安装 / 归档导入 | ❌ | ✅ `JavaInstallPage` + `onInstallArchive`（归档解压 → `JavaInfo.fromArchive` → 逐文件复制并算 SHA-1，符号链接与可执行位都处理） | `JavaManagementPage.java:159-177`、`JavaInstallTask.java:60-107` |
| Java 卸载 | ❌ | ✅ 删 manifest + 删目录 | `JavaManager.java:244-272` |
| Java 禁用与恢复 | ❌ | ✅ `disabledJava` 集合，扫描阶段直接跳过；`JavaRestorePage` 管理该集合 | `JavaManager.java:690-694`、`JavaRestorePage.java:55-62` |
| 无合适 Java 时降级 | 🟡 报错 `JAVA_RUNTIME_INVALID` | ✅ 允许带「建议违规」运行，仅在「强制违规」时才不可用 | `JavaManager.java:359` |
| 两条来源间降级 | — | 🟡 **Mojang 路径失败后不会自动回退 Disco**；但 Mojang 的每个文件 URL 都经 `getDownloadCandidates`，因此自动获得多镜像回退 | `MojangJavaDownloadTask.java:62,73,117,148` |

> **这是本次对比中最硬的一处功能性差距**：ECL 只能「找到并使用」Java，无法「安装」Java；HMCL 提供 7 家发行版、13 条版本兼容约束、廉价缓存探测与完整的安装/卸载/禁用/恢复闭环。

### 3.4 版本与加载器安装

| 功能 | ECL | HMCL | 说明 |
| --- | --- | --- | --- |
| 原版安装 | ✅ 版本清单分类 Release/Snapshot/**FoolDays**/Beta/Alpha/All | ✅ 分类 全部/正式/快照/愚人节/远古，**愚人节版本有专用图标与标签** | ECL `ECL/services/game/catalog.py:35-64`；HMCL `ui/download/VersionsPage.java:345-359` |
| 可安装加载器 | 🟡 **5 个**：vanilla / forge / neoforge / fabric / quilt | ✅ **11 个**：+ **OptiFine / LiteLoader / Cleanroom / LegacyFabric / Fabric API / Quilt API(QSL)**。统一抽象：每个加载器是 `GameComponentType` 枚举成员，列表走 `DownloadProvider.fetchVersionsAsync`，安装走 `ComponentRemoteVersion.getInstallTask(...) → Task<GameInstancePatch>` | ECL `ECL/services/game/install.py:146-147` 白名单；HMCL `game/GameComponentType.java:43-77`、`download/ComponentRemoteVersion.java:102` |
| 组合冲突显式建模 | ❌ 无 | ✅ `UnsupportedInstallationException` 4 类拒绝码：`UNSUPPORTED_LAUNCH_WRAPPER=1`、`FORGE_1_17_OPTIFINE_H1_PRE2=2`、`FABRIC_NOT_COMPATIBLE_WITH_FORGE=3`、`CLEANROOM_NOT_COMPATIBLE_WITH_FORGE=4`；且 OptiFine 官方要求最后安装、LiteLoader 要求在 Forge 之后 | `download/UnsupportedInstallationException.java:33-40` |
| 自建加载器索引 | ❌（直接用官方源） | ✅ Forge 走 `hmcl.glavo.site/metadata/forge/`（HMCL 自建，官方无稳定索引）、Cleanroom 同源；**OptiFine 走 HMCL 自建 BMCLAPI 镜像 `/optifine/{mcver}`**（官方无公开 API）；**LiteLoader 用内置资源** `/assets/liteloader/versions.json`（官方源已失效） | `ForgeRemoteVersion.java:51`、`OptiFineRemoteVersion.java:58-88`、`LiteLoaderRemoteVersion.java:44-85` |
| 仅可识别不可安装 | ✅ 识别 optifine / liteloader / cleanroom / legacyfabric / babric（`SearchMinecraft.py:180-201` 判定顺序即优先级） | — | 两侧差异的本质 |
| Forge 安装方式 | ✅ 下载 installer.jar → `LoaderInstaller.install_forge()` | ✅ 新/旧安装器双实现（`ForgeNewInstallTask` / `ForgeOldInstallTask`） | ECL `ECL/game/Core/LoaderInstaller.py:132+` |
| NeoForge 安装方式 | ✅ 优先读 `install_profile.json` 的 `processors` 逐条执行（占位符替换后 subprocess），无 processors 时回退 Forge 路径 | ✅ 独立实现 | ECL `LoaderInstaller.py:37-131` |
| Fabric / Quilt 安装 | ✅ 直接拉 profile JSON 写入版本 JSON（无需执行安装器）；Fabric 可自动取最新加载器版本并可选装 Fabric API 到 `mods/` | ✅ 独立实现 | ECL `ECL/game/Core/GetGames.py:151-198,301-367` |
| OptiFine 安装 | ❌ | ✅ 且支持与 Forge 组合 | HMCL `download/optifine/` |
| LiteLoader 安装 | ❌ | ✅ 且内置 `liteloader/versions.json` 资源 | HMCL `download/liteloader/`（8 个文件） |
| Cleanroom / LegacyFabric | ❌ | ✅ | HMCL `download/cleanroom/`、`download/legacyfabric/` |
| **安装事务化** | 🟡 任务级取消 + 临时文件原子提交，但无整体快照/回滚 | ✅ `GameRepositoryDraft` 快照 → 写 → `commit`/`abort`，失败不留半成品 | HMCL `game/GameRepositoryDraft.java`、`DefaultGameRepositoryDraft.java` |
| 版本 JSON 继承合并 | ✅ 支持 `inheritsFrom`，基础版本缺失时自动补装 | ✅ **已重构为 patch 列表 + priority 模型**（取代继承链）：`GameInstancePatch` 带 priority（MC=0 / OptiFine=10000 / 加载器=30000 / LiteLoader=60000），`patches` 顺序即合并顺序，`reconstructByPatches()` 摊平，`removeComponent(type)` 回退；`inheritsFrom` 仅作兼容读取 | ECL `ECL/services/game/launch.py:836-849`；HMCL `game/GameInstanceManifest.java:41-91,671-689`、`game/GameInstancePatch.java:38-67` |
| 版本号比较引擎 | ❌ **没有 Minecraft 版本号比较引擎**（`version_key`/`LooseVersion`/`natural_sort` 零命中）。`updates.py:31-87` 的比较是**启动器自身 SemVer**；`plugins/dependencies.py:95` 用 `packaging.version` 面向插件依赖。游戏版本清单**沿用上游顺序不排序**。**⚠️ 实际缺陷**：唯一对 MC 版本排序处在 `resources.py:1021-1029`（CurseForge）与 `:1067-1075`（FTB），用的是内建 **字典序** `sorted()`，实测 `1.20.10` 会排在 `1.20.2` **之前**（与数值序相反）；且 `:1027` 的正则 `\d+(?:\.\d+){1,2}` 会**丢弃 `1.21-pre1` 这类带后缀版本号**。影响范围目前仅项目详情的 `gameVersions` 展示字段，不参与选版/依赖判定 | ✅ `GameVersionNumber` 类型化引擎（`sealed` class）：`Release` 正则取 major/minor/patch 为 **int** 后 `Integer.compare` 逐段比较 → `1.20.10`(patch=10) > `1.20.1`(patch=1) 正确；pre/rc/snapshot 由 `ReleaseType` 枚举序 + `eaVersion` 比较；`17w43a` 类快照打包为 int 并用 `assets/game/versions.txt` **二分比较**；别名/愚人节版本走 `Special` + `version-alias.csv`；对外 API `compare/between/atLeast/atMost` | ECL `ECL/services/game/resources.py:1021-1029,1067-1075,1044,1088`、`updates.py:31-87`、`catalog.py:35-77`、`GetGames.py:8-83`；HMCL `util/versioning/GameVersionNumber.java:82-96,312-331,348-363,525-565,680-933`（消费点 `JavaVersionConstraint.java:76-180`、`DefaultLauncher.java:307,415,507,519`） |
| 完整性校验与修复 | ✅ `game_instance_files_check` / `_repair`（**显式调用、全量哈希校验**） | ✅ 但语义不同：`checkGameCompletionAsync` 为真正入口；**哈希级校验仅在「上次异常退出」时触发**（实例根 `.abnormal` 标记一次性消费），正常启动走「文件存在即跳过」快速路径；`NOT_CHECK_GAME`（默认 false）则完全跳过校验 | ECL `ECL/services/game/install.py`；HMCL `LauncherHelper.java:159,166-174,1033-1036`、`GameSettings.java:440-447` |
| 实例写事务 | 🟡 任务级取消 + 临时文件原子提交 | ✅ `GameRepositoryDraft`：**一个仓库只允许一个 draft**，内存写集 + commit 时才落盘；commit 顺序为重命名→建目录→移走删除根→复制主 jar→写 manifest→发布 seal 快照；失败按逆序回滚 + `addSuppressed`；**显式声明共享库/资源缓存不在回滚边界内**（幂等） | HMCL `game/GameRepositoryDraft.java`、`DefaultGameRepositoryDraft.java` |
| 卸载 | ✅ 仅允许删 `versions/<name>` 并做父目录逃逸校验 | ✅ 删除移入 `_removed` 目录 | ECL `ECL/services/game/install.py:468-486` |

### 3.5 整合包

| 功能 | ECL | HMCL | 说明 |
| --- | --- | --- | --- |
| 可导入格式 | ✅ **6 种**：Modrinth `.mrpack` / CurseForge / MCBBS / HMCL / MultiMC / ECL-legacy | ✅ **6 个 Provider**：Curse / Mcbbs / Modrinth / **MultiMC** / Server / **HMCL**（后者的清单为 `modpack.json` + `minecraft/pack.json`） | ECL `ECL/services/game/modpack.py:57-73`；HMCL `game/HMCLModpackProvider.java:38-96` |
| 类型判定方式 | 🟡 按特征逐个尝试 | ✅ **硬编码固定顺序** `Mcbbs → Curse → Modrinth → HMCL → MultiMC → Server`，源码注释明确警告「顺序是必要的，不要改成遍历」——原因之一是 MCBBS 导出时会额外写一份 Curse 格式 `manifest.json`，故 MCBBS 必须先判定 | `game/ModpackHelper.java:93-110`、`mcbbs/McbbsModpackExportTask.java:148-151` |
| 在线安装 | ✅ Modrinth + CurseForge + **FTB**（`api.feed-the-beast.com`，注释标注非官方文档） | ✅ CurseForge + Modrinth | ECL `modpack.py:713,775`、`resources.py:142-143` |
| CurseForge API Key | ✅ 可选，未配置仅禁用该源并弹一次提示 | ✅ `-Dhmcl.curseforge.apikey` / jar 清单属性 | ECL `ECL/api/bridge.py:852-860`；HMCL `CurseForgeRemoteAddonRepository.java:62` |
| 导出格式 | 🟡 **仅 Modrinth** | 🟡 **3 种**：MCBBS / Server / Modrinth（全仓仅 3 个 `*ExportTask`）+ **导出向导三页**（类型选择 / 信息填写 / 文件树多选）。**CurseForge 与 MultiMC 导出已不存在**：格式页只建 3 个按钮、`ExportWizardProvider` switch 只处理 3 类、i18n 键 `modpack.type.multimc.export` 已成无引用死字符串 | ECL `modpack.py:1093-1094`；HMCL `McbbsModpackExportTask`/`ModrinthModpackExportTask`/`ServerModpackExportTask`、`ui/export/ModpackTypeSelectionPage.java:56-58`、`ExportWizardProvider.java:94-100`、`I18N_zh_Hans.properties:864` |
| 导出文件分组预置 | ❌ | ✅ 预置 `mods`/`resourcepacks`/`shaderpacks`/`saves`/`config`/`scripts`/`dumps`/`blueprints`/`mods/VoxelMods` 等 | `ui/export/ModpackFileSelectionPage.java:240-248` |
| 可选文件安装 | ❌ **不存在**：`install_modpack_online(source, project_id, file_id, game_path, new_version_id)` **无任何文件选择/排除参数**，前端入口也无勾选步骤。注意 ECL 代码里的 `optional` 是 Modrinth `env.client` 语义（客户端是否需要该文件），与「可选文件勾选」是不同概念 | ✅ `OptionalFilesPage` 是**独立向导页**：用 `excludedFiles`（未勾选的键集合，空集 = 全部安装）+ 逐项 `BooleanProperty` 镜像 + `CELL_HEIGHT=48`/`MAX_LIST_HEIGHT=320` 自适应高度，带 `retry` 回调与 loading/successful 状态 | ECL `ECL/services/game/modpack.py`（`install_modpack_online` 签名）、`:228,310,400,647-648`；HMCL `ui/download/OptionalFilesPage.java:55-105` |
| 下载失败呈现 | ✅ 逐文件失败可重试 | ✅ **聚合模式**：单文件失败直接显示该异常；多文件失败构造 `Failed to download Java` 并用 `addSuppressed` 保留完整失败链（与 `FetchTask` 同款模式） | HMCL `ui/main/JavaDownloadDialog.java:178-215` |
| MultiMC json-patch 引擎 | ❌ | ✅ `multimc/` 6 个文件实现 json-patch 应用 | HMCL `modpack/multimc/` |
| 哈希校验下载 | ✅ 下载后 SHA-512 与清单比对 | ✅ 按哈希反查避免重传 | ECL `modpack.py:1008-1043` |
| 解压安全 | ✅ 路径逃逸校验（`_safe_pack_relative_path`） | 未核实 | ECL `modpack.py:156` |

### 3.6 实例 / 版本管理

| 功能 | ECL | HMCL | 说明 |
| --- | --- | --- | --- |
| 扫描机制 | 🟡 **轮询**守护线程 `ECL-VersionWatcher` + 目录指纹缓存 + 防抖 | ✅ 目录扫描（`GameDirectoryManager`） | ECL `ECL/services/game/scan.py:156-251` |
| 指纹缓存 | ✅ `(相对路径, mtime_ns, size)` 不可变快照，含插件来源 watch 路径 | 未核实 | ECL `scan.py:128-189` |
| 变更事件 | ✅ `game:versions_changed` | — | ECL `scan.py:242-243` |
| 多游戏目录 | ✅ | ✅ `GameDirectoryManager` 多仓库 | 两侧均有 |
| 版本隔离 | ✅ 三态 | ✅ `DefaultIsolationType` = ALWAYS / MODDED / NEVER | ECL `docs/current-instance-isolation-inherit-plan.md` |
| 第三方实例兼容 | ✅ **读取 PCL/PCL-CE 与 HMCL 实例**（`PCL/Setup.ini` 与 `.hmcl/config/instance-game-settings.json`），只读、编码容错 utf-8-sig/utf-8/gb18030、12 项 HMCL 图标映射、插件可注册新来源 | — | ECL `ECL/services/game/instance_compat.py:30-247` |
| 实例图标 | ✅ 白名单 6 个加载器图标 + 自定义 | ✅ `GameInstanceIconType`（含愚人节图标）、HMCL 12 项图标映射 | ECL `instance_compat.py:95-96`；HMCL `setting/GameInstanceIconType.java` |
| 分组 / 分类 | ✅ | ✅ | 两侧均有 |
| 运行统计 | ✅ 启动次数等（`version_stats.py`） | ✅ `VersionLaunchCount`（PCL 兼容读取） | ECL `ECL/services/game/version_stats.py` |
| 三态继承设置 | ✅ inherit / auto / manual + schema 版本保护 | ✅ `InheritableProperty`（继承/覆盖/恢复）+ 实例级描述文案 | ECL `frontend` `instanceSettings.ts`；HMCL `ui/game/GameSettingsPage.java:2232-2267` |
| 设置预设 | ❌ | ✅ `GameSettingsPresets` + `DEFAULT_GAME_SETTINGS_PRESET` | HMCL `setting/GameSettingsPresets.java` |
| 克隆 / 导入 / 导出 | ✅ | ✅ | 两侧均有 |
| 删除策略 | ✅ 直接删除（含路径校验） | ✅ 移入回收式目录 | 差异见上 |
| Windows 快捷方式 | ✅ 桌面/指定 `.lnk`、实例图标、稳定 ICO 缓存、记录数据目录 | ❌ | ECL `ECL/services/instance_shortcuts.py`、`docs/launcher-cli.md:45` |
| 添加实例文件夹后自动选中 | — | ✅（最新提交 #6912） | `4118fa972` |

### 3.7 启动能力

| 功能 | ECL | HMCL | 说明 |
| --- | --- | --- | --- |
| 内存设置 | ✅ 含 `-Xms=-Xmx` 锁定 | ✅ `MAX_MEMORY` / `MIN_MEMORY` / `PERM_SIZE` / `AUTO_MEMORY` | 两侧均有 |
| 内存自动分配 | 🟡 前端常量 `AUTO_MEMORY_DEFAULT=4096`（文档自述「实为固定值」） | ✅ 按系统内存计算 | ECL `docs/feature-recommendations/implementation-plan.md:23-27` |
| 低内存 auto agent | ❌ | ✅ `ALLOW_AUTO_AGENT` 低内存自动 agent | HMCL `setting` |
| GC / JVM 优化选项 | 未核实 | ✅ `NO_JVM_OPTIONS` / `NO_OPTIMIZING_JVM_OPTIONS` | HMCL |
| 进程优先级 | ✅ **真实生效**：Windows 用 `psutil` 的 `IDLE/BELOW_NORMAL/NORMAL/ABOVE_NORMAL/HIGH_PRIORITY_CLASS`，POSIX 用 `nice` 值映射；5 档 | 🟡 **Windows 上完全未生效**：`DefaultLauncher.java:63-95` 中 `cmd /C start /B /low` 等四个 Windows 分支**全部被注释掉**，只有 Linux/BSD/macOS 的 `nice -n ±N` 生效 | ECL `ECL/game/Core/InstancesManager.py:12-30`；HMCL `launch/DefaultLauncher.java:63-95` |
| 强制高性能 GPU | 未核实 | 🟡 `HMCL_FORCE_GPU` 作用于**启动器自身 JavaFX 的 `prism.forceGPU`**（`HMCL/.../Launcher.java:398-404`），**与游戏进程 GPU 无关** | HMCL `Launcher.java:398-404` |
| 预启动命令 | ✅ | ✅ `PROPERTY_PRE_LAUNCH_COMMAND` | 两侧均有 |
| **Wrapper 命令** | ✅ | ✅ `COMMAND_WRAPPER` | 两侧均有 |
| **后退出命令** | ✅ 逐行执行 | ✅ `POST_EXIT_COMMAND` | 两侧均有 |
| **自定义环境变量** | ✅ `create_instance` 支持自定义 env | ✅ `ENVIRONMENT_VARIABLES`（多行解析） | ECL `ECL/game` `03da51c`；HMCL |
| 窗口标题自定义 | ✅ | 🟡 `WINDOW_TYPE` | ECL `docs/launcher-cli.md` |
| 窗口尺寸 / 全屏 | ✅ | ✅ `WIDTH`/`HEIGHT` | 两侧均有 |
| 启动器可见性 | ✅ `none`/`minimize`/`quit` | ✅ `LAUNCHER_VISIBILITY` 四值 | 两侧均有 |
| QuickPlay | ✅ 服务器 / 世界（`--server` / `--world`） | ✅ **三类**：单人 / 多人 / Realms，含版本门槛判定与旧版本降级 | ECL `docs/launcher-cli.md:33`；HMCL `PROPERTY_QUICK_PLAY*` |
| 渲染器选择 | 未核实（存在软回退概念） | ✅ `GRAPHICS_BACKEND` / `OPENGL_RENDERER` / `VULKAN_RENDERER` / 原生 GLFW 或 SDL / 原生 OpenAL；`--graphicsBackend` 注入门槛为游戏版本 ≥ `26.2-snapshot-2` | HMCL `launch/DefaultLauncher.java:414-418,475-481` |
| Natives 补丁 | ❌ 无 Natives 补丁机制 | ✅ `NativePatcher`（290+ 行，规则来自打包资源 `assets/natives.json`）在解压 natives 时按需修补；natives 解压只处理大小不同的条目并跳过 `.sha1`/`.git`。**已核实其设置项全部被真实消费**：`notPatchNatives`（`NativePatcher.java:127`）、`useCustomNatives`（`:82`）、`useNativeGLFWorSDL`（`:100`）、`useNativeOpenAL`（`:101`）——注意常量名检索会漏检，因为它们经由 `GameSettings::xxxProperty` getter 读取 | HMCL `util/NativePatcher.java:79-127`、`launch/DefaultLauncher.java:457-493` |
| 日志环形缓冲 | ✅ + **控制台 stdin 输入** | ✅ 日志窗口 + 分级 | ECL `ECL/api/bridge.py`（`process_input`） |
| 崩溃检测与分析 | ✅ 结构化管道：三源 Mod 索引 + 堆栈分析器 + **移植 HMCL 规则**（命名捕获组）+ Mod 归因 + 前端展示 | ✅ `CrashReportAnalyzer` + `GameCrashWindow` | ECL `ECL/services/game/crash/`；HMCL |
| 启动取消 | ✅ | ✅ | 两侧均有 |
| **启动器命令行参数** | ✅ **完整**：`--launch/--server/--world/--memory/--java-path/--isolation/--process-priority/--lock-memory/--jvm-arg/--game-arg/--launcher-visibility/--open-page/--data-dir` 等 | ❌ **完全没有**（仅 `HMCLBoot --apply-to` 内部自更新参数） | ECL `docs/launcher-cli.md:20-39`、`ECL/cli.py:107-164`；HMCL `ui/main/...`（报告 §11.4/§14 明确无） |
| **单实例互斥与请求转发** | ✅ 回环 TCP 探测 + 快捷启动转发 + 重复合并 + 进程级配置转发拒绝 | ❌ **完全没有**（`FileLock` 命中均与实例互斥无关） | ECL `ECL/services/single_instance.py`；HMCL 报告 §11.4 |

### 3.8 资源管理

| 功能 | ECL | HMCL | 说明 |
| --- | --- | --- | --- |
| Mod 列表 / 启停 / 增删 | ✅ `.disabled` 后缀切换；4 种元数据格式解析（Fabric/Quilt/NeoForge/Forge） | ✅ `LocalMod`/`LocalModFile` + 5 种元数据（含 LiteMod） | ECL `ECL/services/game/resources.py:357-444,648-654` |
| 资源包 / 光影 / 数据包 | ✅ **本地管理完整**：资源包启停**同时**改名 `.disabled` **并**改写 `options.txt` 的 `resourcePacks` 列表；光影启停改写 `optionsshaders.txt`；数据包启停改写世界 `level.dat` NBT | 🟡 **光影包无本地管理类型**：`addon/` 下只有 `mod`/`resourcepack`/`datapack`/`repository`/`meta`，**无 shaderpack 包**，也没有 `ShaderPackListPage`，只能「打开文件夹」；资源包启停**只改写 `options.txt` 列表、不改名**；`RemoteAddon.Type` 含 `RESOURCE_PACK`/`SHADER_PACK` 仅为**远程可搜索/可下载**类型 | ECL `resources.py:649-672`、`:657-662`；HMCL `addon/` 目录结构、`ui/instances/ResourcePackListPage.java`、`LocalAddonManager.java:45,54-56` |
| 数据包范围 | ✅ **全局 + 世界内**（`world / "datapacks"`） | 🟡 **仅世界内**：`DataPackListPage` 由 `world.getFile().resolve("datapacks")` 构造，**无全局数据包管理** | ECL `resources.py:328-332`；HMCL `ui/instances/DataPackListPage.java` |
| 资源包图标 | ✅ 读包内 PNG，失败保留条目 | ✅ 图标读取 + 延迟加载缓存 | ECL `resources.py:279-317` |
| 数据包区分全局/世界内 | ✅ 走 `saves/<world>/datapacks` | ✅ `DataPack` 类 | 两侧均有 |
| 在线搜索源 | ✅ Modrinth + CurseForge + **FTB** + 中文关键词先译英文再搜（`mcmod.py` MC 百科离线译名） | ✅ Modrinth + CurseForge | ECL `resources.py:826-1520`、`mcmod.py` |
| 支持的在线资源类型 | ✅ mod / resourcepack / shaderpack / datapack / world / modpack | ✅ MOD / MODPACK / RESOURCE_PACK / SHADER_PACK / WORLD / **CUSTOMIZATION（主题包）** | ECL `resources.py`；HMCL `RemoteAddon.java:206-213` |
| 批量哈希反查更新 | 🟡 有 `identify_resource_hash`（SHA-512 严格校验 + Modrinth `version_files` + CurseForge，歧义时不猜测来源），但**未见定时/一键批量检查任务** | 🟡 `AddonCheckUpdatesTask` + `AddonUpdatesPage`，但**纯手动触发、无缓存、无定时任务**；且**逐文件**反查（Modrinth 单文件 SHA-1 GET；CurseForge `POST /v1/fingerprints/432` 单指纹 MurmurHash2），**不是批量接口**；同时查双源取发布时间更晚者；用户手改过的文件哈希不匹配则静默跳过（不误报）；只有 `LocalModFile` 与 `ResourcePackZipFile` 实现 `checkUpdates`（目录型资源包不参与） | ECL `resources.py:1504-1532`、IPC `mods_updates_*`；HMCL `ui/instances/AddonUpdatesPage.java`、`AddonCheckUpdatesTask.java:42-56` |
| 依赖解析 | 🟡 仅**标记** `missingDependencies`，无跳转安装路径 | 🟡 有 **7 类依赖模型**（REQUIRED/OPTIONAL/TOOL/**INCLUDE**/EMBEDDED/INCOMPATIBLE/BROKEN）+ CurseForge 6 种 relationType 映射 + 版本详情按类分组渲染；依赖项可作为**跳转下载入口**（前置缺失可一键跳转）；任一依赖 `BROKEN` 时顶部插入警告标签；`INCOMPATIBLE` 跳过。**但同样不自动安装前置依赖**（源码有 `// TODO: Massive tasks may cause OOM.`） | ECL `resources.py:364-441,544`；HMCL `addon/RemoteAddon.java:54-119`、`ui/instances/DownloadPage.java:570-624` |
| 重复检测 | ✅ `duplicateHash`（SHA-512）+ `duplicateProjectId` | ✅ mod 以 `(modId, ModLoaderType)` 为键，`LocalModFile` 按文件名（忽略大小写）去重；**jar-in-jar**：Forge 新版递归解析 `META-INF/jarjar/metadata.json` 内嵌 mod；扫描进入 `mods` 子目录（跳过 `.connector`） | ECL `resources.py:530-543`；HMCL `addon/meta/ForgeNewModMetadata.java:232-303`、`ModManager.java:70,230-244` |
| 更新时保留旧版 | ❌ | ✅ `OLD_EXTENSION = ".old"`：mod 更新时把旧文件改名为 `.old` 并从列表移除，`setOld(false)` 可还原（**可回滚**）；`LocalModFile.keepOldFiles()` 返回 true | HMCL `addon/LocalAddonManager.java:48,109-156`、`LocalModFile.java:169-185` |
| 搜索排序 | ✅ CurseForge `sortField` 映射 5 种 | ✅ 分类/排序/分页/游戏版本/加载器过滤 | ECL `resources.py:145-152` |

### 3.9 世界 / 存档 / 服务器 / 截图 / 原理图

| 功能 | ECL | HMCL | 说明 |
| --- | --- | --- | --- |
| 世界列表与详情 | ✅ | ✅ `World` 解析 `level.dat`（种子/游戏模式/难度/版本/游玩时长） | 两侧均有 |
| 世界 NBT 改写 | 🟡 `patch_world` 支持难度/作弊/锁定**三个字段**（**可写**） | 🟡 `NBTEditorPage` 提供完整 NBT 树形**浏览**，但 `save()` 方法体是 `// TODO` → **目前只读、不可编辑**；底层为 `org.glavo:HelloNBT 0.4.0`，`NBTFileType` 还能读 `.mca`/`.mcr` 区域文件成 `ChunkRegion` | ECL `docs/feature-recommendations/implementation-plan.md:124-146`；HMCL `ui/nbt/NBTEditorPage.java:80-86,130-131` |
| 世界复制 / 导入 / 导出 | ✅ 含**目录型存档导入** | ✅ 导入**强制 `*.zip`**，先解析校验（`Data`/`LevelName`/`LastPlayed` 三级字段）再让用户确认世界名；导出确为 zip（`Zipper.putDirectory`，以世界名为顶层目录）；复制排除 `session.lock`；重命名 = 改 `LevelName` + move 目录 | ECL `4b2a93c`、IPC `game_world_*`；HMCL `HMCLCore/.../game/World.java:244-255` |
| 存档锁处理 | 未核实 | ✅ 探测 `session.lock` 抢占并**把 `readOnly` 绑定到所有写盘控件**（只读模式贯通 UI） | HMCL `World.java` |
| **世界备份** | 🟡 有备份列表 `game_world_backup_list` | 🟡 `WorldBackupsPage` + `WorldBackupTask`：**手动触发**的整目录 zip 快照，位置 `<实例运行目录>/backups`，命名 `yyyy-MM-dd_HH-mm-ss_<世界名>.zip`（同秒追加序号、`CREATE_NEW`、最多 256 次尝试）；**无自动清理、无保留份数上限、无恢复按钮**（恢复 = 用「添加」导入该 zip），且备份不可锁定。i18n 无 `world.backup.restore` 键即为佐证 | ECL `ECL/api/registry.py`；HMCL `GameInstance.java:158-163`、`ui/instances/WorldBackupsPage.java`、`I18N.properties`（`world.backup*` 共 8 键） |
| 存档种子编辑 | ✅ `world_seeds.py` + IPC（`ef30fa4`/`d1d32a4` 修复精度） | 🟡 只读种子 + **ChunkBase 种子地图一键打开**（纯 URL 构造器，种子放 URL fragment，不经 HMCL 服务器；4 个应用：种子地图/要塞/下界要塞/末地城，各带硬编码版本数组做向下取整匹配，超大生物群系加 `_lb` 后缀） | ECL `ECL/services/game/world_seeds.py`；HMCL `util/ChunkBaseApp.java` |
| **服务器列表管理** | ✅ `servers.py`（238 行）：读写 `servers.dat` + 批量状态查询（`mcstatus`） | ❌ **无**（`servers.dat` 仅出现在整合包导出文件清单里） | ECL `ECL/services/game/servers.py`；HMCL grep 结果 |
| **截图管理** | ✅ `screenshots.py`：缩略图 / 复制 / 删除 / 设为封面或背景 | ❌ **无独立截图管理**（仅出现在整合包文件分组中） | ECL `ECL/services/game/screenshots.py`；HMCL grep 结果 |
| 原理图 | ✅ `schematics.py` **1168 行**：格式解析 + **3D 会话查看器**（worker 建面、chunk 流式加载、atlas 合成、Y 层切片）+ 材料导出 | 🟡 `schematic/` 6 个文件 + `Schematic` sealed 类族解析 `.litematic`（最完整）/ `.schematic`｜`.schem`（legacy + Sponge 双格式）/ `.nbt`（原版结构）；**仅 Litematica 有内嵌 ARGB 缩略图预览**，其余降级为图标；`SchematicsPage` 管理并与 NBT 浏览器联动；**无 3D 渲染** | ECL `ECL/services/game/schematics.py`；HMCL `HMCLCore/.../schematic/` |
| `options.txt` 读写 | ✅ `instance_options.py` + 资源包/光影启停改写 | ✅ `ResourcePackManager` 改写 | 两侧均有 |
| 实例资源文件类型校验 | ✅ `resource_files.py` 后缀白名单策略类 | 未核实 | ECL `resource_files.py:56-64` |

### 3.10 联机

> **核实说明**：HMCL 的联机核心（burningtnt/Terracotta，AGPL）是**外部预编译二进制**，仓库内只有客户端托管代码，核心源码不在本仓库。因此「穿透类能力」（邀请码算法、核心内部 TCP 分帧、核心是否做 UPnP/组播）**无法确证**；本节仅确证「HMCL 侧有没有」。HMCL 架构为「拉起外部二进制 + 通过 `http://127.0.0.1:<port>` 本地 HTTP API 控制」（`TerracottaManager.java:117,256`）。

| 功能 | ECL（florolding + connector） | HMCL（Terracotta） | 说明与证据 |
| --- | --- | --- | --- |
| 实现方式 | ✅ EasyTier 以 **Python 扩展（`easytier-pyo3`）内嵌**，无外部二进制 | ✅ 启动**外部核心二进制** + 本地 HTTP API 控制；EasyTier **内嵌在核心内**（不单独下载，证据：崩溃文案 `guest_et_crash`/`host_et_crash` 点名 EasyTier，`about/deps.json:82-86` 列 EasyTier 为 LGPL 3.0 依赖） | ECL `ECL/services/florolding/`；HMCL `terracotta/TerracottaManager.java:117,256` |
| 联机核心的分发与校验 | 🟡 EasyTier 依赖随包分发，**无独立下载/校验流程** | ✅ **完整托管**：`assets/terracotta.json` 是核心二进制的下载与校验清单（`version_latest=0.4.2`、9 个平台 classifier、1 条 GitHub + 3 条 CN 镜像 + 2 条第三方链接）→ 下载（约 8 MiB）→ **逐文件 SHA-512 校验** → 解包安装 → 启动 → **失败回滚**；国内优先 Gitee/cnb.cool/alist；macOS 用 `osascript` 提权装 pkg；支持**拖入 `.tar.gz` 离线安装**（精确文件名校验）；`LEGACY_VERSION` 升级与旧版清理 | HMCL `assets/terracotta.json`、`terracotta/TerracottaBundle.java`、`TerracottaMetadata.java:123` |
| 节点来源 | ✅ 自动获取 / 追加自定义 / 仅自定义；支持 `tcp/udp/quic/faketcp/ws/wss` 6 种协议 | 🟡 **用户无法自定义节点**：节点列表来自**硬编码远端 URL** `https://terracotta.glavo.site/nodes`，按区域自动筛选（仅中国大陆收 `region=CN`），多节点以重复 `public_nodes` 参数下发并本地缓存；非中国大陆显示不受支持警告。setting 中联机相关持久化项**只有 `terracottaAgreementVersion`** | ECL `connector_nodes.py`、`docs/launcher-cli.md:47`；HMCL `terracotta/TerracottaNodeList.java:38` |
| 房间创建 / 加入 | ✅ | ✅ 房主入口要求**必须有正在运行的 MC 进程**，否则先弹窗提示启动游戏；房客弹窗输入邀请码（**仅非空校验**），邀请码自动复制到剪贴板；文案明确兼容 PCL CE | ECL `ECL/services/connector.py`；HMCL `terracotta/`、`ui/terracotta/` |
| 房间内成员管理 | ✅ 支持踢人 | ❌ **无踢人、无房主转让、无成员权限管理**（grep `kick`/`ban`/`removePlayer`/`transferHost` 零命中）；房主只能整体关房。玩家列表**只读**（名字 + vendor + 房主/你/房客标签），按 `profile_index` 增量刷新不闪屏 | ECL `ECL/services/connector.py`；HMCL `terracotta/`（全仓检索确证不存在） |
| 连接质量反馈 | ✅ NAT 类型检测（线程池异步） | 🟡 **无 NAT 检测**（无 `NatType`/STUN/Full Cone/Symmetric 代码），只有一句解释文案「连接成功率由房主和房客的 NAT 类型推算得到」+ 核心给出的**连接难度 4 档**（EASIEST→TOUGH） | ECL `ECL/api/connector.py`；HMCL `terracotta/TerracottaState.java` |
| NAT 类型检测 | ✅ `connector_nat_type` | ❌ **未实现** | 同上 |
| 端口探测 / 端口映射 | ✅ `connector_detect_ports` + `connector_search_mc_port` | ❌ **无 UPnP/NAT-PMP/端口映射/端口可达性探测**（代码中 `port` 全是本地控制端口） | ECL `ECL/api/connector.py`；HMCL 全仓检索确证不存在 |
| 局域网广播伪装 | ✅ 绑定 MC 端口并向 UDP 播组广播 | ❌ **无实现**（`multicast`/`224.0.2.60`/`LanServer`/`25565` 零命中）；LAN 扫描仅存在于状态名与文案，实现在核心内 | ECL florolding（`771924c`）；HMCL 全仓检索确证不存在 |
| TCP 转发与半包/粘包 | ✅ 按长度头读取完整请求与响应 | ❌ **无任何 TCP 代理/隧道/自定义分帧**（`terracotta` 包内无 `socket`/`java.net`；`readNBytes`/`DataInputStream` 只出现在 PNG 解码） | ECL florolding（`c4d38a5`）；HMCL 全仓检索确证不存在 |
| Mod / 资源一致性校验 | ❌ 无 UI、无校验闭环 | ❌ **未实现**（`terracotta` 包无 mod 代码，`game` 包无 terracotta 引用，`TerracottaProfile` 无模组字段）。**即此为两侧共同空白** | ECL `frontend` 缺失项 #6；HMCL 全仓检索确证不存在 |
| 房间码二维码 / 分享卡片 | ❌（仅文本复制） | ❌ **无**：`QrCodeUtils` + `nayuki-qrcodegen` 全仓**唯一调用点是微软登录扫码**（`MicrosoftAccountLoginPane.java:226-242`），联机分享只有剪贴板文本。**两侧共同空白** | ECL `frontend` 缺失项 #7；HMCL `ui/account/MicrosoftAccountLoginPane.java:226-242` |
| EasyTier 参数透出 | ✅ 有高级设置界面 | ❌ 启动命令行**仅** `<exe> --hmcl <portfile>`，无 EasyTier 参数配置 | ECL `8f7f43a`；HMCL `terracotta/TerracottaManager.java` |
| 状态机与错误处理 | ✅ 有状态与错误码 | ✅ **14 个状态类 / 8 个 JSON 子类型**；**乐观「假状态」UI**；`index` **严格单调保护**（防旧状态覆盖新状态）；轮询按窗口焦点在 **500 ms / 15 s** 间自适应；**6 种可恢复异常 + 4 种致命错误**分类提示；核心日志与 HMCL 日志打包 zip 导出。注：`Fatal.Type.OS` 是**死枚举**（无构造点）且语言文件缺 `terracotta.status.fatal.os` 文案 | ECL `connector` 状态模块；HMCL `terracotta/TerracottaState.java` |
| 邀请码格式 | 未核实（ECL 生成稳定虚拟 IPv4 房号） | **无法确证**（数字/长度/校验算法均在核心二进制内） | — |
| 房间密码 / 加密 / 白名单 | 未核实 | ❌ **无** | HMCL 全仓检索确证不存在 |
| 房间内聊天 | ❌ 无 | ❌ **无成员聊天**。**两侧共同空白** | 两侧均确证不存在 |
| 实例自动匹配房间 | 🟡 后端返回占位、前端无 UI | 未核实 | ECL `ECL/api/connector.py`；`frontend` 缺失项 #3 |

> **本节结论**：在**联机基础设施**上 HMCL 更成熟（核心二进制全生命周期托管 + 哈希校验 + 多镜像回退 + 离线安装 + 精细化状态机）；在**联机功能面**上 ECL 明显更广（自定义节点、NAT 检测、端口探测、局域网广播、成员管理、EasyTier 参数透出）。
> **两侧共同空白（可差异化）**：房间 Mod 一致性校验、房间码二维码/分享卡片、房间内聊天。
> **HMCL 空白但 ECL 已有**：NAT 可视化、端口映射、自定义中继节点、EasyTier 参数、踢人。
> **HMCL 有而 ECL 缺**：核心二进制的自动下载 + SHA-512 逐文件校验 + 多镜像回退 + 拖入离线安装 + 状态机错误分类 + 日志打包导出。

### 3.11 插件与扩展性

| 功能 | ECL | HMCL |
| --- | --- | --- |
| 插件系统 | ✅ **有**：`ECL/plugins/` 27 个文件 | ❌ **无**（HMCL 的扩展方式只能是改源码/提 PR） |
| 宿主扩展点 | ✅ **6 类**：联机扩展、启动钩子（prepare/pre_launch/post_launch/exit）、账户认证提供方、崩溃分析富化、受控网络请求、实例兼容来源注册 | ❌ |
| 前端插槽 | ✅ **30 个**（侧边栏/标题栏/内容区/设置分区/版本详情/任务队列/在线搜索），支持 HTML 与运行时 Vue 转译、多实例槽 | ❌ |
| 进程与依赖隔离 | ✅ 归档插件走**独立 Worker 进程** + 依赖安装目录隔离 | ❌（单进程） |
| 安装事务 | ✅ `.eclplugin` 归档预检 → 依赖锁 → 事务安装 → 回滚；有专门测试 | ❌ |
| 权限 / 鉴权 | ✅ 逐命令鉴权 + `PluginNetworkPolicy` 方法/主机权限映射 | ❌ |
| 签名校验 | ❌ **无签名机制** | — |
| 设置项 | ✅ 插件设置读写 IPC | ❌ |
| 开发者工具 | ✅ 开发者通道 WebSocket（`--dev-channel`）+ DevTools 页 | ❌ |

### 3.12 UI / UX 与交互细节

| 功能 | ECL | HMCL |
| --- | --- | --- |
| 页面/窗口框架 | Vue Router + 组件化；7 个路由级页面 + 8 个子页 + 开发页 | 统一 `Decorator` + `Navigator` + `WizardController` + `JFXDialogPane` + `ListPageBase` 四套框架，18 个顶层页 + 9 个向导页 + 11 个列表页 + 4 个独立 Stage |
| 窗口边框模式 | ✅ **三级**：`custom` / `system_shadow`（仅 Windows）/ `native`，按平台能力探测 | ✅ 双模式：系统原生 / 自绘透明；`hmcl.nativeDecoration` 可强制；Windows 深色模式/透明主题下自动不用原生装饰 |
| 标题栏交互 | ✅ 原生拖拽 | ✅ 双击最大化/还原、**最大化状态拖动标题栏还原并跟随鼠标**、自实现四边四角缩放、可拖动区与禁止拖动区注册 |
| 系统托盘 | ❌ **无 OS 托盘**；但有标题栏内溢出菜单（可注册条目 + 插件插槽） | ❌ **完全没有**（grep `tray/SystemTray/TrayIcon` 零命中） |
| 拖拽安装 | ✅ DOM 拖放 + Tauri 原生 `tauri://drag-*` 双通道 + 全局遮罩 | ✅ **13 个拖入目标**：整合包/Mod/资源包/世界/数据包/原理图/Java/主题包/皮肤/NBT/实例 JSON 等 |
| 快捷键 | 🟡 无自定义快捷键/命令面板；非 dev 下禁用 F12 与 Ctrl+Shift+I/J/C/U | ✅ macOS ⌘Q/⌘W、F11、ESC 关闭弹层、原生装饰下不接管 |
| 右键菜单 | ✅ 侧边栏等 | ✅ 列表页/账户/实例多级 `PopupMenu` + `IconedMenuItem` |
| 弹窗体系 | ✅ **优先级仲裁栈**：七档优先级 + 插件弹窗上限 60 / 启动器下限 70 强隔离 + `cacheable`/`once` 去重 + 子弹窗保留父页面不退场 | ✅ `JFXDialogPane` + `DialogController`；嵌套弹窗有专门的父子引用属性 |
| 空状态 / 加载态 | ✅ 近期统一（`e7b65c7`、`4ec8402` 等） | ✅ |
| 页面预热 / 内存回收 | ❌ | ✅ 页面预热 + 关闭界面后 `trimHeap()`（`HMCL_AUTO_TRIM_HEAP=false` 可关） |
| 无障碍 | 🟡 部分 `aria-*` | ✅ Accessibility High Contrast 变体外观 |
| 演示模式 | ✅ **Showcase**：1173 行假传输 + 899 行 fixture，无后端跑通 90+ 命令 | ❌ |

### 3.13 主题与外观

| 功能 | ECL | HMCL |
| --- | --- | --- |
| 内置主题 | ✅ 2 套皮肤 `classic` / `folia`（`ThemeCatalog.builtin_theme_ids`） | ✅ 2 个主题包 `hmcl.default` / `hmcl.classic` + **9 种色彩风格** |
| 动态取色 | ✅ Monet 取色：从背景图提取种子色生成完整主色阶 | ✅ MonetFX（Material You）动态取色 |
| 背景来源 | ✅ 图片 / 视频背景（`background_media.py` 本地 HTTP 服务） | ✅ **6 种背景来源** + 3 张内置壁纸 + **网络壁纸缓存策略** + 备用背景与加载策略 |
| 外观细项 | ✅ 圆角、透明度、模糊（可独立控制）、定时深色切换、字体自定义、标题栏三模式 | ✅ 窗口透明、标题栏透明、壁纸取色、`hmcl.uiScale` / `HMCL_UI_SCALE`、字体覆盖、APNG |
| 主题包在线获取 | ❌（无主题商店） | ✅ `RemoteAddon.Type.CUSTOMIZATION`（可下载主题包）+ `ThemePackManagementPage` |
| 主题工作室 | 🟡 窗口类型已解析，**前端无界面** | ✅ 主题包管理页 + CSS 5 个文件 |

### 3.14 多语言

| 功能 | ECL | HMCL |
| --- | --- | --- |
| 语言数 | 6（zh-CN / en-US / ja-JP / ru-RU / de-DE / zh-TW） | 10（默认 + ar / de / es / ja / **lzh 文言文** / ru / uk / zh_Hans / zh_Hant + 生成语言 `en-Qabs` 倒置英文） |
| 键规模 | zh-CN/en-US 各 1693；其余 4 种各 1657（**少 31–36 键**，统一回退 zh-CN） | 默认 1499 键，代码实际引用 1024 键 |
| 回退策略 | 🟡 `fallbackLocale = defaultLocale`（即 zh-CN），非中文用户会看到简体中文 | ✅ `getFallbackLocale` 返回 **null 以抑制退到 JVM 默认语言**，基座 `I18N.properties`（英文）兜底；候选列表沿宏语言链向上爬并给中文插 zh-CN/zh-TW；缺失键 → `LOG.error` 并**返回 key 本身**作为显示文本（构建期由 `checkTranslations` 提前拦截） | ECL `frontend/src/i18n/index.ts`；HMCL `util/i18n/LocalizedText.java`、`build.gradle.kts` |
| 别名与书写系统表 | ❌ | ✅ **三张 CSV 驱动**：`sublanguages.csv`（ISO639-3 子语言→宏语言）、`language_aliases.csv`（标签归一化）、`default_script.csv`（书写系统→文字方向，驱动 RTL） | HMCL `HMCL/src/main/resources/assets/lang/` |
| 可按语言注入 Java 逻辑 | ❌ | ✅ 每个 locale 可提供 `Translator_xx` 子类：已有 `Translator_lzh`（数字转干支）与 `Translator_en_Qabs`（文字倒置） | HMCL `util/i18n/` |
| 组件库文案 | 🟡 naive-ui 内置 locale 仅映射 zhCN/enUS，其余 4 语言组件内部文案串英文 | — | ECL `frontend/src/app/AppProviders.vue` |
| 硬编码中文 | 🟡 实例模块大面积硬编码（截图/服务器/世界/分类/右键菜单/详情 tab 名/图标名） | — | ECL `frontend/src/components/instances/*` |
| 工程化 | 6 份 JSON + 测试 | ✅ 构建期生成语言列表与倒置英文包 + 自定义 Gradle 任务 `checkTranslations` 做键完整性与**中文错别字/全半角检查**（帐户→账户、其它→其他、全角括号）+ `syncTranslations` 脚本 + `SyncTranslationsTest` | HMCL `build.gradle.kts:457-462`、`config/` |

### 3.15 通知 / 日志 / 崩溃

| 功能 | ECL | HMCL |
| --- | --- | --- |
| Toast / 通知 | ✅ 分级（`message` / `modal`）+ 100 条环形去重 + 5 秒抑制 | ✅ `JFXSnackbar` |
| 日志窗口 | ✅ 三态悬浮日志窗（最小化/浮动/最大化 + 未读计数 + 双击复制）+ 独立日志视图 + 环形缓冲 + stdin 输入 | ✅ `LogWindow`（含过滤、计数；游戏运行时 macOS 下禁用 ⌘W） |
| 崩溃窗口 | ✅ 崩溃弹窗展示可疑 Mod + 原因码文案 | ✅ `GameCrashWindow` + `CrashWindow`（启动器自身崩溃） |
| 崩溃分析 | ✅ **38 条规则**（`CrashRule` 实例数），带置信度（`certain` 等）与优先级，支持命名捕获组，**移植了 HMCL 精选规则**；三源 Mod 索引 + 独立堆栈分析器 + Mod 归因 | ✅ **59 条规则**（`CrashReportAnalyzer.Rule` 枚举），**全部为 Java 正则** + 可选命名捕获组（file/class/id/reason/sourcemod/destmod/type/location）；一次日志可同时命中多条；崩溃报告路径正则定位 + 兜底按标记截取 | ECL `ECL/services/game/crash/rules.py`、`analyzer.py`（30 KB）、`mod_index.py`、`stacks.py`；HMCL `HMCLCore/.../game/CrashReportAnalyzer.java:31-199` |
| 进程监控 | ✅ 日志环形缓冲 + stdin 写入 | ✅ `ManagedProcess`（原始命令行、classpath、线程安全日志缓冲、`getLines(Predicate)` 过滤）；`StreamPump` 双线程分别抽 stdout/stderr；**不检测真实窗口句柄**，改用日志启发式（出现 `lwjgl version`/`lwjgl openal` 即认为窗口已起） | ECL `ECL/services/processes.py`；HMCL `ManagedProcess.java:34-204`、`LauncherHelper.java:989-1002` |
| 崩溃上报 | ❌ | ❌（报告明确「无崩溃上报」） |
| 错误呈现 | ✅ `error_id` 补录机制 + 前端日志回传 | ✅ |

### 3.16 自更新

| 功能 | ECL | HMCL |
| --- | --- | --- |
| 检查更新 | ✅ 按通道（alpha 禁用 / beta / release）查 GitHub Releases，SemVer 比较含预发布 | ✅ 三通道 `UpdateChannel` + 气泡提示 + `UpgradeDialog`（抓 CHANGELOG 只渲染高于当前版本的条目，失败回退浏览器） |
| 下载与替换 | ✅ 分阶段：匹配平台 asset → 下载到暂存 → 摘要校验 → 落 pending → Windows/POSIX 引导脚本延迟替换 → 备份回滚 → 启动时清理陈旧 pending | ✅ `UpdateHandler` + `Restarter.restartSelf()` + `ExecutableHeaderHelper`（保留 exe 头） |
| 自更新校验 | ✅ 下载后摘要校验 + 失败回滚 | 🟡 下载校验用 **SHA-1**（响应字段 `jarsha1`）；SHA-256 仅作为发布产物旁的 `.sha256` 文件。**更强的保证来自 jar 内 RSA 签名**（`META-INF/hmcl_signature` + `hmcl_signature_pub`，SHA-512withRSA），未通过则**不允许检查更新**、并对非官方构建限制离线/微软账户 | ECL `ECL/services/app_update.py:183-577`；HMCL `upgrade/IntegrityChecker.java` |
| 替换机制 | ✅ 引导脚本延迟替换（Windows/POSIX 双实现） | ✅ `Restarter.restartSelf()` + `ExecutableHeaderHelper` 保留 exe 头；`--apply-to` **两段式替换**（新进程改写旧 jar）解决 Windows 无法自覆盖；HMCLBoot 编译目标为 **Java 8**，因此能在过旧的 JVM 上先弹「需要新 Java」提示 | ECL `app_update.py:577,615`；HMCL `upgrade/`、`HMCLBoot/` |
| 通道限制 | ✅ 仅 beta/rc/release 开放自更新，alpha 与源码运行保持调试态 | ✅ 三通道均支持 |
| 不打扰选项 | 🟡 **无用户可配置的「跳过此版本 / 稍后提醒 / 不自动检查」设置项**（`skip_version`/`ignore_version`/`auto_check`/`remind`/`snooze` 零命中，后端无任何更新开关）。但有**自动去重**（非设置项）：localStorage 键 `euoracraft-seen-update-version`，只在未读该版本时才弹窗，关闭即写已读；手动检查不受限。更新检测**启动时自动执行一次 + 可手动**（`StartupUpdateService` 由装配根启动，`start()` 防重；结果经 `update:check_completed` 自动打开弹窗，前端晚就绪时重放） | ✅ 「不自动弹更新窗」设置项 | ECL `frontend/src/features/settings/model/updateNotice.ts:4,12-39`、`App.vue:100,194-196`、`ECL/services/updates.py:265-321`、`ECL/application.py:389-395,568`；HMCL `ui/UpgradeDialog.java` |
| 通道禁用检测 | ⚠️ **文档与实现不一致**：`status="disabled"`（alpha 不检测）**只出现在 docstring 与前端 i18n 字符串**，全 ECL **无任何赋值点**；`_channel()` 对 alpha 返回 `"alpha"` 且不过滤预发布 → **按代码 alpha 通道也会检测更新** | ✅ `UpdateChannel` 三通道均支持 | ECL `ECL/services/updates.py:95,197-201` |

### 3.17 平台与打包

| 功能 | ECL | HMCL |
| --- | --- | --- |
| 平台 | 声明 win32 / linux / darwin，非打包时告警；打包以 Windows（Nuitka）为主，另有 `PKGBUILD` 与 `.desktop` | Windows / macOS / Linux + FreeBSD |
| CPU 架构 | 未核实（Python 依赖约束） | ✅ **23 种**：x86/IA-32/IA-64/SPARC(V9)/ARM32/ARM64/MIPS(EL)/MIPS64(EL)/PPC(LE)/PPC64(LE)/S390(X)/RISCV32/64/LoongArch32/LoongArch64(新旧世界)/Unknown |
| Windows 原生集成 | 🟡 `system_shadow` 窗口模式、单实例、快捷方式 | ✅ JNA 取 HWND + `IPropertyStore` 写 AppUserModel（任务栏固定/最近使用可正确重启）、DWM 深色标题栏、高 DPI 关闭子像素抗锯齿、隐藏 `.hmcl` 目录、ARM64 提示、路径含 `!` 拒绝启动 |
| macOS | 🟡 分支存在 | ✅ `NSAppearance`（含高对比变体）、Dock 图标、**App Translocation 检测 + 5 秒倒计时**、⌘Q/⌘W |
| Linux | 🟡 分支存在 | ✅ `glass.gtk.uiScale`、`gdbus` 查询系统配色、Wayland/`Xft.dpi` 推断 DPI、LoongArch/MIPS64EL 提示 |
| 打包 | Nuitka onefile + PyInstaller spec + PKGBUILD + 插件运行时预编译 | Shadow **fat jar**（`jar` 任务被禁用）+ exe/sh **引导壳字节 + 追加 jar 内容** + **自研 `CreateDeb`** 手工组装；deb 用 `alternatives` 让 stable/beta/nightly 三通道共存（优先级 100/200/300）；**官方目标平台 9 个**（含 `linux-loongarch64`、`linux-riscv64`、`freebsd-x86_64`）；`lib/JFoenix.jar` 是随仓库分发的定制版控件库 |
| 构建期属性注入 | 环境变量 + `.env` | ✅ `-D` 属性实际走构建期生成的 `assets/hmcl.properties`（`JarUtils` 读取），系统属性可覆盖 |
| 用户数据格式契约 | 未核实 | ✅ `docs/schemas/` 下 **12 份 JSON Schema** 做用户数据版本化契约；根目录另有 `AGENTS.md`（Java 规范：`@NotNullByDefault`/显式 `@Nullable`、`@Unmodifiable`、`///` Markdown Javadoc） |
| 第三方许可清单 | 未核实 | ❌ **无 NOTICE / THIRD-PARTY 聚合清单**（全仓检索仅命中 LICENSE 与 checkstyle `license-header.txt`）；第三方许可信息只散落在 `libs.versions.toml` 与少量源码内联注释（如 `VersionNumber` 注明来自 Apache Maven, Apache-2.0）。**这是 HMCL 相对薄弱的一环** |
| 系统属性覆盖 | 环境变量 + `.env`（`ECL_DOWNLOAD_PROXY`、`CURSEFORGE_API_KEY`） | ✅ **26 处 `-Dhmcl.*`**（`hmcl.bmclapi.override` / `hmcl.discoapi.override` / `hmcl.curseforge.apikey` / `hmcl.update_source.override` / `hmcl.self_integrity_check.disable` / `hmcl.home` / `hmcl.uiScale` / `hmcl.april_fools` 等） |

### 3.18 安全与隐私

| 项 | ECL | HMCL |
| --- | --- | --- |
| 凭据落盘 | ❌ **明文 JSON**（access/refresh token、id_token 全明文） | 🟡 ChaCha20-Poly1305 + **硬编码密钥**（混淆级，源码自述非设备绑定） |
| 原子写 | ✅ 全部 `atomic_write_text` | ✅ `FileUtils.saveSafely` |
| 写入回滚 | 🟡 无失败回滚备份 | 🟡 同 |
| 归档解压安全 | ✅ 路径逃逸校验 | 未核实 |
| IPC 鉴权 | ✅ 逐命令鉴权 + 窗口级 `authorize_window_command` + Tauri capability 白名单 | — （无 IPC 层） |
| 网络边界 | ✅ 插件网络策略（方法/主机白名单） | — |
| 日志脱敏 | 未核实 | ✅ 未核实 |
| 插件签名 | ❌ 无 | — |
| 自校验 | 🟡 仅下载摘要校验 | ✅ **jar 内 RSA 签名**：`META-INF/hmcl_signature` + 内置公钥，**SHA-512withRSA**；未通过则**不允许检查更新**，非官方构建限制离线/微软账户；`-Dhmcl.self_integrity_check.disable` 可关 |

### 3.19 工程化与发布

| 项 | ECL | HMCL |
| --- | --- | --- |
| 静态检查 / 格式 | Ruff（`ruff.yml`、行宽 120）+ 架构测试（`test_architecture.py`、`test_public_returns.py`、`test_plugin_sdk_boundary.py`）+ 前端 `pnpm check`（Prettier+ESLint+类型+测试） | ✅ **checkstyle 152 行规则**，含**两条自定义正则禁止裸 `.toLowerCase()`/`.toUpperCase()`**（强制 `Locale.ROOT`）+ `RegexpHeader` 强制 GPL 版权头；`check-codes.yml` + `check-codes-comment.yml` 组合用 reviewdog 让 fork PR 也能收到行级评论 | ECL `.github/workflows/`；HMCL `config/checkstyle/` |
| CI 工作流 | 4 个主仓（`backend-tests` / `build` / `ruff` / `submodule`）+ 3 个前端（`build-frontend` / `deploy-showcase` / `lint`） | 6 个（`check-codes` / `gradle` / `release` / `mirror` / `pr-size-label` / `check-codes-comment`）；`release.yml` 会 `sha256sum -c` 校验产物并**双写 GitHub + CNB Release**；`pr-size-label` 忽略名单排除翻译/资源/脚本以免虚增 PR 规模；`config/` 内另有 Jenkins（dev/stable）作为第二条发布路径 | ECL `.github/workflows/`；HMCL `.github/workflows/` |
| 测试数 | 82 个 pytest 文件（后端覆盖插件、崩溃、联机、单实例、打包隔离、世界种子等） | **69 个** `*Test.java`（Core 41 / HMCL 27 / Boot 1），代表用例：`SchematicTest`、`GameDirectoriesTest`、`DefaultGameRepositoryDraftTest`、`CrashReportAnalyzerTest`、`WindowBoundsTest`、`ProtectedPayloadTest`、`CompressingUtilsTest`、`CommandBuilderTest` |
| 发布 | semantic-release + beta 标签规程（`vMAJOR.MINOR.PATCH-beta.N+YYYYMMDD`）+ `docs/release-process.md` | 三通道发布 + Gradle release 工作流 |
| 子模块治理 | ✅ 强制推送顺序与指针核验（`submodule.yml` + AGENTS.md 第 5 节） | — |
| 依赖更新 | ✅ **使用 Renovate**：根 `renovate.json`（`config:recommended`、Asia/Shanghai、`deps:` 前缀、周五执行、`ignoreDeps` 含 cryptography/python/node/pnpm、pep621 分组）+ `frontend/renovate.json`（npm minor/patch 合组、major 需 `dependencyDashboardApproval` 且 7 天 `minimumReleaseAge`、`@types` 自动合并）；**无 Dependabot** | ❌ **未使用 Renovate 或 Dependabot**：根目录无相关配置文件，`.github/` 仅有 ISSUE_TEMPLATE(3) + scripts(2) + workflows(6)，无 `dependabot.yml`；全仓检索仅命中一个无关模组名 | ECL `renovate.json:1-43`、`frontend/renovate.json:1-74`、`ECL/.github/workflows/`（4 个）；HMCL `.github/`（6 个 workflow，无依赖机器人） |

---

## 4. ECL 独有优势（HMCL 没有）

1. **插件系统**——6 类宿主扩展点（联机/启动钩子/账户认证/崩溃分析/网络/实例兼容）+ 30 个前端插槽 + 运行时 Vue 转译 + Worker 进程隔离 + 依赖目录隔离 + `.eclplugin` 事务安装 + 逐命令鉴权。HMCL 完全无插件机制。`ECL/plugins/`、`ECL/plugin.py:29-35`
2. **单实例互斥与请求转发**——回环 TCP 探测、快捷启动转发、重复请求合并、进程级配置转发拒绝。HMCL 报告 §11.4 确认零命中。`ECL/services/single_instance.py`
3. **完整启动器命令行**——17 类参数 + Windows `.lnk` 快捷方式生成（含实例图标与稳定 ICO 缓存）。HMCL 无。`docs/launcher-cli.md`、`ECL/cli.py:107-164`
4. **标题栏溢出菜单（Tray）**——`TitleBarTray.vue`：标题栏内的向上箭头溢出菜单，条目可注册/注销、支持优先级排序与插件插槽注入（`plugin-slot-titlebar-tray`）。**注意**：这是窗口内菜单而非操作系统级托盘，两侧**都没有** OS 托盘。`frontend/src/components/layout/TitleBarTray.vue:1-60`、`frontend/src/composables/useTrayItems.ts`
5. **联机房间体系**——EasyTier 集成 + 自定义节点（6 种协议）+ NAT 类型异步检测 + 端口探测 + 局域网广播伪装 + TCP 半包/粘包处理 + 房间码生成稳定虚拟 IPv4。`ECL/services/florolding/`、`ECL/services/connector.py`
6. **原理图 3D 查看器 + 材料清单导出**——worker 建面、chunk 流式加载进度、atlas 合成、Y 层切片滑杆；HMCL 仅能解析与管理，无 3D 渲染。`ECL/services/game/schematics.py`（1168 行）
7. **第三方实例兼容读取**——直接识别并展示 PCL/PCL-CE 与 HMCL 实例的分组、收藏、图标、启动次数，且只读不写入；编码三级容错；插件可注册新来源。`ECL/services/game/instance_compat.py:30-247`
8. **服务器列表管理 + 状态查询**——`servers.dat` 读写与批量查询（`mcstatus`）。HMCL 无此能力。`ECL/services/game/servers.py`
9. **截图管理**——缩略图/复制/删除/设为封面或背景。HMCL 无独立截图管理。`ECL/services/game/screenshots.py`
10. **本地衣柜**——皮肤/披风库，哈希去重、导出、同步应用；配合微软皮肤上传/重置与披风装备/卸下。HMCL 无披风管理。`ECL/services/wardrobe.py`
11. **开发者通道与工具箱**——`--dev-channel` 起 WebSocket 服务（43 KB 服务实现）+ DevTools 页组件检查器。HMCL 无。`ECL/services/dev_channel.py`
12. **Showcase 演示运行时**——无后端即可跑通 90+ 命令的完整 UI 与下载速度模拟，用于文档站/回归。HMCL 无。
13. **并行分片下载**——ECL 有、HMCL 无（HMCL 明确无分片）。
14. **双通道代理分离**——启动器通道与下载通道（`ECL_DOWNLOAD_PROXY`）互不干扰，下载器 `trust_env=False` 不继承系统代理。HMCL 为全局 `ProxySelector` 注入。
15. **弹窗优先级仲裁栈**——七档优先级 + 插件/启动器弹窗强隔离 + 去重记忆 + 子弹窗保留父页面不退场。
16. **实例完整性检查与逐项修复**（`game_instance_files_check` / `_repair`）+ 资源批量删除返回逐项结果。
17. **自定义下载工具**——任意 HTTP(S) 地址 + 完整保存路径，复用下载器与任务队列，可取消/重试/覆盖勾选。HMCL 无。
18. **运行统计与实例 profile**——独立的实例资料存储 + schema 版本保护。
19. **光影包本地管理**——ECL 可启停光影并改写 `optionsshaders.txt`；HMCL `addon/` 下**没有 shaderpack 类型**、也没有 `ShaderPackListPage`，只能「打开文件夹」。
20. **全局 + 世界内数据包管理**——ECL 两者都支持；HMCL 只有世界内数据包（`DataPackListPage` 从 `world/datapacks` 构造）。
21. **真实生效的 Windows 进程优先级**——ECL 用 `psutil` 的 `*_PRIORITY_CLASS` 五档；HMCL `DefaultLauncher.java:63-95` 的四个 Windows 分支全部被注释掉，仅 POSIX `nice` 生效。
22. **资源包启停的双重保证**——ECL 同时改名 `.disabled` **并**改写 `options.txt` 的 `resourcePacks` 列表；HMCL 只改写 `options.txt`。

---

## 5. ECL 落后 / 缺失清单（按优先级）

### P0 —— 硬功能缺口

1. **Java 运行时自动下载**（完全不存在）。ECL 只有 `JavaScanner.py` 扫描 + `_probe_java_runtime` 探测；HMCL 有 Mojang piston-meta + foojay Disco **双来源**、**7 家发行版**（Temurin/Liberica/Zulu/GraalVM/Semeru/Corretto + Mojang 官方）、平台×架构矩阵、下载→解压→平台一致性校验→写 manifest→注册全链路，以及 `javaCache.json` 廉价变更检测与 `JavaVersionConstraint` 13 条兼容约束。影响：新机器上无法自助补装 Java，也无法为实例装匹配版本。证据：`ECL/services/game/launch.py:513-574`（仅探测）；HMCL `download/java/`（11 文件）、`JavaManager.java`、`JavaVersionConstraint.java:32-223`。
2. **OptiFine / LiteLoader / Cleanroom / LegacyFabric 安装**（仅识别）。`install.py:146-147` 与 `catalog.py:90-99` 白名单只有 vanilla/forge/neoforge/fabric/quilt，其余抛 `UNSUPPORTED_LOADER`。
3. **账户令牌明文落盘**。`accounts.json` / `ms_accounts/<id>.json` / `yggdrasil_accounts/<id>.json` 均为明文 JSON，含 refresh_token。grep `encrypt/Fernet/keyring/DPAPI/AES/PBKDF2` 零命中。证据：`ECL/services/accounts.py:295-337`、`ECL/game/Core/MicrosoftAuth.py:125-151`。
4. **插件无签名校验**。`.eclplugin` 有预检/依赖锁/事务回滚，但无签名与信任链。

### P1 —— 高价值增强

5. **下载缓存层缺失**：无 ETag 条件请求缓存、无内容寻址硬链接库缓存。每次安装/修复都可能重新传输同样的库文件。对照 HMCL `CacheRepository.java`（etag.json + SHA-1 硬链接 + index.json）。
6. **断点续传缺失**：临时文件截断写入、无 `Range` 续传、无 `.part` 持久化索引，进程重启从零开始；异常时 `.tmp` 不清理。对照 HMCL `HttpResumeContext`（含 206/Content-Range 严格校验与 416 处理）。
7. **镜像源过少**：仅 official / bmclapi。HMCL 有 **24 条**重写规则，含腾讯云 Maven、`alist.8mi.tech`、`mod.mcimirror.top`（Modrinth/CurseForge 加速，且强制优先）。
8. **模组批量更新未定时化**：ECL 有 SHA-512 哈希反查能力但缺少独立页面与批量入口；HMCL 有 `AddonUpdatesPage` 但**同样手动触发、无缓存、无定时任务，且是逐文件反查**。ECL 若做「批量接口 + 本地缓存 + 可选定时」即可反超。
9. **依赖缺失未闭环**：ECL 只标记 `missingDependencies` 且**无跳转安装路径**；HMCL 有 7 类依赖模型 + 依赖项可点击跳转下载 + `BROKEN` 警告标签，但**同样不自动安装前置依赖**。ECL 可在此形成差异化（真正的「一键补齐前置」）。
10. **世界备份无恢复入口**：ECL 只有备份列表接口；HMCL 有 `WorldBackupTask` 手动整目录 zip 快照（命名即元数据 `yyyy-MM-dd_HH-mm-ss_<世界名>.zip`），但**同样无轮转、无保留份数上限、无恢复按钮**（恢复靠「添加」导入 zip），备份也不可锁定。**因此这一项是两侧共同的空白**，ECL 若做「备份轮转 + 一键恢复 + 锁定」即可反超。
11. **NBT 可视化编辑缺失**：ECL 只能改难度/作弊/锁定三个字段，`options.txt` 仍是文本改写；HMCL 有完整 NBT 树形**浏览**（`NBTEditorPage`，可读 `.dat`/`.mca`/`.mcr`）但 **`save()` 仍是 `// TODO` → 只读**。**即「可编辑的 NBT 编辑器」是两侧共同空白**，ECL 若实现读+写即形成差异化。
12. **QuickPlay 仅两类**：ECL 支持服务器/世界；HMCL 支持单人/多人/Realms 三类含版本门槛判定。
13. **渲染与原生库选项缺失**：无渲染器（OpenGL/Vulkan/软回退）选择、无原生 GLFW/SDL 与原生 OpenAL 开关、无 Natives 补丁控制（HMCL 的 `NativePatcher` 规则来自打包资源 `assets/natives.json`）。
14. **启动器自更新缺少系统属性/通道细节**：无 `-D` 级覆盖；HMCL 有 `hmcl.update_source.override`、**jar 内 RSA 签名自校验**与「不自动弹窗」选项。
15. **Deep link（`ecl://`）缺失**：两侧都没有；HMCL 也只在内置 Yggdrasil/导出文件里出现 `hmcl://` 字样。对 ECL 是差异化机会。
16. **借鉴时的实现细节注意**：HMCL 硬链接缓存的降级路径有顺序缺陷（先删原文件再建链接，失败被静默吞掉）。ECL 若实现内容寻址硬链接缓存，应把 `createLink` 放在 `delete` 之前。

### P2 —— 体验与工程化

17. 联机实例自动匹配（后端占位、前端无 UI）、房间 Mod 一致性校验、房间码二维码/分享卡片、建房时 NAT 提示。
18. 前端 6 语言键不齐（4 种各缺 31–36 键且回退 zh-CN）；实例模块大量硬编码中文；naive-ui 组件内部文案仅中/英。
19. 主题工作室窗口无前端界面；鼠标特效的 10 个 i18n 键零实现（幽灵功能）。
20. 插件只能本地安装，无在线市场/仓库。
21. 无自定义快捷键/命令面板；非 dev 构建禁用 F12 与部分 DevTools 快捷键。
22. 无设置「一键恢复默认」（危险区仅在 `/dev`）。
23. 无实例导入（从其它启动器目录/压缩包扫描导入）；仅支持整合包与存档导入。
24. 内存自动分配仍为固定 4096 常量（文档自述），HMCL 按系统内存计算。
25. 下载限速器已实现但未接线（无任何 `speed_limit_mb=` 实参）。
26. Java 目录扫描为轮询而非原生文件系统事件（HMCL 未核实，此项为改善而非差距）。

---

## 6. HMCL 缺失而 ECL 已有

| 能力 | HMCL 状态 | 证据 |
| --- | --- | --- |
| 单实例互斥 | ❌ 无（无文件锁/互斥量/IPC） | HMCL 报告 §11.4：`grep FileLock/CreateMutex/Global\\` 均无相关命中 |
| 启动器命令行参数 | ❌ 无 | HMCL 报告 §14 |
| 系统托盘（OS 级） | ❌ 无（两侧都没有；ECL 有标题栏溢出菜单） | — | `grep tray/SystemTray/TrayIcon` 零命中 |
| **标题栏溢出菜单 + 插件插槽注入** | ✅ 菜单条目可运行时注册/注销、优先级排序、插件可注入条目 | ❌ 无 | `frontend/src/components/layout/TitleBarTray.vue:1-60`、`composables/useTrayItems.ts` |
| 插件系统 | ❌ 无 | — |
| 服务器列表管理（`servers.dat`） | ❌ 无 | `servers.dat` 仅在整合包导出文件清单出现 |
| 截图管理 | ❌ 无独立页面 | `screenshot` 仅出现在 `ModAdviser` / `GameInstancePage` / 整合包分组 |
| **光影包本地管理** | ❌ `addon/` 下无 shaderpack 类型、无 `ShaderPackListPage`，只能「打开文件夹」 | ECL 可启停并改写 `optionsshaders.txt` |
| **全局数据包管理** | ❌ 只有世界内数据包 | ECL 同时支持全局与世界内 |
| **Windows 进程优先级** | 🟡 四个 Windows 分支全被注释掉，实际不生效 | ECL 用 `psutil` 优先级类真实生效 |
| 披风管理（装备/卸下） | ❌ 无 | ECL 有 `set_cape` / `reset_cape` |
| 自定义下载工具 | ❌ 无 | — |
| 演示模式 | ❌ 无 | — |
| 开发者通道 | ❌ 无 | — |
| 并行分片下载 | ❌ 无 | `FetchTask.java:246-294` 单流循环，全仓无其它 `range` 构造点 |
| 第三方实例兼容读取（PCL/HMCL） | ❌ 无 | ECL `instance_compat.py` |
| 下载暂停 / 恢复 | 🟡 仅取消 | ECL 有 `pause()/resume()` |
| 联机节点自定义与 NAT 检测 | 未核实 | ECL 有完整实现 |

---

## 7. 小功能级差异清单（逐条）

> 这一节是对比报告中最容易被忽略、但对用户感知影响最大的部分。

### 7.1 ECL 有、HMCL 没有的小功能

| # | 小功能 | 证据 |
| --- | --- | --- |
| 1 | 无闪烁启动：`index.html` 内联同步快照恢复主题，主脚本加载前已应用亮暗与主色 | `frontend/index.html`、`src/main.ts:19-21` |
| 2 | 三态悬浮日志窗（最小化/浮动/最大化 + 未读计数 + 双击复制） | `frontend/src/features/terminal/components/FloatingLauncherLog.vue` |
| 3 | 向游戏进程 stdin 写入命令（`process_input`） | `ECL/api/registry.py` |
| 4 | 实例快捷方式使用**稳定 ICO 缓存**，移动启动器后仍可用 | `ECL/services/instance_shortcuts.py` |
| 5 | 资源包图标读取（包内 PNG） | `ECL/services/game/resources.py:279-317` |
| 6 | 存档种子精度修复（避免大种子被浮点截断） | `387ddd8 fix: 修复存档种子精度并统一服务器列表` |
| 7 | 模组启停后同步实际文件名（避免 `.disabled` 与列表不一致） | `2e79e23`、`153a585` |
| 8 | 大量服务器分批查询 + 超时放宽 | `fcacde3 test: 放宽大量服务器分批查询用例超时` |
| 9 | 大量模组时列表行内容重叠修复 | `bc57e26` |
| 10 | 崩溃弹窗展示可疑 Mod + 补全新原因码文案 | `ae7b8c4` |
| 11 | 启动按钮优先显示自定义名称 | `83c0211` |
| 12 | 背景模糊滑杆与毛玻璃开关**独立控制** | `f0de7ca`、`e1ff038` |
| 13 | 子弹窗打开时父全屏页面不退场（保留父页面） | `7b41fe0` |
| 14 | 联机创建房间步骤接入全局页面动画 | `53d3fec` |
| 15 | 插件依赖安装后展示「待重启」状态 | `949eda3` |
| 16 | 资源管理模式 + 统一在线搜索导航 | `510bb1a` |
| 17 | 实例列表工具栏筛选与排序收纳 | `6373d36` |
| 18 | 实例运行实例页加载与空状态统一 | `82025b3` 等 |
| 19 | 自定义 EasyTier 节点高级设置界面 | `8f7f43a` |
| 20 | 工具页自定义下载与**持续任务管理**（离开页面任务继续） | `d0a2297` |
| 21 | 插件运行时资产构建期按大小写折叠去重 | `f1e3281` |
| 22 | 插件运行时在 Windows 清理阶段处理文件占用 | `bb89d0c` |
| 23 | POSIX 上实例目录目标剥离尾部分隔符 | `517eb80` |
| 24 | 打包脚本在 ANSI 代码页控制台输出中文不崩溃 | `6f55e23` |
| 25 | 公告接口地址修正为 `EuoraCraft-Launcher.API` | `fd75722` |
| 26 | 插件归档篡改用例处理平台相关换行差异 | `957d282` |

### 7.2 HMCL 有、ECL 没有的小功能

| # | 小功能 | 证据 |
| --- | --- | --- |
| 1 | 最大化状态下拖动标题栏自动还原并跟随鼠标（宽度一半处对齐指针） | `ui/decorator/Decorator.java:473-487` |
| 2 | 自绘窗口四边 + 四角命中判定缩放（强制不小于 minWidth/minHeight） | `Decorator.java:493-547` |
| 3 | 窗口尺寸/位置/最大化/全屏状态记忆 | `ui/decorator/WindowBounds`/`WindowState` |
| 4 | 页面预热 + 关闭界面后自动 `System.gc()` 收缩堆（可关） | `ui/Controllers.java:654-659` |
| 5 | Windows 任务栏固定/最近使用可正确重启启动器（写 AppUserModel RelaunchCommand/DisplayName/Icon） | `ui/WindowsNativeUtils.java:45-93,197-207` |
| 6 | Windows 深色标题栏注入（`DWMWA_USE_IMMERSIVE_DARK_MODE`） | `theme/Themes.java:1160-1190` |
| 7 | 高 DPI 时自动关闭子像素抗锯齿 | `ui/Controllers.java:237-241` |
| 8 | Windows 路径含 `!` 时用 Swing 弹窗拒绝启动（JavaFX/Swing 都无法渲染中文） | `HMCLBoot/.../Main.java:110-126` |
| 9 | macOS App Translocation 检测 + 5 秒倒计时警告 | `HMCL/.../Launcher.java:113-118` |
| 10 | Linux 通过 `gdbus` 查询系统配色实现深色跟随 | `theme/Themes.java:531-534` |
| 11 | Linux/Wayland 读 `Xft.dpi` 推断 DPI | `HMCL/.../Launcher.java` |
| 12 | 13 个拖入目标（含 Java 归档、主题包、皮肤、NBT、实例 JSON） | `ui/` 各页 `applyDragListener` |
| 13 | 整合包导出**文件树多选** + 预置 9 类分组 | `ui/export/ModpackFileSelectionPage.java:240-248` |
| 14 | 更新弹窗只渲染**高于当前版本**的 CHANGELOG 条目，失败回退浏览器 | `ui/UpgradeDialog.java:47-103` |
| 15 | 更新包 exe 头保留 | `upgrade/ExecutableHeaderHelper.java` |
| 16 | 世界备份独立页面与任务 | `ui/instances/WorldBackupsPage.java`、`WorldBackupTask.java` |
| 17 | NBT 树形浏览器（**只读**，`save()` 为 TODO）+ 与原理图联动「探索」按钮 | `ui/nbt/NBTEditorPage.java` |
| 18 | ChunkBase 种子地图一键打开（最新提交还更新了地图地址） | `util/ChunkBaseApp.java`、`fdf883d09` |
| 19 | 愚人节版本专用图标与标签；快照/远古版各有图标 | `ui/download/VersionsPage.java:246-263` |
| 20 | 版本列表 Wiki 按钮（直接跳对应版本 Wiki） | `VersionsPage.java:180-183,208-214` |
| 21 | 整行水波纹点击反馈 + 防串扰 | `VersionsPage.java:194,225` |
| 22 | 文言文（`lzh`）语言包与专用格式化器 | `assets/lang/I18N_lzh.properties` |
| 23 | 构建期生成倒置英文语言包 `en-Qabs` | `HMCL/build.gradle.kts:457-462` |
| 24 | 键一致性 Gradle 校验与同步任务 | `HMCL/build.gradle.kts` |
| 25 | 在线下载**主题包**（`RemoteAddon.Type.CUSTOMIZATION`） | `addon/RemoteAddon.java:206-213` |
| 26 | 6 种背景来源 + 网络壁纸缓存 + 备用背景与加载策略 | `setting/BackgroundType.java`、`ui/main/PersonalizationPage.java` |
| 27 | 9 种色彩风格（MonetFX） | `setting/ThemeColorType.java` |
| 28 | 逐项设置描述文案 + 逐项「继承/覆盖」按钮 + 描述随继承状态变化 | `ui/game/GameSettingsPage.java:2232-2267` |
| 29 | 设置预设（`GameSettingsPresets`）与默认预设 | `setting/GameSettingsPresets.java` |
| 30 | 旧配置自动迁移 + `MigrationReceipt` 指纹回执（源文件未变则不重复迁移） | `setting/LegacyGameSettingsMigrator.java`、`MigrationReceipt.java` |
| 31 | 自完整性校验与「更新未验证」提示 | `upgrade/IntegrityChecker.java` |
| 32 | 下载 404 短路（该候选不再重试，直接用下一个源） | `task/FetchTask.java:488-490` |
| 33 | `x-bmclapi-hash` 响应头直查本地缓存跳过下载 | `FetchTask.java:355-363` |
| 34 | 缓存篡改检测（lastModified 变化即重算 SHA-1 比对） | `util/CacheRepository.java:191-195` |
| 35 | 多实例并发下的 ETag 索引合并（避免互相覆盖） | `CacheRepository.java:358-389` |
| 36 | 未上架版本清单内置（`unlisted-versions.json`，1439 行）并与官方清单合并去重 | `download/game/GameRemoteVersion.java:54-80` |
| 37 | 23 种 CPU 架构判定与对应提示 | `util/platform/Architecture.java` |
| 38 | 26 处 `-Dhmcl.*` 系统属性覆盖 | `setting/DownloadProviders.java:40-57` 等 |
| 39 | 安装目录使用 `GameRepositoryDraft` 事务，失败不留半成品 | `game/DefaultGameRepositoryDraft.java` |
| 40 | 删除实例移入回收目录而非直接删除 | HMCL `game/` |
| 41 | 更新 mod 时旧文件改名为 `.old` 保留、可还原（可回滚更新） | `addon/LocalAddonManager.java:48,109-156` |
| 42 | Forge 新版 jar-in-jar 递归解析内嵌 mod（`META-INF/jarjar/metadata.json`） | `addon/meta/ForgeNewModMetadata.java:232-303` |
| 43 | 依赖项可点击直接跳转下载（前置缺失的可用路径） | `ui/instances/DownloadPage.java:598` |
| 44 | 整合包 Provider 判定顺序硬编码并有源码注释警告（因 MCBBS 会额外写 Curse 格式清单） | `game/ModpackHelper.java:93-110` |
| 45 | 加载器组合冲突 4 类拒绝码显式建模 | `download/UnsupportedInstallationException.java:33-40` |
| 46 | Checkstyle 自定义正则禁止裸 `toLowerCase()`/`toUpperCase()`（强制 `Locale.ROOT`） | `config/checkstyle/` |
| 47 | 自定义 Gradle 任务 `checkTranslations` 检查中文错别字与全半角（帐户→账户、其它→其他） | `build.gradle.kts` |
| 48 | `docs/schemas/` 12 份 JSON Schema 作为用户数据格式契约 | HMCL `docs/schemas/` |

---

## 8. 架构与工程化对比

| 维度 | ECL | HMCL |
| --- | --- | --- |
| 前后端边界 | 清晰：Python 后端 216 条 IPC 命令 + 事件桥（34 后端事件 → 35 前端事件） | 无边界：UI 与引擎同进程直接调用 |
| 可测试性 | 后端可脱离 UI 单测（82 个 pytest，含架构约束测试）；前端有 vitest | JUnit 69 个，主要覆盖工具类与引擎 |
| 可扩展性 | 极强（插件 + 6 类扩展点 + 30 个插槽 + Worker 隔离） | 弱（只能改源码） |
| 一致性 | 依赖开发者自觉（后端有架构测试兜底，前端近期在还 UI 一致性债） | 极强（四套统一框架，所有页面都是组合） |
| 平台覆盖 | 窄（Windows 优先） | 极宽（23 架构 + 三平台原生集成 + FreeBSD） |
| 成熟度 | 0.0.1-alpha，功能补齐速度快（12 天 92 个提交） | 十年以上，10 语言、114 设置项、海量平台分支 |
| 代码可信度 | 🟡 存在「幽灵功能」（i18n 键无实现、后端占位返回） | 🟡 同样存在「注释掉的实现」（Windows 进程优先级四分支、NBT `save()` TODO）与「名不副实」（`GameVerificationFixTask`、`ResourceCleaner`） |
| 风险点 | 单进程内嵌 Tauri 主循环（阻塞式 portal 桥接）、轮询扫描、无缓存层、账户令牌明文 | JavaFX 与 JDK 版本约束、原生装饰与透明主题冲突、无插件带来的定制门槛、无第三方许可聚合清单 |

---

## 9. 结论与建议路线图

### 9.1 结论

1. **ECL 的定位不是「HMCL 的复刻」**，而是「Web 技术栈 + 插件生态」的差异化路线。在**扩展性、联机、单实例/CLI、演示与开发者工具**这些方向，ECL 已经超出 HMCL；另有 5 项「ECL 有而 HMCL 实际没有或未生效」的能力值得明确记录：**光影包本地管理、全局数据包管理、真实生效的 Windows 进程优先级、服务器列表管理、截图管理**。
2. **ECL 的短板集中在「引擎成熟度」**：Java 运行时安装、非主流加载器、下载缓存与续传、镜像覆盖、跨平台与多架构。这些恰好是 HMCL 十年积累最厚的地方，也是最难短期补齐的部分。
3. **安全上两侧都不合格**，但 ECL 更差：HMCL 至少做了混淆级加密与私有数据分离，ECL 是明文 refresh token。
4. 相对于 2026-09-26 的旧文档，ECL 已在 12 天内补齐了**整合包在线安装、自更新、启动高级选项、崩溃分析**四项被判定为缺失的能力，补齐速度显著。
5. **「已实现」不等于「已生效」**——本次对比在两侧都发现了这种落差：HMCL 的 Windows 进程优先级四分支全被注释、`NBTEditorPage.save()` 是 `// TODO`、`GameVerificationFixTask` 与 `ResourceCleaner` 名不副实；ECL 则存在 i18n 幽灵键、`connector_match_instances` 返回占位、`theme-studio` 窗口无界面。评估功能时必须以「调用点是否存在且生效」为准，而非以「设置项/键/类是否存在」为准。

### 9.2 建议路线图

| 阶段 | 内容 | 出口标准 |
| --- | --- | --- |
| 第 1 阶段（安全与引擎地基） | 账户令牌加密（Windows DPAPI / macOS Keychain / Linux Secret Service，附旧明文自动迁移）、插件签名与信任链、**下载 ETag + 内容寻址硬链接缓存**、断点续传（`Range` + `.part` 索引 + 416/206 校验） | 旧明文账户自动迁移测试通过；重复安装同一版本时网络流量显著下降；中断后重启可续传 |
| 第 2 阶段（硬功能补齐） | **Java 运行时自动下载**（Mojang piston-meta + foojay Disco 双来源）、**OptiFine / LiteLoader / Cleanroom / LegacyFabric 安装**、镜像规则表扩展（腾讯云 Maven、mcimirror） | 安装向导可完成「无 Java → 装 Java → 装 OptiFine」全流程；`ruff check ECL` + `pytest` + `pnpm check` + `pnpm build` 通过并实机验证 |
| 第 3 阶段（体验升级，含 3 项可反超的机会点） | QuickPlay 三类含版本门槛、渲染器/原生库选项、模组批量更新（批量接口 + 本地缓存 + 定时）、**依赖一键补齐前置**、**世界备份轮转 + 一键恢复 + 锁定**、**可写 NBT 编辑器**（HMCL 只读）、内存自动分配按系统内存计算、下载限速接线 | 各功能有 pytest 覆盖并在实机验证；上述 3 项「两侧共同空白」完成即形成差异化 |
| 第 4 阶段（生态与差异化） | 插件在线市场、联机房间 Mod 一致性校验与二维码分享、主题工作室界面、i18n 补齐（4 语言 31–36 键 + 实例模块硬编码中文）、自定义快捷键/命令面板、`ecl://` Deep link | 签名插件可发布安装；非中文语言下无中文残留 |

---

## 附录 A：证据来源与盘点文件

本报告的 **10 份支撑盘点文件**（逐条带行号证据，**合计 6,016 行 / 约 730 KB**）位于 `E:\Projects\EuoraCraft-Launcher\_compare\`。

| 文件 | 内容 | 行数 |
| --- | --- | --- |
| `ecl-backend-a.md` | ECL 架构/进程模型、**216 条 IPC 命令全表**、账户与认证（含明文令牌取证）、下载引擎与镜像、Java 运行时（仅扫描）、加载器安装、整合包 | 970 |
| `ecl-backend-b.md` | ECL 实例管理、启动能力（含 CLI 全表）、资源管理（Mod/资源包/光影/数据包/存档/服务器/截图/原理图/options.txt）、联机（florolding 8 节）、插件系统（7 节）、**其它应用服务（更新/维护/长任务/自定义下载/公告/背景媒体/单实例/开发者通道）**、**安全·平台·工程化** | 762 |
| `ecl-frontend.md` | ECL 前端外壳与窗口、完整路由表、逐页明细、设置逐项、主题、i18n、交互细节、Pinia 全表、组件与 IPC 面、**20 项缺失清单** | 1229 |
| `hmcl-ui.md` | HMCL UI 层 244 个文件逐项：导航结构、18 个页面、9 个向导页、11 个列表页、设置页逐项、账户页、下载页、主题、i18n、通知/日志、快捷键、命令行、平台适配、彩蛋、明确缺失项 | 900 |
| `hmcl-core-c.md` | HMCL 账户与认证：两种 OAuth 流、本地回调服务器（PKCE + state）、离线皮肤注入、authlib-injector、`ProtectedPayload` 令牌保护（§2–§6 由下方补充文件覆盖） | 230 |
| `hmcl-core-d.md` | HMCL 实例管理、**114 个持久化设置项与 56 个 `PROPERTY_*`**、旧配置迁移器与 `MigrationReceipt`（§3–§7 由下方补充文件覆盖） | 389 |
| `hmcl-download-java.md` | HMCL 下载引擎（重构后形态、**24 条镜像规则**、候选回退、ETag/硬链接缓存、并发模型含虚拟线程 + Semaphore、Range 续传严格校验、系统属性）+ **Java 运行时管理**（7 家发行版矩阵、`javaCache.json` 廉价变更检测、`JavaVersionConstraint` 13 条约束、4 值 `JavaVersionType`、两级择优、安装/卸载/禁用/恢复） | 441 |
| `hmcl-loaders-modpack.md` | HMCL **11 个加载器**逐个（Forge 新旧安装器 / NeoForge / Fabric / Fabric API / Quilt / QSL / OptiFine / LiteLoader / Cleanroom / LegacyFabric）、**patch + priority 版本模型**、`GameRepositoryDraft` 事务、库与 assets 校验、**6 个整合包 Provider**、导出格式与 MultiMC 移除取证 | 333 |
| `hmcl-launch-addon.md` | HMCL 启动能力（LaunchOptions、JVM 参数注入规则与顺序、内存自动分配、隔离、命令链、QuickPlay 三类、渲染器/GPU/Natives 补丁、**ManagedProcess 与日志启发式窗口检测**、**59 条崩溃规则**）+ addon 资源管理（Mod 元数据 6 读取器、jar-in-jar、`.old` 回滚、7 类依赖模型、repository/meta 机制） | 455 |
| `hmcl-worlds-platform.md` | HMCL 世界/存档/备份/ChunkBase/原理图（`Schematic` sealed 类族）/NBT/自更新（含 RSA 签名自校验）+ 平台矩阵（23 架构）、打包（fat jar/exe 壳/自研 CreateDeb/9 平台）、测试 69 个、6 个 CI、checkstyle 与 `checkTranslations`、国际化三表、许可合规 | 307 |

> **覆盖面说明**：`hmcl-core-c.md` 与 `hmcl-core-d.md` 末尾各有 5 个未填小结节（§2–§7），其内容已分别由 `hmcl-download-java.md`、`hmcl-loaders-modpack.md`、`hmcl-launch-addon.md`、`hmcl-worlds-platform.md` 四份补充文件完整覆盖，因此本报告的取证是完整的；`ecl-backend-b.md` 的 §6/§7 已补齐。

**未核实清单**（本报告中明确标注，需后续确认）：

- ECL 是否有 OptiFine 之外的加载器安装计划；`SearchMinecraft.py` 中 optifine/liteloader/cleanroom/legacyfabric 的识别是否覆盖所有版本。
- HMCL 新版 `DownloadProvider` 是否仍支持第三方自定义 Provider（重构中删除了 `BMCLAPIDownloadProvider` 等三个子类与 Wrapper）。
- ECL 前端 `theme-studio` 窗口后端是否会真的开窗。
- ECL 日志脱敏、HMCL 日志脱敏的具体策略。
- ECL 是否存在独立版本号比较引擎。
- HMCL `JavaInfoUtils` 的具体命令行、Disco 列表拉取失败的 UI 提示、Java 解压失败残留是否清理、`DownloadCandidate` 自定义 retry/超时是否有插件使用。

**已核实并修正的细节**（供后续引用时注意）：

- HMCL 下载镜像重写规则为 **24 条**（20 条常规 + 4 条 `fallback=true` 的 Modrinth/CurseForge 规则），不是 23 条。
- `JavaVersionConstraint` 为 **13 个常量**（不含构造器行）。
- HMCL **无 CNB 下载镜像**：全仓 `git grep -il cnb` 仅命中 `terracotta.json`（联机中继）与 README。
- `unlisted-versions.json` 是**本地打包资源**（`HMCLCore/src/main/resources/assets/game/`，1439 行），不是远程源。
- `ResourceCleaner` **不是缓存清理器**，而是幂等信号量 RAII 守卫且当前无调用点；真正的缓存清理入口是 `DownloadSettingsPage.clearCacheDirectory`。
- HMCL 硬链接降级存在顺序缺陷：`CacheRepository.restore()` 先 `Files.delete(original)` 再 `createLink()`，跨盘/不支持硬链接时异常被调用方静默 `catch(IOException)` 吞掉，导致原文件已删而内容只在缓存中。**ECL 借鉴时应把 `createLink` 放到 `delete` 之前。**
- HMCL **无 WMI 扫描**（Windows 仅注册表 + 目录 + PATH）；`JavaRestorePage` 管理的是「被禁用的 Java 列表」而非恢复文件；Mojang 与 Disco 两条 Java 来源之间**无自动降级**。
- HMCL 版本 JSON 已从**继承链重构为 patch + priority 模型**（优先级 MC=0 / OptiFine=10000 / 加载器=30000 / LiteLoader=60000），`inheritsFrom` 仅作兼容读取；且 `InheritableProperty` **不在 `game/` 包**，它是「全局/预设 → 实例」的设置继承抽象，与版本 JSON 无关。
- HMCL **MultiMC 导出已移除**（4 条独立证据），现存导出格式只有 MCBBS / Server / Modrinth；`modpack.type.multimc.export` i18n 键已是死字符串。
- HMCL 的 `GameVerificationFixTask` **名不副实**：它只做一件事——MC<1.6 且含 Forge 时删 client jar 内的 `META-INF/MOJANG_C.DSA`/`.SF`；真正的完整性校验入口是 `DefaultDependencyManager.checkGameCompletionAsync`。
- HMCL 实例删除的回收目录是 `{id}_removed`。
- HMCL 哈希级校验只在**上次异常退出**时触发（实例根 `.abnormal` 标记一次性消费），正常启动走「文件存在即跳过」快速路径。这一点与 ECL「显式全量哈希校验」的取舍相反，值得在优化时权衡。
- HMCL **无 NOTICE / THIRD-PARTY 聚合许可清单**，这是它相对薄弱的一环；`docs/schemas/` 下 12 份 JSON Schema 是其用户数据契约，值得 ECL 参考。

**第二轮核实（2026-10-02 追加）——已消除的「未核实」项**：

| 原未核实项 | 核实结论 | 证据 |
| --- | --- | --- |
| HMCL 新版 `DownloadProvider` 是否支持第三方自定义 Provider | ❌ **不支持**：`DownloadProviders` 为 `final` + 私有构造 + `private static final HMCLDownloadProvider PROVIDER`，`getDownloadProvider()` **无 setter、无注册表**；`DownloadProvider` 本身虽是 `public class` 但无扩展点约定 | `setting/DownloadProviders.java:32-33,60,80-82` |
| HMCL `NOT_PATCH_NATIVES` 的运行时消费点 | ✅ **确实被消费**：`NativePatcher.java:127` 读 `settings.getInheritable(GameSettings::notPatchNativesProperty)`；同文件还消费 `useCustomNatives`(:82)、`useNativeGLFWorSDL`(:100)、`useNativeOpenAL`(:101)。**教训**：常量名检索会漏检经 getter 读取的设置，必须按 getter 方法名再检索一次 | `util/NativePatcher.java:79-127` |
| HMCL `JavaInfoUtils` 的具体命令行 | ✅ 不是解析 `java -version` 文本，而是运行 **`<java> -classpath <HMCL jar> org.glavo.info.Main`** 并解析其 JSON 输出（取 `javaVersion` / `javaVendor` / `osArch`） | `HMCL/.../java/JavaInfoUtils.java:42-66` |
| Disco 列表拉取失败的 UI 提示 | ✅ **有**：`warningLabel.setText(i18n("java.download.load_list.failed"))`，文案为 `Failed to load version list` | `ui/main/JavaDownloadDialog.java:216,318,334`；`I18N.properties` |
| HMCL Java 解压失败残留是否清理 | ❌ **失败路径不清理**：`MojangJavaDownloadTask.postExecute()` 仅在 `isDependenciesSucceeded()` 时才 `cleanDirectory(target)` / move / copy+delete tempDir；依赖失败时 `<platformRoot>/.tmp/<name>` **会残留**。**ECL 借鉴时应在 finally 中清理临时目录** | `download/java/mojang/MojangJavaDownloadTask.java:173-190` |
| `DownloadCandidate` 的自定义 retry/超时是否有人用 | 🟡 **机制已接线、无人填充**：`FetchTask.java:113-115` 消费三个字段并回退到 `DEFAULT_RETRY=5` / `NetworkUtils.TIMEOUT=10s`，但全仓仅两个工厂方法且均传 `-1, null, null` → 当前恒取默认值 | `task/FetchTask.java:113-115`、`download/DownloadCandidate.java:22-43` |
| HMCL `OptionalFilesPage` 是否真的可用 | ✅ **完整可用**：独立向导页，`excludedFiles` 空集 = 全装，逐项 `BooleanProperty` 镜像 + 高度自适应 + `retry` 回调 + loading/successful 状态机 | `ui/download/OptionalFilesPage.java:55-105` |
| ECL 是否支持可选文件安装 | ❌ **不支持**：`install_modpack_online` 签名为 `(source, project_id, file_id, game_path, new_version_id)`，**无文件选择/排除参数**；ECL 代码中的 `optional` 是 Modrinth `env.client` 语义，属不同概念 | `ECL/services/game/modpack.py`（签名）、`:228,310,400,647-648` |
| HMCL 路径笔误 | 修正：`NetworkUtils` 实际位于 **`util/io/NetworkUtils.java`**（`TIMEOUT = Duration.ofSeconds(10)` 在 `:50`），不是 `util/NetworkUtils.java` | `util/io/NetworkUtils.java:50` |

---

*本报告由对两侧最新代码的逐文件考古产出，所有结论均可回溯到上文证据路径。*
