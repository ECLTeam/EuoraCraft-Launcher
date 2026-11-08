# ECL 前端样式真源

## 目录

- 设计基础
- 核心基准页面
- 页面结构规则
- 控件与密度
- 常见反模式

## 设计基础

先读取这些文件：

- `frontend/src/styles/tokens.css`：`--ecl-*` 浅色/深色令牌。
- `frontend/src/styles/design-system.css`：`ecl-page`、`ecl-surface`、`ecl-toolbar`、页面标题。
- `frontend/src/styles/components/layout/SectionLayout.css`：稳定二级分区布局。
- `frontend/src/styles/components/ui/Button.css`、`Card.css`、`Input.css`：通用控件尺寸与状态。
- `frontend/src/styles/common.css`：仍在使用的旧通用模式；不要让它覆盖新版 `--ecl-*` 语义。

项目同时存在 `--ecl-*` 和旧 `--bg-*`/`--s-*` 变量。新页面优先使用 `--ecl-*`；修改旧组件时保持其所在体系，除非任务明确要求迁移整块组件。

## 核心基准页面

这些页面共同确定产品风格，不把开发工具页或单个实验组件当成基准。

| 页面     | 代码位置                                                            | 应继承的模式                                                 |
| -------- | ------------------------------------------------------------------- | ------------------------------------------------------------ |
| 游戏首页 | `views/Game.vue`、`styles/views/Game.css`、`components/game/*`      | 背景参与构图、右侧紧凑玻璃卡、底部启动操作；仅用于沉浸首页   |
| 账户管理 | `views/Game.vue` 中账户 `FullscreenModal`、`Game.css`               | 全屏任务容器、单一列表表面、紧凑表头与行操作                 |
| 本地衣柜 | `features/accounts/components/WardrobeModal.vue`                    | 双主表面、工具栏、紧凑资源网格、预览区                       |
| 实例下载 | `views/Download.vue`                                                | 156px 分区栏、单一主内容表面、44–56px 列表行、顶部工具栏     |
| 设置     | `views/Settings.vue`、`views/settings/*`、`SectionLayout.css`       | 分区栏、右侧分组表面、行级说明与控件对齐                     |
| 插件     | `views/Plugins.vue`、`styles/views/Plugins.css`                     | 一张填满页面的大表面，表面内工具栏、表头和紧凑列表           |
| 实例管理 | `views/Instances.vue`、`views/instances/InstancesTab.vue`、相关 CSS | 路径栏 + 主表面、密集表格、紧凑图标操作                      |
| 联机     | `views/Connect.vue`、`styles/views/Connect.css`                     | 无二级导航；一张大表面，内部根据状态切换入口、表单或房间信息 |

## 页面结构规则

1. 先确定页面原型，再写 DOM。
2. 单一管理任务使用一张覆盖可用内容区的主要 `ecl-surface`；把标题和工具栏放入该表面顶部。
3. 只有三个及以上稳定分区才使用 `SectionLayout` 左侧栏。
4. 同一层级最多一到两个主要表面。卡片内部可有分组或行，但不要继续套完整卡片。
5. 主表面设置 `min-width: 0; min-height: 0; overflow: hidden`，滚动交给明确的 viewport。
6. 页面内容在 950×610 窗口中应无需水平滚动，主要操作和标题不得被裁切。

## 页面骨架

页面根容器一律包含 `ecl-page`，主表面包在 `ecl-page__body` 内。三种原型对应的骨架：

```vue
<!-- 单一管理面板：插件、联机等无二级信息架构的页面 -->
<div class="xxx-page ecl-page">
  <section class="ecl-surface xxx-panel">
    <header class="xxx-toolbar">…</header>
    <div class="ecl-page__scroll">…</div>
  </section>
</div>

<!-- 分区管理页：下载、设置等存在三个以上稳定分区的页面 -->
<SectionLayout :title="…" icon="…" :items="…">
  <div class="ecl-page">
    <section class="ecl-surface">
      <div class="ecl-page__scroll">…</div>
    </section>
  </div>
</SectionLayout>

<!-- 沉浸首页：仅游戏页 -->
<div class="game-page ecl-page">
  <PageHeader … />
  <div class="ecl-page__body">…</div>
</div>
```

宽度约束：

- 页面根容器与主表面都撑满内容区，禁止 `max-width` 加 `margin: 0 auto` 居中缩窄。
- 表面内部靠栅格和分栏填充，表单字段过宽时用两列栅格，不恢复居中定宽。
- 同一页面不同标签页共用同一宽度写法，切换时不跳变。

## 控件与密度

- 应用侧栏约 56px；页面内容由应用容器提供外边距。
- `SectionLayout` 左栏为 156px，窄窗口可降至 140px。
- 主表面标题/工具栏通常为 44–72px；仅含文字的标题更靠近 44px。
- 默认按钮 29px，大号按钮 36px；方形操作按钮遵循同档高度。
- 列表、设置项、资源行通常 44–60px。
- 带图标、标题和一行说明的选择条目标高度为 56–64px，图标容器 32–40px。
- 内容区常用 12–16px padding，复杂表单可使用 18–20px；避免无依据的 28px 以上留白。
- 同组间距 6–10px，分组间距 12–16px，页面级间距 16px。

## 常见反模式

- 为两个选项新增页内侧边栏。
- 为整块内容加 `max-width` 居中缩窄，导致同一页面不同标签页宽度不一致。
- 用固定 `height` 撑满窗口，而不是 `flex: 1; min-height: 0`。
- 自建页面根类（`xxx-page`）而不复用 `ecl-page`，把宽度和高度规则重写一遍。
- 外部 `PageHeader` 下再放一张重复标题的大卡片。
- 把普通选择条做成 80–120px 高的 hero 卡片。
- 同时使用强背景、粗边框、大阴影和位移动效。
- 在 feature CSS 中硬编码浅色背景，导致深色或背景图片模式失效。
- 为了“填满空间”放大按钮、图标或空白；管理页应优先保持信息密度。
- 使用全局选择器覆盖 Naive UI，而不是在 scoped feature 根类下用 `:deep()` 精确控制。
