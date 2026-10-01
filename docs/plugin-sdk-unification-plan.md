# 插件 SDK 统一实施方案

状态：用户已于 2026-10-01 确认实施，功能及本地验收已完成。结果见第 9 节。

## 1. 目标与决策依据

当前插件机制尚未发布，不需要维护旧的无参数命令 SDK。统一目录插件与 `.eclplugin` 归档插件的基类和加载流程，删除为了旧接口保留的转出口、适配器和构建引用。

唯一推荐的公共导入方式为：

```python
from ECL.plugins import Plugin
```

基类实现仍位于 `ECL/plugins/plugin.py`，不新建 SDK 包，不重写现有完整插件 API，也不引入新的解释器或运行环境。

## 2. 当前情况

- 根目录 `ecl_plugin_sdk.py` 仅重新导出 `ECL.plugins.command_sdk.Plugin`，没有依赖安装或环境隔离逻辑。
- `command_sdk.py` 实现另一套无参数命令基类，使用 `CommandPluginAdapter` 接入正式宿主生命周期。
- `manager/discovery.py` 识别旧基类并选择无参数构造；其他插件使用宿主提供的构造参数。
- `ECL.plugins` 已公开导出完整基类；ECLPluginDevTool 当前插件模板也使用此入口，无需修改该独立仓库。
- Nuitka、PyInstaller 与 setuptools 配置仍显式收集根目录兼容模块。
- 部分归档测试和冻结产物验收代码仍使用旧入口。格式文档及此前实施方案包含“已发布示例需要兼容”的错误前提，应一并纠正。

## 3. 范围与不变项

本次移除的是旧 SDK 接口兼容，不是所有名称中含“兼容”的业务逻辑。

保持以下行为不变：

- 所有插件在启动器主进程运行。
- Python 依赖安装目录隔离、完整锁匹配共享目录、共享 wheel 缓存与显式允许联网。
- 包内离线 wheels、归档完整性检查、依赖和导入冲突检查。
- 安装与更新待重启、原子活动指针、失败恢复、安全模式、插件依赖排序。
- 正式基类的现有权限声明、生命周期、事件、设置及前端扩展能力。
- 旧 Worker 安装记录迁移及旧代码、venv、缓存的数据保留策略；不自动删除任何用户目录。
- `ECL.plugins.plugin.Plugin` 实现模块保持存在，不额外禁止直接导入该实现模块。

不修改前端源码、子模块指针、ECLPluginDevTool，或无关的 `docs/launch-advanced-options-plan.md` 工作区修改。

## 4. 具体改动

| 文件 | 实施内容 |
| --- | --- |
| `ecl_plugin_sdk.py` | 删除根目录兼容转出口，不保留别名或导入回退。 |
| `ECL/plugins/command_sdk.py` | 删除旧命令基类与 `CommandPluginAdapter`。 |
| `ECL/plugins/manager/discovery.py` | 删除旧 SDK 导入和识别分支，统一使用现有宿主构造流程。 |
| `ECL/plugins/host_dependencies.py` | 移除已删除模块的专用保留导入名；标准库、ECL 与现有宿主模块保护不变。 |
| `pyproject.toml` | 删除仅服务于兼容模块的 `py-modules` 声明及空配置节，保留 ECL 包发现。 |
| `EuoraCraft-Launcher.spec` | 删除兼容模块 hidden import，保留宿主插件包收集。 |
| `packaging/build-nuitka.ps1` | 删除兼容模块显式收集参数。 |
| `.github/workflows/build.yml` | 删除通用和 Arch 构建中的兼容模块收集参数，保留插件安装与产物验收步骤。 |
| `tests/plugin_wheel_helpers.py` | 默认归档示例改用正式 SDK，并明确所需命令权限。 |
| `tests/test_plugin_manager_packages.py` | 迁移旧导入，验证正式归档实例、生命周期、命令和依赖排序。 |
| `tests/test_plugin_host_dependencies.py` | 更新受保护模块测试，不继续把已删除入口当作宿主保留名。 |
| `tests/test_packaged_plugin_directories.py` | 迁移实际冻结产物中的测试插件，并声明 probe 命令权限，保留原生扩展和包资源验收。 |
| `docs/eclplugin-package-format.md` | 替换 SDK 示例，补充构造及命令权限说明，取消两套 SDK 的区分。 |
| `docs/plugin-install-directory-isolation-plan.md` | 标明本次方案取代旧 SDK 兼容决策，纠正“已发布”前提，避免历史方案被当作当前要求。 |

如检索发现额外的直接引用，在同一功能范围内同步更新；不借此清理其他历史迁移或异常别名。

## 5. 正式 SDK 使用契约

归档入口继承正式 `Plugin`；没有自定义构造函数时直接继承基类构造。确需自定义构造时，接收宿主传入的 `framework`、`plugin_dir`、`metadata`、`is_system`，并调用 `super().__init__`。不再支持旧基类的无参数初始化约定。

初始化业务优先使用现有 `on_load` 钩子，命令通过 `@Plugin.on_command` 或 `register_command` 登记。不增加自动暴露普通方法的行为。

正式基类登记命令会检查 `plugin.json.permissions`。文档示例须同时提供所需声明，例如 greet 命令对应：

```json
{
  "permissions": [
    { "scope": "commands", "action": "execute", "resource": "greet" }
  ]
}
```

测试辅助函数仅为自带默认 pid 示例提供对应权限；调用方显式传入空权限时必须保留空权限，不能通过默认值掩盖缺失声明。自定义代码所需权限由各用例显式给出，冻结产物示例声明 probe 权限。

仍引用已删除模块的本地实验插件需要由作者修改代码、重新打包；不增加自动改写插件、接口版本判断或静默兼容路径。

## 6. 实施顺序

1. 确认实施后复查工作区，保留无关修改，核验当前主分支与构建收集方式。
2. 迁移测试示例、权限声明和作者文档，补充正式 SDK 统一加载与兼容层删除的回归覆盖。
3. 移除兼容模块、发现分支及保留导入名，清理 setuptools、Nuitka、PyInstaller 引用。
4. 运行相关测试和全量后端检查，复查所有有效代码和构建配置不存在旧 SDK 引用。
5. 完成前端检查与构建，再实际启动启动器执行插件安装、重启加载及命令调用验收。
6. 使用本次源码重新制作 Windows 单文件测试产物，执行离线 wheel、包资源和原生扩展验收；不使用旧产物证明新代码通过。
7. 测试通过后只提交本次相关文件到本地 `main`，使用中文 Conventional Commit；不推送、不打标签、不发布。

## 7. 测试与验收

新增或更新的覆盖应包含：

- 根目录旧入口、旧适配模块和打包收集参数已移除，防止后续重新带入。
- 归档插件实例直接继承正式基类，不再经过命令适配器；宿主构造参数正确传入。
- 装饰器和动态登记命令可用；未登记命令不可调用，禁用插件不可调用。
- 缺少命令权限的归档按现有规则拒绝登记，显式空权限不会被测试辅助函数补齐。
- `on_load` 失败不进入启用阶段，正常生命周期、安全模式和插件依赖顺序不回退。
- 安装不执行入口，重启后在主进程调用命令；更新继续显示待重启。
- 离线 wheel 安装、缓存复用、依赖目录共享和冲突拒绝行为保持有效。
- 单文件产物离线安装后重启，可读取包资源并导入 xxhash 原生扩展，未创建插件 venv 或额外运行时。

后端执行仓库环境下的 `ruff check ECL tests`、`ruff format --check ECL tests` 与完整 `pytest --tb=short -q`；修改的根目录脚本和构建辅助 Python 文件也纳入 Ruff 检查，结合独立 Ruff CI 的 `ruff check .` 复核。

本次涉及插件加载流程，因此即使不修改前端源码，也执行 `frontend` 的 `pnpm check` 和 `pnpm build`，之后实际启动验收。构建输出和测试临时数据置于忽略目录，不加入提交，也不使用用户真实插件数据。

Windows 冻结产物通过设置 `ECL_PLUGIN_SMOKE_EXECUTABLE`、`ECL_PLUGIN_SMOKE_WHEELS` 执行 `tests/test_packaged_plugin_directories.py`。缺少新产物时的 skip 不视为产物验收通过。其他平台真实产物仍由对应 CI 执行；本地总结明确区分已验证平台与待远端 CI 项目，不宣称本地已覆盖所有平台。

## 8. 风险与完成标准

主要风险是测试示例遗漏正式权限声明、遗留构建参数引用已删除模块，以及误用旧冻结产物验收。通过统一示例、完整引用检索和新产物测试控制这些风险。

完成标准：只有一套正式插件基类和加载路径；旧兼容代码及有效构建引用全部移除；文档示例可运行；相关与全量检查、实际启动和 Windows 新单文件产物验收通过；本地相关提交完成。保留的历史文档引用须明确为已废弃决策。

实现回退通过恢复本次相关代码完成，不删除插件数据目录、不覆盖用户插件或依赖内容。若测试发现需修改本方案范围外的模块，先说明影响并征求确认。

## 9. 实施与验收记录（2026-10-01）

- 已删除根目录转出口、旧命令基类和适配器；发现流程统一使用宿主构造参数。正式公共入口与宿主基类为同一个类。
- 已清理 setuptools、Nuitka 通用及 Arch 构建、PyInstaller 和宿主保留导入名的旧 SDK 引用。当前代码及构建配置不再引用旧模块；名称只保留在删除回归测试及历史说明中。
- 归档测试和作者示例已迁移正式 SDK；默认 pid 示例及冻结产物 probe 示例明确声明命令权限。显式空权限不补齐，缺少声明的归档无法创建实例或调用命令。
- 删除边界测试在变更前得到 8 项失败、1 项通过，变更后全部通过；补充正式实例、宿主构造、自定义生命周期和动态命令验收。
- `ruff check .`、`ruff check ECL tests` 与 `ruff format --check ECL tests` 全部通过；全量后端测试为 840 passed、4 skipped。设置 `PYTHONUTF8=1` 后复跑全量，无编码警告；跳过项未算作验收通过。
- 针对 SDK、归档管理、宿主依赖、安装目录及准备流程的测试为 68 passed、2 skipped；前端 `pnpm check` 为 102 个测试文件通过、419 项通过、1 项 todo，`pnpm build` 通过，保留现有大 chunk 提示。
- 用本次源码和新前端产物重新构建 Windows CPython 3.13 / PyInstaller 单文件启动器，产物位于忽略的 `build/plugin-sdk-smoke-dist`。实际产物执行两次原生启动，离线安装、重启加载、主进程命令调用、包资源读取与 xxhash 3.6.0 原生导入测试为 1 passed；验证正式 SDK 模块与宿主上下文，未创建插件 venv 或运行时。
- 直接检查新产物的内嵌 Python 归档：包含正式插件基类，不包含两个已删除的旧 SDK 模块。测试创建的启动器进程均已退出。
- PyInstaller 仍提示原有显式收集项 pyperclip 和 tzdata 在本地缺失；本次构建和插件验收通过，未借此修改无关依赖或打包策略。本地未执行 Nuitka、macOS、Linux 真实产物验收，等待对应远端 CI，不声明跨平台产物已验证。
- 发现现有归档列表在构造权限失败时显示 unloaded，并携带权限错误；内部状态为 permission_denied。测试验证拒绝加载和实际错误，不在本次 SDK 清理中更改列表展示规则。
- 未修改依赖安装机制、旧安装记录迁移、用户数据、前端源码或子模块指针；无关的启动高级选项文档修改保持原样。本次仅本地提交，不推送、打标签或发布。
