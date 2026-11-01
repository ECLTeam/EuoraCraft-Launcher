# 外观 / 工具界面细节优化实施方案

日期：2026-11-01  
状态：已实施

## 目标与现状

本轮处理四处界面细节：

1. 「外观 → 高级字体设置」用原生 `<details>`，展开收起没有过渡，观感生硬。
2. 「更多 → 工具」里的「下载选项」「导出设置」同样是原生 `<details>`，问题一致。
3. 部分设置项在条件不满足时只是禁用置灰，例如视频背景静音后仍显示禁用的音量滑杆、窗口样式不是自定义标题栏时仍显示窗口圆角滑杆。
4. 设置/更多页的卡片滚动到滚动容器边缘时被裁成直角，与卡片自身的圆角不一致；工具卡片里「包含帽子层」用原生复选框，与「允许覆盖已有文件」的 naive-ui 复选框样式不一致。

## 选定方案

### 折叠面板

新增共用组件 `components/ui/CollapsiblePanel.vue`（样式 `styles/components/ui/CollapsiblePanel.css`）：外层 `display: grid`，在 `grid-template-rows: 0fr / 1fr` 之间过渡，按内容实际高度展开，不需要写死 `max-height`；收起时同时设 `visibility: hidden` 与 `inert`，避免隐藏内容被 Tab 聚焦；`prefers-reduced-motion` 下关闭过渡。

三处折叠统一改为「`button` + `CollapsiblePanel`」：

- 外观页高级字体：`aria-expanded`、右侧 chevron 旋转 180°、保留覆盖数量徽标。
- 工具页下载选项：`optionsOpen` 取代原来的 `HTMLDetailsElement` 引用；校验失败时仍自动展开（改为 `watch(validationError)`）。
- 工具页导出设置：`optionsOpen` 本地状态，chevron 旋转 90°。

### 禁用项改为隐藏

- 视频背景：静音时整行音量调节 `v-if="!backgroundVideo.muted"`，去掉 `:disabled`。
- 窗口样式：仅自定义标题栏下显示窗口圆角，其余样式直接隐藏。

### 卡片裁切圆角

滚动容器 `.section-layout__viewport` 增加 `border-radius: var(--ecl-card-radius)`：内容滚到容器边缘时按圆角裁切，被遮挡的断面跟随卡片圆角，而不是直角。层级、位置与滚动行为不变。

### 复选框统一

新增 `features/tools/toolCheckboxTheme.ts` 作为工具卡片复选框的统一 `themeOverrides`；下载卡片沿用该常量，头像裁剪的「包含帽子层」由原生 `<input type="checkbox">` 改为同款 `NCheckbox`，字号、边框、选中色与下载卡片一致。

对勾颜色改为由全局主题提供：`config/theme.ts` 新增 `primaryFillTextColor = '#ffffff'`，`createThemeOverrides` 的 Button 正文色与新增的 `Checkbox.checkMarkColor` 都引用它。原因是 `--text-on-primary` 由 `textOnColor(primary)` 按对比度计算，`#5B6FF5` 这类较亮紫色会算出黑色，与主按钮固定的白色正文不一致；naive 复选框默认又取主题 `baseColor`（暗色取 `cardColor`），同样会出现深色对勾。

## 修改范围

- 新增：`components/ui/CollapsiblePanel.vue` + CSS + 测试；`features/tools/toolCheckboxTheme.ts`。
- 修改：`views/settings/AppearanceTab.vue`（折叠结构、隐藏两行设置）、`styles/views/settings/AppearanceTab.css`；`components/tools/CustomDownloadCard.vue`、`SkinAvatarToolCard.vue` 及 `ToolOptions.css`、`SkinAvatarToolCard.css`；`styles/components/layout/SectionLayout.css`。
- 测试：新增 `AppearanceTab.test.ts`（折叠展开、静音隐藏音量、非自定义标题栏隐藏圆角）、`CollapsiblePanel.test.ts`；更新 `CustomDownloadCard.test.ts`、`SkinAvatarToolCard.test.ts` 中针对 `details[open]` 与原生复选框的断言。

## 验证

- `eslint`、`prettier --check`、`vue-tsc` 通过；`vitest run` 全量 197 文件 / 1201 用例通过。
- 演示模式实测（无头浏览器逐帧采样）：外观页高级字体高度 0 → 197 平滑增长；工具页下载选项 0 → 295，chevron 旋转 90°；截图确认展开态布局正常。
- 卡片裁切：滚动到卡片被顶部裁切的位置，容器圆角处命中测试返回容器外元素，截图放大可见断面为圆角。

## 风险与边界

- `grid-template-rows` 过渡依赖较新的 Chromium（WebView2 常青版本满足）；不支持时退化为瞬时展开，功能不受影响。
- 滚动容器的圆角会同时影响「更多」页等复用 `SectionLayout` 的页面，属于同一视觉规则；Folia 皮肤若单独改写卡片圆角，裁切圆角仍取默认令牌，存在轻微差异。
- 视频背景的静音态需要真实视频文件才能切换，界面验证以单元测试覆盖。
