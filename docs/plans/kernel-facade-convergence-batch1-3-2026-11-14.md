# 内核门面收敛批 1-3 实施方案（修订版）

- 日期：2026-11-14。
- 状态：待用户确认，未实施。
- 关联：[原始方案 kernel-facade-convergence-plan-2026-11-13](kernel-facade-convergence-plan-2026-11-13.md)、[ADR-005 插件 API 版本契约](../ADR-005-plugin-api-version-contract.md)、[后端模块划分方案第 3 步](backend-module-partition-plan-2026-11-14.md)。
- 本文档不取代原始方案，只记录**批 1-3 按 11-14 现状核实后的修订**；门面划分语义（原始方案第 3 节）不变。

## 1. 现状核实与修订依据（2026-11-14 实测）

原始方案基于 11-13 现状，此后发生了基线重置与 MC 拆分迁移，以下数据已变化：

| 项 | 原方案 | 实测现状 | 修订 |
| --- | --- | --- | --- |
| `api_version` 基线 | 2 时代，批 3 假设 `{2, 3}` | 基线已重置为 1（`contracts.py:48`），manifest 为 `Field(ge=1, le=1)`（`installation_store.py:64`），握手为 `!= 1`（`supervisor.py:99`） | 批 3 改为 `supported_api_versions: frozenset[int] = frozenset({1})`，集合初始只含 1；未来升版时向集合添加，不动校验逻辑 |
| 未知方法兜底错误码 | 新造 `PLUGIN_METHOD_NOT_FOUND` | `_control_service` 末尾已抛 `PLUGIN_API_METHOD_NOT_FOUND`（`broker.py:502`），且该码在 8 个文件中已确立 | 不新造错误码，统一沿用 `PLUGIN_API_METHOD_NOT_FOUND` |
| 前端 IPC 命令 | 241 | 238（`IpcCommandRegistry.command_names`） | 数字修正；"一个不改"约束不变 |
| broker 变更方法 | 42 | 42（`PluginHostBroker.mutation_methods`） | 不变 |
| SDK 公开方法 | 26 | 26（`ECL/plugins/api/plugin.py`） | 不变 |
| 服务实现位置 | `ECL/services/` 全部 | MC 相关已迁 `ECL/minecraft`；`ECL/services/` 只剩通用服务（operations / processes / updates / custom_downloads） | 4.2 节"不动"范围改为：`ECL/services/` 与 `ECL/minecraft/` 下全部业务实现一行不改 |
| broker 依赖 import | — | `broker.py:59` 仍 `from ECL.services.operations import OperationManager`（通用服务，合法） | 无需处理；批 2 只动路由表，不动依赖装配 |

兜底行为澄清：当前未知方法**并非静默成功**——它落入 `_control_service` 后被最后一行抛 `PLUGIN_API_METHOD_NOT_FOUND`("此 Host API 尚未接线")。原始方案"修掉静默兜底"的表述不准确；批 2 的真实收益是把"前缀误判可能落到错误服务"的隐式顺序判断变成显式查表，并让未知方法在**入口处**就得到明确的错误，而非穿过所有前缀分支后依赖兜底尾行。

## 2. 架构修订：路由表与门面目录分离

原始方案批 2 写的是"`_dispatch` 改为 `facade_by_method` 查表，按门面分发"。实测该表述存在概念混淆：**门面 ≠ 路由单位**。例如 `launch` 门面同时含 `game.*`（→ `_game_service`）与 `java.*`（→ `_query_service` 的 `_java_query`），一个门面横跨多个 service，按门面分发无法实现。

修订后的结构是**两张表、两个职责**：

```
PluginMethodRouter（批 2，即划分方案第 3 步"broker 方法表按域注册"）
  route_by_method: method → broker 服务 handler（可路由性，运行时用）
  未知方法 → ProtocolError("未知宿主方法", "PLUGIN_API_METHOD_NOT_FOUND")

PluginFacadeCatalog（批 1，API 契约层）
  method_by_facade / facade_by_method（承诺分组，契约快照与版本承诺用）
  promised_facades: {"instances", "launch", "accounts", "resources", "runtime", "ui"}
```

- 门面目录**不参与运行时路由**，只承载契约：快照测试、ADR-005 的版本承诺、AGENTS.md 第 2 节的分域描述。
- 路由表**不做门面分组**，方法名直接映射到 handler；`resources.list` 按 capability 二次分发的逻辑（`_is_java_query` / `_is_file_query`）留在 handler 内部不动。
- 两表之间的等价性由批 1 的契约快照测试保证：目录的全集 ⊆ 路由表键集，路由表新增键必须显式决定是否入某个门面（入承诺面或弱承诺扩展面），不允许无意识扩面。

### 2.1 路由表形态（按域注册，划分第 3 步）

按 AGENTS.md 第 3.3 节，映射作为领域类属性存放，不新增游离常量。`_dispatch` 的前缀链替换为：

```python
class PluginHostBroker:
    query_methods: ClassVar[frozenset[str]] = frozenset({...})       # 显式方法名集合
    query_prefixes: ClassVar[tuple[str, ...]] = ("files.", "java.", ...)  # 无法枚举的子方法前缀
    # 各前缀命名空间 → handler 的注册表，键为命名空间元组
    service_routes: ClassVar[Mapping[...]] = MappingProxyType({...})
```

实现要点：

1. **显式方法名集合**（`operations.get/list/cancel`、`resources.list`、`instances.scan/get/profile_patch`、`network.request`、`downloads.submit`、`runtime.info`、`mutations.get`、`commands.*`、`settings.*`、`events.*`）逐条从现有 `_dispatch` 与 `_query_service` 抄录，不凭语义重推。
2. **前缀命名空间**（`game./mods./resourcepacks./shaders./modpacks./files./java./accounts./config./panels./ui./plugins./hooks./crash./compat./tools./auth.`）保持前缀判断，但从"顺序 if 链"改为"注册表遍历"，消除分支顺序依赖。
3. `runtime.info` / `mutations.get` 等控制方法从 `_control_service` 的大 if 链提升为显式登记，`_control_service` 保留为这些方法的实现体。
4. 未知方法在 `_dispatch` 入口直接抛 `PLUGIN_API_METHOD_NOT_FOUND`，删除 `return self._control_service(request)` 兜底尾行。

### 2.2 门面目录内容（沿用原始方案第 3 节，一处勘误）

六门面划分与 3.1 节边界处理**全部维持**。勘误一处：原始方案 3.3 节表格中 `callbacks.invoke` 的"不入门面"结论不变；补充实测确认——`callbacks.invoke` 是宿主→Worker 反向通道，`broker` 方法表中本就不存在它，契约测试的排除断言依然保留作为防线。

## 3. 批次执行（修订后）

### 批 1：门面目录 + 契约快照（纯新增，零风险）

- 新增 `ECL/plugins/api/facades.py`：`PluginFacadeCatalog`，含 `method_by_facade`（`MappingProxyType`）、推导的 `facade_by_method`、`promised_facades`、`extension_namespaces = ("tools.", "crash.", "compat.", "plugins.")`。
- 新增 `tests/test_plugin_facade_catalog.py`：
  - 双向一致：`method_by_facade` 展平 == `facade_by_method` 键集。
  - 排除断言：键集不含 `callbacks.invoke`、不含权限标识（`plugin.base` 等）、不含 `__main__` 类非方法字符串。
  - 契约快照：六门面方法名集合逐面快照断言；新增/删除方法必须显式更新快照。
  - 集合比对：目录全集 ⊆ `PluginHostBroker` 可路由方法集（批 2 落地前先用当前 `_dispatch` 的前缀链 + 显式集合手工核对，批 2 后改为对路由表键集断言）。
- 不动任何路由逻辑，测试全绿即验收。

### 批 2：路由查表化（与划分方案第 3 步合并实施）

- 按 2.1 改 `ECL/plugins/runtime/broker.py`：`_dispatch` 查表；未知方法入口抛 `PLUGIN_API_METHOD_NOT_FOUND`。
- `_query_service` 的 capability 二次分发、各 `_x_service` 方法体**一行不改**。
- 等价性验证：`tests/test_plugin_host_broker.py` 全量通过；新增 `test_plugin_router_table.py`：
  - 对目录全集逐方法断言路由落点与改造前一致（用改造前快照的落点清单作基准）。
  - 未知方法（如 `game.nonexistent`、`foo.bar`）断言 `PLUGIN_API_METHOD_NOT_FOUND` 且不再进 `_control_service`。
  - 前缀顺序无关性：注册表遍历顺序打乱后路由结果不变。
- 改造前先扫描现有插件与测试，确认无依赖兜底尾行行为的用例（预期无：兜底尾行本就抛错）。

### 批 3：清单三字段与两时点协商（基线 1 版）

- `WorkerManifest` 增加 `min_launcher: str | None = None`、`features: list[str] = []`；`api_version: StrictInt` 的 `ge=1, le=1` 改为集合校验：`api_version in supported_api_versions`（集合现值 `{1}`，单一来源 `contracts.py`）。
- 安装期（`installation_store.py`）与握手期（`supervisor.py`）按 ADR-005 错误码表校验：`api_version` 失败 → `PLUGIN_API_VERSION_UNSUPPORTED`；`min_launcher` 高于宿主 → 独立错误码；`features` 未知 → 独立错误码；其余仍 `PLUGIN_MANIFEST_INVALID`。
- 两时点：安装期拦截（不留半安装状态，复用现有事务回滚）+ 握手期复核（防安装后宿主降级）。
- 回归基线：现有 v1 插件（两官方插件）manifest 不填新字段，必须安装、启用、面板、设置全链路无感。

## 4. 测试与验收计划

沿用原始方案第 6 节，修订三处：

| 项 | 修订 |
| --- | --- |
| 多版本并存用例 | `{1}` 集合下先验证"集合拒绝 2、放行 1"；多版本并存用例改为集合含 `{1, 2}` 的假想形态，只在测试内构造，不实际升版 |
| 未知方法错误码 | `PLUGIN_API_METHOD_NOT_FOUND`（沿用现有码），断言不再落入控制服务 |
| 回归范围 | 后端全量 pytest + ruff；前端零改动故跑一次构建确认无契约漂移即可，无需全量 1327 项 |

真实界面验收：安装 OnlineGame 与 QomicexCompatible（现 v1 插件），加载、面板、命令调用与改造前一致。

## 5. 风险与应对（增量）

| 风险 | 应对 |
| --- | --- |
| 显式方法集合抄漏导致某方法 404 | 批 1 契约快照先行 + 批 2 落点等价测试用改造前快照作基准；快照覆盖目录全集 |
| 前缀注册表顺序敏感残留 | 打乱顺序的专测（2.1 要点 2） |
| `api_version` 集合化后旧比较残留 | 全局搜索 `!= 1`、`== 1`、`ge=1, le=1`（`contracts.py` / `installation_store.py` / `supervisor.py` / `broker.py` `runtime.info` 返回值）逐一核对，`runtime.info` 的**响应字段** `api_version: 1` 保持字面量（它是对外声明的当前版本，不是校验） |
| 门面分组争议 | 目录只是分组承诺，不参与路由；调整分组只改 `facades.py` 与快照，无运行时影响 |

## 6. 提交与回滚策略

- 三批各一个独立提交，任一批失败可回滚到上一批。
- 批 2 提交信息注明"与后端模块划分方案第 3 步合并完成"。
- 全部完成后在划分方案文档第 7 节表格将步骤 3 标注为"已随门面收敛批 2 完成"。

## 7. 实施与验证记录（2026-11-14）

三批全部完成，独立提交：批 1 `48ee37a`、批 2 `38c50e9`、批 3 `1f273e6`。

| 批 | 结果 |
| --- | --- |
| 批 1 | `PluginFacadeCatalog`（facades.py）+ 契约快照测试 6 项。实施中实测补齐两处遗漏：`ui.windows.open/focus/close` 入 ui 门面（mutation_methods 对照发现）、`files.list/stat` 入 runtime 门面（file_api 核验发现）；`window.close` 确认为窗口内部协议排除 |
| 批 2 | broker 注册表 `prefix_routes`/`method_routes` 替换前缀分支链；未知命名空间方法入口抛 `PLUGIN_API_METHOD_NOT_FOUND`；命名空间内未知子方法仍由委托实现拒绝（与改造前一致）。路由表测试 6 项：落点基准逐条一致、注册值可调用、目录全解析、顺序无关、入口失败 |
| 批 3 | `supported_api_versions={1}` 集合化（contracts/supervisor/runtime.info/Worker hello 单一来源）；`minLauncher`（PEP 440 比较，PydanticCustomError 专指过高）与 `features` 白名单（panels/webview/hot_reload）；错误码四类归类 + 校验位置别名归一（Pydantic loc 用别名 minLauncher）；文档三处同步，ADR-005 标记已实施 |

验证：

- 批 2 插件域回归 978 项通过；后端全量 **2742 passed / 42 skipped / 0 failed**；ruff check + format（376 文件）通过。
- 实施勘误：批 3 未把 min_launcher 的版本比较放在独立流程层，而是放进 WorkerManifest 校验器（PydanticCustomError 区分格式问题与版本过高），使安装、发现登记、开发模式三处清单校验点自动获得同一行为。
- 划分方案第 3 步（broker 方法表外移）随批 2 完成。
