# 后端职责划分：内核与 MC 的边界

- 日期：2026-11-14。
- 状态：待用户确认，未移动任何文件。
- 关联：[内核门面收敛方案](kernel-facade-convergence-plan-2026-11-13.md)、[ADR-005](../ADR-005-plugin-api-version-contract.md)。

## 1. 结论

后端按职责分三个域：

| 域 | 定义 | 判据 |
| --- | --- | --- |
| **内核** | 通用插件宿主运行时。不含任何 Minecraft 概念，去掉 MC 仍是完整可运行的插件平台 | 文件不引用版本、实例、Java、模组、整合包、游戏账户等 MC 概念 |
| **MC 模块** | 所有 Minecraft 语义。作为内核的第一个应用存在，未来可走内置模块加载 | 文件承担 MC 数据格式或启动链路 |
| **边界层** | 协议与适配。IPC 与 Broker 都是编译器，不含业务 | 只做参数校验与结果投影 |

判据用两条，机械判据初筛、语义判据处理争议：

1. **机械**：文件是否 import 或引用 MC 概念。可用静态检查，但会误伤"只做转发"的文件。
2. **语义**：去掉它，内核作为插件平台是否还能跑。用于处理 1 判不出的跨界文件。

## 2. 现状规模

| 目录 | 文件 | 行数 | 归属 |
| --- | --- | --- | --- |
| `ECL/api/` | 18 | 8,275 | 边界层（协议），handler 随域归位 |
| `ECL/foundation/`（已拆解） | — | — | 并入 `ECL/utils/`，`build_env.py` 迁 `ECL/utils/build_env.py`（构建注入配置归内核工具域） |
| `ECL/desktop/` | 8 | 2,802 | 内核（Tauri 宿主、单实例、更新） |
| `ECL/java/` | 6 | 2,193 | **MC** |
| `ECL/plugins/api/` | 14 | 3,353 | 内核（插件 SDK） |
| `ECL/plugins/` | 63 | 19,850 | **混合，需拆** |
| `ECL/headless/` | 5 | 420 | 内核 |
| `ECL/services/` | 50 | 24,471 | 混合，主体是 MC |
| `ECL/tooling/` | 3 | 530 | 内核（开发工具） |
| `ECL/utils/` | 19 | 4,004 | 混合，需拆少量 |
| 根文件 | 4 | 1,505 | 混合 |
| `ECL/minecraft/game/`（子模块） | — | 8,865 | **MC**，不主动改 |

合计约 7.6 万行，其中 MC 侧约 3.7 万行（`services/game` + `services/account` + `java` + `game` + 适配器）。

## 3. 内核清单

### 3.1 插件系统（主体）

| 归属 | 文件 |
| --- | --- |
| 管理 | `plugins/manager/`（9 文件 2,854 行） |
| 生命周期与装载 | `worker_service.py`、`plugin.py`、`framework.py`、`run_store.py`、`run_models.py`、`development.py` |
| 安装与包 | `installation_store.py`、`package_archive.py`、`package_preparation.py`、`package_activation.py` |
| 依赖与环境 | `dependencies.py`、`host_dependencies.py`、`dependency_lock.py`、`environment_pool.py`、`wheel_installation.py` |
| 设置 | `settings.py` |
| SDK | `plugin_api/` 全部 |

### 3.2 插件运行时

`plugins/runtime/` 下 16 个基础设施文件：

`broker.py`（路由机制）、`supervisor.py`、`process_scope.py`、`posix.py`、`windows.py`、`executable.py`、`network_transport.py`、`network.py`、`models.py`、`custom_ui.py`、`custom_ui_models.py`、`file_storage.py`、`file_api.py`、`download_api.py`、`operation_adapters.py`、`selected_files.py`

### 3.3 通用服务与工具

| 归属 | 文件 |
| --- | --- |
| 任务 | `services/operations.py` |
| 进程 | `services/processes.py` |
| 启动器更新 | `services/updates.py` |
| 文件系统 | `utils/` 的 `windows_file_handles.py`、`posix_file_handles.py`、`posix_file_transactions.py`、`posix_file_native.py`、`file_handles.py`、`files.py`、`file_models.py` |
| 基础设施 | `utils/logging.py`、`errors.py`、`environment.py`、`config.py`、`network.py`、`ui_locale.py`、`operation_logging.py` |
| 应用骨架 | `application.py`、`bootstrap.py`、`launcher.py`、`host/`、`foundation/`、`runtime/`、`tooling/` |

## 4. MC 模块清单

### 4.1 整体归属

`ECL/minecraft/game/`（Core 子模块，8,865 行）、`ECL/java/`（2,193 行）、`ECL/minecraft/services/game/`（32 文件 18,452 行）、`ECL/minecraft/services/account/`（7 文件 3,854 行）、`services/custom_downloads.py`（476 行）。

### 4.2 需要从 `plugins/` 迁出的 11 个适配器

`runtime/` 下的 `game_install.py`、`game_launch.py`、`game_queries.py`、`game_models.py`、`game_resources.py`、`modpacks.py`、`instance_profiles.py`、`resource_queries.py`、`resources.py`、`java_api.py`、`public_queries.py`

### 4.3 需要从 `plugins/` 根迁出的 7 个声明与扩展

| 文件 | 职责 |
| --- | --- |
| `resource_catalog.py` | MC 资源目录策略 |
| `instance_compat.py` | 实例兼容扩展 |
| `java_resources.py` | Java 资源清单 |
| `account_resources.py` | 账户资源清单 |
| `auth_providers.py` | 认证提供者（LittleSkin 等） |
| `launch_hooks.py` | 启动钩子 |
| `crash_extensions.py` | 崩溃分析扩展 |

`network_targets.py` 不在此列：实测其内容是 `NetworkOrigin` / `NetworkTargetPolicy`，做精确站点身份、URL 校验与公网地址准入，是通用网络机制，**留内核**。

### 4.4 需要从 `utils/` 迁出的 4 个

`instance_reading.py`、`instance_shortcuts.py`、`account_reading.py`、`download_settings.py`。

## 5. 跨界文件与处理

这五处是划分的真正难点，不是靠移动目录能解决的。

### 5.1 `plugins/runtime/broker.py`

现在它既是路由机制，又内置了全部 MC 方法名的字符串路由。机制归内核，方法清单归 MC。

处理：broker 只保留"查表 + 校验 + 授权"的机制，方法表改为由各域注册。MC 模块启动时注册自己的方法表。

### 5.2 `ECL/api/`

18 个文件是协议边界，整体归内核（协议骨架不动）。按域分布：`game.py`、`java.py`、`mods.py`、`workspace.py`、`accounts.py` 是 MC 域的 handler，其实现要改为调用 MC 模块，而不是直接持有 MC 服务；`windows.py`（窗口控制）、`system.py`、`settings.py`、`plugins.py`、`files.py` 属内核域。

前端 237 个命令的名称、参数、返回结构一律不变。

### 5.3 `ECL/cli.py`

含 `--launch <instance>` 这类 MC 启动参数。内核保留通用 CLI 骨架，MC 相关的子命令由 MC 模块注册。

### 5.4 `ECL/utils/config.py`

配置机制归内核，MC 相关字段归 MC。实测比预想混杂得更彻底：`DEFAULT_CONFIG` 字典里通用项（`single_instance`）与 MC 项（`minecraft_paths`、`java_path`、`java_auto`、`instance_isolation_policy`、游戏窗口标题模板）平铺在一起，同一份默认值无法按域拆开。

处理：内核保留配置的读写机制与通用字段；MC 字段改为由 MC 模块注册后再并入默认值，不再由内核预置。

### 5.5 `plugins/network_targets.py`（核实后不迁）

原本按"下载站点白名单"判断，实测其内容是 `NetworkOrigin` 与 `NetworkTargetPolicy`：精确协议/域名/端口的站点身份、URL 校验、排除特殊用途地址的公网策略。这是通用网络机制，与 MC 无关，**整体留内核**，本节不再作为跨界项。

## 6. 划分后的形态

```
ECL/
  kernel/                 新增容器，装现有内核代码（可先不动目录，只建边界声明）
  mc/                     MC 模块，聚合 services/game + java + 适配器
  api/                    协议边界层，前端唯一入口
  plugin_api/             插件 SDK
  plugins/                插件系统（迁走 MC 部分后只剩基础设施）
  services/               通用服务（operations / processes / updates）
  utils/                  通用工具
```

第一步不新建目录也能成立：先在 `AGENTS.md` 里声明三个域的归属清单，用静态检查守住边界，移动可以分批做。

## 7. 落地顺序

| 步 | 内容 | 风险 |
| --- | --- | --- |
| 1 | 声明归属清单，加静态检查（内核文件不得 import MC 概念） | 低，不改代码 |
| 2 | 迁 `plugins/` 的 18 个 MC 文件到 MC 侧，只改 import | 中，便于验证的机械迁移 |
| 3 | broker 方法表外移，改为按域注册 | 中，需等价性测试 |
| 4 | `api/` handler 改为经 MC 模块调用 | 高，涉及 168 个 MC 命令 |
| 5 | 收敛目录结构（是否新建 `kernel/`、`mc/`） | 低，纯移动 |

建议先做 1、2。第 3 步与门面收敛方案的批 2 是同一件事，可以合并。第 4 步只有在前面都稳定后才动。

## 8. 未决

1. `java/` 全部归 MC，还是"Java 安装机制"在内核、"MC 所需 Java 版本选择"在 MC。当前按全部归 MC 处理。
2. `services/account/` 归 MC。凭据加密存储机制建议留在内核的 `utils`，账户语义归 MC。
3. 是否新建 `kernel/` 与 `mc/` 目录，还是只在逻辑上划分。建议第一步只做逻辑划分。
