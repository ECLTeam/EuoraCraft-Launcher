# 内核门面收敛实施方案

- 日期：2026-11-13。
- 状态：待用户确认，未实施。
- 关联：[ADR-005 插件 API 版本契约](../ADR-005-plugin-api-version-contract.md)。

## 1. 目标

给现存的方法命名空间建立**显式门面**与版本挂载点，让"内核 API"从字符串前缀变成可声明、可测试、可承诺的契约。

硬约束：**只加门面层，不改任何方法名、命令名、参数结构。** 前端 241 个 IPC 命令与现有插件代码零改动。

## 2. 现状清单（实测）

| 面 | 规模 | 位置 |
| --- | --- | --- |
| 插件 SDK 公开方法 | 26 | `ECL/plugins/api/plugin.py` |
| broker 变更方法 | 42 | `ECL/plugins/runtime/broker.py` `mutation_methods` |
| broker 服务分组 | 11 | `ECL/plugins/runtime/broker.py` `_dispatch` |
| 方法命名空间 | 20+ | 同上 |
| 前端 IPC 命令 | 241 | `ECL/api/registry.py` `command_names` |
| 版本常量 | 3 处 | `contracts.py` / `installation_store.py` / `supervisor.py` |

`_dispatch` 当前按前缀顺序判断，最后一句是 `return self._control_service(request)`。这意味着**任何未知或拼错的方法名都会落进控制服务**，而不是明确报错。收敛时顺手修掉：未知方法返回 `PLUGIN_METHOD_NOT_FOUND`。

## 3. 门面划分

六个承诺门面按业务归属切分，映射的全是已有命名空间，不新增能力：

| 门面 | 纳入的现有命名空间 | 语义 |
| --- | --- | --- |
| `instances` | `instances.*`、`config.*` | 实例、档案与配置 |
| `launch` | `game.install` / `game.launch` / `game.stop`、`java.*` | 启动链路，永不出内核 |
| `accounts` | `accounts.*`、`auth.*` | 账户、登录与外部认证 |
| `resources` | `resources.list`、`mods.*`、`resourcepacks.*`、`shaders.*`、`modpacks.*`、`downloads.submit`、`network.request` | 内容安装、查询与来源 |
| `runtime` | `operations.*`、`events.*`、`settings.*`、`mutations.get`、`runtime.info`、`files.*` | 插件自身运行时能力 |
| `ui` | `panels.*`、`ui.*`、`commands.*`、`hooks.*` | 界面注入与协作 |

扩展面（弱承诺，只承诺"存在且不静默失败"）：`tools.*`、`crash.*`、`compat.*`、`plugins.*`。

### 3.1 跨门面的边界处理

三处归属需要显式说明，避免实施时反复：

- `modpacks.*` 归 `resources` 而非 `launch`：整合包是内容来源，不是启动动作。`game.install` 归 `launch`，因为它是启动前置且必须由内核保证。
- `resources.list` 归 `resources` 而非 `instances`：它查询的是实例下已安装内容，主体是内容不是实例。这样门面名与命名空间同名同义，不再出现"`resources` 门面不含 `resources.*`"的歧义。
- `files.*` 归 `runtime`：它是插件写自身数据的存储通道，与游戏文件无关。
- `network.request` 归 `resources`：当前只服务于内容下载；若将来扩为通用网络，需提升为独立门面并走版本递增。

### 3.2 映射的存放位置

按 `AGENTS.md` 第 3.3 节，不得新增脱离领域归属的模块级映射常量。映射作为领域类属性存放：

```python
class PluginFacadeCatalog:
    method_by_facade: ClassVar[Mapping[str, frozenset[str]]] = MappingProxyType({...})
    facade_by_method: ClassVar[Mapping[str, str]] = MappingProxyType({...})
    promised_facades: ClassVar[frozenset[str]] = frozenset(
        {"instances", "launch", "accounts", "resources", "runtime", "ui"}
    )
```

`facade_by_method` 由 `method_by_facade` 在类定义后推导，不手写两份。

### 3.3 不属于门面的三类标识

实测 broker 中还出现三个易被误收的标识，映射表必须显式排除：

| 标识 | 实际身份 | 处理 |
| --- | --- | --- |
| `callbacks.invoke` | 宿主 → Worker 的反向调用通道（`broker._invoke`），不是插件可调方法 | 不入门面；门面只描述"插件 → 宿主"单向 |
| `plugin.base` | 权限标识，不是方法名 | 不入门面；门面名与权限标识是两个命名空间，不合并 |
| `config.public` | 真实方法，归 `instances` 门面 | 入表 |

契约测试必须同时断言：映射表键集不含这三类之外的任何非方法标识，避免把权限字符串或反向通道当成接口承诺出去。

## 4. 改造边界

### 4.1 改动文件

| 文件 | 改动 | 风险 |
| --- | --- | --- |
| `ECL/plugins/api/facades.py` | 新增。门面目录与版本映射 | 无（纯新增） |
| `ECL/plugins/api/contracts.py` | `api_version` 常量 → `supported_api_versions: frozenset[int]` | 低 |
| `ECL/plugins/api/plugin.py` | 方法按门面分组排列，方法名与签名不变 | 低 |
| `ECL/plugins/runtime/broker.py` | `_dispatch` 前缀判断 → 查表路由；未知方法显式失败 | 中 |
| `ECL/plugins/installation_store.py` | manifest 增加 `min_launcher`、`features`；`api_version` 改查集合 | 中 |
| `ECL/plugins/runtime/supervisor.py` | 握手校验改为查支持集合 | 低 |

### 4.2 明确不动的部分

- `ECL/api/registry.py` 的 241 个命令名：一个不改。
- `ECL/services/` 下全部业务实现：一行不改。
- 实例目录结构、`.eclplugin` 归档结构：不改。
- 前端：不改。门面是后端概念，不进入前端协议。
- 权限标识与授权模型：不改；本方案不新增业务权限。

## 5. 实施批次

每批独立可验收、可回滚，失败可停在上一批。

### 批 1：门面目录与契约快照（纯新增）

- 新增 `facades.py`，建立双向映射与承诺集合。
- 新增契约测试：枚举全部门面方法名，与 `broker._dispatch` 实际可路由的方法做集合比对，任何一边多出方法即失败。
- 不动任何路由逻辑，测试应当全绿。

### 批 2：路由改查表

- `_dispatch` 改为 `PluginFacadeCatalog.facade_by_method` 查表，按门面分发到对应 service 方法。
- 移除末尾的 `return self._control_service(request)` 兜底，未知方法抛 `ProtocolError("未知宿主方法", "PLUGIN_METHOD_NOT_FOUND")`。
- 用批 1 的集合比对测试 + 既有 broker 测试兜底，要求**行为完全等价**，唯一的差异是未知方法的错误码。

风险点：`_query_service` 里现在的判断混合了前缀和显式方法名集合（`operations.*`、`resources.list`、`instances.*` 等），改表时必须逐条对齐，不能凭语义猜。

### 批 3：清单三字段与两时点协商

- `WorkerManifest` 增加 `min_launcher: str | None`、`features: list[str]`（默认空），`api_version` 改为集合校验。
- 安装期与握手期按 ADR-005 的错误码表校验。
- 错误码区分保持现有做法：仅 `api_version` 失败时报 `PLUGIN_API_VERSION_UNSUPPORTED`，其余清单问题报 `PLUGIN_MANIFEST_INVALID`；新增的 `min_launcher` 与 `features` 按 ADR-005 各自独立错误码。
- 现有 v2 插件不填新字段即可正常工作，作为回归基线。

### 批 4：UI 通道（不做改动）

实测修正：`ui.register_html` / `register_vue` / `register_page` 在后端共用同一条 `_slot_sink` 通道，只是 payload 类型不同；加上声明式面板与原生窗口，实际是**三条并列通道**，不是五条独立路径。

按用户决策，三条通道不设准入限制、不新增防护、不补容错机制，现有实现保持原样。结论见[插件界面不新增防护的决策](plugin-ui-no-guardrails-2026-11-13.md)。

**本批无代码改动。**

## 6. 测试计划

| 层 | 内容 |
| --- | --- |
| 契约快照 | 六门面的方法名集合快照；新增或删除方法必须显式更新快照，防止无意识破坏 |
| 双向一致 | `method_by_facade` 展开后与 `facade_by_method` 键集相等；与可路由方法集相等 |
| 路由等价 | 对全部已知方法名逐一断言其被分发的 service 与改造前一致 |
| 未知方法 | 未登记方法返回 `PLUGIN_METHOD_NOT_FOUND`，不再落进控制服务 |
| 清单校验 | `api_version` 越界、`min_launcher` 过高、`features` 未知分别返回既定错误码，且不留半安装状态 |
| 多版本并存 | `{2, 3}` 支持集合下 v2 与 v3 测试插件同时可用 |
| 回归 | 既有后端测试全量、前端 1327 项全量、生产构建 |

真实界面验收：安装一个现有 v2 插件，确认加载、面板、设置、命令调用与改造前一致。

## 7. 风险与应对

| 风险 | 应对 |
| --- | --- |
| 查表漏项导致某方法失效 | 批 1 的集合比对测试先行，不等改造完才发现 |
| 未知方法错误码变化影响现有插件 | 改造前扫描全部插件与测试，确认无依赖兜底行为 |
| `api_version` 改集合后旧逻辑残留 | 全局搜索 `!= 2`、`== 2`、`ge=2, le=2`，一处不漏 |
| 门面划分本身有争议 | 本方案的门面只是分组，不改变调用方式；划分可后续调整，成本低于改名 |

## 8. 不在本方案范围

- 任何功能拆出内核。
- 前端 IPC 的分组或重命名。
- 可视化工具链、插件市场、签名体系。
- `AGENTS.md` 第 2 节修订的落地（见 ADR-005，需单独确认后修改规范文本）。
