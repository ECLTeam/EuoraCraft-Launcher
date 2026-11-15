# 插件模型统一方案：移除 v2 概念，统一系统插件与用户插件（2026-11-15）

## 背景与决策

当前插件体系为双轨：系统插件走 v1 进程内装饰器框架（`ECL/plugins/plugin.py`），用户插件走 v2 Worker 子进程 RPC（`ECL/plugins/api/` + `worker_service` + broker）。经用户确认，做出以下决策：

1. **保留子进程隔离**：用户插件的 Worker 子进程运行时完整保留。
2. **系统插件保留进程内**：系统插件不进子进程，但改用统一插件 API，经进程内通道访问宿主。
3. **彻底删除 v1 装饰器框架**：`ECL/plugins/plugin.py`、`ECL/plugins/framework.py` 及全部 v1 分支代码与专属测试移除，仓库只剩一套插件 API。
4. **全面去版本化**：移除 "v1 / v2"、"api_version" 契约概念；`ECL/plugins/api/` 目录改名；文档、AGENTS.md 全面改写。
5. **能力补齐**：v1 独有能力（任意方法 HTTP、通用事件总线）补进统一 API，系统插件不因统一而降级，插件能力整体增强。

### 目标架构

```
统一插件 API（一套基类 ECL.plugins.sdk.Plugin + HostClient）
├─ 系统插件（resources/system_plugins，内置可信）
│    进程内执行：HostClient ← InProcessTransport → 宿主方法目录（broker 路由抽取）
└─ 用户插件（安装的 .eclplugin / 目录插件）
     子进程执行：HostClient ← 管道 RPC → supervisor → broker → 宿主方法目录
```

核心不变式：**broker 的方法路由与注册表是唯一的宿主数据面**。前端全部 IPC（slots/routes/vue_components/panels/call_command/settings）只面向这一数据面，不感知执行形态差异。"系统插件"退化为来源标记（内置分发、默认启用），运行时行为与用户插件一致。

## 非目标

- 不在本方案内新增在线分发、签名、商店能力。
- 不重写官方插件仓库（ECL-Plugin-OnlineGame / ECL-Plugin-QomicexCompatible 已基于 Worker API，本方案对其基本兼容；清单 `api_version` 字段改为忽略不校验，现有包无需重新打包）。
- 不恢复权限系统（ADR-005 决策维持）。

## 分阶段实施

### 阶段 1：去版本化改名（纯机械，独立可提交）

1. `ECL/plugins/api/contracts.py` → `ECL/plugins/contracts.py`（`JsonValue`/`ProtocolError`/`ProtocolPolicy` 被全仓库共享，脱离 `api/` 命名）；`api/_errors.py` 的 `PluginRuntimeError` 并入 contracts。
2. `ECL/plugins/api/` 整目录 → `ECL/plugins/sdk/`；更新全部导入与 docstring 中 "v2"/"API v2"/"HostClient 白名单 RPC" 的版本化表述。
3. `installation_store.WorkerManifest` 更名 `PluginManifest`；清单校验不再读取/要求 `api_version` 字段（旧包带该字段时忽略），`PLUGIN_API_VERSION_UNSUPPORTED` 错误路径删除。
4. 全仓库搜索 "v2"/"API v2"/"api_version" 表述清理（代码注释、错误码、日志）。

验收：全量测试通过（仅导入路径断言类测试需同步更新）；`tests/test_architecture.py:35` 对 `plugins/api/__init__.py` 的断言改为 `plugins/sdk/__init__.py`。

### 阶段 2：进程内执行通道（核心新增）

1. **抽取宿主方法目录**：将 `ECL/plugins/runtime/broker.py` 中的方法路由（`prefix_routes`/`method_routes` → 各 `_*_service` 分派）抽取为独立的 `HostMethodCatalog`，broker 保留会话层（lease 绑定、`mutation_key` 去重回执、协议帧校验）并调用该目录。worker RPC 路径行为不变。
2. **新增 `InProcessTransport`**（`ECL/plugins/sdk/`）：系统插件的 `HostClient` 底层直接调用 `HostMethodCatalog`，跳过管道与帧协议。会话身份使用合成 lease（`is_system=True` 标记）。
3. **系统插件加载重写**（`manager/discovery.py`）：
   - 清单 schema 统一为 `entry_file`/`entry_class`（删除 v1 的 `entry_point`）；
   - `_create_instance` 保留现有路径越界防护（入口文件必须位于插件目录内），实例化统一基类，注入进程内 HostClient；
   - 生命周期钩子（`on_load/on_enable/on_frontend_ready/on_disable/on_unload`）由 manager 直接调用（进程内无需 supervisor）。
4. **manager 各 mixin 去双轨**：`discovery`/`lifecycle`/`registry`/`packages` 中 `assert self.worker_service is not None` 与 `is_system` 分流收敛——系统插件与用户插件在注册表、设置（统一走 `settings.py` revision 协议）、命令调用上走同一条路径，仅执行形态不同。
5. **UI/命令注册对齐**：系统插件的页面/插槽/命令注册经统一基类（`define_command`/`define_vue_page`/`define_vue_slot` 等）进入 broker 注册表，前端 `plugin_get_*` 无需区分来源；v1 的 `register_route`/`inject_*` 事件路径删除，注入类事件统一由 slot_sink 发射。

### 阶段 3：删除 v1 框架

1. 删除 `ECL/plugins/plugin.py`（装饰器基类）、`ECL/plugins/framework.py`、`ECL/plugins/network.py`（v1 `PluginHttpResponse` 语义由统一网络 API 的返回结构取代）。
2. 删除 `manager/storage.py` 中 v1 设置路径（`_config_values`），设置统一走 `PluginSettingsService`。
3. 扩展点注册表（`instance_compat`/`launch_hooks`/`auth_providers`/`crash_extensions`）保留，仅保留经统一基类注册的一条路径；`registry.py` 的 worker 详情/设置/命令分流删除断言分支。
4. 删除 v1 专属测试并改写混合测试（清单见"测试计划"）。

### 阶段 4：能力补齐（统一 API 增强）

1. **网络 API 扩展**：`network.request` 支持任意安全方法（POST/PUT/PATCH/DELETE）+ 请求体（`json`/`data` 字段）。保留既有边界：内网防护、重定向逐跳校验、HTTPS 降级拒绝、响应大小上限、超时预算。`runtime/network_api.py` 的 `NetworkRequest` 模型与方法校验同步扩展，worker 与进程内共用同一实现。
2. **通用事件总线**：
   - 宿主侧新增插件事件中枢（broker 内）：`events.emit(topic, payload)` / `events.subscribe(topics, callback_ref)` 统一方法；
   - 插件 emit 的主题限定自身命名空间前缀（`<插件名>.`）+ 宿主公布的少量系统主题；订阅白名单从仅 `settings.changed` 泛化为任意已发布主题；
   - Worker 订阅经 callbacks 反向通道投递；进程内系统插件直接函数调用；`settings.changed` 行为保持不变；
   - 事件载荷大小与订阅数量设置上限（复用 ProtocolPolicy 风格）。
3. 跨插件命令（`plugins.list`/`plugins.call`）对系统插件同样开放（进程内通道直达）。
4. **装饰器语法糖层**：
   - 提供 `@command`、`@launch_hook`、`@event` 等装饰器，作为 `define_command`/`register_launch_hook`/`events.subscribe` 的快捷写法；
   - 装饰器仅给方法打静态元数据标记（不触碰宿主），统一基类在 `on_load` 前的预置阶段扫描标记方法并批量刷入既有底层注册——一条契约、无独立语义、进程内与子进程通用；
   - 边界：装饰器只承载静态声明（名称、schema 字段、导出标记、事件主题）；需要运行期宿主状态的动态注册（`define_tool`、`register_auth_provider` 等）仍用显式方法调用，界限写入开发手册；
   - 显式 `define_*` 调用与装饰器可混用，预置扫描先于用户 `on_load` 执行，后者的显式注册可覆盖或补充装饰器声明。

### 阶段 5：测试与文档

**测试计划**：

- 删除（v1/装饰器体系专属）：`test_plugin_lifecycle.py`（v1 分支断言）、`test_plugin_slots.py`（v1 注入路径）、`test_plugin_disabled_state.py`/`test_plugin_startup_mode.py`/`test_plugin_shutdown.py` 中进程内 v1 断言改写为统一基类断言。
- 改写：`test_plugin_info_registry.py`（删 worker 双轨断言）、`test_layer_boundary.py:43-45`（装配缝更新）、`test_api_models.py:342`、`test_plugin_runtime_connector.py`（命令面裁剪）。
- 新增：系统插件进程内端到端测试（统一基类 + InProcessTransport → HostMethodCatalog → 注册表 → 前端数据面）、`network.request` 非安全方法/请求体边界测试、事件总线（命名空间校验、反向投递、上限）测试、装饰器预置扫描测试（与显式注册混用、`on_load` 覆盖行为）。
- Worker 侧 46 个现有测试不动（阶段 2 重构 broker 时仅需保证其通过）。

**文档**：

- `docs/guides/plugin-development/` 10 篇整册改写为单轨教程（统一基类、双执行形态说明、扩展后的网络与事件 API）。
- `AGENTS.md` L3、L183-194 更新（`ECL/plugins/api/` → `ECL/plugins/sdk/`，三域描述去版本化）。
- 归档标注：`docs/ADR-005-plugin-api-version-contract.md` 标注被本方案取代；v2 设计文档（worker-api-v2 等 6 篇）头部加"历史设计，模型已统一"注记，不删除。
- `docs/protocols/eclplugin-package-format.md` 保留改写（用户插件分发格式不变，去版本化表述）。

## 风险与权衡

| 风险 | 应对 |
| --- | --- |
| broker 会话模型与 worker 耦合深（lease、去重回执），抽取 `HostMethodCatalog` 是最大工程量点 | 阶段 2 单独提交；worker 测试全量守护；抽取为纯函数式路由搬移，不改行为 |
| 系统插件进程内崩溃/卡死影响主进程 | 用户已确认接受此权衡（可信内置代码）；对 `on_load` 等钩子保留现有的失败日志与状态标记路径 |
| 前端对 system 插件展示字段差异（v1 字段 vs worker 详情） | 统一后 system 也走 `_apply_worker_details`；`plugin_list` 输出为超集，前端类型宽松处无需改动，严格处同步更新 |
| 官方插件仓库兼容 | 现有 `.eclplugin` 带 `api_version` 字段 → 校验改为忽略，天然兼容；无需协调发版 |
| 历史系统插件（如有本地自定义）使用 v1 装饰器 API | 未正式发布阶段不承诺内部兼容（AGENTS.md 第 2 节）；失败时明确报"插件入口需使用统一插件 API" |

## 验收标准

1. 仓库内只存在一套插件基类与一条清单 schema；`grep -r "api_version\|API v2\|WorkerManifest"` 无业务代码残留（历史文档注记除外）。
2. 系统插件（进程内）与用户插件（子进程）在插件管理页展示、设置读写、命令调用、UI 插槽注入上行为一致。
3. `network.request` 可发起带请求体的 POST；事件总线可跨插件发布订阅；Worker 46 个既有测试全部通过。
4. 全量测试、架构测试、层边界测试通过；文档与 AGENTS.md 无双轨表述。
