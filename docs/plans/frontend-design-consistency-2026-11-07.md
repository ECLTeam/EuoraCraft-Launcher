# 前端视觉一致性治理实施方案

- 日期：2026-11-07
- 范围：主仓库 `AGENTS.md` 前端章节、`.skills/` 迁移、`frontend/docs/ECL_DESIGN_SYSTEM.md`、`frontend/src/styles/design-system.css`、3 个设置页样式、`Plugins.vue` 根类
- 已确认决策：修复规范投递链路（B）+ 建立页面骨架范例（A）；页面宽度契约取「一律撑满内容区」
- 不在本次范围：扩展 `design-style-rules.mjs` 几何规则、Playwright 视觉回归（列为后续阶段 C）

## 1. 问题与证据

Agent 修改前端时反复自造样式、忽略宽度，不是模型偏好问题，是三条链路同时断开。

### 1.1 规范送不到

- 主仓库 `AGENTS.md` 共 349 行，前端章节在第 4 章；注入上下文时在 4.2 节被截断，测试分级与交付规则丢失。
- `frontend/AGENTS.md`、`ECL_DESIGN_SYSTEM.md`、`tokens.css` 均不在自动注入范围，需要主动读取；而「必须先读设计规范」这句提示本身位于被截断区域内。
- 对照 `Wuchang325-Blog`：`AGENTS.md` 仅 58 行，只放必读清单、红线与验证命令，正文在 `DESIGN.md`（28KB）与 `rules/`、`docs/` 中按需读取。

### 1.2 已写好的技能不在可发现路径

- `.skills/maintain-euoracraft-frontend/SKILL.md` 内容完整：包含页面原型分类、密度约束、决策优先级、常见反模式；`references/style-system.md` 列出 8 个基准页面及各自应继承的模式。
- 但 `.skills/` 既不是 agentskills.io 标准路径（`.agents/skills/`），也不是本环境的项目技能路径（`.workbuddy-ai/skills/`）。当前会话可用技能列表中不含该技能，说明从未被自动加载。
- 对照博客：使用 `.agents/skills/`，并在 `AGENTS.md` 的 Required documents 中逐条点名。

### 1.3 设计系统只有词表，没有范例

- `design-system.css` 定义了 `.ecl-page`、`.ecl-page-header`、`.ecl-surface`、`.ecl-toolbar`。
- `.ecl-page` 全仓仅 2 个 `.vue` 文件引用；17 个顶层视图全部未使用，各自建立根类：

    | 视图                 | 根类                           |
    | -------------------- | ------------------------------ |
    | `Download.vue`       | `download-page`                |
    | `Game.vue`           | `game-page`                    |
    | `Plugins.vue`        | `plugins-page`                 |
    | `ConnectRoomTab.vue` | `connect-page workspace-page`  |
    | `InstancesTab.vue`   | `install-page`                 |
    | `ManageTab.vue`      | `manage-page`                  |
    | `AppearanceTab.vue`  | `tab-pane appearance-settings` |

- `components/layout/PageHeader.vue` 实现完整，但全仓引用数为 0，属于未被接线的死代码。
- 对照博客：`src/layouts/` 是唯一页面模板层，`atoms/manifest.json` 是组件清单单一真源。

### 1.4 宽度契约在存量里互相矛盾

设置页 5 个 tab 的主表面宽度写法：

| 文件                | 当前写法                                         | 是否符合「撑满」 |
| ------------------- | ------------------------------------------------ | ---------------- |
| `AppearanceTab.css` | `max-width: 820px; margin: 0 auto; height: auto` | 否               |
| `GameTab.css`       | `max-width: 820px; margin: 0 auto; height: auto` | 否               |
| `LauncherTab.css`   | `max-width: 820px; height: auto`                 | 否               |
| `AboutTab.css`      | 无宽度限制                                       | 是               |
| `JavaTab.css`       | `width: 100%; height: 100%`                      | 是               |

同一页面内 3 个 tab 居中定宽、2 个 tab 撑满，这是「格格不入」最直接的来源。`Connect.css` 另有 `max-width: 720px` / `820px` 居中列，`InstanceDetailModal.css` 为 `820px`，属同一写法的复制扩散。

### 1.5 自动闸门不覆盖布局

- `scripts/design-style-rules.mjs` 第 28 行规则集固定为 `literal-color`、`literal-motion`、`keyframe-owner`、`important-owner`。
- `width`、`max-width`、`height`、`padding`、`flex` 不在检查范围，`frontend-style-checks.md` 明确说明几何数值不做机械禁用。因此 `max-width: 820px` 可以干净通过 `pnpm check`。
- 前端无 Playwright 视觉回归；vitest 运行在 jsdom，不计算真实布局，布局类偏差在 CI 中全部漏过。

## 2. 宽度契约的定义

「一律撑满内容区」约束的是**主表面**，不是表面内部的表单字段：

1. 页面根容器高度与宽度均撑满扣除标题、工具栏后的内容区，不因内容少而收缩。
2. 页面主表面（`.ecl-surface`）撑满页面根容器，不使用 `max-width` 居中缩窄。
3. 表面内部的表单和内容通过栅格、分栏或合理控件宽度填充可用空间；**不允许**用「整块内容居中定宽」的方式制造留白。
4. 滚动交给明确的 viewport 容器，不用固定高度或 `overflow: hidden` 掩盖布局问题。

对应到具体页面：`AppearanceTab`、`GameTab`、`LauncherTab` 去掉 `max-width` 与 `margin: 0 auto`，改为撑满，与 `JavaTab` 对齐；`Connect.css` 的 720/820 居中列在第二批改为「主表面撑满 + 内部栅格」，不保留居中定宽列。

## 3. 改动点

### 3.1 迁移前端技能到标准路径（B1）

- 移动 `.skills/maintain-euoracraft-frontend/` → `.agents/skills/maintain-euoracraft-frontend/`，保持目录内 `SKILL.md`、`agents/`、`references/` 结构不变。
- 同步更新仓库内对 `.skills/` 的引用（当前检索结果为 0 处，无需改动其他文件）。
- 另外两个技能 `euoracraft-agent-workflow`、`maintain-euoracraft-backend` 一并迁移，避免目录内出现两套路径。

理由：`.agents/skills/` 是 agentskills.io 标准，与已验证有效的博客项目一致，且可被 Claude Code、Codex、Cursor 等按任务自动发现。

### 3.2 主仓库 AGENTS.md 前端章节瘦身（B2）

目标：把前端细则移入 `frontend/`，主仓库只留红线与指针，保证注入时不被截断。

- 第 4 章从约 60 行压缩到 12 行以内，只保留：
    - 改前端前必读：`frontend/AGENTS.md`、`frontend/docs/ECL_DESIGN_SYSTEM.md`、`.agents/skills/maintain-euoracraft-frontend/SKILL.md`（显式路径，不依赖自动发现）
    - 四条红线：不自建页面根类、主表面撑满不用 `max-width` 居中、颜色与间距走现有令牌、共性问题在公共层修
    - 指向第 6 节测试分级，不重复内容
- 把 4.1 单卡片布局、4.2 公共样式与组件等细则原文迁入 `frontend/docs/ECL_DESIGN_SYSTEM.md` 对应章节，内容不丢失。
- 全文目标 200 行以内。

### 3.3 补齐页面骨架契约（A1）

`frontend/src/styles/design-system.css` 新增两类内容，不改动已有类语义：

- `.ecl-page__body`：`flex: 1; min-height: 0; min-width: 0; overflow: hidden`，用于承载撑满的主表面。
- `.ecl-page__scroll`：`flex: 1; min-height: 0; overflow: auto`，用于内容超出时滚动。
- 文件顶部补一段注释说明三种页面原型的骨架组合方式（单表面 / 分区栏 + 单表面 / 沉浸页），作为可直接复制的范例。

### 3.4 接通 PageHeader 与页面根类（A2）

- `Plugins.vue`：根类 `plugins-page` → `plugins-page ecl-page`，`.plugins-panel` 外层补 `.ecl-page__body`，样式侧删除 `.plugins-page` 中与 `.ecl-page` 重复的 `display/width/height/min-height` 声明。信息架构不变，不新增页面标题。
- 其余视图的根类迁移**不在此次批量进行**，按第 2 节「存量随功能变更收敛」原则，在各自功能变更时逐个替换。本次在 `ECL_DESIGN_SYSTEM.md` 中登记目标根类清单，作为后续收敛依据。

### 3.5 设置页宽度收敛（A3）

| 文件                                          | 改动                                                                                                                 |
| --------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `src/styles/views/settings/AppearanceTab.css` | `.appearance-settings` 删除 `max-width: 820px`、`margin: 0 auto`、`height: auto`，改为 `height: 100%; min-height: 0` |
| `src/styles/views/settings/GameTab.css`       | `.tab-pane` 同上，并把该文件内 `.tab-pane` 规则改为仅保留本页差异（避免与 `common.css` 的 `.tab-pane` 重复定义）     |
| `src/styles/views/settings/LauncherTab.css`   | `.launcher-settings` 删除 `max-width: 820px`、`height: auto`                                                         |

三个 tab 改动后需检查：内容在 960×600 窗口下不出现水平滚动，表单字段不被拉伸到不可读宽度（必要时用两列栅格，而不是恢复居中定宽）。

### 3.6 技能内容补充（B3）

在 `.agents/skills/maintain-euoracraft-frontend/SKILL.md` 的「页面原型」章节后新增「宽度契约」小节，写明第 2 节的四条规则与三个设置页的对照写法；`references/style-system.md` 的「常见反模式」补充：

- 为整块内容加 `max-width` 居中缩窄，导致同一页面内不同 tab 宽度不一致。
- 用固定 `height` 撑满窗口，而非 `flex: 1; min-height: 0`。

验收命令补充真实浏览器步骤：`pnpm dev` 后在 960×600 与默认窗口下逐页查看，不依赖 jsdom 单测。

## 4. 实施步骤

1. 迁移 `.skills/` → `.agents/skills/`，确认三个技能目录完整。
2. 改写主仓库 `AGENTS.md` 第 4 章，把细则原文迁入 `frontend/docs/ECL_DESIGN_SYSTEM.md`。
3. `design-system.css` 补 `.ecl-page__body`、`.ecl-page__scroll` 与骨架注释。
4. 改 `Plugins.vue` 根类与 `Plugins.css` 重复声明。
5. 改三个设置页宽度，逐页在浏览器核对。
6. 更新 `SKILL.md` 与 `references/style-system.md`。
7. 在 `frontend/docs/ECL_DESIGN_SYSTEM.md` 登记「目标根类清单」，作为后续收敛依据。

## 5. 测试与验收

- `frontend/` 下执行 `pnpm check`（格式、ESLint、设计检查、类型、Vitest 全量）。
- 设置页与插件页在 `pnpm dev` 中逐页查看：960×600、默认窗口、最大化三种尺寸；Classic/Folia × 亮暗；含背景图片时表面可读性。
- 重点核对：3 个设置 tab 主表面左右边界一致、无水平滚动、表单字段宽度合理、列表末项可达。
- 若 `pnpm check` 因样式改动产生设计检查命中，按 `frontend-style-checks.md` 第 4 节登记精确例外，不扩大主题源白名单。
- 主仓库与 `frontend` 子仓库分别提交，使用中文 Conventional Commits，先集成前端本地 main 再更新主仓库指针。**不推送远端、不发布。**

## 6. 风险与回退

| 风险                               | 处理                                                        |
| ---------------------------------- | ----------------------------------------------------------- |
| 设置页撑满后表单字段过宽影响可读性 | 用两列栅格填充，不恢复居中定宽；改动可单独回退              |
| 迁移 `.skills/` 导致某工具链失效   | 迁移为目录移动，无内容改写；回退只需移回原路径              |
| `AGENTS.md` 瘦身造成细则丢失       | 细则原文整体迁入 `ECL_DESIGN_SYSTEM.md`，逐条核对不删内容   |
| 注入长度仍超限                     | 瘦身后按实际注入结果复核，必要时继续把第 5—9 节拆到 `docs/` |

## 7. 后续阶段

- **C1（已完成 2026-11-07）**：`design-style-rules.mjs` 新增 `literal-geometry` 规则，拦截业务样式新增的固定像素 `max-width`、`min-width`、`max-height`。
- **C2（已完成 2026-11-07）**：新增 `check-page-root-class.mjs`，全量核对 `src/views/**` 根元素必须落到公共骨架，并以待迁移清单管理存量。
- **C3（已完成 2026-11-07）**：`tests/browser/test_visual.py` 建立四个基准页面 × Classic 亮暗共 8 张视觉回归基线。
- **C4（已完成 2026-11-07）**：`Connect.css` 的 720px/820px 居中定宽列改为自适应栅格；`InstanceDetailModal.css` 的 820px 上限移除。

## 8. 实施记录（2026-11-07）

已按第 3 节完成，改动清单：

| 文件                                                                     | 改动                                                                                                                                    |
| ------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------- |
| `.gitignore`                                                             | `.agents/` 改为 `.agents/*` 加 `!.agents/skills/`，移除已失效的 `.skills/`；技能目录由本地文件转为随仓库分发（共 14 个文件）            |
| `.agents/skills/*`                                                       | 由 `.skills/` 迁入；三个 SKILL.md 的项目命名统一为 `EuoraCraft-Launcher` / `ECL启动器`；前端技能新增「宽度契约」小节与浏览器验收步骤    |
| `.agents/skills/maintain-euoracraft-frontend/references/style-system.md` | 新增「页面骨架」代码范例与三条反模式                                                                                                    |
| `AGENTS.md`                                                              | 第 4 章由 66 行压到 12 行，只保留阅读顺序与四条视觉红线；全文 349 → 298 行                                                              |
| `frontend/AGENTS.md`                                                     | 承接原 4.4—4.9 的行为要求，新增界面状态、异步响应、国际化、类型边界、键盘焦点、列表身份六节                                             |
| `frontend/docs/ECL_DESIGN_SYSTEM.md`                                     | 第 5 章扩为 5.1—5.5，承接原 4.1—4.3 与主题验证细则；新增第 8 章「页面根类收敛清单」                                                     |
| `frontend/src/styles/design-system.css`                                  | 新增 `.ecl-page__body`、`.ecl-page__scroll`，文件顶部补三种页面原型的骨架注释                                                           |
| `frontend/src/views/Plugins.vue`                                         | 根类追加 `ecl-page`，主表面追加 `ecl-page__body`                                                                                        |
| `frontend/src/styles/views/Plugins.css`                                  | 删除与 `.ecl-page` / `.ecl-page__body` 重复的尺寸声明                                                                                   |
| `AppearanceTab.css`、`GameTab.css`、`LauncherTab.css`                    | 去掉 `max-width: 820px` 与 `margin: 0 auto`，改为 `min-height: 100%` 撑满；`GameTab.css` 的裸 `.tab-pane` 选择器收敛为 `.game-settings` |

验证结果：

- `pnpm check` 通过：格式、ESLint、设计检查、类型、Vitest 全绿（212 个测试文件、1363 个用例通过，1 个 todo）。
- `pnpm check:design`：解析 185 个文件、变更 6 个、新增命中 0、有效例外 0。
- 真实浏览器（showcase 模式）实测主表面宽度，四种设置页与插件页均等于内容区宽度，无水平滚动：

| 页面       | 960×600 主表面 / 内容区 | 1280×800 主表面 / 内容区 |
| ---------- | ----------------------- | ------------------------ |
| 外观设置   | 696 / 696               | 1016 / 1016              |
| 游戏设置   | 696 / 696               | 1016 / 1016              |
| 启动器设置 | 696 / 696               | 1016 / 1016              |
| Java 管理  | 696 / 696               | 1016 / 1016              |
| 插件管理   | 696 / 696               | 1016 / 1016              |

修复前在 1280 宽度下三个设置页被限制在 820px 并居中，与撑满的 Java 管理页并列时宽度跳变；现四页一致。

遗留与说明：

- `AGENTS.md` 全文为 298 行，未达第 3.2 节写的 200 行目标。第 4 章已进入可注入区间，但第 5—9 节（日志、测试、Git、发布）可能仍被截断。进一步压缩需要拆分第 0—3 章，超出本次授权范围，留作后续。
- `frontend` 工作区存在一处非本次改动：`MAPPING_TABLE.yaml` 被删除（210 行），来源未知，未做恢复也未提交，交付前需确认。
- 浏览器验收时「调试工具」浮动面板会遮挡页面顶部工具栏，属既有行为，与本次改动无关。

## 9. 实施记录：静态闸门（2026-11-07 第二轮）

### 9.1 删除 MAPPING_TABLE.yaml

该文件是主题系统重构的一次性迁移记录（`removal-map`），全仓无代码引用；ADR-009 曾要求新令牌映射在此登记，但主题系统已按 ADR-012 移除，该要求随之失效。历史上多个任务刻意绕过它的删除，因此该删除在工作区搁置多日。已确认删除，并清理 `.prettierignore` 中失效的条目。

### 9.2 C1：literal-geometry

`scripts/design-style-rules.mjs` 新增第五条规则，判定条件：

- 属性限于 `max-width`、`min-width`、`max-height`；
- 值必须是纯像素长度且不为零；
- 主题来源（`base.css`、`tokens.css`、`folia.css`、`main.css`）的合法变量定义仍豁免，普通声明不豁免。

刻意不覆盖 `width`、`height`、`min-height`：图标几何、细边框和布局基础值大量使用固定像素，纳入会产生大量噪音；`max-width` 加 `margin: 0 auto` 才是「居中定宽列」的实际形态。`var()`、`calc()`、`min()` 等复合值与 `@media`、`@container` 断点参数不在判定范围。

### 9.3 C2：页面根类检查

新增 `scripts/check-page-root-class.mjs` 与 `pnpm check:page-root`，已并入 `pnpm check`。

- 判定：根元素 class 含 `ecl-page` 或 `tab-pane`；根元素是 `SectionLayout`、`FullscreenModal`、`Modal`、`ConfirmDialog`、`ErrorModal` 时由布局壳负责骨架。
- 动态 `:class`（数组、变量、含插值的模板字符串）无法静态判定，一律按不符合处理，防止用拼接绕过。
- 与增量样式检查不同，本检查每次核对全量视图，否则存量违规永远不会再次触发。
- 存量通过 `pendingMigration` 清单管理，共 8 个视图；清单条目修复后不删除会导致检查失败，清单外出现新不符合项同样失败。

新增用例：`src/__tests__/pageRoots.test.mjs`（6 个用例，含一条全量清单一致性守卫）；`src/__tests__/designStyles.test.mjs` 增加 3 个 `literal-geometry` 用例。

### 9.4 顺带修复

`AboutTab.vue` 根类补 `tab-pane`，成为第五个与其它设置页一致的标签页；`AboutTab.css` 相应改为 `height: auto; min-height: 100%` 并保留原有 `gap: 14px`，不改动既有间距节奏。

### 9.5 验证

- `pnpm check` 通过：213 个测试文件、1372 个用例通过（1 个 todo）。
- `pnpm check:design`：解析 185 个文件、变更 8 个、新增命中 0、有效例外 0。
- `pnpm check:page-root`：解析 17 个视图、不符合 8（全部在待迁移清单）、清单外新增 0、过期条目 0。
- `AboutTab` 浏览器实测：主表面 696/696（960×600）与 1016/1016（1280×800），`viewportScrollHeight` 等于内容高度，滚动正常、无裁切、无水平滚动。

### 9.6 仍待处理

- 8 个待迁移视图的根类收敛（C2 清单），随各自功能变更推进。按主仓库第 2 节「存量随功能变更收敛」原则，不做批量替换。

## 10. 实施记录：视觉回归与定宽收敛（2026-11-07 第三轮）

### 10.1 C4：Connect 与实例详情弹窗

- `Connect.css` 的 `.connect-idle-primary` 由 `max-width: 820px` 居中列改为 `grid-template-columns: repeat(auto-fit, minmax(min(100%, 420px), 1fr))`：单张卡片时铺满，两张时并排，窄窗口回落单列，无需媒体查询。实测 1280 宽度下内容区 1016、容器 1011、卡片 500×2；960 宽度下容器 691、单列。
- `.connect-scroll-area > .connect-room-card` 移除 `max-width: 720px` 与 `margin: 14px auto`。
- `InstanceDetailModal.css` 的 `.vdm-page` 移除 `max-width: 820px` 与 `margin: 0 auto`。实测 1280 下页面区由 820 变为 1040，960 下为 720，均撑满弹窗可用宽度。
- 保留 `.connect-state-content` 的 `max-width: 480px`：它是状态卡内部的居中提示内容，属于内容而非主表面。

### 10.2 C3：视觉回归基线

新增 `tests/browser/test_visual.py` 与 `pnpm test:visual`，复用 `test_accessibility.py` 的 showcase 启动与主题注入，避免两套 harness 分叉。

- 场景：`settings-appearance`、`plugins`、`room`、`instance-detail`，对应四种页面原型。
- 主题：Classic 亮暗两态，视口固定 1280×800，共 8 张基线。
- 确定性：拦截外部网络、等待字体与 `data-theme`/`data-ui-skin` 就绪、注入样式关闭动画与过渡、隐藏调试工具浮层。容差为单通道差值 8、变化像素占比上限 0.1%。
- 基线存放在 `tests/browser/visual-baselines/`，运行产物 `tests/browser/visual-output/` 已加入 `.gitignore`。

### 10.3 验证

- 基线确定性：连续两次比对，8 张全部 0 像素变化。
- 基线有效性：临时把 `max-width: 820px; margin: 0 auto` 加回 `.connect-idle-primary`，`room-classic-light` 变化 13.40%、`room-classic-dark` 变化 18.84%，其余 6 张保持 0；撤销后恢复全通过。证明基线不是空跑，且定位精确到改动页面。
- `pnpm check` 通过，设计检查与页面根类检查均无新增命中。

### 10.4 说明

- 视觉回归未并入 `pnpm check`：基线平台相关，且需要 Python Playwright 与浏览器，纳入主检查会让无浏览器环境的 CI 直接失败。它是布局改动的独立验收命令。
- 基线在 Windows + Microsoft Edge 下生成；其他平台比对失败属预期，须复核差异后重建。

## 11. 实施记录：公共 Ui 单一入口（2026-11-07 第四轮）

### 11.1 问题

`src/components/ui/` 有 13 个控件实现，但没有对外入口。调用方直接引用 `@/components/ui/X.vue` 共 151 处、分布在 90 个文件。目录里放着实现文件，使「在这里加一个控件」成为默认动作，控件清单也没有单一可读位置。

### 11.2 改动

- 新增 `src/components/ui/index.ts` 作为唯一对外入口，显式导出 13 个控件：`CollapsiblePanel`、`MarkdownContent`、`UiAvatar`、`UiButton`、`UiCard`、`UiEmptyState`、`UiIcon`、`UiInput`、`UiLoading`、`UiProgress`、`UiSelect`、`UiSlider`、`UiTag`。导出名与既有调用方别名一致，改写不改动本地变量名。
- 89 个文件的 150 处深层引用合并为 `import { … } from '@/components/ui'`。
- `components/ui/Card.vue` 对 `Icon.vue` 的引用保留直接路径：走 barrel 会形成 `index.ts → Card.vue → index.ts` 的循环。
- 新增 ESLint 规则 `ecl-design/ui-entry`，作用于 `components/ui/` 之外的生产源码，拦截 `@/components/ui/` 下的深层引用（含别名、动态导入、重导出、require）。测试文件与 `components/ui/` 内部不受限制。
- 一次性改写脚本用后即删，不留可重复执行的迁移工具。

### 11.3 验证

- `pnpm check` 通过：213 个测试文件、1381 个用例（1 个 todo）。
- `pnpm lint` 无告警；`ui-entry` 用例由 18 个增至 27 个，覆盖深层引用、barrel 导入、`components/ui/` 内部例外。
- **视觉回归 8 张全部 0 像素变化**：导入方式改变不影响任何渲染结果。
- `pnpm check:design` 变更 89 个文件、新增命中 0；`pnpm check:page-root` 无新增、无过期。

### 11.4 说明

- 这是一次跨 89 个文件的机械改写，没有功能变更。改写的必要性在于建立单一入口这一结构事实；是否值得由评审确认。
- 规则只检查调用方是否绕过入口，不检查 `index.ts` 清单是否与实际文件一致。新增控件时清单遗漏不会被拦截。
- 目录结构未拆分独立包：项目不是 pnpm workspace，为单一入口引入 workspace 包会改变构建与发布链路，超出本次范围。

## 12. 本地提交与插件迁移集成（2026-11-08）

用户已明确要求提交上述现存样式改动。本轮核对原工作区后，先对样式状态执行完整 `pnpm check`（1403 通过、1 todo）、生产构建及真实 Windows Edge 视觉回归（8 张全部 0 像素变化），前端样式提交为 `c19e214`。

项目技能实际目录名为 `ecl-agent-workflow`、`maintain-ecl-backend`、`maintain-ecl-frontend`；同步修正迁移后的技能入口、调用提示和脚本路径，主规范不再引用已删除的前端小节。

后续将已提交的外置插件分支合入本地 main，保留本样式提交。旧联机组件的修改/删除冲突按完整外置方案处理：原模板和样式已迁入插件，宿主删除旧组件；旧联机视图也从页面根类迁移清单移除。前端浏览器视觉回归保留宿主拥有的三个页面、六张基线，插件页面由真实宿主加载和禁用测试验收。只做本地集成，不推送、不发布。
