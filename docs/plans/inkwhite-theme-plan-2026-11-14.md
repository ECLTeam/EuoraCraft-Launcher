# 墨白配色主题实施方案

日期：2026-11-14
状态：已实施

## 目标

为启动器新增一个内置配色主题「墨白」（`inkwhite`）：墨与白两端的中性画布、发丝描边分层、大圆角、近黑药丸 CTA、极淡阴影、无玻璃高光，彩色只保留品牌蓝一支。仅替换配色与表面几何，不改布局皮肤，不影响页面结构。

已确认的三项取舍：

1. 范围＝**配色 + 表面几何**（不动 `data-ui-skin` 布局皮肤，不做等高线波纹背景与代码窗口式卡片）。
2. 默认强调色＝**亮色 `#0a0a0a`、深色 `#3b82f6`**。近黑主色在 `#0a0a0a` 画布上不可见，且现有 `buttonBackground()` 会把浅色主色压暗成中灰才能满足白字 4.5:1，因此深色改用品牌蓝，不改动 `primaryFillTextColor` 契约。
3. 亮色画布＝**`#f4f4f5`**（保留画布与白卡的层次差，不采用纯白）。

## 与现有主题的差异

本主题的中性色与现有 `absolutely` 高度重合（同为 `#0a0a0a` / `#111` / `#262626` 一系），因此辨识度必须由几何承担，方案据此定位：

| 维度 | Absolutely | 墨白 |
| --- | --- | --- |
| 亮色画布 | 中灰 `#e7e7ea` | 浅灰 `#f4f4f5` |
| 卡片圆角 | 10px | 14px |
| 控件圆角 | 8px | 10px |
| 内高光 | 无 | 无 |
| 亮色阴影 | `0 1px 2px` 发丝 | `0 1px 2px` 发丝 + 悬停柔和扩散 |
| 默认主色 | 亮色蓝 / 深色蓝 | 亮色近黑 / 深色蓝 |
| 卡片不透明度（亮/暗） | 0.98 / 0.88 | 0.98 / 0.90（暗色卡片更实，接近 `#111` 实色卡） |

## 色板

`frontend/src/config/inkwhite.ts`（新增）。

亮色（`INKWHITE_LIGHT`）：

| 令牌 | 值 | 来源 |
| --- | --- | --- |
| canvas | `#f4f4f5` | 已确认取舍 |
| surface | `#ffffff` | 卡片白 |
| surfaceMuted | `#fafafa` | 次级底 |
| controlFill / Hover / Active | `#ffffff` / `#f4f4f5` / `#e9e9eb` | 逐级压深 |
| text / secondary / tertiary | `#0a0a0a` / `#52525b` / `#71717a` | 主文字近黑 + zinc 灰阶 |
| border / borderStrong | `#e5e5e5` / `#d4d4d8` | 发丝描边 `#e5e5e5` |
| divider | `#f0f0f0` | 卡片内分隔线 |
| accent | `#0a0a0a` | 近黑 CTA |
| success / warning / error | `#16a34a` / `#d97706` / `#dc2626` | 中性语义色 |

深色（`INKWHITE_DARK`）：

| 令牌 | 值 |
| --- | --- |
| canvas / surface / surfaceMuted | `#0a0a0a` / `#111111` / `#171717` |
| controlFill / Hover / Active | `#1a1a1a` / `#242424` / `#2e2e2e` |
| text / secondary / tertiary | `#fafafa` / `#a1a1aa` / `#71717a` |
| border / borderStrong / divider | `#262626` / `#3f3f46` / `#1f1f1f` |
| accent | `#3b82f6` |
| success / warning / error | `#22c55e` / `#f59e0b` / `#ef4444` |

其余令牌（`badgeFabric` 等）按语义映射：Fabric 取 warning、Forge 取 `mix(textSecondary, accent, 0.55)`、NeoForge 取 accent、Quilt 取 success，与 Absolutely 的处理一致；亮色 Fabric 徽标在近黑主色下仍用暖色，避免四个徽标全变灰。

`surfaceOpacity`：亮 0.98 / 暗 0.90（卡片为实色，但需保留 `--card-opacity` 用户滑杆的缩放空间）；`controlOpacity`：亮 1 / 暗 0.96。

## 表面几何

`frontend/src/config/surface.ts` 新增 `inkwhiteSurfaceStyle(isDark)`：

- `cardRadius: 14`、`controlRadius: 10`
- `borderWidth: '1px'`，`borderColor: var(--ecl-border)`，hover 用 `--ecl-border-strong`
- `shadow`：亮 `0 1px 2px rgba(10, 10, 10, 0.04)`，暗 `none`（深色靠描边分层，与实色卡一致）
- `shadowHover`：亮 `0 6px 20px rgba(10, 10, 10, 0.07)`，暗 `0 6px 20px rgba(0, 0, 0, 0.45)`
- `highlight: 'inset 0 0 0 0 transparent'`（扁平，无玻璃内高光）
- `backdrop: 'none'`（不做背景模糊，避免与纯色画布冲突）
- 控件填充取色板的 `controlFill*` 三态，`borderColorFocus: var(--primary)`

## 主色注册与预设

主色按现有主题的同一条路径注册，不引入新机制：

1. **官方配色作为默认主色**。`setThemeId('inkwhite')` 首次进入时把 `primaryColor` 写成该外观的默认配色（亮 `#0a0a0a` / 暗 `#3b82f6`），与 Absolutely（首次进入改用其电光蓝）、Catppuccin（首次进入改用官方 Mauve）的处理一致。切换后用户已自定义的主色不被覆盖。
2. **亮暗互换**。`syncPaletteAccent` 增加 inkwhite 分支：当前主色等于另一外观的官方色时，换成当前外观的官方色，避免深色下残留近黑导致主按钮不可见。
3. **预设色板自成一套**。`inkwhiteAccentPresets(isDark)` 返回墨白自己的八色，不复用 Absolutely 列表；首项「石墨黑」按当前外观替换为官方色（亮 `#0a0a0a` / 暗 `#3b82f6`），保证设置页选中态与生效主色对得上：

| 名称 | 值 | 说明 |
| --- | --- | --- |
| 石墨黑 | 亮 `#0a0a0a` / 暗 `#3b82f6` | 主操作的近黑，与深色品牌蓝成对 |
| 品牌蓝 | `#2563eb` | 品牌蓝主色 |
| 青蓝 | `#00baff` | 品牌蓝的青色端 |
| 翠绿 | `#10b981` | 语义绿 |
| 琥珀 | `#f59e0b` | 语义黄 |
| 橙红 | `#f97316` | 暖色强调 |
| 玫红 | `#f43f5e` | 冷色对比 |
| 紫 | `#a855f7` | 与代码卡关键字色呼应 |

用户选上述任一预设或自定义颜色后保持生效，切回 inkwhite 也不会被重置；只有当前值仍等于旧官方色时才互换。


## 修改范围

前端（`frontend` 子仓库）：

- `src/config/inkwhite.ts`（新增）：色板、`inkwhitePalette`、`inkwhitePaletteTokens`、`inkwhiteAccentPresets`。
- `src/config/inkwhite.test.ts`（新增）：亮暗色板关键值、`inkwhitePaletteTokens` 的语义映射、预设首项随外观切换。
- `src/config/theme.ts`：`BuiltinThemeId` 联合类型加入 `'inkwhite'`；`BUILTIN_THEMES` 在 `absolutely` 之后插入 `{ id: 'inkwhite', icon: 'terminal', labelKey: 'themeInkwhite' }`（`terminal` → `terminal-2`，已在 `ICON_MAP` 注册，图标校验用例自动覆盖）。
- `src/config/theme.test.ts`：`BUILTIN_THEMES` 期望列表加 `inkwhite`；补 `isBuiltinThemeId('inkwhite')` 与 `layoutSkinOf('inkwhite') === 'classic'`。
- `src/config/surface.ts`：新增 `inkwhiteSurfaceStyle`，在 `surfaceStyleOf` 中分支；`src/config/surface.test.ts` 补圆角、无内高光、深色无静态阴影三处断言。
- `src/composables/useTheme.ts`：`paletteTokensOf` 增加 inkwhite 分支；`accentPresets` 增加 inkwhite 分支；`saveSnapshot` / `updateTheme` 的 `paletteFamily` 与 `dataset.themePalette` 纳入 inkwhite；`updateTheme` 的 `auroraBase` 把 inkwhite 与 absolutely 归入中性灰底（避免近黑主色派生出发闷的光斑）；`syncPaletteAccent` 增加 inkwhite 的亮暗主色互换；`setThemeId` 增加「首次进入 inkwhite 时把主色换为该外观 accent」分支。
- `index.html`：启动画面补 `[data-theme-palette='inkwhite'][data-theme='light'|'dark']` 两组 `--boot-*`，避免挂载前闪默认蓝。
- `src/views/settings/AppearanceTab.vue`：`showEffectSettings` 纳入 inkwhite，并更新该 computed 的注释（现仅写 Absolutely / Catppuccin）。
- 六语言 `settings.themeInkwhite`：zh-CN / zh-TW 用「墨白」，en-US / de-DE / ja-JP / ru-RU 用 `Inkwhite`（沿用 Absolutely 不译的做法）。

后端（主仓库）：

- `ECL/utils/themes.py`：`builtin_theme_ids` 在 `absolutely` 之后加入 `"inkwhite"`，并同步类文档字符串的主题说明。
- `tests/test_themes.py`：期望元组加入 `"inkwhite"`；`normalize_theme_id` 用例补 `inkwhite` 通过、未知值仍回退 `classic`。

文档：

- `frontend/docs/ECL_DESIGN_SYSTEM.md` §3：内置配色列表补墨白。
- `docs/junsi-dev-docs/6-UI/组件设计/外观设置页结构.md`：新增「墨白配色（`inkwhite`）」小节，说明与 Absolutely 的定位差异（几何差异承担辨识度）。

## 不做的事

- 不新增 `data-ui-skin` 布局皮肤，不做等高线波纹背景、药丸形输入框、代码窗口式卡片。这些若要做，需单独评审布局皮肤契约。
- 不改 `primaryFillTextColor` 契约、不改 `buttonBackground()`、不动 Naive UI overrides 结构。
- 不为「亮暗主色不同」增加新机制：复用 Absolutely 已有的「按外观取 accent + 切换时互换」路径。
- 不做存量的焦点环收敛（见下）。

## 已知风险

1. **亮色焦点环偏弱**：`--ecl-focus-ring` 由 `rgba(var(--primary-rgb), 0.28)` 派生，近黑主色下白底约为 `#bababa`，对比度约 1.9:1，低于 WCAG 2.2 焦点外观的 3:1。这是全项目共用公式的存量问题（默认主色约 1.35:1），本次不扩大范围修改；若要修，需把 `--ecl-focus-ring` 纳入主题调色板变量并重新验证所有主题，属独立评审项。
2. **深色主色不是近黑**：已在取舍阶段确认，代价是亮暗两态的主色语义不同（黑 CTA / 蓝 CTA）。
3. **圆角变化会影响用户已调过滑杆的情况**：`setThemeId` 的 `releaseThemeRadius` 已按「等于旧主题预设才释放」处理，14/10 与 Absolutely 的 10/8 不同，切换时不会误清用户自定义值。

## 验证

- 前端：`pnpm check`（prettier → eslint → check:design → check:page-root → vue-tsc → vitest）。
- 后端：`ruff check`、`ruff format --check`，`pytest tests/test_themes.py`。
- 浏览器：`pnpm dev` 在 960×600 与放大窗口下查看「外观设置」主题下拉与实例页，验证 inkwhite × 亮/暗两态，以及卡片透明度 80%、开启背景模糊、减少动效三种代表状态；重点看 14px 圆角下的卡片嵌套、发丝描边在浅灰画布上的可辨性、深色下近黑卡片与 `#0a0a0a` 画布的层次。
- 不做像素基线更新（`test:visual` 基线只覆盖 Classic 亮暗两态，本次不涉及）。

## 实施顺序

1. 新增 `inkwhite.ts` 与单测 → 2. 接 `theme.ts` / `surface.ts` / `useTheme.ts` → 3. `index.html` 启动色 → 4. 设置页与六语言 → 5. 后端白名单与测试 → 6. 文档 → 7. 全量检查与浏览器验收。

## 实施结果

方案遗漏的两处主题标识清单，在类型检查阶段发现并补齐：

- `frontend/src/types/config.ts`：`ThemeConfig.theme_id` 是独立维护的联合类型，需加入 `'inkwhite'`，否则新建配置无法写入该主题。
- `frontend/src/features/settings/model/settingsSnapshot.ts`：`themeIds` 是 valibot `picklist`，缺失时校验会丢弃 inkwhite 的 `theme_id`，导致重启后回退经典皮肤。

新增 `frontend/src/composables/useTheme.inkwhite.test.ts` 覆盖注册路径：`data-theme-palette='inkwhite'`、14/10 圆角、默认主色按外观注册、亮暗换档、自选色不被覆盖、中性光斑与切回经典后的变量清理。

验证记录：

- 前端 `pnpm check`（format / eslint / check:design / check:page-root / vue-tsc / vitest）通过：209 个测试文件、1288 个用例通过、1 个 todo。
- 后端 `ruff check`、`ruff format --check` 通过；`pytest tests/test_themes.py` 2 个用例通过。
- 浏览器（showcase 模式，1280×820）实测：主题下拉出现「墨白」；切到该主题后 `data-theme-palette=inkwhite`、`--ecl-card-radius=14px`、`--ecl-control-radius=10px`、`--ecl-canvas=#f4f4f5`、`--ecl-primary=#0a0a0a`；切换深色后 `--ecl-canvas=#0a0a0a`、`--ecl-text=#fafafa`、`--ecl-border=#262626`、`--ecl-primary` 自动换成 `#3b82f6`，光斑保持中性。设置页在近黑主色下卡片、描边、开关与选中态均清晰可读；主界面的「启动实例」为黑色胶囊按钮，符合近黑主色的预期。
- 未做像素基线更新（`test:visual` 只覆盖 Classic 亮暗两态）。

