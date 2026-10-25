# 日常检查与发行构建分离

状态：已选择方案 A，待确认实施。日期：2026-10-25。

## 目标

| 操作 | 执行内容 |
| --- | --- |
| 推送 `main`、提交或更新面向 `main` 的 PR | 后端测试、前端质量检查，不编译平台安装包 |
| 推送 `v*` 版本标签 | 校验发布条件，完整打包并发布 |
| 手动运行 `Build` | 完整打包，提供 Actions 产物，不创建或更新 Release |

完整构建保留 Windows x64、Windows ARM64、Linux、macOS 和 Arch Linux 的既有流程。需要提前验证打包时手动运行；日常检查通过不代表平台打包已经通过。

## 改动点与顺序

1. 新增 `.github/workflows/frontend-checks.yml`，命名为 `Frontend Checks`。在 `main` 推送、面向 `main` 的 PR 和手动运行时，检出主仓库记录的子模块，安装锁定依赖并执行 `pnpm check`。使用现有 Node 22、pnpm 10；不编译后端。不添加路径过滤，保证发布提交自身有质量检查记录。
2. 修改 `.github/workflows/build.yml`：删除普通分支推送和 PR 触发，只保留 `v*` 标签推送与 `workflow_dispatch`。既有前端构建、平台打包和产物上传用于这两类触发。
3. 修改 Beta 标签门禁：同一提交必须通过主分支 `push` 触发的 `Backend Tests` 与 `Frontend Checks`，不再要求主分支 `Build`。继续校验带注释标签、版本格式、日期及远端 `main` 当前提交；检查记录缺失、未完成或失败均拒绝发布。
4. 给普通平台与 Arch 的 Release 上传增加事件限制：仅版本标签的 `push` 可以发布；手动运行即使选择标签，也只生成 Actions 产物。
5. 同步 `docs/guides/release-process.md`、`AGENTS.md` 的 CI 与 Beta 说明：创建标签前通过日常质量检查，标签构建及全部产物成功后才算发布完成。
6. 核对远端分支规则是否要求旧的构建检查；如存在依赖，列明需要替换的检查名称，避免 PR 永久等待已移除的工作流。远端管理权限不足时如实报告。

`backend-tests.yml`、现有 PR Ruff 工作流及子模块同步工作流的职责不变；本次不修改前端子仓库或应用运行代码。

## 验证与交付

- 用 actionlint 检查工作流语法；补充回归验证普通推送/PR 不触发完整构建，标签和手动运行可触发，手动运行不能发布。
- 验证 Beta 门禁绑定同一提交的两项主分支检查；成功放行，失败、排队和缺失记录拒绝，不再依赖主分支完整构建。
- CI 配置变更按 `AGENTS.md` 第 6 节执行主仓库全量检查：`ruff check ECL tests`、`ruff format --check ECL tests`、仓库级 Ruff 检查、`python -m pytest --tb=short -q`；执行 `frontend` 的 `pnpm check` 和 `pnpm build`，确认拆出的检查命令可运行。
- 检查文档、链接及 `git diff --check`。完成后提交并交付本地 `main`，在提交中记录验证结果。
- 本次选择方案不自动授权新的远端推送。获得推送授权后，验证普通推送仅启动日常检查；完整矩阵可手动验收，不为测试创建版本标签或 Release。未实际运行的远端步骤明确标为待验证。

实施完成后，现行规则保留在发布指南和项目规范，本方案按文档维护约定移出 `plans/`。
