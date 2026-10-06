# 后端分层重构实施计划：services 减负与 Core 归位

> 状态：**方案已确认，待实施**（三个决策点已由用户确认，见 §8）
> 适用仓库：主仓库 `EuoraCraft-Launcher` + 子模块 `ECL/game`（EuoraCraft-Launcher.Core）
> 依据流程：`AGENTS.md` §0 功能变更固定工作流

---

## 1. 背景与目标

### 1.1 现状数据（实测）

主仓库 `ECL/` 的 Python（剔除 `frontend`、`ECL/game`、`ECL/services/florolding` 三个子模块）合计 **38305 行**，分层如下：

| 层 | 行数 | 文件 | 占比 |
|---|---:|---:|---:|
| **services/** | **22818** | **65** | **59.6%** |
| api/ | 6579 | 18 | 17.2% |
| plugins/ | 5910 | 27 | 15.4% |
| utils/ | 1549 | 10 | 4.0% |
| 根级（main / cli / launcher / application） | 1050 | 4 | 2.7% |
| adapters / common / events | 397 | 8 | 1.0% |

`services/` 内部：

| 子域 | 行数 | 文件 |
|---|---:|---:|
| `services/game/` | 14041 | 37 |
| 联机（`connector.py` + `connector_nodes.py`） | 1105 | 2 |
| 其余散装 | 7672 | 26 |

### 1.2 三个问题（彼此正交，不能用一个手段解决）

1. **重**：`services/` 是"不是 api、不是 utils 就丢进来"的筐，占主仓库近六成代码，且缺少准入标准。
2. **错位**：至少两个模块放错了层——`utils/nbt.py` 是 Minecraft 格式解析却在通用工具层；`services/java/` 是 JVM 运行时管理却在领域服务层。
3. **散**：过度切分带来的认知跳跃成本。但**拆解不治"重"，只加重"散"**——把 14041 行的 `services/game/` 拆成 74 个文件，它仍是同一个无边界的大域。

### 1.3 目标

- **减重**：让 `services/` 只保留"领域编排"，把原语下沉、把基础设施移出。
- **归位**：修正放错层的模块。
- **收敛**：把过度切碎的模块合回去，而不是继续拆。
- **不破坏**：`ECL/api` 有 18 个模块架在 services 上，对外契约（IPC 命令名、错误码）必须保持不变。

---

## 2. 设计原则（本次重构的判据）

### 2.1 分层判据（谁进 Core、谁留主仓库）

> **Core 收"游戏与运行时原语"；主仓库收"编排与产品策略"。**

具体判据——一个模块只要依赖以下任一项，就属于**编排**，不进 Core：

- `ECL.events.EventBus`（宿主事件总线）
- `ECL.services.operations`（长任务 `OperationContext` / `OperationManager`）
- `ECL.utils.config.ConfigStore` / `ECL.utils.logging`（宿主配置与日志）
- `ECL.plugins.*`（插件注册表）
- `ECL.api.*`（IPC 契约）

反之，只依赖标准库 + 纯数据模型的模块，才够格下沉 Core。

### 2.2 文件结构颗粒度规则（防"拆太散"）

**先区分两件常被混为一谈的事**：

- **拆散**：把一个内聚的职责切成多份 → 只增加认知跳跃成本，是坏结果。
- **分组**：把一堆**互不相关**的平铺文件归入有意义的子包 → 建立结构，是好结果。

`ECL/services/` 根目录原有 **20 个互不相关的平铺文件**（`accounts`、`authlib`、`wardrobe`、`connector`、`updates`、`app_update`、`dev_channel`、`themes`…），这才是"结构不合理"——它需要**分组**，不是拆分。

| 判据 | 动作 |
|---|---|
| 一个文件有多个独立变更理由，且 > 800 行 | 拆，但**拆成 3~5 个以内**，按变更理由、不按类 |
| 一个文件 < 150 行，且无独立变更理由 | 合并进宿主 |
| 同级目录 > 15 个文件 | 需要分组 |
| 同级目录 < 3 个文件且总量很小 | 该层级多余，应合并回去 |

**本方案的两条自我约束**：

1. **不做"按类逐文件拆"**。`api/models.py`（1146 行 / 109 个模型）即使要拆，也是按域归为 4~5 个，不是 109 个；本次不拆（见 §10）。
2. **`utils/` 只收纯函数式无状态工具**。有状态的配置、日志、环境不得进 `utils/`，否则 services 的重量只是被转嫁，`utils/` 会变成第二个筐。

---

## 3. 调研结论

### 3.1 Core 的边界与唯一反向依赖

Core（`ECL/game`，**4482 行**生产代码）现有能力：下载、文件校验、启动命令构建、版本清单、实例进程管理、认证、Java 扫描、版本搜索。

- **唯一反向依赖**：`ECL/game/Core/MicrosoftAuth.py:13` → `from ECL.utils.files import atomic_write_text`。
- Core **没有** `# ===` 标准文件头（12 个生产文件全部缺失），docstring 风格为老式中文多行，另有 `# ---------- xxx ----------` 装饰分隔线、`type()` 代替 `isinstance()`、`os.path` + `open()` 等与 `AGENTS.md` §3 相冲突的写法。
- Core **没有**独立 `pyproject.toml` / ruff 配置 / CI 测试工作流；主仓库 `pyproject.toml` 的 ruff `exclude` 和 pytest `testpaths=["tests"]` 都把 `ECL/game` 排除在外。**这意味着下沉代码会脱离现有质量门禁。**

### 3.2 `services/game/` 与相关模块的归属划分

| 类别 | 模块 | 行数 | 够格进 Core |
|---|---|---:|---|
| **纯原语** | `instance_health`、`mod_versions`、`mod_metadata`、`instance_options`、`world_seeds`、`resource_files` | ~1565 | 是（下沉 Core） |
| **JVM 运行时**（放错位置） | `java/`(8) | 1720 | 否——决策 1=B：整体提为顶层 `ECL/java/`，见 §4.2.4 |
| **产品策略** | `modpack`、`resources`、`resource_search`、`mods`、`worlds`、`servers`、`screenshots`、`schematics`、`profiles`、`instance_profiles`、`scan`、`install`、`launch`、`catalog`、`workspace`、`launch_settings`、`launch_target`、`instance_compat`、`download_sources`、`crash/`(6) | ~10000 | 否 |
| **应用基础设施** | `app_update`、`single_instance`、`dev_channel`、`info_card`、`background_media`、`maintenance`、`instance_shortcuts`、`processes`、`frontend_events`、`themes` | ~2798 | 否，且本不该在 `services/` |
| **皮肤域**（放错位置） | `skin_avatar` | 96 | 否，但应归入 `services/account/`（与 `wardrobe` 同域），而非 `utils/` |

### 3.3 兼容 shim 的实测（比预想的好）

| 文件 | 实情 | 处理 |
|---|---|---|
| `services/game/crash_analysis.py`（16 行） | 纯 shim，**1 个测试**在用（`tests/test_plugin_crash_extensions.py:17`） | 可删，改 1 处导入 |
| `services/game/operations.py`（19 行） | `GameOperationManager` 别名是**死代码**；但 `OperationContext` 被 `modpack`/`resources`/`workspace`/`worlds` 4 个模块捷径导入 | 可删，改 4 处导入 |
| `plugins/framework.py`（11 行） | **不是 shim**，有 4 个真实使用者 | **不动** |

**结论：真正能清的 shim 只有 2 个（35 行），收益很小。**"散"的主要成本在顶层微型层与放错层的模块，不在 shim。

### 3.4 ⚠️ 对前期判断的修正：`services/java/` 并非零依赖

前期调研中曾据一次参数失效的扫描得出"`services/java/` 零 ECL 依赖"，**该结论错误**。修正后的真实依赖：

| 文件 | 依赖的主仓库设施 |
|---|---|
| `manager.py` | `ECL.events.EventBus`、`ECL.services.operations`(OperationContext/OperationManager)、**`ECL.services.game.launch_settings`**、`ECL.utils.logging`、`ECL.game` |
| `installer.py` | `ECL.services.operations`、`ECL.utils.network.download_proxy_url`、`ECL.game` |
| `lifecycle.py` | `ECL.utils.atomic_write_text`、`.registry` |
| `registry.py` | `ECL.utils.atomic_write_text`、`ECL.game` |
| `models.py` | `ECL.utils.errors.GameServiceError`、`ECL.game` |
| `catalog.py` / `archives.py` | 仅 `.models` |

**关键点**：`manager.py` → `services/game/launch_settings` 是一处**反向依赖**；而 `services/game/base.py` → `services/java` 是正向依赖。二者包级互相牵扯。

**因此"`services/java/` 整体下沉 Core"不可行**——那会迫使 Core 吸收 `EventBus` 与 `OperationManager`，等于让 Core 变成启动器。**决策 1 已定为方案 B：`java/` 整体留在主仓库，提为顶层 `ECL/java/` 层（见 §4.2.4）**，从而避免跨仓库碎片。

---

## 4. 改动清单

### 4.1 删除兼容 shim（2 个）

| 动作 | 影响面 |
|---|---|
| 删除 `services/game/crash_analysis.py` | 改 `tests/test_plugin_crash_extensions.py:17` → `from ECL.services.game.crash.analyzer import CrashAnalyzer` |
| 删除 `services/game/operations.py` | 改 `modpack.py:39`、`resources.py:65`、`workspace.py:41`、`worlds.py:46` → `from ECL.services.operations import OperationContext` |

`ECL/plugins/framework.py` 保留。

### 4.2 下沉 Core

#### 4.2.1 前置：Core 需要先具备的基础设施

| 新增项 | 理由 |
|---|---|
| 公共异常基类（如 `ECL/game/errors.py` 的 `CoreError`，带 `error_code` 字段） | 待下沉模块现依赖 `ECL.utils.errors.GameServiceError`（带稳定错误码）；Core 现仅有 `MicrosoftAuth` 内的私有 `BException` |
| `atomic_write_text`（从 `ECL/utils/files.py` 迁入，该文件纯标准库） | 消除 Core 唯一反向依赖，并供下沉模块使用 |
| Core 侧 `ruff` 配置 + 测试工作流 | 否则下沉代码脱离质量门禁（见 §3.1） |
| 补齐 `# ===` 标准文件头 | 下沉文件按 `AGENTS.md` §3.2 写，Core 存量文件顺带收敛（不跨模块大规模重写） |

#### 4.2.2 批次 1：无阻塞下沉（1246 行）

| 模块 | 行数 | 现状依赖 |
|---|---:|---|
| `ECL/utils/nbt.py` | 405 | **纯标准库**（gzip / io / struct / pathlib） |
| `services/game/instance_health.py` | 170 | **零 ECL 依赖** |
| `services/game/mod_versions.py` | 155 | **零 ECL 依赖** |
| `services/game/mod_metadata.py` | 516 | 仅 `.mod_versions` |

`nbt.py` 的迁移影响面：6 个生产模块（`resources`、`resource_files`、`schematics`、`servers`、`worlds`、`world_seeds`）+ 4 个测试文件的导入路径。

#### 4.2.3 批次 2：需依赖解耦后下沉（724 行）

| 模块 | 行数 | 需解决 |
|---|---:|---|
| `services/game/instance_options.py` | 160 | `ECL.utils.atomic_write_text` + `GameServiceError` → 改用 Core 版 |
| `services/game/world_seeds.py` | 182 | `ECL.utils.nbt`（批次 1 后已在 Core）+ `GameServiceError` |
| `services/game/resource_files.py` | 382 | 同上 |

#### 4.2.4 批次 3：`java/` 提为顶层 `ECL/java/`（已确认：决策 1 = B）

**不下沉 Core，整体留在主仓库，但从 `services/java/` 提升为顶层 `ECL/java/` 层**（与 `services/` 平级）。

理由：`services/java/` 是 JVM 运行时管理，既不属于"游戏领域服务"（放 `services/` 错位），也不能进 Core（会把 `EventBus` / `OperationManager` 拖进 Core，见 §3.4）。提升为顶层层后语义归位，且**避免跨仓库碎片**。

| 动作 | 说明 |
|---|---|
| 移动 | `ECL/services/java/` → `ECL/java/`（8 文件 1720 行） |
| 收敛 | 8 文件 → 3~4 个：`models.py` + `registry.py` 合并；`catalog.py` + `archives.py` 合并；`manager.py`（含 installer/lifecycle 的编排）保留 |
| 解耦 | 消除 `java/manager.py` → `services/game/launch_settings` 的反向依赖：`InstanceLaunchOverrides` 若属启动设置契约，随 `launch_settings` 一并提升或改为注入 |
| 改导入 | `api/java.py`、`services/game/base.py`、`application.py`、`tests/test_java_manager.py` 等 |

> 注：`services/game/base.py` → `ECL.java` 的依赖方向变为 `services` → `java`，与 `java` → `services` 的反向依赖需一并清理，确保层级单向。

### 4.3 主仓库内收敛（治"散"）

| 动作 | 说明 | 把握 |
|---|---|---|
| `services/game/catalog.py`（122 行）并入 `scan.py` | 其 `CatalogCoordinator` 仅被 `services/game/__init__.py` 引用，无其他消费者 | 中，需实施时确认 |
| `services/game/launch_target.py`（109 行）并入 `launch.py` | 仅被 `ECL/api/bridge.py` 与 1 个测试引用，语义属启动流程 | 中，需改 `bridge.py` 导入 |
| `services/java/` 8 文件 → 收敛为 3~4 个（已确认：决策 1 = B） | 单子系统 8 个文件粒度过细，`models`+`registry`、`catalog`+`archives` 可合并 | 高 |
| Core `Utils/`（2 文件）并入 `Core/` | 2 个文件单独成子包，粒度过细 | 高 |
| 顶层微型层 `adapters/` + `common/` + `events/` 合并为 `ECL/foundation/` | 8 文件 399 行 → 6 文件；3 个顶层包 → 1 个（已确认：决策 2 = 是） | 高 |

**明确不动的**（经核查为合理边界）：`profiles.py` / `instance_profiles.py`（协调器 vs 存储策略）、`crash/` 子包 6 文件、`workspace.py` / `base.py`（跨模块共享基础设施）、`launch_settings.py`、`instance_compat.py`、`version_stats.py`、`download_sources.py`。

### 4.4 应用基础设施移出 `services/`

⚠️ **命名冲突**：`ECL/launcher.py` 已存在，新层**不能**叫 `launcher/`。建议 **`ECL/host/`**（宿主层）。

| 去向 | 模块 | 行数 |
|---|---:|---:|
| **→ `ECL/host/`** | `app_update`(578)、`dev_channel`(805)、`single_instance`(255)、`info_card`(232)、`background_media`(209)、`maintenance`(171)、`frontend_events`(153) | 2403 |
| **→ `ECL/utils/`**（纯无状态工具） | `instance_shortcuts`(191) | 191 |
| **→ `ECL/foundation/`**（纯常量） | `themes`(20) | 20 |
| **→ `services/account/`**（分组，见下） | `skin_avatar`(96) | 96 |
| **暂留 `services/`** | `processes.py`(184) — 依赖 `ECL.game.InstancesManager`，需先确认移入 `host/` 不会造成 host→game 反向耦合 | 184 |

**新增 `services/account/` 分组**——这是 §2.2"分组而非拆散"的样板：把 `services/` 根目录中同属**账户与皮肤域**的 4 个平铺文件收敛为一个子包。

| 文件 | 行数 |
|---|---:|
| `accounts.py` | 1120 |
| `authlib.py` | 542 |
| `wardrobe.py` | 298 |
| `skin_avatar.py`（**修正**：原计划归 `utils/`，实际与 `wardrobe` 同属皮肤域） | 96 |
| **合计** | **2056** |

依赖关系已确认：`dev_channel` → `frontend_events` 是唯一内部依赖；`processes` 是唯一与游戏域耦合的模块；这些模块**无公共基类**，均通过 `ECL/api/registry.py` 注册 IPC handler。

---

## 5. 实施步骤（有序，每步独立可验证）

1. **建基线**：`ruff check ECL tests` + `pytest` + `cd frontend && pnpm check`，记录当前通过状态。
2. **删 shim**（§4.1）：改 5 处导入 → 跑测试 → 提交 `refactor: 移除游戏服务兼容导入门面`。
3. **Core 前置设施**（§4.2.1）：在 Core 仓库内建 `CoreError`、迁入 `atomic_write_text`、加 ruff 配置与测试工作流、解除唯一反向依赖 → **在子模块仓库内独立提交并推送**（`AGENTS.md` §5）。
4. **批次 1 下沉**（§4.2.2）：改导入路径 → 主仓库测试全绿 → 提交。
5. **批次 2 下沉**（§4.2.3）：同上。
6. **`java/` 提为顶层 `ECL/java/`**（§4.2.4）：移动整包 → 清理与 `services/game` 的双向依赖 → 收敛为 3~4 个文件 → 改导入。
7. **顶层微型层合并**（§4.3）：`adapters/` + `common/` + `events/` → `ECL/foundation/`。⚠️ `from ECL.events import EventBus` 有 **31 处**引用，全仓共约 45 处导入需同步修改。
8. **主仓库内收敛**（§4.3）：`catalog.py` 并入 `scan.py`、`launch_target.py` 并入 `launch.py`，逐个合并、每合并一次跑一次测试。
9. **应用基础设施移出**（§4.4）：建 `ECL/host/` → 迁 7 个模块 → 建 `services/account/` 并归入 4 个账户皮肤模块 → `themes` 进 `foundation/` → 改 `application.py` / `api/*` 导入。
10. **收尾**：更新 `services/__init__.py` 与 `ECL/services/game/__init__.py` 的聚合导出；确认 IPC 命令名与错误码零变化。
11. **推送**：按 `AGENTS.md` §5 校验全部子模块指针在远端存在 → 推子模块 → 推主仓库。

**每步都必须单独提交**（`AGENTS.md` §1），提交信息用 Conventional Commits + 中文描述。

---

## 6. 测试计划

| 层级 | 内容 |
|---|---|
| 后端静态 | `ruff check ECL tests`、`ruff format --check ECL tests` |
| 后端单测 | `pytest`（当前 90 个测试文件，其中 67 处引用 `services.game`，被移模块对应测试需同步改路径） |
| Core 侧 | 子模块自身测试 + 新增下沉模块的单测（Core 现有仅 2 个测试文件，覆盖极薄，需补） |
| 主仓库集成 | 涉及子模块集成，除子模块测试外必须通过主仓库集成测试 |
| 前端 | `cd frontend && pnpm check`（Prettier + ESLint + 类型 + 测试） |
| 实机验证 | `pnpm build` 后实际启动启动器，验证：账户登录、实例列表、启动游戏、崩溃分析、联机房间、Java 管理、自更新、单实例 —— 确认 IPC 契约未变 |
| 架构约束 | 项目架构测试会检查已声明 docstring 的多行格式与文件头，下沉文件须合规 |

**新增测试要求**：每个被下沉/合并的模块，都要有"迁移前通过、迁移后仍通过"的等价性测试；依赖解耦点（CoreError、atomic_write_text）需补边界用例。

---

## 7. 风险与回滚

| 风险 | 等级 | 缓解 |
|---|---|---|
| 跨仓库改动（子模块），推送顺序错误导致 CI 无法检出 | 高 | 严格按 `AGENTS.md` §5 用 `git ls-tree -r HEAD` 校验全部指针 |
| Core 吸收下沉代码后质量门禁真空 | 高 | §4.2.1 必须先建 Core 的 ruff + 测试工作流 |
| IPC 契约漂移（前端受影响） | 中 | 步骤 10 显式核对命令名与错误码；实机验证 |
| `java/` 提层导致导入面变化 | 低 | 决策 1 = B 后已无跨仓库碎片；影响面为 `api/java.py`、`services/game/base.py`、`application.py` 等少数调用点 |
| 一次性改动过大难以定位回归 | 中 | 严格按 §5 分 11 步、每步单独提交 |
| 导入路径大批量变更遗漏 | 中 | 每步后跑全量 `pytest`；用 `rg` 全仓扫描旧路径 |
| `ECL/foundation/` 合并涉及约 45 处导入（`EventBus` 独占 31 处） | 中 | 单独一步提交；合并后用 `rg 'from ECL\.(events|common|adapters)'` 确认归零 |
| `java/` 提层时遗留与 `services/game` 的双向依赖 | 中 | 优先清理 `manager.py` → `launch_settings` 的反向依赖，确保层级单向后再移动 |

**回滚**：每步一个 commit，出问题 `git revert` 该步即可；子模块同理。不做跨步的批量提交。

---

## 8. 决策点（已全部确认）

| 决策点 | 结论 | 影响 |
|---|---|---|
| 1. `services/java/` 归位 | **B：整体留主仓库，提为顶层 `ECL/java/`** | 不下沉 Core；`java/` 8 文件收敛为 3~4 个；需清理与 `services/game` 的双向依赖 |
| 2. 顶层微型层是否合并 | **是：合并为 `ECL/foundation/`** | `adapters/` + `common/` + `events/` → 6 文件；约 45 处导入需改 |
| 3. 本次是否含 `ECL/host/` 新层 | **A：包含** | 移出 7 个模块（2403 行）；`instance_shortcuts` 归 `utils/`、`themes` 归 `foundation/`、`skin_avatar` 归新建的 `services/account/` |

**因决策 1 = B 而简化的部分**：批次 3（java 下沉）取消，Core 只吸收批次 1 + 批次 2 共 **1970 行**，不再需要处理"`java/` 跨两个仓库"的碎片问题。

---

## 9. 目标文件结构（验收标准）

每步实施完成后对照本结构检查，避免过程中滑回"为拆而拆"。

```
ECL/
├── foundation/     基础支撑（合并 adapters + common + events + themes）  7 文件
├── utils/          纯无状态工具（收紧准入，见 §2.2）                     ~8
├── game/           [子模块] Minecraft 原语                               12
├── java/           JVM 运行时（从 services 提层）                        3~4
├── services/
│   ├── game/       游戏域编排                                            ~31
│   ├── account/    账户与皮肤（accounts + authlib + wardrobe + skin_avatar）  4
│   ├── connector.py / connector_nodes.py              联机                2
│   └── operations.py / processes.py / updates.py / custom_downloads.py    4 个真单例
├── host/           宿主基础设施                                          7
├── plugins/        插件系统（仅内部收敛，公共 API 不动）                  27
└── api/            IPC 契约（models 本次不拆，见 §10）
```

`ECL/services/` 根目录：**20 个平铺 → 1 个子包 + 6 个平铺文件**。

---

## 10. 后续建议（本次不做，另行立项）

| 建议 | 理由 | 时机 |
|---|---|---|
| **补 2 条架构约束测试** | ① 层方向单向性：`foundation → utils → game/java → services/host → api`，用 ast 扫描禁止反向；② Core 边界：`ECL/game` 不得导入 `ECL.{events,services,plugins,api}`。现有 `tests/test_architecture.py` 已约束"只能经 `ECL.game` 公共入口导入 Core"，但 `_python_files()` 排除了 `ECL/game`，不覆盖 Core 自身 | **建议并入本次**（待确认） |
| 拆 `api/models.py` | 1146 行 / **109 个 Pydantic 模型**横跨所有域；按域归为 **4~5 个**（窗口·设置·进程 / Java / 游戏·实例 / 文件·衣橱·皮肤），非按类拆 | 另立 |
| `plugins/` 内部收敛 | 5910 行 / 27 文件；`manager/` 9 文件、包安装三件套 782 行、依赖三件套 526 行。**仅收敛内部实现，公共 API 不动** | 另立 |
| `tests/` 分组 | 90 个测试文件全平铺；按域分 **4~5 个**目录（`api` / `services` / `plugins` / `core` / `utils`），不镜像到每模块 | 另立 |
| `_GameState` 巨型构造器 | `GameService` 多重继承聚合 14 个 coordinator，`_GameState.__init__` **20+ 参数**；改显式组合 + 显式委托，注入依赖收进 dataclass | 另立 |

---

## 附：预期结果

| 指标 | 现在 | 完成 §4.1~§4.3 后 | 再完成 §4.4 后 |
|---|---:|---:|---:|
| `services/` | 22818 | ~19500 | ~16790 |
| `services/` 占主仓库比 | 59.6% | ~54% | ~46% |
| Core | 4482 | ~6450 | ~6450 |
| 顶层包数 | 8 | 9（+`java/`） | 8（-3 微型层 +`foundation/` +`host/`） |
| 兼容 shim | 2 个 | 0 | 0 |

> 说明：`services/` 占比下降有限，因为**大头（`services/game` 的产品策略 ~10000 行）本就不该拆**——这是"避免过分拆分"的必然结果。真正的收益是**层语义清晰**，而不是数字变小。