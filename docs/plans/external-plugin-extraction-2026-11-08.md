# 外置插件迁出为独立仓库方案

- 日期：2026-11-08。
- 状态：已实施，插件仓库已推送，主仓库待提交。
- 决策者：用户。
- 关联：[ADR-003](../ADR-003-connector-and-qomicex-external-plugins.md)。

## 1. 背景与目标

`external_plugins/` 下的联机插件与 Qomicex 兼容插件按用户要求迁出主仓库，成为独立仓库：

- https://github.com/ECLTeam/ECL-Plugin-OnlineGame
- https://github.com/ECLTeam/ECL-Plugin-QomicexCompatible

主仓库不再携带插件源码、锁文件与专用工具；发布产物本就不打包这两个插件，因此发布链路不受影响。

## 2. 命名决策

插件内部标识与 GitHub 仓库名对齐：

- `ECL-OnlineGame` → `ECL-Plugin-OnlineGame`
- `Qomicex-Compatible` → `ECL-Plugin-QomicexCompatible`

已知代价：`plugin.json` 的 `name` 是已安装插件的识别标识，更名后旧安装的授权与设置不会自动迁移，用户需重新安装或手动迁移。

## 3. 迁移内容

| 项 | 处理 |
| --- | --- |
| 插件源码、锁文件、Florolding 第三方代码 | 复制进独立仓库，import 路径改为仓库根 |
| 7 个插件侧行为测试 | 随代码迁入对应仓库；`test_connector.py` 等 3 个文件改写 import |
| `ECL/tooling/external_plugins.py` | 删除（只服务这两个插件） |
| `backend-tests.yml` 下载并安装插件锁的步骤 | 删除 |
| `dependency-review.yml` 的插件锁路径过滤 | 删除 |
| `pyproject.toml` 的 florolding ruff 排除 | 删除 |
| `test_operation_logging.py` 的插件模型导入 | 改为本地 Pydantic 探针模型（验证的是宿主日志脱敏，与插件无关） |

## 4. 独立仓库新增

- `pyproject.toml`：与主仓库同一套 Ruff 规则；`pytest-asyncio` auto 模式与宿主一致；`ruff==0.16.10` 固定版本。
- 最小 CI：Ruff 检查 + pytest，push/PR/manual 触发，带 concurrency。
- `README.md`、`AGENTS.md`：安装、开发、标识不可随意变更的约束。
- OnlineGame 仓库保留 `package.py`（打包 `.eclplugin`，需宿主仓库在 PYTHONPATH）。

## 5. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 旧安装按旧标识授权，更名后重复授权 | README 与 ADR-003 状态行已说明；不做自动迁移 |
| 主仓库协议文档引用插件源码路径失效 | 改为指向插件仓库的 permalink |
| 插件 CI 首次运行环境差异 | 已按 CI 同版本依赖本地验证 |
| Florolding 第三方代码不再被主仓库 Ruff 排除 | 排除项随目录一并移入插件仓库 pyproject |

## 6. 验证

- 插件仓库本地：Ruff check/format 全过；OnlineGame pytest 51 passed、Qomicex 2 passed（Playwright/宿主无关，纯单元）。
- 主仓库：见提交信息中的全量验证记录。
- 插件仓库 CI 需推送后观察首轮运行。
