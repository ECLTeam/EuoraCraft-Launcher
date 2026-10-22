# EuoraCraft Launcher 全面体检与对标报告

> 生成日期：2026-09-12 · 扫描方式：三路并行深查（项目问题扫描 / HMCL 盘点 / Qomicex.Tauri + PCL-CE 盘点）
> 对标对象：HMCL（E:\Projects\HMCL，JavaFX）、Qomicex.Tauri（Tauri v2 + React + Rust，下称 QML）、PCL-CE（C#/WPF，下称 PCL）
> 关联文档：`docs/feature-recommendations/implementation-plan.md`（五项功能的落地实施方案，本文档同步更新其落地状态）
> 本文所有问题均给出文件:行号证据；对比结论均经源码核实，非文档转述。

---

## 一、总体评价

| 维度 | 评价 |
| --- | --- |
| 架构 | ★★★★☆ IPC 命令模式清晰（api handlers → services coordinators），原子写盘、统一异常边界、窗口权限模型、事件总线均属上乘 |
| 代码卫生 | ★★★★☆ TODO/FIXME 计数为 0，i18n 六语言键完全对称，请求序号守卫防竞态 |
| 功能广度 | ★★★★☆ 已覆盖主流启动器强预期功能的大部（详见对比矩阵），整合包导出/收藏夹/主页广场等尚未布局 |
| 健壮性 | ★★★☆☆ 存在并发竞态（联机建房、锁内网络/磁盘 IO）、自更新校验缺口等 2 个 P0 |
| 易用性打磨 | ★★★☆☆ 核心流程完整，但缺首次引导、崩溃"直达修复"闭环、快捷键体系 |

**最需要立刻处理的是自更新安全性（2 个 P0）**：自更新已实现（`app_update.py`），但引导脚本在替换失败时会删除旧启动器，且安装包下载没有任何哈希校验（写好的校验函数 `_peer_digest_asset` 是死代码）。

---

## 二、项目自身问题清单

### 2.1 逻辑缺陷 / 隐患

| # | 严重度 | 位置 | 问题 | 建议 |
| --- | --- | --- | --- | --- |
| L1 | **P0** | `ECL/services/app_update.py:500-504` | Windows 引导脚本：旧程序移到 backup 后，若新程序 move 被杀软/占用阻断，错误被 `>nul 2>&1` 吞掉，随后 `del /f /q backup` 仍执行——旧版已移走、新版未就位，**启动器本体消失** | 逐步检查 errorlevel，任一步失败即回滚 `move backup→target`，校验新程序可执行后才允许删 backup |
| L2 | **P0** | `app_update.py:164-174, 316-341` | 安装包下载仅校验 GitHub 返回的 `size` 字段；`_peer_digest_asset`（sha256 校验）写好了但从未被调用 | 在 `stage()` 接入 digest 校验；无 digest 资产时告警。可参照 `services/authlib.py:65-97` 已做对的 sha256 校验 |
| L3 | P1 | `services/connector.py:547-586` | `host_port`/`host_instance` 无 `starting` 状态与锁保护：建房期间（fetch_nodes+create_room 可达数十秒）再次点击建房/加入会并发通过，后完成者覆盖房间状态，先创建的房间句柄泄漏且无法 stop | 仿照 `join()` 引入 starting 状态 + RLock 统一 `_transition` 守卫 |
| L4 | P1 | `services/connector.py:183-233` | `fetch_nodes` 持锁做最长 10s 的 HTTP 请求 + 线程池扇出，期间所有建房/加入被阻塞 | 锁内只读写缓存与"refreshing"标记，网络移到锁外 |
| L5 | P1 | `application.py:174-201` | `_build_local_player_icon_provider` 在全局 RLock 内下载皮肤（15s 超时），阻塞所有头像解析 | 缓存读写留锁内，下载移锁外（per-account 去重） |
| L6 | P1 | `services/accounts.py:445-461, 1131-1141` | 微软登录链路在锁内调用 `_save_state()`（Windows 原子写最多 0.37s 重试），且运行于 asyncio 事件循环 → 冻结 IPC 循环 | 锁内只保护内存字典，落盘丢 `to_thread` |
| L7 | P1 | `accounts.py:1239-1242` + `application.py:269-271` | 关闭时在新事件循环里 `await` 主循环创建的 `_login_task`，跨 loop await 必抛 RuntimeError，被过宽 `suppress(Exception)` 静默 | close 只 `cancel()`，在其自身 loop 上 `wait_for` 收尾 |
| L8 | P2 | `application.py:132-138, 477-485` | 运行时热替换 httpx 私有属性 `_transport`，in-flight 请求瞬间报错 | 明确"重启生效"语义或双缓冲 |
| L9 | P2 | `utils/config.py:156-223` | `save_config` 读改写无锁；新增配置键不会深合并进老用户 `setting.json` | 加载时 deepcopy(default) 深合并；save 加锁 |
| L10 | P2 | `utils/config.py:169-178` | 配置 JSON 损坏时静默重置为默认值，用户自定义（游戏目录/代理/主题）无感丢失 | 恢复默认后 emit notify 事件告知备份位置 |
| L11 | P2 | `utils/logging.py:95, 222-230` | `_FRONTEND_BUFFER` 全局单例 shutdown 后不复位，二次初始化被跳过且持有已关闭 EventBus | shutdown 中置 None |
| L12 | P2 | `api/bridge.py:767-799` | 主窗口销毁后 `_webviews["main"]`/`_window_metadata` 不清理，`_main_window_event_bound` 一次性绑定使热重载后事件发往死窗口 | destroy 时弹出自身条目并复位绑定标志 |
| L13 | P2 | `services/accounts.py:1170-1182` | `poll_microsoft_login` 空闲时返回 `status:"error"`（语义应为 idle），污染前端错误态 | 返回 idle 或前端跳过非弹窗期轮询 |
| L14 | P2 | `composables/useAccountManager.ts:439-441` | 取消微软登录后前端状态被置回 `pending`，下次打开弹窗显示陈旧"等待授权"UI | 置回 idle 并清空 stage |
| L15 | P2 | `services/game/launch.py:510-535` | 附加游戏参数字符串拼接进整条命令交给 `create_instance(args=str)`；POSIX 下 Popen(str) 不可用，且绕过参数列表语义 | `create_instance` 改收 argv 列表 |
| L16 | P2 | `app_update.py:52-66, 144-155` | 平台关键词匹配误报（`_DARWIN_KEYWORDS` 含 "app"）；多候选取 `[0]` 依赖 GitHub 返回顺序，可能选错架构 | 加 arch 关键词优先级过滤，多候选告警 |
| L17 | P2 | `api/bridge.py:391-403` | 定向事件分发在无匹配窗口时直接 return，事件既不发也不排队 | labels 为空时回退广播 |
| L18 | P2 | `api/connector.py:77-81` | `connector_status`（前端 2s 轮询）同步调用可能阻塞 5s 的 `get_status()`，冻结事件循环 | 包 `to_thread.run_sync` |
| L19 | P2 | `api/files.py:196-208` | `fs_read_dir` 对条目 `stat()` 的 OSError 未捕获，坏符号链接/断开的网络盘会让整个目录浏览报错 | try/except continue |
| L20 | P2 | `utils/logging.py:95` + `api/client/commands.ts` | 测试间污染根因线索：进程级单例 + 前端模块级单例。test_downloader 全量挂/单跑过与此特征吻合 | 以这些全局为入口排查（已用干净工作区复现确认与本会话改动无关） |

### 2.2 错误处理与用户反馈

| # | 严重度 | 位置 | 问题 | 建议 |
| --- | --- | --- | --- | --- |
| E1 | P1 | `api/settings.py:59`、`api/bridge.py:584,599` | `settings_set`/authlib 历史读写是同步磁盘 IO 跑在事件循环上（对比 workspace.py 全量 `to_thread`）；GameTab 300ms 防抖连续保存会周期性冻结 IPC | 包 `to_thread.run_sync` |
| E2 | P2 | `services/accounts.py:540,684,704,820-875` | `f"认证失败: {exc}"` 等裸异常文本直达用户（主流程 `_make_error_response` 已做友好映射，账户域没跟上） | 仿 bridge `_MODAL_ERROR_MESSAGES`，httpx.RequestError → "无法连接认证服务器" |
| E3 | P2 | `views/Game.vue:767-768` | `'已启动' / '正在启动'` 硬编码中文绕过 i18n（六语言中唯一缺口） | 补 key 走 `t()` |
| E4 | P2 | `features/connect/composables/useConnector.ts:56-70` | 联机状态轮询非首次失败完全静默，后端崩溃时 UI 永远显示最后成功态 | 连续 N 次失败显示"连接中断" |
| E5 | P2 | `views/Connect.vue:459-466`、`Game.vue:706-715` | `loadRunningInstances` 吞错后显示"无运行实例"；`openAccountDetails` 材质读取空 catch，皮肤无端消失 | 失败态提示 |
| E6 | P1 | `api/client/commands.ts:12-30` + `registry.py` | 前端超时只是放弃等待，后端命令继续执行；`guard_ipc_handler` 默认无 timeout。超时重试 → 后端双任务 | 长命令统一走 GameOperation/task_id（install/download 已做，clone/export/import 待补） |

### 2.3 易用性

| # | 严重度 | 位置 | 问题 | 建议 |
| --- | --- | --- | --- | --- |
| U1 | P1 | `useConnector.ts:202-226` + `connector.py:740-750` | 离开联机页只清定时器不退出房间：EasyTier 虚拟网卡/会话挂在后台且无任何指示 | 卸载时确认退出，或全局状态栏常驻联机指示 |
| U2 | P2 | `connector.py:824-855` + `useConnector.ts:185-194` | 端口扫描每 1s 全进程枚举（psutil process_iter + net_connections），CPU 开销可观 | 前 10 次后降频至 3-5s；后端 500ms 结果缓存 |
| U3 | P2 | `connector.py:395-458` | NAT 检测同步阻塞 25s 且无取消入口 | 缩短超时或支持取消 |
| U4 | P2 | `Modal.vue` / `FullscreenModal.vue` | 无统一 Escape 关闭；无 `Ctrl+,` 打开设置等快捷键 | 抽 `useModalEscape` 组合式 |
| U5 | P2 | 设置各 Tab | 数值输入（内存/宽高/模糊度）无 min/max 内联提示，后端有钳制但用户不知范围 | 加滑杆/范围说明 |
| U6 | P2 | `bridge.py:812-858`、`Game.vue` | 首次使用无"添加游戏目录 → 下载版本 → 添加账户"分步引导（后端已有三种 popup 机制可复用） | `hasGamePath===false` 时渲染 onboarding 卡片 |
| U7 | P2 | `Connect.vue:578-585` | 粘贴房间码不即时校验，格式错误要等提交才报 | 粘贴时 `validateRoomCode` 即时标红 |
| U8 | P2 | `InstanceDetailModsTab` / `InstanceResourcesTab` | 模组/资源列表无虚拟化（大型整合包 >500 条会卡）；InstancesTab 已自实现虚拟滚动可复用 | 引入 `useVirtualList` |
| U9 | P2 | `instanceInstallApi.ts:142` | 破坏性命令确认弹窗覆盖不全（卸载版本无 ConfirmDialog 包装） | 统一破坏性命令必经 ConfirmDialog |

### 2.4 一致性 / 健壮性

| # | 严重度 | 位置 | 问题 | 建议 |
| --- | --- | --- | --- | --- |
| C1 | P1 | `types/api.ts:528-550,800-811` vs `ECL/api/registry.py` | **12 个命令只有前端契约、后端未注册**（调用即失败）：`detect_modpack_type`、`import_modpack`、`export_modpack`、`list_resourcepacks`、`list_shaderpacks`、`list_saves`、`remove_resourcepack`、`remove_shaderpack`、`delete_save`、`open_*_folder`×3。真实视图未调用（仅 showcase mock 引用），但类型完整存在，新代码极易踩中 | 删除死契约，或实现整合包类型检测时同步登记 registry |
| C2 | P2 | `registry.py:71` | `build_dispatch` 用 `getattr(api, op)`，契约新增而 handler 缺失要到启动时才炸 | 加 import 期单测逐名 `hasattr` 断言 |
| C3 | P2 | `bridge.py:138` / `app_update.py:119` | `_is_http_url` 重复实现 ×2；`frontend.py` re-export bridge 私有函数当公共 API | 提取 `ECL/utils/http_url.py` |
| C4 | P2 | `services/updates.py:122-123` vs `146-174` | docstring 说 alpha 禁用检测，实现却照常检测——文档与行为不一致 | 二选一：实现禁用或修注释 |
| C5 | P2 | `Game.vue:791-794`、`useAccountManager.ts:476` | 魔法数字（5000ms 强制复位、2000ms 延迟）散落 | 提常量并注释理由 |

### 2.5 性能

| # | 严重度 | 位置 | 问题 | 建议 |
| --- | --- | --- | --- | --- |
| F1 | P2 | `api/bridge.py:81-99` | `_read_image_data_url` LRU 按条数（32）不限字节，大图 base64 膨胀后可驻留数百 MB | 按字节淘汰（仿 `files.py` 的 `_REMOTE_IMAGE_CACHE_MAX_BYTES`） |
| F2 | P2 | `api/files.py:130-148` | 远程图片缓存每次写入全目录 `iterdir+stat` 排序 | 内存索引或每 32 次清理一次 |
| F3 | P2 | `services/accounts.py:558-576, 634-647` | `_emit_changed` 重复调用 `list_accounts`，一次写操作做两次全量 deepcopy+排序 | 复用已构造列表 |
| F4 | P2 | `composables/useAppRuntime.ts:293` | 每秒轮询 pending errors 的 IPC 空转 | 事件驱动 + 指数退避兜底 |
| F5 | P2 | `services/game/servers.py:221-232` | 服务器状态刷新注释提到 30s 缓存但未见实现，反复 ping | 落实短缓存 |

### 2.6 安全

| # | 严重度 | 位置 | 问题 | 建议 |
| --- | --- | --- | --- | --- |
| S1 | **P0** | `app_update.py:164` | 自更新包无哈希校验（同 L2），投毒的二进制会被引导脚本直接执行 | 同 L2 |
| S2 | P1 | `application.py:52-59, 318-322` + `accounts.py:266` | `disable_ssl_verify` 一个布尔即可关闭**全局** TLS 校验（含微软 OAuth 令牌刷新通道），且可被环境变量覆盖 | UI 常驻红色警告；认证通道与下载通道拆分，认证强制校验 |
| S3 | P2 | `api/files.py:150-233` | `fs_read_*` 无路径白名单（纵深防御缺口；主窗口 XSS 已被 DOMPurify 缓解） | 限制在 minecraft_paths + data_path 子树 |
| S4 | P2 | `bridge.py:695-717` | `debug_process_spawn` 未过滤环境变量白名单，debug 模式下前端可拉起继承全量父 env（含 `ECL_CURSEFORGE_API_KEY`）的子进程（launch.py 对游戏进程已做白名单合并） | debug spawn 同样走白名单 |
| S5 | P2 | `services/dev_channel.py:156,317` | dev_channel.json 明文 token（单机场景可接受） | `os.open(..., 0o600)` |

**已确认无问题项**：命令注入（全仓 0 处 `shell=True`，subprocess 均为列表参数）、markdown/插件 HTML 均经 DOMPurify 消毒、authlib-injector 下载带 sha256 校验（正面教材）、CURSEFORGE_API_KEY 不经 IPC/日志外泄。

---

## 三、功能对比矩阵

图例：✅ 完整实现 · 🟡 部分/体验落后 · ❌ 未实现 · ❓ 未核实（子模块/低频路径）

| 功能域 | 功能点 | ECL | HMCL | QML | PCL |
| --- | --- | :-: | :-: | :-: | :-: |
| **账户** | 微软 OAuth | ✅ | ✅ | ✅ | ✅ |
| | 离线账户 | ✅ | ✅ | ✅ | ✅ |
| | 外置登录 authlib-injector | ✅ | ✅ | ✅ | ✅ |
| | 皮肤/披风 3D 预览 + 衣橱 | ✅ | ✅ | ✅ | 🟡 |
| | 账户敏感数据加密存储 | ❌ | ✅(ProtectedPayload) | ❓ | ❓ |
| **实例管理** | 多游戏目录 | ✅ | ✅ | ✅ | ✅ |
| | 版本隔离 | ✅ | ✅ | ✅ | ✅ |
| | 实例重命名/复制/删除 | ✅ | ✅ | ✅ | ✅ |
| | 实例分组/分类 | ✅(分类标签) | ❌ | ✅(拖拽分组) | ✅(分类) |
| | 实例导出为整合包 | ❌ | ✅(向导) | ✅(CF/mrpack/qmodpack) | ✅ |
| | 实例右键快捷菜单 | 🟡(列表行动作) | ✅(悬停弹出菜单) | ✅ | ✅ |
| | 版本设置继承（全局↔实例） | 🟡(实例独立设置) | ✅(Inheritable) | 🟡 | ✅ |
| **下载** | 原版/快照/远古版本 | ✅ | ✅ | ✅ | ✅ |
| | Forge/Fabric/NeoForge/Quilt/OptiFine | ✅ | ✅(更多: LiteLoader/Cleanroom/LegacyFabric) | ✅(更多: Babric/Cleanroom) | ✅ |
| | 可视化"版本+Loader 合并安装"一页流 | 🟡(分步) | 🟡(分步向导) | 🟡 | ✅(一页流) |
| | 模组/资源包/光影/数据包在线下载 | ✅ | ✅ | ✅ | ✅ |
| | 世界存档在线下载 | ✅ | ✅ | ✅ | ✅ |
| | CurseForge + Modrinth 双源 | ✅ | ✅ | ✅(+FTB) | ✅ |
| | mcmod 中文译名 | ✅ | ✅ | ✅ | ✅(可选样式) |
| | BMCLAPI 镜像切换 | ✅ | ✅(自动切换) | ✅(+自动换源冷却) | ✅ |
| | 下载失败自动换源回退 | ❌ | ✅ | ✅ | ❌ |
| | 模组前置递归解析+一键连装 | ❌ | 🟡(依赖提示) | ✅ | ❌ |
| | 模组批量更新检查 | ❌ | ✅(AddonUpdatesPage) | ✅(哈希+6h 缓存) | ❌ |
| | 资源收藏夹+批量下载 | ❌ | ❌ | ❌ | ✅ |
| **整合包** | CurseForge/Modrinth 导入 | ✅ | ✅(+MultiMC/MCBBS) | ✅(+FTB/拖拽) | ✅ |
| | 整合包导出 | ❌ | ✅ | ✅ | ✅ |
| **启动** | Java 自动扫描 | ✅ | ✅ | ✅ | ✅ |
| | Java 缺失自动下载 | ❌ | ✅ | ✅ | ❓ |
| | 内存设置 | ✅ | ✅ | ✅ | ✅ |
| | 动态推荐内存 | ❌ | ❌ | ❌ | ✅(按游戏类型) |
| | 内存锁定(-Xms=-Xmx) | ❌ | 🟡 | ❓ | ❓ |
| | 进程优先级 | ❌ | ✅ | ❓ | ✅ |
| | JVM 参数/环境变量/启动前后命令 | ✅ | ✅ | ✅ | ✅ |
| | QuickPlay 快捷进入存档/服务器 | ✅ | ✅ | ❓ | ❓ |
| | 启动进度分步显示 | ✅ | ✅ | ✅(SSE) | ✅ |
| | 测试游戏实时日志独立窗口 | ✅(终端模块) | ✅(LogWindow) | ✅(独立窗口) | ✅ |
| | 游戏退出联动动作 | ❌ | ✅(自动隐藏/显示) | ❓ | ✅(关启动器/清内存) |
| **联机** | 内置联机 | ✅(Scaffolding+EasyTier) | ✅(Terracotta) | ✅(SCF+EasyTier 内嵌) | ✅(SCF+EasyTier) |
| | 与其他启动器互通 | ✅(与 QML 共用节点) | ✅ | ✅(与 ECL/PCL 互通) | ✅ |
| | 中继节点列表 | ✅ | ✅ | ✅(+自动获取) | ✅ |
| | 房主踢人 | ✅ | ✅ | ✅(+持久封禁/黑名单) | ❓ |
| | 主机 Mod 校验/强制同步 | ✅ | ❓ | ✅ | ❓ |
| | 世界发现（大厅广播） | ❌ | ❓ | ❓ | ✅ |
| | 联机实名/账号体系 | ❌ | ❓ | ❌ | ✅(NAID) |
| **存档/世界** | 世界清单/详情/备份 | ✅ | ✅ | ✅ | ✅ |
| | 世界备份锁定 | ✅ | ✅ | ❓ | ❓ |
| | 世界导入/导出 | ✅ | ✅ | ✅ | ✅ |
| | level.dat 可视化编辑 | ✅(2026-09 落地) | ✅(NBT 编辑器) | ✅(ADR-022) | ❌ |
| | 原理图 3D 预览 | ✅(2026-09 落地) | ✅(SchematicsPage) | ✅(Deepslate+材质提取) | ❌ |
| **截图** | 截图管理/封面/背景 | ✅ | ❓ | ❓ | ✅ |
| **服务器** | servers.dat 管理 | ✅ | ✅ | ❓ | ✅ |
| | 在线状态/MOTD/人数 | ✅ | ❓ | ❓ | ✅(ServerQuery 卡) |
| | 实例专属服务器列表 | ❌ | ❓ | ❓ | ✅ |
| **崩溃/日志** | 崩溃自动分析 | ✅(规则库) | ✅(52 条规则+中文) | ✅(44 种模式+AI) | ✅(完整子系统) |
| | 崩溃"直达修复"动作闭环 | ❌ | ✅(GameCrashWindow) | ❓ | ✅(三键对话框) |
| | 崩溃报告导出分享 | ✅ | ✅ | ✅ | ✅ |
| | 重复模组检测 | ❓ | ✅ | ✅ | ✅(ModIndex 反查) |
| **更新** | 更新检测 | ✅ | ✅ | ✅ | ✅ |
| | 启动器自更新 | ✅(刚实现，待修 L1/L2) | ✅(--apply-to) | ✅(独立 Updater) | ✅ |
| **插件系统** | 插件生态 | ✅(Python 宿主+前端 sdk+系统插件) | ❌ | ✅(L2/L3 WASM/L4 WebView+商店+签名) | ❌ |
| **个性化** | 主题/亮暗 | ✅(classic/folia) | ✅(+Material You) | ✅(Catppuccin 预设) | ✅ |
| | 自定义背景图/透明度/模糊 | ✅ | ✅ | ✅(+视频背景) | ✅(+视频/音乐) |
| | 背景音乐/视频随游戏暂停 | ❌ | ❌ | ❌ | ✅ |
| | 自定义主页/主页广场 | ❌ | ❌ | ✅(小组件网格) | ✅(XAML 主页+广场) |
| | 首次启动初始化向导 | ❌ | ❓ | ✅(快速/自定义) | ❓ |
| **设置** | 代理 | ✅ | ✅ | ✅ | ✅ |
| | 多语言 | ✅(6 语言) | ✅(10 语言) | ✅(7 语言) | ✅ |
| | 遥测/匿名统计 | ❌ | ✅(Countly) | ✅(可关闭) | ❓ |
| | 反馈状态跟踪 | ❌ | ❌ | ❌ | ✅ |
| **其他** | 展示模式/演示数据 | ✅(独有) | ❌ | ❌ | ❌ |
| | 开发者通道/DevTools | ✅(独有) | ❌ | ✅(调试面板) | ✅(调试区) |
| | 插件测试插件加载验证 | ✅ | — | — | — |

> ECL 独有优势：展示模式（无后端演示数据，对本仓库的开发/测试体验价值很大）、DevTools 与开发者通道、插件测试插件回归验证。ECL 已落地的两项此前规划（level.dat 可视化编辑、原理图 3D 预览）在矩阵中以"2026-09 落地"标注。

---

## 四、三大参考启动器值得移植的设计（按投入产出比排序）

### 第一梯队：低成本高感知

1. **崩溃"直达修复"闭环**（PCL `CrashDialogPresenter.cs`）——ECL 崩溃分析已能定位原因，补"前往修复（跳转对应实例 Loader/模组页）/ 导出报告"两个动作按钮即可闭环。HMCL 的 `CrashReportAnalyzer` 52 条规则正则库可补充进 ECL 规则库。
2. **下载失败自动换源回退**（HMCL `*DownloadProvider`、QML 自动换源+冷却）——ECL 后端 `base.py` 已支持 official/bmclapi 双源，补"首选失败换源重试一次"逻辑（原方案 P1 内容，仍未落地）。
3. **下载链接剪贴板自动识别**（PCL `PageSetupGameManage.xaml:175`）——几十行代码的开关 + 监听，用户感知极强。
4. **公告按钮直通动作**（PCL `AnnouncementService` + `CustomEvent`）——ECL 信息卡已支持远程公告，给公告项加可选 action 字段即可跳转功能页。
5. **房间码粘贴即时校验**（U7）与**服务器在线状态卡**（PCL `MinecraftServerQuery`）——ECL 服务器状态刷新接口已存在，只差在实例管理页展示。

### 第二梯队：中期功能补齐

6. **可视化"版本+Loader 合并安装"一页流**（PCL `PageDownloadInstall`）——当前 ECL 分步安装，改造成单页联动勾选 + Fabric API 依赖联动校验。
7. **模组批量更新检查 + 前置递归安装**（QML ADR-011/014；HMCL `AddonCheckUpdatesTask`）——ECL 已有 `checkResourceUpdates` 命令骨架，补哈希匹配与下载编排。
8. **整合包导出**（HMCL `ExportWizardProvider` / QML `modpack_export.rs`）——同时顺带清理 C1 的 12 条死契约（`export_modpack` 等已有前端契约无后端实现）。
9. **Java 缺失自动下载**（HMCL `JavaInstallTask`、QML）——ECL 有扫描无下载，新玩家最大门槛。
10. **动态推荐内存 + 内存锁定 + 进程优先级**（原方案 P0，仍未落地；PCL 按游戏类型推荐的做法可参考）。
11. **首次启动初始化向导**（QML ADR-017）——配合 U6 的 onboarding 卡片。
12. **实例分组拖拽管理**（QML ADR-019）——ECL 已有分类标签体系，补拖拽与右键菜单。
13. **世界发现式联机**（PCL `DiscoverWorldAsync`）——ECL 联机已成熟，补"发现局域世界"交互降低上手门槛。

### 第三梯队：长线打磨

14. **自定义主页/主页广场**（PCL 主页广场、QML react-grid-layout 小组件）——ECL 信息卡/组件化基础好，可做 Markdown 主页 + 社区源。
15. **插件签名信任链与插件商店**（QML ADR-049/050）——ECL 插件系统已有权限模型，补 `.qplugin` 式包格式与 Ed25519 签名。
16. **崩溃规则库扩展**——把 HMCL 52 条规则与 QML 44 种模式并入 ECL `crash_analysis.py`，并考虑"重复模组检测"。
17. **背景音乐/视频随游戏暂停**（PCL `ModMusic`/`ModVideoBack`）——ECL 已有运行中实例事件，联动成本低。
18. **多语言扩展**——ECL 6 语言已对称；HMCL 10 语言、崩溃原因逐条中文化的粒度值得对标。

---

## 五、建议路线图（结合已有 implementation-plan 的落地状态）

| 阶段 | 内容 | 对应问题编号 |
| --- | --- | --- |
| **M0（本周，纯修复）** | 自更新引导回滚 + sha256 校验接入；host_port 竞态 + fetch_nodes 锁外网络；settings_set/authlib 移出事件循环；账户锁内落盘/跨 loop await；connector_status 包线程 | L1/L2/S1、L3/L4、E1、L6/L7、L18 |
| **M1（小改动批量）** | 12 条死契约清理 + hasattr 断言测试；Game.vue i18n 缺口；取消登录状态、poll idle 语义；配置损坏通知；静默吞错三处提示；房间码即时校验；剪贴板下载识别；公告 action | C1/C2、E3、L13/L14、L10、E4/E5、U7、四-1.3、四-1.4 |
| **M2（原方案续期）** | 动态推荐内存/锁定/优先级（原 P0）；下载源自动回退（原 P1 后半）；Java 自动下载；合并安装一页流 | 四-2.6/2.9/2.10、四-2.7 |
| **M3（功能补齐）** | 崩溃直达修复闭环 + 规则库扩充；模组批量更新+前置连装；整合包导出；实例分组拖拽；世界发现联机 | 四-2.7/2.8、四-1.1、四-2.12/2.13 |
| **M4（长线）** | 主页广场、插件签名/商店、背景联动、多语言扩充 | 四-3.14~18 |

## 六、备注

- 本报告的 ECL 问题扫描基于主仓库代码（子模块 `ECL/game`、`ECL/services/florolding` 仅略读）；`tests/test_downloader.py` 在全量运行时的失败已在干净工作区复现，属测试间状态污染，根因线索见 L20。
- 三方联机互操作是既成事实：QML README 明确与 ECL 共用联机节点并兼容 SCF 拓展协议（ECL 侧对应 `qomicex_compat` 系统插件与 `docs/plugin-connector-extensions.md`），PCL-CE 亦实现同一协议。联机相关决策（如封禁策略、实名）可参考但需与 QML 侧协同。
