# 插件权限系统移除实施方案

- 日期：2026-11-08。
- 状态：已实施并验证（2026-11-08）。后端 pytest 2725 passed / 57 skipped，前端 pnpm check 1268 passed；插件仓库清单已同步（OnlineGame 0f04146、QomicexCompatible 098b836）。
- 决策者：用户。
- 关联：[ADR-003](../ADR-003-connector-and-qomicex-external-plugins.md)（引入权限审阅）、[ADR-004](../ADR-004-plugin-settings-center-default.md)。

## 1. 已确认的产品决策

1. **权限概念彻底移除**：声明、审批、存储、运行时能力检查、权限 UI 全部删除；启用的插件可使用全部 Worker API。
2. **内容绑定只剥权限维度**：代码快照复核、依赖锁哈希、ABI 标签、安装校验（manifest_sha256）、内容变化阻断全部保留；`content_digest` 移除权限声明成分。
3. **epoch 机制保留非权限职责**：异步操作时效、去重回执代次绑定、面板/订阅缓存失效、会话关闭失效、操作归属全部保留；"授权变更"不再是 epoch 递增来源。
4. **停用语义**：停用/更新插件时，其发起的宿主侧操作（下载、安装、启动）继续完成，操作归属机制不变。
5. **全局 UI 注入开关一并移除**：`allow_ui_injection` 配置项与 `_ui_injection_allowed` 门控删除，UI 注入能力随插件启用可用。
6. **主仓库与两个插件仓库同步**：插件仓库删除 `plugin.json` 的 `permissions` 字段并更新自身文档。
7. 移除后插件获得：全部实例读写、全部账户读取、全部 Java 运行时、任意网络访问。第三方插件从此按完全受信任代码对待。

## 2. 架构变更总述

```
现状：plugin.json 声明 → 宿主校验 → 用户审批 → GrantStore 记录
      → Worker 启动建立 ContentBinding → 运行时 authorize(capability) → GrantLease
      → 每次副作用 check(lease)（内容 + 授权 + epoch 四重核对）

目标：plugin.json 无 permissions 字段
      → 启用即建立 ContentBinding（代码 + 锁 + ABI）
      → 运行时领取纯时效 Lease（run + epoch）
      → 副作用前 check(lease)（内容 + epoch 核对，无能力判断）
      → 数据视图全量：全部实例 / 全部账户 / 全部 Java
```

命名随职责收敛（AGENTS 1.1）：`grant_*` 名称在授权移除后不再成立，本次变更内一次性重命名：

- `ECL/plugins/grant_models.py` → `ECL/plugins/run_models.py`（保留 `PluginContentBinding`、`GrantLease→PluginLease`、`PluginGrantError→PluginRunError`；删除权限模型）
- `ECL/plugins/grant_store.py` → `ECL/plugins/run_store.py`（`PluginGrantStore→PluginRunStore`：安装登记、内容复核、epoch；删除授权记录与决策）
- `ECL/plugins/permissions.py` 整体删除（旧代 PermissionManager）

## 3. 后端改动清单

### 3.1 删除

| 文件/符号 | 内容 |
| --- | --- |
| `ECL/plugins/permissions.py` | 旧代 PermissionManager 全删（198 行） |
| `grant_models.py` 权限部分 | `PermissionDeclaration`、`_PermissionScope`、`PermissionDecision`、`PermissionView`、`GrantView`、`PermissionManagementCaller`、`expanded_capabilities`、能力 Literal 目录 |
| `grant_store.py` 授权部分 | 授权记录、`update()` 决策提交流程、`authorize()` 能力判断、资源范围匹配、`selected_resources()` |
| `WorkerManifest.expand_permissions` 与 permissions 字段校验（installation_store.py） | 清单不再包含 permissions |
| `ECL/api/plugins.py` | `plugin_permissions_get` / `plugin_permissions_update` / `plugin_permission_resources` 三个 IPC 端点 |
| `ECL/api/models.py` | 上述三个端点的请求模型与路由表条目；`IpcLogPolicy` 对应条目 |
| `manager/discovery.py`、`manager/lifecycle.py` | `permission_denied` 状态、PermissionManager 登记/注销 |
| `manager/base.py`、`manager/registry.py` | PermissionManager 实例、permissions 元数据上报（保留 installation_id / content_digest 身份数据） |

### 3.2 修改

| 位置 | 变更 |
| --- | --- |
| `grant_store.py`（改 run_store.py） | `open_session` 去掉声明参数；`update_resource_scope` 等三个范围供给改为"全量视图"语义（服务仍推送可用集，插件视图不再过滤）；`_authorize_locked` 退化为纯时效 lease 生成 |
| `worker_service.py` | `_refresh_declared_*` 不再按声明门控，全量推送实例/账户/Java 目录；`permissions(name)` 瘦身为运行身份（installation_id、content_digest、run_id）；去掉 binding.declarations 依赖 |
| `runtime/*.py`（17 个模块） | `authorize("xxx")` 调用点改为无能力 lease；`GrantLease` 构造与比较随模型简化；file_api 的 root_ref 语义从"授权根"改为"全部实例根" |
| `runtime/broker.py` | `permissions.list` RPC 方法删除；epoch 逻辑原样保留 |
| `runtime/custom_ui.py`、`application.py` | 保留 `allow_ui_injection` 全局开关；删除按插件批准的 ui.inject 判断 |
| `runtime/network_api.py`、`network_targets.py` | 站点声明校验随权限删除；实施时核实 download_api 是否还依赖 origin 校验防 SSRF——若仅服务权限则一并删，若有独立安全用途则保留并注明 |
| `runtime/operation_adapters.py` | 操作归属保留；剥离其中的授权字段 |
| `development.py` | 开发模式审批（`begin_development`/`end_development`）中授权部分删除；内容复核保留 |
| `auth_providers.py`、`launch_hooks.py`、`account_resources.py`、`java_resources.py`、`crash_extensions.py`、`instance_compat.py`、`modpacks.py` 等 | 注册类 API 保留业务机制，删除其"需要某能力"的准入判断 |
| `ECL/minecraft/services/game/launch.py` | 唯一的真实 `PluginGrantError` 导入随重命名调整 |
| `ECL/plugins/plugin.py`、`package_activation.py`、`package_preparation.py` | 内容校验链路保留；`PluginPreparedPackage` 的声明参数移除 |

## 4. 前端改动清单（子仓库）

| 位置 | 变更 |
| --- | --- |
| `features/plugins/components/PluginPermissionsModal.vue`（含 test） | 删除 |
| `features/plugins/api/pluginPermissionValidation.ts`（含 test） | 删除 |
| `features/plugins/components/PluginResourceSelector.vue`、`PluginLocalResourceSelector.vue`（含 test） | 删除（选目录即授权范围，无残留职责） |
| `features/plugins/api/pluginManagementApi.ts` | 删除 permissions 相关请求 |
| `features/plugins/stores/pluginStore.ts` | 删除授权状态与决策动作 |
| `views/Plugins.vue` | 移除权限弹窗入口；插件卡片不再显示权限徽标/审批按钮 |
| 安装/启用流程 | 不再出现审阅步骤；`permission_denied` 状态展示移除 |

## 5. 插件仓库改动

| 仓库 | 变更 |
| --- | --- |
| ECL-Plugin-OnlineGame | `plugin.json` 删 `permissions` 字段；README/AGENTS 中权限表述删除 |
| ECL-Plugin-QomicexCompatible | 同上；AGENTS 第 4 条（只申请声明能力）改为能力说明 |

## 6. 文档改动

- 新增 `docs/ADR-005-remove-plugin-permissions.md`：记录移除决策、与 ADR-003 的关系（推翻其"普通权限审阅"部分）、信任模型变化、保留的非权限机制。
- `docs/guides/plugin-development/`（README、settings、extensions）：删除权限章节与表述。
- `docs/protocols/`：Worker 协议中 permissions.list 方法删除。
- `AGENTS.md`：如有权限相关表述同步。
- 收口时删除本方案与 ADR-003 状态行的衔接说明。

## 7. 实施顺序

1. 后端核心：run_models/run_store 重命名与瘦身（含 binding 简化）→ 编译通过
2. worker_service 与 manager 链路 → discovery/lifecycle/registry/base
3. runtime 17 模块的 lease 调用点改造 → broker
4. IPC 三端点删除 + IpcLogPolicy + api 模型
5. services 与 development、package 链路收尾
6. 后端全量验证（ruff + pytest）
7. 前端删除与 Plugins.vue 改造 → 前端检查（pnpm check）
8. 插件仓库清单与文档 → 插件仓库各自验证（ruff + pytest）
9. ADR-005 与指南文档 → 提交（主仓库 + frontend 子模块 + 两个插件仓库）

## 8. 测试计划

- 删除纯权限测试 7 个文件（约 1350 行）：test_plugin_permissions、test_plugin_grant_store、test_plugin_permission_ipc、test_plugin_permission_simplification、test_plugin_permission_vocabulary、test_plugin_development_grants、test_plugin_network_targets（实施时确认是否含非权限用例）
- 其余约 65 个测试文件中 authorize/lease/permission 相关断言适配为时效 lease 语义
- 保留并核实的关键行为测试：内容变化阻断（PLUGIN_CONTENT_CHANGED）、epoch 失效（异步回执、去重、订阅）、操作归属（停用后操作继续完成）、ui.inject 全局开关、安装校验拒绝损坏包
- 全量：`ruff check .`、`ruff format --check`、主仓库 pytest、前端 `pnpm check`、两插件仓库 ruff + pytest
- 真实界面验收：启用第三方插件不再出现权限弹窗；停用后其下载继续完成

## 9. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| epoch 与归属机制缠在授权代码里，剥离不干净导致停用后操作异常 | 实施顺序先核心后外围；保留 test_plugin_operation_ownership 等行为测试作为安全网 |
| 第三方插件获得账户与实例全量访问 | 产品决策已确认；ADR-005 写明信任模型 |
| 两代权限系统（permissions.py 与 grant_*）耦合点比预期多 | 先删旧代 permissions.py（无状态收集器），再动 v2 |
| 前端 pluginStore 状态被权限弹窗之外的界面依赖 | 改造前先跑 pluginStore 测试确认引用面 |
| 网络限制删除后 Worker 可任意联网 | 属于既定决策；download_api 若有独立 URL 校验则保留 |
| 旧插件（带 permissions 字段的清单）无法安装 | 项目未正式发布，无兼容承诺（第 2 节）；清单解析忽略未知字段即可 |

## 10. 待实施时确认的细节

- `test_plugin_network_targets.py` 与 `network_targets.py` 是否含独立于权限的安全用途
- `files.py`、`MicrosoftAuth.py`、`Libs.py` 中 grep 命中的 "grant" 预计为 OAuth 术语，实施时逐个排除
- 前端 `auto-imports.d.ts` 与 showcase 面板对权限类型的引用清理范围
