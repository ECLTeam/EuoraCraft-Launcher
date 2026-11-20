# 文件集媒体库与窗口表面方案（2026-11-20）

- 状态：已实施（2026-11-20）。文件集后端、媒体库页面、背景与实例图标接入、窗口表面配置均已落地；未改动 PyTaruiX。实施记录见第 10 节。
- 关联：背景媒体加载见 [ADR-002](ADR-002-background-active-media-loading.md)；数据目录语义见 [数据目录重组方案](data-directory-reorg-and-lockless-plan-2026-11-13.md)。

## 1. 需求与已确认决策

用户提出引入「文件集」：可自由添加图片与视频，用于设置背景图片/视频、实例自定义图标等；同时引入自定义窗口透明配置项。

经确认的决策：

| 决策项 | 选择 | 含义 |
| --- | --- | --- |
| 文件集定位 | 独立媒体库 + 新入口 | 背景与图标各增加「从文件集选择」，保留原有本地文件选择 |
| 存储策略 | 复制入库、按内容去重 | 导入即复制到 `userdata/filesets/`，文件移动/删除不影响启动器 |
| 窗口透明 | 模式枚举 + 透明度滑杆 | 实色 / 半透明 / 毛玻璃三档，配 0-100 表面不透明度 |
| 文件类型 | 仅图片与视频 | 不做通用文件管理器，不收纳皮肤披风 |

窗口透明不需要改动 PyTaruiX：`transparent` 是 Tauri 的构建期配置项，PyTaruiX 已完整透传，项目当前已在 `_build_config()` 中使用；透明度由 CSS 控制。详见第 5 节。

## 2. 现状与硬约束

### 2.1 背景与图标现状

| 项 | 现状 | 影响 |
| --- | --- | --- |
| 背景配置 | `ui.background`，后端仅校验「是对象」，前端 valibot 宽松校验 | 新增字段后端零改动 |
| 背景文件 | 只存绝对路径，不复制；仅远程图片落盘 `userdata/backgrounds/` | 文件移动即失效，正是文件集要解决的问题 |
| 图片加载 | `ecl-img` 协议，令牌 TTL 6 小时、上限 512 | 可直接复用 |
| 视频加载 | `ecl-media` 协议，**单令牌、每次签发清空全部旧令牌**（`ECL/api/media_protocol.py:122`） | 媒体库多视频预览会互相踢掉，必须改令牌模型 |
| 实例图标 | 必须复制为 `versions/<id>/.ecl/icon.<ext>`，字段只接受裸文件名 | `source_path` 传文件集真实路径即可，后端零改动 |

### 2.2 窗口透明现状

| 项 | 现状 | 影响 |
| --- | --- | --- |
| 窗口标志 | `_build_config()`（`ECL/desktop/tauri.py:151-190`）按 `window_chrome` 派生 `transparent`/`shadow` | `custom` 模式**已经** `transparent=true`；Windows 默认 `system_shadow` 模式下为 `false`，CSS 半透明透不出桌面 |
| 透明开关 | `transparent` 是 Tauri 构建期布尔配置，PyTaruiX 透传（`python/pytaurix/webview.py:116`） | 开启透明不需要新增原生接口 |
| 运行时改窗口 | PyTaruiX 无 `set_opacity`、无 `set_effects` | 表面模式变更需重启生效，与现有 `window_chrome` 做法一致 |
| CSS 能力 | `backdrop-filter` 只模糊页面内已绘制内容 | 永远模糊不到桌面，真毛玻璃只能靠原生 effects |
| 半成品字段 | `theme.transparent_bg` 有持久化与 setter，设置页无入口；`--main-bg-layer-opacity` 无消费者 | 本次一并收敛，避免留下第二套语义 |

### 2.3 可复用设施

| 能力 | 复用点 |
| --- | --- |
| 「元数据 JSON + 摘要命名 + 去重导入」模式 | `ECL/minecraft/services/account/wardrobe.py:103-260` |
| 图片真实解码与尺寸校验 | `ECL/minecraft/services/game/instance_profiles.py:134-193` |
| 缩略图生成与缓存 | `ECL/minecraft/services/game/screenshots.py:79-104`（WebP + sha256 缓存键） |
| 原子写入 | `ECL/utils/files.py:37-71` |
| 卡片 + 网格 UI | `frontend/src/features/accounts/components/WardrobeCard.vue`、`WardrobeGrid.vue` |
| 多选文件对话框 | `select_files`（`ECL/api/files.py:872-895`） |

## 3. 目标与非目标

目标：

1. 用户能把本地图片/视频导入文件集，重复导入不产生副本，源文件移动或删除不影响使用。
2. 背景图片、背景视频、实例图标都能从文件集选择，原有本地选择入口保留。
3. 窗口提供「实色 / 半透明 / 毛玻璃」三档表面模式与表面不透明度滑杆，配置可持久化并在重启后生效。

非目标：

- 不做多文件集分组、标签、搜索（单集合足够覆盖当前用途；不预留分组抽象）。
- 不收纳皮肤、披风、任意文档；衣柜与既有资源管理不变。
- 不改 `qml/` 移植前端（未进发行物）。
- 不做连原生标题栏一起半透明的整窗 alpha；`mica` 仅 Windows 提供，其他平台明确降级为半透明并上报不支持，不静默失效。

## 4. 文件集设计

### 4.1 数据模型与存储

目录布局（新增于 `userdata/` 下，符合「用户拥有、不可重建」语义）：

```
<data_path>/userdata/filesets/
  filesets.json          # {"version": 1, "items": [...]}
  files/<sha256>.<ext>   # 内容寻址，同内容只存一份
<data_path>/cache/fileset-thumbnails/<sha256>.webp   # 可重建，不入 userdata
```

条目结构：

```python
@dataclass(frozen=True, slots=True)
class FileSetItem:
    id: str                    # uuid4 hex
    kind: str                  # image | video
    name: str                  # 可改的显示名，默认原文件名
    sha256: str
    extension: str             # 含点，小写
    byte_size: int
    width: int | None          # 图片必有；视频为 None
    height: int | None
    created_at: str            # ISO 8601 UTC
    updated_at: str
```

校验沿用现有策略，不新造阈值：

| 类型 | 扩展名 | 上限 | 内容校验 |
| --- | --- | --- | --- |
| 图片 | png/jpg/jpeg/gif/bmp/webp | 10 MiB、4096px | 解析真实图片头与尺寸 |
| 视频 | mp4/webm | 1 GiB | 扩展名 + 大小；不做解码探测 |

### 4.2 后端服务 `FileSetStore`

位置：`ECL/fileset/store.py`（新建子包，纯内核能力，不依赖 MC 语义，符合分层约束）。

公开接口：

| 方法 | 行为 |
| --- | --- |
| `list_items()` | 按 `updated_at` 倒序返回副本，不含绝对路径 |
| `import_files(paths)` | 逐个校验 → sha256 → 去重 → 原子写入 → 生成缩略图 → 更新元数据；返回 `{imported, duplicated, failed}` |
| `rename_item(item_id, name)` | 改显示名，不动文件 |
| `delete_items(item_ids)` | 删元数据与文件，清孤立缩略图 |
| `resolve(item_id)` | 返回 `{path, kind, extension}`，供 IPC 层签发协议地址 |
| `thumbnail_path(item_id)` | 返回缩略图路径，缺失时按需生成 |

元数据损坏处理照衣柜：备份为 `filesets.corrupt-<时间戳>.json` 后重建，不静默丢数据。
`_cleanup_files()` 清理无引用文件与残留 `.tmp`，逻辑同 `wardrobe.py:157-165`。

### 4.3 IPC 契约

新增命令（`ECL/api/fileset.py` 新建领域处理器）：

| 命令 | 入参 | 返回 | 说明 |
| --- | --- | --- | --- |
| `fileset_list` | 无 | `FileSetItem[]` | 列表，不含绝对路径 |
| `fileset_import` | `{paths: string[]}` | `{imported, duplicated, failed[]}` | 批量导入，部分成功也返回明细 |
| `fileset_rename` | `{item_id, name}` | `FileSetItem` | 显示名，1-80 字 |
| `fileset_delete` | `{item_ids: string[]}` | `{deleted: string[], in_use: string[]}` | 被背景引用的条目拒绝删除 |
| `fileset_resolve` | `{item_id}` | `{path, url, kind}` | 图片走 `ecl-img`，视频走 `ecl-media` |
| `fileset_thumbnail` | `{item_id}` | `{path, url}` | 图片缩略图；视频返回空，前端用首帧 |

同步改动（每条命令都要过）：

1. `ECL/api/registry.py` 的 `command_names`
2. `ECL/api/models.py` 的 `RequestModelRegistry.models` 与 `request_schemas()`
3. `ECL/api/operation_logging.py` 的 `IpcLogSpec`
4. `frontend/src/types/api.ts` 的 `CommandPayloadMap`、`COMMAND_NAMES`、`CommandResponseMap`
5. `frontend/src/api/transport/showcase/index.ts` 的展示模式 mock

`select_files` 扩展：`FileSelectionPurpose` 增加 `FILESET_IMPORT`，过滤扩展名为图片 + 视频白名单，多选。不改现有 purpose 的语义。

### 4.4 引用保护

文件集条目可能被背景引用，删除时必须拦截。

在 IPC 边界层（`ECL/api/fileset.py`）做检查，因为这里同时可访问配置与文件集服务，不把反向依赖塞进 store：

```
fileset_delete(item_ids):
  referenced = 收集 ui.background 中 image.path / video.path / poster_path
  for item in item_ids:
      若 resolve(item).path 与任一 referenced 路径规范化后相等 → in_use
      否则 → 可删
```

路径比较统一 `Path.resolve(strict=False)` 后按平台大小写规则比较（Windows 忽略大小写）。
实例图标与封面是**复制**语义，不构成引用，无需检查。

### 4.5 背景接入

背景配置**不新增字段**。文件集是内容寻址（`files/<sha256>.<ext>`），路径本身稳定，因此继续写入 `image.path` / `video.path` 即可，旧配置无需迁移。

差异只体现在入口：背景设置多一个「从文件集选择」按钮，选中后经 `fileset_resolve` 取得路径与协议地址，再走现有 `patchUiBackground` 与 `setBackgroundImage` / `setBackgroundVideo`。

### 4.6 视频协议改多令牌

`ecl-media` 现在单令牌语义（`register_local` 中 `_bindings.clear()`），媒体库画廊无法同时持有多个视频地址。改为：

- 令牌表按 `path` 复用：同一路径已存在未过期令牌则直接返回，不重复签发
- 上限从 1 提升到 32，超出按最早过期淘汰
- 保留 `revoke()` 供背景切换时主动失效
- 令牌数量与 TTL 仍是硬边界，不放开为无限

这是本方案唯一改动共享协议服务的地方，需单独回归背景视频的既有行为（切换背景时旧流必须失效）。

## 5. 窗口透明的实现方式

### 5.1 配置即可开启，不需要改 PyTaruiX

`transparent` 是 Tauri `WindowConfig` 的构建期布尔项，PyTaruiX 已完整透传：`python/pytaurix/webview.py:116` 的 `transparent → transparent` 映射直接写入窗口配置，`docs/zh-CN/windows.md:36` 也把它列为基础构建项。ECL 当前已在用（`ECL/desktop/tauri.py:175`，`custom` 模式为 `true`）。

需要分开的两个层次：

| 要控制什么 | 靠什么 | 生效时机 | 是否需要原生接口 |
| --- | --- | --- | --- |
| 窗口能否透明 | `transparent: true` 构建期配置 | 重启 | 不需要 |
| 透明到什么程度 | CSS alpha（`--window-surface-alpha`） | 实时 | 不需要 |

`transparent: true` 让 WebView 背景参与透明合成，页面中 alpha 小于 1 的地方直接透出桌面。因此「可调的窗口透明度」等于一个配置项加一个 CSS 变量，不需要新增任何绑定；拖动滑杆能实时预览，比经 IPC 调原生接口的反馈还快。

### 5.2 各表面模式的效果

| 模式 | transparent | shadow | 附加 | 观感 |
| --- | --- | --- | --- | --- |
| `solid` | 跟随 `window_chrome` | 跟随 `window_chrome` | 无 | 现状不变 |
| `translucent` | `true` | `false` | 无 | 背景层按 alpha 透出桌面 |
| `mica` | `true` | `false` | `windowEffects: mica` | 桌面被系统模糊后作为底色 |

`mica` 同样不需要改上游：主窗口配置是整份 `tauri::Config` 反序列化，`windowEffects` 可以直接写进 `_build_config()` 返回的窗口字典。只有**子窗口**走 `WebviewWindowBuilder` 白名单才会拒绝未知字段，本方案不涉及子窗口。

### 5.3 确实需要改上游的边界

| 场景 | 需要改 PyTaruiX 吗 | 说明 |
| --- | --- | --- |
| 透明开关 + 可调 alpha | 不需要 | 配置 + CSS |
| 主窗口毛玻璃 | 不需要 | 配置可透传 `windowEffects` |
| 连系统标题栏一起半透明 | 需要 `set_opacity` | CSS 覆盖不到原生标题栏，且整体观感通常更差 |
| 不重启切换 `transparent` | 需要重建窗口或新增绑定 | 构建期标志 |

后两类当前没有需求，按「未来可能扩展不作为单独依据」不预留实现。将来确需时再按 PyTaruiX 的变更流程单独立项，包含版本发布与 POC。

### 5.4 需要实测的观感项

配置能开透明不等于观感可接受，实施时要在 Windows 上确认：

1. `transparent: true` + `shadow: false` + 自绘边缘：窗口边缘与圆角是否出现硬边或黑边。
2. `transparent: true` 播放视频背景：无合成闪烁、无黑框。
3. mica 在 Windows 11 深色与浅色主题下的可读性。
4. 低 alpha（如 60%）时侧栏、卡片、文字对比度是否仍可读。

## 6. ECL 侧窗口表面设计

### 6.1 配置模型

`ui.theme` 新增两项，并删除半成品 `transparent_bg`：

```python
"window_surface": "solid",    # solid | translucent | mica
"window_opacity": 100,        # 0-100，写入 CSS alpha；solid 下只能看到相对窗口底色的变化
```

同步位置：`ECL/utils/config.py:98-124` 默认值、`frontend/src/types/config.ts` 的 `ThemeConfig`、`frontend/src/features/settings/model/settingsSnapshot.ts` 的 valibot schema、`useTheme.ts` 的 ref/写入 CSS/`initTheme`/`saveThemeConfig`/导出、`AppearanceTab.vue` 的 `SettingRow`、六套 i18n。

### 6.2 窗口标志推导

`_build_config()` 增加表面模式分支，优先级高于 `window_chrome`：

| surface | transparent | shadow | windowEffects | 说明 |
| --- | --- | --- | --- | --- |
| `solid` | 跟随 `window_chrome` | 跟随 `window_chrome` | 无 | 现状不变 |
| `translucent` | `true` | `false` | 无 | 强制 chrome 降为 `custom`，UI 提示边缘改为自绘 |
| `mica` | `true` | `false` | `mica`（仅 win32） | 非 Windows 降级为 `translucent` 并上报不支持 |

`window_opacity` 只有一条生效路径：写入 CSS 变量 `--window-surface-alpha`，与表面模式无关，任何模式下都可调。`solid` 模式下调整只是让背景层相对于窗口自身底色变淡，看不到桌面，因此前端在该模式下应给出说明而非隐藏滑杆。

能力上报沿用 `system_shadow_supported` 的范式：`launcher_info` 增加 `window_opacity_supported`、`window_effects_supported`，前端据此禁用选项并给出去向文案。

### 6.3 前端渲染

```css
.app-background {
  background: color-mix(in srgb, var(--bg-base) calc(var(--window-surface-alpha, 1) * 100%), transparent);
}
```

`PluginWindowShell.vue` 现有的 `--bg-app` 消费者改为读取新变量，随 `transparent_bg` 一并收敛。

不透明度只作用于窗口最底层表面；卡片与面板的不透明度仍由既有 `card_opacity` 独立控制，两者语义在设置页文案中明确区分。

## 7. 分阶段实施

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| 1 | ECL 文件集后端：`FileSetStore`、6 条 IPC、`select_files` 扩展、视频协议多令牌、测试 | 导入去重、格式与大小拦截、损坏元数据备份重建、删除引用拦截、背景视频切换仍正常 |
| 2 | ECL 前端媒体库页面 + 背景/图标接入 | 导入、改显示名、删除、从文件集设背景与图标均可用；旧配置继续可用 |
| 3 | ECL 窗口表面：配置项、标志推导、CSS 变量、设置滑杆、能力上报 | 三档模式切换后重启生效；不支持平台选项禁用且文案正确 |

阶段 2 依赖 1；阶段 3 只改配置与样式，可与前两个阶段并行。

## 8. 测试计划

后端：

- 文件集：重复导入返回 `duplicated` 而不写第二份；超限图片/视频被拒；元数据损坏备份后重建；删除被背景引用的条目返回 `in_use`；越界路径与符号链接被拒。
- 视频协议：同路径复用令牌；超过 32 条按最早过期淘汰；`revoke` 后旧地址返回 403；Range 与 `if-range` 行为不变。
- 窗口：`_build_config` 的 surface × chrome × platform 组合矩阵，扩展 `tests/test_frontend_api_config.py:1509-1531` 的参数化断言。

前端：

- 媒体库页面：空态、加载、失败、部分导入成功、删除确认。
- 背景接入：从文件集选择后配置写入 `source: 'fileset'` 且路径正确；删除被引用条目的提示。
- 图标接入：从文件集选择后 `.ecl/icon.<ext>` 正确生成。
- 窗口表面：三档模式设置页状态、能力位禁用逻辑、`AppearanceTab.test.ts` 增补。

手工验收（Windows）：

- `transparent: true` 下播放视频背景、拖动窗口、跨显示器、最小化恢复时的合成表现。
- mica 在 Windows 11 的观感与深色/浅色主题切换。
- 非 Windows 明确提示不支持，不出现静默失效。

## 9. 风险与未决项

| 风险 | 影响 | 处理 |
| --- | --- | --- |
| `transparent: true` 与自绘边缘组合在 Windows 上可能出现 1px 白边或硬边 | 半透明模式观感变差 | 5.4 实测；必要时只在 `native` 边缘下提供半透明 |
| 表面模式是构建期标志，改后需重启 | 用户以为设置没生效 | 设置项文案注明「重启后生效」，与现有 `window_chrome` 保持一致 |
| 视频背景与透明合成叠加 | 视频边缘发灰或出现黑框 | 5.4 实测；必要时透明模式下限制视频背景 |
| 视频画廊同时预览耗资源 | 滚动卡顿 | 卡片懒加载（可见时才加载视频），网格限制同时播放数量 |
| 删除文件集条目破坏现有背景 | 用户背景突然失效 | 删除前引用检查 + `in_use` 明确拒绝 |
| `transparent_bg` 删除 | 插件窗口外壳背景变量变更 | 同步改 `PluginWindowShell.vue`，一次替换 |

## 10. 实施记录（2026-11-20）

### 后端

| 文件 | 变更 |
| --- | --- |
| `ECL/fileset/store.py` | 新建 `FileSetStore`：内容寻址存储、导入去重、PIL 真实解码校验、WebP 缩略图、删除清理、损坏元数据备份重建 |
| `ECL/fileset/__init__.py` | 包标记 |
| `ECL/api/fileset.py` | 新建 `FileSetHandlers`，6 条命令；删除时在边界层比对背景引用 |
| `ECL/api/models.py` | 新增 4 个请求模型、6 条 `RequestModelRegistry` 映射、`FileSelectionPurpose.FILESET_IMPORT` |
| `ECL/api/registry.py` | 注册 6 条命令 |
| `ECL/api/operation_logging.py` | 补齐 6 条日志规格 |
| `ECL/api/files.py` | `select_files` 支持 `fileset-import`，过滤图片与视频扩展名 |
| `ECL/api/media_protocol.py` | 令牌改为按路径复用、上限淘汰，移除「新签发清空旧令牌」 |
| `ECL/api/frontend.py`、`ECL/api/bridge.py` | 聚合 `FileSetHandlers`，注入 `context.fileset`，`FileSetError` 进入已知错误集合 |
| `ECL/application.py`、`ECL/utils/config.py` | 装配 `FileSetStore`；新增 `window_surface` / `window_opacity`，删除 `transparent_bg` |
| `ECL/desktop/tauri.py` | 按表面模式推导 `transparent` / `shadow` / `decorations`，`mica` 仅在 Windows 写入 `windowEffects` |
| `ECL/api/system.py` | `launcher_info` 增加 `window_effects_supported` |
| `tests/test_layer_boundary.py`、`AGENTS.md` | `ECL/fileset/` 归入内核域并纳入分层扫描 |

### 前端

| 文件 | 变更 |
| --- | --- |
| `src/views/more/FilesetTab.vue` | 媒体库页面：网格、导入、改名、删除、视频悬停取流 |
| `src/features/fileset/components/FileSetPicker.vue` | 选择弹窗，供背景与实例图标复用 |
| `src/features/fileset/api/filesetApi.ts` | 6 条命令封装 |
| `src/types/fileset.ts`、`src/types/api.ts` | 条目类型与命令表 |
| `src/views/settings/AppearanceTab.vue` | 背景图片/视频增加「从文件集选择」；新增窗口表面下拉与不透明度滑杆 |
| `src/views/instances/InstanceDetailModal.vue` | 图标选择增加「文件集」入口，复用 `game_instance_icon_set` 的 `source_path` |
| `src/composables/useTheme.ts`、`src/styles/base.css`、`src/styles/app.css` | 表面色移到 `body`，由 `--window-surface-alpha` 控制；背景层不再叠加 alpha |
| `src/app/runtime/windowChrome.ts`、`src/types/system.ts` | `window_effects_supported` 能力位 |
| `src/features/windows/PluginWindowShell.vue` | 旧 `--bg-app` 消费者改为新变量 |
| 六套 i18n | 文件集与窗口表面文案；删除废弃的 `transparentBg` 键 |

### 验证

- 后端：`pytest tests/` 2746 passed、44 skipped、0 failed；`ruff check`、`ruff format --check` 对改动文件通过。
- 前端：`vitest run` 全量通过；`vue-tsc` 类型检查通过；`eslint` 通过；`vite build` 换输出目录构建成功（`dist/` 清理被环境的安全删除机制拦截，与代码无关）。
- 新增测试：`tests/test_fileset_store.py` 12 项；`tests/test_media_protocol.py` 多令牌语义 3 项；`tests/test_frontend_api_config.py` 窗口表面 6 项参数化用例。

### 未做

- PyTaruiX 未改动。整窗 alpha（连原生标题栏一起透明）与不重启切换 `transparent` 仍无实现，当前无需求。
- 未在真实 Windows 窗口上做 5.4 的观感实测；`color-mix` 与 `windowEffects` 的实际显示需启动器实机确认。
