# ADR-005：整体移除插件权限系统

- 日期：2026-11-08。
- 状态：用户已确认并实施，本地验证完成，未推送或发布。

## 问题与选择

插件 Worker API v2 配有一套完整的权限系统：清单声明 `permissions` 字段（13 种能力）、用户在主窗口逐项 allow/deny、`grant_store` 持久化授权记录、每个运行时 API 经 `authorize(capability) → GrantLease → check()` 四重核对。权限概念渗透到 17 个运行时模块、3 个 IPC 端点、前端审批弹窗与资源选择器。

用户明确要求：将这套系统全面移除，不再有"权限"这个概念。经确认的边界：

1. **彻底全删**：声明、审批、授权存储、运行时能力检查、权限 UI、资源选择器全部删除；启用的插件可使用全部 Worker API。
2. **内容绑定只剥权限维度**：代码快照复核、依赖锁哈希、ABI 标签、`.eclplugin` 安装校验（manifest_sha256）、内容变化阻断全部保留；仅 `content_digest` 不再包含权限声明。
3. **epoch 失效机制保留非权限职责**：异步操作时效、去重回执代次绑定、面板/订阅缓存失效、会话关闭失效、操作归属全部保留；"授权变更"这一递增来源随授权消失。
4. **`allow_ui_injection` 全局开关一并移除**：UI 注入能力随插件启用可用。
5. **停用插件时其发起的宿主侧操作继续完成**（与既有跨页面任务约定一致）。
6. **主仓库与两个插件仓库同步**删除清单 `permissions` 字段。

## 契约

- 清单不再有 `permissions` 字段；`WorkerManifest` 使用 `extra="ignore"`，历史清单中的该字段被忽略而非报错。
- 启用流程收敛为 register → enable，无审阅门禁；管理器状态值不再有 `review_required`/`permission_denied`。
- 运行时 API 从 `authorize(capability, refs)` 改为无能力 `lease()`；`GrantLease` 更名 `PluginLease`（仅 installation_id、content_digest、run_id、run_epoch 四元组），`grant_models`/`grant_store` 更名 `run_models`/`run_store`，`PluginGrantStore`/`PluginGrantSession`/`PluginGrantError` 相应更名，`grant_epoch` 更名 `run_epoch`。
- 数据视图不再按授权范围过滤：实例文件访问覆盖目录中全部活动根；Java 与账户目录全量可见；站点允许清单删除（`NetworkOrigin` 解析与内网/特殊用途地址防护保留）。
- IPC 删除 `plugin_permissions_get`、`plugin_permissions_update`、`plugin_permission_resources` 三个端点及对应模型与日志策略条目。
- 前端删除 `PluginPermissionsModal`、`pluginPermissionValidation`、`PluginResourceSelector`、`PluginLocalResourceSelector`、`enableBasic`/`enableReviewed` 流程及插件页审阅分支；i18n 清理权限文案。

## 信任模型

启用插件后，第三方 Worker 可以读取全部账户摘要视图、访问宿主目录中全部活动实例文件、向任意公网地址发起 GET/HEAD、注入主窗口界面、登记认证提供方。**第三方插件按受信任代码对待**，用户应只启用来源可信的插件。与权限无关的安全边界全部保留：内容变化阻断（TOCTOU）、运行代次失效、安装包完整性校验、内网地址防护、跨运行引用拒绝、任务归属。

## 验证

- 后端：`ruff check ECL tests`、`ruff check .`、`ruff format --check` 通过；全量 pytest **2725 passed / 57 skipped**（含内容变化阻断、epoch 失效、操作归属、Worker 生命周期等保留行为覆盖）。
- 前端：`pnpm check`（prettier、eslint、design、page-root、typecheck、test）通过，**1268 passed**。
- 协议与指南文档同步：`eclplugin-package-format.md` 启用与信任模型、实例/Java/账户/网络各节，插件开发指南九个文件，`plugin-connector-extensions.md`。
