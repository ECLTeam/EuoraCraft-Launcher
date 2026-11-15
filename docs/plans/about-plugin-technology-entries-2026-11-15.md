# 关于页插件技术栈声明与合并展示

用户确认的方案：插件以声明式方式向宿主贡献技术栈条目（方案 A）；宿主技术栈中插件独占的条目移交插件声明；展示采用独立分类 + 插件名徽标，多个插件合并渲染。

## 背景与现状

- 「设置 → 关于」技术栈卡片由 `frontend/src/features/settings/about/aboutContent.ts` 硬编码，全部按启动器自身依赖组织。
- EasyTier、EasyTier-PyO3 在主仓库 `pyproject.toml` 与 `ECL/` 后端代码中均无引用，实际是联机插件（ECL-Plugin-OnlineGame）使用的组网组件，却被列在启动器技术栈中。
- Authlib-Injector 经核实被主仓库账户功能实际使用（`ECL/api/accounts.py` 等），保留在宿主列表。
- 插件运行时已有声明式注册模式：`PanelDefinition` 经 broker RPC `panels.register` 登记，IPC `plugin_panels_get` 聚合下发；本功能按同一模式扩展。

## 后端改动（主仓库）

### 1. 新增声明模型（`ECL/plugins/runtime/models.py`）

新增 `TechnologyEntryDefinition(RequestModel)`，字段有界、纯展示、无可执行内容：

| 字段 | 约束 |
| --- | --- |
| `key` | `^[A-Za-z_][A-Za-z0-9_.-]{0,63}$`，插件运行内唯一 |
| `name` | 展示名，1–64 字符 |
| `description` | 简介，≤200 字符 |
| `url` | http/https 绝对地址，复用 `homepage` 的校验方式 |
| `license` | 可选展示文本，≤128 字符 |

`initials` 等视觉细节由宿主生成，不由插件声明。

### 2. Broker 注册通道（`ECL/plugins/runtime/broker.py`）

- 仿照 `panels.register` 新增 RPC 方法 `technology.register` / `technology.list`（挂在既有 UI 服务分派下），`_keys` 白名单校验参数。
- 新增 `self._technology_entries: dict[str, tuple[TechnologyEntryDefinition, PluginLease]]`，走 `session.lease()` / `session.accept()` 原子登记，会话关闭随 lease 失效，插件禁用/卸载后条目自动消失。
- 每插件单次注册上限 16 条，`key` 重复拒绝（`PLUGIN_SCHEMA_INVALID` / `PLUGIN_REGISTRATION_CONFLICT`，沿用面板错误码语义）。
- 新增 `technology_entries_view()`：`model_dump(mode="json")` 返回本运行已登记条目。

### 3. IPC 聚合接口（`ECL/api/plugins.py`）

新增 `plugin_technology_entries_get`：遍历所有运行中插件 broker，聚合 `technology_entries_view()`，返回 `{"entries": [{"plugin_name": str, "entries": [...]}]}`。无运行插件的插件名跳过，单个 broker 失败不影响整体（按 `plugin_panels_get` 现有聚合方式处理）。

## 前端改动（frontend 子仓库）

### 1. 数据获取

- `pluginManagementApi.ts` 新增 `technologyEntries(): Promise<PluginTechnologyEntryGroup[]>`，对应新 IPC。
- `types/plugins.ts` 新增 `PluginTechnologyEntry` / 分组类型。

### 2. 关于页渲染（`AboutTab.vue`）

- 技术栈卡片在宿主分类（`technologyCategories`）之后渲染独立分类「插件技术栈」，复用既有 `technology-category` 折叠交互（CollapsiblePanel、默认收起、数量统计）。
- 内部按插件分小节：小节标题为插件名，每条目渲染为 `AboutEntryRow` 同款行，行尾追加插件名徽标（复用关于页现有 badge 样式；多个插件的小节顺序按插件列表顺序）。
- 数据在 `onMounted` 中与 `pluginStore.load()` 一并拉取；失败时插件分类整体隐藏，不阻塞宿主分类（与关于页插件卡片的容错策略一致）。
- 插件条目点击仍走 `openExternalUrl`；插件 `url` 为空或校验失败时后端已拒绝，前端不兜底降级。

### 3. i18n 与样式

- `zh-CN.json` 新增：分类标题 `settings.aboutTab.technologyCategories.pluginTechnologyStack`、插件徽标 aria 标签、加载失败文案。
- `AboutTab.css` 仅追加插件徽标与小节标题的少量规则，不改动宿主条目与折叠动效。

## 存量移交

- `aboutContent.ts`：删除 `technologies.easyTier` 与 `backendRuntimeEntries` 中 `easy-tier-pyo3` 条目；`runtime-frameworks` 分类只剩 PyTaruiX。
- `AboutTab.vue`：`licenseEntries` / `licenseById` 移除 EasyTier、EasyTier-PyO3 两条许可证记录。
- i18n 清理 `easyTier` / `easyTierPyo3Desc` 等失去消费者的键。
- Authlib-Injector 及其余条目不动。

## 插件仓库同步（不在本仓库内实施）

- ECL-Plugin-OnlineGame：在其注册代码中声明 EasyTier、EasyTier-PyO3 两条 `technology.register` 条目，随其仓库规范提交。
- 本仓库先行交付宿主能力；插件仓库未更新前，插件分组不出现，宿主列表已无 EasyTier，属于预期过渡状态。

## 测试计划

- 后端（`tests/`）：
  - `TechnologyEntryDefinition` 校验用例：字段边界、url 非 http/https 拒绝、key 重复拒绝。
  - broker 注册：注册/重复注册/会话失效后条目消失/上限拒绝。
  - IPC 聚合：多插件合并、无运行插件返回空、单 broker 失败不拖垮聚合。
- 前端（`AboutTab.test.ts` 等）：
  - 有插件条目时渲染插件分类、徽标与小节标题；无条目时分类隐藏。
  - 宿主列表不再包含 EasyTier / EasyTier-PyO3。
  - 折叠交互与宿主分类行为一致。
- 工具链：Ruff、含文件头与 docstring 规则的架构测试、Prettier/ESLint、vue-tsc + Vite 构建。

## 影响范围与风险

- 内核域新增模型与 RPC 方法不引入 Minecraft 语义，`tests/test_layer_boundary.py` 不受影响。
- 插件 SDK / 文档（插件仓库侧）需要同步 `technology.register` 方法说明，宿主先行时旧插件不受影响（不声明即不展示）。
- 过渡期联机插件技术栈不出现在关于页，待插件仓库跟进后恢复，需在交付说明中注明。
