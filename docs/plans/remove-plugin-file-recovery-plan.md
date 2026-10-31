# 移除插件文件恢复能力（事务降级）实施文档

> 状态：已实施（2026-11-01）。

## 1. 背景与目标

「文件恢复」是插件覆盖文件时两步提交的产物：先备份旧文件、再发布新文件，中断后留下待恢复记录并阻塞该路径。
用户认为该入口平时无用，决定移除整套机制。

目标：删除恢复入口、登记、阻塞与 4 个 IPC 命令；把写入事务降级为**暂存 + 原子替换**。
保留原子性（不会出现半写文件），放弃可回滚性（旧内容不再保留）。

## 2. 关键语义决策

1. **不做「直接覆盖」，做「原子替换」。** `MoveFileEx(temp, target, MOVEFILE_REPLACE_EXISTING)` 在同卷 NTFS 上是原子的：
   崩溃发生在替换前，目标保持旧内容；发生在替换后，目标是新内容。任何时刻都不会读到半个文件。
   这比「就地写目标文件」安全得多，且不需要备份记录。
2. **失败即失败。** 目标被外部独占、卷被拔出等场景直接返回 `PLUGIN_STORAGE_FAILED`，不再登记待恢复、不再阻塞后续写入。
3. **暂存残留由启动清理。** `.upload-*` 暂存文件在进程崩溃后可能残留，启动扫描时直接删除（它们从不被当作数据）。
4. **已存在的历史残留按用户选择处理**（见第 6 节待确认项）。

## 3. 现状规模

| 部分 | 体量 |
|:---|:---|
| `ECL/plugins/runtime/file_replacement.py` | 691 行，整文件删除 |
| `ECL/services/game/instance_profile_transactions.py` | 905 行，改为原子替换 |
| 后端引用恢复的文件 | 18 个 |
| IPC 命令 | `plugin_file_recoveries_list`、`plugin_file_recovery_resolve`、`plugin_instance_profile_recoveries_list`、`plugin_instance_profile_recovery_resolve` |
| 前端 | 弹窗 240 行 + API + 校验 + 6 语言文案 |
| 测试 | `test_plugin_file_replace.py`(17)、`test_instance_profile_models.py`(7)、`test_plugin_permission_ipc.py`(6)、`test_api_models.py`(17) |

## 4. 改动点明细

### 4.1 后端

1. 删除 `ECL/plugins/runtime/file_replacement.py`。
2. `ECL/plugins/runtime/file_storage.py`：移除 `self.replacements` 及其 `discover/blocks/has_pending/for_upload/cleanup` 调用；
   `commit_upload` 改走「核对原文件 etag + SHA-256 → 原子替换 → 归还预留」；`close()` 不再等待 `has_pending`。
3. `ECL/utils/windows_file_handles.py`：为已打开的暂存句柄提供「以替换方式发布」的原生调用；
   保留现有的身份与边界校验，不放开路径规则。
4. `ECL/services/game/instance_profile_transactions.py`：删除 `ReplacementRecord` 等价物与 `resolve/list`；
   保留 JSON 结构校验、字段白名单、64 KiB 上限与 etag 冲突检测；提交改为原子替换。
5. `ECL/api/plugins.py`、`ECL/api/registry.py`、`ECL/api/models.py`、`ECL/api/operation_logging.py`：
   删除 4 个命令、对应 DTO 与日志规格。
6. `ECL/api/worker_service.py`、`ECL/services/operations.py`：删除 `plugin_file_recovery` 任务类型与恢复分支。
7. 错误码：`PLUGIN_FILE_RECOVERY_REQUIRED`、`PLUGIN_FILE_CHANGED` 若仅服务于恢复则下线，并清查引用。

### 4.2 前端

8. 删除 `features/plugins/components/PluginFileRecoveryModal.vue` 与其测试。
9. 删除 `features/plugins/api/pluginFileRecoveryValidation.ts` 与其测试。
10. `features/plugins/api/pluginManagementApi.ts` 移除 4 个方法；`types/plugins.ts`、`types/api.ts` 移除相关类型。
11. `views/Plugins.vue` 移除工具栏按钮与弹窗挂载。
12. 六个语言包删除 `plugins.fileRecovery*`、`plugins.profileRecovery*` 键。

### 4.3 文档与示例

13. `docs/protocols/eclplugin-package-format.md` 第 150—210 行改写为原子替换语义，删除恢复入口说明。
14. `examples/plugins/worker_file_replace/`、`examples/plugins/worker_instance_profile/` 的 README 与代码同步。
15. `docs/designs/plugin-worker-api-v2-implementation-design.md`、`docs/plans/plugin-three-batch-implementation-plan-2026-10-31.md`
    标注该能力已移除，避免后续按旧设计实现。

### 4.4 测试

16. `tests/test_plugin_file_replace.py`：删除恢复用例，保留覆盖写入主流程并改为断言原子替换（替换成功即旧内容消失、无 `.replace-*` 残留）。
17. `tests/test_instance_profile_models.py`、`tests/test_api_models.py`、`tests/test_plugin_permission_ipc.py`：清理恢复用例与 DTO 断言。
18. 新增用例：替换过程中断后目标仍是旧内容；启动清理删除 `.upload-*` 残留；目标被外部占用时返回 `PLUGIN_STORAGE_FAILED` 且不阻塞后续写入。

## 5. 实施顺序

1. 后端事务降级（4.1 的 1—4），跑 `pytest tests/test_plugin_file_replace.py` 验证主流程。
2. 后端命令与 DTO 下线（4.1 的 5—7），跑全量 `pytest`。
3. 前端入口与类型下线（4.2），跑 `pnpm check`。
4. 文档与示例同步（4.3），单独 `docs` 提交。
5. 手工验证：插件覆盖写入、实例资料修改、任务管理器强杀进程后检查目标文件与残留。

## 6. 已确认的处理方式

1. **历史残留：保留不读。** 老版本留下的 `plugin_file_replacements` 记录目录与 `.replace-*` 备份继续留在磁盘上，
   不再读取、不再阻塞路径、不新增清理流程；不删除任何用户文件。
2. **不做过渡提示。** 入口直接消失，插件页不出现「能力已移除」之类的提示。

## 7. 风险

- 触及插件文件写入与实例资料提交的核心不变量，回归面覆盖写入、权限、任务与存储关闭流程。
- 一旦替换成功，旧内容不可找回；这是本方案有意接受的代价。
- `windows_file_handles` 的原子替换需在真实 NTFS 上验证，不能只靠单元测试。

## 8. 实施结果

- 后端：删除 `file_replacement.py`（691 行）与 `tests/instance_profile_crash_helper.py`；`file_storage`/`file_api` 改为「核对版本 → 释放独占句柄 → 原子替换」；
  `instance_profile_transactions` 由 905 行收缩到约 360 行，保留版本核对、字段白名单与 64 KiB 上限，新增 `has_active` 表示在途占用；
  `windows_file_handles.FileHandle` 新增 `replace()`（`_rename(replace=True)`）。
- IPC：下线 4 个恢复命令及其 DTO、日志规格；`PluginFileRecoveryResolve` 等模型删除。
- 前端：删除恢复弹窗与校验模块及其测试，`pluginManagementApi` 去掉 4 个方法，类型与 6 语言文案同步移除。
- 测试：`test_plugin_file_replace.py` 保留 20 个用例（含原子替换失败时旧内容不变）；实例资料与权限 IPC 的恢复用例删除。
- 验证：`ruff check` / `ruff format` 通过；`pytest` 2599 passed；前端 `pnpm check` 189 文件 1153 用例通过。
- 已知遗留（与本方案无关）：`test_plugin_host_broker`、`test_operation_logging`、`test_frontend_api_config`、`test_application_context`、`test_plugin_game_install` 的失败由更早的插件能力提交（9093a15 / 3751573 / 922e14d）引入，本次未处理。
