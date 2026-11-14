# 启动器数据目录重分类与去锁文件实施方案

日期：2026-11-13（Asia/Shanghai）。状态：用户已选择「目录方案 A 完整分组 + 锁方案 L1 纯进程内 + 任务中心不落盘」；本文待审阅，**尚未授权实施**。审阅通过并明确开始实施后，才修改生产代码、测试与文档。

## 1. 目标

1. 启动器数据目录按职责重新分类，顶层条目从 21（最终约 38）收敛到 7。
2. 插件相关目录收纳到单一 `plugins/` 根下，不再占用顶层。
3. 启动器不再在磁盘上产生任何 `.lock` 文件，并发保护改由进程内锁承担。
4. 任务中心不再落盘：删除 `operations/` 目录与全部任务历史文件，任务记录仅存于进程内存，审计信息转入 `logs/`。

不在本轮范围：数据目录名 `ECL_data` 本身（`ECL_DATA_PATH`、`--data-dir`、打包与文档均引用）、`frontend/` 与 `ECL/minecraft/game` 子仓库、用户 Minecraft 目录结构。

## 2. 现状基线

### 2.1 顶层条目清点

代码中写入 `data_path` 根的一级条目共 36 个，其中插件占 16 个：

| 类别 | 条目 |
| --- | --- |
| 配置 | `setting.json`、`notice.json` |
| 个人资产 | `wardrobe/`、`backgrounds/`（账户数据不在数据目录内，生产环境位于 `~/.ECL/accounts`，见 `ECL/application.py:412`） |
| 用户产物 | `downloads/`、`exports/` |
| 插件（16） | `plugins/`、`plugin_config/`、`plugin_settings_v2/`、`plugin_settings_schemas/`、`plugin_installations/`、`plugin_packages/`、`plugin_install_staging/`、`plugin_install_backups/`、`plugin_install_transactions/`、`plugin_file_selections/`、`plugin_deps/`、`plugin_cache/`、`plugin_state.json`、`plugin_resources.json`、`plugin_account_resources.json`、`plugin_java_resources.json` |
| 运行时 | `runtimes/java/`、`dependencies/authlib-injector/`、`java_cache.json`、`single_instance.json`、`dev_channel.json`、`.pending_debug_maintenance.json` |
| 可重建缓存 | `cache/`、`updates/`、`.pending_update.json`、`operations/`（`operations/` 本轮删除，见 2.3） |
| 临时与日志 | `temp/`、`logs/`、`maintenance-history.jsonl` |
| 维护备份 | `backups/debug-maintenance/` |

### 2.2 锁文件清点

`_exclusive_lock`（`ECL/plugins/environment_pool.py:71`）以 `msvcrt.locking` / `fcntl.flock` 做跨进程字节锁，共 11 处调用点：

| # | 锁文件位置 | 代码 | 外层进程内锁 |
| --- | --- | --- | --- |
| 1 | `plugin_resources.json.lock` | `resource_catalog.py:179` | 有（`self._lock`） |
| 2 | `plugin_account_resources.json.lock` | `account_resources.py:109` | 有（`self._lock`） |
| 3 | `plugin_java_resources.json.lock` | `java_resources.py:153` | 有（`self._lock`） |
| 4-5 | `plugin_installations/<name>.lock` | `installation_store.py:225`、`:243` | **无** |
| 6 | `plugin_deps/<key>.lock` | `environment_pool.py:303` | **无** |
| 7 | `plugin_packages/<name>.install.lock` | `packages.py:89` | 有（`_plugin_action_lock`） |
| 8 | `plugin_install_transactions/<name>.lock` | `install_transaction.py:205` | **无** |
| 9-12 | `plugin_packages/…/activation.lock` | `package_activation.py:137`、`:156`、`:252`、`:264` | **无** |
| 13 | `plugin_packages/<name>.lock` | `package_preparation.py:238` | **无** |
| 14 | `plugin_config/<key>.lock` | `settings.py:339` | **无** |
| 15 | `runtimes/java/.registry.lock` | `java/models.py:332` | 有（`self._thread_lock`） |

**关键结论**：方案 L1 不是单纯删除。7 处（#4-6、#8-13、#14）当前**只靠文件锁串行化**，删掉后必须补进程内锁，否则同一进程内的并发写入也失去保护。

### 2.3 任务中心持久化清点

`OperationManager._persist()`（`ECL/services/operations.py:591`）向 `operations/` 写两类文件，**没有任何清理逻辑**：

| 文件 | 内容 | 读取方 |
| --- | --- | --- |
| `operations/<operation_id>.json` | 非插件任务完整快照 `_payload()`，含 `details`（如 `downloadDirectory` 绝对路径）、`result`、`message`、`error` | 无 |
| `operations/plugin/<operation_id>.json` | 插件任务最小审计摘要 `_PluginJournal`（installation_id、content_digest、run_id、effect 等） | 无 |

已核实的三个事实：

- 前端 `applicationOperationStore`（`frontend/src/features/operations/stores/applicationOperationStore.ts`）为纯内存 Pinia，无 `localStorage` / `sessionStorage` / persist；`TaskQueuePanel` 的任务全部来自 `game:operation_progress` 事件流与主动查询，没有"列出历史任务"的接口，启动时也不从磁盘恢复。
- `get()` 的磁盘回落分支（`:748-761`）在实际调用链上不可达：所有调用方传入的标识都来自本进程 `submit` / `submit_owned` 的返回值（`ECL/java/manager.py:369,448`、`ECL/services/custom_downloads.py:316,338`、`ECL/api/workspace.py:388`、插件 operation_ref），内存中必然命中。
- `owned_snapshot()` 明确注释"不从历史文件恢复控制权"，`tests/test_plugin_operation_ownership.py::test_origin_snapshot_does_not_restore_from_journal` 即验证该行为；启动流程也不扫描 `operations/plugin/` 做崩溃对账（崩溃对账走 `plugin_install_transactions/`）。

结论：该持久化只产生数据、不产生功能。它在数据目录内无限堆积含用户绝对路径的 JSON，且 `plugin_file_write`、`plugin_download`、`plugin_file_delete`、`plugin_file_replace`、`plugin_instance_profile_patch`、`plugin_instance_profile_recovery`、`plugin_file_recovery` 等 22 种不走 UI 事件的内部操作同样各落一文件。

另有一处内存泄漏：`max_plugin_operations = 1024` 只统计 `origin is not None` 的插件任务，非插件任务在 `self._operations` 中永不移除（仅 `release_owner` 清理插件任务），长时间运行持续增长。

### 2.4 现有单实例门禁

`ECL/launcher.py:75` 在日志初始化前调用 `probe_running_instance`，命中即移交并退出。但该探测依赖 `single_instance.json` 存在，两个启动器同时冷启动时可同时通过探测——这是文件锁原本兜住的窄窗口，L1 后将不再兜。

## 3. 目标布局

```
ECL_data/
├── config/
│   ├── setting.json
│   └── notice.json
├── userdata/                     用户拥有、不可重建
│   ├── wardrobe/
│   ├── backgrounds/
│   ├── downloads/
│   ├── exports/
│   └── backups/debug-maintenance/
├── plugins/                      插件唯一根
│   ├── sources/<name>/           插件代码目录
│   ├── config/<name>.json
│   ├── settings/<installation_id>.json
│   ├── schemas/<installation_id>.json
│   ├── installations/<name>.json
│   ├── packages/<name>/pkg-<sha>/
│   ├── staging/<transaction_id>/
│   ├── backups/<transaction_id>/
│   ├── transactions/<name>.json
│   ├── selections/<installation_id>.json
│   ├── deps/<env_key>/
│   ├── cache/wheels/<sha256>
│   ├── state.json
│   ├── resources.json
│   ├── account_resources.json
│   └── java_resources.json
├── runtime/                      可重新获取的运行时组件与进程文件
│   ├── java/
│   ├── authlib-injector/
│   ├── java_cache.json
│   ├── single_instance.json
│   ├── dev_channel.json
│   └── .pending_debug_maintenance.json
├── cache/                        可重建
│   ├── remote-images/
│   └── updates/                  （含 .pending_update.json）
├── temp/
│   └── crash-analysis/
└── logs/                         （含 maintenance-history.jsonl）
```

设计说明：

- 顶层 7 个，语义互不重叠：配置 / 用户数据 / 插件 / 运行时 / 缓存 / 临时 / 日志。
- `plugins/sources/` 作为代码目录而非直接用 `plugins/<name>`，避免插件名与 `config`、`deps`、`packages` 等元数据子目录撞名。
- **只移动目录，不改文件名**（`setting.json`、`single_instance.json` 等保持原名），把迁移风险压到路径常量一层。

## 4. 改动清单

### 4.1 路径常量（生产代码，24 个文件）

| 旧 → 新 | 修改位置 |
| --- | --- |
| `setting.json` → `config/setting.json` | `ECL/utils/config.py:143`、`ECL/desktop/maintenance.py:36`、`ECL/services/game/modpack_export.py:111` |
| `notice.json` → `config/notice.json` | `ECL/services/updates.py:431`、`ECL/desktop/maintenance.py:36` |
| `accounts.json` → **不在本轮范围** | 生产环境位于 `~/.ECL/accounts`（`ECL/application.py:412`），不写入数据目录 |
| `wardrobe/` → `userdata/wardrobe/` | `ECL/services/account/wardrobe.py:65-68` |
| `backgrounds/` → `userdata/backgrounds/` | `ECL/api/files.py:464`、`ECL/services/game/screenshots.py:211` |
| `downloads/` → `userdata/downloads/` | `ECL/api/files.py:692`、`ECL/services/custom_downloads.py:268` |
| `exports/` → `userdata/exports/` | `ECL/api/system.py:396`、`ECL/services/game/crash/analyzer.py:649` |
| `plugins/` → `plugins/sources/` | `ECL/plugins/manager/base.py:231`、`ECL/desktop/dev_channel.py:734` |
| `plugin_config/` → `plugins/config/` | `ECL/plugins/manager/base.py:233-234`、`ECL/plugins/manager/storage.py:27,36`、`ECL/desktop/maintenance.py:37` |
| `plugin_settings_v2/` → `plugins/settings/` | `ECL/plugins/settings.py:73` |
| `plugin_settings_schemas/` → `plugins/schemas/` | `ECL/plugins/settings.py:73` |
| `plugin_installations/` → `plugins/installations/` | `ECL/plugins/installation_store.py:213` |
| `plugin_packages/` → `plugins/packages/` | `ECL/plugins/manager/packages.py:189`、`ECL/plugins/package_activation.py:86`、`ECL/plugins/package_preparation.py:191,230` |
| `plugin_install_staging/` → `plugins/staging/` | `ECL/plugins/manager/install_transaction.py:70,114` |
| `plugin_install_backups/` → `plugins/backups/` | `ECL/plugins/manager/install_transaction.py:71` |
| `plugin_install_transactions/` → `plugins/transactions/` | `ECL/plugins/manager/install_transaction.py:67,186`、`ECL/plugins/manager/lifecycle.py:315` |
| `plugin_file_selections/` → `plugins/selections/` | `ECL/plugins/settings.py:247` |
| `plugin_deps/` → `plugins/deps/` | `ECL/plugins/environment_pool.py:186` |
| `plugin_cache/wheels/` → `plugins/cache/wheels/` | `ECL/plugins/environment_pool.py:187,218` |
| `plugin_state.json` → `plugins/state.json` | `ECL/plugins/manager/base.py:235`、`ECL/plugins/manager/storage.py:45,58` |
| `plugin_resources.json` → `plugins/resources.json` | `ECL/plugins/resource_catalog.py:76` |
| `plugin_account_resources.json` → `plugins/account_resources.json` | `ECL/plugins/account_resources.py:55` |
| `plugin_java_resources.json` → `plugins/java_resources.json` | `ECL/plugins/java_resources.py:61` |
| `runtimes/java/` → `runtime/java/` | `ECL/java/models.py:278`（`storage_root` 的 LOCALAPPDATA 回退分支同步改 `runtime/java`） |
| `dependencies/authlib-injector/` → `runtime/authlib-injector/` | `ECL/services/account/authlib.py:70` |
| `java_cache.json` → `runtime/java_cache.json` | `ECL/services/game/base.py:187` |
| `updates/` → `cache/updates/` | `ECL/desktop/app_update.py:239` |
| `.pending_update.json` → `cache/updates/.pending_update.json` | `ECL/desktop/app_update.py:194,240,555` |
| `single_instance.json`、`dev_channel.json` → `runtime/` | `ECL/desktop/single_instance.py:36,62,260`、`ECL/desktop/dev_channel.py:131,166` |
| `.pending_debug_maintenance.json` → `runtime/.pending_debug_maintenance.json` | `ECL/desktop/maintenance.py:34,125,145` |
| `maintenance-history.jsonl` → `logs/maintenance-history.jsonl` | `ECL/desktop/maintenance.py:81` |
| `backups/debug-maintenance/` → `userdata/backups/debug-maintenance/` | `ECL/desktop/maintenance.py:77` |
| `operations/` → **删除，不落盘** | `ECL/services/operations.py:300`，见 4.4 |
| `cache/`、`temp/`、`logs/` | 不变 |

实施要求：

- 各模块统一在自己构造路径处拼装（`self._data_path / "plugins" / "deps"`），不新增跨模块共享的路径常量模块；同一路径只由所属模块定义一次，符合 AGENTS.md 1.4。
- 所有新目录在首次写入前 `mkdir(parents=True, exist_ok=True)`；启动期不预创建空目录，避免把 7 个顶层目录全变成空壳。
- `single_instance.json` 迁到 `runtime/` 后，`probe_running_instance` 需先确保 `runtime/` 存在再写；退出时按现有逻辑 `unlink`。

### 4.2 移除文件锁（11 处 → 0）

1. 删除 `ECL/plugins/environment_pool.py` 中的 `_open_lock_file`、`_exclusive_lock`、`lock_wait_seconds`，以及 `os`、`msvcrt`/`fcntl`、`monotonic`、`sleep`、`IO` 等随之失效的导入。
2. 按 2.2 表中标注「**无**」的 7 处补进程内锁：
   - `PluginInstallationStore`：新增 `self._thread_lock = Lock()`，包住 `ensure` / `retire`。
   - `PluginEnvironmentPool`：新增 `self._thread_lock = Lock()`，包住 `ensure` 的 ready 检查与安装段。
   - `PluginPackageActivationStore`：新增 `self._thread_lock = Lock()`，包住 `_write_activation` / `restore` / `set_enabled` / `uninstall`。
   - `PluginPackagePreparer.prepare`：复用所在管理器已有的 `_plugin_action_lock(name)`（与 `packages.py:90` 同一把），不新增第二把。
   - `PluginSettingsStore.update`：新增 `self._thread_lock = Lock()`。
   - `recover_plugin_install_transactions`（`install_transaction.py:205`）：由调用方 `PluginManagerBase.__init__` 的启动恢复流程持有管理器级锁；改为使用 `_plugin_action_lock(name)`。
   - `JavaRegistry.locked()`（`java/models.py:332`）：保留 `self._thread_lock`，删除文件锁与 `.registry.lock` 句柄处理。
3. 删除 `_exclusive_lock` 的全部 import（8 个文件）。
4. 调用点语义保持：`wait_seconds=2` 之类短预算在有界等待不再需要，改为直接获取进程内锁；原先「等待超时抛 `PluginDependencyError`」的分支随文件锁一起删除，不再保留无来源的异常。

### 4.3 配套位置

| 位置 | 改动 |
| --- | --- |
| `ECL/desktop/maintenance.py:34-37,77,81,125,145` | `reset_launcher_data` 目标改 `config/setting.json`、`config/notice.json`（`info_card.json` 已无生产代码写入，移出白名单）；`clear_plugins` 目标改为 `plugins/sources` 与 `plugins/config`，保留依赖缓存与安装记录；备份落 `userdata/backups/`，历史落 `logs/maintenance-history.jsonl`，待执行标记落 `runtime/` |
| `ECL/services/game/modpack_export.py:87-118` | 导出排除/屏蔽表补充新路径条目（`plugins`、`runtime`、`userdata` 等），保证整合包导出不夹带数据目录 |
| `ECL/desktop/dev_channel.py:734` | `pluginDir` 指向 `plugins/sources` |
| `.gitignore` | `/ECL_data/` 已覆盖，无需改动；可选清理根级遗留的 `/plugins/`、`/plugin_config/`、`/setting.json` |
| `docs/guides/plugin-development/development-mode.md` 等 8 篇文档 | 按新路径更新示例与说明 |

### 4.4 任务中心改为纯内存

删除 `operations/` 的全部落盘，任务记录只存在于进程内存。

`ECL/services/operations.py` 改动：

| 删除项 | 位置 | 说明 |
| --- | --- | --- |
| `_PluginJournal` | `:175-194` | 审计摘要模型，无读取方 |
| `_persist()` | `:591-632` | 唯一落盘入口，含 `operations/plugin/` 分支 |
| `get()` 磁盘回落 | `:748-761` | 未命中改为直接抛 `OPERATION_NOT_FOUND` |
| `_Operation.audit_pending` | `:222` | 无持久化后失去含义 |
| `OperationSnapshot.audit_pending` | `:170` | 同步从快照移除 |
| 落盘失败分支 | `:373,400-401,521-522,549-552,707` | `PLUGIN_OPERATION_PERSIST_FAILED` 不再可能发生 |

连带简化：

- `_execute()` 去掉 `is_initial_saved` 前置保存与相应失败路径。
- `_report_effect()` / `_finalize()` 不再因保存失败回滚 `effect`。
- `_retry_terminal()` / `owner_idle()` 只看 future 完成与 cleanup 结果。
- `OperationSnapshot.cleanup_pending` 的计算去掉 `audit_pending` 项。
- `__init__` 不再需要 `data_path` 参数，`ECL/application.py:436` 调用改为 `OperationManager(event_bus)`。

审计去向：插件任务进入终态时补一行结构化日志（`kind`、`phase`、`status`、`effect`、`error_code`、`installation_id`），经现有日志器写入 `logs/` 并随轮转，不含路径、请求参数与业务结果。

内存回收：

- 非插件终态任务超过 200 条时按 `created_at` 驱逐最旧，新增 `_evict_terminal_operations()`，在任务进入终态后调用。
- 插件任务保持 `max_plugin_operations = 1024` 与 `release_owner()` 释放语义，不参与 200 条驱逐，避免 run 存活期间 `owned_snapshot` 失效。

**与用户选项「只记 UI 可见的 11 种」的冲突（本节按保留全类型实施，待复核）**：`plugin_file_write`、`plugin_download`、`plugin_file_delete`、`plugin_file_replace`、`plugin_instance_profile_patch`、`plugin_instance_profile_recovery`、`plugin_file_recovery` 由 `download_api.py:96`、`file_api.py:547`、`instance_profiles.py:94` 经 `submit_owned` 提交，并把可查询的 `operation_ref` 返回给插件。若这些类型不建内存记录，插件调用 `operations.get` / `operations.cancel` 会得到 `PLUGIN_RESOURCE_NOT_FOUND`，属于破坏公共接口契约。

因此内存记录保留全部类型，仅「不发给前端」维持现状；插件任务内存由 `release_owner()` 在 run 关闭时释放，本身有界。用户的「不落盘」诉求已由本节完整满足。若仍要求这 22 种不建记录，需同步修改插件 Worker API 契约（对这些方法返回明确的不跟踪结果），应作为独立变更另行评审。

## 5. 数据处置

按 AGENTS.md 第 2 节，本阶段不承诺内部持久化格式兼容，但**不授权删除账户凭据与用户文件**。因此：

- **不编写自动迁移代码**。新旧布局不共存、不双读、不保留旧路径别名。
- 本地这份 `ECL_data` 当前不含账户数据（生产账户在 `~/.ECL/accounts`），`wardrobe/` 为空 → 可直接按新布局重建，仅需手工搬 `setting.json` 与 `notice.json`。
- 若用户已有真实账户数据，实施后按下表手工搬迁一次（文档形式给出，不进代码）：

```bash
cd ECL_data
mkdir -p config userdata runtime plugins/sources plugins/config plugins/settings \
         plugins/schemas plugins/installations plugins/packages plugins/staging \
         plugins/backups plugins/transactions plugins/selections plugins/deps \
         plugins/cache cache/updates
mv setting.json notice.json config/
mv wardrobe backgrounds downloads exports userdata/ 2>/dev/null
mv plugins/* plugins/sources/ 2>/dev/null
mv plugin_config/* plugins/config/ 2>/dev/null
mv plugin_deps/* plugins/deps/ 2>/dev/null
mv plugin_cache/wheels plugins/cache/ 2>/dev/null
mv plugin_installations/* plugins/installations/ 2>/dev/null
mv plugin_settings_schemas/* plugins/schemas/ 2>/dev/null
mv plugin_settings_v2/* plugins/settings/ 2>/dev/null
mv plugin_state.json plugins/state.json
mv plugin_resources.json plugins/resources.json
mv plugin_account_resources.json plugins/account_resources.json
mv plugin_java_resources.json plugins/java_resources.json
mv runtimes/java runtime/ && mv dependencies/authlib-injector runtime/
mv updates cache/
rm -rf operations
```

搬迁前先整目录复制一份 `ECL_data.bak`；确认新布局可用后再删旧目录。

## 6. 已知限制（L1 的取舍）

选择 L1 后，**同一数据目录被多个启动器进程同时写入时不再有跨进程互斥**。具体影响面：

- `single_instance: true`（默认）：`probe_running_instance` 覆盖绝大多数场景，仅剩两进程同时冷启动的毫秒级窗口。
- `single_instance: false`（用户主动多开）：两个实例同时安装插件或写设置时，可能出现后写覆盖、安装目录交叉、`plugin_packages` 半成品。
- 单进程内并发（插件 Worker 回调 + 前端 IPC）：**不受影响**，进程内锁覆盖完整。

本轮**不为该限制新增拦截或行为变更**（例如禁止多开时安装插件），仅在 `setting.json` 的 `single_instance` 关闭时记一条启动 warning 日志，说明多开下插件写入不保证互斥。如后续需要更强保证，再回到方案 L2（命名互斥体）。

任务中心改为纯内存后，以下行为变化属于预期：

- 启动器重启或 WebView 刷新后，已完成任务列表为空。此前磁盘上虽留有 JSON，但前端从不读取，用户可见行为不变。
- 超过 200 条的非插件终态任务会从内存驱逐，查询得到 `OPERATION_NOT_FOUND`。插件任务由 run 生命周期与 1024 上限管理。
- 崩溃后不再留下插件任务审计文件；审计信息在 `logs/` 中随轮转保留，长期归档依赖日志而非数据目录。

## 7. 测试计划

按 AGENTS.md 6.1，本变更属于「配置结构及持久化、文件写入及安装更新、进程与并发控制」→ **必须全量验证**，不能归入局部改动。

| 类型 | 内容 |
| --- | --- |
| 路径回归（新增 `tests/test_data_layout.py`） | 断言各模块在给定 `data_path` 下解析出的路径落在 7 个顶层目录内；断言不存在顶层 `plugin_*` 条目 |
| 锁回归（新增） | 断言插件安装、设置写入、Java 登记流程结束后，数据目录下不存在任何 `*.lock` 文件（`rglob("*.lock")` 为空，`requirements.lock` 除外） |
| 并发回归 | 同进程多线程并发 `ensure` / `update` / `prepare`，断言只写入一次、结果一致 |
| 任务中心去持久化（新增） | 触发自定义下载与插件任务后，断言 `data_path` 下不存在 `operations/` 目录；任务终态可通过 `get()` 查询；同一标识在未命中时抛 `OPERATION_NOT_FOUND` 而不是读盘 |
| 任务内存回收（新增） | 连续提交 201 个终态非插件任务，断言最旧的被驱逐、最近 200 个可查询；插件任务不受驱逐影响 |
| 现有用例迁移 | 47 处 `data_path / "plugins"` 改为 `plugins/sources`；`test_debug_maintenance.py:69-81` 的 `plugin_config` 断言改新名；`test_plugin_package_activation.py:96`、`test_packaged_plugin_directories.py:482` 等同步；`test_plugin_operation_ownership.py:88-92,157-158,236-245,322` 中针对 journal 文件的四处断言改为断言日志行或直接删除 |
| 首次运行 | 空 `data_path` 启动，确认按需创建、不产生锁文件、`single_instance.json` 在 `runtime/` 下且退出后清理 |

## 8. 验收命令

```bash
ruff check ECL tests
ruff format --check ECL tests
python -m pytest --tb=short -q
```

前端未改动，不执行 `pnpm check`；`dev_channel` 的 `pluginDir` 字段变化需在插件开发工具箱联调时确认（不涉及前端构建）。

## 9. 风险与回滚

| 风险 | 处理 |
| --- | --- |
| 遗漏某处硬编码路径，运行期新建顶层目录 | 用第 7 节路径回归用例兜底；实施后本地跑一次完整启动，`ls ECL_data` 验收顶层只有 7 项 |
| 漏补进程内锁导致同进程并发覆盖 | 第 7 节并发用例 + 逐点对照 2.2 表复核 |
| 用户已有账户数据未搬迁 | 第 5 节搬迁脚本；实施前要求用户确认是否存在真实账户 |
| 整合包导出夹带数据目录 | 更新 `modpack_export` 排除表并跑导出现有用例 |
| 插件任务审计信息丢失导致崩溃后无法追溯 | 终态结构化日志写入 `logs/`；在工程总结中说明审计位于日志而非数据目录 |
| 插件侧 `operations.get` 因类型裁剪失效 | 4.4 已按保留全类型实施，不裁剪；若后续要求裁剪须先改 Worker API 契约 |

回滚：变更集中在路径常量、锁封装与任务记录，`git revert` 单次提交即可；搬迁后的用户数据需手工回搬。

## 10. 实施顺序

1. 目录常量改造（24 文件）+ 测试路径迁移 → 跑受影响测试。
2. 移除文件锁 + 补 7 处进程内锁 → 跑插件与 Java 相关测试。
3. 任务中心去持久化（`operations.py` 删 `_persist` / `_PluginJournal` / 回落分支，补终态日志与 200 条驱逐）→ 跑 `tests/test_operations.py`、`tests/test_plugin_operation_ownership.py`。
4. 配套位置（maintenance / modpack_export / dev_channel / 文档）。
5. 新增路径、锁与任务回收回归用例。
6. 全量验证 + 本地启动验收 `ls ECL_data`。
7. 工程总结，提交 `refactor: 重分类启动器数据目录、移除文件锁与任务历史落盘`。

## 11. 实施与验证记录（2026-11-13）

按第 10 节顺序完成全部实施，未发现需要回滚的风险项。

| 项目 | 结果 |
| --- | --- |
| 路径常量改造 | 生产代码 33 个文件；`plugins/sources/` 作为代码目录，其余插件子目录平铺于 `plugins/` |
| 文件锁移除 | `_exclusive_lock`、`_open_lock_file`、`lock_wait_seconds` 全部删除；为安装存储、依赖池、活动指针、包准备器各补进程内锁，恢复流程改用 `_plugin_action_lock(name)` |
| 任务中心 | 删除 `_PluginJournal`、`_persist`、`get()` 磁盘回落、`audit_pending` 与 5 处落盘失败分支；新增终态审计日志与 200 条非插件终态任务驱逐 |
| 配套位置 | 维护白名单/备份/历史/标记、导出排除表、`dev_channel.pluginDir`、4 篇活文档同步更新 |
| 额外修正 | `supervisor._dependency_payload` 的依赖池根目录由 `parents[1]` 修正为 `parents[2]`，适配新增目录层级 |

验证：

- `ruff check ECL tests`：通过。
- `ruff format --check ECL tests`：381 个文件已格式化。
- `python -m pytest`（`tests/test_crash_analysis.py` 单独运行，其余全量一次运行）：2717 passed、45 skipped、1 failed；失败项 `test_plugin_network_api.py::test_total_deadline_stops_continuous_small_chunks` 属超时竞态，单独连续运行 3 次全部通过，与本次变更无调用关系。`test_crash_analysis.py` 单独运行 17 passed。
- 新增 `tests/test_data_layout.py`：断言各模块落点、无 `*.lock`、顶层仅 7 项。

环境说明：本机沙箱对单次删除超过 50 个条目会要求确认，pytest 结束时清理临时目录被拦截，导致默认临时根目录下的全量运行拿不到终端摘要。验证改用独立临时根目录（`TMP`/`TEMP`/`TMPDIR` 指向干净目录）取得完整摘要。
