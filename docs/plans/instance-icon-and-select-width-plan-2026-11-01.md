# 运行中实例图标与筛选下拉框文字截断修复方案

日期：2026-11-01
范围：

- 后端 `ECL/services/game/instance_profiles.py`、`ECL/services/game/launch.py`
- 前端 `frontend/src/components/instances/InstanceIcon.vue`、`frontend/src/views/instances/RunningInstancesTab.vue`
- 前端 `frontend/src/components/ui/Select.vue`、`frontend/src/styles/components/ui/Select.css`
- 前端 `frontend/src/styles/views/instances/InstancesTab.css`、`frontend/src/styles/views/OnlineMods.css`、`frontend/src/components/mods/OnlineModSearch.vue`

## 1. 现象

用户反馈三处界面问题：

1. 游戏页「实例运行中管理」弹窗（`FullscreenModal` → `RunningInstancesTab`）的列表图标不跟随实例设置的图标，原版实例一律显示草方块。
2. 下载页「实例下载」顶部的版本类型筛选下拉框，收起状态和展开菜单里的文字都不完整。
3. 下载页模组标签的「来源」「排序方式」下拉框同样出现文字被截断。

## 2. 根因

### 2.1 运行中实例图标

`RunningInstancesTab` 自己实现了一套 `instanceImage()`：只用 `loader` 字段查加载器像素图，查不到就回退 `/img/item/grass.png`。原版加载器在 `LOADER_IMAGE_MAP` 中没有图片，因此恒为草方块。后端 `GameService.list_instances()` 也只返回 `loader`，没有返回实例资料里的图标，前端无法拿到用户设置的图标。

### 2.2 下拉框文字截断

公共 `UiSelect` 用 Naive UI 的 `NSelect` 承载，Naive 默认 `consistentMenuWidth = true`，下拉菜单宽度被锁成触发器宽度；同时 `Select.css` 给 `.ui-select-option-label` 设了 `overflow: hidden; text-overflow: ellipsis`，于是选项文字按菜单宽度截断。

实测（临时验证页，视口 990×760，读取 `scrollWidth`）：

| 位置 | 触发器宽度 | 菜单宽度 | 结果 |
| --- | --- | --- | --- |
| 实例下载 · 版本类型 | 144px（`@media (max-width:1000px)` 下仅 117px） | 同触发器宽度 | 菜单首项「全部版本 (1234)」显示为「全部版本 (12…」 |
| 模组 · 来源 | 84px（`.filter-item-small` 的 `min-width`） | 84px | 选中「CurseForge」时触发器与菜单都被截断 |
| 模组 · 排序方式 | 84px | 84px | 「最近更新」超出可用宽度被截断 |

启动器窗口默认 900×600、最小 960px 宽，因此「实例下载」页始终落在 `max-width: 1000px` 分支，`.category-select` 实际只有 117px，问题必现。

## 3. 选定方案

### 3.1 图标

后端在 `GameService.list_instances()` 的返回项里增加 `icon` 字段，由 `InstanceProfileStore` 新增的 `resolve_icon()` 解析出**当前生效的图标**；前端统一用 `InstanceIcon` 渲染，不再自行拼加载器图片。

- `resolve_icon()` 优先取 ECL 资料中用户设置的图标；未设置或资料不可读时回退到 `InstanceProfilePolicy.automatic_icon()` 的自动图标（模组实例用加载器图标，原版实例按版本类型取内置图标，快照版为命令方块）。`enrich_version()` 原本内联的同一段规则改为复用该方法，保证实例列表和运行中列表口径一致。
- 自动图标需要版本类型，因此 `_RunningGame` 增加 `version_type`，启动时从 Core 扫描结果 `VanillaType` 取值（缺省 `release`）。
- 第三方（PCL / HMCL）来源图标需要完整扫描上下文（原版版本号、加载器、插件兼容选项），运行中列表不具备这些输入，仍回退自动图标。
- 资料缺失、损坏或正在写入时记录警告并返回自动图标，不影响实例列表其余字段。

> 首版只读取了资料里的图标、未做自动回退，导致没有显式设置图标的原版快照实例退回 `UiIcon` 的立方体字形（用户截图反馈）。本次补齐自动图标解析。

### 3.1.1 移除运行中实例的刷新入口

实例列表由 `game:instances_changed` 事件驱动实时更新，空状态与工具栏都不再需要手动刷新按钮：删除 `#empty-actions` 里的刷新按钮、工具栏 `.running-refresh` 按钮及其样式与相关测试断言。

### 3.2 下拉框

- 公共 `UiSelect` 与模组页四个筛选 `NSelect` 统一加 `:consistentMenuWidth="false"`，菜单按内容宽度展开（`ResourceInstanceSelect` 已是该写法）。
- `Select.css` 把省略号样式限定在触发器内（`.ui-select .ui-select-option-label`）。下拉菜单被 `to="#app"` 传送到根节点，不在 `.ui-select` 内，因此菜单选项恢复完整文字。
- 触发器宽度改为跟随内容自适应并保留上下限：`.category-select` 由固定 144px 改为 `width: auto; min-width: 144px; max-width: 208px`（窄屏分支 128–176px）；`.filter-item` / `.filter-item-small` 的最小宽度分别提到 116px / 104px，最大宽度提到 184px / 152px。

## 4. 改动点

| 文件 | 改动 |
| --- | --- |
| `ECL/services/game/instance_profiles.py` | 新增 `InstanceProfilePolicy.automatic_icon()` 与 `InstanceProfileStore.resolve_icon()`，`enrich_version()` 改用同一规则 |
| `ECL/services/game/base.py`、`launch.py` | `_RunningGame` 增加 `version_type`（启动时取 `VanillaType`）；`list_instances()` 返回生效图标 |
| `tests/test_instance_profiles.py` | 新增 `test_resolve_icon_prefers_persisted_icon_and_falls_back_to_automatic`（自动图标 / 内置图标 / 本地图片绝对路径 / 资料损坏） |
| `tests/test_game_service.py` | 运行实例列表断言补 `"icon": {"type": "loader", "value": "neoforge"}` |
| `frontend/src/types/instances.ts` | 新增 `InstanceIconSource`，`GameInstance` 增加可选 `icon` |
| `frontend/src/components/instances/InstanceIcon.vue` | props 由 `version: ScannedVersion` 改为 `source: InstanceIconSource` |
| `frontend/src/components/instances/InstalledInstanceList.vue`、`InstanceDetailModal.vue` | 调用点改传 `:source` |
| `frontend/src/views/instances/RunningInstancesTab.vue` | 改用 `InstanceIcon`，删除自造 `instanceImage()`、`.running-version-icon` 样式，以及空状态与工具栏的刷新按钮 |
| `frontend/src/views/instances/RunningInstancesTab.test.ts` | 断言改 `.instance-icon img`，新增图标用例，刷新相关用例改为事件驱动 |
| `frontend/src/components/ui/Select.vue` | 增加 `:consistentMenuWidth="false"` |
| `frontend/src/components/mods/OnlineModSearch.vue` | 来源 / 排序 / 版本 / 加载器四个筛选下拉框增加 `:consistentMenuWidth="false"` |
| `frontend/src/styles/components/ui/Select.css` | 省略号样式限定在触发器内 |
| `frontend/src/styles/views/instances/InstancesTab.css` | `.category-select` 自适应宽度 |
| `frontend/src/styles/views/OnlineMods.css` | `.filter-item` / `.filter-item-small` 最小与最大宽度上调 |

## 5. 验证

- 临时验证页（Vite dev + 无头 Edge 截图）确认：视口 990px 下版本类型触发器按内容撑到 138.7px，文字完整；展开菜单首项「全部版本 (1234)」完整显示；模组页选中「CurseForge」「最近更新」时触发器文字完整。
- 后端：`ruff check`、`ruff format --check` 通过；`pytest tests/test_game_service.py tests/test_instance_profiles.py tests/test_instance_metadata.py tests/test_plugin_instance_queries.py tests/test_game_workspace.py tests/test_plugin_game_launch.py` 266 通过 / 3 跳过。
- 前端全量 `vitest run` 189 文件 / 1155 用例通过（含刷新入口移除后的用例改写）。
- 未做真实启动器联调：本机未启动 pytauri 运行态，运行中列表的图标链路以单元测试和接口字段断言覆盖。
