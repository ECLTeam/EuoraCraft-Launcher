# 更多页多页化实施方案（联机 / 插件 / 工具）

> 状态：已实施并验证（pnpm check / pnpm build / 启动器实测均通过）；后续已把原独立「插件」页并入更多页，子页扩展为**联机 · 插件 · 工具**（见第 10 节）
> 日期：2026-09-27
> 已确认决策：侧边栏入口与父路由命名为**更多**（`/more`），联机只是其中一个子页提供的功能；工具页**仅放 NAT 检测**且尽量与联机页解耦；EasyTier 由内置库 EasyTier-PyO3 提供，**不存在未安装态**；房间中**允许**切到工具页；连接状态用**父级 provide/inject** 共享。

## 1. 现状调研

| 项 | 现状 | 位置 |
| --- | --- | --- |
| 联机页 | 单页 610 行，含 EasyTier 服务卡片、加入房间、创建房间（选实例 → 端口探测 / 手填端口）、启动中、房间态（玩家列表 + 房间信息侧栏）、NAT 检测按钮、流程调试条 | `frontend/src/views/Connect.vue` |
| 页内菜单组件 | 通用组件，仅依赖 `title` / `icon` / `items`（`path` / `icon` / `label`），设置页同款 | `frontend/src/components/layout/SectionLayout.vue` |
| 侧边栏激活态 | `route.path === item.path \|\| (item.path !== '/' && route.path.startsWith(item.path))`，故 `/more/*` 仍高亮「更多」，**无需改动** | `frontend/src/components/layout/SideBar.vue:33` |
| 受管滚动 | `route.path.startsWith('/settings')` 时给 `.main-content` 加 `content-managed-scroll`（`overflow: hidden`，由页面自管滚动） | `frontend/src/App.vue:40`、`frontend/src/styles/app.css:111` |
| 组件 key | 以 `currentRoute.matched[0]?.path` 作 key，故 `/more/room` ↔ `/more/tools` 切换**不会重挂父组件** | `frontend/src/App.vue:49` |
| 连接状态 | `useConnector` 用 `onMounted` / `onUnmounted` 管理状态轮询（2s）、EasyTier 轮询（1s）、端口扫描（1s）三组定时器 | `frontend/src/features/connect/composables/useConnector.ts` |
| EasyTier 状态 | 后端 `get_easytier_status()` **恒定返回 `installed: True`**，前端安装卡片、下载按钮、进度轮询、`connector_easytier_download` 全部不可达 | `ECL/services/connector.py:413-421` |

可用联机能力：`status` / `join` / `hostPort` / `hostInstance` / `leave` / `kick` / `detectPorts` / `searchMcPort` / `natType`；`matchInstances` 后端仍是空占位，不在本次范围。

NAT 与 EasyTier 下载相关的引用**仅存在于** `Connect.vue`、`Connect.test.ts` 与 `useConnector` 自身，迁移与清理范围可控。

## 2. 目标结构

### 2.1 路由

`frontend/src/router/index.ts` 中把原 `/connect` 叶子路由改为 `/more` 父路由 + 两个子路由，与 `/settings` 写法保持一致：

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
```

进入 `/more` 自动落到 `/more/room`；侧边栏与标题栏菜单的入口由 `MENU_ITEMS` 统一定义，改为一处即可同步。

### 2.2 文件划分

| 文件 | 角色 | 来源 |
| --- | --- | --- |
| `frontend/src/views/Connect.vue` | 更多页外壳：`SectionLayout` + 嵌套 `RouterView` + `provide(useConnector)` | 重写（原 610 行内容迁出） |
| `frontend/src/views/connect/ConnectRoomTab.vue` | 联机子页：加入房间、创建房间、启动中、房间态、流程调试条 | 由原 `Connect.vue` 主体迁入 |
| `frontend/src/views/connect/ConnectToolsTab.vue` | 工具子页：仅 NAT 检测 | 新建 |
| `frontend/src/views/Plugins.vue` | 插件子页：由原独立 `/plugins` 页迁入（去掉页内标题，复用外壳菜单） | 复用（见第 10 节） |

子页目录 `views/connect/` 与 `views/settings/` 的既有约定一致；外壳组件沿用 `Connect.vue` 文件名，因为它仍是联机上下文的持有者（`useConnector` 在此 provide）。

### 2.3 菜单

`Connect.vue` 的 `navItems`：

```ts
const navItems = computed(() => [
  { path: '/more/room', icon: 'wifi', label: t('connect.nav.room') },
  { path: '/more/plugins', icon: 'puzzle', label: t('connect.nav.plugins') },
  { path: '/more/tools', icon: 'activity', label: t('connect.nav.tools') },
])
```

- 侧边栏项与页面标题取 `t('sidebar.more')`（「更多」），图标 `more`（`apps` 九宫格，`iconify.ts` 中已注册）；`menu.ts` 中该项为 `{ path: '/more', labelKey: 'sidebar.more', iconName: 'more' }`。
- 父标题「更多」与子页「联机 / 工具」不再字面重复，工具页也不再被表述为联机专属功能。
- 工具页图标复用现有 `activity`（`iconify.ts` 中已注册，语义为诊断/脉冲）。若要专用图标，需在 `ICON_MAP` 增加 `tool: 'tool'` 并重跑 `node scripts/gen-tabler-subset.mjs`（`tabler-subset.ts` 为自动生成文件，禁止手改）。

## 3. 状态共享与解耦

### 3.1 连接状态提升到父级

`useConnector` 在 `Connect.vue`（父级）调用**一次**，通过 `provide` 下发给联机子页：

```ts
const connector = useConnector({ onError: (error) => message.error(error) })
provide(CONNECTOR_KEY, connector)
```

由于父组件在子页切换时不重挂（`App.vue:49` 的 key 取 `matched[0].path`），三组轮询定时器与房间状态在切页过程中**持续有效**，切回联机子页即可看到实时房间态，不会出现「切页即断线重连」。

联机子页用 `inject(CONNECTOR_KEY)` 消费，不再自行调用 `useConnector`。

### 3.2 工具页与联机页解耦

工具页**不注入** `useConnector`，改为直接调用 `connectorApi.natType()` 并自持局部状态（`result` / `busy` / `error`）。理由：NAT 检测是一次性请求，不需要轮询与房间状态；这样工具页不依赖联机状态机，符合「两页尽量少联系」的要求。

随之把 NAT 从 `useConnector` 中移除（`natType` / `natBusy` / `detectNat`），避免留下无人使用的状态与导出。

### 3.3 联机页的 NAT 按钮

移除创建房间卡片头部的「NAT 检测」按钮与 `detectNatWithNotify()`。NAT 检测改为**工具页单一入口**，避免同一功能两处入口。

## 4. 一并处理的既有问题

### 4.1 EasyTier 安装卡片是死代码

后端 `get_easytier_status()` 恒定返回 `installed: True`（`ECL/services/connector.py:420`），因此：

- 服务卡片 `v-if="availability === 'available' && easyTier && !easyTier.installed"` 永不成立；
- 下载按钮、`isEasyTierWorking` 进度条、`easyTierPhaseText`、`downloadEasyTier` 全部不可达；
- 对应测试 `renders EasyTier progress`（`Connect.test.ts:191`）在测试里靠 mock 强行置 `installed: false` 才通过，属于为死代码维持的测试。

**建议**：移除服务卡片与下载路径，仅保留 `easyTier.installed` 参与 `serviceReady` 判定（即保留 `refreshEasyTier`，删除 `downloadEasyTier` / `easyTierBusy` / EasyTier 进度轮询 / 相关 i18n 键与测试）。**待确认**（见第 9 节）。

若确认移除，`connect.easyTier.*` 文案键（6 语言）一并删除；后端 `connector_easytier_download` 命令**不在本次范围**内改动。

### 4.2 受管滚动需覆盖 `/more`

`App.vue:40` 的条件由 `route.path.startsWith('/settings')` 改为同时匹配 `/more`，使 `SectionLayout` 拿到确定高度、由子页自管滚动，与设置页行为一致：

```ts
'content-managed-scroll': route.path.startsWith('/settings') || route.path.startsWith('/more'),
```

## 5. i18n（6 种语言：zh-CN / zh-TW / en-US / ja-JP / de-DE / ru-RU）

新增（实际落地键）：

| 键 | 中文文案 |
| --- | --- |
| `sidebar.more` | 更多（侧边栏项与更多页标题共用） |
| `connect.nav.room` | 联机 |
| `connect.nav.tools` | 工具 |
| `connect.tools.natCardTitle` | NAT 类型检测 |
| `connect.tools.natCardDesc` | 检测当前网络的 NAT 类型，判断联机穿透能力 |
| `connect.tools.detecting` | 正在检测网络类型... |
| `connect.tools.detectFailed` | 检测失败 |
| `connect.tools.idleTitle` | 尚未检测网络类型 |
| `connect.tools.idleDesc` | 点击右上角「NAT 检测」开始。结果仅用于判断能否直连，不影响已建立的房间。 |
| `connect.tools.publicAddress` | 公网地址 |

复用现有 `connect.nat.*`（`detect` / 各类型标签 / `ipv6Available` 等）渲染结果。

清理（已执行）：`connect.easyTier.*`（4.1）、`connect.nav` 下未使用键、整个 `connect.entry.*` 块、`sidebar.connect` 与 `connect.title`（随 `/more` 重命名一并移除）——经检索确认**无任何代码引用**，且 6 语言 `connect.*` / `sidebar.*` 键已校验一致。

## 6. 样式与布局

- `frontend/src/styles/views/Connect.css` 保持**单一共享文件**，由外壳与两个子页共同 `<style scoped src>` 引入（与 `InstanceDetailModal.css` 被 5 个组件共享的既有做法一致）。
- 原 `.connect-page` 是整页 `flex` 容器；多页化后它成为 `SectionLayout` 右侧 viewport 内的子页根节点，需改为 `height: 100%` 填充 viewport，并保留内部 `.connect-scroll-area` 自管滚动，避免与 `.section-layout__viewport` 的 `overflow: auto` 形成双滚动条。
- 新增工具页样式：NAT 结果卡片（类型标签 + 公网地址 + IPv6 支持 + 重新检测按钮），复用 `UiCard` / `UiTag` / `UiButton` / `UiIcon` 与既有 CSS 变量，保持与联机页一致的卡片透明度与边框风格。

## 7. 测试计划

| 文件 | 内容 |
| --- | --- |
| `frontend/src/views/Connect.test.ts` | 保留外壳用例：菜单渲染两个子项、默认落到联机子页、切换子页后路由与内容正确 |
| `frontend/src/views/connect/ConnectRoomTab.test.ts` | 由现 `Connect.test.ts` 迁入房间相关 12 个用例，改为挂载子页 + `provide` mock 连接状态 |
| `frontend/src/views/connect/ConnectToolsTab.test.ts` | 新建：点击检测调用 `connectorApi.natType()`；成功渲染类型标签与公网地址；失败提示错误；检测中禁用按钮 |
| `frontend/src/features/connect/composables/useConnector.test.ts` | 移除 NAT 相关断言；若按 4.1 移除下载路径，同步移除对应断言 |

需删除的用例：`triggers NAT detection from the corner button`（迁至工具页测试）、`renders EasyTier progress`（4.1 确认后删除）。

## 8. 验收标准

1. `cd frontend && pnpm check` 全绿（Prettier / ESLint / vue-tsc / vitest）。
2. `pnpm build` 通过。
3. 启动器实测：
   - 点击侧边栏「更多」进入 `/more/room`，页内菜单默认选中「联机」；
   - 切到「工具」子页，NAT 检测可正常发起并展示结果；
   - 建房或加入房间后切到工具页再切回，房间态与玩家列表仍在（父级轮询未中断）；
   - 侧边栏「更多」在 `/more/tools` 下仍保持高亮；
   - 无双滚动条、无布局错位，卡片透明度与联机页其余卡片一致。
4. 后端无改动，但仍需 `python -m ruff check ECL tests` 与相关 pytest 通过（回归确认）。

## 9. 已确认决策

1. **EasyTier 死代码**：一并清理——移除服务卡片与下载路径、`downloadEasyTier` / `easyTierBusy` / EasyTier 进度轮询、相关 i18n 键与测试；保留 `easyTier.installed` 参与 `serviceReady` 判定。后端 `connector_easytier_download` 命令本次不动。
2. **菜单与标题文案**：侧边栏入口与父路由命名为「更多」（`/more`），子页为「联机 / 工具」——工具页不属于联机专属功能，故父级用更中性的「更多」承载。
3. **工具页图标**：复用现有 `activity`，未新增图标。
4. **未使用文案键**：一并清理。

## 10. 后续扩展：插件页并入更多页

原「插件」是侧边栏独立叶子路由 `/plugins`；现并入更多页，作为第 2 个子页（顺序 **联机 · 插件 · 工具**）。方案细节见 `docs/plugins-into-more-plan.md`，要点：

- 路由：`/more/plugins`（`more-plugins`）新增为 `/more` 子路由；原 `/plugins` 叶子路由直接删除，**不保留重定向**（该路径落入通配符路由回到首页）。
- 侧边栏：`menu.ts` 移除 `{ path: '/plugins', labelKey: 'sidebar.plugins', iconName: 'puzzle' }`，插件仅从更多页内菜单进入。
- 页内菜单：`Connect.vue` 的 `navItems` 插入 `{ path: '/more/plugins', icon: 'puzzle', label: t('connect.nav.plugins') }`。
- 插件页：`Plugins.vue` 去掉工具栏左侧「插件」标题（父级菜单已表明当前位置）；`Plugins.css` 移除根节点 `overflow: auto`、把工具栏网格列由 4 列收敛为 3 列，避免与 `.section-layout__viewport` 形成嵌套滚动。
- i18n：6 语言新增 `connect.nav.plugins`，删除已无引用的 `sidebar.plugins`。
- 测试：`Connect.test.ts` 菜单断言由 2 项改为 3 项，并新增插件子页渲染用例。