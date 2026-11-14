# 插件失败链路可观测性实施方案

- 日期：2026-11-14。
- 状态：待用户审阅，未改动代码。
- 背景：2026-11-14 排查 `PLUGIN_ENABLE_FAILED`（两个官方插件 SDK 路径漂移）与 `PLUGIN_INSTALL_FAILED`（api_version 基线）时，失败链路的真实原因均无法从日志与前端提示获得，每次都需手工复现定位。

## 1. 现状核实

### 1.1 已完成的修复（不再列入本轮）

- 安装失败：`ECL/api/plugins.py` 已有 `_install_failure`（`claim_failure` 领取日志权写入具体原因）；`lifecycle.py` 清单校验失败已记录 `字段(错误类型)`，依赖缺失已拆出 `_unsatisfied_dependencies` 并写入 message。均已随 `54354f4` 前后提交推送。
- 插件仓库 CI 已用宿主仓库源码（PYTHONPATH）提供 SDK 跑 pytest。

### 1.2 仍存在的三个缺口

**缺口 1：Worker 入口异常的 traceback 完全丢失。**

- Worker 进程侧 `ECL/plugins/api/_process.py:224-226`：`except Exception` 只回发固定错误码 `PLUGIN_WORKER_EXECUTION_FAILED`，无任何日志、无 traceback。
- 宿主侧 `ECL/plugins/runtime/supervisor.py:192-197`：收到 error 帧后白名单压平，非 `PLUGIN_CONTENT_CHANGED` / `PLUGIN_DEPENDENCY_CONFLICT` / `PLUGIN_PROTOCOL_ERROR` 一律变 `PLUGIN_WORKER_EXECUTION_FAILED`。
- `lifecycle.py` `enable` 的 except 分支（`:175-179`）只把错误码存进 `_plugin_errors`，**不写日志**。前端只见裸码。
- 白名单压平与"不转发插件可控异常文本"是既定安全设计（`_report_failure` docstring 明示），**保留不动**；缺的是把原因写进宿主日志，不是改前端契约。

**缺口 2：插件仓库 CI 不 import 入口 main.py。**

- OnlineGame 的 `tests/` 只有 connector 相关用例，QomicexCompatible 同理。本次 SDK 路径漂移（`ECL.plugin_api` → `ECL.plugins.api`）全部发生在 `main.py` 与 `remote_extensions.py`，CI 全绿但装上就 `PLUGIN_ENABLE_FAILED`。

**缺口 3：目录安装把 `.git` 一并复制进安装副本。**

- `ECL/plugins/manager/install_transaction.py:85` `shutil.copytree(source_path, self.stage_path)` 无 ignore 规则。带 `.git` 的源码目录安装后：只读对象文件导致卸载 `WinError 5`（已由 `remove_tree` 缓解）；且安装副本的 `.git` 缺 HEAD，不是可用仓库，git 命令会误落主仓库。
- 上一轮已登记"排除 `.git` 需同步改 `CodeSnapshot.capture` 与 copytree，两处摘要必须一致"。

## 2. 改动清单

### 2.1 Worker 失败原因进宿主日志（缺口 1）

协议改动最小方案：error 帧增加可选 `detail` 字段，宿主只落日志、不下发前端。

| 位置 | 改动 |
| --- | --- |
| `ECL/plugins/api/_process.py` | `_report_failure` 增加 `detail: str \| None` 参数；顶层 `except Exception` 用 `traceback.format_exc()` 生成 detail；`PluginRuntimeError`/`ProtocolError` 分支 detail 取 `str(exc)`。发送 `WireMessage("error", {"code": ..., "detail": ...})`。`_report_failure` docstring 同步：错误码仍走白名单，detail 只用于宿主日志诊断 |
| `ECL/plugins/runtime/supervisor.py` | `authorize_load` 的 error 帧分支：`detail = reply.params.get("detail")`，有则 `self._logger.warning("插件 Worker 加载失败（%s）：%s", code, detail)`。抛出的 `PluginRuntimeError` 消息仍只含错误码，不变更异常契约 |
| `ECL/plugins/manager/lifecycle.py` | `enable` 的 except 分支补 `self.logger.warning("插件启用失败：%s；错误码：%s", name, code, exc_info=True)`；`disable` 同型分支若有静默 except 一并补齐 |
| 协议文档 | `docs/protocols/` 下 Worker 协议相关文档补 error 帧的 `detail` 字段说明（可选、仅诊断用途、不进入前端） |

不做的事：

- 不放宽错误码白名单，不把 detail 返回给 IPC/前端（`enable` 对前端仍只回错误码）。
- 不给 Worker 引入文件日志、不向插件代码目录写任何文件（会破坏 CodeSnapshot 摘要）。
- 修改 `docs/plans/plugin-ui-no-guardrails-2026-11-13.md` 的立场不冲突：这是宿主自身诊断日志，不是面向插件的防护。

### 2.2 插件仓库入口冒烟测试（缺口 2）

两个独立插件仓库各新增 `tests/test_entry_import.py`：

```python
def test_entry_modules_import() -> None:
    import main  # noqa: F401

def test_remote_extensions_import() -> None:  # 仅 OnlineGame
    import onlinegame.remote_extensions  # noqa: F401
```

- 前置核实项（实施时确认）：`main.py` 顶层只做类定义与常量装配，无连接/网络副作用；若有，改为受保护导入并在方案中注明。
- CI 无需改动（tests job 已有 PYTHONPATH 指向宿主 SDK，新增用例自动被收集）。
- 同步登记一条约定到两个插件仓库的 AGENTS.md：入口与扩展模块的 import 必须能脱离宿主进程独立通过 `import` 冒烟。

### 2.3 目录安装排除 `.git`（缺口 3）

| 位置 | 改动 |
| --- | --- |
| `ECL/plugins/manager/install_transaction.py:85` | `shutil.copytree(..., ignore=shutil.ignore_patterns(".git"))` |
| `ECL/plugins/api/snapshot.py` `CodeSnapshot.capture` | `file_paths` 过滤 `entry.relative_to(root_path).parts[0] == ".git"` 的条目；同时跳过 `__pycache__` 是否纳入属决策点（见第 3 节问题 2），默认不动 |
| 测试 | 新增：源目录含 `.git`（含只读对象文件）时，安装后 stage/最终目录无 `.git`；`CodeSnapshot.capture` 的 files 与摘要不含 `.git` 条目；卸载不再需要清只读属性 |

一致性约束：`capture` 过滤规则与 `copytree` ignore 规则必须字面一致（同一份忽略清单），否则 `verify` 阶段 `PLUGIN_CONTENT_CHANGED`。实施时把忽略清单提为两处共用的单一来源（按 AGENTS.md 3.3 归属 `CodeSnapshot` 或安装事务类）。

影响面说明：这是插件内容身份语义变更（同目录重装摘要会变），按 AGENTS.md 第 2 节未发布阶段不承诺兼容，不写迁移代码。开发模式快照走同一 `capture`，行为随之统一。

## 3. 决策点（需用户确认）

1. **detail 字段是否纳入本轮**：不动协议也能做 2.1 的宿主侧日志（lifecycle `exc_info` 仍能记录宿主侧异常），但 Worker 进程内的入口异常（本次的真实痛点）只有 detail 方案能拿到。建议纳入。
2. **`__pycache__` 是否一并排除**：`.git` 明确排除；`__pycache__` 目前会被复制与快照，排除可减小副本并避免陈旧字节码，但属同类语义变更。默认本轮只排 `.git`，`__pycache__` 留待需要时单独做。
3. **卸载只读清属性逻辑去留**：`.git` 排除后 `remove_tree` 的 `S_IWRITE` 重试逻辑对其他来源（杀毒软件占用等）仍有价值，保留不动。

## 4. 测试计划

| 层 | 内容 |
| --- | --- |
| Worker 协议 | 模拟入口抛异常：error 帧含 `code`（白名单内）与 `detail`（traceback）；宿主日志含 traceback；`authorize_load` 抛出的错误码契约不变 |
| lifecycle | enable 失败路径断言 `logger.warning` 被调用（caplog）；`_plugin_errors` 与返回码不变 |
| 既有回归 | 全部 Worker 协议、supervisor、lifecycle 用例零改动通过（error 帧无 detail 时行为不变） |
| 插件仓库 | 两仓库新增冒烟用例本地通过；CI 全绿 |
| 安装事务 | 含 `.git` 目录安装 → 无 `.git`；摘要与 capture 一致；重装幂等 |
| 全量 | `ruff check ECL tests`、`ruff format --check ECL tests`、全量 pytest（独立临时根目录）；前端不改，跑 `pnpm check` 确认无联动 |

## 5. 验收命令与顺序

1. 2.1（协议 + supervisor + lifecycle）→ 2.2（插件仓库冒烟）→ 2.3（`.git` 排除），三步各自独立可提交、可回滚。
2. 全量验证后端 + 两个插件仓库。
3. 插交信息建议：`feat: Worker 失败原因写入宿主日志`、`test: 插件入口导入冒烟用例`（两仓库）、`feat: 目录安装排除 .git`。

## 6. 风险

| 风险 | 应对 |
| --- | --- |
| detail 含用户路径等敏感文本进日志 | 按第 5 节日志脱敏规范；traceback 来自插件自身代码，宿主日志本就含路径类信息（如安装失败 message），风险与现状一致 |
| `import main` 冒烟在 CI 有顶层副作用 | 实施前人工核实；有副作用则改用 AST 扫描 `ECL.plugin_api` 禁用导入的静态用例兜底 |
| capture 与 copytree 忽略规则漂移 | 单一来源清单 + 测试断言两处引用同一常量 |
| 官方插件仓库 CI 依赖宿主 main 的时序 | 现状已如此（PYTHONPATH checkout main），不在本轮扩大 |

## 7. 实施与验证记录（2026-11-14）

三个决策点按默认执行：纳入 detail、只排除 `.git`（`__pycache__` 不动）、`remove_tree` 保留。三步均完成。

| 项目 | 结果 |
| --- | --- |
| 2.1 Worker 失败明细 | `_process.py`：`_report_failure` 增加可选 `detail`（协议异常取 `str(exc)`，入口异常取 `traceback.format_exc()`），白名单压平与前端契约不变；`supervisor.py`：error 帧含 detail 时经 `get_logger("PluginWorkerSupervisor")` 落 warning 日志，抛出的错误码契约不变；`lifecycle.py`：enable 与 reload 失败分支补 `logger.warning(..., exc_info=exc)` |
| 2.2 插件仓库冒烟 | 两仓库新增 `tests/test_entry_import.py`（import `main`；OnlineGame 另含 `onlinegame.remote_extensions`）；本地模拟 CI（PYTHONPATH 指宿主）通过；两仓库 AGENTS.md 登记入口顶层无副作用与冒烟拦截的约定 |
| 2.3 `.git` 排除 | `CodeSnapshot` 新增 `excluded_entry_names`（单一来源），capture 在链接/越界检查前过滤；`install_transaction.stage` 的 copytree 改用 `ignore_patterns(*excluded_entry_names)`；`stage` 既有暂存复核自动互证两边一致 |
| 测试修正 | `test_uninstall_removes_readonly_files_copied_from_repository` 前置（安装副本含 `.git`）随排除规则失效，改用普通只读插件文件保留 `remove_tree` 覆盖；新增用例：快照排除 `.git` 且摘要不受 `.git` 变化影响、`.git` 符号链接被忽略而非拒绝（Windows 无特权跳过）、目录安装副本无 `.git`、`_report_failure` 压平与 detail 行为、宿主日志含入口异常明细 |

验证：

- 定向：插件协议/引导/依赖/生命周期/快照/安装事务/开发模式共 81+44 项通过（首轮 2 处失败已按上述修正）。
- `ruff check ECL tests`、`ruff format --check ECL tests`（373 文件）通过。
- 两个插件仓库冒烟用例通过，ruff 通过。
- 全量 pytest：见提交前最终验证。
