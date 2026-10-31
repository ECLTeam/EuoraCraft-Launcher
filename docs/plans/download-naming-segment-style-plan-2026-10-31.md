# 下载「保存名称」分段控件样式修复方案

日期：2026-10-31
范围：`frontend/src/components/tools/CustomDownloadCard.vue`（工具页 → 自定义下载 → 下载选项 → 保存名称）

## 1. 现象

截图（962×602）实测：未选中段「自定义名称」的描边像素值为 `#e7e8e9`，与卡片背景 `#e7e8e9` 完全一致，即轮廓不可见；未选中段底色为纯白 `#ffffff`，比卡片亮一档，看起来像一块贴上去的白板。选中段「原文件名」为 `--primary` 描边 + 淡紫底，对比强烈。两段之间只有 1px 分隔线，未选中侧与描边同色，因此只有选中项右侧冒出一条紫线。

结果：控件看起来是「一个紫描边药丸 + 一块无边界白板」，而不是一个完整的分段控件。

补充反馈（同一次修复）：两段文字都不在按钮的垂直中心，而是明显贴上边缘。浏览器实测（`showcase` 模式、1100×820 视口）：

| 测量项 | 修复前 |
| --- | --- |
| 按钮盒 | `top=363.59`，`height=29`，中心 378.09 |
| 文字行盒 | `top=366.59`，`height=13`，中心 373.09 |
| 偏差 | 文字比按钮中心高 5px |

原因是 `CustomDownloadCard.vue` 里 `:deep(.n-radio-button .n-radio__label)` 覆盖了 `line-height: 17px`。`.n-radio__label` 是块级元素，块级子元素不受父级 `line-height: var(--n-height)` 影响，直接贴在内容区顶部；Naive 原本靠按钮行高完成的垂直居中因此失效。

## 2. 根因

`CustomDownloadCard.vue` 的 `namingTheme` 把 Naive UI 的 radio-button 主题变量直接映射到低对比度令牌：

| 变量 | 原取值 | 叠在白色控件上的实际值 |
| --- | --- | --- |
| `buttonBorderColor` | `--control-border` → `--ecl-control-border-color` → `rgba(29,36,51,0.1)` | ≈ `#e8e9eb` |
| `buttonColor` | `--control-bg`（`#fff`） | `#ffffff` |

`rgba(29,36,51,0.1)` 适合输入框那种大块面积的柔和描边，但分段控件只有 1px 轮廓和 1px 分隔线，且底色是纯白、卡片背景是半透明白，两者亮度接近，轮廓因此消失。

## 3. 选定方案

标准分段控件（用户已选）：保留 `NRadioGroup` / `NRadioButton` 的语义与结构，只提高轮廓对比度并把控件高度对齐到 32px。不新增抽象、不改全局 Radio 主题。

## 4. 改动点

仅修改 `namingTheme` 三个值，其余保持不变：

```diff
 const namingTheme = {
-  buttonHeightMedium: '29px',
+  buttonHeightMedium: 'var(--ecl-control-height-sm, 32px)',
   fontSizeMedium: '12px',
   buttonBorderRadius: 'var(--r-sm)',
-  buttonColor: 'var(--control-bg)',
+  buttonColor: 'var(--ecl-control-fill)',
   buttonColorActive: 'var(--primary-alpha)',
-  buttonBorderColor: 'var(--control-border)',
+  buttonBorderColor: 'var(--border-strong)',
   buttonBorderColorActive: 'var(--primary)',
   buttonBorderColorHover: 'var(--control-border-hover)',
   buttonTextColor: 'var(--text-primary)',
   buttonTextColorActive: 'var(--primary)',
   buttonTextColorHover: 'var(--text-primary)',
   buttonBoxShadowFocus: 'var(--control-ring)',
 }
```

- `buttonBorderColor` 改用 `--border-strong`：浅色 `#d0d0d0`、深色 `rgba(255,255,255,0.12)`、Folia `rgba(29,36,51,0.16)`，在三种主题下都能在白底上留下可见的 1px 轮廓与分隔线。
- `buttonColor` 改用 `--ecl-control-fill`：与同一张卡片里的 `UiInput` 同源（Folia 下是半透明填充），避免分段控件比输入框更「白」。
- `buttonHeightMedium` 用 `--ecl-control-height-sm` 令牌而不是写死 32px，跟随主题与外观设置。
- 选中段仍为 `--primary` 描边 + `--primary-alpha` 底，与侧栏选中项一致。

文字居中在 scoped 样式里补齐：

```diff
+.custom-download-naming :deep(.n-radio-button) {
+  display: inline-flex;
+  align-items: center;
+  justify-content: center;
+}
 .custom-download-naming :deep(.n-radio-button .n-radio__label) {
   white-space: normal;
   line-height: 17px;
 }
```

保留 `line-height: 17px` 是为了让窄容器下换行时行距仍紧凑（两行 34px，溢出对称）；改用 flex 居中后，单行与换行都以按钮中心对齐，不再依赖 Naive 的行高居中。

## 5. 影响面

- 只影响本卡片的「保存名称」切换。
- `views/Plugins.vue` 的筛选分段、`PluginFileRecoveryModal.vue` 的目录选择、`AppearanceTab.css` 的亮暗模式切换均使用各自的配置，不受影响。
- 组件测试 `CustomDownloadCard.test.ts` 断言的是 `role="radiogroup"`、`aria-labelledby` 与 radio input 的选中/禁用行为，DOM 结构不变，预期全部通过。

## 6. 验证结果

按 `AGENTS.md` 6.1「单页或单组件的局部样式修复」执行定向验证，未运行全量 Vitest。

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 格式 | `pnpm exec prettier --check src/components/tools/CustomDownloadCard.vue` | 通过 |
| ESLint | `pnpm exec eslint src/components/tools/CustomDownloadCard.vue` | 通过 |
| 类型 | `pnpm typecheck` | 通过 |
| 组件测试 | `pnpm exec vitest run src/components/tools/CustomDownloadCard.test.ts` | 15/15 通过 |

真实浏览器验收（`vite --mode showcase`，Edge headless + CDP，1100×820 与 720×820 视口）：

- 两段高度均为 32px，文字行盒中心与按钮中心完全一致（均为 379.59），修复前偏差 5px。
- 未选中段描边计算值为 `rgb(208,208,208)`，在浅色卡片上可见；两段分隔线清晰。
- Classic 亮色、Classic 深色、Folia 亮色三种状态截图确认观感一致，未选中段均保留可见轮廓。
- 720px 窗口下两段文字仍为单行（行盒 17px），无换行、无裁切。

尚未完成：真实启动器内的验收（下载进行中的禁用态、卡片透明度 60% 与壁纸背景叠加）需要在启动器中人工确认；本次浏览器验证使用 showcase 数据，未连接后端。

## 7. 不纳入本次

- **焦点环**：Classic 皮肤下 `--control-ring` 为 `none`，本卡片的单选、复选框和「下载选项」折叠标题都依赖它，键盘聚焦时没有可见指示。`Input.css`、`ToolOptions.css` 同样引用该变量，属全站既有问题，需要单独统一到 `--ecl-focus-ring`，本次不改，避免顺手扩散改动范围。
- **问题登记**：本机未安装 `gh`，GitHub 连接器需要重新授权，暂时无法创建 Issue，待授权后补登记。
