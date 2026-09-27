# 插件页并入「更多」实施方案

> 状态：待确认（尚未实施）
> 日期：2026-09-27
> 已确认决策：页内菜单顺序为**联机 · 插件 · 工具**；侧边栏**不**为「更多」加二级菜单；插件页工具栏**去掉**「插件」标题；旧 `/plugins` **重定向**到 `/more/plugins`。

## 1. 现状调研

| 项 | 现状 | 位置 |
| --- | --- | --- |
| 插件页 | 独立叶子路由，253 行；自带工具栏卡片（标题 + 搜索 + 筛选 + 安装）与插件列表；未使用 `SectionLayout` | `frontend/src/views/Plugins.vue` |
| 路由 | `{ path: '/plugins', name: 'plugins', component: withErrorBoundary(...) }` 叶子路由 | `frontend/src/router/index.ts:36` |
| 侧边栏 / 顶部导航 | 共用 `MENU_ITEMS`，含 `/plugins` 项（图标 `puzzle`） | `frontend/src/constants/menu.ts:16` |
| 更多页外壳 | `SectionLayout` 页内菜单，现有两项：联机 / 工具 | `frontend/src/views/Connect.vue` |
| 侧边栏激活态 | 前缀匹配 `route.path.startsWith(item.path)`，`/more/*` 仍高亮「更多」，无需改动 | `frontend/src/components/layout/SideBar.vue:33` |
| 顶部导航激活态 | 同样前缀匹配，`/more/plugins` 高亮「更多」，无需改动 | `frontend/src/components/layout/TitleBar.vue:157` |
| 受管滚动 | `/more` 已在 `content-managed-scroll` 条件内，无需改动 | `frontend/src/App.vue:40` |
| 滚动结构 | `.plugins-page` 与 `.plugins-list-body` 都有 `overflow: auto`；并入 `SectionLayout` 视口后会叠成三层滚动容器 | `frontend/src/styles/views/Plugins.css:1,46` |
| 工具栏网格 | `.plugins-toolbar` 为 4 列 `auto minmax(180px,280px) auto auto`，首列即标题 | `frontend/src/styles/views/Plugins.css:18` |
| i18n | `sidebar.plugins` 仅被 `menu.ts` 与 `Plugins.vue` 引用；`connect.nav` 现有 room / tools | `frontend/src/i18n/locales/*.json` |
| 深链 | 后端、插件侧、文档均无 `/plugins` 前端路由引用（全仓库检索确认） | — |
| 测试 | `Connect.test.ts` 断言页内菜单 2 项，测试路由仅挂 room / tools | `frontend/src/views/Connect.test.ts` |

## 2. 目标结构

### 2.1 路由

`frontend/src/router/index.ts` 把插件页从叶子路由改为 `/more` 的第三个子路由，并为旧路径补一条重定向：

```ts
{
  path: '/more',
  component: withErrorBoundary(() => import('@/views/Connect.vue')),
  redirect: '/more/room',
  children: [
    { path: 'room', name: 'more-room', component: () => import('@/views/connect/ConnectRoomTab.vue') },
    { path: 'plugins', name: 'more-plugins', component: withErrorBoundary(() => import('@/views/Plugins.vue')) },
    { path: 'tools', name: 'more-tools', component: () => import('@/views/connect/ConnectToolsTab.vue') },
  ],
},
{ path: '/plugins', redirect: '/more/plugins' },
```

插件子页保留 `withErrorBoundary`：原叶子路由带该保护，去掉会让插件页失去白屏降级能力，属行为回退。重定向必须位于通配符路由 `/:pathMatch(.*)*` 之前（现有顺序已满足）。

### 2.2 菜单

`Connect.vue` 的 `navItems` 在中间插入插件项：

```ts
const navItems = computed(() => [
  { path: '/more/room', icon: 'wifi', label: t('connect.nav.room') },
  { path: '/more/plugins', icon: 'puzzle', label: t('connect.nav.plugins') },
  { path: '/more/tools', icon: 'activity', label: t('connect.nav.tools') },
])
```

`constants/menu.ts` 删除 `/plugins` 项，侧边栏与顶部导航随之不再单列「插件」。`puzzle` 图标短名已在 `ICON_MAP` 与图标子集内，无需重新生成。

### 2.3 页面与样式

- `Plugins.vue`：删除工具栏左侧 `.plugins-title`（图标 + 「插件」标题）及其无用引入
- `Plugins.css`：
  - `.plugins-page` 去掉 `overflow: auto`，滚动交给 `SectionLayout` 视口与 `.plugins-list-body`，消除嵌套滚动；保留 `height: 100%`、`min-height: 0` 与居中宽度
  - `.plugins-toolbar` 网格列改为 `minmax(180px, 280px) auto auto`（3 列）
  - `@media (max-width: 820px)` 中 `grid-column: 2 / 4` 按 3 列复核，仍成立

### 2.4 i18n（6 种语言）

- 新增 `connect.nav.plugins`
- 删除 `sidebar.plugins`（改动后无任何引用）

涉及 `zh-CN` / `zh-TW` / `en-US` / `de-DE` / `ja-JP` / `ru-RU`。

## 3. 实施步骤

1. 路由：新增 `/more/plugins` 子路由，删除 `/plugins` 叶子路由并补重定向
2. `Connect.vue`：`navItems` 插入插件项（中间位）
3. `constants/menu.ts`：移除 `/plugins` 项
4. `Plugins.vue` + `Plugins.css`：去掉工具栏标题、修正网格列与滚动容器
5. i18n 6 个语言文件：新增 `connect.nav.plugins`、删除 `sidebar.plugins`
6. 测试：`Connect.test.ts` 更新为 3 项并断言顺序，补 `/more/plugins` 渲染用例；新增 `/plugins` 重定向用例
7. 文档：本文件；并在 `connect-multi-page-plan.md` 的子页结构处标注已被本次变更扩展

## 4. 测试计划

- `pnpm check`：Prettier / ESLint / 类型检查 / 单测（当前 101 个测试文件、408 用例）
- `pnpm build`
- 浏览器实测（`pnpm exec vite --mode showcase --port 5199 --strictPort`）：
  - 侧边栏与顶部导航不再出现独立「插件」入口
  - `/more` 页内菜单为 联机 · 插件 · 工具，顺序正确
  - `/more/plugins` 正常渲染插件列表，工具栏无重复标题，滚动只有一层（列表体）
  - 直接访问 `/plugins` 落到 `/more/plugins`
  - 从插件页切回联机页，房间状态与状态轮询不中断
- 启动器实测

## 5. 影响与风险

| 风险 | 说明 | 处理 |
| --- | --- | --- |
| 嵌套滚动 | 三层 `overflow: auto` 叠加会使列表滚动条出现在内层、观感异常 | 去掉 `.plugins-page` 的 `overflow`，只保留列表体滚动 |
| 联机轮询在插件页继续 | 连接状态由 `Connect.vue` 外壳持有（2s 轮询），插件页成为子页后停留在插件页期间也会轮询 | 保持现状；若需停止，可另行讨论（会让外壳状态逻辑变复杂） |
| 删除 `sidebar.plugins` | 若有未检索到的引用会直接显示原始 key | 全仓库检索仅两处引用，均已移除；如需保守可保留该键 |
| 错误边界 | 插件页原带 `withErrorBoundary` | 子路由继续包裹，行为不变 |
| 顶部导航宽度 | `MENU_ITEMS` 少一项，顶部导航更宽松，无副作用 | 无需处理 |

## 6. 验收标准

- 侧边栏、顶部导航均无独立「插件」入口；「更多」高亮覆盖 `/more/plugins`
- `/more` 页内菜单为 联机 · 插件 · 工具，切换子页不重挂外壳
- 插件列表功能（搜索、筛选、启用/禁用、重载、卸载、安装、插件设置）与改动前一致
- `/plugins` 与 `/more/plugins` 最终都渲染插件页
- `pnpm check` 与 `pnpm build` 全部通过，启动器可正常运行