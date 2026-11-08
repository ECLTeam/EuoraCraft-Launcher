---
name: maintain-ecl-frontend
description: Maintain and standardize the EuoraCraft-Launcher (ECL启动器) Vue 3 frontend visual design. Use for changes or reviews under frontend/src involving page layouts, components, CSS, design tokens, Naive UI, responsive behavior, light/dark themes, Showcase browser screenshots, or visual consistency; especially Game, account management, wardrobe, instance download, settings, plugins, instance management, and connect pages.
---

# ECL启动器前端样式维护

## 工作流

1. 先读需求涉及的 Vue、CSS、测试和设计令牌，不凭单张截图直接改样式。
2. 从 [references/style-system.md](references/style-system.md) 选择页面原型、页面骨架组合和样式真源；先定骨架再写 DOM，不先写样式再补结构。
3. 在 Showcase 浏览器中查看目标页和至少两个同类基准页；按 [references/browser-validation.md](references/browser-validation.md) 验收。
4. 先修正信息架构、主表面、密度和对齐，再调整颜色、边框和动效。
5. 复用现有 `UiButton`、`UiCard`、`UiInput`、`UiIcon`、`SectionLayout`、Naive UI 和设计令牌；公共 Ui 一律从 `@/components/ui` 导入，不直接引用目录内的 `.vue` 文件。
6. 只给目标 feature 增加必要的 scoped 样式；不要用全局覆盖补救局部布局。
7. 检查浅色、深色、背景图片和窄窗口；交互页还要检查空闲、加载、错误、成功和禁用状态。
8. 格式化改动文件，并运行相称的 lint、类型检查、测试和 Showcase 构建。

## 决策顺序

按以下优先级解决视觉冲突：

1. 用户明确指定的基准页面和当前浏览器效果。
2. `frontend/src/styles/tokens.css` 与 `design-system.css`。
3. 核心确定页面的共同模式，不追随某一个页面的偶然写法。
4. 通用 UI 组件契约。
5. feature 局部 CSS。

不要套用其他项目的 React/Tailwind 类名和视觉值；只迁移功能结构与交互语义。

## 页面原型

- 沉浸首页：仅游戏页使用，可让背景和底部启动操作成为视觉主角。
- 单一管理面板：插件页、联机页等没有二级信息架构的页面，使用一张填满内容区的 `ecl-surface`，标题、工具栏、列表或表单放在同一表面内。
- 分区管理页：下载、设置等确实存在多个稳定分区时使用 `SectionLayout`，左侧约 156px，右侧为主要表面。
- 路径与内容双栏：实例管理使用路径栏加主要内容表面，不要套第三层导航。
- 全屏任务页：账户管理和衣柜通过 `FullscreenModal` 呈现；内部使用一到两个主要表面，不复制应用侧边栏。

二级分区少于三个时，不新增页内侧边栏。单一任务不要同时出现“外部页头 + 内部重复标题卡片”。

## 宽度契约

页面宽度一律撑满内容区，禁止为整块内容加 `max-width` 居中缩窄。

1. 页面根容器必须包含 `.ecl-page`，宽度和高度都撑满扣除标题、工具栏后的内容区，不因内容少而收缩。
2. 主表面用 `.ecl-page__body` 包裹，撑满页面根容器；不使用 `max-width` 加 `margin: 0 auto`。
3. 表面内部靠栅格、分栏和合理控件宽度填充可用空间，**不允许**用“整块内容居中定宽”制造留白。表单字段过宽时用两列栅格，不要恢复居中定宽。
4. 滚动交给 `.ecl-page__scroll` 或明确的 viewport 容器，不用固定 `height` 撑窗口，不用 `overflow: hidden` 掩盖溢出。

同一页面内不同标签页必须共用同一宽度写法。设置页当前是反面案例：`AppearanceTab`、`GameTab`、`LauncherTab` 曾各自写 `max-width: 820px; margin: 0 auto`，与撑满的 `JavaTab` 并列时宽度会跳变。新增标签页一律与同页基准标签页对齐，不复制相邻文件的偶然写法。

## 密度约束

- 页面间距优先使用 `--ecl-page-gap`、`--ecl-page-padding` 和已有 `--s-*` 令牌。
- 普通按钮使用组件默认 29px 或大号 36px；只有游戏启动主操作可明显放大。
- 工具栏、设置行、列表行和选择条保持紧凑，通常为 44–64px。
- 管理页选择条不得因说明文字扩张成 80px 以上；图标容器通常为 32–40px。
- 页面标题约 16–20px，分区标题约 13–15px，正文/控件约 12–13px，辅助文字约 10–11px。
- 用一条边框和轻微背景区分层级；避免多重描边、大阴影、发光和无功能渐变。

如果新控件明显比下载列表、插件行或设置行更高，先收紧尺寸，而不是继续增加装饰。

## 主题与交互

- 颜色、表面、边框、文字、圆角和阴影使用现有变量；禁止为浅色主题硬编码白色面板。
- 保证背景图片下表面仍可读，并保持现有半透明表面语言。
- 所有可点击自定义行必须有 hover、`:focus-visible`、disabled 和明确的光标状态。
- 动效使用已有 duration/easing；管理页只使用轻微颜色或 1px 位移反馈。
- 在窄窗口优先隐藏辅助描述或重排控制，不压缩主要操作到不可读。

## 验收

在 `frontend/` 执行：

```powershell
pnpm exec prettier --check <changed-files>
pnpm lint
pnpm check:design
pnpm check:page-root
pnpm typecheck
pnpm test
pnpm build:showcase
```

布局和宽度改动另跑视觉回归，它比对四个基准页面与基线：

```powershell
$env:PLAYWRIGHT_CHANNEL = 'msedge'
python tests/browser/test_visual.py
```

确认差异都是预期改动后才用 `--update` 重建基线，不吸收无关的页面高度或环境漂移。

样式和布局改动必须在真实浏览器查看，jsdom 单测不计算布局，不能作为宽度和间距的验收依据。启动 `pnpm dev` 后逐页核对：

- 960×600 与默认窗口下主表面左右边界一致、无水平滚动；
- 同一页面不同标签页切换时宽度不跳变；
- 列表末项与主要操作可滚动到达；
- Classic / Folia × 亮暗，以及带背景图片时的表面可读性。

测试范围可以先针对 feature，但交付前至少运行类型检查和 Showcase 构建。报告现有无关警告，不要为通过检查改动用户的无关文件。
