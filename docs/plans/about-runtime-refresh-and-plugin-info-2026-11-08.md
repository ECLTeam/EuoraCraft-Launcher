# 关于页运行时信息更新与插件信息注册扩展字段 — 实施方案

日期：2026-11-08
状态：已实施（2026-11-09）

## 背景与目标

1. **关于页运行时信息过时**：运行时已迁移到 PyTaruiX（依赖 `pytaurix==0.1.0a1`，仓库 `github.com/ECLTeam/PyTauriX`，Apache-2.0），但关于页「运行时框架」「后端运行依赖」「法律信息」仍列 PyTauri / Tauri / pytauri-wheel。
2. **插件信息注册**：让插件通过 `plugin.json`（manifest）注册 `homepage`、`license` 扩展字段，后端校验并透传，关于页新增「插件」卡片展示已启用插件的信息。

用户已确认的决策：

- 运行时框架区块**只留 PyTaruiX 和 EasyTier，删掉 Tauri**。
- 插件信息走 **manifest 注册扩展字段**方案。

## 变更一：关于页运行时信息更新（仅前端）

### 改动点

| 文件 | 改动 |
| --- | --- |
| `frontend/src/config/urls.ts` | 删除 `pytauri`、`tauri` 条目；新增 `pytaruix: { repo: 'https://github.com/ECLTeam/PyTauriX', license: '<PyTauriX 仓库 LICENSE 链接>' }` |
| `frontend/src/features/settings/about/aboutContent.ts` | `technologies` 中 `pyTauri`/`tauri` 替换为 `pytaruix` 条目（initials `PX`，无自定义头像，走字母头像）；`runtime-frameworks` 分类 entries 改为 `[pytaruix, easyTier]`；`backendRuntimeEntries` 移除 `pytauri-wheel`（PyTaruiX 已在运行时框架展示，避免重复） |
| `frontend/src/views/settings/AboutTab.vue` | `licenseEntries` 删除 PyTauri、Tauri、pytauri-wheel 三条，新增 PyTaruiX（Apache 2.0）；`licenseById` 删除 `pytauri`/`tauri`/`pytauri-wheel` 键，新增 `pytaruix` |
| `frontend/public/img/avatars/pytauri.png` | 删除（唯一引用消失后） |
| 六语言 locale（de-DE / en-US / ja-JP / ru-RU / zh-CN / zh-TW） | `settings.aboutTab.technologies` 下删除 `pytauri`、`tauri`、`pytauriWheel`，新增 `pytaruix` 描述文案 |

### 验证

- 定向 vitest（AboutTab / aboutContent 相关）+ `vue-tsc` typecheck + eslint + prettier。
- 全局 grep 确认 `pytauri`/`URLS.tauri` 无残留引用（`pytaurix` 除外）。

## 变更二：manifest 注册插件信息扩展字段

### 后端（主仓库）

1. **`ECL/plugins/installation_store.py` — `WorkerManifest`** 新增可选字段：
   - `homepage: str = ""`：仅接受 `http://` / `https://` URL，最长 256。
   - `license: str = ""`：SPDX 表达式或简短说明文字，最长 128。
   - 校验失败走既有 `manifest_validation_error_code` 归类为 `PLUGIN_MANIFEST_INVALID`。
2. **`ECL/plugins/plugin.py` — `Plugin.__init__`**（系统插件路径）新增 `self.homepage` / `self.license` 属性，从 metadata 读取。
3. **`ECL/plugins/manager/registry.py` — `_plugin_entry`**：`Plugin` 实例分支与 dict 分支均透传 `homepage` / `license`（默认空字符串）。

### 前端

1. **`frontend/src/types/plugins.ts`**：`PluginInfo` 增加 `homepage: string`、`license: string`。
2. **关于页新增「插件」卡片**（`AboutTab.vue`）：
   - 数据源：复用 `usePluginStore`（与插件管理页同源），仅展示 `status === 'enabled'` 的插件。
   - 每行展示：图标（puzzle）/ title + 版本 / 作者 / 描述；`homepage`、`license` 为 `https` 链接时以按钮形式经 `openExternalUrl` 打开。
   - 无启用插件时不渲染整卡（避免空卡片）。
   - 新增行组件 `frontend/src/features/settings/components/AboutPluginRow.vue`，复用 AboutEntryRow 的行样式类，页面样式限定作用域。
3. **i18n**：六语言新增 `settings.aboutTab.sections.plugins`、`homepageLabel`、`licenseLabel`。

### 插件仓库同步

`D:/Projects/EuoraCraft-Launcher/` 下两个开发仓库的 `plugin.json` 增加：

```json
"homepage": "https://github.com/ECLTeam/ECL-Plugin-OnlineGame",
"license": "GPL-3.0"
```

（QomicexCompatible 同理，指向各自仓库。）`ECL_data/plugins/` 运行副本同步更新。

### 测试计划

- 后端定向 pytest：
  - `installation_store` 相关测试：homepage 合法/非法（非 http URL、超长）/缺省；license 超长拒绝。
  - `registry` 相关测试：`_plugin_entry` 两种分支透传 homepage/license。
  - `ruff check` / `ruff format --check`。
- 前端：AboutTab.test.ts 增加插件卡片用例（启用插件渲染、禁用不渲染、无插件整卡隐藏）；vitest + typecheck + eslint + prettier。

## 边界与说明

- 项目处于未正式发布阶段（AGENTS.md 第 2 节）：manifest 新字段不设旧格式别名；未提供 homepage/license 时字段为空字符串，前端对应链接不渲染。
- `homepage` 在后端限 http(s)，前端统一经 `openExternalUrl` 打开，不新增内联渲染面。
- 展示范围仅「已启用」插件；禁用/报错插件的完整信息仍在插件管理页查看，关于页不承担管理职责。
- 不涉及 IPC 协议结构变化（plugins 列表响应为既有 dict 通道增加两个字段）。

## 实施顺序

1. 变更一（纯前端，独立可交付）。
2. 变更二后端字段与透传 + 测试。
3. 变更二前端类型、关于页卡片、i18n + 测试。
4. 两个插件仓库 plugin.json 更新并提交；ECL_data 运行副本同步。
