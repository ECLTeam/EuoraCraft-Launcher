# 插件设置空状态居中实施方案

日期：2026-11-14
状态：已实施

## 现象

「插件设置」弹窗在没有任何可配置插件时，右侧详情卡片的空状态（图标 +「选择一个插件以查看配置」）贴着卡片顶部显示，垂直方向留出大片空白；左侧列表的「没有需要配置的插件」位置正常。

复现条件：打开插件设置，且已安装插件里没有任何一个提供设置 schema。截图见对应 Issue / 对话。

## 原因

`PluginSettingsModal.vue` 的详情区结构是：

```
section.plugin-settings-detail            （flex column，overflow hidden）
└── div.plugin-settings-detail-view       （flex:1 + column）
    ├── template v-if="selected"          → header + .plugin-settings-content（content 用 flex:1 撑满）
    └── NEmpty v-else                     → 直接子元素
```

选中插件时，内部 `header` 与 `flex:1` 的 `.plugin-settings-content` 会把高度撑满，所以问题不可见；未选中时 `NEmpty` 是唯一子元素，而 `.plugin-settings-detail-view` 只有 `flex-direction: column`、没有纵向对齐规则，元素便从顶部排列。水平方向因为 `NEmpty` 自身整行居中，看起来只是「没垂直居中」。

同类区域的对照实现：`.plugin-settings-content--state` 已用 `justify-content: center` 处理选中插件后的加载/错误/无可编辑项状态。缺的只是「未选中」这一支。

## 改法

把空状态包进专用容器，由容器承担撑满与居中，不动既有详情布局：

```html
<div v-else class="plugin-settings-empty">
  <NEmpty :description="t('plugins.selectPlugin')" />
</div>
```

```css
.plugin-settings-empty {
  display: flex;
  flex: 1;
  min-height: 0;
  align-items: center;
  justify-content: center;
}
```

选择包容器而不是给 `.plugin-settings-detail-view` 直接加 `justify-content: center`，是因为后者在选中插件时会同时居中 `header + content`，一旦内容不足一屏就会把标题栏推离顶部。

左侧列表的空状态保持原样：它跟在搜索框下方，属于列表区的常规位置，与右侧「整块空面板」的语义不同。

## 验证

- 新增 `PluginSettingsModal.test.ts` 用例：无可用 schema 时 `.plugin-settings-empty` 存在、内含 `.n-empty`、文案为 `plugins.selectPlugin`，且父元素是 `.plugin-settings-detail-view`。12 个用例通过。
- 真实浏览器（showcase，1280×820，详情区高 730px）：把同结构与同 scoped 标记的探针注入 `.plugin-settings-detail`，量得内容上下各 317px、左右各 314px，纵向居中成立。
- `pnpm check` 全量通过（格式、ESLint、设计检查、页面根类、类型、Vitest）。

## 后续待办（项目规划）

本次只修空状态对齐，以下为已确认但未做的项，按需要再排期：

1. **布局皮肤**：等高线波纹背景、药丸形输入框、代码窗口式卡片属于 `data-ui-skin` 新皮肤，会动布局皮肤契约与页面骨架，需单独评审。
2. **焦点指示收尾**：`--glow-focus` 系列已改为 3:1 实色环；folia 皮肤的 `--control-ring`（边框外的装饰光晕）仍为半透明，属次要提示，未逐主题复验。
3. **墨白主题的卡片不透明度**：亮色 0.98 接近实色，透明度滑杆在该主题下观感变化很小，若有用户反馈再单独评估下限。
4. **像素基线**：`test:visual` 仅覆盖 Classic 亮暗两态，新主题未纳入基线；纳入前需重建基线并确认每处差异都是预期改动。
5. **同类空状态排查**：其它弹窗/页面若也用「整块面板 + 单个 NEmpty」的结构，可能存在相同顶对齐，需按实际界面逐个确认后再收敛。
