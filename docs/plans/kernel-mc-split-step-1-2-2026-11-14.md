# 内核与 MC 拆分：第 1、2 步实施文档

- 日期：2026-11-14。
- 状态：待用户确认，未改动代码。
- 前置：[后端职责划分方案](backend-module-partition-plan-2026-11-14.md)。

## 0. 核实结论：原计划的两步范围必须调整

规划阶段给的是"迁 18 个文件"。逐文件核对依赖后，三项硬事实改变了范围：

**事实 1：7 个"MC 声明"其实是插件扩展点注册表，应留内核。**

`auth_providers`、`launch_hooks`、`crash_extensions`、`instance_compat`、`resource_catalog`、`java_resources`、`account_resources` 全是"插件能向宿主注册什么"的注册表，被内核的 `worker_service.py`、`manager/base.py` 和插件 SDK `plugin.py` 直接依赖。它们只是在类型上带 MC 语义，职责属于插件系统。

**事实 2：`resources.py` 是全部 11 个 MC 适配器的基座。**

它导出 `RuntimeResources`、`ResourcePages`、`InstanceQueries`，被 `game_install`、`game_launch`、`game_queries`、`game_resources`、`modpacks`、`instance_profiles`、`java_api`、`resource_queries`、`public_queries` 全部引用，另外还被内核的 `file_api.py` 与 `manager/base.py` 使用。

**事实 3：broker 内部实例化 6 个 MC 适配器，不是注入。**

| 行 | 实例化 |
| --- | --- |
| 252 | `RuntimeGameCatalog` |
| 256 | `RuntimeGameInstall` |
| 261 | `RuntimeGameLaunch` |
| 272 | `RuntimeResourceCatalog` |
| 283 | `RuntimeModpacks` |
| 297 | `RuntimeInstanceProfiles` |

真正靠 `__init__` 注入的只有 `RuntimeResources`、`RuntimeJava`、`RuntimePublicQueries` 三个。

**由此推出：适配器迁移不能只迁一部分。** 只迁 `resources.py`，留在原地的 6 个适配器会 import `ECL.minecraft`，而它们物理上仍在 `plugins/runtime/` 下，静态检查会判为"内核依赖 MC"。迁移是原子的，必须 11 个一起走，并同步改掉 broker 的 6 处就地实例化。

## 1. 调整后的范围

| 步 | 内容 | 是否可独立提交 |
| --- | --- | --- |
| 1 | 归属清单 + 边界检查框架（带豁免） | 是 |
| 2a | 拆出通用分页 `pagination.py` | 是 |
| 2b | 迁 11 个适配器 + broker 改适配器工厂 | 是（一次原子改动） |

比原计划多出的工作只有 broker 的 6 处改动，换来的是目录真正变纯。

## 2. 目标结构

```
ECL/
  mc/
    adapters/
      resources.py  java_api.py  public_queries.py  game_queries.py
      game_install.py  game_launch.py  modpacks.py  instance_profiles.py
      resource_queries.py  game_resources.py  game_models.py
  plugins/
    runtime/
      pagination.py      新增：ResourcePages + _Snapshot
      adapters.py        新增：McAdapterFactory 协议（内核侧，无 MC import）
      broker.py          改：6 处改为经工厂取得
```

`ECL/mc/` 后续继续收 `services/game`、`java`、`core`，本步不建空目录。

## 3. 第 1 步：归属清单与边界检查

不改业务代码。

### 3.1 归属清单

写入 `AGENTS.md` 新增小节"后端分层边界"：

| 域 | 目录 | 依赖方向 |
| --- | --- | --- |
| 内核 | `ECL/plugins/`（除 `mc/`）、`ECL/plugins/api/`、`ECL/services/operations.py`、`processes.py`、`updates.py`、`ECL/utils/`、`ECL/desktop/`、`ECL/headless/` | 不得 import `ECL.minecraft` |
| MC | `ECL/mc/`、`ECL/minecraft/services/game/`、`ECL/minecraft/services/account/`、`ECL/java/`、`ECL/minecraft/game/`（子模块） | 可依赖内核 |
| 边界 | `ECL/api/` | 可依赖两者 |

### 3.2 检查实现

新增 `tests/test_layer_boundary.py`，用 AST 扫描 import，不用文本匹配：

- 遍历内核目录下的 `.py`，收集 `Import` / `ImportFrom` 的目标模块名。
- 命中 `ECL.minecraft`、`ECL.minecraft.services.game`、`ECL.minecraft.services.account`、`ECL.java` 即违规。
- 豁免以 `dict[str, str]` 显式维护（文件 → 原因），并断言豁免条目数不超过上限。

### 3.3 当前存量违规

必须写成豁免，否则检查上线即红：

| 文件 | 违规 | 何时消除 |
| --- | --- | --- |
| `plugins/runtime/broker.py` | 11 个适配器 | 第 2b 步 |
| `plugins/runtime/file_api.py` | `resources.ResourcePages` | 第 2a 步 |
| `plugins/manager/base.py` | `resources.InstanceQueries` | 长期保留：它需要该 Protocol 描述注入的查询能力 |
| `plugins/runtime/game_install.py` 等 8 个 | `resources` | 第 2b 步（随文件一起迁走） |

豁免上限设为 13 条，2a 后降到 12，2b 后降到 1。

## 4. 第 2a 步：拆出通用分页

`ResourcePages` 与 `_Snapshot`（`resources.py` 78-178 行附近）是纯通用机制：HMAC 游标、快照表、按字节分页，不含 MC 语义，却被内核的 `file_api.py` 使用。

- 新建 `ECL/plugins/runtime/pagination.py`，使用 `AGENTS.md` 3.2 的标准文件头，公开接口列 `class ResourcePages`。
- `resources.py` 改为 `from ECL.plugins.runtime.pagination import ResourcePages`，不留同名转发。
- 同步改 `file_api.py`、`manager/base.py` 的 import。
- 逻辑零改动，只移动。

验收：分页专项测试通过，边界检查豁免从 13 降到 12。

## 5. 第 2b 步：迁移 11 个适配器

### 5.1 内核侧新增工厂协议

新建 `ECL/plugins/runtime/adapters.py`，只含协议，不含任何 MC import：

```python
class McAdapterFactory(Protocol):
    """
    在插件运行会话建立后创建 MC 能力适配器。

    broker 不再直接实例化 MC 适配器，构造与依赖装配由 MC 侧实现本协议完成，
    内核仅持有该协议的引用。
    """

    def create(self, session: PluginRunSession) -> McAdapters: ...
```

`McAdapters` 为持有 6 个就地实例化适配器的聚合对象（`game_catalog`、`game_install`、`game_launch`、`resource_catalog`、`modpacks`、`instance_profiles`）。

这是本方案唯一新增的抽象。调用方是 `PluginHostBroker`，解决的问题是切断内核到 MC 的编译期依赖；没有它，broker 无法在内部实例化 MC 适配器而不反向依赖。

### 5.2 broker 改动

- `__init__` 增加可选的 `mc_adapters: McAdapters | None` 参数。
- 删除 252/256/261/272/283/297 六处的就地实例化，改为读取 `self.mc_adapters`。
- MC 适配器的类型 import 移入 `if TYPE_CHECKING:`。
- 构造方（`manager/base.py` 或 `worker_service.py`）在创建 broker 前，用 MC 侧工厂实现装配 `McAdapters`。

注意：`RuntimeInstanceProfiles` 依赖注入的 `instance_profiles`（来自 `services/game`），`RuntimeGameInstall` 依赖 `resources` 与 `operations`，这些依赖顺序在工厂实现里保持原样，不重排。

### 5.3 迁移动作

11 个文件从 `plugins/runtime/` 移到 `mc/adapters/`，同步：

| 改动 | 范围 |
| --- | --- |
| 文件内相对 import | 11 个文件自身 |
| 引用方 import | `broker.py`（改 TYPE_CHECKING）、`worker_service.py`、`manager/base.py`、`resources` 的 10 个引用方 |
| 测试 import | `tests/` 中引用这些模块的用例，只改 import 路径，不改断言 |

不留旧路径别名。

### 5.4 验收

- `python -c "import ECL.minecraft.adapters.resources"` 等 11 个逐个导入成功。
- 边界检查豁免降到 4（实施修正：`application.py` 构造根、`worker_service.py` 与 `manager/base.py` 两个装配缝、`java_resources.py` 类型引用——`worker_service` 仍内联构造三个注入式适配器，工厂只接管 broker 的七个就地实例化，全部消除需装配逻辑统一，另行推进）。
- 全量 `pytest` 通过。
- 现有 v2 示例插件加载、命令、面板、设置调用正常。

## 6. 不做的事

- 不迁 7 个插件扩展点注册表（职责在内核）。
- 不动 `services/game`、`services/account`、`ECL/java`、`ECL/minecraft/game` 子模块。
- 不改任何 IPC 命令名、插件方法名。
- 不动 `api_version`（2→1 是独立一刀，单独提交）。
- 不改 `api/` 下的 handler 实现（第 4 步）。

## 7. 顺序与回滚

```
第 1 步（清单 + 检查 + 豁免）—— 可独立提交
   ↓
第 2a 步（拆 pagination）—— 可独立提交
   ↓
第 2b 步（11 个适配器 + broker 工厂）—— 原子提交
```

2b 若失败，回到 2a 仍是完全可运行状态。

## 8. 测试计划

| 层 | 内容 |
| --- | --- |
| 边界 | AST 扫描依赖方向；豁免条目数断言 13 → 12 → 1 |
| 分页 | 游标伪造、快照过期、容量耗尽、跨代次失效，与拆分前一致 |
| 适配器 | 11 个适配器原有测试全过，只改 import |
| broker | 工厂注入路径与原有就地构造路径行为等价；`mc_adapters` 为 None 时相关方法返回既有"能力不可用"错误码 |
| 插件集成 | 现有 v2 示例插件全流程 |
| 全量 | `ruff check ECL tests`、`ruff format --check ECL tests`、`python -m pytest --tb=short -q` |
| 前端 | 不改，跑 `pnpm check` 确认无联动 |

## 9. 风险

| 风险 | 应对 |
| --- | --- |
| broker 改工厂后漏传依赖 | 工厂实现先照抄原 6 处构造参数顺序，逐行对照 |
| 迁移遗漏 import 只在运行时暴露 | 每个文件单独导入验证，不依赖测试覆盖 |
| 豁免列表被扩大 | 测试内断言上限 |
| 子模块指针意外变动 | 提交前 `git diff --submodule` 检查 |
