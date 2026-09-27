# ECL × Qomicex.Tauri × PCL-CE × HMCL 功能对比与优化整体方案

> 生成日期：2026-09-26。四个仓库均已更新至当日最新提交：
> - Qomicex.Tauri（main）：`cff4361` fix(ui): 重做更新弹窗三层布局
> - PCL-CE（dev）：`510e59f2` imp: 服务器卡片 MyHint 换行占位（2.15.1-beta.1）
> - HMCL（main）：`59bcc7fe6` 在 JavaFX27+ 环境中使用原生窗口装饰
> - EuoraCraft-Launcher（main）：`16341c0`
>
> 本文所有结论均经逐文件核实并附证据路径（相对各仓库根）。Qomicex.Tauri 的 5 个业务子模块（core/downloader/connector 等）本地为空检出，其能力描述基于调用点、依赖声明与官方文档，已标注。

---

## 0. 结论速览

| 维度 | 结论 |
| --- | --- |
| ECL 明显缺失、三家全有的硬功能 | **Java 运行时自动下载**、**OptiFine 安装**、**整合包在线安装（CurseForge/Modrinth 直连 resolve）与更多格式**、**MCBBS/HMCL 格式整合包导入** |
| ECL 缺失、至少两家有的高价值功能 | Mod 批量哈希反查更新、存档 level.dat 可视化编辑器、世界 2D 地图查看器、wrapper/post-exit/环境变量/窗口尺寸启动项、启动器可见性选项、mcmod 简介、机器翻译、下载引擎 ETag 缓存与内容寻址硬链接库缓存、慢连接自愈、指纹化版本扫描缓存 |
| ECL 缺失、仅一家有的可选项 | 原理图管理仅 ECL 已有 3D 版（HMCL/Qomicex 也有此域）、统一通行证内置（Qomicex）、YggdrasilConnect OAuth 版外置登录（PCL）、联机账号实名体系（PCL Natayark）、音乐播放器（PCL）、隐藏功能系统（PCL） |
| ECL 独有优势 | Python 插件系统 + 独立 Worker 进程隔离 + .eclplugin 归档事务安装、插件 Vue SFC/WebView 注入与逐命令鉴权、开发者通道 WebSocket、原理图 3D 查看器 + 材料清单导出、单实例互斥、Showcase 演示模式、第三方启动器实例兼容层、本地衣柜（皮肤/披风库） |
| 最紧急的安全项 | 账户令牌明文存储风险（对比 PCL 的 DPAPI+AES-GCM、HMCL 的 AES/GCM、Qomicex 的加密 accounts.dat）；.eclplugin 无签名（对比 Qomicex Ed25519 三级信任链） |
| 借鉴代码许可注意 | HMCL、Qomicex 为 GPL-3.0（可借鉴/合并需同许可并署名）；PCL-CE 主程序为自定义许可证（只能参考设计思路，不能复制代码） |

---

## 1. 四项目概览

| | ECL | Qomicex.Tauri | PCL-CE | HMCL |
| --- | --- | --- | --- | --- |
| 定位 | 全功能 MC 启动器，插件生态优先 | 全功能 MC 启动器（测试阶段），联机与个性化优先 | PCL2 社区发行版，下载体验与联机优先 | 老牌跨平台启动器，平台兼容与引擎成熟度优先 |
| 技术栈 | Python(pytauri) + Tauri + Vue 3.5 | Rust(Tauri v2 + axum) + React 19 | .NET 10 WPF（net10.0-windows） | Java 17+ JavaFX（HMCLCore/HMCL/HMCLBoot 三模块） |
| 架构形态 | 单 Python 进程内嵌 Tauri 事件循环；插件走独立 Worker 进程 | 四层：Tauri 壳（IPC 命名管道）→ React SPA → axum 后端 → Rust 业务子模块；后端 exe 内嵌进壳 | 主程序 + PCL.Core 框架层 + 源生成器 | 引擎库 HMCLCore（纯逻辑）+ JavaFX UI + 引导壳 |
| 规模 | ECL 主仓 170 个 IPC 命令、67 个测试文件 | 30 个 endpoint 模块、24 个 service | 851 个 .cs/.xaml、约 15.6 万行 | 约 872 个 Java 主源文件 |
| 许可 | GPL-3.0 | GPL-3.0 | PCL2 目录自定义许可 / PCL.Core Apache-2.0 | GPL-3.0 |
| 近期活跃方向（最近提交） | CLI 快速启动、归档插件安装事务 | 更新弹窗/CI/实例视图持久化 | 下载器分片重构、档案系统重构、TCP 转发 | 原理图管理、平台适配、i18n 工程化 |

---

## 2. 功能对比矩阵

图例：✅ 完整实现；🟡 部分/受限；❌ 无。括号内为证据路径（ECL 相对本仓根，其余相对各自仓库根）。

### 2.1 账户体系

| 功能 | ECL | Qomicex | PCL-CE | HMCL |
| --- | --- | --- | --- | --- |
| 微软 OAuth 设备码 | ✅（ECL/game/Core/MicrosoftAuth.py 839 行） | ✅（endpoints/auth.rs） | ✅（PCL.Core/…/MicrosoftProvider.cs） | ✅（auth/microsoft/MicrosoftService.java） |
| 微软授权码 + PKCE + 本地回调 | ❌ | ❌ | ❌ | ✅（OAuthServer.java，NanoHTTPD 回调） |
| 离线账户（UUID 派生/自定义） | ✅（accounts_add_offline） | ✅（account.rs 内联 MD5） | ✅ | ✅（OfflineAccountFactory） |
| authlib-injector 外置登录 | ✅（services/authlib.py + YggdrasilAuth.py 509 行，服务器历史 20 条） | ✅（默认 LittleSkin，DnD 一键添加，ALI 解析） | ✅（AuthlibProvider + 预设列表 + 拖拽排序 + OAuth 能力探测 #3594） | ✅（auth/authlibinjector/ + 服务器列表分发 authlib-injectors.json + 元数据缓存） |
| 外置登录 OAuth 版（YggdrasilConnect） | ❌ | ❌ | ✅（YggdrasilConnectProvider.cs，discovery + 内置 ClientId） | ❌ |
| 统一通行证内置 | 🟡（无内置，但插件可注册自定义登录表单 plugins/auth_providers.py） | ✅（auth/tongyi） | ❌ | ❌ |
| 皮肤上传/重置（微软 API） | ✅（upload_skin/reset_skin） | ✅（/skin/upload） | ✅（MySkin.xaml.cs） | ❌（仅离线注入） |
| 披风管理 | ✅（set_cape/reset_cape） | ✅（微软官方披风 equip/unequip） | ✅（BtnSkinCape） | ❌ |
| 本地皮肤库/衣柜 | ✅（services/wardrobe.py，哈希去重/导出/同步应用） | 🟡（skin save-to + 头像裁剪） | 🟡（皮肤预览/保存/刷新） | ✅（离线皮肤经本地 Yggdrasil + javaagent 注入，auth/offline/YggdrasilServer.java，独门方案） |
| 3D 皮肤预览 | ✅（SkinViewer3D.vue，skinview3d） | ✅（skinview3d + 全景背景） | ✅（皮肤页） | ✅（自研 JavaFX SkinCanvas，含动画） |
| 多账户/档案管理 | ✅（收藏/置顶/刷新/去重/事件推送） | ✅（默认账户 + 实例绑定） | ✅（刚重构：McProfile/SafeProfile 脱敏 + 加密存储 + lastUsed + 迁移 #3561） | ✅（全局/便携双存储 + 跨目录迁移 + 每实例启动时选择） |

### 2.2 下载引擎与镜像

| 功能 | ECL | Qomicex | PCL-CE | HMCL |
| --- | --- | --- | --- | --- |
| 并发下载/动态并发 | ✅（DynamicSemaphore，game/Core/Downloader.py 850 行） | ✅（threads 1-512 可配） | ✅（全局连接配额 256 + 每主机配额租约） | ✅（Task 框架） |
| 大文件分片并行 | ✅（>10MB 且支持 Range，最多 200 片） | ✅（file_chunk_threads 分段） | ✅（自适应 8MB 分段，最多 1024→2048 段，Channel+多 worker） | 🟡（文件级多 URL，无 HTTP Range 分片） |
| 慢连接自愈（慢速段拆分重下） | ❌ | 🟡（子模块内，未核实） | ✅（15s 读超时 + 8s 慢速检查 50KB/s 判慢拆分重下，AdaptiveRangeDownloader.cs） | ❌ |
| 断点续传 | 🟡（任务暂停/恢复 + 3 轮重试） | ✅（downloader 子模块） | ✅（文件预分配 + 随机写段） | 🟡（重试 5 次 + ETag，非 Range 续传） |
| 全局内存缓冲预算（防 OOM） | ❌ | ❌ | ✅（512MB 预算 + ArrayPool 租约，#3462） | ❌ |
| 双镜像源（官方/BMCLAPI） | ✅（请求级回退 + 失败文件备用源补全，services/game/download_sources.py） | ✅（三层：全局镜像/NeoForge BMCLAPI 优先/自建 CDN 重写） | ✅（文件源+版本列表源分离切换） | ✅（AUTO 合并多源列表 + 候选 URL 逐个回退） |
| 未上架版本补充源（愚人节等） | ❌ | ✅（april_fools 过滤） | ✅（alist.8mi.tech unlisted-versions） | ✅（unlisted-versions.json + versions.txt） |
| ETag/条件请求缓存 | ❌ | ❌ | 🟡 | ✅（util/CacheRepository.java etag.json） |
| 内容寻址硬链接缓存（多实例省盘） | ❌ | ❌ | ❌ | ✅（CacheRepository.createLink 按 sha1 硬链接共享库文件） |
| 限速 | ✅（令牌桶 RateLimiter） | ❌（未见） | ✅（全局限速 ThrottleAsync） | ❌（未见） |
| HTTP/3 与按源协议路由 | ❌ | ✅（QUIC 回退；modrinth CDN 强制 H1 并行实测快 3.7 倍） | 🟡（HTTP 模式可配） | ❌ |
| DoH DNS（含 SRV 解析） | ❌ | ❌ | ✅（多 DoH 竞速 + SRV，PCL.Core/IO/Net/Dns/） | ❌ |
| 代理 | ✅（双通道：API 走 transport、下载走 ECL_DOWNLOAD_PROXY） | ✅（设置项） | ✅（含按实例代理 + JVM 注入） | ✅（启动时注入 JVM 代理） |

### 2.3 版本与加载器安装

| 功能 | ECL | Qomicex | PCL-CE | HMCL |
| --- | --- | --- | --- | --- |
| 原版（正式/快照/历史） | ✅（catalog.py 分类清单） | ✅ | ✅ | ✅（+版本号比较引擎 GameVersionNumber） |
| Forge（含新旧安装器） | ✅（install_profile 处理器链 + 旧版回退，ECL/game/Core/LoaderInstaller.py） | ✅ | ✅ | ✅（ForgeNew/OldInstallTask） |
| NeoForge | ✅ | ✅（中文环境 BMCLAPI 优先） | ✅ | ✅（新旧安装任务） |
| Fabric（+API 可选） | ✅（安装时可选 Fabric API，从 Modrinth 查询） | ✅（+qsl 附加） | ✅ | ✅ |
| Quilt | ✅ | ✅ | ✅（可设置忽略） | ✅ |
| OptiFine（含 Forge 组合） | ❌ | ✅ | ✅（OptiFine+Forge 组合安装） | ✅ |
| LiteLoader | ❌ | ✅ | ✅ | ✅ |
| Cleanroom / LegacyFabric | ❌ | ✅（Cleanroom/LegacyFabric/Babric） | ✅（Cleanroom/LegacyFabric/LabyMod） | ✅（Cleanroom/LegacyFabric） |
| 安装事务化（失败不留半成品） | 🟡（安装后 _retry_install_downloads 补全） | ✅（失败自动清理） | 🟡（LoaderCombo 流水线） | ✅（GameRepositoryDraft 快照 + commit/abort，game/GameRepositoryDraft.java，最佳实践） |
| 实例完整性校验与修复 | ✅（inspect/repair_instance_files + FilesChecker SHA1） | ✅（/resources/complete） | ✅（补全文件） | 🟡（启动前自动校验，可关） |

### 2.4 整合包

| 功能 | ECL | Qomicex | PCL-CE | HMCL |
| --- | --- | --- | --- | --- |
| 在线整合包安装（CurseForge/Modrinth 直连） | ❌（仅本地导入） | ✅（resolve + install，含 FTB） | ✅（下载页 PageDownloadPack） | ✅ |
| Modrinth .mrpack | ✅ 导入+导出 | ✅ | ✅ | ✅ |
| CurseForge | ✅ ZIP 导入 | ✅ | ✅ | ✅ |
| MCBBS/HMCL 格式 | ❌ | ❌ | ✅（自动识别 6 种格式） | ✅（MCBBS + Server 格式） |
| MultiMC 导入 | ❌ | ✅（parse-folder/import） | ✅（带启动器压缩包） | ✅（导出已移除，导入保留） |
| 导出 | ✅ .mrpack（export_instance_pack） | ✅（zip 导出任务化） | ✅（Modrinth 上传模式 + zip + glob 排除 + 配置可保存） | ✅（Mcbbs/Server/Modrinth） |
| 可选文件选择安装 | ❌ | ❌ | ❌ | ✅（OptionalFilesPage #1771） |
| 导出时哈希反查（免重传） | ❌ | ✅（CF MurmurHash2/Modrinth SHA1） | 🟡 | 🟡 |

### 2.5 Java 运行时管理

| 功能 | ECL | Qomicex | PCL-CE | HMCL |
| --- | --- | --- | --- | --- |
| 本机扫描 | ✅（注册表/JAVA_HOME/PATH/常见目录/macOS JVM，带缓存，game/Utils/JavaScanner.py） | ✅（quick/deep 两档） | ✅（5 种扫描器：注册表/默认路径/微软商店/PATH/where） | ✅（注册表/WMI/常见目录/macOS jre.bundle，javaCache.json） |
| 自动下载 JRE | ❌ | ✅（Adoptium 目录 + 下载 + 解压 + 注册） | ✅（Mojang piston-meta + BMCLAPI 镜像） | ✅（Mojang + foojay Disco API 双源） |
| 版本要求推断 | ✅（按版本 JSON javaVersion 校验） | ✅（requirement/recommended 端点） | ✅ | ✅（JavaVersionConstraint） |
| 每实例 Java 偏好 | ✅ | ✅ | ✅（UserPreference） | ✅（javaType/customJavaVersion/AUTO） |

### 2.6 启动能力

| 功能 | ECL | Qomicex | PCL-CE | HMCL |
| --- | --- | --- | --- | --- |
| JVM 参数/内存自定义 | ✅（归一化/锁定堆/G1GC/ZGC） | ✅（自动=可用 70%/自定义） | ✅（初始堆可调 #3565 + 固定堆 #3282 + -Xmn 15%） | ✅（metaspace/permsize/自动 GC 选择/低内存 auto agent） |
| 版本隔离 | ✅（全局策略 + 实例覆盖） | ✅（全局 + 实例覆盖，目录规则明细） | ✅（全局 + 每实例） | ✅（ALWAYS/MODDED/NEVER 预设 + 每实例运行目录） |
| 多游戏目录 | 🟡（多根扫描） | ✅（settings.directories） | 🟡 | ✅（GameDirectoryManager 多仓库） |
| 预启动命令 | ✅（game.pre_launch_command + 插件四阶段钩子） | ❌ | ✅（全局 + 每实例，可等待完成） | ✅ |
| 包装命令 wrapper | ❌ | ❌ | ✅（Java Wrapper 自释放） | ✅ |
| 后退出命令 | ❌ | ❌ | ❌ | ✅（postExitCommand） |
| 自定义环境变量 | ❌ | ❌ | ❌ | ✅（environmentVariables Map） |
| 窗口标题自定义 | ❌ | ❌ | ✅（全局 + 每实例 + 左下角版本信息） | 🟡（仅 native 装饰相关） |
| 启动器可见性（启动后隐藏/退出） | ❌ | ❌ | ✅（LauncherVisibility 枚举） | 🟡（launcher visibility 设置） |
| 全屏/分辨率启动参数 | 🟡（全屏写 options.txt） | ❌ | 🟡 | ✅（width/height/fullscreen） |
| QuickPlay 快捷进入服务器/世界 | ✅（quickPlayMultiplayer，老版本降级 --server/--port，launch_target.py） | ✅（服务器 + 世界 + 版本门槛判断） | ✅（进存档 + 进服务器 + OptiFine 警告） | ✅（单人/多人/Realms 三类，每实例属性） |
| 进程监控 | ✅（psutil + 优先级五档 + 输出环形缓冲 500 行） | ✅（launch_tracker + 运行页 + kill） | ✅（Watcher 状态机 + 窗口检测 + 音乐联动） | ✅（ManagedProcess + 窗口检测 + 日志分级） |
| 崩溃检测 | ✅（多信号判定 + 异步分析线程） | ✅（退出码 + crash-reports/hs_err 收集 + 游玩时长结算） | ✅ | ✅（进程异常事件） |
| 崩溃自动分析 | ✅（913 行规则引擎 + 敏感脱敏 + 插件富化 + 导出 ZIP） | ✅（44 种错误模式 + mclo.gs 上传 + 二维码） | ✅（结构化流水线：29 规则/45 原因枚举 + 堆栈定位肇事 Mod + 三方 Mod 索引 + 置信度分级 + 证据链） | ✅（100+ 条正则规则 + 关键词提取，CrashReportAnalyzer.java） |
| 渲染器软回退 | ✅（mesa-loader-windows 注入） | ❌ | ✅（mesa-loader-windows + 渲染器选择） | ✅（graphicsBackend/openGLRenderer/vulkanRenderer） |
| 进程优先级 | ✅（五档） | ❌ | ✅（RealTime~Low + PriorityBoost） | ✅（processPriority） |
| 高性能 GPU | ✅（Windows GPU 偏好登记） | ❌ | ✅（管理员 --gpu 提权重启） | ✅（HMCL_FORCE_GPU） |
| CLI 快速启动 | ✅（--launch/--server/--world，frontend_ready 后派发） | ❌ | ❌ | ❌（仅更新器 --apply-to） |
| 启动取消 | ✅（cancel_launch） | ✅ | ✅ | ✅ |

### 2.7 实例/版本管理

| 功能 | ECL | Qomicex | PCL-CE | HMCL |
| --- | --- | --- | --- | --- |
| 扫描与自动识别 | ✅（多根 + 后台监视线程 0.75s 防抖 + 事件推送） | ✅（6 级回退 + 自动修复 gameVersion/loader + 指纹缓存） | ✅（30 版本缓存 + 卡片分组） | ✅ |
| 第三方实例兼容 | ✅（instance_compat.py 识别 PCL/HMCL 元数据 + 插件扩展点，独有） | ✅（PCL 图标兼容） | —（自身即 PCL） | —（自身即 HMCL） |
| 分组/分类 | ✅（分类 CRUD + 颜色 + 置顶） | ✅（groups.json 多对多 + 颜色） | ✅（详细分类开关） | ✅ |
| 图标 | ✅（四类：自动/内置/加载器/本地图） | ✅（内置 8 + PCL 风格整合包图标 + 后端降采样 128px 防 413） | ✅（自定义 Logo） | ✅（+愚人节图标） |
| 克隆 | ✅（clone_instance） | ❌ | ✅（右键复制） | ✅ |
| 删除/重命名 | ✅ | ✅（+隐藏） | ✅ | ✅（移入 xxx_removed 回收式目录） |
| 导出 | ✅（实例 ZIP / mrpack） | ✅（整合包导出） | ✅（Modrinth 上传 + zip + 配置保存） | ✅ |
| 实例级设置三态继承 | ✅（.ecl/settings.json + profile store 覆盖） | ✅（实例覆盖） | ✅（ArgConfig 三态 + ConfigSource.GameInstance） | ✅（InheritableProperty 继承/覆盖/恢复 + JSON Schema 校验 + 保存备份，41 个 PROPERTY_*） |
| 组件级安装/升级/卸载 UI | 🟡（重装/修复） | ✅ | ✅（修改加载器页） | ✅（InstallerListPage 每组件一行） |
| 更新实例时同步 client jar | 🟡（repair 覆盖） | 🟡 | 🟡 | ✅（#6843 自动同步） |
| 运行统计 | ✅（.ecl 运行时长/计数 + 第三方对账，version_stats.py） | ✅（游玩时长结算） | ❌ | ❌ |

### 2.8 资源管理

| 功能 | ECL | Qomicex | PCL-CE | HMCL |
| --- | --- | --- | --- | --- |
| 本地 Mod 列表/禁用 | ✅（jar 元数据解析 + .disabled 原子操作） | ✅（元数据 enrich + .disabled） | ✅（+ .old 格式） | ✅ |
| 在线搜索双源 | ✅（Modrinth + CurseForge，分类/排序/分页） | ✅（Modrinth/CF/FTB 聚合 + all） | ✅（Modrinth + CF 统一封装 + 收藏夹 + 分享码 + 剪贴板识别） | ✅（Modrinth + CF） |
| Mod 批量哈希反查更新 | 🟡（逐资源 identify_resource_hash） | ✅（Modrinth POST v2/version_files 批量 + CF fingerprints + 6h 缓存 + 自动检查） | ✅ | ✅（AddonCheckUpdatesTask） |
| 一键更新（严格兼容校验） | ✅（当前游戏版本+加载器 + 原子替换） | ✅（更新编排交下载中心） | ✅ | ✅ |
| 依赖/前置检测 | ✅（RequiredModDependencies.vue） | ✅（详情依赖解析） | ✅（自动装前置策略 + Jar-in-Jar 5 层解析 #3378 + 下载原因追踪） | 🟡 |
| mcmod 中文名 | ✅（离线库中文名反查英文搜索 + 百科链接，mcmod.py） | ✅（gzip 内嵌离线库 + 两段式回填） | ✅（protobuf mcmod.buf + 两套名字格式） | ✅（mod_data.txt） |
| mcmod 简介/机器翻译 | ❌ | ✅（MyMemory/Google/Bing 机翻） | ✅（简介） | ❌ |
| 资源包/光影/数据包 | ✅（统一 list/install/toggle/delete + 重复哈希标记 + 数据包定位世界目录） | ✅ | ✅ | ✅（+资源包图标延迟加载缓存 #6730） |
| 存档管理 | ✅（列表/详情/NBT 安全改写/复制/导入导出/图标/ZIP 备份（保留 10 个）/Chunkbase 链接） | ✅（+ level.dat 可视化编辑器：模式/难度/天气/出生点/边界/游戏规则 + level.dat_old 恢复） | ✅（NBT 解析 + 存档编辑器 + DataVersion 边界表） | ✅（备份/恢复/导出 zip 专页） |
| 世界 2D 地图查看器 | ❌ | ✅（region 渲染瓦片 + leaflet + 方块探针） | ❌ | ❌ |
| 截图管理 | ✅（日期分组 + WebP 缩略图 + 剪贴板 + 设为封面/背景） | ✅ | ✅ | ❌（仅打开文件夹） |
| 服务器列表/状态查询 | ✅（servers.dat NBT 读写 + 收藏 + mcstatus 查询 30s 缓存） | ✅（读写 + ping + 局域网发现） | ✅（McPing 新旧协议 + 玩家列表） | ❌（无 ping/query） |
| 原理图管理 | ✅（litematic/schem 解析 + 分块会话 + three.js 3D + 材料 CSV 导出，schematics.py 1168 行，独有 3D） | ✅（+ Deepslate WebGL 预览 + 材质合规提取） | ✅（元数据解析） | ✅（schem/schematic/nbt/litematic 四格式 + NBT 编辑器联动 #6094） |
| options.txt 游戏设置编辑 | 🟡（instance_options.py 服务层，无 schema 化 UI） | ✅（内嵌 options.json 定义 + 多语言描述 + 数组 chips） | 🟡 | 🟡（自动生成 game options） |
| NBT 可视化编辑器 | ❌ | ✅（level.dat 编辑器） | ✅（存档编辑器） | ✅（NBTEditorPage） |
| 在线存档下载 | ✅（WorldDownloadTab） | ✅（saves 分类） | ✅（World 下载页） | ✅ |
| 剪贴板/拖拽资源导入 | ✅（拖放 dataTransfer） | ✅（全局拖拽分类安装 DropInstallDialog） | ✅（剪贴板链接识别） | 🟡 |

### 2.9 联机

| 功能 | ECL | Qomicex | PCL-CE | HMCL |
| --- | --- | --- | --- | --- |
| 内网穿透组网 | ✅（EasyTier-PyO3 库内嵌 + no_tun 用户态 + KCP + zstd，florolding 子模块） | ✅（EasyTier 库内嵌 + smoltcp + 非管理员回退） | ✅（easytier-core.exe 进程 + RPC 控制） | ✅（Terracotta，按平台分类器下载核心） |
| 房间协议 | ✅（Scaffolding-MC，florolding） | ✅（SCF，与 HMCL/PCL-CE 互通） | ✅（Scaffolding 帧协议 + 房间码 34 进制） | ✅（陶瓦协议） |
| 房间码/加入 | ✅（generate/validate） | ✅ | ✅（U/ 前缀分享码） | ✅ |
| 主机端口自动探测 | ✅（Java 进程扫描 + varint 握手探测） | ✅（scan-ports） | ✅（GetServerPort） | ✅ |
| NAT 类型检测 | ✅（STUN） | ✅（STUN 双 binding + 多服务器降级缓存） | ✅（StunInfo + 对称 NAT 端口猜测） | ✅ |
| 踢人 | ✅（machine_id） | ✅（+ 物理封禁 + 误踢审核状态机 + 解封） | ✅ | ✅ |
| 房间 Mod 校验 | 🟡（connector_match_instances 为空占位） | ✅（Mod 卡片 + 强制同步） | ✅ | ✅ |
| 局域网广播伪装 | ✅（florolding Broadcaster） | ✅（lan-games） | ✅（UDP 广播伪装局域网世界） | ✅ |
| 联机账号体系 | ❌（本地优先，无第三方账号） | ✅（插件商店账号体系） | ✅（Natayark ID OAuth2 + 实名/封禁校验 + 游客） | ✅（Terracotta 体系） |
| TCP 转发 | ❌ | ✅ | ✅（TcpForwardWorker：信号量限连 + 优雅回收，刚重构 #3593） | ✅ |
| IPv6 | ❌ | 🟡 | ✅（配置项） | ✅ |

### 2.10 UI/UX

| 功能 | ECL | Qomicex | PCL-CE | HMCL |
| --- | --- | --- | --- | --- |
| 主题系统 | ✅（双内置主题 classic/folia + Material 色板 + 主色/圆角/透明度/模糊） | ✅（自定义 .qtheme 主题包 + 图标主题 + 玻璃材质级微调） | ✅（Lab 色彩空间引擎 + 预设 + HSL 渐变自定义） | ✅（25 类主题引擎 + 壁纸 Monet 取色 + .hwt 主题包导入导出） |
| 自定义背景 | ✅（背景视频流：令牌 + Range，截图设背景） | ✅（图片/视频/GIF/APNG + 模糊/遮罩 + 上传端点） | ✅（视频背景 + 游戏时自动暂停 + 高级模糊着色器） | ✅（图片/网络背景 + 缓存策略） |
| 深浅色 | ✅（含定时切换，独有定时） | ✅（跟随系统） | ✅（跟随系统） | ✅（跟随系统 + macOS 修复） |
| 多语言 | ✅ 6 种（zh-CN/zh-TW/en/ja/ru/de） | ✅ 7 种（含 zh-HK/en-GB） | ✅ 7 种（zh-TW/en-GB 新增） | ✅ 10 种（含文言 lzh、镜像文字彩蛋，i18n 工程化最佳） |
| 通知/弹窗 | ✅（notify/popup 幂等/error modal + error_id 补录确认） | ✅（MessageBox + toast + 任务完成通知） | ✅（MyToast/MyMsg） | ✅ |
| 公告系统 | ✅（本地 + 远程校验缓存） | ✅（通道缓存回退 + 已读持久化） | ✅（公告可触发 19 种事件） | 🟡（更新提示） |
| 日志窗口 | ✅（浮动日志窗 + 实例终端支持 stdin 输入，独有） | ✅（SSE 独立窗口 + 后端托管浏览器页双通道 + 5000 行上限） | ✅（实时日志页 + 脱敏） | ✅（过滤/计数/导出 jstack） |
| 主页形态 | ✅（账户卡 + 信息卡 + 启动栏） | ✅（可拖拽小组件仪表盘 + 插件贡献组件 + 布局迁移） | ✅（自定义主页 XML + 事件系统 + 新闻视图） | ✅（启动卡 + 内存状态条） |
| 标题栏 | ✅（三模式：custom/native/system-shadow Win11） | ✅（Win 自绘，Linux/macOS 保留系统） | ✅（自绘装饰器） | ✅（JavaFX27+ 原生装饰） |
| 系统托盘 | ❌（仅标题栏托盘区组件） | ❌ | ❌ | ❌（四家均无） |
| 单实例互斥 | ✅（文件 + pid + TCP 令牌握手 + 第二实例置前，四家唯一） | ❌ | ✅（SingleInstanceService + 命名管道 RPC） | ❌ |
| 拖拽安装 | ✅ | ✅（全局分类对话框） | ✅ | 🟡 |
| 首次启动向导 | ❌ | ✅（快速 6 步/自定义 9 步） | ❌ | ❌ |
| 隐藏功能系统 | ❌ | ❌（F8×8 隐藏调试 tab） | ✅（可隐藏页/子页/功能，入口自隐藏） | ❌ |
| 音乐播放器 | ❌ | ❌ | ✅（NAudio + SMTC + 游戏自动暂停） | ❌ |
| 动画控制 | ✅（主题级） | ✅（GSAP + 速度/GPU/帧率上限） | ✅（自研 Clock/KeyFrame 框架 + FPS 限帧） | ✅（高刷自适应 + 减少动效检测） |
| 无障碍 | ❌ | ❌ | ❌ | 🟡（减少动效/HiDPI/Wayland 缩放） |
| 彩蛋 | ❌ | ✅（愚人节 TNT 图标 + F8×8） | ✅（1/1000 PLC logo + 关于页连点链 + 一键卸载） | ✅（文言文/愚人节开关） |

### 2.11 插件与扩展

| 功能 | ECL | Qomicex | PCL-CE | HMCL |
| --- | --- | --- | --- | --- |
| 插件系统 | ✅ Python 进程内：事件/命令/设置/路由/CSS/HTML/Vue 插槽与路由，声明式权限 + 依赖拓扑 | ✅ 四层 l0-l4（manifest 贡献点/iframe 沙箱/wasmtime WASM 网关/独立窗口） | ❌ | ❌ |
| 插件分发与签名 | 🟡 .eclplugin 归档（逐文件 SHA-256 + 预检 + 依赖锁 + 离线 wheel + 安装事务回滚），**无作者签名** | ✅ 商店 + Ed25519 三级信任链（根公钥→开发者证书→包签名）+ 灰度更新 | ❌ | ❌ |
| 插件商店 | ❌ | ✅（市场/评价/登录/设备码授权/遥测白名单） | ❌ | ❌ |
| 插件运行时隔离 | ✅ 独立 Worker 进程 + 专用 Python/uv 运行时（固定哈希）+ 有界 JSON 帧 | ✅ WASM 沙箱（wasmtime + host 函数 + 权限） | ❌ | ❌ |
| 插件 UI 注入 | ✅ Vue SFC 热注册（sucrase 转译）+ 独立 WebView 窗口逐命令白名单鉴权 | ✅ inline/iframe/独立窗口 | ❌ | ❌ |
| 插件参与启动/联机/崩溃分析/账户 | ✅ 四阶段启动钩子 + Scaffolding 扩展协议 + 崩溃富化 + 自定义认证提供方 | ✅（hooks/slots/downloadSources 贡献点） | ❌ | ❌ |
| 插件开发工具 | ✅ 开发者通道（本地 WebSocket + 令牌 + 日志回放 + 内嵌前端托管） | ✅ @qomicex/cli（create/dev/pack/verify/publish）+ Playwright harness | ❌ | ❌ |
| CLI 参数 | ✅（--data-dir/--debug/--log-level/--disable-plugins/--frontend-dist/--dev-channel/--launch/--server/--world） | ❌（deep link 无） | ✅（update execute/activate/promote 子命令 + RPC 命名管道） | ❌（仅 --apply-to） |
| deep link | ❌ | ❌ | ❌（四家均无 OS 级 deep link） | ❌（hmcl:// 仅内部协议） |

### 2.12 自更新

| 功能 | ECL | Qomicex | PCL-CE | HMCL |
| --- | --- | --- | --- | --- |
| 更新渠道 | ✅（GitHub Releases 通道匹配 alpha/beta/release） | ✅（release/beta/alpha 三列车独立序数 + 服务端时间裁决） | ✅（stable/beta × x64/arm64 + beta 不降级逻辑） | ✅（stable/dev/nightly） |
| 多源回退 | ❌（仅 GitHub） | ✅（自建 API + 30min 缓存） | ✅（Mirror 酱 CDK → Minio ×2 → GitHub） | ✅（更新源可 -D 重定向 + CNB 镜像） |
| 安装方式 | ✅（StagedUpdate 替换计划 + 引导脚本 + 优雅重启 + 遗留清理） | ✅（独立 Updater 二进制内嵌 + minisign + 强制更新 + snooze 24h） | ✅（UpdateRestart + CLI 协作） | ✅（SHA-256 + 自签名校验 IntegrityChecker + --apply-to 外部替换 + 回滚迁移） |
| 更新日志展示 | 🟡 | ✅（更新弹窗三层布局） | ✅（公告含 changelog） | 🟡 |

### 2.13 平台与打包

| 功能 | ECL | Qomicex | PCL-CE | HMCL |
| --- | --- | --- | --- | --- |
| Windows | ✅ x64 + arm64（CI 矩阵） | ✅ x64 + arm64（NSIS） | ✅ x64 + arm64（单文件） | ✅（exe 引导壳，Win7-11） |
| Linux | ✅（Ubuntu CI + Arch makepkg 冒烟） | ✅（deb/rpm/AppImage） | ❌（仅交叉编译开发） | ✅（deb + 脚本，FreeBSD 兼容） |
| macOS | ✅（PyInstaller DMG） | ✅（x64/arm64 DMG） | ❌ | ✅ |
| 国产/冷门架构 | ❌ | ✅ LoongArch64 CI（RISC-V 实验性） | ❌ | ✅（MIPS/PPC/S390/RISCV/LOONGARCH 新旧世界，最强） |
| 打包注入密钥 | ✅（MICROSOFT_CLIENT_ID/CURSEFORGE_API_KEY 构建注入 build_env.py） | ✅（appsettings.json 内嵌占位） | ✅（Secrets.cs 全构建注入） | ✅（-D 系统属性可覆盖） |

### 2.14 安全与隐私

| 功能 | ECL | Qomicex | PCL-CE | HMCL |
| --- | --- | --- | --- | --- |
| 账户令牌加密存储 | ❌（需确认；当前 accounts 目录未见加密层） | ✅（accounts.dat CryptHelper 加解密） | ✅（CNG/DPAPI 包裹密钥 + AES-GCM/ChaCha20 认证加密 + 版本化迁移，最完善） | ✅（AES/GCM 弱信封，防误读不防本地攻击者） |
| 日志脱敏 | ✅（崩溃分析 redaction_patterns + 令牌/用户名） | 🟡 | ✅（McLogFilter：accessToken + Windows 用户名） | 🟡 |
| 自完整性校验 | ❌ | ✅（更新包签名） | 🟡 | ✅（IntegrityChecker jar 自签名校验） |
| API Key 保护 | ✅（构建注入，不落盘） | 🟡（内嵌 appsettings.json） | ✅（构建注入） | ✅（jar manifest/系统属性） |
| 网络边界校验 | ✅（远图 50MB/15s、zip 安全解压、路径穿越校验、IPC 白名单） | ✅（CSP + 插件权限） | ✅（XAML 事件净化 + 路径穿越修复） | 🟡 |
| 遥测 | ❌（无，隐私友好） | ✅（匿名错误遥测白名单） | ✅（默认关闭 + Sentry 黑名单） | 🟡（countly 本地展示不上传） |

### 2.15 工程化

| 功能 | ECL | Qomicex | PCL-CE | HMCL |
| --- | --- | --- | --- | --- |
| 后端测试 | ✅ 67 个 pytest 文件 + AST 架构约束测试（tests/test_architecture.py） | 🟡（cargo test + 前端无单测） | ✅（PCL.Core.Test） | ✅（核心测试如 Draft 221 行） |
| 前端测试 | ✅（vitest + pinia testing，pnpm check） | ❌ | ❌ | ❌ |
| 静态检查 | ✅ ruff 全家桶 + pnpm check | 🟡（clippy advisory 138 条 / ESLint 63 error 未清） | ✅ | ✅（check-codes CI） |
| 版本发布 | ✅ semantic-release + beta 标签强约束 | ✅（版本列车脚本 bump-version.mjs） | ✅（metadata.json） | ✅（三渠道 release matrix） |
| 打包产物 | Nuitka onefile + PyInstaller DMG + Arch pkg | 全平台安装包 + 更新 zip | 单文件 exe | exe + deb |
| API 文档 | ❌（170 命令无生成文档） | ❌（无 OpenAPI） | 🟡（源生成器配置文档） | ❌ |

---

## 3. 推荐添加功能清单（按优先级）

优先级定义：P0 = 直接决定产品竞争力的硬缺口或安全项；P1 = 显著提升体验、三家中有两家具备；P2 = 差异化/锦上添花。工作量：S ≤ 2 天、M ≤ 1 周、大 = 2 周以上（含测试）。

### P0（必须补齐）

| # | 功能 | 理由 | 参考实现 | 工作量 |
| --- | --- | --- | --- | --- |
| 1 | **Java 运行时自动下载与管理** | 三家全有、ECL 仅扫描。新用户装不了 Java 是最大流失点 | Mojang piston-meta（HMCL download/java/mojang/ + PCL ModJava.cs:343-486，BMCLAPI 有镜像）或 Adoptium（Qomicex java.rs:299-480）；复用现有 Downloader；落位 `ECL/services/game/java_runtime.py` + 设置页管理 UI + 启动前自动补装 | M |
| 2 | **OptiFine 安装支持** | 主流加载器仅缺此项；BMCLAPI 提供列表 | HMCL download/optifine/OptiFineInstallTask + BMCLAPI 列表；PCL-CE 有 OptiFine+Forge 组合安装可作为进阶 | M |
| 3 | **整合包在线安装 + 格式扩展** | 当前仅本地导入 3 格式；竞品可在线浏览/安装 CF/Modrinth/FTB 整合包，且支持 MCBBS/HMCL/MultiMC 格式 | Qomicex modpack.rs resolve/install 端点设计；下载页加"整合包"Tab；导入侧补 MCBBS mcbbs.packmeta 与 MultiMC 文件夹解析（HMCL HMCLCore/modpack/multimc/ 的 json-patch 引擎可借鉴） | M-大 |
| 4 | **账户令牌加密存储** | PCL（DPAPI+AES-GCM）、HMCL（AES/GCM）、Qomicex（加密 dat）均有；ECL `~/.ECL/accounts` 若为明文属高危 | PCL-CE EncryptHelper.cs 双层设计：随机主密钥用 DPAPI（Windows）/keyring（POSIX）包裹，数据用 AES-GCM 带版本魔数头，旧数据自动迁移 | S-M |
| 5 | **.eclplugin 签名信任链** | 归档体系已完备但无作者认证（docs/eclplugin-package-format.md 自述）；插件是 ECL 核心卖点，供应链必须闭环 | Qomicex plugin_signature.rs：Ed25519 根公钥内置 → 开发者证书 → 包签名，canonicalJson 规范化；安装时校验，未签名保持现有 confirm_unverified_source 流程 | M |

### P1（强烈建议）

| # | 功能 | 理由 | 参考实现 | 工作量 |
| --- | --- | --- | --- | --- |
| 6 | Mod 批量哈希反查更新 | ECL 逐资源查询慢；批量端点一次拉全 | Qomicex：Modrinth `POST /v2/version_files`（sha1 批量）+ CF fingerprints + 6h 缓存 + 定时自动检查 | S |
| 7 | 存档 level.dat 可视化编辑器 | 游戏模式/难度/天气/出生点/游戏规则/世界边界编辑是高频需求；ECL 已有自研 NBT 解析（utils/nbt.py）与安全改写 | Qomicex SaveSettingsDialog.tsx（含 level.dat_old 恢复）；ECL 在 worlds.py 上加字段 schema + 前端表单 | M |
| 8 | 世界 2D 地图查看器 | 与现有原理图 3D 形成"看世界"组合卖点 | Qomicex world_view.rs（region 渲染瓦片 + 方块探针）+ 前端 leaflet；Python 侧可用 anvil 解析 + PNG 瓦片生成 | M-大 |
| 9 | 崩溃分析升级为结构化管道 | 规则表已 913 行，继续堆规则会不可维护 | PCL-CE CrashAnalysis/（规则目录 + CrashStackAnalyzer 堆栈包名定位肇事 Mod + CrashModIndex 三源索引 + 置信度分级 + 证据链）；HMCL CrashReportAnalyzer 的 100+ 规则可移植扩充 | M |
| 10 | 启动项补全：wrapper / post-exit 命令 / 环境变量 / 窗口尺寸全屏 / 启动器可见性 | 全是低成本高感知项；HMCL 全占（LaunchOptions.java + 41 个 PROPERTY_*），PCL 大半 | ECL launch.py `LaunchContext` 直接扩展；settings 默认分区加对应键 | S |
| 11 | 原理图管理对齐 HMCL：nbt 结构方块格式 + 存档内联预览 | HMCL #6094 支持 .nbt；ECL 已有 litematic/schem | HMCLCore/schematic/ 四格式抽象 | S |
| 12 | mcmod 简介 + 机器翻译 | 中文社区核心体验 | PCL-CE 简介（mcmod.buf）；Qomicex translation.rs（MyMemory/Google，免费层即可） | S |
| 13 | 下载引擎增强：ETag 缓存 / 慢连接自愈 / 内存预算 / 库文件硬链接缓存 | 磁盘与带宽双省；HMCL 硬链接缓存对多实例用户价值极大 | HMCL CacheRepository.createLink（按 sha1 硬链接）；PCL AdaptiveRangeDownloader.cs 慢速段拆分（8s/50KB/s 判慢）与 DownloadResourceManager 512MB ArrayPool 预算 | M |
| 14 | 版本扫描指纹缓存 + 事件驱动监听 | Qomicex 实测 73 实例 37.6s→11.4s；ECL 当前 0.75s 轮询 | Qomicex scan_cache.rs（目录指纹）；Windows ReadDirectoryChangesW / POSIX inotify 替代轮询 | S |
| 15 | 微软 WebView/授权码登录 | 设备码在移动端体验差 | HMCL OAuthServer.java（PKCE + 本地 HTTP 回调页）；Tauri 侧可用 shell 打开浏览器 + 回环监听 | M |
| 16 | 联机补全：实例匹配实装 + 房间 Mod 校验 + 踢人封禁/审核 | connector_match_instances 目前是空占位（api/connector.py:138）；竞品已内置 | Qomicex kick.rs（deny 封禁 + 重连审核状态机）+ Mod 校验卡片；ECL 已有插件扩展协议可挂接 | M |
| 17 | 更多加载器：LiteLoader / Cleanroom / LegacyFabric | BMCLAPI 均有列表，接入成本低 | HMCL download/{liteloader,cleanroom,legacyfabric}/ | S（每个） |
| 18 | 错误码 i18n 映射 | ECL 已有稳定 errorCode 体系，前端直接展示原文 | Qomicex i18n/errors.ts：errorCode → 翻译键 + 参数插值 | S |

### P2（差异化与长线）

| # | 功能 | 说明 |
| --- | --- | --- |
| 19 | 系统托盘 + 最小化到托盘 | 四家均无，做了即独有；配合现有 useTrayItems |
| 20 | deep link `ecl://`（一键安装整合包/资源/加入联机房） | 四家均无 OS 级 deep link；单实例协议 v1 已预留 action 位（single_instance.py），天然衔接 |
| 21 | 首次启动向导（语言/主题/Java/内存/下载源） | Qomicex InitialSetupWizard 6/9 步模式 |
| 22 | 壁纸取色主题（Monet 式）+ 主题包分发格式 | HMCL WallpaperColorExtractor；ECL 已有 material-color-utilities 依赖基础 |
| 23 | 统一下载中心（聚合安装/Java/资源/插件/更新任务 + 速度曲线 + 暂停恢复） | Qomicex DownloadCenter + SSE 三通道聚合；ECL 有 GameOperationManager 基础 |
| 24 | 插件商店（市场/评价/灰度更新） | ECL 插件体系最完整，缺分发端；签名（P0-5）先行 |
| 25 | 隐藏功能系统 / 关于页彩蛋链 / 愚人节 | PCL 传统艺能，社区向情感化功能 |
| 26 | 音乐播放器（SMTC 系统媒体控制 + 游戏自动暂停） | PCL ModMusic.cs |
| 27 | DoH DNS + SRV 解析 | PCL.Core/IO/Net/Dns/，对国内网络环境有实际价值 |
| 28 | i18n 扩容（en-GB/zh-HK + 语言回退链 + CI 同步） | 对标 HMCL 的 i18n 工程化（sublanguages.csv + 别名表） |
| 29 | 无障碍专项（减少动效检测、焦点管理、对比度） | 四家基本空白，可做行业标准亮点 |
| 30 | LoongArch64 / RISC-V 构建 | HMCL/Qomicex 已验证需求存在；Nuitka 交叉编译需预研 |
| 31 | 更新多源回退（GitHub → 镜像加速） | PCL Mirror 酱/Minio 模式；国内可用性 |
| 32 | HTTP/3 + 按源协议路由 | Qomicex state.rs:262-303（modrinth CDN 强制 H1 并行实测快 3.7 倍），httpx 受限时可评估 aioquic 或 Rust 边车 |

---

## 4. 代码与架构优化方案（针对 ECL）

### 4.1 安全加固（最高优先）

1. **账户凭据加密信封**（对应 P0-4）：`ECL/services/accounts.py` 持久化层加 `EncryptedStore`（AES-GCM + 版本头 + 旧明文迁移），主密钥 Windows 走 CNG/DPAPI、POSIX 走 libsecret/keyring，均不可用时回退口令派生并明确告知。
2. **插件包签名**（对应 P0-5）：`ECL/plugins/package_*` 加 Ed25519 验签；CI 产出签名密钥分离（根密钥离线保管）。
3. **IPC 命令面收敛**：`ECL/api/registry.py:34-244` 的 170 个硬编码命令名与 `FrontendApi` 方法靠 `getattr` 弱耦合——改为注册时自动收集命令名 + 一致性 pytest（防拼错漂移），并让 `command_names` 仅供前端类型生成。

### 4.2 架构债清偿

4. **异常边界统一**：`guard_ipc_handler`（bridge.py:252）与 `_ipc_handler` 装饰器（bridge.py:270）双轨并存，统一为 registry 单一路径，避免错误码行为漂移。
5. **拆分 `frontend_ready` 上帝方法**：bridge.py:758（`# noqa: C901`，130 行）按"就绪注册 / 事件绑定 / 引导提示 / CLI 派发"拆成四个私有步骤，CLI 派发已有 `_cli_launch_dispatched` 防重入，拆分无风险。
6. **httpx 私有属性替换**（application.py:139 `client._transport = ...`）：封装 `HttpClientFactory`，代理热切换改为重建 client + 原子换引用，消除升级 httpx 的破坏风险。
7. **大文件拆分**：launch.py（1103 行）→ `launch_context`（参数与事务）/`launch_monitor`（进程与日志）/`crash_capture`；resources.py（1429）、schematics.py（1168）同理按读模型/写模型/会话拆分。GameService 的 Mixin 聚合方式不变。
8. **配置热更新去 deepcopy**：application.py:542 每次 `config:updated` 全量 deepcopy + 重放 CLI 覆盖；改为按变更键差分应用（config 层已能给出变更路径）。

### 4.3 性能优化（对标竞品已验证收益）

9. **版本扫描指纹缓存**：scan.py 加目录指纹（mtime+size+名称集合哈希），无变化直接复用上次结果——Qomicex 同款优化实测 3.3 倍。
10. **目录监听事件化**：0.75s 轮询线程 → Windows `ReadDirectoryChangesW` / POSIX inotify（或 watchdog），保留轮询兜底。
11. **图标管线**：后端对本地图片图标降采样（Qomicex pcl_icon.rs 因 22.4MB 响应体 413 踩过的坑）；前端列表图标懒加载 + 缓存（HMCL #6730）。
12. **下载引擎**（对应 P1-13）：ETag 条件请求缓存；库/资产按 SHA1 内容寻址 + 硬链接复用；慢速段拆分重试；下载缓冲全局预算（`Resource` 信号量或 ArrayPool 思路）防大文件 OOM。

### 4.4 崩溃分析与日志

13. **结构化重构**（对应 P1-9）：规则目录（声明式 `CrashRule` dataclass）+ 堆栈分析器（包名/关键词 → 肇事 Mod）+ Mod 索引（崩溃报告 ∪ Forge 调试日志 ∪ mods 目录）+ 置信度输出；现有插件富化扩展点保留为管道末端步骤。
14. **错误码前端 i18n**（对应 P1-18）+ `launcher:error` 的 error_id 补录机制文档化。

### 4.5 工程卫生

15. **仓库产物清理**：`tests/.pytest-app-update/`、根目录 `ECL_data/`、`.minecraft/` 运行产物加 .gitignore 并清出索引。
16. **IPC 接口文档生成**：从注册表自动生成 170 命令的 Markdown/OpenAPI 文档（供插件开发者），随 semantic-release 发布。
17. **单实例协议第二期**：`single_instance.py` 已预留 action 位，落地第二实例参数转发（`ecl.exe --launch <目标>` 唤起已有实例），与 P2-20 deep link 打通。
18. **平台一致性验证**：Linux 下 transparent + shadow 组合降级路径（adapters/tauri.py:105-115）与 `minWidth: 960` 经验值（tauri.py:130）做一轮实测并注释依据。

---

## 5. 建议落地路线图

| 阶段 | 内容 | 出口标准 |
| --- | --- | --- |
| 第 1 阶段（安全与地基） | P0-4 令牌加密、P0-5 插件签名、4.1/4.2 的 4/5/6/7 项架构债 | ruff + pytest 全绿；旧明文账户自动迁移测试通过 |
| 第 2 阶段（硬功能补齐） | P0-1 Java 下载、P0-2 OptiFine、P0-3 整合包在线安装、P1-17 补加载器 | 安装向导可完成 OptiFine/整合包全流程；`pnpm check` + `pnpm build` 通过并实机验证 |
| 第 3 阶段（体验升级） | P1-6/7/8/10/12/14/18 + 4.3 性能组 | 批量更新、level.dat 编辑、地图查看器上线；扫描缓存生效 |
| 第 4 阶段（生态与差异化） | P1-16 联机补全、P1-9/13 引擎与崩溃重构、P2 托盘/deep link/下载中心/插件商店 | 联机房间 Mod 校验闭环；deep link 打通单实例协议 |

---

## 附：来源说明

- 功能矩阵中 ECL 结论来自对主仓与 frontend/ECL/game/florolding 子模块的逐文件盘点；Qomicex 子模块（core/downloader/connector）为空检出，其内部能力以调用点与文档为准，已在涉及处保守标注。
- 各竞品证据路径：Qomicex（src-backend/qomicex-backend/、src-tauri/、src/）；PCL-CE（Plain Craft Launcher 2/Modules/、PCL.Core/）；HMCL（HMCLCore/src/main/java/org/jackhuang/hmcl/、HMCL/src/main/java/org/jackhuang/hmcl/）。
- 许可提醒：HMCL/Qomicex 为 GPL-3.0，移植其代码需保持同许可并保留版权声明；PCL-CE 主程序为自定义许可，仅可借鉴设计思路。
