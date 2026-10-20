# Python 根目录入口与插件 API 下沉实施方案

状态：用户已确认实施；目录、导入、打包与文档迁移已完成，适用本机验收通过。

## 1. 目标与范围

根目录直接包含的 Python 源文件只保留 `main.py`，启动引导与插件 API 统一归入 `ECL`。

用户已选择同时迁移 `ecl_bootstrap.py` 和 `ecl_plugin_api/`，并统一调整插件导入路径。`tests/`、`tools/`、`packaging/` 和资源内的插件示例保留各自职责；前端与 Core 等子模块不因本次目录整理迁移。

本文落实 [项目规范](../AGENTS.md) 第 0 节的实施前审阅要求；范围确认不等于代码实施批准。

## 2. 当前实现与影响

| 位置 | 当前职责 | 本次影响 |
| --- | --- | --- |
| `main.py` | 调用 `ecl_bootstrap.run_process`，保留 `run_launcher` 入口 | 改为调用 `ECL.bootstrap`，保持命令行与退出码行为 |
| `ecl_bootstrap.py` | 仅依赖标准库，先识别角色，再加载对应执行器 | 迁移至 `ECL/bootstrap.py` |
| `ecl_plugin_api/` | 标准库插件 API、协议、快照、RPC、进程引导及依赖解析 | 整包迁移至 `ECL/plugin_api/` |
| `ECL/__init__.py` | 导出 `ECL.foundation.version` 的版本属性 | 导入版本模块会先执行 `ECL.foundation.__init__`，需解除聚合导入 |
| `ECL/cli.py` | 复用角色解析，处理桌面与普通任务参数 | 更新启动引导导入 |
| `ECL/plugins/` 中的监管器与授权模型 | 复用插件 API 的协议和运行资源 | 更新为新的包路径 |
| `pyproject.toml` | 单独收集根模块、ECL 包和独立插件包 | 移除旧根模块与独立包收集声明 |
| PyInstaller、Nuitka 与构建工作流 | 入口均为 `main.py`，额外收集独立插件包 | 更新收集路径并验证发行物 |
| 入口、插件和依赖隔离测试 | 包含旧导入路径及“禁止任意 ECL 导入”的断言 | 按新的轻量包边界更新，不放宽业务隔离 |

现有入口测试以 `python -S` 验证无需 site 初始化即可分流。迁入 ECL 后，加载 ECL 包本身成为必然，验收应改为明确允许轻量入口，继续禁止桌面和业务服务加载。

当前检索未发现项目自有代码通过 `from ECL import ...` 使用根包版本导出。实施时复查全部调用方；版本信息统一从现有 `ECL.foundation.version` 获取。

## 3. 选定目录与接口

```text
main.py
ECL/
  __init__.py          # 纯包标记，不聚合导出版本或宿主服务
  bootstrap.py        # 标准库角色解析与延迟分流
  plugin_api/
    __init__.py        # 轻量公共插件 API
    plugin.py
    contracts.py
    snapshot.py
    _process.py
    _imports.py
    _rpc.py
    _runtime.py
    _transport.py
    _windows.py
  cli.py
  plugins/            # 现有宿主插件管理逻辑
```

插件 API 使用独立的 `ECL.plugin_api` 包，不挂在 `ECL.plugins` 下，避免触发宿主插件管理器的聚合导入。目录下沉不改变 API 与宿主服务之间的依赖方向。

新导入示例：

```python
from ECL.bootstrap import ProcessRole, select_process_route
from ECL.plugin_api import Plugin
from ECL.plugin_api.contracts import ProtocolError
```

不保留 `ecl_bootstrap`、`ecl_plugin_api` 的转发模块、包别名、双路径导入或自动回退。使用旧 API 路径的插件源码须更新导入；不扫描或改写已安装插件和用户文件。已有宿主插件接口 `ECL.plugins.Plugin` 不属于此次替换对象，不因名称相似而删除。

## 4. 实施步骤

1. 复查工作区、子模块和本地 main 状态，建立本次工作分支，记录已有改动，避免夹带其他工作。
2. 将 `ECL/__init__.py` 收敛为纯包标记，移除根包版本重导出与对应 `__all__`。复查调用方，若发现项目内调用，改为直接读取既有版本模块；不为此次迁移改造整个 foundation 包。
3. 迁移引导模块和插件 API 包，更新文件头、公开接口说明及必要中文多行 docstring。`main.py` 只负责参数转交、调用与进程退出。
4. 更新主入口、CLI、插件 API 内部、宿主监管器、授权模型和测试中的导入，包括测试生成的插件源码、子进程脚本及模拟模块名称。
5. 调整依赖解析器的受保护模块名。以 `ECL` 顶层命名空间继续保护新的 API；移除无现行职责的旧包名保留项，保持标准库、宿主模块和锁定依赖的冲突拒绝行为。
6. 更新 `pyproject.toml`：删除旧 `py-modules` 配置，包发现只收集 `ECL*`，Ruff 第一方包配置移除旧名称。更新 spec、PowerShell 构建脚本和 `.github/workflows/build.yml`，统一依靠 ECL 包收集新模块，清除旧独立包的显式收集项。
7. 更新入口和插件导入边界测试，增加根目录结构与旧路径移除的架构约束，补齐下一节的行为验证。
8. 更新 [无头运行说明](headless-runtime.md) 中的现行路径与导入边界。在相关插件设计文档中标明本方案取代的目录约定，更新仍作为当前使用指导的导入示例；保留明确标注的历史现状记录，避免把旧阶段描述伪装为当前实现。
9. 按第 5 节通过检查后提交并完成安全的本地 main 集成。远端推送与发布仍需针对当前变更的明确授权。

## 5. 测试与验收

### 5.1 结构与轻量导入

- 架构测试验证根目录 `.py` 文件集合恰为 `{"main.py"}`，旧模块与旧包目录不存在，新模块可以通过规范路径导入。
- 用禁用 site 的独立子进程导入 `main`：ECL 范围只允许 `ECL` 与 `ECL.bootstrap`，不预载插件执行器、foundation 聚合入口、CLI、桌面或业务服务。
- 独立导入 `ECL.plugin_api`：ECL 范围仅允许包标记与 API 子包，不导入 bootstrap、foundation、CLI 或业务模块；公共 API 导入本身也不应加载插件执行器。使用导入拦截器验证禁止项，避免只检查模块名称遗漏传递依赖。
- 在 `-S` 子进程中验证合法插件角色只分流到所选执行器；模拟执行器时返回码正常透传。非法和混合参数只允许加载包标记与 bootstrap，以退出码 2 拒绝，不加载插件执行器或桌面。
- 旧路径拒绝测试以当前源码或新安装发行物为边界，避免外部 Python 环境残留旧安装造成误判。

### 5.2 行为回归

- 运行并按需更新 `test_process_bootstrap.py`、`test_cli_launch_options.py`、`test_headless_subprocess.py`、`test_headless_runtime.py`、`test_plugin_worker_*.py`、`test_plugin_windows_supervision.py`、`test_plugin_sdk_boundary.py` 及架构测试。
- 保持命令行参数、帮助、版本、退出码、单实例转发和普通无头任务行为；普通无头任务不创建 UI、不触碰业务文件、不通知桌面实例。
- Windows 验证真实源码插件子进程的连接、授权、生命周期、双向 RPC、停止与不同锁定依赖版本。保留 POSIX 平台拒绝与无头任务分支；不把 Windows 专属用例的跳过记为通过。
- 更新并执行“不能遮蔽 ECL 命名空间”的依赖冲突测试，保持快照完整性、内容许可和依赖隔离规则。

### 5.3 检查命令与发行物

按 [项目规范第 6 节](../AGENTS.md) 执行全部适用检查：

1. 主仓库执行 `ruff check ECL tests`、`ruff format --check ECL tests`、`ruff check . --format github`，并单独检查迁移涉及的 `main.py` 格式。
2. 先运行相关 pytest，再执行 `python -m pytest --tb=short -q`。按主仓库 CI 在 `ECL/game` 执行 `ruff check .`，在主仓库执行 `python -m pytest ECL/game/tests --tb=short -q`，验证包入口变更不破坏 Core 集成。
3. 执行 `python main.py --help`、`python main.py --version`、隔离数据目录下的 `--headless --task=runtime-check` 及非法角色参数，核对退出码、输出和文件副作用。
4. 构建 Wheel，检查包含 `ECL/bootstrap.py` 与 `ECL/plugin_api/`，不包含旧根模块或旧独立包；在隔离环境导入新 API，防止源码目录掩盖漏包。
5. 运行流程受到影响，前端先执行 `pnpm check`，通过后执行 `pnpm build`。实际启动 Windows 启动器验证窗口、退出、单实例行为与插件相关入口。
6. 构建当前版本的 Nuitka、PyInstaller 产物，使用现有发行物无头探测和插件冒烟测试验证新路径。平台专属冻结验证按对应平台执行；本机未运行的平台、缺少真实产物导致的跳过和待跑 CI 单独记录，不据此宣称全平台发行物验证完成。
7. 完成文档链接和内容检查，执行 `git diff --check`，复查旧名称：生产代码和构建配置清零；测试中的旧入口拒绝断言、本文迁移说明与已标注的历史记录可保留。

若任一适用门槛未通过，先修复或明确环境阻碍，不提交代码并宣称实施完成。

## 6. 风险与交付

- **提前加载宿主模块**：通过纯包标记与严格子进程导入检查阻断，不使用修改 `sys.path`、动态文件加载或模块别名绕过正常包导入。
- **插件源码导入变化**：统一采用 `ECL.plugin_api`，随实现更新项目内调用与文档；用户插件不会被自动迁移，旧路径报错应明确呈现。
- **打包漏收集**：同时更新三处构建链路，分别验证 Wheel 和冻结发行物。包路径检查不能替代实际执行检查。
- **测试边界失真**：把原来的“禁止所有 ECL”改为明确允许轻量包入口，不放宽为允许所有 `ECL.*`。
- **影响范围**：主仓库启动引导、插件 API、相关测试、打包配置和文档；不迁移账户、实例、存档或插件持久化数据。

计划文档独立提交。实施完成后以中文 Conventional Commit 提交相关代码，使用 `git worktree list` 确认原工作区与本地 main，保护无关改动并完成安全本地集成。交付分别列明代码提交、本地集成、各类检查与实际运行结果；本方案不授权推送或发布。

## 7. 实施与验收记录（2026-10-20）

目录、内部导入、插件源码示例、构建收集与文档均已按方案更新。根包不再导出版本属性；版本定义仍归属 `ECL.foundation.version`。Worker 握手允许 ECL 包标记、bootstrap 与插件 API，继续拒绝其他 ECL 模块提前加载，并覆盖相似前缀不能绕过检查的回归场景。

| 验证项 | 本次结果 |
| --- | --- |
| 新增目录约束 | 迁移前 2 项失败，迁移后通过；根目录仅 `main.py` |
| Ruff 与格式 | 主仓库、ECL/tests 格式与 Core Ruff 均通过；本机 Ruff 使用 `--output-format github` 提供 CI 所需的 GitHub 输出 |
| 主仓库全量 pytest | 1535 通过、15 跳过；其中依赖真实产物的用例另行验收，不将跳过计为成功 |
| Core 集成 pytest | 12 通过 |
| 前端 `pnpm check` | 格式、ESLint、类型检查通过；162 个测试文件、889 个测试通过，1 个现有 todo |
| 前端 `pnpm build` | 通过；保留现有大分块体积提示 |
| Wheel | 构建、内容检查及隔离安装导入通过；新路径可用、旧路径不可导入 |
| 源码入口 | 帮助、版本、无头探测和非法角色退出码通过 |
| Windows 源码实机 | 使用独立 WebView2 profile，真实页面加载、原生 IPC、单实例转交与关闭后的资源清理通过 |
| PyInstaller 单文件 | 构建成功；无头任务、源码/冻结 Worker 与依赖用例共 11 通过；离线插件安装及重启加载原生依赖 1 通过 |
| Nuitka 单文件 | 构建成功；无头任务、源码/冻结 Worker、依赖隔离及离线插件安装共 12 通过 |

环境为 Windows x64、CPython 3.11.9。主仓库全量测试出现一次 Windows 默认编码导致的子进程读取线程警告；使用 `PYTHONUTF8=1` 重跑对应插件打包 CLI 用例后通过且无警告。源码窗口关闭后 Playwright CLI 尝试读取已关闭页面而返回错误，后端已记录前端退出、插件卸载与联机资源清理完成，不将 CLI 的收尾错误视为应用仍在运行。

本次没有修改子模块指针，也没有更改账户、实例和插件持久化格式。Linux/macOS 的真实图形窗口与发行物未在本机运行；POSIX 拒绝分支由现有自动化用例验证。未执行远端推送或发布。
