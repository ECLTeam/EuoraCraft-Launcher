# 联机页 NAT 检测入口与结果缓存方案

日期：2026-10-31
范围：`frontend`（联机页 `ConnectRoomTab.vue`、工具页 `ToolsTab.vue`、NAT 相关组件与文案）

## 1. 需求

把 NAT 类型检测从工具页移到联机页「加入联机房间」卡片标题行：位于标题右侧、「联机设置」左侧；点击以弹窗展示检测结果；结果支持缓存。已确认的取舍：

- 缓存：内存缓存，10 分钟过期。
- 打开弹窗：有有效缓存直接展示，无缓存或已过期才自动探测；另提供「重新检测」。
- 工具页旧卡片：删除，避免两个入口。

## 2. 现状

| 位置 | 现状 |
| --- | --- |
| `views/more/ToolsTab.vue` | 三张卡片：自定义下载、皮肤头像裁剪、NAT 类型检测 |
| `components/tools/NatToolCard.vue` | 点击按钮才探测，`state` 为组件内 `ref`，卸载即丢失，无缓存 |
| `views/connect/ConnectRoomTab.vue` | 加入卡片标题行 `.connect-join-heading` 为 flex：标题块 `flex: 1`，「联机设置」按钮 `margin-left: auto` |
| `views/connect/ConnectRoomTab.test.ts:363` | 既有用例断言「房间流程内不再出现 NAT 检测」，本次需改写 |
| `i18n` | `tools.natCardTitle`、`tools.nat.*`、`tools.detecting/detectFailed/timeout/busy/probeFailed/publicAddress/notObtained/unknownType/ipv6Detected/ipv6NotDetected/natDetails` 仅被该卡片引用；`tools.nat.resultTitle` 当前无引用 |

后端无需改动：`connector_nat_type` 已保留 `CONNECTOR_NAT_TYPE_TIMEOUT` / `_BUSY` / `_FAILED` 领域错误码。

## 3. 方案

### 3.1 缓存：`features/connect/composables/useNatType.ts`（新增）

模块级单例状态，跨页面切换保留：

```ts
const cacheTtlMs = 10 * 60 * 1000
const state = ref<'idle' | 'detecting' | 'success' | 'error'>('idle')
const result = ref<NatTypeResult | null>(null)
const detectedAt = ref<number | null>(null)
const failureKind = ref<'timeout' | 'busy' | 'probeFailed' | 'other' | null>(null)
const failureMessage = ref('')
let pendingDetection: Promise<NatTypeResult | null> | null = null
```

- `detect(force = false)`：已有进行中的探测直接复用该 Promise（避免并发触发后端 `CONNECTOR_NAT_TYPE_BUSY`）；未过期且非强制时返回缓存；否则发请求。
- 失败时清空 `result` 与 `detectedAt`（失败即无有效缓存），记录 `failureKind` 与原始消息，由弹窗负责翻译文案——composable 不依赖 i18n。
- `isCacheFresh()`：普通函数而非 computed（依赖 `Date.now()`，做成 computed 会产生不随时间更新的误导性缓存）。
- 导出 `resetNatDetection()` 供测试清理模块级状态。

### 3.2 弹窗：`components/connect/NatDetectModal.vue`（新增）

- 复用 `components/modals/Modal.vue`（`type="content"`、`width="460px"`、`#footer` 自定义），自动获得焦点陷阱、Esc 关闭与全局模态栈仲裁。
- `visible` 用 `defineModel<boolean>('visible')`。
- `watch(visible)`：打开时若 `isCacheFresh()` 为假则 `detect()`，否则直接渲染缓存。
- 展示优先级：`detecting` → 加载；`error` → 错误块；`result` → 结果；否则空态。
- 结果区沿用原卡片的呈现：类型标签（`UiTag` tone 按 `type`）+ 公网地址 + IPv6 状态 + 「上次检测于 {time}」。
- 底部：`关闭` + `重新检测`（`detect(true)`，探测中禁用并显示 loading）。

### 3.3 入口按钮：`ConnectRoomTab.vue`

在 `.connect-join-heading` 中、「联机设置」之前插入：

```vue
<UiButton size="sm" variant="ghost" icon="wifi" data-action="nat-detect" @click="natVisible = true">
  {{ t('connect.nat.entry') }}
</UiButton>
```

样式（`styles/views/Connect.css`）：`[data-action='nat-detect']` 承接 `margin-left: auto`，`[data-action='connector-settings']` 的 `margin-left` 改为 0，两个按钮靠右成组。

窄窗口风险：标题块本身是 `flex: 1; min-width: 0`，两个按钮 `flex-shrink: 0`。960 / 720 宽实测若出现标题或提示被挤压换行，则对 `[data-action='nat-detect']` 加容器查询：宽度不足时只保留图标，`title` 与 `aria-label` 保留完整文案。

### 3.4 工具页清理

- `ToolsTab.vue` 移除 `<NatToolCard />` 与 import。
- 删除 `components/tools/NatToolCard.vue`、`NatToolCard.test.ts`、`styles/components/tools/NatToolCard.css`。
- `ToolsTab.test.ts` 改为断言两张卡片，并去掉只为 NAT 卡片存在的 `connectorApi` mock。

### 3.5 文案

`tools.*` 下的 NAT 键整体迁移到 `connect.nat.*`，六语言同步；新增 `entry`（按钮）、`title`（弹窗标题）、`redetect`（重新检测）、`cachedAt`（上次检测于 {time}）、`detect`（空态按钮）。删除 `tools.*` 下迁移后的键与无引用的 `tools.nat.resultTitle`。

## 4. 测试计划

新增 `components/connect/NatDetectModal.test.ts`：

1. 无缓存打开 → 自动探测一次，展示类型、公网地址、IPv6 状态。
2. 关闭再打开 → 命中缓存，`natType` 不再调用。
3. `vi.setSystemTime` 前进 11 分钟 → 重新打开触发探测。
4. 探测失败（`CONNECTOR_NAT_TYPE_TIMEOUT` / `_BUSY`）→ 展示对应文案，且缓存被清空。
5. 「重新检测」→ 强制再探测一次。
6. 探测进行中重复打开 → 复用同一 Promise，只调用一次。

改写 `ConnectRoomTab.test.ts`：原「不再暴露 NAT 检测」用例替换为「加入卡片标题行存在 NAT 检测按钮，位于联机设置左侧，点击打开弹窗」。

## 5. 验证

### 5.1 实际改动

| 文件 | 变更 |
| --- | --- |
| `features/connect/composables/useNatType.ts` | 新增，Pinia 单例 `natDetection`，10 分钟缓存、进行中请求复用、失败清空缓存 |
| `components/connect/NatDetectModal.vue` | 新增，复用 `Modal.vue`，`watch(visible, { immediate: true })` 决定展示缓存或自动探测 |
| `components/connect/NatDetectModal.test.ts` | 新增 7 个用例 |
| `views/connect/ConnectRoomTab.vue` | 标题行插入 NAT 按钮，挂载弹窗，新增 `natVisible` |
| `styles/views/Connect.css` | `[data-action='nat-detect']` 承接 `margin-left: auto`，联机设置紧随其后 |
| `views/more/ToolsTab.vue`、`ToolsTab.test.ts` | 移除 NAT 卡片，断言两张卡片 |
| `views/connect/ConnectRoomTab.test.ts` | 「不再暴露 NAT 检测」改为「入口位于标题行、联机设置左侧、点击打开弹窗」 |
| `components/tools/NatToolCard.vue`、`NatToolCard.test.ts`、`styles/components/tools/NatToolCard.css` | 删除 |
| `i18n/locales/*.json`（6 种） | `tools.*` 的 NAT 文案迁到 `connect.nat.*`，新增 `entry`/`title`/`redetect`/`cachedAt`/`ipv6`，删除无引用的 `tools.nat.resultTitle`、`tools.nat.ipv6Available` |
| `components.d.ts` | 自动生成文件补 `NatDetectModal`，移除已删除组件的 `NatToolCard` 残留声明 |

### 5.2 检查结果

| 检查 | 结果 |
| --- | --- |
| `prettier --check`（改动文件 + 6 个 locale） | 通过（locale 统一为 LF） |
| `eslint`（改动文件） | 通过 |
| `pnpm typecheck` | 通过 |
| `vitest run`（全量 190 文件） | 1159 通过、1 todo、0 失败（首轮 `importOrder.test.ts` 等 2 个文件超时，复跑通过，属环境抖动） |

### 5.3 真实界面验收

`vite --mode showcase` + Edge 无头（CDP）实测：

- 960 / 720 宽：标题行内按钮顺序为「NAT 检测 → 联机设置」，`margin-left: auto` 生效，标题与提示不换行、无溢出（`scrollWidth` 未超过 `clientWidth`）。
- 点击入口：弹窗展示「端口受限锥形 NAT / 203.0.113.42:51820 / 已检测到公网 IPv6 / 上次检测于 23:10」，底部「关闭 + 重新检测」右对齐。
- 关闭后重新打开：直接展示同一结果，未出现「正在检测网络类型...」，缓存命中。
- Classic 亮色、Classic 深色、Folia 亮色三种状态截图确认对比度与布局正常。

未覆盖：真实启动器内的实际探测耗时与网络异常路径（showcase 返回固定结果）；`pnpm test:accessibility` 需要 Python Playwright，本机未安装，未执行。

## 6. 影响面与风险

- 工具页少一张卡片，布局从三卡变两卡（`Tools.css` 的 grid 自适应，需目视确认无空列）。
- `useNatType` 是模块级单例，测试之间必须 `resetNatDetection()`，否则用例互相污染。
- 缓存 10 分钟：更换网络后可能短暂看到旧结论，弹窗显示「上次检测于」时间，并提供重新检测。
