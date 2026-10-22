# 路由切换时实例扫描与 ecl.json 读取优化实施方案

## 1. 决策、目标与非目标

本方案仅优化启动器前台在首页和实例管理页之间切换时产生的重复本地实例请求，优先消除同一运行会话内对同一路径 `ecl.json` 的重复读取。实施后，已经载入且未失效的实例列表和 `activeVersion` 不应因路由重新挂载而再次发起 `game_scan` 或 `game_config_get`。

本方案不改变 Minecraft 实例扫描结果、实例选择规则、手动刷新语义和后端文件变更事件的对外契约；也不把 `ecl.json` 的内容持久化到浏览器存储。缓存仅存在于当前启动器进程的 Pinia/API 模块内存中。

`ecl.json` 是启动器维护的目录配置。运行期间由启动器自己的写入操作必须立即更新缓存；用户或其他程序在启动器运行期间直接编辑该文件，不保证立即反映，用户执行手动刷新或重启启动器后必须能获得最新内容。

## 2. 已核实现状与问题边界

当前 `instanceInstallApi.scan()` 已按标准化游戏路径维护 `scanCache`。相同路径未被失效且未要求 `force` 时，它直接返回克隆结果，不会再次调用 `game_scan`。后端 `GameService.scan_versions()` 也有版本扫描缓存和版本目录变更监视。因此，日志中的：

```text
读取 ecl.json 成功: ...\\ecl.json，activeVersion=...
```

不是版本目录扫描日志，而是 `game_config_get` 调用 `read_ecl_config()` 后输出的 DEBUG 日志。

问题在于 `instancePathConfigApi.getActiveVersion()` 每次都会调用 `readConfig()`；而首页 `useInstanceManager.loadVersions()` 和实例管理页 `switchPath()` 都会在路由挂载或切换路径后调用它。现有 `confirmedActiveVersions` 只用于跳过相同值的写入，尚未用于跳过读取。前台切换路由时，因此仍会看到重复的后端 `ecl.json` 读取日志和 IPC 往返。

## 3. 方案选择与缓存契约

采用“前端会话缓存 + 明确失效 + 强制刷新”方案。

| 数据                            | 缓存位置                                        | 命中时行为                                                  | 失效或强制同步                                                    |
| ------------------------------- | ----------------------------------------------- | ----------------------------------------------------------- | ----------------------------------------------------------------- |
| 已扫描实例                      | `instanceInstallApi.scanCache`（既有）          | 直接返回防御性副本，不调用后端                              | `game:versions_changed`、安装、卸载、路径修改/删除、`force: true` |
| 当前路径 `activeVersion`        | `instancePathConfigApi` 新增的读取缓存          | 直接返回缓存的 `string` 或 `null`，不调用 `game_config_get` | 成功的配置写入、路径修改/删除、显式 `force` 读取                  |
| 正在进行的 `activeVersion` 读取 | `instancePathConfigApi` 新增的按路径 Promise 表 | 合并同一路径的并发读取，只产生一个 IPC 请求                 | Promise 落定后移除；失败不写入结果缓存                            |

缓存键统一复用现有 `normalizePathKey()` 的规则：去除末尾分隔符、统一为 `/`、按不区分大小写的 Windows 路径比较。缓存结果必须包含 `null`，否则没有已选版本的路径会在每次路由进入时重复读取。

用户点击实例管理页的刷新按钮，以及调用 `loadAll(true)` 的强制刷新路径，必须同时强制重新读取该路径的 `activeVersion`。成功的手动刷新后，新的配置结果替换旧缓存。

## 4. 具体改动点

### 4.1 `frontend/src/features/instances/api/instancePathConfigApi.ts`

1. 将 `confirmedActiveVersions` 扩展为能区分“已确认但为空”和“尚未读取”的配置缓存，例如 `Map<string, string | null>` 加 `has(key)` 判断；保留该缓存供写入去重使用。
2. 新增 `pendingActiveVersionReads`，将同一路径正在执行的 `game_config_get` Promise 合并。无论请求成功或失败都必须在 `finally` 中仅删除自身对应的 Promise，防止较旧请求删除后续请求。
3. 为 `getActiveVersion()` 增加可选 `force` 参数。非强制调用先读缓存；强制调用绕过缓存，并以新结果覆盖缓存。失败时不覆盖最后一个成功值，并向调用方继续抛出原错误。
4. `getActiveVersion()` 成功读取、`writeConfig()` 与 `patchConfig()` 成功写入后同步更新 `activeVersion` 缓存。原始 `readConfig()` 保持无缓存，以保留“读取完整配置”接口的实时语义；全量写入和增量写入均以服务端返回的完整配置或传入的最终值为准，兼容历史字段 `active_version`。
5. 新增并导出窄接口 `invalidateActiveVersionCache(gamePath?: string)`。传入路径时只移除该路径的读缓存与已确认值；未传入路径时清空全部。不得把缓存 Map 暴露给调用方。

### 4.2 `frontend/src/features/instances/stores/instanceStore.ts`

1. `loadAll(force)` 在读取当前激活路径时，将同一 `force` 传入 `getActiveVersion()`，以保证全局刷新同时刷新目录配置。
2. `switchPath()` 增加可选的强制配置读取参数；普通路由进入使用默认值，实例管理页手动刷新后使用强制值。
3. `removePath()` 除清除版本扫描缓存外，也调用 `invalidateActiveVersionCache(path)`，避免删掉后又重新添加同一路径时复用旧选择。
4. 现有 `selectVersion()` 的异步写入与读缓存更新须保持一致：若 `setActiveVersion()` 成功，缓存立即反映新值；若失败，不得伪造成功状态或阻塞用户后续重试。

### 4.3 `frontend/src/views/instances/ManageTab.vue`

1. 普通路径选择继续先扫描（由既有 `scanCache` 决定是否真正请求后端），随后调用默认 `switchPath(path)`。
2. `handleRefresh()` 完成 `scanCurrentPath(true)` 后，对当前路径调用强制 `switchPath()`，保证外部修改后的 `activeVersion` 在用户明确刷新时生效。
3. 编辑游戏路径时，对旧路径和新路径都失效版本扫描缓存与 `activeVersion` 缓存；路径名称只变、实际路径不变时不得额外清除有效缓存。

### 4.4 `frontend/src/views/Game.vue` 与后端日志

本次不新增首页的第二套实例列表缓存，也不修改后端扫描算法：前者会与既有 `instanceInstallApi.scanCache` 重复，后者并不能解释给出的 `read_ecl_config()` 日志。

保留后端 `read_ecl_config()` 的成功 DEBUG 日志，作为缓存未命中和手动刷新的诊断依据；不以降低日志级别掩盖重复 IPC。实施完成后，常规路由切换应不再触发该日志。

## 5. 测试计划与验收标准

新增或扩展 Vitest 用例，至少覆盖：

1. 同一标准化路径首次读取 `activeVersion` 调用一次 `game_config_get`，第二次普通读取不再调用后端。
2. 同一路径两次并发读取共用一个未完成请求；请求失败后，下次读取可以重试且不会把失败值写入缓存。
3. 返回空配置时缓存 `null`，普通重复读取仍只发起一次请求。
4. `setActiveVersion()`、`writeConfig()`、`patchConfig()` 成功后，普通读取返回新值且不产生额外读取请求；相同值写入仍保持现有去重行为。
5. `getActiveVersion(path, { force: true })`、`loadAll(true)` 和实例管理页手动刷新都产生一次新的配置读取，并替换缓存值。
6. 删除、替换游戏路径后旧路径缓存失效；同一路径的版本扫描失效逻辑保持现有行为。
7. 现有 `instanceInstallApi` 缓存测试与 `instanceStore` 路由场景回归通过，确认并未增加新的 `game_scan` 调用。

提交前执行：

```powershell
cd frontend
pnpm check
pnpm build
```

随后实际启动启动器，配置至少一个 Minecraft 路径，在首页与实例管理页之间连续切换五次，并记录结果：首次进入允许一次 `game_scan` 和一次 `game_config_get`；未编辑路径、未手动刷新、未收到版本变更事件的后续切换不应新增上述后端调用。点击刷新后应各出现一次，并仍正确恢复该路径的已选实例。

## 6. 风险、回滚与提交边界

主要风险是外部程序直接改写 `ecl.json` 后，前端会话缓存暂时显示旧的 `activeVersion`。该风险由“手动刷新或重启必定重新读取”限制；若后续确认需要实时感知外部配置改动，应另行设计低频文件监视或按文件版本戳校验，不能在本次优化中暗加高频轮询。

缓存逻辑仅位于前端内存，回滚时删除新增读缓存与相关调用参数即可，不会修改用户的 `ecl.json` 格式、实例目录或后端 IPC 协议。

实施完成后，文档、前端实现和测试作为同一项性能优化提交，提交信息使用：`perf: 减少路由切换时的实例配置读取`。不包含无关工作区文件或子模块指针。
