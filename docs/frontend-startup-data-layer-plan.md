# 前端统一启动数据层 与 IPC 日志耗时收敛 实施方案

本方案包含两件相互独立、可分次提交的改动：

- **第一部分（D）**：建立前端统一启动数据层，收敛启动期重复的 IPC 读请求。
- **第二部分**：操作日志的「耗时」按需输出，只在真正耗时的操作上保留。

两部分都遵循 `AGENTS.md` 的固定工作流与测试要求。

---

## 1. 背景

### 1.1 实测现象

一次冷启动的 IPC 日志中，同一份数据被反复读取：

| 命令 | 次数 | 说明 |
| --- | --- | --- |
| `settings_get` | 4 | 同一份配置文件，3 次单分区 + 1 次批量 |
| `launcher_info` | 2 | 各取一半字段（窗口装饰 / 版本号） |
| `accounts_list` | 2 | 插件状态层与应用层各拉一次 |
| `plugin_get_routes` / `_slots` / `_vue_slots` / `_vue_components` | 各 1 | 插件注册表 |
| `info_card_get` | 1 | 已走 vue-query |
| `image_read_file` | 1 | 背景图，单次编码长度约 24 MB |

后端每次耗时均为 0 ms，因此这不是性能瓶颈，主要是日志噪声与职责分散。

### 1.2 根因

1. **启动期存在多条互不知情的读取路径**，各自直连 IPC：

   | 读取点 | 位置 | 命令 |
   | --- | --- | --- |
   | 语言 | `src/main.ts` → `src/i18n/index.ts` | `settings_get('ui')` |
   | 窗口装饰 | `src/app/runtime/windowChrome.ts` | `launcher_info` |
   | showcase 判定 | `src/app/runtime/useAppRuntime.ts` | `settings_get('launcher')` |
   | 初始配置 | `src/app/runtime/useAppRuntime.ts` | `settings_get`（4 分区） |
   | 版本号 | `src/app/runtime/useAppRuntime.ts` | `launcher_info` |
   | 插件状态层 | `src/plugin-sdk/state.ts` | `accounts_list` |
   | 插件注册表 | `src/composables/usePluginBridge.ts` | `plugin_get_*` ×4 |
   | 主题保存 | `src/composables/useTheme.ts` | `settings_get('ui')` |
   | 账户列表 | `src/features/accounts/stores/accountStore.ts` | `accounts_list` |

2. **IPC 客户端无去重/缓存**：`src/api/client/commands.ts` 的 `call()` 每次调用都真实往返后端，只有图片读取有按路径的内存缓存。

3. **项目已有 vue-query 层但未被复用**：`src/app/queryClient.ts` 已配置 `staleTime: 60_000`、`retry: false`，`src/app/queryKeys.ts` 已登记 `gameHome.infoCard`、`instanceInstall.*`，`gameHomeStore` / `instanceInstallStore` 已在使用 `queryClient.fetchQuery`。

   也就是说，去重与缓存能力**已经具备**，缺的是让启动期的读请求统一走这条通道。

### 1.3 日志耗时机制现状

`ECL/utils/operation_logging.py` 中：

- `OperationTrace.log()` 会给每个阶段追加「距操作开始的累计耗时」。
- `OperationLogFilter.filter()` 会给**操作上下文内的任意一行日志**追加同样的耗时。

因此连「开始读取设置；耗时：0 毫秒」这种必然是 0 的行也带上了耗时；而 `files.py` 里 `图片读取成功…耗时：94 毫秒` 这类嵌套行则被附加了外层操作的耗时。对 0 ms 的本地读取而言这是纯噪声。

---

## 2. 目标与非目标

### 2.1 目标

- 全局/启动期数据统一经 `queryClient.fetchQuery` 获取，按 `queryKey` 去重与缓存。
- `plugin-sdk/state.ts` 不再自行发起 IPC，改为订阅统一数据层。
- 数据层覆盖范围尽量广，避免只解决 4 个命令、后续又长出新的并行路径。
- 操作日志的耗时只在真正耗时的操作上输出，开始阶段行不再输出耗时。

### 2.2 非目标

- 不改动后端命令签名与 IPC 契约（前端契约零变化）。
- 不引入新的状态管理库；复用现有 vue-query + Pinia。
- 不重构与启动无关的视图级数据流（实例列表、模组搜索等），除非它们属于下方「统一层范围」。

---

## 3. 第一部分（D）：统一启动数据层

### 3.1 数据范围

按「全局/跨视图」原则划分。范围刻意取宽，凡是被两个以上入口读取、或属于应用级状态的数据都纳入。

**纳入统一层（Tier 1）**

| 域 | 命令 | 现有读取点 |
| --- | --- | --- |
| 配置（全部分区） | `settings_get` | i18n、runtime、主题、settingsStore |
| 启动器信息 | `launcher_info` | windowChrome、runtime |
| 账户 | `accounts_list` | plugin-sdk、accountStore |
| 信息卡片 | `info_card_get` | gameHomeStore（已走 vue-query） |
| 插件注册表 | `plugin_get_routes` / `_slots` / `_vue_slots` / `_vue_components` | usePluginBridge |
| 用户协议状态 | `user_agreement_get` | 首次启动引导 |
| 主题与背景 | 由 `ui` 配置派生 + `image_read_file`（背景图） | useTheme |

**后续一并纳入（Tier 2，本轮不做）**

| 域 | 命令 | 理由 | 结论 |
| --- | --- | --- | --- |
| Java 清单 | `game_java_inventory` | 多视图共享（设置、实例、Java 面板） | 纳入 |
| Minecraft 版本目录 | `game_versions` | 安装向导与实例设置共享 | 纳入 |
| 本地实例 | `game_scan` / `game_instances` | 首页与实例页共享 | **不纳入**：实例列表变化频繁，缓存与失效成本高 |

**明确不纳入**：按需触发、结果与视图强绑定的查询（模组搜索、下载源探测、单实例设置读取等）。

### 3.2 架构

**不新建并行体系**，在现有 vue-query 之上补一层薄的查询定义。

```
src/app/
  queryClient.ts          # 已存在，不改
  queryKeys.ts            # 扩展：登记全部全局域 key
  data/
    index.ts              # 按域导出的 query 函数（fetch + 失效）
```

**扩展 `src/app/queryKeys.ts`**

```ts
export const queryKeys = {
  config: {
    section: (section: ConfigSection) => ['config', section] as const,
    sections: (sections: readonly ConfigSection[]) => ['config', 'sections', [...sections].sort()] as const,
  },
  launcherInfo: ['launcher-info'] as const,
  accounts: ['accounts'] as const,
  userAgreement: ['user-agreement'] as const,
  pluginRegistry: {
    routes: ['plugin-registry', 'routes'] as const,
    slots: ['plugin-registry', 'slots'] as const,
    vueSlots: ['plugin-registry', 'vue-slots'] as const,
    vueComponents: ['plugin-registry', 'vue-components'] as const,
  },
  gameHome: { infoCard: ['game-home', 'info-card'] as const },
  instanceInstall: {
    loaderVersions: (loader: string, gameVersion: string) =>
      ['instance-install', 'loader-versions', loader, gameVersion] as const,
    fabricApiVersions: (gameVersion: string) =>
      ['instance-install', 'fabric-api-versions', gameVersion] as const,
  },
} as const
```

**`src/app/data/index.ts` 形态**

每个域导出一组函数，内部统一走 `queryClient.fetchQuery`，并导出对应的失效函数：

```ts
export function configSectionQuery(section: ConfigSection) {
  return queryClient.fetchQuery({
    queryKey: queryKeys.config.section(section),
    queryFn: () => settingsApi.getSection(section),
  })
}

export function configSectionsQuery(sections: readonly ConfigSection[]) {
  return queryClient.fetchQuery({
    queryKey: queryKeys.config.sections(sections),
    queryFn: () => settingsApi.getSections(sections),
  })
}

export function launcherInfoQuery() {
  return queryClient.fetchQuery({
    queryKey: queryKeys.launcherInfo,
    queryFn: () => aboutApi.getLauncherInfo(),
  })
}

export function accountsQuery() { /* 同形 */ }
export function pluginRegistryQuery() { /* Promise.all 四个注册表命令 */ }

export function invalidateConfig(section?: ConfigSection): Promise<void> {
  return section
    ? queryClient.invalidateQueries({ queryKey: queryKeys.config.section(section) })
    : queryClient.invalidateQueries({ queryKey: ['config'] })
}
// invalidateLauncherInfo / invalidateAccounts / invalidatePluginRegistry 同形
```

### 3.3 消费者改造

| 位置 | 改动 |
| --- | --- |
| `src/i18n/index.ts` | `loadLocaleFromBackend` 改为 `configSectionQuery('ui')` |
| `src/app/runtime/windowChrome.ts` | 改为 `launcherInfoQuery()`（保留 3s 超时语义） |
| `src/app/runtime/useAppRuntime.ts` | showcase 判定与 `loadInitialConfig` 合并为一次 `configSectionsQuery([...])`；版本号改为 `launcherInfoQuery()`（与 windowChrome 共用缓存） |
| `src/composables/useTheme.ts` | `saveThemeConfig` 的读改写改为 `configSectionQuery('ui')` + 写后 `invalidateConfig('ui')` |
| `src/features/settings/stores/settingsStore.ts` | `load()` 改为 `configSectionsQuery([...])`；写操作后 `invalidateConfig(section)` |
| `src/plugin-sdk/state.ts` | 删除自有 IPC；改为订阅数据层（见下） |
| `src/composables/usePluginBridge.ts` | 注册表改为 `pluginRegistryQuery()`；相关 `plugin:*` 事件改为 `invalidatePluginRegistry()` |
| `src/features/accounts/stores/accountStore.ts` | 改为 `accountsQuery()`；`accounts:changed` 事件改为 `invalidateAccounts()` |

**`plugin-sdk/state.ts` 的订阅方式**

保留其对外 API（`getThemeState` / `getLauncherState` / `getAccountState` 等）不变，避免影响插件 SDK 契约。内部改为：

- 首次读取：调用统一层 query（走缓存，不再多发 IPC）。
- 后续更新：监听统一层的失效/更新（通过 `queryClient.getQueryCache().subscribe` 或由数据层在失效后回调），同步到自身的 `ref`。

这样插件窗口与主窗口共享同一份缓存。

### 3.4 失效策略

| 触发 | 失效对象 |
| --- | --- |
| `settings_set` 成功 | `invalidateConfig(对应分区)` |
| 后端 `config:init` / `config:updated` 事件 | `invalidateConfig()` |
| `accounts:changed` 事件 | `invalidateAccounts()` |
| `plugin:*_registered` / `plugin:status_changed` 事件 | `invalidatePluginRegistry()` |
| 安装/卸载等写操作 | `invalidateConfig()` + 相关实例查询 |

### 3.5 实施步骤（每步独立可提交、可回滚）

1. **扩展 `queryKeys.ts` + 新增 `src/app/data/index.ts`**，先不接入消费者。跑 `pnpm check`。
2. **接入 config 域**：改造 `i18n`、`windowChrome`、`useAppRuntime`、`useTheme`、`settingsStore`。此步后 `settings_get` 应从 4 次降到 1~2 次。
3. **接入 `launcher_info`**：`windowChrome` 与 `useAppRuntime` 共用缓存。应从 2 次降到 1 次。
4. **接入账户域**：`plugin-sdk` 与 `accountStore` 共用。应从 2 次降到 1 次。
5. **接入插件注册表**：`usePluginBridge` 走统一层。
6. **接入 Tier 2**（如确认纳入）：Java 清单、本地实例、版本目录。
7. **实机验证**：抓一次冷启动日志，逐条核对命令次数。

### 3.6 测试计划

- 前端：`cd frontend && pnpm check`（Prettier + ESLint + 类型 + 单测）；`pnpm build`。
- 为新增的 `src/app/data/*` 补单测：相同 queryKey 并发调用只触发一次 `queryFn`；失效后重新拉取。
- 更新受影响的现有单测（`windowChrome.test.ts`、`useAppRuntime` 相关、`settingsStore.test.ts`、`accountStore.test.ts`、`plugin-sdk` 相关）。
- 实机：`python main.py` 启动，确认启动日志中 `settings_get` / `launcher_info` / `accounts_list` 次数下降，且界面数据正常。
- 后端：本次不改后端命令，仍执行 `ruff check ECL tests` + `pytest` 作为回归。

---

## 4. 第二部分：操作日志耗时按需输出

### 4.1 现状

- `ECL/utils/operation_logging.py`：`OperationTrace.log()` 与 `OperationLogFilter.filter()` 无条件追加耗时。
- `ECL/api/bridge.py` 的 `guard_ipc_handler` 用 `operation_scope(spec.title)` 建立上下文，并记录「开始/完成/失败」行。

### 4.2 改造

1. **开始阶段行不再输出耗时**（无条件）。「开始」行的累计耗时恒为 0，没有信息量。
2. **仅对声明为「计时」的操作输出耗时**：`IpcLogSpec` 新增布尔字段（例如 `timed`），`guard_ipc_handler` 据此设置 `OperationTrace` 的计时开关；`OperationLogFilter` 只在开关打开时追加耗时。

   建议标记为 `timed = True` 的命令：网络或重型操作，例如
   `launcher_check_update`、`launcher_update_download`、`game_versions`、`game_loader_versions`、`game_fabric_api_versions`、`game_java_catalog`、`game_java_install`、`game_java_check_updates`、`game_install`、`game_scan`、`game_java_scan`、模组搜索与下载相关命令。

3. 保持 `IpcLogPolicy` 作为唯一声明处，新增命令时同步维护。

> 备选方案：阈值法（累计耗时 ≥ 50 ms 才输出）。实现更简单、无需维护名单，且能捕获"本地操作意外变慢"的异常；代价是日志不确定性更强、单测需要 mock 时钟。默认采用显式声明，见 §7。

### 4.3 测试

- `ECL/utils/operation_logging.py`：新增用例覆盖「开始行不含耗时」「未声明计时的操作不含耗时」「声明计时的操作含耗时」。
- `ECL/api/bridge.py`：`guard_ipc_handler` 的日志用例同步更新。
- 执行 `ruff check ECL tests`、`ruff format --check ECL tests`、`pytest`。

---

## 5. 风险与回滚

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| 缓存导致数据陈旧 | `staleTime` 60s 内不重取 | 写操作与后端事件一律显式 `invalidate`；对时效敏感的命令不纳入或单独设 `staleTime: 0` |
| `plugin-sdk` 契约被破坏 | 插件依赖其导出 API | 对外 API 保持不变，仅替换内部实现 |
| 启动顺序变化 | i18n/主题依赖读取时机 | 分步接入，每步实机验证启动流程 |
| 后端日志行为变化影响排障 | 耗时信息减少 | 仅收敛 0 ms 的本地操作，网络/重型操作保留；保留失败行与阶段信息 |

回滚：每个实施步骤独立提交，出问题可按提交粒度回退。

---

## 6. 验收标准

- 冷启动日志中：`settings_get` ≤ 2 次、`launcher_info` = 1 次、`accounts_list` = 1 次。
- 启动器功能无回归：语言、主题与背景、账户、首页信息卡片、插件路由与插槽均正常。
- `pnpm check` / `pnpm build` / `ruff` / `pytest` 全部通过。
- 日志中不再出现「开始…；耗时：0 毫秒」这类无信息量的行。

---

## 7. 已确认决策

1. **Tier 2 范围**：纳入 Java 清单（`game_java_inventory`）与 Minecraft 版本目录（`game_versions`）；本地实例不纳入。
2. **耗时改造方式**：采用「显式声明名单」——`IpcLogSpec` 新增计时字段，网络/重型命令显式置位。
3. **实施批次**：本轮先做 §3.5 的第 1~4 步（数据层骨架 + config + `launcher_info` + 账户），验证通过后再推进插件注册表、Tier 2 与第二部分的日志改造。