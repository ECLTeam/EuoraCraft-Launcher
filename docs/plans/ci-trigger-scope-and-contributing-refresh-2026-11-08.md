# 检查触发范围拆分实施方案

状态：已实施，等待提交。

日期：2026-11-08。

## 1. 目标

1. PR 只运行与改动范围相关的检查，纯文档改动不再点亮全套流水线。
2. 依赖更新走独立工作流，不与页面扫描等无关检查混在同一次运行里。
3. 消除由遗留分支、浮动工具版本和重复运行造成的红灯噪声。

## 2. 约束

`packaging/release_metadata.py` 的 `_validate_main_checks` 要求发布提交最近一次由 `main` 推送触发的 `Backend Tests`（`backend-tests.yml`）与 `Frontend Checks`（`frontend-checks.yml`）成功。因此这两个工作流的文件名不能改，`push: main` 也不能加路径过滤，否则标签发布直接失败。PR 侧的路径判断只能放在 job 内完成，保证 push 与手动运行始终全量。

## 3. 现状问题

- `plugin-native-platforms.yml`、`plugin-worker-platforms.yml` 的 push 触发仍挂在已废弃的 `codex/plugin-posix-worker` 分支。
- `plugin-worker-platforms.yml` 的 `pull_request` 没有路径过滤，三平台矩阵对任何 PR 都运行。
- `governance.yml` 每个 PR 都跑 4 个 pip-audit job 加 31 场景可访问性扫描，与 PR 内容无关。
- `ruff.yml` 使用 `python-version: 3.x` 与不固定版本的 `pip install ruff`，linter 升级会让无关提交突然变红。
- 所有工作流都没有 `concurrency`，同一 PR 连续推送会堆积重复运行。

## 4. 改动点

| 文件 | 改动 |
| --- | --- |
| `backend-tests.yml` | 新增 `changes` 作业（`dorny/paths-filter`），`pytest` 作业在 PR 上按后端路径判定；push/手动保持全量 |
| `frontend-checks.yml` | 同上，按 `frontend` 指针与子目录判定 |
| `governance.yml` | PR 仅在 `docs/governance/**`、`ECL/tooling/governance.py`、扫描脚本、本工作流变动时运行；push main 与每周定时全量 |
| `dependency-review.yml`（新增） | 仅 PR，且依赖清单变动时审计：Python runtime/all、前端 production/all，复用 `governance.py audit` 与 `check` |
| `ruff.yml` | 改为 PR 且仅 Python 相关路径触发；固定 Python 3.12 与 `ruff==0.16.10` |
| `plugin-worker-platforms.yml` | 移除废弃分支，PR 补路径过滤 |
| `plugin-native-platforms.yml` | push 分支改为 `main` |
| `pyproject.toml` | `ruff` 由区间依赖改为固定版本，与工作流保持一致 |
| 全部工作流 | 增加 `concurrency` 取消同分支旧运行；补齐 `permissions: contents: read` 与超时 |

## 5. 触发矩阵

| 改动范围 | PR | 推送到 main |
| --- | --- | --- |
| 后端、测试、打包、原生、`pyproject.toml` | Backend Tests、Ruff Lint | 全量 |
| `frontend` 指针 | Frontend Checks | 全量 |
| 插件运行时路径 | Plugin Worker Platforms | 按路径 |
| 原生隔离路径 | Plugin Native WebViews | 按路径 |
| 依赖清单 | Dependency Review | 每周治理任务 |
| 治理配置与扫描脚本 | Engineering Governance | 每次推送 + 每周四 |
| 仅文档、模板 | 无 | — |

## 6. 验证

1. 全部工作流与模板用 PyYAML 解析通过，作业与触发结构符合预期。
2. 工作流内 `shell: python` 的内嵌脚本逐个编译通过。
3. 改动涉及依赖与 CI，按第 6 节执行后端全量验证：`ruff check ECL tests`、`ruff format --check ECL tests`、`ruff check .`、`python -m pytest --tb=short -q`（2911 passed / 58 skipped）。
4. 文档改动检查 Markdown 结构与 `git diff --check`。

## 7. 风险

| 风险 | 处理 |
| --- | --- |
| 新增目录未加入 PR 路径过滤器，检查被静默跳过 | 过滤器集中在各工作流 `changes` 作业内，新增顶层目录时同步更新；主分支推送仍全量兜底 |
| 可访问性回归只能由 main 推送和每周定时发现 | 已确认取舍：PR 不被整套扫描阻塞，界面回归在下一次主分支推送暴露 |
| 依赖审计在 PR 上只覆盖 Linux/Python 一侧 | 平台相关的完整矩阵仍由每周治理任务覆盖，PR 侧用于尽早发现清单问题 |
| 第三方 action `dorny/paths-filter` 引入供应链风险 | 仅用于读取改动文件列表，不接触凭据；由 Renovate 统一跟踪版本 |

## 8. 后续

`submodule.yml`、`build.yml` 的触发未改动：前者是推送后的子模块同步，后者只在标签和手动运行时触发，都不参与 PR 噪声。
