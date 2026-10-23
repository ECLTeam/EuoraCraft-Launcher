# 文档索引

本目录保存启动器的长期文档与历次功能变更的过程记录。

## 目录结构

| 目录 | 内容 | 维护方式 |
| --- | --- | --- |
| `guides/` | 面向使用者的操作指南 | 随功能演进持续更新 |
| `protocols/` | 插件、主题等对外协议与格式规范 | 协议变更时同步更新 |
| `designs/` | 架构设计与实现设计 | 设计定稿后基本冻结 |
| `adr/` | 架构决策记录 | 只追加，不修改历史结论 |
| `plans/` | 单次功能变更的实施方案与验收记录 | 完成后归档，不再更新 |

长期文档与历史计划分开存放，是为了让「当前有效的行为约定」不被大量一次性记录淹没。查阅历史决策时优先看 `plans/` 中对应文档的「实施与验收记录」章节。

## 使用指南

| 文档 | 说明 |
| --- | --- |
| [发布流程](guides/release-process.md) | Beta 标签、产物验收与失败处理；`AGENTS.md` 第 9 节的完整版本 |
| [无头运行说明](guides/headless-runtime.md) | `--headless` 任务协议、输出格式与插件子进程边界 |
| [启动器命令行](guides/launcher-cli.md) | 启动参数、实例快捷启动与设置生效顺序 |
| [可视化主题设计指南](guides/theme-designer-guide.zh-CN.md) | 主题设计器、预设库与 `.ecltheme` 文件 |

## 协议与格式

| 文档 | 说明 |
| --- | --- |
| [插件包格式](protocols/eclplugin-package-format.md) | `.eclplugin` 归档结构、权限声明与 Worker API v2 |
| [主题与插件外观协议](protocols/theme-plugin-protocol.md) | 主题数据格式、插件插槽与外观扩展点 |
| [联机扩展协议插件开发](protocols/plugin-connector-extensions.md) | 第三方联机协议如何接入启动器 |
| [插件实例兼容扩展点](protocols/plugin-instance-compatibility.md) | 只读实例元数据来源的注册方式 |

## 架构设计

| 文档 | 说明 |
| --- | --- |
| [Worker API v2 设计](designs/plugin-worker-api-v2-implementation-design.md) | 插件 Worker 进程的接口、权限与生命周期设计 |
| [无头进程集成设计](designs/plugin-headless-process-integration-design.md) | 插件子进程与启动器运行时的集成边界 |
| [原生 WebView 隔离设计](designs/plugin-native-webview-isolation-design.md) | 插件原生窗口的隔离与安全边界 |

## 决策记录

| 文档 | 说明 |
| --- | --- |
| [ADR-051 前端代码逻辑与结构优化](adr/ADR-051-前端代码逻辑与结构优化计划.md) | 前端分层与职责边界的决策依据 |

## 实施计划

`plans/` 保存每次功能变更的完整过程记录：需求、方案取舍、实施步骤与验收结果。这些文档描述的是**当时**的决策，不保证与当前代码一致；需要确认现行行为时以代码和 `AGENTS.md` 为准。

### 插件体系（15）

插件包格式、Worker 隔离、私有文件、权限与 SDK 统一。

- [plugin-dependency-isolation-installation-plan](plans/plugin-dependency-isolation-installation-plan.md)
- [plugin-download-task-implementation-plan](plans/plugin-download-task-implementation-plan.md)
- [plugin-install-directory-isolation-plan](plans/plugin-install-directory-isolation-plan.md)
- [plugin-instance-profile-write-implementation-plan](plans/plugin-instance-profile-write-implementation-plan.md)：选定实例的四字段修改、双许可与可信恢复验收
- [plugin-instance-read-api-implementation-plan](plans/plugin-instance-read-api-implementation-plan.md)
- [plugin-java-query-implementation-plan](plans/plugin-java-query-implementation-plan.md)：Java 查询与校验第一阶段方案，待审阅
- [plugin-network-read-implementation-plan](plans/plugin-network-read-implementation-plan.md)
- [plugin-operation-ownership-implementation-plan](plans/plugin-operation-ownership-implementation-plan.md)
- [plugin-private-file-delete-implementation-plan](plans/plugin-private-file-delete-implementation-plan.md)
- [plugin-private-file-replace-implementation-plan](plans/plugin-private-file-replace-implementation-plan.md)
- [plugin-private-files-implementation-plan](plans/plugin-private-files-implementation-plan.md)
- [plugin-sdk-unification-plan](plans/plugin-sdk-unification-plan.md)
- [plugin-worker-isolation-implementation-plan](plans/plugin-worker-isolation-implementation-plan.md)
- [plugin-worker-manager-integration-plan](plans/plugin-worker-manager-integration-plan.md)
- [plugins-into-more-plan](plans/plugins-into-more-plan.md)

### 联机与连接器（9）

联机页面布局、节点设置、房间创建与 NAT 工具。

- [connect-create-step-transition-plan](plans/connect-create-step-transition-plan.md)
- [connect-idle-form-redesign-plan](plans/connect-idle-form-redesign-plan.md)
- [connect-multi-page-plan](plans/connect-multi-page-plan.md)
- [connect-node-settings-entry-plan](plans/connect-node-settings-entry-plan.md)
- [connect-room-layout-redesign-plan](plans/connect-room-layout-redesign-plan.md)
- [connect-running-instance-select-plan](plans/connect-running-instance-select-plan.md)
- [connect-settings-autosave-header-plan](plans/connect-settings-autosave-header-plan.md)
- [connector-settings-plain-config-plan](plans/connector-settings-plain-config-plan.md)
- [nat-tools-compact-card-plan](plans/nat-tools-compact-card-plan.md)

### 实例与世界（15）

实例列表、扫描、隔离继承、存档、截图与服务器列表。

- [current-instance-isolation-inherit-plan](plans/current-instance-isolation-inherit-plan.md)
- [installed-instance-list-density-plan](plans/installed-instance-list-density-plan.md)
- [installed-instance-list-redesign-plan](plans/installed-instance-list-redesign-plan.md)
- [installed-instance-toolbar-plan](plans/installed-instance-toolbar-plan.md)
- [instance-health-precheck-removal-plan](plans/instance-health-precheck-removal-plan.md)
- [instance-list-layout-and-running-empty-state-plan](plans/instance-list-layout-and-running-empty-state-plan.md)
- [instance-management-loading-empty-state-plan](plans/instance-management-loading-empty-state-plan.md)
- [instance-mod-toggle-filename-sync-plan](plans/instance-mod-toggle-filename-sync-plan.md)
- [instance-resource-file-types-and-world-import-plan](plans/instance-resource-file-types-and-world-import-plan.md)
- [instance-route-cache-implementation-plan](plans/instance-route-cache-implementation-plan.md)
- [instance-scan-candidate-filter-plan](plans/instance-scan-candidate-filter-plan.md)
- [instance-shortcut-icon-scale-plan](plans/instance-shortcut-icon-scale-plan.md)
- [instance-world-seed-and-server-list-plan](plans/instance-world-seed-and-server-list-plan.md)
- [screenshots-servers-toolbar-style-plan](plans/screenshots-servers-toolbar-style-plan.md)
- [worlds-toolbar-icon-style-plan](plans/worlds-toolbar-icon-style-plan.md)

### 资源与下载（10）

下载页、资源搜索、整合包补全与资源包图标。

- [download-page-prefetch-implementation-plan](plans/download-page-prefetch-implementation-plan.md)
- [download-prefetch-and-debug-docking-plan](plans/download-prefetch-and-debug-docking-plan.md)
- [download-search-overlay-removal-plan](plans/download-search-overlay-removal-plan.md)
- [download-source-fallback-implementation-plan](plans/download-source-fallback-implementation-plan.md)
- [dual-source-resource-search-implementation-plan](plans/dual-source-resource-search-implementation-plan.md)
- [mcim-mod-source-plan](plans/mcim-mod-source-plan.md)
- [modpack-completion-plan](plans/modpack-completion-plan.md)
- [resource-detail-local-loading-plan](plans/resource-detail-local-loading-plan.md)
- [resource-version-selection-layout-plan](plans/resource-version-selection-layout-plan.md)
- [resourcepack-icons-selection-removal-plan](plans/resourcepack-icons-selection-removal-plan.md)

### 启动与运行时（12）

启动参数、CLI、无头运行、Java 管理与崩溃分析。

- [cli-entrypoint-responsibility-plan](plans/cli-entrypoint-responsibility-plan.md)
- [cli-launch-options-plan](plans/cli-launch-options-plan.md)
- [cli-quick-launch-plan](plans/cli-quick-launch-plan.md)
- [crash-analysis-pipeline-plan](plans/crash-analysis-pipeline-plan.md)
- [game-launch-button-display-name-plan](plans/game-launch-button-display-name-plan.md)
- [headless-runtime-implementation-plan-2026-10-13](plans/headless-runtime-implementation-plan-2026-10-13.md)
- [java-download-and-management-plan-2026-10-05](plans/java-download-and-management-plan-2026-10-05.md)
- [java-management-card-surface-plan](plans/java-management-card-surface-plan.md)
- [java-management-style-and-install-failure-plan-2026-10-11](plans/java-management-style-and-install-failure-plan-2026-10-11.md)
- [launch-advanced-options-plan](plans/launch-advanced-options-plan.md)
- [launch-command-multiline-plan](plans/launch-command-multiline-plan.md)
- [python-entrypoint-consolidation-plan](plans/python-entrypoint-consolidation-plan.md)

### 数据与架构（9）

数据层统一、后端分层、公告与头像缓存、接口清理。

- [announcement-service-consolidation-plan](plans/announcement-service-consolidation-plan.md)
- [avatar-cache-repair-plan](plans/avatar-cache-repair-plan.md)
- [backend-layer-restructure-plan](plans/backend-layer-restructure-plan.md)
- [backend-startup-cache-and-surface-correction-plan](plans/backend-startup-cache-and-surface-correction-plan.md)
- [data-layer-unification-plan-2026-10-13](plans/data-layer-unification-plan-2026-10-13.md)
- [domain-and-announcement-migration-plan](plans/domain-and-announcement-migration-plan.md)
- [frontend-startup-data-layer-plan](plans/frontend-startup-data-layer-plan.md)
- [project-guidelines-consolidation-plan](plans/project-guidelines-consolidation-plan.md)
- [redundant-interface-cleanup-plan](plans/redundant-interface-cleanup-plan.md)

### 评审与调研（4）

项目评审、UX 审计与同类启动器对比。

- [ecl-vs-hmcl-comparison-2026-10-02](plans/ecl-vs-hmcl-comparison-2026-10-02.md)
- [feature-comparison-and-optimization-plan](plans/feature-comparison-and-optimization-plan.md)
- [project-review-2026-09-12](plans/project-review-2026-09-12.md)
- [ux-and-semantic-audit-2026-10-04](plans/ux-and-semantic-audit-2026-10-04.md)

### 界面与交互（22）

主题、侧栏、工具栏、弹窗、标题栏与工具页布局。

- [account-locator-color-plan-2026-10-13](plans/account-locator-color-plan-2026-10-13.md)
- [background-blur-independent-control-plan](plans/background-blur-independent-control-plan.md)
- [business-child-modal-parent-links-plan-2026-10-12](plans/business-child-modal-parent-links-plan-2026-10-12.md)
- [font-customization-plan](plans/font-customization-plan.md)
- [global-modal-transition-handoff-plan](plans/global-modal-transition-handoff-plan.md)
- [language-selector-neutral-labels-implementation-plan](plans/language-selector-neutral-labels-implementation-plan.md)
- [launcher-ui-management-and-advanced-features-plan](plans/launcher-ui-management-and-advanced-features-plan.md)
- [macos-app-icon-and-dock-plan](plans/macos-app-icon-and-dock-plan.md)
- [nested-modal-preservation-plan](plans/nested-modal-preservation-plan.md)
- [sidebar-docs-debug-floating-and-process-hover-plan](plans/sidebar-docs-debug-floating-and-process-hover-plan.md)
- [sidebar-local-state-plan](plans/sidebar-local-state-plan.md)
- [startup-main-window-focus-plan](plans/startup-main-window-focus-plan.md)
- [task-center-style-and-java-install-move-plan](plans/task-center-style-and-java-install-move-plan.md)
- [task-center-whole-card-hover-plan](plans/task-center-whole-card-hover-plan.md)
- [theme-style-select-and-push-plan](plans/theme-style-select-and-push-plan.md)
- [tools-compact-workbench-plan](plans/tools-compact-workbench-plan.md)
- [tools-form-alignment-plan](plans/tools-form-alignment-plan.md)
- [tools-layout-and-custom-download-options-plan](plans/tools-layout-and-custom-download-options-plan.md)
- [tools-vertical-layout-and-skin-avatar-plan](plans/tools-vertical-layout-and-skin-avatar-plan.md)
- [ux-and-semantic-implementation-plan-2026-10-04](plans/ux-and-semantic-implementation-plan-2026-10-04.md)
- [window-titlebar-mode-implementation-plan](plans/window-titlebar-mode-implementation-plan.md)
- [windows11-system-shadow-window-plan](plans/windows11-system-shadow-window-plan.md)

### 其他归档

- [功能建议实施计划](plans/feature-recommendations/implementation-plan.md) — 早期功能建议的落地实施方案
- `plans/feature-recommendations/feature-recommendations.html` — 同一份建议的 HTML 导出产物，仅作留档

## 维护约定

- 新增功能方案放入 `plans/`，文件名沿用现有 `主题-plan.md` 风格；带日期的方案保留日期后缀。
- 方案完成后在文首状态行记录结论，不再另开文档。
- 协议、格式或对外行为变更时，同步更新 `protocols/` 与 `guides/` 中的对应文档，而不是只写在方案里。
- 移动或重命名文档时，必须同步修正引用方链接；`AGENTS.md` 与根目录 `README.md` 的引用同样适用。
